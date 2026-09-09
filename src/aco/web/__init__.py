"""Server-rendered management dashboard (#18).

Read-only HTML pages inside the same FastAPI app: batch list, batch detail,
and trial detail. Pages render what the domain queries return — eligibility
and verification summaries come from the same tables the API reads; no
capability rule, eligibility rule, or "best result" selection is re-implemented
here or in the browser. Artifact downloads address database IDs only; host
paths are never accepted from the URL.
"""

import json
import re
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from starlette.templating import Jinja2Templates

from .. import results
from .. import runs
from ..api.verifications import list_verifications
from ..app import AppError, TRIAL_SELECT, get_experiment
from pathlib import Path

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))  # autoescape on by default

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")  # digests are content addresses, not paths


def batch_rows(conn) -> list[dict]:
    """One read model row per experiment: plan/execution/seal/verification progress."""
    experiments = conn.execute(
        "SELECT id, status, created_at FROM experiments ORDER BY created_at, id"
    ).fetchall()
    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT experiment_id, status, COUNT(*) AS n FROM trials GROUP BY experiment_id, status"
    ):
        counts.setdefault(row["experiment_id"], {})[row["status"]] = row["n"]
    seals: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT t.experiment_id AS eid, sa.status AS status, COUNT(*) AS n"
        " FROM sealed_answers sa JOIN trials t ON t.id = sa.trial_id"
        " GROUP BY t.experiment_id, sa.status"
    ):
        seals.setdefault(row["eid"], {})[row["status"]] = row["n"]
    scores: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT t.experiment_id AS eid, v.status AS status, COUNT(*) AS n"
        " FROM verifications v JOIN trials t ON t.id = v.trial_id"
        " GROUP BY t.experiment_id, v.status"
    ):
        scores.setdefault(row["eid"], {})[row["status"]] = row["n"]
    return [
        {
            "id": e["id"],
            "status": e["status"],
            "created_at": e["created_at"],
            "trials_total": sum(counts.get(e["id"], {}).values()),
            "trial_counts": counts.get(e["id"], {}),
            "sealed": seals.get(e["id"], {}).get("sealed", 0),
            "anomalies": seals.get(e["id"], {}).get("anomaly", 0),
            "scored": scores.get(e["id"], {}).get("succeeded", 0),
            "score_errors": scores.get(e["id"], {}).get("error", 0),
        }
        for e in experiments
    ]


def trial_summary(conn, experiment_id: str) -> list[dict]:
    """Trials of one experiment with per-trial verification counters."""
    rows = conn.execute(
        f"{TRIAL_SELECT} WHERE t.experiment_id = ? ORDER BY t.plan_order", (experiment_id,)
    ).fetchall()
    stats: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT trial_id, status, COUNT(*) AS n FROM verifications GROUP BY trial_id, status"
    ):
        s = stats.setdefault(row["trial_id"], {"total": 0, "succeeded": 0, "error": 0, "open": 0})
        s["total"] += row["n"]
        if row["status"] in ("succeeded", "error"):
            s[row["status"]] += row["n"]
        else:
            s["open"] += row["n"]
    sealed = {
        r["trial_id"]: r["status"]
        for r in conn.execute("SELECT trial_id, status FROM sealed_answers")
    }
    return [
        {
            "id": r["id"],
            "plan_order": r["plan_order"],
            "repetition": r["repetition"],
            "status": r["status"],
            "task": f'{r["task_name"]}@{r["task_version"]}',
            "config": f'{r["config_name"]}@{r["config_version"]}',
            "seal": sealed.get(r["id"]),
            "verifications": stats.get(r["id"], {"total": 0, "succeeded": 0, "error": 0, "open": 0}),
        }
        for r in rows
    ]


def _run_view(run: dict) -> dict:
    """Requested launch profile next to what was actually observed, each
    observation annotated with where the recorded value came from (the
    supervisor's own record: observe_run / mark_running / label discovery).
    Unknowns stay unknown."""
    observed = [
        ("adapter 版本", run["adapter_version"], "监督进程上报（observe_run）"),
        ("harbor 版本", run["harbor_version"], "监督进程上报（固定版本）"),
        ("容器镜像", run["image"], "启动配置 + 运行时记录（mark_running）"),
        ("容器 ID", run["container_id"], "按运行标签从容器运行时发现（discover_container）"),
    ]
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "requested_profile": json.dumps(run["requested_profile"], ensure_ascii=False, indent=2),
        "observed": observed,
        "phases": run["phases"],
        "exit_kind": run["exit_kind"],
        "exit_detail": run["exit_detail"],
        "log_dir": run["log_dir"],
        "launched_at": run["launched_at"],
        "finished_at": run["finished_at"],
    }


