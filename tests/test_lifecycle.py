"""Unit tests for explicit cancel/resume, restart reconciliation, and timeout
verdicts (#16).

Docker is faked at the function boundary (container cleanup is recorded, not
executed); supervisor liveness uses real helper processes; everything else
runs against a real SQLite database. Real restart-during-execution coverage
lives in tests/e2e/test_execution.py.
"""

import json
import os
import sqlite3
import subprocess
import time

import pytest

from aco import artifacts, db, lifecycle, runs, supervisor, verification
from aco.execution import claim_next_planned, recover_stale_claims, reap_lost_supervisors


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "aco.db")
    db.migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from aco.app import create_management_app
    app = create_management_app(data_root=str(tmp_path), token="test-management-token")
    return TestClient(app, headers={"Authorization": "Bearer test-management-token"})


def make_experiment(conn, n_trials=2, status="planned") -> str:
    task_content = {
        "contract": {"required_outputs": ["/workspace/answer.txt"]},
        "contract_digest": artifacts.contract_digest(
            artifacts.ArtifactContract(required_outputs=("/workspace/answer.txt",))),
    }
    conn.execute(
        "INSERT INTO versions (id, kind, name, version, content, created_at)"
        " VALUES ('v-task', 'task', 'task', 'v1', ?, 'now'),"
        " ('v-cfg', 'config', 'cfg', 'v1', '{}', 'now')",
        (json.dumps(task_content),),
    )
    conn.execute(
        "INSERT INTO experiments (id, status, requested, created_at)"
        " VALUES ('e1', ?, '{}', 'now')", (status,)
    )
    for i in range(1, n_trials + 1):
        conn.execute(
            "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
            f" repetition, plan_order, requested) VALUES ('t{i}', 'e1', 'v-task', 'v-cfg', {i}, {i}, '{{}}')"
        )
    conn.commit()
    return "e1"


def seal_row(conn, trial_id, status="sealed", frozen_at="now"):
    run_id = conn.execute(
        "SELECT run_id FROM trial_runs WHERE trial_id = ?", (trial_id,)
    ).fetchone()["run_id"]
    conn.execute(
        "INSERT INTO sealed_answers (trial_id, run_id, receipt_id, digest, manifest,"
        " seal_trigger, trigger_at, frozen_at, copied_at, published_at, registered_at, status)"
        " VALUES (?, ?, 'r1', 'd1', '{}', 'submit', 'now', ?, 'now', 'now', 'now', ?)",
        (trial_id, run_id, frozen_at, status),
    )
    conn.commit()


