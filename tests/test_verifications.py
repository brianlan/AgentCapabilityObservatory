"""Unit tests for independent verification (#15).

Docker is faked at the subprocess boundary; the runner's parsing, error
classification, idempotency, and record keeping run against real files and a
real SQLite database. Real-container isolation behavior and the deterministic
fixture scoring live in tests/e2e/test_verification.py.
"""

import json
import sqlite3
import subprocess
import uuid

import pytest

from aco import artifacts, db, runs
from aco.app import version_digest as app_version_digest
from aco.models import AssetRef
from aco.verification import runner

SCHEMA = "aco.verification-result/v1"
IMAGE = "python@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254"


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "aco.db")
    db.migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def trial(conn):
    """A trial with a launch-intent run (run_id is referenced by sealed answers)."""
    conn.execute(
        "INSERT INTO versions (id, kind, name, version, content, created_at)"
        " VALUES ('v-task', 'task', 'task', 'v1', '{}', 'now'),"
        " ('v-cfg', 'config', 'cfg', 'v1', '{}', 'now')"
    )
    conn.execute("INSERT INTO experiments (id, status, requested, created_at) VALUES ('e1', 'planned', '{}', 'now')")
    conn.execute(
        "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
        " repetition, plan_order, requested) VALUES ('t1', 'e1', 'v-task', 'v-cfg', 1, 1, '{}')"
    )
    conn.commit()
    runs.create_run(conn, "t1", {"harness": "fake", "model": "none"}, supervisor_pid=1)
    return "t1"


