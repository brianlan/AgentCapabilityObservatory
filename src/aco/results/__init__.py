"""Results query and aggregation (#19).

Server-side module that turns persisted experiment plans and append-only
verification records into chart/table-ready JSON. The API and the dashboard
both render what this module returns — statistics are never recomputed in
the browser or in templates.

Comparability rules (from the long-run comparison decision):

- Every denominator comes from the experiment plan (the trials table), never
  from successful scoring rows alone. Anomalies, cancellations, and pending
  work stay in the denominator and are counted separately — they never
  default to zero and never disappear.
- A capability main score is labeled only when every planned sample of the
  batch has a valid verdict. Otherwise the point reports fixed-weight bounds:
  lower = confirmed passes / planned, upper = (confirmed passes + unknown) /
  planned ("all unknown pass"). Bounds are missing bounds, not confidence
  intervals.
- Per task, the pass rate is over that task's planned repetitions; the batch
  score is the equal-weight mean over tasks — never a pooled average over
  trials, which would drift weight toward tasks with more repetitions.
- Series are split by task-set version, target config, and the exact scorer
  version that produced a verdict. Different task sets, configs, or grader
  versions are never merged into one line.

Raw vs unified views: the raw view shows what each scorer version said (one
series per observed scorer version — a re-evaluated trial appears under both
graders); the unified view reports only the explicitly chosen grader
version, ignoring rows from any other scorer version.

Verdict resolution per (trial, scorer version), append-only aware:

- re-evaluation appends rows; every *successful* verdict of the pair counts
  toward stability, and contradictory successful verdicts mark the pair
  unstable — the state is excluded from capability scores and surfaced in
  its own bucket instead of silently picking the latest result. Re-running
  until pass cannot erase the mark: only agreeing verdicts are stable;
- without a contradiction the latest row (created_at, then insertion order)
  is the current verdict and supersedes older ones — stale rows never
  influence verdicts, counts, pass rates, bounds, or submetrics;
- latest row succeeded -> a valid verdict (pass 0/1, including a failing
  one);
- latest row errored -> scoring error (unknown, counted);
- no rows for this scorer version -> pending / anomaly / cancelled per the
  trial and seal state.

Ceiling: a batch forms a series point only when at least one of its matching
trials has a scoring row for the series' scorer version; completely
unscored batches are visible in the batch list, not as zero-coverage points.
"""

import json
import sqlite3
from collections import defaultdict

from ..app import AppError

TRIAL_FACTS_SELECT = (
    "SELECT t.id AS trial_id, t.experiment_id, t.status AS trial_status,"
    " t.opened_at, tv.name AS task_name, tv.version AS task_version,"
    " cv.name AS config_name, cv.version AS config_version,"
    " e.created_at AS batch_created_at, e.requested AS exp_requested"
    " FROM trials t"
    " JOIN experiments e ON e.id = t.experiment_id"
    " JOIN versions tv ON tv.id = t.task_version_id"
    " JOIN versions cv ON cv.id = t.config_version_id"
)

BUCKETS = ("valid", "unstable", "score_error", "anomaly", "cancelled", "pending")


def parse_ref(value: str) -> tuple[str, str]:
    """'name@version' -> ('name', 'version'). The version may contain '@'."""
    name, _, version = value.rpartition("@")
    if not name or not version:
        raise AppError(422, "invalid_selection", f"expected 'name@version', got {value!r}")
    return name, version


def _task_set_of(requested: dict) -> str:
    ref = requested.get("suite") or requested.get("task")
    return f'{ref["name"]}@{ref["version"]}'


