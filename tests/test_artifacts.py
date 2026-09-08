"""Unit tests for sealed answers: contract, manifest, publish, seal, recovery (#14).

Docker interactions are monkeypatched; the freeze/publish/register pipeline
and all recovery states run against real files and a real SQLite database.
End-to-end freeze behavior (real container pause/copy) lives in tests/e2e.
"""

import hashlib
import json
import os
import shutil
import stat

import pytest

from aco import artifacts, db, runs


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "aco.db")
    db.migrate(conn)
    yield conn
    conn.close()


@pytest.fixture()
def trial(conn, tmp_path):
    """A trial with a launch-intent run and an accepted submission."""
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
    run_id = runs.create_run(conn, "t1", {"harness": "fake", "model": "none"}, supervisor_pid=1)
    conn.execute(
        "INSERT INTO submissions (trial_id, idempotency_key, request_digest, receipt_id,"
        " status, answer, created_at) VALUES ('t1', 'k1', 'd1', 'r-123', 'accepted', '{}', 'now')"
    )
    conn.commit()
    return runs.get_run(conn, run_id)


@pytest.fixture()
def fake_container(tmp_path, monkeypatch):
    """A fake container workspace backing the monkeypatched docker calls."""
    workspace = tmp_path / "container-workspace"
    workspace.mkdir()
    calls = {"pause": 0, "copy": 0}

    def fake_pause(container_id):
        calls["pause"] += 1

    def fake_unpause(container_id):
        pass

    def fake_copy(container_id, container_path, dest):
        # mirrors `docker cp cid:/workspace <target>`: non-existent target
        # becomes the copy of the container path
        calls["copy"] += 1
        shutil.copytree(workspace, dest, symlinks=True)

    monkeypatch.setattr(artifacts, "pause_container", fake_pause)
    monkeypatch.setattr(artifacts, "unpause_container", fake_unpause)
    monkeypatch.setattr(artifacts, "copy_from_container", fake_copy)
    return workspace, calls


def seal_trial(conn, trial, tmp_path, trigger="submit", **kwargs):
    return artifacts.seal(conn, trial, "cid-1", trigger, tmp_path, **kwargs)


class TestArtifactContract:
    def test_valid_contract_passes(self):
        artifacts.ArtifactContract().validate()

    def test_relative_path_rejected(self):
        with pytest.raises(artifacts.SealError, match="absolute and normalized"):
            artifacts.ArtifactContract(allowed_paths=("workspace",)).validate()

    def test_required_output_outside_allowed_paths_rejected(self):
        contract = artifacts.ArtifactContract(allowed_paths=("/workspace",), required_outputs=("/etc/passwd",))
        with pytest.raises(artifacts.SealError, match="outside allowed paths"):
            contract.validate()

    def test_required_output_inside_allowed_paths_accepted(self):
        artifacts.ArtifactContract(required_outputs=("/workspace/answer.txt",)).validate()


class TestManifest:
    def test_entries_and_binary_detection(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "answer.txt").write_bytes(b"hello")
        (staging / "blob.bin").write_bytes(b"\x00\x01binary-ish")
        manifest = artifacts.build_manifest(staging, "t1", "r1", "submit", None)
        by_path = {e["path"]: e for e in manifest["files"]}
        assert by_path["answer.txt"]["sha256"] == hashlib.sha256(b"hello").hexdigest()
        assert by_path["answer.txt"]["type"] == "regular"
        assert by_path["blob.bin"]["type"] == "binary"
        assert manifest["total_bytes"] == len(b"hello") + len(b"\x00\x01binary-ish")

    def test_changes_added_modified_deleted(self, tmp_path):
        baseline = tmp_path / "baseline"
        final = tmp_path / "final"
        baseline.mkdir()
        final.mkdir()
        (baseline / "kept.txt").write_text("same")
        (baseline / "modified.txt").write_text("old")
        (baseline / "gone.txt").write_text("bye")
        (final / "kept.txt").write_text("same")
        (final / "modified.txt").write_text("new")
        (final / "added.txt").write_text("hi")
        manifest = artifacts.build_manifest(final, "t1", "r1", "submit", baseline)
        assert manifest["changes"] == {"added": ["added.txt"], "modified": ["modified.txt"],
                                       "deleted": ["gone.txt"]}

    def test_digest_is_content_addressed(self, tmp_path):
        one = tmp_path / "one"
        two = tmp_path / "two"
        one.mkdir()
        two.mkdir()
        (one / "a.txt").write_text("x")
        (two / "a.txt").write_text("x")
        first = artifacts.build_manifest(one, "t1", "r1", "submit", None)
        second = artifacts.build_manifest(two, "t1", "r1", "submit", None)
        assert artifacts.manifest_digest(first) == artifacts.manifest_digest(second)
        (two / "a.txt").write_text("changed")
        third = artifacts.build_manifest(two, "t1", "r1", "submit", None)
        assert artifacts.manifest_digest(first) != artifacts.manifest_digest(third)

    def test_manifest_rebuilds_the_snapshot(self, tmp_path):
        """Every file in the snapshot is verifiable against the manifest."""
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "sub").mkdir()
        (staging / "sub" / "a.bin").write_bytes(os.urandom(64))
        (staging / "b.txt").write_text("text")
        manifest = artifacts.build_manifest(staging, "t1", "r1", "submit", None)
        for entry in manifest["files"]:
            data = (staging / entry["path"]).read_bytes()
            assert len(data) == entry["bytes"]
            assert hashlib.sha256(data).hexdigest() == entry["sha256"]