def trial_detail(conn, root: Path, trial_id: str) -> dict:
    """The full evidence chain of one trial, for rendering only."""
    row = conn.execute(f"{TRIAL_SELECT} WHERE t.id = ?", (trial_id,)).fetchone()
    if row is None:
        raise AppError(404, "not_found", f"trial {trial_id} does not exist")
    submission = conn.execute(
        "SELECT receipt_id, status, created_at FROM submissions WHERE trial_id = ?", (trial_id,)
    ).fetchone()
    sealed = conn.execute("SELECT * FROM sealed_answers WHERE trial_id = ?", (trial_id,)).fetchone()
    manifest = json.loads(sealed["manifest"]) if sealed else None
    patch_available = bool(sealed) and (root / "runs" / sealed["run_id"] / "diagnostics.patch").is_file()
    verifications = list_verifications(conn, trial_id)
    timeline = [
        ("任务打开", row["opened_at"]),
        ("提交意图", submission["created_at"] if submission else None),
        ("封存触发", sealed["trigger_at"] if sealed else None),
        ("冻结（提交暂停）", sealed["frozen_at"] if sealed else None),
        ("复制完成", sealed["copied_at"] if sealed else None),
        ("发布", sealed["published_at"] if sealed else None),
        ("封存注册", sealed["registered_at"] if sealed else None),
    ]
    return {
        "id": trial_id,
        "experiment_id": row["experiment_id"],
        "status": row["status"],
        "plan_order": row["plan_order"],
        "repetition": row["repetition"],
        "task": {"name": row["task_name"], "version": row["task_version"]},
        "config": {"name": row["config_name"], "version": row["config_version"]},
        "requested": json.dumps(json.loads(row["requested"]), ensure_ascii=False, indent=2),
        # eligibility is displayed from the stored facts, never recomputed
        "seal": dict(sealed) if sealed else None,
        # the current schema records no human-assistance flag: it stays
        # explicitly unknown, never inferred as "none"
        "human_assistance": "未知（当前 schema 未记录人工辅助标志）",
        "manifest_files": manifest["files"] if manifest else [],
        "manifest_changes": manifest.get("changes") if manifest else None,
        "runs": [_run_view(r) for r in (runs.run_out(r) for r in runs.list_runs(conn, trial_id))],
        "submission": dict(submission) if submission else None,
        "timeline": timeline,
        "verifications": verifications,
        "patch_available": patch_available,
    }


def results_view(conn, task_set=None, config=None, scorer=None, view="raw", batch=None) -> dict:
    """Trend/matrix page model. All statistics and SVG coordinates are
    computed here — templates render values only, they never re-implement
    the aggregation (#19 reviewer checklist)."""
    data = results.collect(conn, task_set=task_set, config=config,
                           scorer=scorer, view=view, batch=batch)
    palette = ["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed", "#0891b2"]
    # shared x scale across all series: batch answering time
    times = sorted(t for t in (_parse_ts(p["batch_created_at"])
                               for s in data["series"] for p in s["points"]) if t)
    tmin, tmax = (times[0], times[-1]) if times else (None, None)

    def x_of(ts: str) -> float | None:
        t = _parse_ts(ts)
        if t is None or tmin is None:
            return None
        if tmax == tmin:
            return 330.0  # single batch: centered point
        return 50.0 + (t - tmin).total_seconds() / (tmax - tmin).total_seconds() * 570.0

    def y_of(score: float) -> float:
        return 15.0 + (1.0 - score) * 170.0

    series_views = []
    for index, s in enumerate(data["series"]):
        color = palette[index % len(palette)]
        main_segments, marks = [], []
        previous_x = previous_y = None
        for p in s["points"]:
            x = x_of(p["batch_created_at"])
            lower, upper = p["bounds"]["lower"], p["bounds"]["upper"]
            if x is None or lower is None:
                continue
            y_low, y_up = y_of(lower), y_of(upper if upper is not None else lower)
            tooltip = (f'{p["batch_created_at"]} '
                       f'main={p["main_score"] if p["main_score"] is not None else "—"} '
                       f'bounds=[{lower:.2f}, {upper if upper is not None else lower:.2f}] '
                       f'coverage={p["coverage"]}')
            marks.append(f'<line x1="{x:.1f}" y1="{y_low:.1f}" x2="{x:.1f}" y2="{y_up:.1f}" '
                         f'stroke="{color}" stroke-width="3" opacity="0.35">'
                         f'<title>{tooltip}</title></line>')
            if p["main_score"] is not None:
                y = y_of(p["main_score"])
                marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}">'
                             f'<title>{tooltip}</title></circle>')
                if previous_x is not None:
                    main_segments.append(f'<line x1="{previous_x:.1f}" y1="{previous_y:.1f}" '
                                         f'x2="{x:.1f}" y2="{y:.1f}" stroke="{color}" stroke-width="2"/>')
                previous_x, previous_y = x, y
            else:
                previous_x = previous_y = None
        # x tick labels: one per batch position (deduplicated)
        series_views.append({
            "key": s["key"], "color": color, "marks": marks,
            "points": s["points"],
            "diagnostics": [_diagnostics(p) for p in s["points"]],
        })
    x_ticks = sorted({round(x_of(p["batch_created_at"]), 1)
                      for s in data["series"] for p in s["points"] if x_of(p["batch_created_at"]) is not None})
    matrix = data["matrix"]
    return {
        "data": data,
        "series_views": series_views,
        "x_ticks": x_ticks,
        "x_labels": {round(x_of(p["batch_created_at"]), 1): p["batch_created_at"][:16]
                     for s in data["series"] for p in s["points"]
                     if x_of(p["batch_created_at"]) is not None},
        "filters": data["filters"] | {"view": view},
        "matrix_cols": sorted({cfg for row in matrix["cells"].values() for cfg in row})
        if matrix else [],
    }


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _diagnostics(point: dict) -> dict:
    """latency / token / cost shown separately with their source; a verifier
    that does not report them leaves them missing — never zero, and never
    part of the capability score."""
    submetrics = point["submetrics"]
    out = {}
    for label in ("latency", "token", "cost"):
        hits = {name: value for name, value in submetrics.items() if label in name}
        out[label] = ({"detail": "；".join(
            f'{name} mean={value["mean"]:.4g} (n={value["samples"]})'
            for name, value in hits.items()), "source": "verifier submetrics"}
            if hits else {"detail": "缺失（verifier 未上报）", "source": None})
    return out