def _verdicts(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Resolve one verdict state per (trial, scorer version) from append-only
    rows, grouped by trial id. All successful verdicts of a pair decide its
    stability: contradictory successful verdicts mark the pair unstable —
    excluded from capability scores instead of "latest wins". Without a
    contradiction the latest row (created_at, then insertion order) is the
    current verdict and supersedes older ones."""
    rows = conn.execute(
        "SELECT v.trial_id, v.scorer_version_id, v.status, v.pass, v.submetrics,"
        " v.finished_at, sv.name AS scorer_name, sv.version AS scorer_version"
        " FROM verifications v JOIN versions sv ON sv.id = v.scorer_version_id"
        " ORDER BY v.created_at, v.rowid"
    ).fetchall()
    history: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        history.setdefault((row["trial_id"], row["scorer_version_id"]), []).append(row)
    by_trial: dict[str, list[dict]] = defaultdict(list)
    for (trial_id, _scorer_version_id), rows_of_pair in history.items():
        last = rows_of_pair[-1]
        verdicts = {
            json.dumps({"pass": row["pass"], "submetrics": row["submetrics"]}, sort_keys=True)
            for row in rows_of_pair if row["status"] == "succeeded"
        }
        by_trial[trial_id].append({
            "trial_id": trial_id,
            "scorer": f'{last["scorer_name"]}@{last["scorer_version"]}',
            "unstable": len(verdicts) > 1,
            "pass": bool(last["pass"]) if last["status"] == "succeeded" else None,
            "score_error": last["status"] == "error",
            "submetrics": [json.loads(last["submetrics"])] if last["submetrics"] else [],
            "finished_at": last["finished_at"],
        })
    return by_trial


def _classify(trial: dict, state: dict | None) -> str:
    """One bucket per trial within a series; buckets sum to the plan."""
    if state is not None:
        if state["unstable"]:
            return "unstable"
        if state["pass"] is not None:
            return "valid"
        if state["score_error"]:
            return "score_error"
    if trial["anomaly"]:
        return "anomaly"
    if trial["trial_status"] == "cancelled":
        return "cancelled"
    return "pending"


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _task_point(states: list[dict]) -> dict:
    """Per-task metrics over that task's planned repetitions. Unstable
    trials stay in the denominator, never count as passes or valid
    coverage, and suppress the batch's official main score."""
    planned = len(states)
    passes = sum(1 for s in states if s["bucket"] == "valid" and s["pass"])
    valid = sum(1 for s in states if s["bucket"] == "valid")
    unknown = planned - valid
    return {
        "planned": planned, "pass": passes, "valid": valid,
        "unstable": sum(1 for s in states if s["bucket"] == "unstable"),
        "lower": _rate(passes, planned),
        "upper": _rate(passes + unknown, planned),
        "coverage": _rate(valid, planned),
        "effective_rate": _rate(passes, valid),
        "complete": valid == planned,
        "trial_ids": [s["trial_id"] for s in states],
    }


def _batch_point(items: list[dict]) -> dict:
    """Equal-weight aggregation over tasks for one batch in one series."""
    by_task: dict[str, list[dict]] = defaultdict(list)
    counts = {bucket: 0 for bucket in BUCKETS}
    passes = 0
    submetric_samples: dict[str, list[float]] = defaultdict(list)
    opened, ends = [], []
    for item in items:
        by_task[f'{item["task_name"]}@{item["task_version"]}'].append(item["state"])
        counts[item["state"]["bucket"]] += 1
        if item["state"]["bucket"] == "valid":
            if item["state"]["pass"]:
                passes += 1
            for metrics in item["state"]["submetrics"]:
                for name, value in metrics.items():
                    submetric_samples[name].append(value)
        if item["opened_at"]:
            opened.append(item["opened_at"])
        if item["state"]["finished_at"]:
            ends.append(item["state"]["finished_at"])
        if item["published_at"]:
            ends.append(item["published_at"])
    tasks = {name: _task_point(states) for name, states in sorted(by_task.items())}
    planned = len(items)
    complete = all(t["complete"] for t in tasks.values())
    bounds = {
        "lower": _mean([t["lower"] for t in tasks.values()]),
        "upper": _mean([t["upper"] for t in tasks.values()]),
    }
    ends.sort()
    return {
        "planned": planned,
        "pass": passes,
        "counts": counts,
        "per_task": tasks,
        "complete": complete,
        "main_score": bounds["lower"] if complete else None,
        "coverage": _mean([t["coverage"] for t in tasks.values()]),
        "effective_rate": (
            _mean([t["effective_rate"] for t in tasks.values()])
            if all(t["effective_rate"] is not None for t in tasks.values()) else None
        ),
        "bounds": bounds,
        "submetrics": {
            name: {"mean": _mean(samples), "samples": len(samples)}
            for name, samples in sorted(submetric_samples.items())
        },
        "span": {"start": min(opened) if opened else None, "end": ends[-1] if ends else None},
        "trial_ids": [item["trial_id"] for item in items],
    }


def _empty_state(scorer: str, trial_id: str) -> dict:
    """State shape for a trial with no scoring row under this scorer version:
    it stays in the plan denominator as an unknown."""
    return {"trial_id": trial_id, "scorer": scorer, "pass": None, "unstable": False,
            "score_error": False, "submetrics": [], "finished_at": None}


def collect(conn: sqlite3.Connection, *, task_set: str | None = None, config: str | None = None,
            scorer: str | None = None, view: str = "raw",
            batch: str | None = None) -> dict:
    """Filter and aggregate all results into per-batch series points."""
    if view not in ("raw", "unified"):
        raise AppError(422, "invalid_selection", f"unknown view: {view!r}")
    if view == "unified" and not scorer:
        raise AppError(422, "invalid_selection",
                       "the unified re-evaluation view requires an explicit scorer 'name@version'")

    anomalies = {row["trial_id"] for row in conn.execute(
        "SELECT trial_id FROM sealed_answers WHERE status = 'anomaly'")}
    published = {row["trial_id"]: row["published_at"] for row in conn.execute(
        "SELECT trial_id, published_at FROM sealed_answers WHERE published_at IS NOT NULL")}
    verdicts_by_trial = _verdicts(conn)
    verdict_by_trial_scorer = {(state["trial_id"], state["scorer"]): state
                               for states in verdicts_by_trial.values() for state in states}

    for value, label in ((task_set, "task_set"), (config, "config")):
        if value and "@" not in value:
            raise AppError(422, "invalid_selection",
                           f"expected 'name@version' for {label}, got {value!r}")
    if scorer:
        parse_ref(scorer)

    # (task_set, config) -> batch id -> all planned trials of that group; the
    # plan is the denominator, so every trial joins regardless of scoring state
    groups: dict[tuple[str, str], dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in conn.execute(f"{TRIAL_FACTS_SELECT} ORDER BY e.created_at, t.plan_order"):
        trial = dict(row)
        trial["anomaly"] = trial["trial_id"] in anomalies
        trial["published_at"] = published.get(trial["trial_id"])
        if batch and trial["experiment_id"] != batch:
            continue
        ts = _task_set_of(json.loads(trial["exp_requested"]))
        if task_set and ts != task_set:
            continue
        cfg = f'{trial["config_name"]}@{trial["config_version"]}'
        if config and cfg != config:
            continue
        groups[(ts, cfg)][trial["experiment_id"]].append(trial)

    # series key -> batch id -> items; raw splits by every observed scorer
    # version (a batch scored by no grader at all forms an explicitly
    # unscored series so anomalies and cancellations never disappear),
    # unified uses exactly the chosen one — with a point only when that
    # grader version actually has rows in the batch.
    series_map: dict[tuple, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for (ts, cfg), batches in groups.items():
        for eid, group_trials in batches.items():
            observed = {state["scorer"] for t in group_trials
                        for state in verdicts_by_trial.get(t["trial_id"], [])}
            if view == "unified":
                scorers_here = [scorer]
            elif scorer:
                scorers_here = sorted(observed & {scorer})
            else:
                scorers_here = sorted(observed) or [None]  # None = unscored batch
            for scr in scorers_here:
                items = []
                has_rows = False
                for t in group_trials:
                    state = verdict_by_trial_scorer.get((t["trial_id"], scr))
                    if state is not None:
                        has_rows = True
                        state = dict(state)
                    else:
                        state = _empty_state(scr, t["trial_id"])
                    items.append(t | {"state": state | {"bucket": _classify(t, state)}})
                if not has_rows and scr is not None:
                    continue  # no scoring rows for this grader version in this batch
                series_map[(ts, cfg, scr)][eid] = items

    series = []
    for key, batches in sorted(series_map.items(),
                               key=lambda item: (item[0][0], item[0][1], item[0][2] or "")):
        ts, cfg, scr = key
        points = []
        for eid, items in sorted(
                batches.items(), key=lambda pair: pair[1][0]["batch_created_at"]):
            points.append({"batch_id": eid,
                           "batch_created_at": items[0]["batch_created_at"],
                           **_batch_point(items)})
        series.append({"key": {"task_set": ts, "config": cfg, "scorer": scr},
                       "points": points})

    matrix = None
    task_sets = {s["key"]["task_set"] for s in series}
    if len(task_sets) == 1:
        matrix = _matrix(series)
    return {
        "view": view,
        "filters": {"task_set": task_set, "config": config, "scorer": scorer, "batch": batch},
        "series": series,
        "matrix": matrix,
    }


def _matrix(series: list[dict]) -> dict | None:
    """Task × config matrix pooled over batches. Cells are only comparable
    under one grading standard, so the matrix requires exactly one scorer
    version across the filtered series."""
    scorers = {s["key"]["scorer"] for s in series}
    if len(scorers) != 1 or None in scorers:
        return None
    cells: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"planned": 0, "pass": 0, "valid": 0, "trial_ids": []})
    for s in series:
        for point in s["points"]:
            for task_name, task in point["per_task"].items():
                cell = cells[(task_name, s["key"]["config"])]
                cell["planned"] += task["planned"]
                cell["pass"] += task["pass"]
                cell["valid"] += task["valid"]
                cell["trial_ids"] += task["trial_ids"]
    rows: dict[str, dict] = defaultdict(dict)
    for (task_name, cfg), cell in sorted(cells.items()):
        unknown = cell["planned"] - cell["valid"]
        rows[task_name][cfg] = {
            "planned": cell["planned"], "pass": cell["pass"], "valid": cell["valid"],
            "lower": _rate(cell["pass"], cell["planned"]),
            "upper": _rate(cell["pass"] + unknown, cell["planned"]),
            "coverage": _rate(cell["valid"], cell["planned"]),
            "complete": cell["valid"] == cell["planned"],
            "trial_ids": cell["trial_ids"],
        }
    return {
        "scorer": next(iter(scorers)),
        "cells": dict(rows),
        "note": "pooled over batches under one grader version; bounds are missing bounds, not confidence intervals",
    }
