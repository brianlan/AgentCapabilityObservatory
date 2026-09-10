"""End-to-end pi target tests: TargetProfile -> Harbor -> Pi 0.84.1 -> mock
Ark endpoint -> seal, observation, and diagnostics (#37).

Docker-gated like the fake e2e. The provider is a local mock Ark endpoint
(no real key, no paid call); the credential value is a dummy that tests
assert never leaks into the database.
"""

import os
import sqlite3
import subprocess
import sys
import time

import pytest

from mock_ark import MockArk
from test_execution import (
    MGMT_TOKEN,
    REPO_ROOT,
    create_trial,
    free_port,
    http,
    wait_for_run,
)

PI_PROFILE = {
    "schema_version": 1,
    "harness": "pi",
    "harness_version": "0.84.1",
    "model": "glm-5.3-flash",
    "thinking": "max",
    "provider": "ark-agent-plan",
    "provider_api_style": "openai-responses",
    "adapter_version": "0.1.0",
    "credentials": ["ark-agent-plan-main"],
}
DUMMY_KEY = "e2e-dummy-key"

SKILL_MD = (
    "---\n"
    "name: e2e-skill\n"
    "description: reminder that answers must go to /workspace/answer.txt\n"
    "---\n\n"
    "When asked to record an answer, write it to /workspace/answer.txt.\n"
)


def docker_available() -> bool:
    import shutil
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=True)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not docker_available(), reason="docker daemon not reachable")


def start_stack(root, mgmt_port, session_port, extra_env):
    env = {
        **os.environ,
        "PYTHONPATH": f"{REPO_ROOT}/src",
        "ACO_MANAGEMENT_TOKEN": MGMT_TOKEN,
        **extra_env,
    }
    mgmt = f"http://127.0.0.1:{mgmt_port}"
    session_base = f"http://127.0.0.1:{session_port}"
    logs = root / "stack-logs"
    logs.mkdir(parents=True, exist_ok=True)
    server_env = {**env, "ACO_DATA_ROOT": str(root)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "aco.app:management_app", "--port", str(mgmt_port),
             "--log-level", "warning"], env=server_env,
            stdout=subprocess.DEVNULL, stderr=open(logs / "mgmt.log", "w")),
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "aco.app:session_app", "--port", str(session_port),
             "--log-level", "warning"], env=server_env,
            stdout=subprocess.DEVNULL, stderr=open(logs / "session.log", "w")),
        subprocess.Popen(
            [sys.executable, "-m", "aco.execution", "--data-root", str(root),
             "--api-url", mgmt, "--api-token", MGMT_TOKEN, "--session-api-url", session_base],
            env=env, stdout=subprocess.DEVNULL, stderr=open(logs / "manager.log", "w")),
    ]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if (http("GET", mgmt + "/healthz")[0] == 200
                    and http("GET", session_base + "/healthz")[0] == 200):
                return {"root": root, "base": mgmt, "session_base": session_base, "procs": procs}
        except Exception:
            time.sleep(0.2)
    for proc in procs:
        proc.kill()
    raise RuntimeError("API did not become healthy")


def stop_stack(stack):
    for proc in stack["procs"]:
        proc.terminate()
    for proc in stack["procs"]:
        proc.wait(timeout=10)


@pytest.fixture(scope="module")
def pi_stack(tmp_path_factory):
    mock = MockArk()
    root = tmp_path_factory.mktemp("aco-pi-e2e")
    stack = start_stack(root, free_port(), free_port(), {
        "ACO_DATA_ROOT": str(root),
        "ARK_AGENT_PLAN_API_KEY": DUMMY_KEY,
        # container-reachable route to the host-side mock
        "ARK_AGENT_PLAN_BASE_URL": f"http://host.docker.internal:{mock.port}",
    })
    stack["mock"] = mock
    yield stack
    stop_stack(stack)
    mock.close()