def register_routes(app: FastAPI, conn, root: Path) -> None:
    @app.get("/dashboard")
    async def dashboard_home(request: Request):
        return templates.TemplateResponse(
            request, "batch_list.html", {"rows": batch_rows(conn)}
        )

    @app.get("/dashboard/experiments/{experiment_id}")
    async def dashboard_experiment(request: Request, experiment_id: str):
        experiment = get_experiment(conn, experiment_id)  # same query service as the API
        return templates.TemplateResponse(
            request, "batch_detail.html",
            {"experiment": experiment, "trials": trial_summary(conn, experiment_id)},
        )

    @app.get("/dashboard/trials/{trial_id}")
    async def dashboard_trial(request: Request, trial_id: str):
        data = trial_detail(conn, root, trial_id)
        return templates.TemplateResponse(request, "trial_detail.html", {"t": data})

    # trends + task × config matrix (#19): renders aco.results output only
    @app.get("/dashboard/results")
    async def dashboard_results(request: Request, task_set: str | None = None,
                                config: str | None = None, scorer: str | None = None,
                                view: str = "raw", batch: str | None = None):
        try:
            data = results_view(conn, task_set=task_set, config=config,
                                scorer=scorer, view=view, batch=batch)
        except AppError as exc:
            data = {"error": exc.message, "series_views": [], "data": {"series": [], "matrix": None},
                    "x_ticks": [], "x_labels": {}, "filters": {}}
        return templates.TemplateResponse(request, "results.html", data)

    # artifact reads address database IDs only (#18): the manifest is served
    # from the sealed_answers row; a host path in the URL can never reach disk
    @app.get("/dashboard/artifacts/answers/{digest}/manifest")
    async def dashboard_manifest(digest: str):
        if not _DIGEST_RE.fullmatch(digest):
            raise AppError(404, "not_found", "no sealed answer with this id")
        row = conn.execute(
            "SELECT manifest FROM sealed_answers WHERE digest = ?", (digest,)
        ).fetchone()
        if row is None:
            raise AppError(404, "not_found", "no sealed answer with this id")
        return Response(
            content=row["manifest"], media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="manifest-{digest[:12]}.json"'},
        )

    @app.get("/dashboard/artifacts/trials/{trial_id}/patch")
    async def dashboard_patch(trial_id: str):
        sealed = conn.execute(
            "SELECT run_id FROM sealed_answers WHERE trial_id = ?", (trial_id,)
        ).fetchone()
        if sealed is None:
            raise AppError(404, "not_found", "no sealed answer for this trial")
        path = (root / "runs" / sealed["run_id"] / "diagnostics.patch").resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise AppError(404, "not_found", "diagnostic patch not available")
        return FileResponse(
            path, media_type="text/x-diff", filename="diagnostics.patch",
        )
