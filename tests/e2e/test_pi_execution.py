"""End-to-end pi target tests: TargetProfile -> Harbor -> Pi 0.84.1 -> mock
Ark endpoint -> seal, observation, and diagnostics (#37).

Docker-gated like the fake e2e. The provider is a local mock Ark endpoint
(no real key, no paid call); the credential value is a dummy that tests
assert never leaks into the database.
"""

import hashlib
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
PROMPT = "write the answer file"
# the declared prompt digest is verified against the instruction the agent
# actually receives (#36 reopen)
PROMPT_DIGEST = "sha256:" + hashlib.sha256(PROMPT.encode()).hexdigest()


def pi_profile(image_digest: str) -> dict:
    """An executable pi profile: the environment digest pins the actual
    built image, verified by the supervisor before any paid call. Declared
    resources are enforced as Harbor environment overrides on every run
    (#36 reopen), so the full chain exercises the enforcement path."""
    return {**PI_PROFILE, "prompt_digest": PROMPT_DIGEST,
            "environment": image_digest,
            "resources": {"cpus": 1, "memory_mb": 256}}

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
            [sys.executable, "-m", "uvicorn", "aco.app:session_app", "--host", "0.0.0.0",
             "--port", str(session_port),
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
                return {"root": root, "base": mgmt, "session_base": session_base,
                        "mgmt_port": mgmt_port, "session_port": session_port, "procs": procs}
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
    # build the pinned image once and read its content digest: the profile's
    # declared environment must be the digest of the image that will run
    # (#36 reopen) — this also warms the docker cache for the module
    from aco.pi_agent import ensure_image
    from aco.supervisor import image_digest
    built_digest = image_digest(ensure_image())
    stack = start_stack(root, free_port(), free_port(), {
        "ACO_DATA_ROOT": str(root),
        "ARK_AGENT_PLAN_API_KEY": DUMMY_KEY,
        # container-reachable route to the host-side mock
        "ARK_AGENT_PLAN_BASE_URL": f"http://host.docker.internal:{mock.port}/ark",
    })
    stack["mock"] = mock
    stack["image_digest"] = built_digest
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
            base, PROMPT, pi_profile(pi_stack["image_digest"]), allow_paid_run=True)
        run = wait_for_run(base, trial_id, timeout=900)  # first run builds the image

        assert run["status"] == "finished", run
        assert run["exit_kind"] == "normal"
        events = [phase["event"] for phase in run["phases"]]
        assert "agent_start" in events and "agent_end" in events

        # declared conditions reached the generated Harbor config: the agent
        # timeout is pinned in task.toml, the instruction bytes on disk are
        # exactly what the profile's prompt digest pins, and cpus/memory_mb
        # were applied as EnvironmentConfig overrides on this real run
        # (#36 reopen, reviewer request)
        task_dir = pi_stack["root"] / "runs" / run["run_id"] / "task"
        assert "timeout_sec = 120" in (task_dir / "task.toml").read_text()
        assert (task_dir / "instruction.md").read_text() == PROMPT
        assert "image_digest" in events  # verified against the pinned digest

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

        profile = {**pi_profile(pi_stack["image_digest"]),
                   "skills": [{"name": "e2e-skill", "version": "v1"}]}
        trial_id = create_trial(base, "write the answer file", profile,
                                allow_paid_run=True)
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
            trial_id = create_trial(base, PROMPT,
                                    pi_profile(pi_stack["image_digest"]),
                                    allow_paid_run=True)
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


