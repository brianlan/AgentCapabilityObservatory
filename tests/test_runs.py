"""Unit tests for run records, profile translation, and manager helpers (#13)."""

import json
import os
import shutil

import pytest

from aco import db, runs, supervisor
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
        run_id = runs.create_run(conn, "t1", {}, supervisor_pid=-1)
        runs.record_supervisor(conn, run_id, os.getpid())  # live pid + verifiable identity
        reap_lost_supervisors(conn)
        assert runs.get_run(conn, run_id)["status"] == "launching"


class TestSkillMaterialization:
    """Requested vs observed skill digests + read-only mounts (#39)."""

    PI_SKILLS = [{"name": "demo", "version": "v1"}]

    def _profile(self, skills):
        return {
            "schema_version": 1, "harness": "pi", "harness_version": "0.84.1",
            "model": "glm-5.3-flash", "thinking": "max",
            "provider": "ark-agent-plan", "provider_api_style": "openai-responses",
            "adapter_version": "0.1.1", "credentials": ["ark-agent-plan-main"],
            "skills": skills,
        }

    def _import(self, tmp_path, conn, name="demo", version="v1"):
        from aco import skills as skills_mod
        bundle = tmp_path / f"{name}-bundle"
        (bundle / "inner").mkdir(parents=True, exist_ok=True)
        (bundle / "SKILL.md").write_text("---\nname: demo\n---\nbody\n")
        (bundle / "inner" / "n.md").write_text("nested\n")
        return skills_mod.import_skill(bundle, tmp_path, name, version, conn)

    def test_verified_mount_when_bytes_match(self, tmp_path, conn):
        from aco.models import parse_config_content
        from aco.supervisor import resolve_skill_mounts
        record = self._import(tmp_path, conn)
        assert record["id"]
        parsed = parse_config_content(self._profile(self.PI_SKILLS))
        state = resolve_skill_mounts(conn, parsed, tmp_path)
        assert len(state) == 1
        assert state[0]["verified"] is True
        # the mount wiring needs the host_dir of the verified bytes
        assert state[0]["host_dir"] == str(tmp_path / "skills" / record["id"])
        assert (tmp_path / "skills" / record["id"] / "SKILL.md").is_file()
        assert state[0]["requested"] == state[0]["observed"]
        assert state[0]["requested"] == record["content"]["bundle"]["digest"]

    def test_unsafe_skill_name_fails_closed(self, tmp_path, conn):
        """A hostile skill name (only possible via trusted-side
        misregistration) never becomes a container path (#39)."""
        import json as _json
        from aco.models import parse_config_content
        from aco.supervisor import resolve_skill_mounts
        conn.execute(
            "INSERT INTO versions (id, kind, name, version, content, assets,"
            " created_at) VALUES (?, 'skill', 'evil/path', 'v1', ?, '[]',"
            " '2026-01-01T00:00:00Z')",
            ("deadbeef", _json.dumps({"schema_version": 1, "entry": "SKILL.md",
                                      "bundle": {"digest": "0" * 64,
                                                 "bytes": 1, "files": 1}})))
        conn.commit()
        parsed = parse_config_content(
            self._profile([{"name": "evil/path", "version": "v1"}]))
        state = resolve_skill_mounts(conn, parsed, tmp_path)
        assert state[0]["verified"] is False
        assert state[0]["observed"] == "unsafe_name"
        assert "host_dir" not in state[0]

    def test_tampered_bytes_fail_verification(self, tmp_path, conn):
        from aco.models import parse_config_content
        from aco.supervisor import resolve_skill_mounts
        record = self._import(tmp_path, conn)
        (tmp_path / "skills" / record["id"] / "SKILL.md").write_text("tampered\n")
        parsed = parse_config_content(self._profile(self.PI_SKILLS))
        state = resolve_skill_mounts(conn, parsed, tmp_path)
        assert state[0]["verified"] is False
        assert state[0]["observed"] != state[0]["requested"]

    def test_missing_bundle_fails_verification(self, tmp_path, conn):
        from aco.models import parse_config_content
        from aco.supervisor import resolve_skill_mounts
        self._import(tmp_path, conn)
        # registry row exists, bytes gone (e.g. different data root)
        for row in conn.execute("SELECT id FROM versions WHERE kind='skill'"):
            shutil.rmtree(tmp_path / "skills" / row["id"])
        parsed = parse_config_content(self._profile(self.PI_SKILLS))
        state = resolve_skill_mounts(conn, parsed, tmp_path)
        assert state[0]["verified"] is False
        assert state[0]["observed"] == "bundle_missing"

    def test_malformed_content_fails_closed(self, tmp_path, conn):
        """A skill row with malformed content (misregistration the #39
        reopen API guard now rejects upstream) becomes the pre-agent
        execution anomaly it is — not a supervisor KeyError crash."""
        import json as _json
        from aco.models import parse_config_content
        from aco.supervisor import resolve_skill_mounts
        conn.execute(
            "INSERT INTO versions (id, kind, name, version, content, assets,"
            " created_at) VALUES (?, 'skill', 'demo', 'v1', ?, '[]',"
            " '2026-01-01T00:00:00Z')",
            ("deadbeef", _json.dumps({"schema_version": 1})))
        conn.commit()
        parsed = parse_config_content(self._profile(
            [{"name": "demo", "version": "v1"}]))
        state = resolve_skill_mounts(conn, parsed, tmp_path)
        assert state[0]["verified"] is False
        assert state[0]["observed"] == "malformed_content"
        assert state[0]["requested"] is None
        assert "host_dir" not in state[0]

    def test_build_task_dir_mounts_declared_skills_readonly(self, tmp_path, monkeypatch):
        from aco.supervisor import build_task_dir, gateway_config, pi_agent
        monkeypatch.setenv("ARK_AGENT_PLAN_BASE_URL", "https://ark.example.com/api/v3")
        monkeypatch.setenv("ACO_BASE_URL", "http://127.0.0.1:8100")
        mounts = [{"name": "demo", "host_dir": "/data/skills/abc123"}]
        task_dir = build_task_dir(tmp_path, "prompt", 30, "run1", harness="pi",
                                  skill_mounts=mounts,
                                  gateway=gateway_config(tmp_path, "run1"))
        compose = (task_dir / "offline.yaml").read_text()
        main_block = compose.split("  main:")[1]
        assert f'"/data/skills/abc123:{pi_agent._PI_CONTAINER_SKILL_ROOT}/demo:ro"' in main_block
        # the no-skill compose mounts nothing into the evaluated container
        plain = build_task_dir(tmp_path / "plain", "prompt", 30, "run2", harness="pi",
                               gateway=gateway_config(tmp_path / "plain", "run2"))
        plain_main = (plain / "offline.yaml").read_text().split("  main:")[1]
        assert pi_agent._PI_CONTAINER_SKILL_ROOT not in plain_main


