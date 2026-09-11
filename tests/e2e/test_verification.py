"""End-to-end verification tests: API + manager + real verifier container (#15).

Sealed answers are created directly (registration + published content) so no
agent run is needed; the manager picks queued verifications from its normal
loop and runs the real verifier fixture container. Skipped when no Docker
daemon is reachable.
"""

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from aco import artifacts, lifecycle
from aco.verification import runner as verification_runner
from test_execution import HEALTH_TIMEOUT, pre_migrate

FIXTURES = Path(__file__).parent.parent / "fixtures" / "verifier"
SCHEMA = "aco.verification-result/v1"
EXPECTED = "EXPECTED-ANSWER"


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


MGMT_TOKEN = "e2e-verification-management-token"
MGMT_AUTH = {"Authorization": f"Bearer {MGMT_TOKEN}"}


def http(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    import urllib.error
    import urllib.request

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
    root = tmp_path_factory.mktemp("aco-e2e-verification")
    mgmt_port, session_port = free_port(), free_port()
    base = f"http://127.0.0.1:{mgmt_port}"
    session_base = f"http://127.0.0.1:{session_port}"
    env = {**dict(__import__("os").environ), "PYTHONPATH": f"{_repo_root()}/src",
           "ACO_MANAGEMENT_TOKEN": MGMT_TOKEN}
    server_env = {**env, "ACO_DATA_ROOT": str(root)}
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
         "--api-url", base, "--api-token", MGMT_TOKEN, "--session-api-url", session_base],
        env=server_env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + HEALTH_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if (http("GET", base + "/healthz")[0] == 200
                    and http("GET", session_base + "/healthz")[0] == 200):
                break
        except Exception:
            time.sleep(0.2)
    else:
        server.kill()
        session_server.kill()
        manager.kill()
        raise RuntimeError(f"API did not become healthy after {HEALTH_TIMEOUT:.0f}s")
    yield {"root": root, "base": base}
    server.terminate()
    session_server.terminate()
    manager.terminate()
    server.wait(timeout=10)
    session_server.wait(timeout=10)
    manager.wait(timeout=10)


