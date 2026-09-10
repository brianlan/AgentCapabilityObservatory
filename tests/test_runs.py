"""Unit tests for run records, profile translation, and manager helpers (#13)."""

import json
import os

import pytest

from aco import db, runs
from aco.execution import claim_next_planned, recover_stale_claims, reap_lost_supervisors
from aco.supervisor import UnsupportedTarget, translate_profile


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "aco.db")
    db.migrate(conn)
    yield conn
    conn.close()


def insert_trial(conn, trial_id="t1"):
    conn.execute(
        "INSERT INTO versions (id, kind, name, version, content, created_at)"
        " VALUES ('v-task', 'task', 'task', 'v1', '{}', 'now'),"
        " ('v-cfg', 'config', 'cfg', 'v1', '{}', 'now'),"
        " ('v-cfg2', 'config', 'cfg2', 'v1', '{}', 'now')"
    )
    conn.execute(
        "INSERT INTO experiments (id, status, requested, created_at)"
        " VALUES ('e1', 'planned', '{}', 'now')"
    )
    conn.execute(
        "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
        " repetition, plan_order, requested) VALUES (?, 'e1', 'v-task', 'v-cfg', 1, 1, '{}')",
        (trial_id,),
    )
    conn.execute(
        "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
        " repetition, plan_order, requested) VALUES ('t2', 'e1', 'v-task', 'v-cfg2', 1, 2, '{}')"
    )
    conn.commit()


class TestTranslateProfile:
    def test_bare_fake_profile_is_supported(self):
        assert translate_profile({"harness": "fake", "model": "none"}) == ("fake", 20)

    def test_real_harness_fails_explicitly(self):
        with pytest.raises(UnsupportedTarget, match="unsupported harness 'codex'"):
            translate_profile({"harness": "codex", "model": "gpt"})

    def test_model_on_fake_target_fails_explicitly(self):
        with pytest.raises(UnsupportedTarget, match="does not support model"):
            translate_profile({"harness": "fake", "model": "gpt"})

    def test_provider_skills_credentials_fail_explicitly(self):
        for profile in (
            {"harness": "fake", "model": "none", "provider": "openai"},
            {"harness": "fake", "model": "none", "skills": ["search"]},
            {"harness": "fake", "model": "none", "credentials": ["key-ref"]},
        ):
            with pytest.raises(UnsupportedTarget):
                translate_profile(profile)

    def test_inference_field_fails_explicitly(self):
        # "inference" is not a TargetProfile field (never registrable, #36):
        # it fails the unknown-field walk like any other stray key
        with pytest.raises(UnsupportedTarget, match="unsupported target profile field 'inference'"):
            translate_profile(
                {"harness": "fake", "model": "none", "inference": {"temperature": 0.7}}
            )

    def test_unknown_profile_fields_fail_explicitly(self):
        for profile in (
            {"harness": "fake", "model": "none", "temperature": 0.7},
            {"harness": "fake", "model": "none", "unknown_key": "x"},
        ):
            with pytest.raises(UnsupportedTarget, match="unsupported target profile field"):
                translate_profile(profile)


class TestRunRecords:
    def test_launch_intent_persisted_before_observation(self, conn):
        insert_trial(conn)
        run_id = runs.create_run(conn, "t1", {"harness": "fake"}, supervisor_pid=4242)
        row = runs.get_run(conn, run_id)
        assert row["status"] == "launching"
        assert row["requested_profile"] == json.dumps({"harness": "fake"}, sort_keys=True)
        assert row["supervisor_pid"] == 4242
        # observations start as NULL, never fabricated
        for column in ("adapter_version", "harbor_version", "container_id", "phases", "exit_kind"):
            assert row[column] is None

    def test_observation_then_finish(self, conn):
        insert_trial(conn)
        run_id = runs.create_run(conn, "t1", {}, supervisor_pid=-1)
        runs.mark_running(conn, run_id, container_id="abc", image="python")
        runs.observe_run(conn, run_id, "0.1.0", "0.22.0", "/logs")
        runs.add_phase(conn, run_id, "agent_start", container_id="abc")
        runs.add_phase(conn, run_id, "trial_finished", verifier_scored=False)
        runs.finish_run(conn, run_id, "finished", runs.EXIT_NORMAL)

        out = runs.run_out(runs.get_run(conn, run_id))
        assert out["status"] == "finished"
        assert out["exit_kind"] == "normal"
        assert out["container_id"] == "abc"
        assert out["harbor_version"] == "0.22.0"
        assert [p["event"] for p in out["phases"]] == ["agent_start", "trial_finished"]
        assert out["phases"][1]["verifier_scored"] is False

    def test_list_runs_by_trial(self, conn):
        insert_trial(conn)
        assert runs.list_runs(conn, "t1") == []
        run_id = runs.create_run(conn, "t1", {}, supervisor_pid=-1)
        assert [r["run_id"] for r in runs.list_runs(conn, "t1")] == [run_id]


class TestManagerHelpers:
    def test_claims_planned_once_in_order(self, conn):
        insert_trial(conn)
        assert claim_next_planned(conn) == "t1"
        assert claim_next_planned(conn) == "t2"
        assert claim_next_planned(conn) is None  # no re-claim of claimed trials

    def test_recover_stale_claim_without_run(self, conn):
        insert_trial(conn)
        conn.execute("UPDATE trials SET status = 'claimed' WHERE id = 't1'")
        conn.commit()
        recover_stale_claims(conn)
        statuses = dict(conn.execute("SELECT id, status FROM trials").fetchall())
        assert statuses == {"t1": "planned", "t2": "planned"}

    def test_claimed_trial_with_active_run_is_kept(self, conn):
        insert_trial(conn)
        runs.create_run(conn, "t1", {}, supervisor_pid=-1)  # status launching
        conn.execute("UPDATE trials SET status = 'claimed' WHERE id = 't1'")
        conn.commit()
        recover_stale_claims(conn)
        assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "claimed"

    def test_dead_supervisor_run_is_reaped_with_diagnostics(self, conn):
        insert_trial(conn)
        run_id = runs.create_run(conn, "t1", {}, supervisor_pid=-1)
        runs.mark_running(conn, run_id, container_id="abc")
        reap_lost_supervisors(conn)
        row = runs.get_run(conn, run_id)
        assert row["status"] == "error"
        assert row["exit_kind"] == "supervisor_lost"
        # the launch intent survives the crash
        assert row["supervisor_pid"] == -1
        assert row["container_id"] == "abc"

    def test_live_supervisor_run_is_left_alone(self, conn):
        insert_trial(conn)
        run_id = runs.create_run(conn, "t1", {}, supervisor_pid=os.getpid())
        reap_lost_supervisors(conn)
        assert runs.get_run(conn, run_id)["status"] == "launching"