class TestCancel:
    def test_cancel_unstarted_is_idempotent(self, client, conn, tmp_path):
        make_experiment(conn)
        resp = client.post("/v1/experiments/e1/cancel")
        assert resp.status_code == 200
        assert resp.json() == {"status": "cancelled", "cancelled": 2, "stopped": 0, "untouched": 0}
        assert dict(conn.execute("SELECT id, status FROM trials").fetchall()) == {"t1": "cancelled", "t2": "cancelled"}
        assert conn.execute("SELECT status FROM experiments WHERE id = 'e1'").fetchone()[0] == "cancelled"
        # idempotent repeat: same persisted summary, no new events
        events_before = conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0]
        resp2 = client.post("/v1/experiments/e1/cancel")
        assert resp2.status_code == 200
        assert resp2.json() == resp.json()
        assert conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0] == events_before

    def test_cancelled_trials_are_never_claimed(self, client, conn):
        make_experiment(conn, n_trials=1)
        client.post("/v1/experiments/e1/cancel")
        assert claim_next_planned(conn) is None

    def test_resume_does_not_revoke_cancel(self, client, conn):
        make_experiment(conn)
        client.post("/v1/experiments/e1/cancel")
        resp = client.post("/v1/experiments/e1/resume")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "experiment_cancelled"
        assert conn.execute("SELECT status FROM experiments WHERE id = 'e1'").fetchone()[0] == "cancelled"
        assert dict(conn.execute("SELECT id, status FROM trials").fetchall()) == {"t1": "cancelled", "t2": "cancelled"}

    def test_late_submit_on_cancelled_trial_401(self, client, conn):
        """A cancelled trial's capability expired: the old token can neither
        submit late nor replay (#12 reopen)."""
        make_experiment(conn, n_trials=1)
        token = client.post("/v1/trials/t1/session-token").json()["token"]
        client.post("/v1/experiments/e1/cancel")
        session_client = _session_client_for(conn)
        resp = session_client.post("/v1/session/submit",
                                   headers={"Authorization": f"Bearer {token}"},
                                   json={"idempotency_key": "k1"})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "session_expired"
        assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0

    def test_cancel_untouched_sealed_trial(self, client, conn):
        make_experiment(conn, n_trials=1)
        runs.create_run(conn, "t1", {}, supervisor_pid=-1)
        runs.finish_run(conn, runs.list_runs(conn, "t1")[0]["run_id"], "finished", "normal")
        conn.execute("UPDATE trials SET status = 'claimed' WHERE id = 't1'")
        seal_row(conn, "t1")
        conn.commit()
        resp = client.post("/v1/experiments/e1/cancel")
        assert resp.status_code == 200
        assert resp.json() == {"status": "cancelled", "cancelled": 0, "stopped": 0, "untouched": 1}
        # the sealed answer keeps its official eligibility: the trial is
        # terminal 'sealed', never retroactively cancelled
        assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "sealed"
        assert conn.execute("SELECT status FROM sealed_answers WHERE trial_id = 't1'").fetchone()[0] == "sealed"

    def test_cancel_stops_running_run_and_marks_anomaly(self, client, conn, monkeypatch):
        make_experiment(conn, n_trials=1)
        cleanups = []
        monkeypatch.setattr(supervisor, "cleanup_container", lambda run_id: cleanups.append(run_id))
        runs.create_run(conn, "t1", {}, supervisor_pid=-1)
        child = subprocess.Popen(["sleep", "30"])
        try:
            conn.execute("UPDATE trial_runs SET supervisor_pid = ?, status = 'running'", (child.pid,))
            conn.execute("UPDATE trials SET status = 'claimed' WHERE id = 't1'")
            conn.commit()
            resp = client.post("/v1/experiments/e1/cancel")
            assert resp.status_code == 200
            assert resp.json()["stopped"] == 1
            deadline = time.monotonic() + 5
            while child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert child.poll() is not None  # supervisor was terminated
        finally:
            child.kill()
            child.wait()
        assert cleanups == [runs.list_runs(conn, "t1")[0]["run_id"]]
        run = runs.get_run(conn, runs.list_runs(conn, "t1")[0]["run_id"])
        assert run["status"] == "error" and run["exit_kind"] == "cancelled"
        assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "cancelled"
        # the never-sealed answer became a diagnostic anomaly, not a capability sample
        row = conn.execute("SELECT status, anomaly FROM sealed_answers WHERE trial_id = 't1'").fetchone()
        assert row["status"] == "anomaly" and "cancelled" in row["anomaly"]

    def test_placeholder_pid_is_never_signalled(self, client, conn, monkeypatch):
        """pid -1/0 must never reach os.kill (it would signal a process group)."""
        make_experiment(conn, n_trials=1)
        monkeypatch.setattr(supervisor, "cleanup_container", lambda run_id: None)
        kills = []
        monkeypatch.setattr(lifecycle.os, "kill", lambda pid, sig: kills.append(pid))
        runs.create_run(conn, "t1", {}, supervisor_pid=-1)
        conn.execute("UPDATE trials SET status = 'claimed' WHERE id = 't1'")
        conn.commit()
        client.post("/v1/experiments/e1/cancel")
        assert kills == []  # placeholder pids are skipped, not signalled

    def test_cancel_unknown_experiment_404(self, client):
        resp = client.post("/v1/experiments/nope/cancel")
        assert resp.status_code == 404