def seal_directly(root, trial_id, content: bytes = b"EXPECTED") -> str:
    """Publish and register a sealed answer without running an agent."""
    conn = sqlite3.connect(root / "aco.db")
    conn.row_factory = sqlite3.Row
    run_id = conn.execute("SELECT run_id FROM trial_runs WHERE trial_id = ?", (trial_id,)).fetchone()["run_id"]
    staging = root / "sealing" / f"test-{uuid.uuid4().hex}" / "staging"
    (staging / "workspace").mkdir(parents=True)
    (staging / "workspace" / "answer.txt").write_bytes(content)
    manifest = artifacts.build_manifest(staging, trial_id, run_id, "submit", None)
    digest = artifacts.manifest_digest(manifest)
    (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    artifacts.publish(staging, digest, root / "answers")
    conn.execute(
        "INSERT INTO sealed_answers (trial_id, run_id, receipt_id, digest, manifest,"
        " seal_trigger, trigger_at, frozen_at, copied_at, published_at, registered_at, status)"
        " VALUES (?, ?, ?, ?, ?, 'submit', 'now', 'now', 'now', 'now', 'now', 'sealed')",
        (trial_id, run_id, f"receipt-{trial_id}", digest, json.dumps(manifest, sort_keys=True)),
    )
    conn.commit()
    conn.close()
    return digest


def make_scorer(conn, root, name="scorer", version="v1", entrypoint=("python", "/verifier/run.py")) -> str:
    """Register a scorer version and place its trusted bundle."""
    content = {"image": IMAGE, "entrypoint": list(entrypoint), "result_schema": SCHEMA}
    bundle_tmp = root / "bundle-prep" / f"{name}-{version}"
    bundle_tmp.mkdir(parents=True)
    (bundle_tmp / "run.py").write_text("# verifier bundle\n")
    digest = runner.bundle_digest(bundle_tmp)
    assets = [{"name": "bundle", "digest": digest}]
    version_id = app_version_digest("scorer", name, version, content,
                                    [AssetRef(**a) for a in assets])
    conn.execute(
        "INSERT INTO versions (id, kind, name, version, content, assets, created_at)"
        " VALUES (?, 'scorer', ?, ?, ?, ?, 'now')",
        (version_id, name, version, json.dumps(content), json.dumps(assets)),
    )
    conn.commit()
    bundle_dir = root / "verifiers" / version_id
    bundle_dir.parent.mkdir(parents=True, exist_ok=True)
    bundle_tmp.rename(bundle_dir)
    return version_id


def queue_verification(conn, trial_id, scorer_id, key="k1") -> str:
    vid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO verifications (id, trial_id, idempotency_key, request_digest,"
        " scorer_version_id, status, created_at) VALUES (?, ?, ?, 'd', ?, 'queued', 'now')",
        (vid, trial_id, key, scorer_id),
    )
    conn.commit()
    return vid


class FakeDocker:
    """Stands in for the docker CLI; writes the configured verifier result
    into the container's /output mount and records every command."""

    def __init__(self, verdict="valid", exit_code=0, fail=False, timeout=False, pass_value=True):
        self.verdict = verdict
        self.exit_code = exit_code
        self.fail = fail
        self.timeout = timeout
        self.pass_value = pass_value
        self.commands = []

    def __call__(self, cmd, **kwargs):
        self.commands.append(list(cmd))
        if cmd[:2] == ["docker", "run"]:
            if self.fail:
                raise FileNotFoundError("docker daemon unreachable")
            if self.timeout:
                raise subprocess.TimeoutExpired(cmd, 60)
            return self._run(cmd)
        if cmd[:2] == ["docker", "inspect"]:
            output = json.dumps([{
                "HostConfig": {"NetworkMode": "none", "Privileged": False,
                               "ReadonlyRootfs": True,
                               "Binds": ["src:/answer:ro", "src:/verifier:ro", "src:/output"]},
                "Mounts": [],
                "Config": {"Env": ["PATH=/usr/local/bin:/usr/bin:/bin"]},
            }])
            return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    @staticmethod
    def mounts(cmd):
        result = {}
        for i, arg in enumerate(cmd):
            if arg == "-v":
                source, target, *mode = cmd[i + 1].split(":")
                result[target] = (source, mode[0] if mode else "rw")
        return result

    def _run(self, cmd):
        output_dir = self.mounts(cmd)["/output"][0]
        with open(cmd[cmd.index("--cidfile") + 1], "w") as handle:
            handle.write("fake-container-id")
        payloads = {
            "valid": {"schema": SCHEMA, "pass": self.pass_value, "submetrics": {"match": 1.0}},
            "wrong-schema": {"schema": "other/v9", "pass": True},
            "no-pass-field": {"schema": SCHEMA},
        }
        if self.verdict in payloads:
            with open(f"{output_dir}/result.json", "w") as handle:
                handle.write(json.dumps(payloads[self.verdict]))
        elif self.verdict == "malformed":
            with open(f"{output_dir}/result.json", "w") as handle:
                handle.write("not json{")
        # "missing" writes nothing
        return subprocess.CompletedProcess(cmd, self.exit_code, stdout="",
                                           stderr="boom" if self.exit_code else "")


@pytest.fixture()
def fake_docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(runner.subprocess, "run", fake)
    return fake


def run_one(conn, root, scorer_id, trial_id="t1", key="k1") -> dict:
    vid = queue_verification(conn, trial_id, scorer_id, key)
    row = runner.claim_next_queued(conn)
    assert row["id"] == vid and row["status"] == "running"
    runner.execute_verification(conn, row, root)
    return dict(conn.execute("SELECT * FROM verifications WHERE id = ?", (vid,)).fetchone())


class TestDeterministicScoring:
    def test_correct_and_wrong_answers_are_valid_verdicts(self, conn, trial, tmp_path, monkeypatch):
        """pass=1 and pass=0 are both *succeeded* records — a failing verdict
        is a valid score, never a scoring error."""
        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial, b"EXPECTED")
        monkeypatch.setattr(runner.subprocess, "run", FakeDocker(pass_value=True))
        correct = run_one(conn, tmp_path, scorer_id, key="correct")

        conn.execute(
            "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
            " repetition, plan_order, requested) VALUES ('t2', 'e1', 'v-task', 'v-cfg', 2, 2, '{}')"
        )
        conn.commit()
        runs.create_run(conn, "t2", {}, supervisor_pid=1)
        seal_directly(tmp_path, "t2", b"WRONG")
        monkeypatch.setattr(runner.subprocess, "run", FakeDocker(pass_value=False))
        wrong = run_one(conn, tmp_path, scorer_id, trial_id="t2", key="wrong")

        assert correct["status"] == "succeeded" and correct["pass"] == 1
        assert wrong["status"] == "succeeded" and wrong["pass"] == 0
        assert correct["submetrics"] == json.dumps({"match": 1.0})

    def test_answer_time_and_scoring_time_are_separate(self, conn, trial, tmp_path, fake_docker):
        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial)
        record = run_one(conn, tmp_path, scorer_id)
        assert record["started_at"] and record["finished_at"]
        assert record["finished_at"] >= record["started_at"]
        sealed_at = conn.execute(
            "SELECT registered_at FROM sealed_answers WHERE trial_id = 't1'"
        ).fetchone()["registered_at"]
        assert record["started_at"] != sealed_at


