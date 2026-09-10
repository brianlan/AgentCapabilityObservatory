"""Dashboard rendering tests (#18).

Fixtures cover a normal sealed-and-scored trial, an execution-condition
anomaly trial, and a never-started trial (unknown telemetry). Static checks
cover semantic labels and keyboard-reachable controls; artifact tests cover
ID-only authorization and path-injection rejection.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from aco import db, runs
from aco.app import create_management_app

MGMT_TOKEN = "test-management-token"
MGMT_AUTH = {"Authorization": f"Bearer {MGMT_TOKEN}"}


@pytest.fixture()
def root(tmp_path):
    return tmp_path


@pytest.fixture()
def client(root):
    app = create_management_app(data_root=str(root), token=MGMT_TOKEN)
    return TestClient(app, headers=MGMT_AUTH)


@pytest.fixture()
def conn(root):
    conn = db.connect(root / "aco.db")
    db.migrate(conn)
    yield conn
    conn.close()


def seed(conn):
    """One experiment, four trials: normal sealed+scored, anomaly, cancelled
    before execution, untouched."""
    conn.execute(
        "INSERT INTO versions (id, kind, name, version, content, created_at) VALUES"
        " ('v-task', 'task', 'task', 'v1', '{}', 'now'),"
        " ('v-cfg', 'config', 'cfg', 'v1', '{}', 'now'),"
        " ('v-scorer', 'scorer', 'scorer', 'v1', '{}', 'now')"
    )
    conn.execute(
        "INSERT INTO experiments (id, status, requested, created_at)"
        " VALUES ('e1', 'planned', '{\"task\":{\"name\":\"task\",\"version\":\"v1\"}}', 'now')"
    )
    for tid, order in (("t-normal", 1), ("t-anomaly", 2), ("t-pending", 3)):
        conn.execute(
            "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
            " repetition, plan_order, status, requested) VALUES (?, 'e1', 'v-task', 'v-cfg', ?, ?, 'claimed', '{}')",
            (tid, order, order),
        )
    # explicitly cancelled before any run or submission (#16 lifecycle)
    conn.execute(
        "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
        " repetition, plan_order, status, requested) VALUES ('t-cancelled', 'e1', 'v-task', 'v-cfg', 4, 4, 'cancelled', '{}')"
    )
    conn.commit()
    # t-normal: observed run, submission, sealed answer, mixed verification history
    run_id = runs.create_run(conn, "t-normal", {"harness": "fake"}, supervisor_pid=1)
    conn.execute(
        "UPDATE trial_runs SET status='finished', adapter_version='0.3.1', harbor_version='0.22.0',"
        " image='agent:1', container_id='c1', exit_kind='normal', finished_at='now' WHERE run_id=?",
        (run_id,),
    )
    conn.execute(
        "INSERT INTO submissions (trial_id, idempotency_key, request_digest, receipt_id, status, created_at)"
        " VALUES ('t-normal', 'k1', 'd1', 'r-normal', 'sealed', 'now')"
    )
    conn.execute(
        "INSERT INTO sealed_answers (trial_id, run_id, receipt_id, digest, manifest, seal_trigger,"
        " trigger_at, frozen_at, copied_at, published_at, registered_at, status)"
        " VALUES ('t-normal', ?, 'r-normal', ?, ?, 'submit', 'now', 'now', 'now', 'now', 'now', 'sealed')",
        (run_id, "a" * 64, json.dumps({"files": [{"path": "workspace/answer.txt", "bytes": 8, "type": "file", "sha256": "b" * 64}], "changes": {"additions": 1}})),
    )
    conn.execute(
        "INSERT INTO submissions (trial_id, idempotency_key, request_digest, receipt_id, status, created_at)"
        " VALUES ('t-anomaly', 'k2', 'd2', 'r-anomaly', 'error', 'now')"
    )
    anomalous_run = runs.create_run(conn, "t-anomaly", {"harness": "fake"}, supervisor_pid=1)
    conn.execute(
        "UPDATE trial_runs SET status='error', exit_kind='timeout', exit_detail='agent exceeded deadline' WHERE run_id=?",
        (anomalous_run,),
    )
    conn.execute(
        "INSERT INTO sealed_answers (trial_id, run_id, receipt_id, digest, manifest, seal_trigger,"
        " trigger_at, frozen_at, copied_at, published_at, registered_at, status, anomaly)"
        " VALUES ('t-anomaly', ?, 'r-anomaly', ?, ?, 'timeout', 'now', 'now', 'now', 'now', 'now',"
        " 'anomaly', 'container was paused for too long; snapshot unreliable')",
        (anomalous_run, "c" * 64, json.dumps({"files": [], "changes": {}})),
    )
    # verification history: pass, conflicting re-score of the same version, and an error
    for vid, idem, status, passed, sub, ek, ed in (
        ("v1", "s1", "succeeded", 1, '{"accuracy": 0.9}', None, None),
        ("v2", "s2", "succeeded", 0, '{"accuracy": 0.2}', None, None),  # same scorer version, disagrees
        ("v3", "s3", "error", None, None, "infra_error", "docker daemon unreachable"),
    ):
        conn.execute(
            "INSERT INTO verifications (id, trial_id, idempotency_key, request_digest, scorer_version_id,"
            " status, pass, submetrics, error_kind, error_detail, created_at)"
            " VALUES (?, 't-normal', ?, 'rd', 'v-scorer', ?, ?, ?, ?, ?, 'now')",
            (vid, idem, status, passed, sub, ek, ed),
        )
    # per-execution attempt records (0008): a succeeded attempt and one
    # interrupted by a manager restart
    conn.execute(
        "INSERT INTO verification_attempts (id, verification_id, attempt_no, status, pass,"
        " submetrics, started_at, finished_at)"
        " VALUES ('a1', 'v1', 1, 'succeeded', 1, '{\"accuracy\": 0.9}', 't0', 't1')")
    conn.execute(
        "INSERT INTO verification_attempts (id, verification_id, attempt_no, status,"
        " error_kind, error_detail, started_at, finished_at)"
        " VALUES ('a3', 'v3', 1, 'error', 'infra_error', 'interrupted by manager restart',"
        " 't0', 't1')")
    conn.commit()
    return {"normal_run": run_id, "anomaly_run": anomalous_run}


def test_batch_list_and_detail_render_progress(client, conn):
    seed(conn)
    home = client.get("/dashboard")
    assert home.status_code == 200
    assert "/dashboard/experiments/e1" in home.text

    detail = client.get("/dashboard/experiments/e1")
    assert detail.status_code == 200
    for trial_id in ("t-normal", "t-anomaly", "t-pending"):
        assert f"/dashboard/trials/{trial_id}" in detail.text
    assert "查看证据链" in detail.text


def test_trial_detail_distinguishes_requested_observed_unknown(client, conn):
    seed(conn)
    page = client.get("/dashboard/trials/t-normal")
    assert page.status_code == 200
    # observed facts render with their values
    assert "0.3.1" in page.text and "agent:1" in page.text
    # requested plan snapshot renders as its own section
    assert "请求配置（计划快照）" in page.text
    # provider-internal identity is explicitly not claimed
    assert "provider 内部" in page.text

    untouched = client.get("/dashboard/trials/t-pending")
    assert untouched.status_code == 200
    # unknown stays unknown: no invented zeros or request values
    assert "未知" in untouched.text
    assert "0.3.1" not in untouched.text and "agent:1" not in untouched.text


def test_all_verification_attempts_and_errors_visible(client, conn):
    seed(conn)
    page = client.get("/dashboard/trials/t-normal")
    # pass, failing re-score, and the error row are all present
    assert "通过" in page.text and "未通过" in page.text
    assert "infra_error: docker daemon unreachable" in page.text
    # same-version disagreement is surfaced, never silently resolved
    assert "同版本评分结果冲突" in page.text
    # per-execution attempt history is visible, including a restart-interrupted one
    assert "#1 succeeded" in page.text
    assert "interrupted by manager restart" in page.text


def test_eligibility_and_anomaly_visible(client, conn):
    seed(conn)
    normal = client.get("/dashboard/trials/t-normal").text
    assert "执行条件合格" in normal

    anomaly = client.get("/dashboard/trials/t-anomaly").text
    assert "执行条件异常" in anomaly
    assert "container was paused for too long" in anomaly
    assert "不计入能力曲线" in anomaly


def test_cancelled_trial_rendering(client, conn):
    seed(conn)
    detail = client.get("/dashboard/experiments/e1")
    assert detail.status_code == 200
    assert 'href="/dashboard/trials/t-cancelled"' in detail.text  # navigable like any trial
    assert "badge-cancelled" in detail.text

    page = client.get("/dashboard/trials/t-cancelled")
    assert page.status_code == 200
    assert "已显式取消" in page.text
    assert "不计入能力统计" in page.text
    # cancellation is not a score or a sealed eligibility result
    assert "执行条件合格" not in page.text
    # batch progress carries the cancelled count in the per-status chips
    home = client.get("/dashboard").text
    assert "cancelled×1" in home


def test_observation_provenance_and_human_assistance_visible(client, conn):
    seed(conn)
    page = client.get("/dashboard/trials/t-normal").text
    # every observed value carries its source, matching supervisor semantics
    for source in ("监督进程上报", "按运行标签从容器运行时发现", "启动配置 + 运行时记录"):
        assert source in page
    # human assistance is explicit, not inferred: the schema records no flag
    assert "人工辅助" in page
    assert "未知（当前 schema 未记录人工辅助标志）" in page


def test_timeline_shows_seal_and_scoring_times(client, conn):
    seed(conn)
    page = client.get("/dashboard/trials/t-normal").text
    for label in ("任务打开", "提交意图", "冻结（提交暂停）", "发布", "封存注册"):
        assert label in page


def test_manifest_download_by_digest_only(client, conn):
    seed(conn)
    ok = client.get(f"/dashboard/artifacts/answers/{'a' * 64}/manifest")
    assert ok.status_code == 200
    assert json.loads(ok.content)["files"][0]["path"] == "workspace/answer.txt"

    assert client.get(f"/dashboard/artifacts/answers/{'e' * 64}/manifest").status_code == 404
    # path-like or traversal ids never resolve to disk reads
    for hostile in ("..%2F..%2Fetc%2Fpasswd", "../../etc/passwd", "%2e%2e%2faco.db"):
        assert client.get(f"/dashboard/artifacts/answers/{hostile}/manifest").status_code == 404


def test_patch_download_by_trial_id_only(client, conn, root):
    run_id = seed(conn)["normal_run"]
    patch = root / "runs" / run_id / "diagnostics.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text("--- a/answer.txt\n+++ b/answer.txt\n")

    ok = client.get("/dashboard/artifacts/trials/t-normal/patch")
    assert ok.status_code == 200
    assert b"--- a/answer.txt" in ok.content

    assert client.get("/dashboard/artifacts/trials/t-pending/patch").status_code == 404
    assert client.get("/dashboard/artifacts/trials/nonexistent/patch").status_code == 404


def test_dashboard_gated_by_management_token(client, conn, root):
    seed(conn)
    # the dashboard belongs to the authenticated management surface (ADR 0001):
    # the management bearer reaches it, an unauthenticated request does not
    for url in ("/dashboard", "/dashboard/experiments/e1", "/dashboard/trials/t-normal"):
        resp = client.get(url)
        assert resp.status_code == 200
    from fastapi.testclient import TestClient

    from aco.app import create_management_app
    bare = TestClient(create_management_app(data_root=str(root), token=MGMT_TOKEN))
    assert bare.get("/dashboard").status_code == 401


def test_semantic_labels_and_keyboard_access(client, conn):
    seed(conn)
    for url in ("/dashboard", "/dashboard/experiments/e1", "/dashboard/trials/t-normal"):
        page = client.get(url).text
        assert "<h1" in page  # one page heading landmark
        assert 'scope="col"' in page  # table headers are programmatically associated
        assert "<caption>" in page  # each table is described
        assert 'lang="zh-CN"' in page
    detail = client.get("/dashboard/experiments/e1").text
    # keyboard-reachable controls: links carry visible text names
    assert 'href="/dashboard/trials/t-normal">查看证据链</a>' in detail
    trial_page = client.get("/dashboard/trials/t-normal").text
    assert 'aria-labelledby="h-verifications"' in trial_page
    # statuses carry text, never color alone: every badge contains a status word
    import re
    for badge in re.findall(r'class="badge[^"]*">([^<]+)<', trial_page):
        assert badge.strip(), "empty badge text"
    # downloads are plain links (keyboard operable), not script-driven buttons
    assert 'href="/dashboard/artifacts/answers/' in trial_page
