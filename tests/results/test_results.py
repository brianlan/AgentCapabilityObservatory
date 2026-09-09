"""Results query, aggregation, and dashboard rendering tests (#19).

All fixtures are hand-computed: the expected equal-weight averages, coverage
fractions, and missing bounds are written next to the assertions. The
denominator is always the experiment plan (planned repetitions), never the
successful scoring rows.
"""

import json
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from aco.app import create_app

TASK_CONTENT = {"prompt": "What is 2+2?", "expected_answer": "4"}
SCORER_CONTENT = {"image": f"registry.test/verifier@sha256:{'a' * 64}",
                  "entrypoint": ["python", "run.py"], "result_schema": "v1"}


def make_client(tmp_path):
    return TestClient(create_app(data_root=str(tmp_path)))


def register_versions(client, tasks=(), configs=(), scorers=(), suites=()):
    ids = {}
    for name in tasks:
        resp = client.post("/v1/versions", json={"kind": "task", "name": name, "version": "v1",
                                                 "content": TASK_CONTENT})
        assert resp.status_code == 201, resp.text
        ids[f"task:{name}"] = resp.json()["id"]
    for name in configs:
        resp = client.post("/v1/versions", json={
            "kind": "config", "name": name, "version": "v1",
            "content": {"harness": "opencode", "model": f"model-{name}", "credentials": []}})
        assert resp.status_code == 201, resp.text
        ids[f"config:{name}"] = resp.json()["id"]
    for name in scorers:
        resp = client.post("/v1/versions", json={"kind": "scorer", "name": name, "version": "v1",
                                                 "content": SCORER_CONTENT})
        assert resp.status_code == 201, resp.text
        ids[f"scorer:{name}"] = resp.json()["id"]
    for name, tasks_in_suite in suites:
        resp = client.post("/v1/versions", json={
            "kind": "suite", "name": name, "version": "v1",
            "content": {"tasks": [{"name": t, "version": "v1"} for t in tasks_in_suite]}})
        assert resp.status_code == 201, resp.text
        ids[f"suite:{name}"] = resp.json()["id"]
    return ids


def make_experiment(client, *, task=None, suite=None, configs=("cfg-a",), repetitions=1):
    body = {"targets": [{"name": c, "version": "v1"} for c in configs],
            "repetitions": repetitions}
    if suite:
        body["suite"] = {"name": suite, "version": "v1"}
    else:
        body["task"] = {"name": task, "version": "v1"}
    resp = client.post("/v1/experiments", json=body)
    assert resp.status_code == 202, resp.text
    return resp.json()


def add_verification(root, trial_id, scorer_id, *, pass_=1, status="succeeded",
                     submetrics=None, idempotency_key=None, created_at="2026-01-01T00:00:00+00:00",
                     finished_at=None):
    conn = sqlite3.connect(root / "aco.db")
    conn.execute(
        "INSERT INTO verifications (id, trial_id, idempotency_key, request_digest,"
        " scorer_version_id, status, pass, submetrics, created_at, finished_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, trial_id, idempotency_key or uuid.uuid4().hex, "d" * 64,
         scorer_id, status, pass_, json.dumps(submetrics) if submetrics else None,
         created_at, finished_at))
    conn.commit()
    conn.close()