class TestErrorClassification:
    def test_verifier_crash_is_error_not_pass_false(self, conn, trial, tmp_path, monkeypatch):
        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial)
        monkeypatch.setattr(runner.subprocess, "run", FakeDocker(verdict="missing", exit_code=3))
        record = run_one(conn, tmp_path, scorer_id)
        assert record["status"] == "error"
        assert record["error_kind"] == "verifier_error"
        assert record["pass"] is None and record["submetrics"] is None
        assert "boom" in record["error_detail"]

    def test_verifier_timeout_is_error(self, conn, trial, tmp_path, monkeypatch):
        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial)
        monkeypatch.setattr(runner.subprocess, "run", FakeDocker(timeout=True))
        record = run_one(conn, tmp_path, scorer_id)
        assert record["status"] == "error" and record["error_kind"] == "verifier_error"
        assert "timed out" in record["error_detail"]
        assert record["pass"] is None

    @pytest.mark.parametrize("verdict,detail", [
        ("malformed", "unreadable"),
        ("missing", "unreadable"),
        ("wrong-schema", "schema"),
        ("no-pass-field", "pass"),
    ])
    def test_invalid_output_is_error_not_pass_false(self, conn, trial, tmp_path, monkeypatch, verdict, detail):
        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial)
        monkeypatch.setattr(runner.subprocess, "run", FakeDocker(verdict=verdict))
        record = run_one(conn, tmp_path, scorer_id)
        assert record["status"] == "error"
        assert record["error_kind"] == "invalid_output"
        assert record["pass"] is None
        assert detail in record["error_detail"]

    def test_docker_unreachable_is_infra_error(self, conn, trial, tmp_path, monkeypatch):
        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial)
        monkeypatch.setattr(runner.subprocess, "run", FakeDocker(fail=True))
        record = run_one(conn, tmp_path, scorer_id)
        assert record["status"] == "error" and record["error_kind"] == "infra_error"

    def test_missing_bundle_is_infra_error(self, conn, trial, tmp_path):
        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial)
        for path in (tmp_path / "verifiers" / scorer_id).iterdir():
            path.unlink()
        record = run_one(conn, tmp_path, scorer_id)
        assert record["status"] == "error" and record["error_kind"] == "infra_error"
        assert "bundle" in record["error_detail"]

    def test_undeclared_bundle_digest_is_infra_error(self, conn, trial, tmp_path):
        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial)
        conn.execute("UPDATE versions SET assets = '[]' WHERE id = ?", (scorer_id,))
        conn.commit()
        record = run_one(conn, tmp_path, scorer_id)
        assert record["status"] == "error" and record["error_kind"] == "infra_error"
        assert "bundle" in record["error_detail"]

    def test_bundle_digest_mismatch_is_infra_error(self, conn, trial, tmp_path):
        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial)
        # the trusted bundle changed after registration (tamper/placement error)
        (tmp_path / "verifiers" / scorer_id / "run.py").write_text("# tampered\n")
        record = run_one(conn, tmp_path, scorer_id)
        assert record["status"] == "error" and record["error_kind"] == "infra_error"
        assert "digest mismatch" in record["error_detail"]


class TestAuthoritativeParsing:
    def test_planted_result_file_cannot_be_parsed(self, conn, trial, tmp_path, monkeypatch):
        """A submission-planted result in the output location never reaches
        the authoritative parse: every execution wipes its output dir first."""
        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial)
        vid = queue_verification(conn, trial, scorer_id)
        planted = tmp_path / "verifications" / vid / "attempt-1" / "output"
        planted.mkdir(parents=True)
        (planted / "result.json").write_text(json.dumps({"schema": SCHEMA, "pass": True}))
        monkeypatch.setattr(runner.subprocess, "run", FakeDocker(verdict="missing"))
        row = runner.claim_next_queued(conn)
        runner.execute_verification(conn, row, tmp_path)
        record = dict(conn.execute("SELECT * FROM verifications WHERE id = ?", (vid,)).fetchone())
        assert record["status"] == "error" and record["error_kind"] == "invalid_output"

    def test_answer_mount_resolved_from_registration_only(self, conn, trial, tmp_path, fake_docker):
        """The container's answer mount is the registered content-addressed
        location — a request can never point scoring at an arbitrary path."""
        scorer_id = make_scorer(conn, tmp_path)
        digest = seal_directly(tmp_path, trial)
        run_one(conn, tmp_path, scorer_id)
        mounts = FakeDocker.mounts(fake_docker.commands[0])
        assert mounts["/answer"][0].endswith(f"answers/{digest}")