class TestRestart:
    def test_restart_pauses_unstarted_and_resume_releases(self, client, conn):
        make_experiment(conn)
        lifecycle.pause_unstarted_on_restart(conn)
        assert conn.execute("SELECT status FROM experiments WHERE id = 'e1'").fetchone()[0] == "paused"
        assert claim_next_planned(conn) is None  # paused plans never start implicitly
        resp = client.post("/v1/experiments/e1/resume")
        assert resp.status_code == 200
        assert resp.json() == {"status": "running", "resumed": 2}
        assert claim_next_planned(conn) == "t1"
        # the first claim flips the experiment to running (state machine)
        assert conn.execute("SELECT status FROM experiments WHERE id = 'e1'").fetchone()[0] == "running"

    def test_resume_without_pause_is_idempotent_noop(self, client, conn):
        make_experiment(conn)
        resp = client.post("/v1/experiments/e1/resume")
        assert resp.status_code == 200
        assert resp.json() == {"status": "planned", "resumed": 0}

    def test_attempted_trial_survives_restart_without_rerun(self, conn):
        """Claimed + active run: adopted, never reset, never re-claimed (#16)."""
        make_experiment(conn, n_trials=1)
        run_id = runs.create_run(conn, "t1", {}, supervisor_pid=os.getpid())  # live pid
        conn.execute("UPDATE trials SET status = 'claimed' WHERE id = 't1'")
        conn.commit()
        adopted = lifecycle.adopt_live_supervisors(conn)
        assert adopted == 1
        assert runs.get_run(conn, run_id)["status"] == "launching"  # untouched
        recover_stale_claims(conn)
        reap_lost_supervisors(conn)
        assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "claimed"
        assert claim_next_planned(conn) is None  # no implicit second attempt
        event = conn.execute(
            "SELECT reason FROM lifecycle_events WHERE event = 'adopted'").fetchone()
        assert event["reason"] == "supervisor_survived_restart"

    def test_unknowable_start_is_never_rerun(self, conn):
        """Run in 'launching' with an unreadable pid: the trial ends as an
        execution-condition anomaly — explainable, terminal, never silently
        returned to the plan, never re-answered."""
        make_experiment(conn, n_trials=1)
        runs.create_run(conn, "t1", {}, supervisor_pid=-1)
        conn.execute("UPDATE trials SET status = 'claimed' WHERE id = 't1'")
        conn.commit()
        recover_stale_claims(conn)
        reap_lost_supervisors(conn)
        assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "anomaly"
        assert claim_next_planned(conn) is None
        run = runs.get_run(conn, runs.list_runs(conn, "t1")[0]["run_id"])
        assert run["exit_kind"] == "supervisor_lost"  # explainable, not rerun
        row = conn.execute(
            "SELECT status, seal_trigger FROM sealed_answers WHERE trial_id = 't1'").fetchone()
        assert row["status"] == "anomaly" and row["seal_trigger"] == "supervisor_lost"

    def test_stuck_running_verification_is_requeued(self, conn):
        make_experiment(conn, n_trials=1)
        conn.execute("INSERT INTO versions (id, kind, name, version, content, created_at)"
                     " VALUES ('sv', 'scorer', 'scorer', 'v1', '{}', 'now')")
        conn.execute(
            "INSERT INTO verifications (id, trial_id, idempotency_key, request_digest,"
            " scorer_version_id, status, started_at, created_at)"
            " VALUES ('v1', 't1', 'k1', 'd', 'sv', 'running', 'earlier', 'now')")
        conn.commit()
        assert verification.requeue_stuck_running(conn) == 1
        row = conn.execute("SELECT status, started_at FROM verifications WHERE id = 'v1'").fetchone()
        assert row["status"] == "queued" and row["started_at"] is None


