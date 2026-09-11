"""End-to-end execution tests: API + manager + supervisor + Harbor + Docker (#13).

Each test registers unique versions, creates a fixture experiment, and waits
for the singleton manager to run it through the supervisor. Skipped when no
Docker daemon is reachable.
"""

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

import pytest
import urllib.request

REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
).stdout.strip()
MGMT_TOKEN = "e2e-management-token"
# ponytail: matches the #48 loaded-CI budget; tunable for slow runners via env.
HEALTH_TIMEOUT = float(os.environ.get("ACO_E2E_HEALTH_TIMEOUT", "90"))
MGMT_AUTH = {"Authorization": f"Bearer {MGMT_TOKEN}"}
ENV = {
    **dict(__import__("os").environ),
    "PYTHONPATH": f"{REPO_ROOT}/src",
    "ACO_MANAGEMENT_TOKEN": MGMT_TOKEN,
}


def pre_migrate(env):
    """Migrate the data root once before spawning the API subprocesses.

    Both uvicorn processes (and the manager, via aco.app's import side effects)
    run db.migrate() on the same fresh data root at import time; concurrent
    migrations race on the schema_version insert and one process dies with
    `UNIQUE constraint failed` before /healthz can ever answer (#59).
    """
    subprocess.run(
        [sys.executable, "-c",
         "from aco.app import create_management_app; create_management_app()"],
        env=env, check=True,
    )


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=True)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not docker_available(), reason="docker daemon not reachable")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Content-Type": "application/json", **MGMT_AUTH},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    root = tmp_path_factory.mktemp("aco-e2e")
    mgmt_port, session_port = free_port(), free_port()
    mgmt = f"http://127.0.0.1:{mgmt_port}"
    session_base = f"http://127.0.0.1:{session_port}"
    server_env = {**ENV, "ACO_DATA_ROOT": str(root)}
    pre_migrate(server_env)
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aco.app:management_app", "--port", str(mgmt_port),
         "--log-level", "warning"],
        env=server_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    session_server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aco.app:session_app", "--port", str(session_port),
         "--log-level", "warning"],
        env=server_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    manager = subprocess.Popen(
        [sys.executable, "-m", "aco.execution", "--data-root", str(root),
         "--api-url", mgmt, "--api-token", MGMT_TOKEN, "--session-api-url", session_base],
        env=server_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + HEALTH_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if (http("GET", mgmt + "/healthz")[0] == 200
                    and http("GET", session_base + "/healthz")[0] == 200):
                break
        except Exception:
            time.sleep(0.2)
    else:
        server.kill()
        session_server.kill()
        manager.kill()
        raise RuntimeError(f"API did not become healthy after {HEALTH_TIMEOUT:.0f}s")
    yield {"root": root, "base": mgmt, "session_base": session_base,
           "server": server, "manager": manager}
    server.terminate()
    session_server.terminate()
    manager.terminate()
    server.wait(timeout=10)
    session_server.wait(timeout=10)
    manager.wait(timeout=10)


def create_trial(base: str, prompt: str, profile: dict, allow_paid_run: bool = False) -> str:
    suffix = uuid.uuid4().hex[:8]
    # the supervisor refuses to start an agent whose task declares no
    # artifact contract (#14): the fake agent always writes answer.txt
    from aco import artifacts as aco_artifacts
    contract = aco_artifacts.ArtifactContract(required_outputs=("/workspace/answer.txt",))
    task_content = {"prompt": prompt, "expected_answer": "hidden",
                    "contract": {"required_outputs": ["/workspace/answer.txt"]},
                    "contract_digest": aco_artifacts.contract_digest(contract)}
    for kind, name, content in (
        ("task", f"task-{suffix}", task_content),
        ("config", f"cfg-{suffix}", profile),
    ):
        status, _ = http("POST", base + "/v1/versions",
                         {"kind": kind, "name": name, "version": "v1", "content": content})
        assert status in (200, 201)
    status, body = http("POST", base + "/v1/experiments", {
        "task": {"name": f"task-{suffix}", "version": "v1"},
        "targets": [{"name": f"cfg-{suffix}", "version": "v1"}],
        "allow_paid_run": allow_paid_run,
    })
    assert status == 202, body
    return body["trials"][0]["id"]


def wait_for_run(base: str, trial_id: str, timeout: float = 180) -> dict:
    """Wait for a terminal run record (finished or error)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, runs_list = http("GET", base + f"/v1/trials/{trial_id}/runs")
        if status == 200 and runs_list and runs_list[0]["status"] in ("finished", "error"):
            return runs_list[0]
        time.sleep(0.5)
    raise AssertionError(f"no finished run for {trial_id} within {timeout}s")


def container_ids(run_id: str) -> list[str]:
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=aco.run={run_id}"],
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout.split()


def submit_response(db_path, trial_id: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM submissions WHERE trial_id = ?", (trial_id,)).fetchone()
    finally:
        conn.close()


class TestHarborExecution:
    def test_submit_flow_with_safety_invariants(self, stack):
        """Normal submit: unique task via Session API, end-intent recorded,
        verifier disabled, agent container without docker privileges."""
        base = stack["base"]
        trial_id = create_trial(base, "FAKE:submit\nwrite and submit", {"harness": "fake", "model": "none"})
        run = wait_for_run(base, trial_id)

        assert run["status"] == "finished"
        assert run["exit_kind"] == "normal"
        assert run["container_id"]
        assert run["harbor_version"] == "0.22.0"
        assert run["requested_profile"] == {"harness": "fake", "model": "none"}
        events = [phase["event"] for phase in run["phases"]]
        assert "agent_start" in events and "agent_end" in events

        # harbor scoring is disabled — recorded evidence, not assumption
        finished = next(p for p in run["phases"] if p["event"] == "trial_finished")
        assert finished["verifier_scored"] is False

        # the fake agent's submit became the trial's single end-intent,
        # sealed by the supervisor into the official answer (#14)
        submission = submit_response(stack["root"] / "aco.db", trial_id)
        assert submission is not None
        assert submission["status"] == "sealed"

        # agent container evidence recorded at runtime: no docker socket,
        # never privileged, no host network
        started = next(p for p in run["phases"] if p["event"] == "agent_start")
        security = started["container_security"]
        assert security == {"privileged": False, "network_mode": "none",
                            "docker_socket_mounted": False}
        assert container_ids(run["run_id"]) == []  # harbor cleaned up its container

    def test_single_slot_never_runs_two_trials(self, stack):
        """Two planned trials run strictly serially by the default single slot."""
        base = stack["base"]
        trial_a = create_trial(base, "FAKE:submit\nslot a", {"harness": "fake", "model": "none"})
        trial_b = create_trial(base, "FAKE:submit\nslot b", {"harness": "fake", "model": "none"})

        done = {}
        concurrent = []
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline and len(done) < 2:
            active = 0
            for trial_id in (trial_a, trial_b):
                _, runs_list = http("GET", base + f"/v1/trials/{trial_id}/runs")
                if runs_list:
                    run = runs_list[0]
                    if run["status"] in ("launching", "running"):
                        active += 1
                    elif trial_id not in done:
                        done[trial_id] = run
            concurrent.append(active)
            time.sleep(0.3)
        assert len(done) == 2, "both trials should finish"
        assert max(concurrent) <= 1, "single slot must never run two trials concurrently"
        assert all(run["exit_kind"] == "normal" for run in done.values())

    def test_supervisor_crash_keeps_intent_and_diagnostics(self, stack):
        base = stack["base"]
        trial_id = create_trial(base, "control\nlong enough to crash", {"harness": "fake", "model": "none"})
        # wait until the supervisor is running, then kill it mid-run
        deadline = time.monotonic() + 120
        run = None
        while time.monotonic() < deadline:
            _, runs_list = http("GET", base + f"/v1/trials/{trial_id}/runs")
            if runs_list and runs_list[0]["status"] == "running":
                run = runs_list[0]
                break
            time.sleep(0.3)
        assert run is not None, "run never started"
        pid = run["supervisor_pid"]
        subprocess.run(["kill", "-9", str(pid)], check=True)

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            _, runs_list = http("GET", base + f"/v1/trials/{trial_id}/runs")
            if runs_list and runs_list[0]["status"] == "error":
                run = runs_list[0]
                break
            time.sleep(0.5)
        assert run["status"] == "error"
        assert run["exit_kind"] == "supervisor_lost"
        # launch intent and associations survive the crash
        assert run["launched_at"]
        assert run["requested_profile"] == {"harness": "fake", "model": "none"}
        assert run["container_id"]
        # bounded label-based cleanup is asynchronous — wait for it
        deadline = time.monotonic() + 30
        leftover = container_ids(run["run_id"])
        while leftover and time.monotonic() < deadline:
            time.sleep(0.5)
            leftover = container_ids(run["run_id"])
        assert leftover == [], f"containers not reaped after supervisor crash: {leftover}"

    def test_foreground_exit_recorded_as_agent_error(self, stack):
        base = stack["base"]
        trial_id = create_trial(base, "FAKE:exit\nno submit", {"harness": "fake", "model": "none"})
        run = wait_for_run(base, trial_id)
        assert run["status"] == "finished"
        assert run["exit_kind"] == "agent_error"
        assert "NonZeroAgentExitCodeError" in (run["exit_detail"] or "")
        # no end-intent was submitted
        assert submit_response(stack["root"] / "aco.db", trial_id) is None

    def test_background_writer_outlives_agent_exit(self, stack):
        base = stack["base"]
        trial_id = create_trial(base, "FAKE:background\nkeep writing", {"harness": "fake", "model": "none"})
        run = wait_for_run(base, trial_id)
        assert run["status"] == "finished" and run["exit_kind"] == "normal"
        # harbor artifact collection captures the background writer's file
        workspace = sorted((stack["root"] / "runs").rglob("bg.txt"))
        assert workspace, "background writer output missing from collected artifacts"

    def test_unsupported_target_fails_without_side_effects(self, stack):
        base = stack["base"]
        trial_id = create_trial(base, "never runs", {"harness": "codex", "model": "gpt"})
        run = wait_for_run(base, trial_id)
        assert run["status"] == "error"
        assert run["exit_kind"] == "unsupported_target"
        assert "unsupported harness 'codex'" in run["exit_detail"]
        # explicit failure only: no container was ever created
        assert run["container_id"] is None
        assert "agent_start" not in [p["event"] for p in run["phases"]]


def published_dirs(root) -> list:
    return sorted((root / "answers").glob("*")) if (root / "answers").is_dir() else []


def read_manifest(answer_dir) -> dict:
    return json.loads((answer_dir / "manifest.json").read_text())


class TestSealedAnswers:
    def test_submit_seals_official_answer_with_stable_receipt(self, stack):
        """The submit trigger seals through the pause/copy boundary: the
        published answer is content-addressed, read-only, and its receipt is
        stable across repeated queries (#14)."""
        import hashlib
        import stat as stat_mod

        base = stack["base"]
        trial_id = create_trial(base, "FAKE:submit\nseal me", {"harness": "fake", "model": "none"})
        run = wait_for_run(base, trial_id)

        events = [p["event"] for p in run["phases"]]
        assert "sealed" in events, run["phases"]
        sealed_phase = next(p for p in run["phases"] if p["event"] == "sealed")
        digest = sealed_phase["answer_digest"]
        answer_dir = stack["root"] / "answers" / digest
        assert answer_dir.is_dir()

        # the official answer is the frozen workspace, published read-only
        answer_file = answer_dir / "workspace" / "answer.txt"
        assert answer_file.is_file()
        assert stat_mod.S_IMODE(answer_file.stat().st_mode) & 0o222 == 0
        manifest = read_manifest(answer_dir)
        assert manifest["trial_id"] == trial_id
        assert manifest["trigger"] == "submit"
        entry = next(e for e in manifest["files"] if e["path"] == "workspace/answer.txt")
        assert entry["sha256"] == hashlib.sha256(answer_file.read_bytes()).hexdigest()
        assert manifest["changes"]["added"] == ["workspace/answer.txt"]

        # registration: the receipt in the submissions row matches the sealed
        # answer and is stable across repeated reads (#14)
        conn = sqlite3.connect(stack["root"] / "aco.db")
        conn.row_factory = sqlite3.Row
        try:
            submission = conn.execute(
                "SELECT * FROM submissions WHERE trial_id = ?", (trial_id,)
            ).fetchone()
        finally:
            conn.close()
        assert submission["receipt_id"] == sealed_phase["receipt_id"]
        assert submission["status"] == "sealed"
        assert submission["idempotency_key"]  # the agent's single end-intent

        # the session capability expired with the trial: no new token can be
        # minted for the finished trial (#12 reopen)
        status, body = http("POST", base + f"/v1/trials/{trial_id}/session-token", {})
        assert status == 409 and body["error"]["code"] == "trial_not_runnable", body

        # Harbor's post-hoc artifact dir is a different location and never
        # takes the answers/ place
        assert answer_dir in published_dirs(stack["root"])

    def test_sealed_answer_excludes_post_submit_writes(self, stack):
        """The agent keeps writing after its submit intent (#16 reopen): the
        watchdog stops the container, and the sealed answer contains nothing
        written after the submit. The late writes start >= 2s after the POST
        (vs a 0.5s poll), so exclusion is deterministic; if the watchdog ever
        stops firing, late.txt appears in the sealed answer and this fails."""
        base = stack["base"]
        trial_id = create_trial(base, "FAKE:submit-late-write\nlate writes stay out",
                                {"harness": "fake", "model": "none"})
        run = wait_for_run(base, trial_id, timeout=60)
        assert run["exit_kind"] == "submit"

        events = [p["event"] for p in run["phases"]]
        # non-vacuous proof the watchdog stopped a live agent: the poll fired
        # while the agent was still mid-run (sleeping past its submit)
        assert "submit_watch_fired" in events, run["phases"]
        sealed_phase = next(p for p in run["phases"] if p["event"] == "sealed")
        answer_dir = stack["root"] / "answers" / sealed_phase["answer_digest"]
        manifest = read_manifest(answer_dir)
        assert manifest["trigger"] == "submit"

        # the sealed workspace froze at submit time: pre-submit answer.txt,
        # no post-submit late.txt
        assert (answer_dir / "workspace" / "answer.txt").is_file()
        assert list(answer_dir.glob("workspace/late*")) == []

        conn = sqlite3.connect(stack["root"] / "aco.db")
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT seal_trigger, status FROM sealed_answers WHERE trial_id = ?",
                (trial_id,)).fetchone()
        finally:
            conn.close()
        assert row["status"] == "sealed" and row["seal_trigger"] == "submit"

    def test_background_writer_cannot_change_sealed_answer(self, stack):
        """Parent (agent) exits while a background writer keeps writing:
        the sealed snapshot freezes at pause time and stays byte-stable, while
        Harbor's later diagnostic collection sees the writer's extra output."""
        import hashlib

        base = stack["base"]
        trial_id = create_trial(base, "FAKE:background\nkeep writing", {"harness": "fake", "model": "none"})
        run = wait_for_run(base, trial_id)
        assert run["exit_kind"] == "normal"
        sealed_phase = next(p for p in run["phases"] if p["event"] == "sealed")
        answer_dir = stack["root"] / "answers" / sealed_phase["answer_digest"]
        sealed_bg = answer_dir / "workspace" / "bg.txt"
        assert sealed_bg.is_file()
        frozen_digest = hashlib.sha256(sealed_bg.read_bytes()).hexdigest()

        # the detached writer keeps appending after the freeze: the published
        # answer never changes
        time.sleep(3)
        assert hashlib.sha256(sealed_bg.read_bytes()).hexdigest() == frozen_digest
        assert sealed_phase["answer_digest"] in {d.name for d in published_dirs(stack["root"])}

        # Harbor's diagnostics captured at least as much (likely more), in a
        # different location, and cannot override the sealed answer
        harbor_bg = sorted((stack["root"] / "runs").rglob("bg.txt"))
        assert harbor_bg, "diagnostic artifact missing"
        assert len(harbor_bg[0].read_text().splitlines()) >= len(sealed_bg.read_text().splitlines())

    def test_exit_without_submission_still_seals_workspace(self, stack):
        """Agent exit with no end-intent: the workspace is still sealed with
        trigger 'exit' and a generated receipt — one official answer per trial."""
        base = stack["base"]
        trial_id = create_trial(base, "FAKE:exit\nno submit", {"harness": "fake", "model": "none"})
        run = wait_for_run(base, trial_id)
        assert run["exit_kind"] == "agent_error"
        assert submit_response(stack["root"] / "aco.db", trial_id) is None

        sealed_phase = next(p for p in run["phases"] if p["event"] == "sealed")
        answer_dir = stack["root"] / "answers" / sealed_phase["answer_digest"]
        manifest = read_manifest(answer_dir)
        assert manifest["trigger"] == "exit"
        assert (answer_dir / "workspace" / "answer.txt").is_file()
        conn = sqlite3.connect(stack["root"] / "aco.db")
        conn.row_factory = sqlite3.Row
        try:
            sealed = conn.execute("SELECT receipt_id, status FROM sealed_answers WHERE trial_id = ?", (trial_id,)).fetchone()
        finally:
            conn.close()
        assert sealed["status"] == "sealed"
        assert sealed["receipt_id"]  # generated: no submission receipt existed

    def test_harbor_agent_timeout_uses_timeout_verdict_path(self, stack):
        """Harbor's own AgentTimeoutError lands on the same verdict path as
        the ACO outer deadline (#16 reopen): a timeout run, a sealed answer
        with trigger 'timeout', and a timeout_verdict event carrying the
        planned deadline, the freeze time, and the eligibility verdict."""
        base = stack["base"]
        trial_id = create_trial(base, "FAKE:sleep\nhang past the deadline",
                                {"harness": "fake", "model": "none"})
        run = wait_for_run(base, trial_id, timeout=120)
        assert run["exit_kind"] == "timeout"  # never a plain agent_error

        sealed_phase = next(p for p in run["phases"] if p["event"] == "sealed")
        assert sealed_phase["event"] == "sealed"
        manifest = read_manifest(stack["root"] / "answers" / sealed_phase["answer_digest"])
        # the manifest is content-addressed and immutable at seal time; the
        # authoritative termination reason is the DB seal_trigger below
        assert manifest["trigger"] in ("exit", "timeout")

        timeout_phase = next(p for p in run["phases"] if p["event"] == "trial_timeout")
        assert timeout_phase["source"] in ("outer_deadline", "harbor_agent_timeout")

        conn = sqlite3.connect(stack["root"] / "aco.db")
        conn.row_factory = sqlite3.Row
        try:
            verdict = conn.execute(
                "SELECT reason, detail FROM lifecycle_events"
                " WHERE trial_id = ? AND event = 'timeout_verdict'", (trial_id,)
            ).fetchone()
            trial_status = conn.execute(
                "SELECT status FROM trials WHERE id = ?", (trial_id,)).fetchone()[0]
            answer = conn.execute(
                "SELECT status, seal_trigger FROM sealed_answers WHERE trial_id = ?",
                (trial_id,)).fetchone()
        finally:
            conn.close()
        assert verdict is not None
        detail = json.loads(verdict["detail"])
        assert {"planned_deadline", "frozen_at", "verdict"} <= set(detail)
        assert verdict["reason"] == "within_tolerance"  # hang ~= the deadline, grace absorbs it
        assert answer["seal_trigger"] == "timeout"
        assert answer["status"] == "sealed"  # eligible: stays a capability sample
        assert trial_status == "sealed"  # the funnel terminalised the trial


class TestEnvironmentMaterialization:
    """Registered TaskVersion environments: verified bytes materialized into
    /workspace before any agent call; broken sources fail pre-agent (#20)."""

    def _register_with_environment(self, base: str, root, *, tamper: bool = False):
        suffix = uuid.uuid4().hex[:8]
        from aco import artifacts as aco_artifacts
        from aco.verification import runner
        env_dir = root / "env-src"
        (env_dir / "workspace").mkdir(parents=True, exist_ok=True)
        (env_dir / "workspace" / "README.md").write_text(
            "FAKE:submit\nimplement add(a, b) per the registered environment\n"
            f"bundle instance: {suffix}\n")
        (env_dir / "workspace" / "starter.py").write_text(f"a, b = 2, 4  # {suffix}\n")
        digest = runner.bundle_digest(env_dir)
        store_dir = root / "environments" / digest
        if not store_dir.exists():
            shutil.copytree(env_dir, store_dir)
        if tamper:
            (store_dir / "workspace" / "README.md").write_text("tampered!")
        contract = aco_artifacts.ArtifactContract(required_outputs=("/workspace/answer.txt",))
        task_content = {"instruction": {"asset": "environment", "path": "workspace/README.md"},
                        "contract": {"required_outputs": ["/workspace/answer.txt"]},
                        "contract_digest": aco_artifacts.contract_digest(contract)}
        for kind, name, content, assets in (
            ("task", f"task-{suffix}", task_content, [{"name": "environment", "digest": digest}]),
            ("config", f"cfg-{suffix}", {"harness": "fake", "model": "none"}, []),
        ):
            status, body = http("POST", base + "/v1/versions",
                                {"kind": kind, "name": name, "version": "v1",
                                 "content": content, "assets": assets})
            assert status in (200, 201), body
        status, body = http("POST", base + "/v1/experiments", {
            "task": {"name": f"task-{suffix}", "version": "v1"},
            "targets": [{"name": f"cfg-{suffix}", "version": "v1"}],
        })
        assert status == 202, body
        return body["trials"][0]["id"], env_dir, digest

    def test_registered_environment_materializes_into_trial(self, stack):
        base, root = stack["base"], stack["root"]
        trial_id, env_dir, digest = self._register_with_environment(base, root)
        run = wait_for_run(base, trial_id)

        assert run["status"] == "finished" and run["exit_kind"] == "normal"
        # the instruction came from the registered asset — the version
        # declares no prompt at all
        instruction = (root / "runs" / run["run_id"] / "task" / "instruction.md").read_text()
        assert instruction == (env_dir / "workspace" / "README.md").read_text()
        # the initial environment files are inside the sealed answer, with the
        # exact registered digests: baseline, instruction, and seal share one
        # environment digest
        conn = sqlite3.connect(root / "aco.db")
        seal_digest = conn.execute(
            "SELECT digest FROM sealed_answers WHERE trial_id = ?", (trial_id,)).fetchone()[0]
        conn.close()
        answer_dir = root / "answers" / seal_digest
        sealed = {entry["path"]: entry["sha256"] for entry in read_manifest(answer_dir)["files"]}
        for rel in ("README.md", "starter.py"):
            expected = (env_dir / "workspace" / rel).read_bytes()
            assert sealed[f"workspace/{rel}"] == hashlib.sha256(expected).hexdigest()

    def test_tampered_environment_fails_before_agent_start(self, stack):
        base, root = stack["base"], stack["root"]
        trial_id, _, _ = self._register_with_environment(base, root, tamper=True)
        run = wait_for_run(base, trial_id)

        assert run["status"] == "error" and run["exit_kind"] == "environment_invalid"
        events = [phase["event"] for phase in run["phases"]]
        assert "agent_start" not in events  # failed before any agent/paid call
        conn = sqlite3.connect(root / "aco.db")
        conn.row_factory = sqlite3.Row
        try:
            answer = conn.execute(
                "SELECT status, seal_trigger FROM sealed_answers WHERE trial_id = ?",
                (trial_id,)).fetchone()
            trial_status = conn.execute(
                "SELECT status FROM trials WHERE id = ?", (trial_id,)).fetchone()[0]
        finally:
            conn.close()
        assert answer["seal_trigger"] == "environment_invalid" and answer["status"] == "anomaly"
        assert trial_status == "anomaly"