class TestContainerCommand:
    def test_isolation_arguments(self, conn, trial, tmp_path, fake_docker):
        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial)
        run_one(conn, tmp_path, scorer_id)
        cmd = fake_docker.commands[0]
        assert cmd[cmd.index("--network") + 1] == "none"
        assert "--read-only" in cmd
        assert "--tmpfs" in cmd
        mounts = FakeDocker.mounts(cmd)
        assert mounts["/answer"][1] == "ro"
        assert mounts["/verifier"][1] == "ro"
        assert mounts["/output"][1] == "rw"
        # no credentials are ever passed into the verifier container
        assert "-e" not in cmd and "--env" not in cmd
        assert cmd[-3].count("@sha256:") == 1  # digest-pinned image
        assert cmd[-2:] == ["python", "/verifier/run.py"]

    def test_evidence_recorded_from_inspect(self, conn, trial, tmp_path, fake_docker):
        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial)
        record = run_one(conn, tmp_path, scorer_id)
        evidence = json.loads(record["evidence"])
        assert evidence["network_mode"] == "none"
        assert evidence["privileged"] is False
        assert evidence["read_only_rootfs"] is True
        assert evidence["docker_socket_mounted"] is False
        assert evidence["sensitive_env_keys"] == []

    def test_raw_output_and_log_kept_as_diagnostics(self, conn, trial, tmp_path, fake_docker):
        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial)
        record = run_one(conn, tmp_path, scorer_id)
        assert (tmp_path / "verifications" / record["id"] / "attempt-1" / "output" / "result.json").is_file()
        assert (tmp_path / "verifications" / record["id"] / "attempt-1" / "docker-run.log").is_file()


class TestBundleDigest:
    def test_stable_and_content_sensitive(self, tmp_path):
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "run.py").write_text("x = 1\n")
        first = runner.bundle_digest(bundle)
        assert first == runner.bundle_digest(bundle)
        (bundle / "run.py").write_text("x = 2\n")
        assert runner.bundle_digest(bundle) != first