class TestTerminationRaces:
    """Deterministic interleavings across separate SQLite connections (#16):
    submit/cancel/timeout race for one legal termination reason; the loser
    observes a stable explainable state and never rewrites history."""

    @pytest.fixture()
    def two_conns(self, tmp_path):
        conn = db.connect(tmp_path / "aco.db")
        db.migrate(conn)
        other = db.connect(tmp_path / "aco.db")  # second process-style connection
        yield conn, other
        conn.close()
        other.close()

    def test_seal_wins_cancel_keeps_official_answer(self, two_conns, monkeypatch):
        """Seal registered first: cancel stops the run as diagnostics but the
        trial keeps its official answer, status, and receipt."""
        conn, other = two_conns
        make_experiment(conn, n_trials=1)
        monkeypatch.setattr(supervisor, "cleanup_container", lambda run_id: None)
        child = subprocess.Popen(["sleep", "30"])
        run_id = runs.create_run(conn, "t1", {}, supervisor_pid=child.pid)
        conn.execute("UPDATE trial_runs SET status = 'running' WHERE run_id = ?", (run_id,))
        conn.execute("UPDATE trials SET status = 'claimed' WHERE id = 't1'")
        seal_row(conn, "t1", status="sealed", frozen_at="2026-09-09T12:00:10+00:00")
        conn.commit()

        client = _client_for(conn)
        summary = client.post("/v1/experiments/e1/cancel").json()
        child.kill(); child.wait()
        assert summary == {"status": "cancelled", "cancelled": 0, "stopped": 1, "untouched": 0}
        # the official answer stands untouched: exactly one answer row, sealed
        assert conn.execute("SELECT COUNT(*) FROM sealed_answers").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM sealed_answers WHERE trial_id = 't1'").fetchone()[0] == "sealed"
        assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "sealed"
        # the in-flight run was still stopped (diagnostic only)
        assert runs.get_run(conn, run_id)["exit_kind"] == "cancelled"

    def test_cancel_wins_timeout_seal_loses(self, two_conns, monkeypatch):
        """Cancel registered first: the supervisor's late timeout outcome
        (seal attempt, verdict, finish) must not create a second answer or
        rewrite the terminal exit reason."""
        from datetime import datetime, timedelta, timezone

        conn, other = two_conns
        make_experiment(conn, n_trials=1)
        monkeypatch.setattr(supervisor, "cleanup_container", lambda run_id: None)
        run_id = runs.create_run(conn, "t1", {}, supervisor_pid=-1)
        conn.execute("UPDATE trial_runs SET status = 'running' WHERE run_id = ?", (run_id,))
        conn.execute("UPDATE trials SET status = 'claimed' WHERE id = 't1'")
        conn.commit()

        # cancel wins (API connection)
        _client_for(conn).post("/v1/experiments/e1/cancel")
        first = runs.get_run(conn, run_id)
        receipt_before = conn.execute(
            "SELECT receipt_id, digest FROM sealed_answers WHERE trial_id = 't1'").fetchone()

        # the supervisor's timeout path lands afterwards (separate connection)
        runs.add_phase(other, run_id, "trial_timeout")
        artifacts.mark_anomaly(other, "t1", run_id, "timeout path lost the race", trigger="timeout")
        verdict = lifecycle.record_timeout_verdict(
            other, "e1", "t1", run_id,
            datetime.now(timezone.utc) - timedelta(seconds=5), timedelta(seconds=90))
        rewritten = runs.finish_run(other, run_id, "error", runs.EXIT_TIMEOUT, "late timeout")

        assert verdict in ("within_tolerance", "over_tolerance")  # recorded, harmless
        assert rewritten is False  # first legal termination is never rewritten
        assert runs.get_run(other, run_id)["exit_kind"] == "cancelled"
        receipt_after = conn.execute(
            "SELECT receipt_id, digest, status FROM sealed_answers WHERE trial_id = 't1'").fetchone()
        assert (receipt_after["receipt_id"], receipt_after["digest"]) == \
               (receipt_before["receipt_id"], receipt_before["digest"])  # stable receipt
        assert conn.execute("SELECT COUNT(*) FROM sealed_answers").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "cancelled"

    def test_submit_accepted_then_cancel_yields_one_reason(self, two_conns, monkeypatch):
        """Submit accepted, seal never happened, cancel arrives: one terminal
        reason (diagnostic anomaly), the submission is closed, a repeat of
        the losing path cannot add a second answer."""
        conn, other = two_conns
        make_experiment(conn, n_trials=1)
        monkeypatch.setattr(supervisor, "cleanup_container", lambda run_id: None)
        run_id = runs.create_run(conn, "t1", {}, supervisor_pid=-1)
        conn.execute("UPDATE trial_runs SET status = 'running' WHERE run_id = ?", (run_id,))
        conn.execute("UPDATE trials SET status = 'claimed' WHERE id = 't1'")
        conn.commit()
        client = _client_for(conn)
        token = client.post("/v1/trials/t1/session-token").json()["token"]
        session_client = _session_client_for(conn)
        resp = session_client.post("/v1/session/submit",
                                   headers={"Authorization": f"Bearer {token}"},
                                   json={"idempotency_key": "k1"})
        assert resp.status_code == 202  # submit wins the acceptance race

        client.post("/v1/experiments/e1/cancel")  # cancel wins the termination race
        # losing timeout path repeats afterwards: exactly-once guards hold
        artifacts.mark_anomaly(other, "t1", run_id, "second attempt", trigger="timeout")
        assert conn.execute("SELECT COUNT(*) FROM sealed_answers").fetchone()[0] == 1
        row = conn.execute("SELECT status, anomaly FROM sealed_answers WHERE trial_id = 't1'").fetchone()
        assert row["status"] == "anomaly" and "cancelled" in row["anomaly"]
        submission = conn.execute("SELECT status FROM submissions WHERE trial_id = 't1'").fetchone()
        assert submission["status"] == "error"
        assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "cancelled"
        assert runs.get_run(conn, run_id)["exit_kind"] == "cancelled"


