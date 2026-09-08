"""End-to-end execution tests: API + manager + supervisor + Harbor + Docker (#13).

Each test registers unique versions, creates a fixture experiment, and waits
for the singleton manager to run it through the supervisor. Skipped when no
Docker daemon is reachable.
"""

import json
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
ENV = {
    **dict(__import__("os").environ),
    "PYTHONPATH": f"{REPO_ROOT}/src",
}


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
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    root = tmp_path_factory.mktemp("aco-e2e")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aco.app:app", "--port", str(port), "--log-level", "warning"],
        env={**ENV, "ACO_DATA_ROOT": str(root)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    manager = subprocess.Popen(
        [sys.executable, "-m", "aco.execution", "--data-root", str(root), "--api-url", base],
        env=ENV,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if http("GET", base + "/healthz")[0] == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        server.kill()
        manager.kill()
        raise RuntimeError("API did not become healthy")
    yield {"root": root, "base": base, "server": server, "manager": manager}
    server.terminate()
    manager.terminate()
    server.wait(timeout=10)
    manager.wait(timeout=10)


def create_trial(base: str, prompt: str, profile: dict) -> str:
    suffix = uuid.uuid4().hex[:8]
    for kind, name, content in (
        ("task", f"task-{suffix}", {"prompt": prompt, "expected_answer": "hidden"}),
        ("config", f"cfg-{suffix}", profile),
    ):
        status, _ = http("POST", base + "/v1/versions",
                         {"kind": kind, "name": name, "version": "v1", "content": content})
        assert status in (200, 201)
    status, body = http("POST", base + "/v1/experiments", {
        "task": {"name": f"task-{suffix}", "version": "v1"},
        "targets": [{"name": f"cfg-{suffix}", "version": "v1"}],
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

        # the fake agent's submit became the trial's single end-intent
        submission = submit_response(stack["root"] / "aco.db", trial_id)
        assert submission is not None
        assert submission["status"] == "accepted"

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
        assert container_ids(run["run_id"]) == []  # bounded label-based cleanup

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