class TestValidateSnapshot:
    def test_symlink_rejected(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "link").symlink_to("/etc/passwd")
        with pytest.raises(artifacts.SealError, match="symlink"):
            artifacts.validate_snapshot(staging, artifacts.ArtifactContract())

    def test_fifo_rejected(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        os.mkfifo(staging / "pipe")
        with pytest.raises(artifacts.SealError, match="special file"):
            artifacts.validate_snapshot(staging, artifacts.ArtifactContract())

    def test_size_limit_enforced(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "big.bin").write_bytes(b"x" * 100)
        contract = artifacts.ArtifactContract(max_total_bytes=10)
        with pytest.raises(artifacts.SealError, match="exceeds contract limit"):
            artifacts.validate_snapshot(staging, contract)

    def test_missing_required_output_rejected(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        contract = artifacts.ArtifactContract(required_outputs=("/workspace/answer.txt",))
        with pytest.raises(artifacts.SealError, match="required output missing"):
            artifacts.validate_snapshot(staging, contract)


class TestPublish:
    def test_publishes_read_only_content_addressed(self, tmp_path):
        staging = tmp_path / "sealing" / "r1" / "staging"
        staging.mkdir(parents=True)
        (staging / "answer.txt").write_text("sealed")
        digest = "d" * 64
        target = artifacts.publish(staging, digest, tmp_path / "answers")
        assert target == tmp_path / "answers" / digest
        assert (target / "answer.txt").read_text() == "sealed"
        assert not staging.exists()
        assert not os.access(target / "answer.txt", os.W_OK)
        assert stat.S_IMODE(target.stat().st_mode) & 0o222 == 0

    def test_republish_keeps_original_and_discards_staging(self, tmp_path):
        answers = tmp_path / "answers"
        first = tmp_path / "s1"
        first.mkdir()
        (first / "a.txt").write_text("original")
        artifacts.publish(first, "d" * 64, answers)
        second = tmp_path / "s2"
        second.mkdir()
        (second / "a.txt").write_text("re-collected-different")
        target = artifacts.publish(second, "d" * 64, answers)
        assert (target / "a.txt").read_text() == "original"
        assert not second.exists()


class TestSeal:
    def test_submit_seal_publishes_and_registers(self, conn, trial, tmp_path, fake_container):
        workspace, calls = fake_container
        (workspace / "answer.txt").write_text("the answer")
        receipt = seal_trial(conn, trial, tmp_path, trigger="submit")

        submission = conn.execute("SELECT status FROM submissions WHERE trial_id='t1'").fetchone()
        assert submission["status"] == "sealed"
        sealed = conn.execute("SELECT * FROM sealed_answers WHERE trial_id='t1'").fetchone()
        assert sealed["receipt_id"] == "r-123"  # the submission's single receipt
        assert sealed["seal_trigger"] == "submit"
        assert sealed["status"] == "sealed"
        manifest = json.loads(sealed["manifest"])
        assert [e["path"] for e in manifest["files"]] == ["workspace/answer.txt"]
        assert sealed["digest"] == artifacts.manifest_digest(manifest)
        assert (tmp_path / "answers" / sealed["digest"] / "workspace" / "answer.txt").read_text() == "the answer"
        assert calls["pause"] == 1 and calls["copy"] == 1
        assert receipt["receipt_id"] == "r-123"

    def test_repeated_seal_returns_same_receipt_without_recopy(self, conn, trial, tmp_path, fake_container):
        workspace, calls = fake_container
        (workspace / "answer.txt").write_text("the answer")
        first = seal_trial(conn, trial, tmp_path)
        (workspace / "answer.txt").write_text("changed after seal")
        second = seal_trial(conn, trial, tmp_path)
        assert first["digest"] == second["digest"]
        assert first["receipt_id"] == second["receipt_id"]
        assert calls["copy"] == 1  # never re-collected
        sealed = conn.execute("SELECT manifest FROM sealed_answers").fetchone()
        assert json.loads(sealed["manifest"])["files"][0]["sha256"] == \
            hashlib.sha256(b"the answer").hexdigest()

    def test_freeze_failure_marks_submission_error(self, conn, trial, tmp_path, monkeypatch):
        def failing_pause(container_id):
            raise RuntimeError("no such container")

        monkeypatch.setattr(artifacts, "pause_container", failing_pause)
        with pytest.raises(artifacts.SealError, match="freeze failed"):
            seal_trial(conn, trial, tmp_path)
        status = conn.execute("SELECT status FROM submissions WHERE trial_id='t1'").fetchone()
        assert status["status"] == "error"
        assert conn.execute("SELECT 1 FROM sealed_answers").fetchone() is None

    def test_validation_failure_marks_submission_error(self, conn, trial, tmp_path, fake_container):
        workspace, _ = fake_container
        os.symlink("/etc/passwd", workspace / "escape")
        with pytest.raises(artifacts.SealError, match="symlink"):
            seal_trial(conn, trial, tmp_path)
        status = conn.execute("SELECT status FROM submissions WHERE trial_id='t1'").fetchone()
        assert status["status"] == "error"

    def test_receipt_stable_across_queries(self, conn, trial, tmp_path, fake_container):
        workspace, _ = fake_container
        (workspace / "answer.txt").write_text("the answer")
        seal_trial(conn, trial, tmp_path)
        first = artifacts.get_receipt(conn, "t1")
        second = artifacts.get_receipt(conn, "t1")
        assert first == second
        assert first["digest"] and first["receipt_id"] == "r-123"

    def test_seal_without_submission_generates_receipt(self, conn, trial, tmp_path, fake_container):
        conn.execute("DELETE FROM submissions")
        conn.commit()
        workspace, _ = fake_container
        (workspace / "answer.txt").write_text("no intent")
        receipt = seal_trial(conn, trial, tmp_path, trigger="exit")
        assert receipt["receipt_id"] and len(receipt["receipt_id"]) == 32

    def test_diagnostic_patch_written_outside_the_answer(self, conn, trial, tmp_path, fake_container):
        workspace, _ = fake_container
        (workspace / "answer.txt").write_text("v1")
        # pre-agent baseline: file existed with different content
        baseline = tmp_path / "baseline"
        (baseline / "workspace").mkdir(parents=True)
        (baseline / "workspace" / "answer.txt").write_text("v0")
        receipt = seal_trial(conn, trial, tmp_path, trigger="submit", baseline_dir=baseline)
        patch = tmp_path / "runs" / trial["run_id"] / "diagnostics.patch"
        assert patch.is_file()
        assert "answer.txt" in patch.read_text()
        # the patch is not part of the sealed content
        answer_dir = tmp_path / "answers" / receipt["digest"]
        assert not (answer_dir / "diagnostics.patch").exists()
        assert [e["path"] for e in json.loads((answer_dir / "manifest.json").read_text())["files"]] \
            == ["workspace/answer.txt"]


class TestRecover:
    @staticmethod
    def _rmtree_force(path):
        """Remove a read-only published tree (simulated disk loss)."""

        def _chmod(func, target, _exc):
            os.chmod(target, 0o700)
            parent = os.path.dirname(target)
            if parent:
                os.chmod(parent, 0o700)  # unlink fails on the read-only parent
            func(target)

        shutil.rmtree(path, onerror=_chmod)

    def _staging_with_manifest(self, conn, trial, tmp_path):
        """Simulate a crash after copy+manifest but before publish/register."""
        run_id = trial["run_id"]
        staging = tmp_path / "sealing" / run_id / "staging"
        (staging / "workspace").mkdir(parents=True)
        (staging / "workspace" / "answer.txt").write_text("frozen before crash")
        manifest = artifacts.build_manifest(staging, "t1", run_id, "submit", None)
        (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        return staging, manifest

    def test_staging_with_manifest_completes_publish_and_register(self, conn, trial, tmp_path):
        staging, manifest = self._staging_with_manifest(conn, trial, tmp_path)
        artifacts.recover(conn, tmp_path)
        sealed = conn.execute("SELECT * FROM sealed_answers WHERE trial_id='t1'").fetchone()
        assert sealed["digest"] == artifacts.manifest_digest(manifest)
        assert sealed["status"] == "sealed"
        assert (tmp_path / "answers" / sealed["digest"] / "workspace" / "answer.txt").is_file()
        assert not staging.parent.exists()  # staging cleaned up
        status = conn.execute("SELECT status FROM submissions WHERE trial_id='t1'").fetchone()
        assert status["status"] == "sealed"
        # repeated recovery is a no-op
        artifacts.recover(conn, tmp_path)
        assert conn.execute("SELECT COUNT(*) FROM sealed_answers").fetchone()[0] == 1

    def test_staging_without_manifest_marks_anomaly(self, conn, trial, tmp_path):
        (tmp_path / "sealing" / trial["run_id"] / "staging").mkdir(parents=True)
        artifacts.recover(conn, tmp_path)
        sealed = conn.execute("SELECT * FROM sealed_answers WHERE trial_id='t1'").fetchone()
        assert sealed["status"] == "anomaly"
        assert "freeze could not be proven" in sealed["anomaly"]
        assert not (tmp_path / "sealing" / trial["run_id"]).exists()
        status = conn.execute("SELECT status FROM submissions WHERE trial_id='t1'").fetchone()
        assert status["status"] == "error"

    def test_published_but_unregistered_is_registered_from_disk(self, conn, trial, tmp_path):
        staging, manifest = self._staging_with_manifest(conn, trial, tmp_path)
        digest = artifacts.manifest_digest(manifest)
        artifacts.publish(staging, digest, tmp_path / "answers")
        # crash between publish and register: no sealed_answers row, staging gone
        artifacts.recover(conn, tmp_path)
        sealed = conn.execute("SELECT * FROM sealed_answers WHERE trial_id='t1'").fetchone()
        assert sealed["digest"] == digest
        assert sealed["status"] == "sealed"
        status = conn.execute("SELECT status FROM submissions WHERE trial_id='t1'").fetchone()
        assert status["status"] == "sealed"

    def test_registered_but_content_missing_marks_anomaly(self, conn, trial, tmp_path, fake_container):
        workspace, _ = fake_container
        (workspace / "answer.txt").write_text("the answer")
        seal_trial(conn, trial, tmp_path)
        digest = conn.execute("SELECT digest FROM sealed_answers").fetchone()["digest"]
        self._rmtree_force(tmp_path / "answers" / digest)  # disk content lost after registration
        artifacts.recover(conn, tmp_path)
        sealed = conn.execute("SELECT status, anomaly FROM sealed_answers").fetchone()
        assert sealed["status"] == "anomaly"
        assert "missing from disk" in sealed["anomaly"]
        status = conn.execute("SELECT status FROM submissions WHERE trial_id='t1'").fetchone()
        assert status["status"] == "error"

    def test_recovery_never_recollects_workspace(self, conn, trial, tmp_path, fake_container, monkeypatch):
        """Recovery works from disk manifests only; docker copy must not run."""
        workspace, calls = fake_container
        (workspace / "answer.txt").write_text("frozen before crash")
        staging, manifest = self._staging_with_manifest(conn, trial, tmp_path)

        def forbidden_copy(*args, **kwargs):
            raise AssertionError("recovery must never re-collect a workspace")

        monkeypatch.setattr(artifacts, "copy_from_container", forbidden_copy)
        artifacts.recover(conn, tmp_path)
        sealed = conn.execute("SELECT digest FROM sealed_answers").fetchone()
        assert sealed["digest"] == artifacts.manifest_digest(manifest)
        assert calls["copy"] == 0

    def test_orphan_staging_of_unknown_run_removed(self, conn, trial, tmp_path):
        (tmp_path / "sealing" / "unknown-run" / "staging").mkdir(parents=True)
        (tmp_path / "sealing" / "unknown-run" / "staging" / "x.txt").write_text("y")
        artifacts.recover(conn, tmp_path)
        assert not (tmp_path / "sealing" / "unknown-run").exists()