class TestRestartDuringSealing:
    """Restart while a seal is mid-flight: startup recovery finishes or flags
    the answer from disk truth and never creates a second run (#16)."""

    def _stage_interrupted_seal(self, conn, root, complete: bool):
        make_experiment(conn, n_trials=1)
        child = subprocess.Popen(["sleep", "30"])
        run_id = runs.create_run(conn, "t1", {}, supervisor_pid=child.pid)
        child.kill(); child.wait()  # supervisor is dead after the restart
        conn.execute("UPDATE trials SET status = 'claimed' WHERE id = 't1'")
        conn.commit()
        staging = root / "sealing" / run_id / "staging"
        (staging / "workspace").mkdir(parents=True)
        (staging / "workspace" / "answer.txt").write_bytes(b"partial run answer")
        if complete:
            manifest = artifacts.build_manifest(staging, "t1", run_id, "timeout", None)
            (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        return run_id

    @pytest.mark.parametrize("complete,expected_status", [(True, "sealed"), (False, "anomaly")])
    def test_startup_recovery_is_terminal_without_new_run(self, tmp_path, monkeypatch,
                                                          complete, expected_status):
        from aco.execution import startup_recovery

        conn = db.connect(tmp_path / "aco.db")
        db.migrate(conn)
        monkeypatch.setattr(supervisor, "cleanup_container", lambda run_id: None)
        run_id = self._stage_interrupted_seal(conn, tmp_path, complete)

        startup_recovery(conn, tmp_path)  # the exact manager-startup sequence

        runs_rows = conn.execute("SELECT run_id FROM trial_runs WHERE trial_id = 't1'").fetchall()
        assert len(runs_rows) == 1  # no second run, no rerun
        trial = conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0]
        assert trial == "claimed"  # never silently returns to the plan
        answer = conn.execute(
            "SELECT status, digest, receipt_id FROM sealed_answers WHERE trial_id = 't1'").fetchone()
        assert answer["status"] == expected_status
        if complete:
            assert answer["digest"] != ""
            assert (tmp_path / "answers" / answer["digest"]).is_dir()  # published from disk truth
            # a trial that never submitted has no submissions row; the receipt
            # still exists on the answer record itself
            assert conn.execute("SELECT COUNT(*) FROM submissions WHERE trial_id = 't1'").fetchone()[0] in (0, 1)
        assert claim_next_planned(conn) is None
        conn.close()


def _client_for(conn):
    """A TestClient bound to the same database file through its own
    connection, like a second server process."""
    from pathlib import Path

    from fastapi.testclient import TestClient

    from aco.app import create_management_app
    db_file = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    app = create_management_app(data_root=str(db_file.parent), token="test-management-token")
    return TestClient(app, headers={"Authorization": "Bearer test-management-token"})