def seal_anomaly(root, trial_id):
    """An execution-condition anomaly seal (no valid verdict possible)."""
    conn = sqlite3.connect(root / "aco.db")
    conn.execute(
        "INSERT INTO trial_runs (run_id, trial_id, status, requested_profile, launched_at, created_at)"
        " VALUES (?, ?, 'finished', '{}', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (uuid.uuid4().hex, trial_id))
    conn.execute(
        "INSERT INTO sealed_answers (trial_id, run_id, receipt_id, digest, manifest,"
        " seal_trigger, trigger_at, frozen_at, copied_at, published_at, registered_at, status, anomaly)"
        " VALUES (?, (SELECT run_id FROM trial_runs WHERE trial_id = ?), ?, ?, '{}',"
        " 'exit', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',"
        " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',"
        " 'anomaly', 'container vanished during sealing')",
        (trial_id, trial_id, uuid.uuid4().hex, uuid.uuid4().hex))
    conn.commit()
    conn.close()


def cancel_trial(root, trial_id):
    conn = sqlite3.connect(root / "aco.db")
    conn.execute("UPDATE trials SET status = 'cancelled' WHERE id = ?", (trial_id,))
    conn.commit()
    conn.close()


def results(client, **params):
    resp = client.get("/v1/results", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def single_series(data):
    assert len(data["series"]) == 1, data["series"]
    return data["series"][0]


def test_equal_weight_across_tasks_not_pooled(tmp_path):
    """t1 has 2 planned repetitions, t2 has 1: the main score is the
    equal-weight mean (1.0 + 0.0) / 2 = 0.5 — never the pooled 2/3."""
    client = make_client(tmp_path)
    ids = register_versions(client, tasks=["t1", "t2"], configs=["cfg-a"],
                            scorers=["ver"], suites=[("pack", ["t1", "t2"])])
    exp = make_experiment(client, suite="pack", repetitions=2)
    # the plan expands uniformly (2 reps per task), so drop one planned t2
    # repetition to build the unequal case: t1×2, t2×1 in the same batch
    conn = sqlite3.connect(tmp_path / "aco.db")
    conn.execute("DELETE FROM trials WHERE repetition = 2 AND task_version_id = ?",
                 (ids["task:t2"],))
    conn.commit()
    conn.close()
    trials = [t for t in exp["trials"] if not (t["task"]["name"] == "t2" and t["repetition"] == 2)]
    assert len(trials) == 3
    for t in trials:
        if t["task"]["name"] == "t1":
            add_verification(tmp_path, t["id"], ids["scorer:ver"], pass_=1)
        else:
            add_verification(tmp_path, t["id"], ids["scorer:ver"], pass_=0)
    point = single_series(results(client))["points"][0]
    assert point["main_score"] == 0.5
    assert point["complete"] is True
    assert point["counts"] == {"valid": 3, "conflict": 0, "score_error": 0,
                               "anomaly": 0, "cancelled": 0, "pending": 0}
    assert point["per_task"]["t1@v1"]["pass"] == 2
    assert point["per_task"]["t1@v1"]["planned"] == 2
    assert point["per_task"]["t2@v1"]["pass"] == 0
    # bounds are equal-weight too: (1.0 + 0.0) / 2 — a pooled rate would be 2/3
    assert point["bounds"] == {"lower": 0.5, "upper": 0.5}


def test_unknown_samples_keep_denominator_and_show_bounds(tmp_path):
    """One of two planned repetitions is pending: no main score, coverage 0.5,
    lower 0.0, upper 0.5 ('all unknown pass')."""
    client = make_client(tmp_path)
    ids = register_versions(client, tasks=["arith"], configs=["cfg-a"], scorers=["ver"])
    exp = make_experiment(client, task="arith", repetitions=2)
    add_verification(tmp_path, exp["trials"][0]["id"], ids["scorer:ver"], pass_=0)
    point = single_series(results(client))["points"][0]
    assert point["complete"] is False
    assert point["main_score"] is None
    assert point["coverage"] == 0.5
    assert point["bounds"] == {"lower": 0.0, "upper": 0.5}
    assert point["counts"]["pending"] == 1
    assert point["counts"]["valid"] == 1


def test_anomaly_and_cancelled_stay_in_denominator(tmp_path):
    """Anomalies and cancellations never vanish: they stay planned-denominator
    unknowns with separate counts, and force bounds instead of a main score."""
    client = make_client(tmp_path)
    ids = register_versions(client, tasks=["t1", "t2"], configs=["cfg-a"], scorers=["ver"])
    ids = register_versions(client, suites=[("pack", ["t1", "t2"])])
    exp = make_experiment(client, suite="pack")
    t1, t2 = exp["trials"][0], exp["trials"][1]
    assert (t1["task"]["name"], t2["task"]["name"]) == ("t1", "t2")
    seal_anomaly(tmp_path, t1["id"])
    cancel_trial(tmp_path, t2["id"])
    point = single_series(results(client))["points"][0]
    assert point["planned"] == 2
    assert point["counts"]["anomaly"] == 1
    assert point["counts"]["cancelled"] == 1
    assert point["complete"] is False
    assert point["main_score"] is None
    assert point["bounds"] == {"lower": 0.0, "upper": 1.0}
    assert point["coverage"] == 0.0


def test_series_split_by_config_suite_and_scorer(tmp_path):
    """Different configs, suite versions, and grader versions are separate
    series — never merged into one line."""
    client = make_client(tmp_path)
    ids = register_versions(client, tasks=["arith"], configs=["cfg-a", "cfg-b"], scorers=["ver"])
    exp_a = make_experiment(client, task="arith", configs=["cfg-a"])
    exp_b = make_experiment(client, task="arith", configs=["cfg-b"])
    ids.update(register_versions(client, scorers=["ver2"]))
    for exp in (exp_a, exp_b):
        for t in exp["trials"]:
            add_verification(tmp_path, t["id"], ids["scorer:ver"], pass_=1)
            add_verification(tmp_path, t["id"], ids["scorer:ver2"], pass_=0)
    data = results(client)
    keys = {(s["key"]["config"], s["key"]["scorer"]) for s in data["series"]}
    assert keys == {("cfg-a@v1", "ver@v1"), ("cfg-b@v1", "ver@v1"),
                    ("cfg-a@v1", "ver2@v1"), ("cfg-b@v1", "ver2@v1")}
    # a different suite version would also be a different series
    register_versions(client, suites=[("pack", ["arith"])])
    exp_c = make_experiment(client, suite="pack", configs=["cfg-a"])
    for t in exp_c["trials"]:
        add_verification(tmp_path, t["id"], ids["scorer:ver"], pass_=1)
    data = results(client, config="cfg-a@v1", scorer="ver@v1")
    keys = {s["key"]["task_set"] for s in data["series"]}
    assert keys == {"arith@v1", "pack@v1"}


def test_raw_view_shows_regrade_under_both_graders_unified_picks_one(tmp_path):
    """A trial re-evaluated under a new grader version appears under both in
    the raw view; the unified view counts only the chosen grader version and
    refuses to run without an explicit one."""
    client = make_client(tmp_path)
    ids = register_versions(client, tasks=["arith"], configs=["cfg-a"], scorers=["v1", "v2"])
    exp = make_experiment(client, task="arith")
    trial = exp["trials"][0]
    add_verification(tmp_path, trial["id"], ids["scorer:v1"], pass_=1)
    add_verification(tmp_path, trial["id"], ids["scorer:v2"], pass_=0)
    raw = results(client)
    by_scorer = {s["key"]["scorer"]: s["points"][0]["pass"] for s in raw["series"]}
    assert by_scorer == {"v1@v1": 1, "v2@v1": 0}
    unified = results(client, view="unified", scorer="v2@v1")
    series = single_series(unified)
    assert series["key"]["scorer"] == "v2@v1"
    assert series["points"][0]["pass"] == 0
    assert series["points"][0]["main_score"] == 0.0
    assert client.get("/v1/results", params={"view": "unified"}).status_code == 422


def test_conflicting_same_version_verdicts_stay_unknown(tmp_path):
    """Two same-version successful verdicts that disagree are a conflict: no
    verdict is selected, the trial stays unknown and is counted."""
    client = make_client(tmp_path)
    ids = register_versions(client, tasks=["arith"], configs=["cfg-a"], scorers=["ver"])
    exp = make_experiment(client, task="arith")
    add_verification(tmp_path, exp["trials"][0]["id"], ids["scorer:ver"], pass_=1,
                     idempotency_key="first")
    add_verification(tmp_path, exp["trials"][0]["id"], ids["scorer:ver"], pass_=0,
                     idempotency_key="second")
    point = single_series(results(client))["points"][0]
    assert point["counts"]["conflict"] == 1
    assert point["counts"]["valid"] == 0
    assert point["complete"] is False
    assert point["main_score"] is None
    assert point["bounds"] == {"lower": 0.0, "upper": 1.0}


def test_partial_batch_is_marked_and_batches_form_time_points(tmp_path):
    """Two batches of the same series: the first is partial (one trial never
    scored), the second complete — points ordered by answering-batch time."""
    client = make_client(tmp_path)
    ids = register_versions(client, tasks=["arith"], configs=["cfg-a"], scorers=["ver"])
    exp1 = make_experiment(client, task="arith", repetitions=2)
    add_verification(tmp_path, exp1["trials"][0]["id"], ids["scorer:ver"], pass_=1,
                     created_at="2026-01-01T00:00:00+00:00")
    exp2 = make_experiment(client, task="arith", repetitions=2)
    for t in exp2["trials"]:
        add_verification(tmp_path, t["id"], ids["scorer:ver"], pass_=1,
                         created_at="2026-02-01T00:00:00+00:00")
    points = single_series(results(client))["points"]
    assert [p["batch_id"] for p in points] == [exp1["id"], exp2["id"]]
    assert points[0]["complete"] is False and points[0]["main_score"] is None
    assert points[0]["bounds"] == {"lower": 0.5, "upper": 1.0}
    assert points[1]["complete"] is True and points[1]["main_score"] == 1.0
    assert points[0]["batch_created_at"] < points[1]["batch_created_at"]


def test_matrix_pools_batches_under_one_grader_only(tmp_path):
    """The task × config matrix pools over batches — but only when a single
    grader version is in play; otherwise it is withheld entirely."""
    client = make_client(tmp_path)
    ids = register_versions(client, tasks=["arith"], configs=["cfg-a"], scorers=["ver"])
    for created_at in ("2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"):
        exp = make_experiment(client, task="arith", repetitions=2)
        for i, t in enumerate(exp["trials"]):
            add_verification(tmp_path, t["id"], ids["scorer:ver"],
                             pass_=1 if i == 0 else 0, created_at=created_at)
    data = results(client)
    cell = data["matrix"]["cells"]["arith@v1"]["cfg-a@v1"]
    assert (cell["pass"], cell["planned"], cell["valid"]) == (2, 4, 4)
    assert cell["lower"] == 0.5 and cell["upper"] == 0.5 and cell["complete"] is True
    assert len(cell["trial_ids"]) == 4
    # a second grader version in the same data blocks the matrix
    ids2 = register_versions(client, scorers=["ver2"])
    add_verification(tmp_path, exp["trials"][0]["id"], ids2["scorer:ver2"], pass_=1)
    assert results(client)["matrix"] is None
    # ...unless the filter pins one grader version
    pinned = results(client, scorer="ver@v1")
    assert pinned["matrix"]["scorer"] == "ver@v1"


def test_submetrics_reported_verifier_side_never_in_score(tmp_path):
    """Verifier-reported submetrics travel with the point (mean + sample count)
    and the capability main score is computed from pass/fail only."""
    client = make_client(tmp_path)
    ids = register_versions(client, tasks=["arith"], configs=["cfg-a"], scorers=["ver"])
    exp = make_experiment(client, task="arith", repetitions=2)
    add_verification(tmp_path, exp["trials"][0]["id"], ids["scorer:ver"], pass_=1,
                     submetrics={"accuracy": 0.5})
    add_verification(tmp_path, exp["trials"][1]["id"], ids["scorer:ver"], pass_=1,
                     submetrics={"accuracy": 1.0})
    point = single_series(results(client))["points"][0]
    assert point["main_score"] == 1.0
    assert point["submetrics"] == {"accuracy": {"mean": 0.75, "samples": 2}}


def test_filter_and_view_validation(tmp_path):
    client = make_client(tmp_path)
    register_versions(client, tasks=["arith"], configs=["cfg-a"], scorers=["ver"])
    assert client.get("/v1/results", params={"view": "bogus"}).status_code == 422
    assert client.get("/v1/results", params={"view": "unified"}).status_code == 422
    assert client.get("/v1/results", params={"task_set": "no-version-ref"}).status_code == 422
    assert client.get("/v1/results", params={"config": "also-bad"}).status_code == 422


def test_empty_results(tmp_path):
    client = make_client(tmp_path)
    data = results(client)
    assert data["series"] == [] and data["matrix"] is None
    assert data["view"] == "raw"


def test_batch_and_scorer_filters(tmp_path):
    client = make_client(tmp_path)
    ids = register_versions(client, tasks=["arith"], configs=["cfg-a"], scorers=["ver"])
    exp = make_experiment(client, task="arith")
    add_verification(tmp_path, exp["trials"][0]["id"], ids["scorer:ver"], pass_=1)
    data = results(client, batch=exp["id"])
    assert single_series(data)["points"][0]["batch_id"] == exp["id"]
    assert results(client, batch="missing")["series"] == []
    assert results(client, scorer="other@v1")["series"] == []


def test_dashboard_results_page(tmp_path):
    """The page renders the API's numbers verbatim: legend, SVG marks, matrix
    cells with drill-down links, raw counts, and missing diagnostics."""
    client = make_client(tmp_path)
    ids = register_versions(client, tasks=["t1", "t2"], configs=["cfg-a"],
                            scorers=["ver"], suites=[("pack", ["t1", "t2"])])
    exp = make_experiment(client, suite="pack", repetitions=2)
    for t in exp["trials"]:
        add_verification(tmp_path, t["id"], ids["scorer:ver"], pass_=1,
                         submetrics={"accuracy": 1.0})
    resp = client.get("/dashboard/results")
    assert resp.status_code == 200, resp.text
    html = resp.text
    assert "pack@v1 × cfg-a@v1 × ver@v1" in html
    assert '<svg viewBox="0 0 640 230"' in html
    assert "缺失（verifier 未上报）" in html  # latency/token/cost stay missing, not zero
    assert "accuracy" in html
    assert f'href="/dashboard/trials/{exp["trials"][0]["id"]}"' in html
    assert f'href="/dashboard/experiments/{exp["id"]}"' in html
    # matrix cell shows the bounds range and counts (2 reps × 2 tasks = 4 trials)
    assert "1.00–1.00" in html and "（2/2" in html
    # the page never re-implements statistics: everything comes from aco.results
    resp = client.get("/v1/results")
    assert resp.json()["series"][0]["points"][0]["main_score"] == 1.0


def test_dashboard_results_page_partial_batch(tmp_path):
    """Partial batches are visible as partial, with bounds and counts, and the
    unified-view error surfaces on the page instead of a broken render."""
    client = make_client(tmp_path)
    ids = register_versions(client, tasks=["arith"], configs=["cfg-a"], scorers=["ver"])
    exp = make_experiment(client, task="arith", repetitions=2)
    add_verification(tmp_path, exp["trials"][0]["id"], ids["scorer:ver"], pass_=1)
    html = client.get("/dashboard/results").text
    assert "否（部分结果）" in html
    assert "0.50–1.00" in html  # lower–upper bounds visible
    assert "覆盖 50%" in html
    html = client.get("/dashboard/results", params={"view": "unified"}).text
    assert "统一重评口径" in html  # error badge rendered, page not broken