# --------------------------------------------------- task network policy (#38)

def test_fake_task_dir_stays_offline(tmp_path):
    from aco.supervisor import build_task_dir

    build_task_dir(tmp_path, "prompt", 20, "run-1")
    toml = (tmp_path / "task" / "task.toml").read_text()
    compose = (tmp_path / "task" / "offline.yaml").read_text()
    assert 'network_mode = "public"' in toml
    assert "allowlist" not in toml
    assert "network_mode: none" in compose


def test_pi_task_dir_declares_allowlist_and_no_explicit_networking(tmp_path, monkeypatch):
    """#38: the pi target's restriction comes from harbor's egress sidecar —
    task.toml allowlists exactly the gateway service, the compose override
    adds NO explicit networking on main (the sidecar owns the network
    namespace), and the gateway service carries the fixed upstreams."""
    from aco import supervisor
    from aco.supervisor import build_task_dir, gateway_config

    monkeypatch.setenv("ARK_AGENT_PLAN_BASE_URL", "https://ark.example.com/api/v3")
    monkeypatch.setenv("ACO_BASE_URL", "http://127.0.0.1:8100")
    gateway = gateway_config(tmp_path, "run-2")
    build_task_dir(tmp_path, "prompt", 120, "run-2", harness="pi", gateway=gateway)
    toml = (tmp_path / "task" / "task.toml").read_text()
    compose = (tmp_path / "task" / "offline.yaml").read_text()
    ip, subnet = supervisor.gateway_ip("run-2"), supervisor.gateway_subnet("run-2")
    assert 'network_mode = "allowlist"' in toml
    assert f'allowed_hosts = ["{ip}"]' in toml
    main_block = compose.split("  main:")[1]
    assert "network_mode" not in main_block  # no bridge/none override on main
    assert "aco-gateway" in main_block  # main depends on the gateway
    assert subnet in compose  # per-run gateway subnet (crash-leak safe, #38)
    # the gateway: pinned python, stdlib gateway module, fixed upstreams
    assert f"ipv4_address: {ip}" in compose  # gateway on the trial net, not the sidecar netns
    assert "ACO_GATEWAY_PROVIDER_UPSTREAM: \"https://ark.example.com/api/v3\"" in compose
    assert "ACO_GATEWAY_SESSION_UPSTREAM: \"http://host.docker.internal:8100\"" in compose
    assert "/aco/gateway.py:ro" in compose