class TestVerificationAPI:
    """POST/GET contract via the FastAPI app (no manager, no docker)."""

    def register_scorer(self, client, name="scorer", version="v1", image=IMAGE):
        resp = client.post("/v1/versions", json={
            "kind": "scorer", "name": name, "version": version,
            "content": {"image": image, "entrypoint": ["python", "/verifier/run.py"],
                        "result_schema": SCHEMA},
            "assets": [{"name": "bundle", "digest": "0" * 64}],
        })
        assert resp.status_code in (200, 201), resp.text
        return resp.json()

    def post_verification(self, client, trial_id, name="scorer", version="v1", key="k1"):
        return client.post(f"/v1/trials/{trial_id}/verifications", json={
            "verifier": {"name": name, "version": version},
            "idempotency_key": key,
        })

    def test_unknown_trial_404(self, client):
        resp = self.post_verification(client, "nope")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_unregistered_scorer_422(self, client, tmp_path):
        self._make_trial(tmp_path, "t1")
        seal_directly(tmp_path, "t1")
        resp = self.post_verification(client, "t1")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "version_not_found"

    def test_trial_without_sealed_answer_not_verifiable(self, client, tmp_path):
        self.register_scorer(client)
        self._make_trial(tmp_path, "t1")
        resp = self.post_verification(client, "t1")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "not_verifiable"

    def test_anomalous_sealed_answer_not_verifiable(self, client, tmp_path):
        self.register_scorer(client)
        self._make_trial(tmp_path, "t1")
        conn = sqlite3.connect(tmp_path / "aco.db")
        run_id = conn.execute("SELECT run_id FROM trial_runs WHERE trial_id = 't1'").fetchone()[0]
        conn.execute(
            "INSERT INTO sealed_answers (trial_id, run_id, receipt_id, digest, manifest,"
            " seal_trigger, trigger_at, frozen_at, copied_at, published_at, registered_at,"
            " status, anomaly) VALUES ('t1', ?, 'r-x', '', '{}', 'exit', 'now', 'now', 'now',"
            " 'now', 'now', 'anomaly', 'incomplete staging copy')",
            (run_id,),
        )
        conn.commit()
        conn.close()
        resp = self.post_verification(client, "t1")
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "not_verifiable"

    @pytest.mark.parametrize("image", [
        "python:3.12",                       # mutable tag, no digest
        "python@sha256:",                    # empty digest
        "python@sha256:abc123",              # short digest
        "python@sha256:" + "g" * 64,         # non-hex digest
        "python@sha256:" + "A" * 64,         # uppercase hex digest
        "python:3.12@sha256:" + "a" * 64,    # tag-plus-digest
        "@sha256:" + "a" * 64,               # empty repository
    ])
    def test_image_must_be_digest_pinned_at_registration(self, client, image):
        resp = client.post("/v1/versions", json={
            "kind": "scorer", "name": "scorer", "version": "v1",
            "content": {"image": image, "entrypoint": ["python", "/verifier/run.py"],
                        "result_schema": SCHEMA},
        })
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_content"
        assert "digest-pinned" in resp.json()["error"]["message"]

    def test_valid_digest_pinned_image_accepted(self, client):
        resp = client.post("/v1/versions", json={
            "kind": "scorer", "name": "scorer", "version": "v1",
            "content": {"image": "python@sha256:" + "a" * 64,
                        "entrypoint": ["python", "/verifier/run.py"],
                        "result_schema": SCHEMA},
        })
        assert resp.status_code == 201, resp.text

    def test_idempotent_same_request(self, client, tmp_path):
        self.register_scorer(client)
        self._make_trial(tmp_path, "t1")
        seal_directly(tmp_path, "t1")
        first = self.post_verification(client, "t1")
        assert first.status_code == 202, first.text
        second = self.post_verification(client, "t1")
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]
        # still exactly one queued record
        conn = sqlite3.connect(tmp_path / "aco.db")
        count = conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0]
        conn.close()
        assert count == 1

    def test_same_key_different_payload_409(self, client, tmp_path):
        self.register_scorer(client)
        self.register_scorer(client, name="scorer", version="v2")
        self._make_trial(tmp_path, "t1")
        seal_directly(tmp_path, "t1")
        assert self.post_verification(client, "t1", version="v1").status_code == 202
        conflict = self.post_verification(client, "t1", version="v2")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"

    def test_new_key_same_version_creates_independent_regrade(self, client, tmp_path):
        self.register_scorer(client)
        self._make_trial(tmp_path, "t1")
        seal_directly(tmp_path, "t1")
        a = self.post_verification(client, "t1", key="k1")
        b = self.post_verification(client, "t1", key="k2")
        assert a.status_code == 202 and b.status_code == 202
        assert a.json()["id"] != b.json()["id"]
        listed = client.get("/v1/trials/t1/verifications").json()
        assert len(listed) == 2
        assert all(r["status"] == "queued" and r["stable"] for r in listed)

    @staticmethod
    def _finish(tmp_path, verification_id, *, status, pass_value=None):
        """Mirror the runner's terminal write for a queued job."""
        conn = sqlite3.connect(tmp_path / "aco.db")
        conn.execute(
            "UPDATE verifications SET status = ?, pass = ?, finished_at = 'now' WHERE id = ?",
            (status, pass_value, verification_id))
        conn.commit()
        conn.close()

    def test_conflicting_verdicts_reported_by_every_read_path(self, client, tmp_path):
        """Reopened #15 (third): after a same-version pass/fail, the POST
        create response, the idempotent replay response, and GET list all
        report stable=false — no read path may disagree with the
        authoritative stability judgment for the trial."""
        self.register_scorer(client)
        self._make_trial(tmp_path, "t1")
        seal_directly(tmp_path, "t1")
        first = self.post_verification(client, "t1", key="a")
        assert first.status_code == 202, first.text
        assert first.json()["stable"] is True  # no verdict yet: nothing to conflict
        self._finish(tmp_path, first.json()["id"], status="succeeded", pass_value=1)
        second = self.post_verification(client, "t1", key="b")
        self._finish(tmp_path, second.json()["id"], status="succeeded", pass_value=0)

        # replaying either idempotency key returns the single-verification
        # response: it must carry the same stable=false the list reports
        for key in ("a", "b"):
            replay = self.post_verification(client, "t1", key=key)
            assert replay.status_code == 200, replay.text
            assert replay.json()["stable"] is False

        listed = {r["id"]: r for r in client.get("/v1/trials/t1/verifications").json()}
        assert listed[first.json()["id"]]["stable"] is False
        assert listed[second.json()["id"]]["stable"] is False

    def test_agreeing_verdicts_and_error_rows_stay_stable(self, client, tmp_path):
        """Reopened #15 (third): same-version pass/pass is stable, and a
        scoring-error execution never counts as a contradictory verdict."""
        self.register_scorer(client)
        self._make_trial(tmp_path, "t1")
        seal_directly(tmp_path, "t1")
        first = self.post_verification(client, "t1", key="a")
        self._finish(tmp_path, first.json()["id"], status="succeeded", pass_value=1)
        second = self.post_verification(client, "t1", key="b")
        self._finish(tmp_path, second.json()["id"], status="succeeded", pass_value=1)
        errored = self.post_verification(client, "t1", key="c")
        self._finish(tmp_path, errored.json()["id"], status="error", pass_value=None)

        replay = self.post_verification(client, "t1", key="a")
        assert replay.status_code == 200, replay.text
        assert replay.json()["stable"] is True
        listed = client.get("/v1/trials/t1/verifications").json()
        assert all(r["stable"] for r in listed)

    @staticmethod
    def _make_trial(tmp_path, trial_id):
        conn = sqlite3.connect(tmp_path / "aco.db")
        conn.execute(
            "INSERT INTO versions (id, kind, name, version, content, created_at)"
            " VALUES ('v-task', 'task', 'task', 'v1', '{}', 'now'),"
            " ('v-cfg', 'config', 'cfg', 'v1', '{}', 'now')"
        )
        conn.execute("INSERT INTO experiments (id, status, requested, created_at) VALUES ('e1', 'planned', '{}', 'now')")
        conn.execute(
            "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
            " repetition, plan_order, requested) VALUES (?, 'e1', 'v-task', 'v-cfg', 1, 1, '{}')",
            (trial_id,),
        )
        conn.execute(
            "INSERT INTO trial_runs (run_id, trial_id, status, requested_profile, launched_at,"
            " supervisor_pid, created_at) VALUES (?, ?, 'launching', '{}', 'now', 1, 'now')",
            (f"run-{trial_id}", trial_id),
        )
        conn.commit()
        conn.close()