def _repo_root() -> str:
    return subprocess.run(["git", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, check=True).stdout.strip()


def make_sealed(root: Path, content: bytes | None) -> tuple[str, str]:
    """Register a trial + sealed answer directly (no agent run). Returns
    (trial_id, digest). The trial status keeps the manager from running it."""
    conn = sqlite3.connect(root / "aco.db")
    conn.row_factory = sqlite3.Row
    suffix = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO versions (id, kind, name, version, content, created_at) VALUES"
        f" ('v-task-{suffix}', 'task', 'task-{suffix}', 'v1', '{{}}', 'now'),"
        f" ('v-cfg-{suffix}', 'config', 'cfg-{suffix}', 'v1', '{{\"harness\": \"fake\", \"model\": \"none\"}}', 'now')"
    )
    conn.execute(
        f"INSERT INTO experiments (id, status, requested, created_at) VALUES"
        f" ('e-{suffix}', 'planned', '{{\"task\": {{\"name\": \"task-{suffix}\", \"version\": \"v1\"}}}}', 'now')"
    )
    trial_id = f"t-{suffix}"
    conn.execute(
        "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
        " repetition, plan_order, requested, status)"
        f" VALUES ('{trial_id}', 'e-{suffix}', 'v-task-{suffix}', 'v-cfg-{suffix}', 1, 1, '{{}}', 'verification-only')"
    )
    run_id = f"run-{suffix}"
    conn.execute(
        "INSERT INTO trial_runs (run_id, trial_id, status, requested_profile, launched_at,"
        " supervisor_pid, created_at) VALUES (?, ?, 'finished', '{}', 'now', 1, 'now')",
        (run_id, trial_id),
    )
    conn.commit()
    conn.close()

    staging = root / "sealing" / f"seed-{suffix}" / "staging"
    (staging / "workspace").mkdir(parents=True)
    if content is not None:
        (staging / "workspace" / "answer.txt").write_bytes(content)
    manifest = artifacts.build_manifest(staging, trial_id, run_id, "submit", None)
    digest = artifacts.manifest_digest(manifest)
    (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    artifacts.publish(staging, digest, root / "answers")
    conn = sqlite3.connect(root / "aco.db")
    conn.execute(
        "INSERT INTO sealed_answers (trial_id, run_id, receipt_id, digest, manifest,"
        " seal_trigger, trigger_at, frozen_at, copied_at, published_at, registered_at, status)"
        " VALUES (?, ?, ?, ?, ?, 'submit', 'now', 'now', 'now', 'now', 'now', 'sealed')",
        (trial_id, run_id, f"receipt-{trial_id}", digest, json.dumps(manifest, sort_keys=True)),
    )
    conn.commit()
    conn.close()
    return trial_id, digest


def register_scorer(base: str, root: Path, script: str, config: dict,
                    entrypoint: list[str] | None = None, name_suffix: str = "") -> str:
    """Place the trusted bundle and register the scorer version; returns id."""
    suffix = uuid.uuid4().hex[:8]
    bundle_tmp = root / "bundle-prep" / f"bundle-{suffix}"
    bundle_tmp.mkdir(parents=True)
    shutil.copy(FIXTURES / script, bundle_tmp / script)
    (bundle_tmp / "config.json").write_text(json.dumps(config))
    digest = verification_runner.bundle_digest(bundle_tmp)
    entrypoint = entrypoint or ["python", f"/verifier/{script}"]
    status, body = http("POST", base + "/v1/versions", {
        "kind": "scorer", "name": f"scorer-{suffix}{name_suffix}", "version": "v1",
        "content": {"image": IMAGE, "entrypoint": entrypoint, "result_schema": SCHEMA},
        "assets": [{"name": "bundle", "digest": digest}],
    })
    assert status in (200, 201), body
    version_id = body["id"]
    shutil.move(str(bundle_tmp), str(root / "verifiers" / version_id))
    return version_id


IMAGE = "python@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254"


def scorer_ref(root: Path, scorer_id: str) -> dict:
    conn = sqlite3.connect(root / "aco.db")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT name, version FROM versions WHERE id = ?", (scorer_id,)).fetchone()
    finally:
        conn.close()
    return {"name": row["name"], "version": row["version"]}


def create_verification(base: str, root: Path, trial_id: str, scorer_id: str, key: str) -> dict:
    status, body = http("POST", base + f"/v1/trials/{trial_id}/verifications",
                        {"verifier": scorer_ref(root, scorer_id), "idempotency_key": key})
    assert status in (202, 200), body
    return body


# ponytail: fixed 90s flaked on a 2.4x-slow CI runner (issue #48); 240s default, override for local runs
VERIFY_TIMEOUT = float(os.environ.get("ACO_E2E_VERIFY_TIMEOUT", "240"))


def wait_for_verifications(base: str, trial_id: str, count: int, timeout: float = VERIFY_TIMEOUT) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, records = http("GET", base + f"/v1/trials/{trial_id}/verifications")
        assert status == 200, records
        if len(records) >= count and all(r["status"] in ("succeeded", "error") for r in records):
            return records
        time.sleep(0.5)
    raise AssertionError(f"verifications for {trial_id} did not finish within {timeout}s")


class TestIndependentVerification:
    def test_sealed_task_default_scorer_is_queued_and_reported(self, stack):
        """Experiment progress waits for the explicit initial scorer and the
        result API can consume its independent verdict."""
        base, root = stack["base"], stack["root"]
        scorer_id = register_scorer(base, root, "run.py", {"expected": EXPECTED})
        trial_id, _ = make_sealed(root, EXPECTED.encode())
        scorer = scorer_ref(root, scorer_id)
        conn = sqlite3.connect(root / "aco.db")
        conn.row_factory = sqlite3.Row
        try:
            task_id = conn.execute(
                "SELECT task_version_id FROM trials WHERE id = ?", (trial_id,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE versions SET content = ? WHERE id = ?",
                (json.dumps({"default_scorer": scorer}), task_id),
            )
            # The seed helper leaves the trial non-runnable; put it through
            # the same terminal funnel a real supervisor uses.
            conn.execute("UPDATE trials SET status = 'running' WHERE id = ?", (trial_id,))
            conn.commit()
            lifecycle.finish_trial(conn, trial_id, "submit", "sealed")
            experiment_id = conn.execute(
                "SELECT experiment_id FROM trials WHERE id = ?", (trial_id,)
            ).fetchone()[0]
        finally:
            conn.close()

        records = wait_for_verifications(base, trial_id, 1)
        assert len(records) == 1
        assert records[0]["verifier"] == scorer
        assert records[0]["status"] == "succeeded"
        assert records[0]["pass"] is True

        status, experiment = http("GET", base + f"/v1/experiments/{experiment_id}")
        assert status == 200
        assert experiment["progress"]["verification_required"] == 1
        assert experiment["progress"]["verification_terminal"] == 1
        assert experiment["progress"]["verification_pending"] == 0

        status, result = http(
            "GET", base + f'/v1/results?scorer={scorer["name"]}@{scorer["version"]}'
        )
        assert status == 200
        assert result["series"] and result["series"][0]["points"][0]["main_score"] == 1.0

    def test_correct_answer_scores_pass_with_isolation_evidence(self, stack):
        base, root = stack["base"], stack["root"]
        scorer_id = register_scorer(base, root, "run.py", {"expected": EXPECTED})
        trial_id, _ = make_sealed(root, EXPECTED.encode())
        status, body = http("POST", base + f"/v1/trials/{trial_id}/verifications", {
            "verifier": scorer_ref(root, scorer_id), "idempotency_key": "k1",
        })
        assert status == 202, body
        records = wait_for_verifications(base, trial_id, 1)
        assert len(records) == 1
        record = records[0]
        assert record["status"] == "succeeded", record
        assert record["pass"] is True
        assert record["error_kind"] is None and record["error_detail"] is None

        # runtime evidence from the actual container, not configuration claims
        evidence = record["evidence"]
        assert evidence["network_mode"] == "none"
        assert evidence["privileged"] is False
        assert evidence["read_only_rootfs"] is True
        assert evidence["docker_socket_mounted"] is False
        assert evidence["sensitive_env_keys"] == []
        mounts = {}
        for spec in evidence["mounts"]:
            _source, target, *mode = spec.split(":")
            mounts[target] = mode[0] if mode else ""
        assert mounts["/answer"] == "ro"
        assert mounts["/verifier"] == "ro"
        assert mounts["/output"] == ""  # only the output directory is writable

        # raw output kept as a diagnostic artifact
        assert (Path(record["raw_output_dir"]) / "result.json").is_file()

        # re-scoring never starts an agent: the seeded run row is untouched
        status, runs_list = http("GET", base + f"/v1/trials/{trial_id}/runs")
        assert len(runs_list) == 1 and runs_list[0]["status"] == "finished"

    def test_wrong_and_missing_answers_score_pass_false(self, stack):
        base, root = stack["base"], stack["root"]
        scorer_id = register_scorer(base, root, "run.py", {"expected": EXPECTED})
        wrong_trial, _ = make_sealed(root, b"something else")
        missing_trial, _ = make_sealed(root, None)  # no answer file in the workspace

        for trial_id, key in ((wrong_trial, "k1"), (missing_trial, "k2")):
            status, body = http("POST", base + f"/v1/trials/{trial_id}/verifications", {
                "verifier": scorer_ref(root, scorer_id), "idempotency_key": key,
            })
            assert status == 202, body
        records = wait_for_verifications(base, wrong_trial, 1) + wait_for_verifications(base, missing_trial, 1)
        assert all(r["status"] == "succeeded" for r in records)
        assert sorted(r["pass"] for r in records) == [False, False]

    def test_same_request_is_idempotent(self, stack):
        base, root = stack["base"], stack["root"]
        scorer_id = register_scorer(base, root, "run.py", {"expected": EXPECTED})
        trial_id, _ = make_sealed(root, EXPECTED.encode())
        first = create_verification(base, root, trial_id, scorer_id, "same-key")
        second = create_verification(base, root, trial_id, scorer_id, "same-key")
        assert first["id"] == second["id"]
        records = wait_for_verifications(base, trial_id, 1)
        assert len(records) == 1

    def test_new_verifier_version_regrade_appends(self, stack):
        base, root = stack["base"], stack["root"]
        v1 = register_scorer(base, root, "run.py", {"expected": EXPECTED})
        v2 = register_scorer(base, root, "run.py",
                             {"expected": EXPECTED, "submetrics": {"match": 1.0}})
        trial_id, _ = make_sealed(root, EXPECTED.encode())
        create_verification(base, root, trial_id, v1, "k1")
        create_verification(base, root, trial_id, v2, "k2")
        records = wait_for_verifications(base, trial_id, 2)
        versions = sorted(r["verifier"]["name"] for r in records)
        assert len(set(versions)) == 2  # two distinct scorer versions, both kept
        assert all(r["status"] == "succeeded" and r["pass"] for r in records)
        with_submetrics = [r for r in records if r["submetrics"]]
        assert len(with_submetrics) == 1 and with_submetrics[0]["submetrics"] == {"match": 1.0}
        assert all(r["stable"] for r in records)  # different versions: no conflict

    def test_probe_verifier_cannot_escape_or_tamper(self, stack):
        base, root = stack["base"], stack["root"]
        scorer_id = register_scorer(base, root, "probe.py", {"expected": EXPECTED})
        trial_id, digest = make_sealed(root, EXPECTED.encode())
        answer_before = (root / "answers" / digest / "workspace" / "answer.txt").read_bytes()

        create_verification(base, root, trial_id, scorer_id, "probe")
        records = wait_for_verifications(base, trial_id, 1)
        record = records[0]
        assert record["status"] == "succeeded", record

        result = json.loads((Path(record["raw_output_dir"]) / "result.json").read_text())
        probes = result["probes"]
        assert probes["network"].startswith("failed:")
        assert probes["answer_write"].startswith("failed:")
        assert probes["verifier_write"].startswith("failed:")
        assert probes["output_write"] == "wrote"  # the only writable mount

        # the sealed answer is byte-identical and the bundle unmodified
        assert (root / "answers" / digest / "workspace" / "answer.txt").read_bytes() == answer_before
        assert not (root / "verifiers" / scorer_id / "probe.txt").exists()

    def test_unverifiable_and_unknown_inputs_rejected(self, stack):
        base, root = stack["base"], stack["root"]
        scorer_id = register_scorer(base, root, "run.py", {"expected": EXPECTED})
        # no sealed answer registered for this trial
        conn = sqlite3.connect(root / "aco.db")
        suffix = uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO versions (id, kind, name, version, content, created_at) VALUES"
            f" ('v-t-{suffix}', 'task', 'task-{suffix}', 'v1', '{{}}', 'now'),"
            f" ('v-c-{suffix}', 'config', 'cfg-{suffix}', 'v1', '{{}}', 'now')"
        )
        conn.execute(f"INSERT INTO experiments (id, status, requested, created_at) VALUES ('e-{suffix}', 'planned', '{{}}', 'now')")
        conn.execute(
            "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
            f" repetition, plan_order, requested) VALUES ('t-{suffix}', 'e-{suffix}', 'v-t-{suffix}', 'v-c-{suffix}', 1, 1, '{{}}')"
        )
        conn.commit()
        conn.close()
        status, body = http("POST", base + f"/v1/trials/t-{suffix}/verifications", {
            "verifier": scorer_ref(root, scorer_id), "idempotency_key": "k1",
        })
        assert status == 422
        assert body["error"]["code"] == "not_verifiable"