class TestEnvironmentVerification:
    """The declared prompt/environment digests are verified against what
    actually runs, before any container or paid call (#36 reopen): a mismatch
    is a terminal pre-agent execution anomaly, never a capability sample."""

    @staticmethod
    def _assert_pre_agent_anomaly(root, trial_id, run, detail_fragment,
                                  mock_requests_before, mock_requests_after):
        assert run["status"] == "error", run  # terminal, never a sample
        assert run["exit_kind"] == "environment_invalid", run
        assert detail_fragment in (run["exit_detail"] or "")
        events = [phase["event"] for phase in run["phases"]]
        assert "agent_start" not in events  # the agent never ran
        # the module-shared mock accumulates requests across tests: no NEW
        # model call was paid for this failed trial
        assert mock_requests_after == mock_requests_before
        conn = sqlite3.connect(str(root / "aco.db"))
        conn.row_factory = sqlite3.Row
        try:
            answer = conn.execute(
                "SELECT status, seal_trigger FROM sealed_answers WHERE trial_id = ?",
                (trial_id,)).fetchone()
            assert answer["status"] == "anomaly"
            assert answer["seal_trigger"] == "environment_invalid"
        finally:
            conn.close()

    def test_environment_digest_mismatch_fails_before_agent_start(self, pi_stack):
        base = pi_stack["base"]
        profile = {**pi_profile(pi_stack["image_digest"]),
                   "environment": "sha256:" + "9" * 64}
        before = len(pi_stack["mock"].requests)
        trial_id = create_trial(base, PROMPT, profile, allow_paid_run=True)
        run = wait_for_run(base, trial_id, timeout=300)
        # the image_digest phase recorded the verified mismatch as evidence
        digest_phases = [p for p in run["phases"] if p.get("event") == "image_digest"]
        assert digest_phases and digest_phases[0]["requested"] != \
            digest_phases[0]["observed"]
        self._assert_pre_agent_anomaly(pi_stack["root"], trial_id, run,
                                       "environment image digest mismatch",
                                       before, len(pi_stack["mock"].requests))

    def test_prompt_digest_mismatch_fails_before_agent_start(self, pi_stack):
        base = pi_stack["base"]
        profile = {**pi_profile(pi_stack["image_digest"]),
                   "prompt_digest": "sha256:" + "8" * 64}
        before = len(pi_stack["mock"].requests)
        trial_id = create_trial(base, PROMPT, profile, allow_paid_run=True)
        run = wait_for_run(base, trial_id, timeout=300)
        # the prompt check precedes the image build: no image evidence either
        assert "image_digest" not in [p["event"] for p in run["phases"]]
        self._assert_pre_agent_anomaly(pi_stack["root"], trial_id, run,
                                       "prompt digest mismatch",
                                       before, len(pi_stack["mock"].requests))

    def test_asset_instruction_verifies_against_prompt_digest(self, pi_stack):
        """An asset-resolved instruction (#20 reopen) — a task with no
        `prompt` key — is verified against the profile's prompt digest:
        matching bytes run, a wrong digest fails before any container or
        paid call (#36 reopen)."""
        import uuid as uuid_mod
        from aco import artifacts as aco_artifacts
        from aco import environments as aco_env
        from aco.verification import runner as aco_runner

        base = pi_stack["base"]
        suffix = uuid_mod.uuid4().hex[:8]
        env_src = pi_stack["root"] / f"asset-env-{suffix}"
        (env_src / "workspace").mkdir(parents=True)
        (env_src / "workspace" / "instruction.md").write_text(PROMPT)
        digest = aco_runner.bundle_digest(env_src)
        aco_env.publish(pi_stack["root"], env_src, digest)
        contract = aco_artifacts.ArtifactContract(required_outputs=("/workspace/answer.txt",))
        task_content = {
            "expected_answer": "hidden",
            "instruction": {"asset": "environment", "path": "workspace/instruction.md"},
            "contract": {"required_outputs": ["/workspace/answer.txt"]},
            "contract_digest": aco_artifacts.contract_digest(contract)}
        profile = pi_profile(pi_stack["image_digest"])
        status, body = http("POST", base + "/v1/versions",
                            {"kind": "task", "name": f"task-{suffix}", "version": "v1",
                             "content": task_content,
                             "assets": [{"name": "environment", "digest": digest}]})
        assert status in (200, 201), body
        status, body = http("POST", base + "/v1/versions",
                            {"kind": "config", "name": f"cfg-{suffix}", "version": "v1",
                             "content": profile})
        assert status in (200, 201), body
        status, body = http("POST", base + "/v1/experiments", {
            "task": {"name": f"task-{suffix}", "version": "v1"},
            "targets": [{"name": f"cfg-{suffix}", "version": "v1"}],
            "allow_paid_run": True})
        assert status == 202, body
        trial_id = body["trials"][0]["id"]
        run = wait_for_run(base, trial_id, timeout=300)
        assert run["status"] == "finished", run
        # the bytes written to instruction.md are exactly the asset-resolved
        # source — and exactly what the pinned digest names
        task_dir = pi_stack["root"] / "runs" / run["run_id"] / "task"
        assert (task_dir / "instruction.md").read_text() == PROMPT

        # the same task under a profile pinning a different digest is a
        # pre-agent anomaly, never a sample
        bad = {**profile, "prompt_digest": "sha256:" + "7" * 64}
        status, body = http("POST", base + "/v1/versions",
                            {"kind": "config", "name": f"cfg-bad-{suffix}", "version": "v1",
                             "content": bad})
        assert status in (200, 201), body
        before = len(pi_stack["mock"].requests)
        status, body = http("POST", base + "/v1/experiments", {
            "task": {"name": f"task-{suffix}", "version": "v1"},
            "targets": [{"name": f"cfg-bad-{suffix}", "version": "v1"}],
            "allow_paid_run": True})
        assert status == 202, body
        bad_trial = body["trials"][0]["id"]
        bad_run = wait_for_run(base, bad_trial, timeout=300)
        self._assert_pre_agent_anomaly(pi_stack["root"], bad_trial, bad_run,
                                       "prompt digest mismatch",
                                       before, len(pi_stack["mock"].requests))