def test_pi_task_dir_defaults_allowlist_to_gateway(tmp_path, monkeypatch):
    from aco import supervisor
    from aco.supervisor import build_task_dir, gateway_config

    monkeypatch.setenv("ARK_AGENT_PLAN_BASE_URL", "https://ark.example.com/api/v3")
    monkeypatch.setenv("ACO_BASE_URL", "http://127.0.0.1:8100")
    build_task_dir(tmp_path, "prompt", 120, "run-3", harness="pi",
                   gateway=gateway_config(tmp_path, "run-3"))
    toml = (tmp_path / "task" / "task.toml").read_text()
    assert f'"{supervisor.gateway_ip("run-3")}"' in toml


# ------------------------------------------- stale-network sweep guard (#52)

class TestStaleSweepGuard:
    """The subnet-overlap retry must never remove a live sibling's network
    (#52); leftovers (no attached containers) are still cleaned up."""

    def _fake_docker(self, monkeypatch, containers_by_net):
        """containers_by_net maps network name -> attached container count."""
        import types
        nets = list(containers_by_net)
        ls = types.SimpleNamespace(stdout="\n".join(nets) + "\n")
        monkeypatch.setattr(supervisor.subprocess, "run",
                            lambda *a, **k: ls)
        removed = []
        def fake_command(*args, check=True):
            if args[1] == "network" and args[2] == "inspect":
                name = args[5]
                count = containers_by_net.get(name, 0)
                return types.SimpleNamespace(stdout=f"{count}\n", returncode=0)
            if args[1] == "network" and args[2] == "rm":
                removed.append(args[3])
            return types.SimpleNamespace(stdout="", returncode=0)
        monkeypatch.setattr(supervisor, "command", fake_command)
        return removed

    def test_leftover_network_is_removed(self, monkeypatch):
        removed = self._fake_docker(
            monkeypatch, {"aco-deadbeef1234__env_default": 0})
        preserved = supervisor._remove_stale_trial_networks("current-run")
        assert removed == ["aco-deadbeef1234__env_default"]
        assert preserved == []

    def test_live_sibling_network_is_preserved(self, monkeypatch):
        removed = self._fake_docker(
            monkeypatch, {"aco-livecont9999__env_default": 2,
                          "aco-deadbeef1234__env_default": 0})
        preserved = supervisor._remove_stale_trial_networks("current-run")
        assert removed == ["aco-deadbeef1234__env_default"]
        assert preserved == ["aco-livecont9999__env_default"]

    def test_own_network_never_touched(self, monkeypatch):
        own = supervisor._trial_network_name("current-run")
        removed = self._fake_docker(monkeypatch, {own: 1})
        preserved = supervisor._remove_stale_trial_networks("current-run")
        assert removed == []
        assert preserved == []

    def test_blocked_subnet_diagnostic_names_octet_and_survivor(self):
        err = supervisor._subnet_blocked_error(
            "current-run", ["aco-livecont9999__env_default"])
        msg = str(err)
        assert supervisor.gateway_subnet("current-run") in msg
        assert "aco-livecont9999__env_default" in msg
