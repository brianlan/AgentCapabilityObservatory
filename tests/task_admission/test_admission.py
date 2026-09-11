"""Task admission tests (#20).

Static gates and gate orchestration run against real bundle files with the
container boundary faked; the full pipeline (real seal + real verifier
container + real image-layer scan) lives in the e2e test.
"""

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from aco import admission, db
from aco.admission import AdmissionError
from aco.verification import runner as verification_runner


def runner_bundle_digest(bundle_dir):
    return verification_runner.bundle_digest(bundle_dir)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "tasks" / "synthetic-add"
PASS = {"status": "succeeded", "pass": True}
FAIL = {"status": "succeeded", "pass": False}


@pytest.fixture()
def bundle(tmp_path) -> Path:
    """A writable copy of the committed synthetic fixture."""
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURE, target)
    for p in target.rglob("*"):
        p.chmod(0o755 if p.is_dir() else 0o644)  # copytree keeps read-only modes
    return target


@pytest.fixture()
def root(tmp_path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


def fake_answers(root, label, content: bytes):
    """A minimal published answer dir the fakes can point at."""
    answer_dir = root / "answers-fake" / label
    (answer_dir / "workspace").mkdir(parents=True, exist_ok=True)
    (answer_dir / "workspace" / "answer.txt").write_bytes(content)
    manifest = {"files": [{"path": "workspace/answer.txt",
                           "sha256": admission._digest(content)}]}
    (answer_dir / "manifest.json").write_text(json.dumps(manifest))
    return answer_dir


def patch_containers(monkeypatch, root, verdicts):
    """Fake the container boundary: prepare_answer returns a fake published
    answer; score_answer replays `verdicts`, a callable of (gate, index)."""

    def fake_prepare(image, workspace_src, data_root, label, contract):
        gate = label.split("-")[2]
        return fake_answers(root, label.replace("-", "_"), b"answer-" + gate.encode())

    def fake_score(answer_dir, bundle_dir, verifier, data_root, label):
        _, _, gate, index = label.split("-")
        return verdicts(gate, int(index))

    monkeypatch.setattr(admission, "prepare_answer", fake_prepare)
    monkeypatch.setattr(admission, "score_answer", fake_score)


def run_report(bundle_path, root):
    return admission.run_admission(bundle_path, root)


def test_report_schema_and_gate_names(bundle, root, monkeypatch):
    patch_containers(monkeypatch, root, lambda gate, index: PASS if gate == "oracle" else FAIL)
    report = run_report(bundle, root)
    assert report["schema"] == admission.SCHEMA
    assert set(report["gates"]) == {"static", "oracle", "nop", "cheats", "rescore",
                                    "registration", "rebuild"}
    assert set(report["digests"]) == {"verifier_bundle", "environment", "artifact_contract", "task_version"}


def test_report_records_registered_task_version(bundle, root, monkeypatch):
    """Regression: _register_candidate must return the locked registration
    result; dropping it left report digests.task_version null and keyed the
    report files by a name digest instead of the registered version id."""
    patch_containers(monkeypatch, root, lambda gate, index: PASS if gate == "oracle" else FAIL)
    report = run_report(bundle, root)
    task_version = report["digests"]["task_version"]
    assert task_version is not None
    conn = sqlite3.connect(root / "aco.db")
    row = conn.execute("SELECT kind, name FROM versions WHERE id = ?", (task_version,)).fetchone()
    conn.close()
    assert row == ("task", "synthetic-add")
    report_dir = root / "admission"
    assert [p.name for p in report_dir.glob("*.json")] == [f"{task_version}.json"]


def test_clean_fixture_passes_leak_scan(bundle):
    """Regression: the unmodified committed fixture must pass the public
    leak scan — unchanged starter files shared with private workspaces are
    exempt, not leaks (review finding on the same-path mapping)."""
    checks = admission.static_checks(admission.Bundle.load(bundle))
    leak = next(c for c in checks if c["name"] == "no_hidden_assets_in_public_bundle")
    assert leak["ok"] is True, leak["detail"]


def test_unchanged_public_file_inside_cheat_case_is_not_a_leak(bundle):
    """A wrong-answer workspace may repeat unchanged public files (e.g. the
    README) without tripping the leak scan; only genuinely hidden content
    appearing in the public tree is a leak."""
    shutil.copy(bundle / "public/environment/workspace/README.md",
                bundle / "private/wrong_answers/deleted-feature/workspace/README.md")
    checks = admission.static_checks(admission.Bundle.load(bundle))
    leak = next(c for c in checks if c["name"] == "no_hidden_assets_in_public_bundle")
    assert leak["ok"] is True, leak["detail"]


def test_good_bundle_passes_all_gates(bundle, root, monkeypatch):
    patch_containers(monkeypatch, root, lambda gate, index: PASS if gate == "oracle" else FAIL)
    report = run_report(bundle, root)
    assert report["all_passed"] is True
    assert report["promotion"] == {"candidate": True, "core": False, "reviewed_by": None}


def test_oracle_failure_fails_admission(bundle, root, monkeypatch):
    patch_containers(monkeypatch, root, lambda gate, index: FAIL if gate == "oracle" else FAIL)
    report = run_report(bundle, root)
    assert report["gates"]["oracle"]["ok"] is False
    assert "did not pass" in report["gates"]["oracle"]["detail"]


def test_unstable_reference_content_fails_oracle(bundle, root, monkeypatch):
    # different sealed content per run: the determinism evidence fails
    monkeypatch.setattr(admission, "prepare_answer",
                        lambda image, src, r, label, contract: fake_answers(root, label, label.encode()))
    monkeypatch.setattr(admission, "score_answer", lambda *a: PASS)
    report = run_report(bundle, root)
    assert report["gates"]["oracle"]["ok"] is False
    assert "different sealed content" in report["gates"]["oracle"]["detail"]


def test_nop_passing_fails_admission(bundle, root, monkeypatch):
    # a verifier that even accepts the untouched workspace is broken
    patch_containers(monkeypatch, root, lambda gate, index: PASS)
    report = run_report(bundle, root)
    assert report["gates"]["nop"]["ok"] is False
    assert report["gates"]["cheats"]["ok"] is False  # passing wrong answers, too


def test_inconsistent_rescoring_fails_admission(bundle, root, monkeypatch):
    verdicts = {("oracle", i): PASS for i in (1, 2, 3)}
    verdicts[("nop", 1)] = verdicts[("nop", 2)] = verdicts[("nop", 3)] = FAIL
    verdicts[("cheat", 1)] = verdicts[("cheat", 2)] = FAIL
    verdicts[("cheat", 3)] = verdicts[("cheat", 4)] = FAIL
    verdicts[("rescore", 1)] = PASS
    verdicts[("rescore", 2)] = FAIL
    patch_containers(monkeypatch, root, lambda gate, index: verdicts[(gate, index)])
    report = run_report(bundle, root)
    assert report["gates"]["rescore"]["ok"] is False
    assert "disagreed" in report["gates"]["rescore"]["detail"]


def test_cheat_case_passing_fails_admission_with_case_name(bundle, root, monkeypatch):
    verdicts = {("oracle", i): PASS for i in (1, 2, 3)}
    verdicts[("nop", 1)] = verdicts[("nop", 2)] = verdicts[("nop", 3)] = FAIL
    verdicts[("cheat", 1)] = FAIL  # deleted-feature fails
    verdicts[("cheat", 2)] = PASS  # fabricated-results sneaks through
    verdicts[("cheat", 3)] = FAIL
    verdicts[("cheat", 4)] = FAIL
    verdicts[("rescore", 1)] = verdicts[("rescore", 2)] = PASS
    patch_containers(monkeypatch, root, lambda gate, index: verdicts[(gate, index)])
    report = run_report(bundle, root)
    assert report["gates"]["cheats"]["ok"] is False
    assert "fabricated-results" in report["gates"]["cheats"]["detail"]


def test_provenance_missing_fails_static(bundle, root):
    text = (bundle / "task.toml").read_text().replace('license = "MIT"\n', "")
    (bundle / "task.toml").write_text(text)
    report = run_report(bundle, root)
    assert report["all_passed"] is False
    static = report["gates"]["static"]
    assert any(not c["ok"] and "license" in c["detail"] for c in static["checks"])
    # execution gates are skipped, not run on a static failure
    assert report["gates"]["oracle"]["detail"].startswith("skipped")


def test_hidden_asset_in_public_bundle_fails_static(bundle, root):
    shutil.copy(bundle / "private/verifier/run.py", bundle / "public/environment/run.py")
    report = run_report(bundle, root)
    leak = next(c for c in report["gates"]["static"]["checks"]
                if c["name"] == "no_hidden_assets_in_public_bundle")
    assert leak["ok"] is False
    assert "run.py" in leak["detail"]


def test_symlink_in_bundle_fails_static(bundle, root):
    (bundle / "public/environment/link.py").symlink_to("solution.py")
    report = run_report(bundle, root)
    check = next(c for c in report["gates"]["static"]["checks"]
                 if c["name"] == "no_symlinks_or_special_files")
    assert check["ok"] is False


def test_hidden_asset_in_image_layers_fails_static(bundle, root, monkeypatch):
    hidden = admission._hidden_digests(admission.Bundle.load(bundle))
    leaked_digest = sorted(hidden)[0]
    monkeypatch.setattr(admission, "image_layer_file_digests", lambda image: {leaked_digest})
    report = run_report(bundle, root)
    check = next(c for c in report["gates"]["static"]["checks"]
                 if c["name"] == "no_hidden_assets_in_image_layers")
    assert check["ok"] is False


def test_missing_verifier_bundle_fails_load(tmp_path):
    broken = tmp_path / "bundle"
    shutil.copytree(FIXTURE, broken)
    shutil.rmtree(broken / "private/verifier")
    with pytest.raises(AdmissionError, match="missing directory"):
        admission.Bundle.load(broken)


def test_report_digests_stable_across_reruns(bundle, root, monkeypatch):
    patch_containers(monkeypatch, root, lambda gate, index: PASS if gate == "oracle" else FAIL)
    first = run_report(bundle, root)
    second = run_report(bundle, root)
    assert first["digests"] == second["digests"]
    assert first["task"] == second["task"]
    assert {k: v["ok"] for k, v in first["gates"].items()} == \
           {k: v["ok"] for k, v in second["gates"].items()}


def test_report_written_as_json_and_markdown(bundle, root, monkeypatch):
    patch_containers(monkeypatch, root, lambda gate, index: PASS if gate == "oracle" else FAIL)
    report = run_report(bundle, root)
    on_disk = json.loads(Path(report["report"]["json"]).read_text())
    assert on_disk["schema"] == admission.SCHEMA
    markdown = Path(report["report"]["markdown"]).read_text()
    assert "Admission report" in markdown and "PASS" in markdown


def test_static_failure_never_registers_versions(bundle, root):
    text = (bundle / "task.toml").read_text().replace('license = "MIT"\n', "")
    (bundle / "task.toml").write_text(text)
    report = run_report(bundle, root)
    assert report["gates"]["registration"]["detail"].startswith("skipped")
    conn = sqlite3.connect(root / "aco.db")
    count = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    conn.close()
    assert count == 0  # failed admission registers nothing


def scorer_row(root):
    conn = sqlite3.connect(root / "aco.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, assets FROM versions WHERE kind = 'scorer'").fetchone()
    conn.close()
    return row


def test_scorer_registration_conflict_leaves_no_versions(bundle, root, monkeypatch):
    """Regression (review finding): the task insert commits independently, so
    a scorer-registration conflict after it must not leave a partially
    registered candidate. The failed attempt rolls back its own rows, keeps
    pre-existing ones, and removes the imported verifier bundle."""
    from aco.app import register_version, version_digest
    from aco.models import AssetRef, VersionRegistration

    b = admission.Bundle.load(bundle)
    conflicting = dict(b.verifier.model_dump())
    conflicting["entrypoint"] = ["python", "other.py"]  # same name@version, different content
    conn = db.connect(root / "aco.db")
    db.migrate(conn)
    register_version(conn, VersionRegistration(
        kind="scorer", name=f"{b.manifest['name']}-verifier", version=b.manifest["version"],
        content=conflicting))
    conn.close()
    bundle_digest = runner_bundle_digest(b.verifier_bundle)
    scorer_id = version_digest(
        "scorer", f"{b.manifest['name']}-verifier", b.manifest["version"],
        b.verifier.model_dump(), [AssetRef(name="bundle", digest=bundle_digest)])
    patch_containers(monkeypatch, root, lambda gate, index: PASS if gate == "oracle" else FAIL)
    report = run_report(bundle, root)
    assert report["gates"]["registration"]["ok"] is False
    assert "registry rejected" in report["gates"]["registration"]["detail"]
    assert report["all_passed"] is False
    conn = sqlite3.connect(root / "aco.db")
    rows = conn.execute("SELECT kind, name FROM versions").fetchall()
    conn.close()
    assert rows == [("scorer", f"{b.manifest['name']}-verifier")]  # only the pre-existing row
    assert not (root / "verifiers" / scorer_id).exists()  # imported bundle removed


def test_idempotent_task_row_survives_registration_failure(bundle, root, monkeypatch):
    """Regression (review finding): a row handed back as idempotent (200) must
    never be rolled back — simulates a concurrent admission inserting the
    identical task between this attempt's snapshot and its failure."""
    from aco.app import register_version, version_digest
    from aco.models import AssetRef, VersionRegistration

    b = admission.Bundle.load(bundle)
    conflicting = dict(b.verifier.model_dump())
    conflicting["entrypoint"] = ["python", "other.py"]  # same name@version, different content
    conn = db.connect(root / "aco.db")
    db.migrate(conn)
    register_version(conn, VersionRegistration(
        kind="scorer", name=f"{b.manifest['name']}-verifier", version=b.manifest["version"],
        content=conflicting))
    conn.close()

    real_register = admission.register_version
    interleaved = {"task": False}

    def register_with_concurrent_task_insert(conn, reg):
        if reg.kind == "task" and not interleaved["task"]:
            interleaved["task"] = True
            real_register(conn, reg)  # concurrent admission creates the identical row
            return real_register(conn, reg)  # this attempt gets it back as 200
        return real_register(conn, reg)

    monkeypatch.setattr(admission, "register_version", register_with_concurrent_task_insert)
    bundle_digest = runner_bundle_digest(b.verifier_bundle)
    scorer_id = version_digest(
        "scorer", f"{b.manifest['name']}-verifier", b.manifest["version"],
        b.verifier.model_dump(), [AssetRef(name="bundle", digest=bundle_digest)])
    patch_containers(monkeypatch, root, lambda gate, index: PASS if gate == "oracle" else FAIL)
    report = run_report(bundle, root)
    assert report["gates"]["registration"]["ok"] is False
    assert "registry rejected" in report["gates"]["registration"]["detail"]
    conn = sqlite3.connect(root / "aco.db")
    rows = sorted(conn.execute("SELECT kind, name FROM versions").fetchall())
    conn.close()
    # the idempotent task row and the pre-existing conflicting scorer survive
    assert rows == sorted([("task", b.manifest["name"]),
                           ("scorer", f"{b.manifest['name']}-verifier")])
    assert not (root / "verifiers" / scorer_id).exists()  # bundle still cleaned up


def test_failed_admission_serializes_and_keeps_concurrent_attempt_working(bundle, root, monkeypatch):
    """Regression (review finding): concurrent admissions of the same
    content-addressed verifier serialize on a lock file — a failing attempt
    finishes its rollback (rows + staging) before another attempt can build
    on its rows or directory, so B always ends with a usable bundle."""
    import fcntl as fcntl_module
    import threading
    from aco.app import AppError, register_version as real_register, version_digest
    from aco.models import AssetRef

    b = admission.Bundle.load(bundle)
    events = {"a_staged": threading.Event(), "b_waiting": threading.Event(),
              "release": threading.Event()}
    tids = {"a": None, "b": None}
    reports = {}
    real_flock = fcntl_module.flock

    def flock_with_signal(file, request):
        if threading.get_ident() == tids["b"] and request == fcntl_module.LOCK_EX \
                and not events["b_waiting"].is_set():
            events["b_waiting"].set()  # B is blocked while A owns the critical section
        return real_flock(file, request)

    def register_for_interleaving(conn, reg):
        tid = threading.get_ident()
        if tid == tids["a"] and reg.kind == "task":
            result = real_register(conn, reg)  # A's task row (rolled back later)
            events["a_staged"].set()  # A holds the lock, task row committed
            events["release"].wait(10)  # failure held back until B is blocked on the lock
            return result
        if tid == tids["a"] and reg.kind == "scorer":
            raise AppError(409, "version_conflict", "injected failure for A")
        return real_register(conn, reg)

    monkeypatch.setattr(fcntl_module, "flock", flock_with_signal)
    monkeypatch.setattr(admission, "register_version", register_for_interleaving)
    patch_containers(monkeypatch, root, lambda gate, index: PASS if gate == "oracle" else FAIL)

    def run(name, expect_ok):
        tids[name] = threading.get_ident()
        report = run_report(bundle, root)
        reports[name] = report
        assert report["all_passed"] is expect_ok

    thread_a = threading.Thread(target=run, args=("a", False))
    thread_a.start()
    assert events["a_staged"].wait(10), "A never reached registration"
    thread_b = threading.Thread(target=run, args=("b", True))
    thread_b.start()
    assert events["b_waiting"].wait(10), "B never blocked on the admission lock"
    events["release"].set()  # A fails, rolls back, unlocks; then B proceeds
    thread_a.join(30)
    thread_b.join(30)
    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert reports["a"]["gates"]["registration"]["ok"] is False
    assert "registry rejected" in reports["a"]["gates"]["registration"]["detail"]

    bundle_digest = runner_bundle_digest(b.verifier_bundle)
    scorer_id = version_digest(
        "scorer", f"{b.manifest['name']}-verifier", b.manifest["version"],
        b.verifier.model_dump(), [AssetRef(name="bundle", digest=bundle_digest)])
    published = root / "verifiers" / scorer_id
    assert published.is_dir(), "concurrent admission lost its verifier bundle"
    assert runner_bundle_digest(published) == bundle_digest
    conn = sqlite3.connect(root / "aco.db")
    rows = sorted(conn.execute("SELECT kind, name FROM versions").fetchall())
    conn.close()
    assert rows == sorted([("task", b.manifest["name"]),
                           ("scorer", f"{b.manifest['name']}-verifier")])  # B's rows only


def test_partial_copy_failure_leaves_no_staging_or_rows(bundle, root, monkeypatch):
    """Regression (review finding): a filesystem failure during the staging
    copy must not leave a partial .import-* directory or registered rows."""
    real_copytree = shutil.copytree

    def copy_then_fail(src, dst, *args, **kwargs):
        real_copytree(src, dst, *args, **kwargs)  # complete the copy...
        if "verifiers" not in str(dst):
            return  # the environment publish copies cleanly; target the verifier staging
        (Path(dst) / "run.py").unlink()    # ...then simulate a mid-copy error
        raise OSError("injected disk failure mid-copy")

    monkeypatch.setattr(admission.shutil, "copytree", copy_then_fail)
    patch_containers(monkeypatch, root, lambda gate, index: PASS if gate == "oracle" else FAIL)
    with pytest.raises(OSError, match="injected disk failure"):
        run_report(bundle, root)

    assert [p.name for p in (root / "verifiers").iterdir()
            if p.name.startswith(".import-")] == []
    conn = sqlite3.connect(root / "aco.db")
    assert conn.execute("SELECT kind, name FROM versions").fetchall() == []
    conn.close()


def test_staging_digest_failure_leaves_no_staging_or_rows(bundle, root, monkeypatch):
    """Regression (review finding): a failing staging digest check must roll
    back rows and remove the staging directory — no apparently usable or
    orphaned candidate state."""
    real_digest = admission.runner.bundle_digest

    def wrong_for_staging(path):
        # the environment publish stages under environments/.import-*; the
        # regression target is the verifier staging copy only
        if ".import-" in str(path) and "verifiers" in str(path):
            return "0" * 64  # digest check fails only for the staging copy
        return real_digest(path)

    monkeypatch.setattr(admission.runner, "bundle_digest", wrong_for_staging)
    patch_containers(monkeypatch, root, lambda gate, index: PASS if gate == "oracle" else FAIL)
    report = run_report(bundle, root)
    assert report["gates"]["registration"]["ok"] is False
    assert "digest mismatch" in report["gates"]["registration"]["detail"]

    assert [p.name for p in (root / "verifiers").iterdir()
            if p.name.startswith(".import-")] == []
    conn = sqlite3.connect(root / "aco.db")
    assert conn.execute("SELECT kind, name FROM versions").fetchall() == []
    conn.close()


def test_admission_leaves_verifier_bundle_usable_for_runtime(bundle, root, monkeypatch):
    """A successful admission must leave the trusted verifier bundle where
    verification.runner loads it, with a digest matching the registered asset."""
    from aco.app import version_digest
    from aco.models import AssetRef

    b = admission.Bundle.load(bundle)
    bundle_digest = runner_bundle_digest(b.verifier_bundle)
    scorer_id = version_digest(
        "scorer", f"{b.manifest['name']}-verifier", b.manifest["version"],
        b.verifier.model_dump(), [AssetRef(name="bundle", digest=bundle_digest)])
    # pre-create a conflicting bundle: registration must refuse and record it
    (root / "verifiers" / scorer_id).mkdir(parents=True)
    (root / "verifiers" / scorer_id / "run.py").write_text("CORRUPTED")
    patch_containers(monkeypatch, root, lambda gate, index: PASS if gate == "oracle" else FAIL)
    report = run_report(bundle, root)
    assert report["gates"]["registration"]["ok"] is False
    assert "different content" in report["gates"]["registration"]["detail"]
    assert report["all_passed"] is False
    conn = sqlite3.connect(root / "aco.db")
    assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 0
    conn.close()

    # now the clean path: bundle imported, digest matches the registered asset
    shutil.rmtree(root / "verifiers" / scorer_id)
    report = run_report(bundle, root)
    assert report["gates"]["registration"]["ok"] is True
    assert report["all_passed"] is True
    row = scorer_row(root)
    declared = [a["digest"] for a in json.loads(row["assets"]) if a["name"] == "bundle"]
    dest = root / "verifiers" / row["id"]
    assert dest.is_dir()
    assert runner_bundle_digest(dest) == declared[0] == bundle_digest


def test_promote_requires_human_review_and_passing_report(bundle, root, tmp_path):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps({"schema": admission.SCHEMA, "all_passed": False}))
    with pytest.raises(AdmissionError, match="failing gates"):
        admission.promote(report_path, "core", "human")
    report_path.write_text(json.dumps({"schema": admission.SCHEMA, "all_passed": True}))
    with pytest.raises(AdmissionError, match="human reviewer"):
        admission.promote(report_path, "core", None)
    promoted = admission.promote(report_path, "core", "reviewer@example")
    assert promoted["promotion"]["core"] is True
    assert promoted["promotion"]["reviewed_by"] == "reviewer@example"