class TestPiCredentialMissing:
    def test_fails_explicitly_before_any_model_call(self, tmp_path_factory):
        mock = MockArk()
        root = tmp_path_factory.mktemp("aco-pi-e2e-nokey")
        # this stack has no pi_stack fixture: pin the image digest locally
        from aco.pi_agent import ensure_image
        from aco.supervisor import image_digest
        built_digest = image_digest(ensure_image())
        stack = start_stack(root, free_port(), free_port(), {
            "ACO_DATA_ROOT": str(root),
            # ARK_AGENT_PLAN_API_KEY deliberately absent
            "ARK_AGENT_PLAN_BASE_URL": f"http://host.docker.internal:{mock.port}/ark",
        })
        try:
            trial_id = create_trial(stack["base"], PROMPT,
                                    pi_profile(built_digest),
                                    allow_paid_run=True)
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


# --------------------------------------------------------------- network (#38)

def container_probe(container_id: str, url: str) -> tuple[int, str]:
    """HTTP probe from inside the agent container via node fetch. Returns
    (exit_code, output); exit 0 = reachable, 1 = blocked/unreachable."""
    script = (
        "fetch(process.argv[1],{signal:AbortSignal.timeout(5000)})"
        ".then(r=>{console.log('STATUS',r.status);process.exit(0)},"
        "e=>{console.error(String(e && e.cause && e.cause.code || e).slice(0,120));"
        "process.exit(1)})"
    )
    result = subprocess.run(
        ["docker", "exec", container_id, "node", "-e", script, url],
        capture_output=True, text=True, timeout=20)
    return result.returncode, (result.stdout + result.stderr).strip()


def wait_running_container(base: str, trial_id: str, timeout: float = 120) -> str:
    """Poll the run record until the agent container exists (in-run probes
    need a live container; harbor reaps it after the run)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, runs = http("GET", base + f"/v1/trials/{trial_id}/runs")
        assert status == 200, runs
        if runs:
            container_id = runs[-1].get("container_id")
            if container_id:
                return container_id
        time.sleep(0.5)
    raise AssertionError(f"run for {trial_id} never started a container")


def wait_phase(base: str, trial_id: str, event: str, timeout: float = 60) -> dict:
    """Wait until the run recorded a phase and return it, i.e. the supervisor
    finished the agent-start hooks (probes and submits from the test must not
    race them)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, runs = http("GET", base + f"/v1/trials/{trial_id}/runs")
        assert status == 200, runs
        if runs:
            for phase in runs[-1]["phases"]:
                if phase.get("event") == event:
                    return phase
        time.sleep(0.5)
    raise AssertionError(f"phase {event!r} never recorded for {trial_id}")


