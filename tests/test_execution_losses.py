"""Unit tests for the execution manager's terminal-state guarantees (#16).

The supervisor subprocess is faked at the Popen boundary (kill/wait/poll
recorded, never a real process in the watchdog paths), container cleanup is
recorded, and every loss path runs against a real SQLite database through
the same finish_trial funnel. Real supervisor identity uses live helper
processes; full manager E2E coverage lives in tests/e2e/test_execution.py.
"""

import os
import subprocess
import time

import pytest

from aco import db, lifecycle, runs, supervisor
from aco.execution import (
    PREP_GRACE_SEC,
    claim_next_planned,
    recover_stale_claims,
    reap_lost_supervisors,
    run_one,
    supervisor_watchdog_sec,
)
from aco.supervisor import DEFAULT_AGENT_TIMEOUT_SEC, TRIAL_GRACE_SEC


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "aco.db")
    db.migrate(conn)
    yield conn
    conn.close()


def make_experiment(conn, n_trials=1) -> str:
    conn.execute(
        "INSERT INTO versions (id, kind, name, version, content, created_at)"
        " VALUES ('v-task', 'task', 'task', 'v1', '{}', 'now'),"
        " ('v-cfg', 'config', 'cfg', 'v1', '{\"harness\": \"fake\", \"model\": \"none\"}', 'now')"
    )
    conn.execute(
        "INSERT INTO experiments (id, status, requested, created_at)"
        " VALUES ('e1', 'planned', '{}', 'now')"
    )
    for i in range(1, n_trials + 1):
        conn.execute(
            "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
            f" repetition, plan_order, requested) VALUES ('t{i}', 'e1', 'v-task', 'v-cfg', {i}, {i}, '{{}}')"
        )
    conn.commit()
    return "e1"


class FakeProc:
    """Stands in for the supervisor subprocess: poll/kill/wait are recorded."""

    def __init__(self, pid=424242, exit_after_kills=True):
        self.pid = pid
        self.returncode = None
        self.kills = 0
        self._exit_after_kills = exit_after_kills

    def poll(self):
        return self.returncode

    def kill(self):
        self.kills += 1
        if self._exit_after_kills:
            self.returncode = -9

    def wait(self):
        return self.returncode


@pytest.fixture()
def manager_env(monkeypatch):
    """Fake Popen + no-op container cleanup + instant token mint."""
    procs = []

    def fake_popen(cmd, **kwargs):
        proc = FakeProc()
        procs.append(proc)
        return proc

    monkeypatch.setattr("aco.execution.subprocess.Popen", fake_popen)
    cleaned = []
    monkeypatch.setattr("aco.execution.cleanup_container", lambda run_id: cleaned.append(run_id))
    monkeypatch.setattr("aco.execution.mint_token", lambda *a, **k: "tok")
    return {"procs": procs, "cleaned": cleaned}