def test_cli_exit_codes(bundle, root, monkeypatch):
    patch_containers(monkeypatch, root, lambda gate, index: PASS if gate == "oracle" else FAIL)
    assert admission.main(["admit", str(bundle), "--data-root", str(root)]) == 0
    patch_containers(monkeypatch, root, lambda gate, index: FAIL)
    assert admission.main(["admit", str(bundle), "--data-root", str(root)]) == 1
    (root / "broken").mkdir(exist_ok=True)
    assert admission.main(["admit", str(root / "broken"), "--data-root", str(root)]) == 2


def test_repo_contains_only_the_synthetic_fixture():
    """No private task content may live in the public repo (#20)."""
    fixture_root = FIXTURE.parent
    assert [p.name for p in fixture_root.iterdir()] == ["synthetic-add"]
    # the fixture is openly synthetic by its own provenance
    manifest = (FIXTURE / "task.toml").read_text()
    assert "synthetic fixture" in manifest


def test_delete_author_source_then_rebuild_from_data_root(bundle, root, monkeypatch):
    """Required regression (#20 reopen): after registration the author's
    source directory can be deleted — instruction and initial workspace must
    rebuild from the data root + registered TaskVersion alone."""
    patch_containers(monkeypatch, root, lambda gate, index: PASS if gate == "oracle" else FAIL)
    report = run_report(bundle, root)
    assert report["all_passed"] is True

    author_workspace = {
        p.relative_to(bundle / "public/environment/workspace").as_posix():
            admission._digest(p.read_bytes())
        for p in (bundle / "public/environment/workspace").rglob("*") if p.is_file()
    }
    author_instruction = (bundle / "public/environment/workspace/README.md").read_text()
    shutil.rmtree(bundle)  # the author tree is gone

    conn = sqlite3.connect(root / "aco.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT content, assets FROM versions WHERE id = ?",
                       (report["digests"]["task_version"],)).fetchone()
    conn.close()
    from aco import environments as aco_environments
    instruction, env_dir = aco_environments.resolve_instruction(
        json.loads(row["content"]), json.loads(row["assets"]), root)
    assert instruction == author_instruction
    assert (env_dir / "workspace" / "README.md").read_text() == author_instruction
    rebuilt = {
        p.relative_to(env_dir / "workspace").as_posix(): admission._digest(p.read_bytes())
        for p in (env_dir / "workspace").rglob("*") if p.is_file()
    }
    assert rebuilt == author_workspace
    assert verification_runner.bundle_digest(env_dir) == report["digests"]["environment"]
    assert report["gates"]["rebuild"]["ok"] is True


def test_environment_store_contains_only_public_assets(bundle, root, monkeypatch):
    """Required regression (#20 reopen): the published environment store must
    contain no hidden asset bytes — verifier/reference/wrong-answer content
    can never reach the materialized /workspace through the store."""
    patch_containers(monkeypatch, root, lambda gate, index: PASS if gate == "oracle" else FAIL)
    report = run_report(bundle, root)
    assert report["all_passed"] is True

    digest = report["digests"]["environment"]
    store_tree = root / "environments" / digest
    stored = {admission._digest(p.read_bytes()) for p in store_tree.rglob("*") if p.is_file()}
    hidden = admission._hidden_digests(admission.Bundle.load(bundle))
    assert not (stored & hidden), "hidden asset bytes inside the environment store"
    # the store is byte-identical to the author's public environment
    public = set(admission._tree_digests(bundle / "public/environment").values())
    assert stored == public