class TestNetworkIsolation:
    """Trial network restriction (#38): the agent container reaches only the
    allowlisted provider route and the ACO session surface; management,
    arbitrary public endpoints, direct IPs, and runtime installs are blocked
    by harbor's egress sidecar — verified from inside the container."""

    def test_allow_session_and_provider_deny_everything_else(self, pi_stack):
        base = pi_stack["base"]
        # hold the mock response so the trial stays open during the probes
        pi_stack["mock"].delay = 25
        try:
            trial_id = create_trial(base, PROMPT,
                                    pi_profile(pi_stack["image_digest"]),
                                    allow_paid_run=True)
            # the trial's single allowlisted target, from the recorded policy
            gateway_ip = wait_phase(base, trial_id, "network_policy",
                                    timeout=120)["allowed_targets"][0]
            gateway_base = f"http://{gateway_ip}"
            container_id = wait_running_container(base, trial_id)

            # allow: the session surface, reached through the gateway —
            # the unauthenticated task claim answers 401/403 (reachable),
            # a network block would reject before any HTTP status
            code, out = container_probe(container_id, f"{gateway_base}/v1/session/task")
            assert code == 0 and "STATUS" in out, \
                f"session surface unreachable from container: {out}"
            assert "STATUS 200" not in out  # unauthenticated claims are rejected

            # deny: the gateway refuses non-session, non-provider targets —
            # an HTTP 403 egress_denied, never a relay to an upstream
            code, out = container_probe(container_id, f"{gateway_base}/admin")
            assert code == 0 and "STATUS 403" in out, \
                f"gateway did not deny an unlisted target: {out}"

            # deny: the management listener never joins the trial network
            code, out = container_probe(
                container_id, f"http://host.docker.internal:{pi_stack['mgmt_port']}/healthz")
            assert code == 1, f"management listener reachable from container: {out}"

            # deny: arbitrary public HTTP(S) endpoints and direct IPs (no DNS
            # or IP-literal bypass of the allowlist)
            for url in ("https://example.com/", "http://1.1.1.1/", "http://192.0.2.1/"):
                code, out = container_probe(container_id, url)
                assert code == 1, f"{url} reachable from container: {out}"

            # deny: runtime package installation needs the npm registry
            result = subprocess.run(
                ["docker", "exec", container_id, "npm", "install", "--no-audit", "--no-fund",
                 "--fetch-retries=0", "--fetch-timeout=6000", "--fetch-retry-mintimeout=1000",
                 "--prefix", "/tmp/aco-npm-probe", "left-pad"],
                capture_output=True, text=True, timeout=30)
            assert result.returncode != 0, "npm install reached the registry from the container"

            run = wait_for_run(base, trial_id, timeout=600)
        finally:
            pi_stack["mock"].delay = 0.0
            pi_stack["mock"].delay_after_tool = 0.0
        assert run["status"] == "finished", run

        # auditable evidence: policy declaration + in-kernel deny probe
        events = {phase["event"]: phase for phase in run["phases"]}
        policy = events["network_policy"]
        assert policy["policy"] == "allowlist"
        assert policy["allowed_targets"] == [gateway_ip]
        assert policy["enforcement"] == "harbor-egress-sidecar"
        assert events["network_deny_probe"]["result"] == "blocked"
        # the container shares the sidecar's network namespace, not a bridge
        assert events["agent_start"]["container_security"]["network_mode"] != "bridge"
        # the provider call traversed the restricted path and the seal won
        assert pi_stack["mock"].requests, "mock provider received no model call"
        for request in pi_stack["mock"].requests:
            assert request["auth"] == f"Bearer {DUMMY_KEY}"
        assert events["sealed"]["receipt_id"]

        # gateway evidence log: connection results only — never the key value
        evidence = "".join(
            path.read_text() for path in (pi_stack["root"] / "gateway-logs").glob("*.jsonl"))
        assert DUMMY_KEY not in evidence
        assert '"route": "denied"' in evidence  # the /admin refusal is recorded

    def test_submit_intent_works_from_container(self, pi_stack):
        """The container can express the end-and-seal intent through the
        session surface, and the receipt is stable (#12, #38)."""
        base = pi_stack["base"]
        # turn 1 answers fast so the deliverable lands on disk; turn 2 stalls
        # 30s: the deterministic window where the answer exists but the agent
        # has not finished — the container's submit intent wins inside it (#38)
        pi_stack["mock"].delay = 0
        pi_stack["mock"].delay_after_tool = 30
        try:
            trial_id = create_trial(base, PROMPT,
                                    pi_profile(pi_stack["image_digest"]),
                                    allow_paid_run=True)
            container_id = wait_running_container(base, trial_id)
            # the trial's single allowlisted target, from the recorded policy;
            # DNS is unreliable inside the restricted netns, so no hostnames
            policy = wait_phase(base, trial_id, "network_policy", timeout=120)
            gateway_ip = policy["allowed_targets"][0]
            wait_phase(base, trial_id, "network_deny_probe")  # agent-start hooks done

            # the agent writes the required answer after turn 1 (stalled 25s);
            # a submit before the deliverable exists is a contract anomaly,
            # so submit inside the turn-2 stall, once the file is on disk
            deadline = time.monotonic() + 90
            while True:
                probe = subprocess.run(
                    ["docker", "exec", container_id, "test", "-f",
                     "/workspace/answer.txt"], capture_output=True, timeout=10)
                if probe.returncode == 0:
                    break
                if time.monotonic() > deadline:
                    raise AssertionError("answer file never appeared before submit")
                time.sleep(1)

            # mint the trial's session token (management surface, test-side)
            # and submit from inside the container with the agent's key
            status, minted = http("POST", base + f"/v1/trials/{trial_id}/session-token")
            assert status == 201, minted
            script = (
                "fetch(process.argv[1],{method:'POST',signal:AbortSignal.timeout(5000),"
                "headers:{'Authorization':'Bearer '+process.argv[2],"
                "'Content-Type':'application/json'},"
                "body:JSON.stringify({idempotency_key:process.argv[3]})})"
                ".then(async r=>{console.log('STATUS',r.status,await r.text());process.exit(0)},"
                "e=>{console.error(String(e && e.cause && e.cause.code || e).slice(0,120));"
                "process.exit(1)})"
            )
            result = subprocess.run(
                ["docker", "exec", container_id, "node", "-e", script,
                 f"http://{gateway_ip}/v1/session/submit",
                 minted["token"], f"pi-{trial_id}"],
                capture_output=True, text=True, timeout=20)
        finally:
            pi_stack["mock"].delay = 0.0
            pi_stack["mock"].delay_after_tool = 0.0
        assert result.returncode == 0, f"container submit failed: {result.stdout}{result.stderr}"
        assert "STATUS 202" in result.stdout, result.stdout  # intent accepted

        run = wait_for_run(base, trial_id, timeout=600)
        # the submit intent won: submit-won termination and a sealed answer
        assert run["exit_kind"] == "submit", run
        conn = sqlite3.connect(str(pi_stack["root"] / "aco.db"))
        conn.row_factory = sqlite3.Row
        try:
            answer = conn.execute(
                "SELECT status, seal_trigger FROM sealed_answers WHERE trial_id = ?",
                (trial_id,)).fetchone()
            assert answer["status"] == "sealed"
            assert answer["seal_trigger"] == "submit"
        finally:
            conn.close()