def _session_client_for(conn):
    """A Session-surface client bound to the same database file."""
    from pathlib import Path

    from fastapi.testclient import TestClient

    from aco.app import create_session_app
    db_file = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    return TestClient(create_session_app(data_root=str(db_file.parent)))


class TestTimeoutVerdict:
    def _sealed_with_freeze(self, conn, frozen_at: str):
        make_experiment(conn, n_trials=1)
        runs.create_run(conn, "t1", {}, supervisor_pid=-1)
        seal_row(conn, "t1", frozen_at=frozen_at)
        return runs.list_runs(conn, "t1")[0]["run_id"]

    def test_within_tolerance_answer_stays_scoreable(self, conn):
        from datetime import datetime, timedelta, timezone

        run_id = self._sealed_with_freeze(conn, "2026-09-09T12:00:25+00:00")
        deadline = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
        verdict = lifecycle.record_timeout_verdict(
            conn, "e1", "t1", run_id, deadline, timedelta(seconds=90))
        assert verdict == "within_tolerance"
        assert conn.execute("SELECT status FROM sealed_answers WHERE trial_id = 't1'").fetchone()[0] == "sealed"

    def test_over_tolerance_answer_becomes_anomaly(self, conn):
        from datetime import datetime, timedelta, timezone

        run_id = self._sealed_with_freeze(conn, "2026-09-09T12:05:00+00:00")
        deadline = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
        verdict = lifecycle.record_timeout_verdict(
            conn, "e1", "t1", run_id, deadline, timedelta(seconds=90))
        assert verdict == "over_tolerance"
        row = conn.execute("SELECT status, anomaly FROM sealed_answers WHERE trial_id = 't1'").fetchone()
        assert row["status"] == "anomaly" and "past the planned deadline" in row["anomaly"]
        event = conn.execute("SELECT detail FROM lifecycle_events WHERE event = 'timeout_verdict'").fetchone()
        detail = __import__("json").loads(event["detail"])
        assert detail["verdict"] == "over_tolerance" and "planned_deadline" in detail

    def test_no_answer_records_nothing(self, conn):
        from datetime import datetime, timedelta, timezone

        make_experiment(conn, n_trials=1)
        verdict = lifecycle.record_timeout_verdict(
            conn, "e1", "t1", "no-run", datetime.now(timezone.utc), timedelta(seconds=90))
        assert verdict == "no_answer"


class TestProgress:
    def test_progress_counts_exposed_on_experiment(self, client, conn):
        make_experiment(conn, n_trials=2)
        conn.execute("UPDATE trials SET status = 'cancelled' WHERE id = 't1'")
        runs.create_run(conn, "t2", {}, supervisor_pid=-1)
        conn.execute("UPDATE trials SET status = 'claimed' WHERE id = 't2'")
        seal_row(conn, "t2")
        conn.commit()
        body = client.get("/v1/experiments/e1").json()
        assert body["progress"] == {"planned": 0, "claimed": 1, "cancelled": 1,
                                    "sealed": 1, "anomaly": 0, "attempted": 1}


class TestEvents:
    def test_lifecycle_changes_append_events(self, client, conn):
        make_experiment(conn)
        lifecycle.pause_unstarted_on_restart(conn)
        client.post("/v1/experiments/e1/resume")
        client.post("/v1/experiments/e1/cancel")
        events = [r["event"] for r in conn.execute(
            "SELECT event FROM lifecycle_events WHERE trial_id IS NULL ORDER BY id")]
        assert events == ["restart_paused", "resumed", "cancelled"]
        assert conn.execute("SELECT reason FROM lifecycle_events WHERE event = 'resumed'").fetchone()[0] == "explicit_resume"


def test_migration_0006_applies(conn):
    columns = {r[1] for r in conn.execute("PRAGMA table_info(lifecycle_events)")}
    assert {"experiment_id", "trial_id", "event", "reason", "detail", "created_at"} <= columns