class TestRestartRecovery:
    """Reopened #15: every actual container start appends its own attempt
    record; manager-restart recovery preserves earlier attempts and the
    re-queued execution appends a new one."""

    @staticmethod
    def attempts(conn, vid):
        return conn.execute(
            "SELECT * FROM verification_attempts WHERE verification_id = ?"
            " ORDER BY attempt_no", (vid,)).fetchall()

    def test_recovery_preserves_first_attempt_and_appends_second(self, conn, trial, tmp_path, monkeypatch):
        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial)
        vid = queue_verification(conn, trial, scorer_id)

        first = runner.claim_next_queued(conn)  # container started, then the manager crashed
        first_started = first["started_at"]
        runner.requeue_stuck_running(conn)  # startup recovery after the crash

        (recorded,) = self.attempts(conn, vid)
        assert recorded["attempt_no"] == 1
        assert recorded["started_at"] == first_started  # first attempt's time preserved
        assert recorded["status"] == "error" and recorded["error_kind"] == "infra_error"
        assert "interrupted" in recorded["error_detail"]

        monkeypatch.setattr(runner.subprocess, "run", FakeDocker(pass_value=True))
        assert runner.run_pending(conn, tmp_path) is True  # the re-queued execution

        first_attempt, second_attempt = self.attempts(conn, vid)
        assert second_attempt["attempt_no"] == 2
        assert second_attempt["status"] == "succeeded" and second_attempt["pass"] == 1
        assert second_attempt["started_at"] != first_started
        assert first_attempt["started_at"] == first_started  # still untouched
        assert "interrupted" in first_attempt["error_detail"]
        # the verifications row mirrors the current (second) attempt state
        row = dict(conn.execute("SELECT * FROM verifications WHERE id = ?", (vid,)).fetchone())
        assert row["status"] == "succeeded" and row["pass"] == 1
        assert row["started_at"] == second_attempt["started_at"]

    def test_each_attempt_keeps_its_own_diagnostics(self, conn, trial, tmp_path, fake_docker):
        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial)
        vid = queue_verification(conn, trial, scorer_id)
        row = runner.claim_next_queued(conn)
        runner.execute_verification(conn, row, tmp_path)
        # simulate the post-crash requeue, then a second execution
        conn.execute("UPDATE verifications SET status = 'queued', started_at = NULL WHERE id = ?", (vid,))
        conn.commit()
        assert runner.run_pending(conn, tmp_path) is True

        base = tmp_path / "verifications" / vid
        assert (base / "attempt-1" / "output" / "result.json").is_file()
        assert (base / "attempt-1" / "docker-run.log").is_file()
        assert (base / "attempt-2" / "output" / "result.json").is_file()
        first = json.loads((base / "attempt-1" / "output" / "result.json").read_text())
        assert first["pass"] is True  # attempt 2's fresh dir never touched attempt 1

    def test_api_lists_attempt_history(self, conn, trial, tmp_path, fake_docker):
        from aco.api.verifications import list_verifications

        scorer_id = make_scorer(conn, tmp_path)
        seal_directly(tmp_path, trial)
        vid = queue_verification(conn, trial, scorer_id)
        row = runner.claim_next_queued(conn)
        runner.execute_verification(conn, row, tmp_path)
        records = {r["id"]: r for r in list_verifications(conn, trial)}
        (attempt,) = records[vid]["attempts"]
        assert attempt["attempt_no"] == 1
        assert attempt["status"] == "succeeded" and attempt["pass"] is True