def db_dump(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        parts = []
        for table in ("trials", "trial_runs", "sealed_answers", "versions"):
            for row in conn.execute(f"SELECT * FROM {table}"):
                parts.append(str(dict(row)))
        return "\n".join(parts)
    finally:
        conn.close()


class TestPiExecution:
    def test_full_chain_with_mock_provider(self, pi_stack):
        """Claim -> pi tool call -> submit -> sealed answer + observation."""
        base = pi_stack["base"]
        trial_id = create_trial(
            base, "write the answer file", {**PI_PROFILE})
        run = wait_for_run(base, trial_id, timeout=900)  # first run builds the image

        assert run["status"] == "finished", run
        assert run["exit_kind"] == "normal"
        events = [phase["event"] for phase in run["phases"]]
        assert "agent_start" in events and "agent_end" in events

        # runtime observation from the pi transcript, with its source
        status, trial = http("GET", base + f"/v1/trials/{trial_id}")
        assert status == 200
        observation = trial["runtime_observation"]
        assert observation["source"] == "pi_json_transcript"
        assert observation["provider"] == "ark-agent-plan"
        assert observation["model"] == "glm-5.3-flash"
        assert observation["stop_reason"] == "stop"
        assert observation["usage"]["input"] > 0 and observation["usage"]["output"] > 0
        assert "bash" in observation["tool_calls"]
        assert len(trial["fingerprint"]) == 64

        # the mock saw the credential injected and the pinned model, and the
        # key value appears nowhere in the database
        assert pi_stack["mock"].requests, "mock provider received no model call"
        for request in pi_stack["mock"].requests:
            assert request["auth"] == f"Bearer {DUMMY_KEY}"
            assert request["model"] == "glm-5.3-flash"
        dump = db_dump(str(pi_stack["root"] / "aco.db"))
        assert DUMMY_KEY not in dump

    def test_declared_skill_is_verified_and_loaded(self, pi_stack):
        """An opt-in SkillVersion is mounted read-only, verified against the
        registered digest, and loaded via explicit --skill (#39)."""
        base = pi_stack["base"]
        # trusted-side import into the stack's local data root
        from aco import db as aco_db, skills as aco_skills
        bundle = pi_stack["root"] / "e2e-skill-bundle"
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "SKILL.md").write_text(SKILL_MD)
        conn = aco_db.connect(pi_stack["root"] / "aco.db")
        try:
            record = aco_skills.import_skill(bundle, pi_stack["root"],
                                             "e2e-skill", "v1", conn)
        finally:
            conn.close()

        profile = {**PI_PROFILE,
                   "skills": [{"name": "e2e-skill", "version": "v1"}]}
        trial_id = create_trial(base, "write the answer file", profile)
        run = wait_for_run(base, trial_id, timeout=300)

        assert run["status"] == "finished", run
        assert run["exit_kind"] == "normal"
        skills_phases = [p for p in run["phases"] if p.get("event") == "skills"]
        assert skills_phases and skills_phases[0]["verified"] is True
        entry = skills_phases[0]["skills"][0]
        assert entry["verified"] is True
        assert entry["requested"] == record["content"]["bundle"]["digest"]
        assert entry["requested"] == entry["observed"]
        # the declared bytes must actually be mounted read-only into the
        # container — this assertion fails if skill_mounts is never wired
        # through execute_run (the vacuous-mount regression)
        compose = (pi_stack["root"] / "runs" / run["run_id"] / "task"
                   / "offline.yaml").read_text()
        from aco import pi_agent as aco_pi_agent
        assert (f'{pi_stack["root"]}/skills/{record["id"]}'
                f':{aco_pi_agent._PI_CONTAINER_SKILL_ROOT}/e2e-skill:ro') in compose
        status, trial = http("GET", base + f"/v1/trials/{trial_id}")
        assert status == 200
        assert trial["runtime_observation"]["source"] == "pi_json_transcript"

    def test_provider_auth_failure_is_an_anomaly(self, pi_stack):
        """A 401 from the provider never becomes a capability sample."""
        base = pi_stack["base"]
        pi_stack["mock"].scenario = "auth_fail"
        try:
            trial_id = create_trial(base, "write the answer file", {**PI_PROFILE})
            run = wait_for_run(base, trial_id, timeout=300)
        finally:
            pi_stack["mock"].scenario = "tool_call"

        # terminal run with a classified provider failure; the answer row is
        # an execution-condition anomaly, never sealed
        assert run["exit_kind"] == "provider_failure"
        failures = [p for p in run["phases"] if p.get("event") == "target_failure"]
        assert failures, run["phases"]
        # the answer row is an execution-condition anomaly, never sealed
        conn = sqlite3.connect(str(pi_stack["root"] / "aco.db"))
        conn.row_factory = sqlite3.Row
        try:
            answer = conn.execute(
                "SELECT status, seal_trigger FROM sealed_answers WHERE trial_id = ?",
                (trial_id,)).fetchone()
            assert answer["status"] == "anomaly"
            assert answer["seal_trigger"] == "target_failure"
        finally:
            conn.close()
        assert pi_stack["mock"].requests  # the model call was attempted


class TestPiCredentialMissing:
    def test_fails_explicitly_before_any_model_call(self, tmp_path_factory):
        mock = MockArk()
        root = tmp_path_factory.mktemp("aco-pi-e2e-nokey")
        stack = start_stack(root, free_port(), free_port(), {
            "ACO_DATA_ROOT": str(root),
            # ARK_AGENT_PLAN_API_KEY deliberately absent
            "ARK_AGENT_PLAN_BASE_URL": f"http://host.docker.internal:{mock.port}",
        })
        try:
            trial_id = create_trial(stack["base"], "write the answer file", {**PI_PROFILE})
            run = wait_for_run(stack["base"], trial_id, timeout=300)
        finally:
            stop_stack(stack)
            mock.close()

        # terminal run with a classified execution-condition exit (#37)
        assert run["exit_kind"] == "harness_failure"
        assert "credential_missing" in (run["exit_detail"] or "")
        failures = [p for p in run["phases"] if p.get("event") == "target_failure"]
        assert failures and failures[-1]["failure_class"] == "credential_missing"
        assert mock.requests == []  # explicit failure before any model call
        conn = sqlite3.connect(str(root / "aco.db"))
        conn.row_factory = sqlite3.Row
        try:
            answer = conn.execute(
                "SELECT status FROM sealed_answers WHERE trial_id = ?", (trial_id,)).fetchone()
            assert answer["status"] == "anomaly"
        finally:
            conn.close()