class TestWatchdogTerminalStates:
    """Supervisor exceeding the manager watchdog leaves consistent terminal
    states for the run, the trial, and the experiment — and the trial is
    never re-claimed after a restart (#16 reopen)."""

    def test_watchdog_kill_terminals_everything(self, conn, tmp_path, manager_env, monkeypatch):
        make_experiment(conn)
        monkeypatch.setattr("aco.execution.supervisor_watchdog_sec", lambda profile: 0.0)

        assert run_one(conn, tmp_path, "http://api", "tok", "http://session") is True
        run_id = runs.list_runs(conn, "t1")[0]["run_id"]
        run = runs.get_run(conn, run_id)
        trial = conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0]
        experiment = conn.execute("SELECT status FROM experiments WHERE id = 'e1'").fetchone()[0]

        assert manager_env["procs"][0].kills == 1  # the run was killed, then terminalized
        assert run["status"] == "error" and run["exit_kind"] == "supervisor_lost"
        assert "watchdog" in run["exit_detail"]
        assert trial == "anomaly"
        assert experiment == "completed"  # the plan can finish across the loss
        answer = conn.execute(
            "SELECT status, seal_trigger FROM sealed_answers WHERE trial_id = 't1'").fetchone()
        assert answer["status"] == "anomaly" and answer["seal_trigger"] == "supervisor_lost"
        assert manager_env["cleaned"] == [run_id]

        # restart: the anomaly trial is never re-claimed (no implicit rerun)
        recover_stale_claims(conn)
        assert claim_next_planned(conn) is None

    def test_abnormal_exit_terminals_through_the_same_funnel(self, conn, tmp_path,
                                                             manager_env, monkeypatch):
        make_experiment(conn)
        proc = FakeProc(exit_after_kills=False)
        proc.returncode = 1  # supervisor exits on its own, no outcome recorded
        monkeypatch.setattr("aco.execution.subprocess.Popen", lambda cmd, **kw: proc)

        assert run_one(conn, tmp_path, "http://api", "tok", "http://session") is True
        run = runs.get_run(conn, runs.list_runs(conn, "t1")[0]["run_id"])
        assert run["status"] == "error" and run["exit_kind"] == "supervisor_lost"
        assert "exit code 1" in run["exit_detail"]
        assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "anomaly"

    def test_spawn_failure_terminals_through_the_same_funnel(self, conn, tmp_path,
                                                             manager_env, monkeypatch):
        make_experiment(conn)

        def failing_popen(cmd, **kwargs):
            raise OSError("fork failed")

        monkeypatch.setattr("aco.execution.subprocess.Popen", failing_popen)

        assert run_one(conn, tmp_path, "http://api", "tok", "http://session") is True
        run = runs.get_run(conn, runs.list_runs(conn, "t1")[0]["run_id"])
        assert run["status"] == "error" and run["exit_kind"] == "supervisor_lost"
        assert "spawn" in run["exit_detail"]
        assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "anomaly"
        assert claim_next_planned(conn) is None

    def test_watchdog_derives_from_effective_trial_timeout(self):
        # fake harness: agent timeout + trial grace + prep allowance
        assert supervisor_watchdog_sec({"harness": "fake", "model": "none"}) == \
            DEFAULT_AGENT_TIMEOUT_SEC + TRIAL_GRACE_SEC + PREP_GRACE_SEC
        # the legacy ceiling only remains the fallback for unusable profiles
        assert supervisor_watchdog_sec({"harness": "nope"}) == 300

    def test_sealed_answer_survives_a_late_watchdog_kill(self, conn, tmp_path,
                                                         manager_env, monkeypatch):
        """The supervisor sealed, then dawdled past the watchdog: the official
        answer keeps its eligibility (outcome sealed, not anomaly) (ADR 0003)."""
        make_experiment(conn)
        monkeypatch.setattr("aco.execution.supervisor_watchdog_sec", lambda profile: 0.0)

        # the answer the supervisor registered before the watchdog fired,
        # seeded while the run is born (the hook run_one calls after spawn)
        original = runs.record_supervisor

        def seed_answer(conn2, run_id, pid):
            tick = original(conn2, run_id, pid)
            conn2.execute(
                "INSERT INTO sealed_answers (trial_id, run_id, receipt_id, digest, manifest,"
                " seal_trigger, trigger_at, frozen_at, copied_at, published_at, registered_at, status)"
                " VALUES ('t1', ?, 'r1', 'd1', '{}', 'submit', 'now', 'now', 'now', 'now',"
                " 'now', 'sealed')", (run_id,))
            conn2.commit()
            return tick

        monkeypatch.setattr("aco.execution.runs.record_supervisor", seed_answer)

        assert run_one(conn, tmp_path, "http://api", "tok", "http://session") is True
        assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "sealed"
        answer = conn.execute(
            "SELECT status, receipt_id FROM sealed_answers WHERE trial_id = 't1'").fetchone()
        assert answer["status"] == "sealed" and answer["receipt_id"] == "r1"  # receipt untouched