class TestQuerySemantics:
    @staticmethod
    def _succeeded(conn, trial_id, scorer_id, key, pass_value, submetrics=None):
        vid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO verifications (id, trial_id, idempotency_key, request_digest,"
            " scorer_version_id, status, pass, submetrics, created_at)"
            " VALUES (?, ?, ?, 'd', ?, 'succeeded', ?, ?, 'now')",
            (vid, trial_id, key, scorer_id, pass_value,
             json.dumps(submetrics) if submetrics else None),
        )
        conn.commit()
        return vid

    def test_same_version_conflict_flagged_not_maxed(self, conn, trial, tmp_path):
        from aco.api.verifications import list_verifications

        scorer_id = make_scorer(conn, tmp_path)
        v1 = self._succeeded(conn, "t1", scorer_id, "a", 1)
        v2 = self._succeeded(conn, "t1", scorer_id, "b", 0)
        records = list_verifications(conn, "t1")
        by_id = {r["id"]: r for r in records}
        assert len(records) == 2  # nothing dropped, nothing selected
        assert by_id[v1]["stable"] is False and by_id[v2]["stable"] is False
        assert by_id[v1]["pass"] is True and by_id[v2]["pass"] is False

    def test_results_bucket_matches_api_stable_flag(self, conn, trial, tmp_path):
        """Reopened #15 (third): the results coverage bucket and the API
        stable field are two views of the same stability judgment — a
        contradictory same-version pair is unstable in both."""
        from aco.api.verifications import list_verifications
        from aco.results import collect

        scorer_id = make_scorer(conn, tmp_path)
        self._succeeded(conn, "t1", scorer_id, "a", 1)
        self._succeeded(conn, "t1", scorer_id, "b", 0)
        conn.execute("UPDATE experiments SET requested = ? WHERE id = 'e1'",
                     (json.dumps({"task": {"name": "task", "version": "v1"}}),))
        conn.execute("UPDATE trials SET fingerprint = 'fp' WHERE id = 't1'")
        conn.commit()

        point = collect(conn)["series"][0]["points"][0]
        assert point["counts"]["unstable"] == 1
        assert all(r["stable"] is False for r in list_verifications(conn, "t1"))

    def test_new_version_regrade_keeps_old_result_readable(self, conn, trial, tmp_path):
        from aco.api.verifications import list_verifications

        v1_id = make_scorer(conn, tmp_path, name="scorer", version="v1")
        v2_id = make_scorer(conn, tmp_path, name="scorer", version="v2")
        old = self._succeeded(conn, "t1", v1_id, "a", 1, {"match": 1.0})
        new = self._succeeded(conn, "t1", v2_id, "b", 0, {"match": 0.0})
        by_id = {r["id"]: r for r in list_verifications(conn, "t1")}
        assert by_id[old]["verifier"] == {"name": "scorer", "version": "v1"}
        assert by_id[old]["pass"] is True and by_id[old]["stable"] is True
        assert by_id[new]["verifier"] == {"name": "scorer", "version": "v2"}
        assert by_id[new]["pass"] is False and by_id[new]["submetrics"] == {"match": 0.0}