class TestTokenMintRollback:
    """A failed token mint may only unclaim trials that are still claimed and
    whose experiment can still run — a concurrent cancel is never undone
    (#16 reopen)."""

    def test_cancel_won_mid_run_one_is_never_resurrected(self, conn, tmp_path, manager_env, monkeypatch):
        make_experiment(conn)

        def mint_after_concurrent_cancel(api_url, trial_id, token):
            # the cancel wins between run_one's claim and the mint
            lifecycle.cancel_experiment(conn, "e1")
            return None

        monkeypatch.setattr("aco.execution.mint_token", mint_after_concurrent_cancel)
        assert run_one(conn, tmp_path, "http://api", "tok", "http://session") is False
        assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "cancelled"
        assert conn.execute("SELECT COUNT(*) FROM trial_runs").fetchone()[0] == 0  # nothing launched

    def test_still_claimed_trial_of_active_experiment_returns_to_planned(
            self, conn, tmp_path, manager_env, monkeypatch):
        make_experiment(conn)
        monkeypatch.setattr("aco.execution.mint_token", lambda *a, **k: None)
        assert run_one(conn, tmp_path, "http://api", "tok", "http://session") is False
        assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "planned"


class TestSupervisorIdentity:
    """Adoption, cancel, and recovery decide on the recorded (pid, start
    tick), never on PID existence alone (#16 reopen)."""

    @staticmethod
    def _run_with_child(conn, *, record_identity=True):
        make_experiment(conn)
        run_id = runs.create_run(conn, "t1", {}, supervisor_pid=-1)
        child = subprocess.Popen(["sleep", "30"])
        if record_identity:
            runs.record_supervisor(conn, run_id, child.pid)
        else:  # simulate PID reuse: a start tick that no longer matches
            conn.execute("UPDATE trial_runs SET supervisor_pid = ?, supervisor_start = '1'",
                         (child.pid,))
        conn.execute("UPDATE trial_runs SET status = 'running' WHERE run_id = ?", (run_id,))
        conn.execute("UPDATE trials SET status = 'claimed' WHERE id = 't1'")
        conn.commit()
        return run_id, child

    def test_reused_pid_is_never_adopted_and_run_becomes_explainable_anomaly(self, conn):
        run_id, child = self._run_with_child(conn, record_identity=False)
        try:
            assert lifecycle.adopt_live_supervisors(conn) == 0  # no adoption
            reap_lost_supervisors(conn)
            run = runs.get_run(conn, run_id)
            assert run["status"] == "error" and run["exit_kind"] == "supervisor_lost"
            assert "identity" in run["exit_detail"]
            assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "anomaly"
            assert claim_next_planned(conn) is None  # never re-answered
        finally:
            child.kill()
            child.wait()

    def test_reused_pid_never_receives_the_cancel_signal(self, conn, monkeypatch):
        run_id, child = self._run_with_child(conn, record_identity=False)
        try:
            monkeypatch.setattr(supervisor, "cleanup_container", lambda run_id: None)
            lifecycle.cancel_experiment(conn, "e1")
            time.sleep(0.2)
            assert child.poll() is None  # the unrelated process at that pid is untouched
            assert runs.get_run(conn, run_id)["exit_kind"] == "cancelled"  # run still stopped
            phases = conn.execute(
                "SELECT phases FROM trial_runs WHERE run_id = ?", (run_id,)).fetchone()["phases"]
            assert "supervisor_identity_unverified" in phases
        finally:
            child.kill()
            child.wait()

    def test_verified_identity_survives_reap_and_is_adopted(self, conn):
        run_id, child = self._run_with_child(conn, record_identity=True)
        try:
            # the recorded tick still matches: reap leaves the run alone
            reap_lost_supervisors(conn)
            assert runs.get_run(conn, run_id)["status"] == "running"
            assert lifecycle.adopt_live_supervisors(conn) == 1
        finally:
            child.kill()
            child.wait()


class TestManagerFileDescriptors:
    """Sequential trials must not grow the manager's open file descriptors
    (#16 reopen): the parent-side supervisor.log handle is always closed."""

    def test_no_fd_growth_across_sequential_trials(self, conn, tmp_path, manager_env, monkeypatch):
        make_experiment(conn, n_trials=5)
        monkeypatch.setattr("aco.execution.supervisor_watchdog_sec", lambda profile: 0.0)
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(5):
            assert run_one(conn, tmp_path, "http://api", "tok", "http://session") is True
        after = len(os.listdir("/proc/self/fd"))
        assert after <= before  # nothing accumulated across the five trials
