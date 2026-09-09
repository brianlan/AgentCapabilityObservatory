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

    def fake_prepare(image, workspace_src, data_root, label):
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
    assert set(report["gates"]) == {"static", "oracle", "nop", "cheats", "rescore"}
    assert set(report["digests"]) == {"verifier_bundle", "environment", "artifact_contract", "task_version"}


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
                        lambda image, src, r, label: fake_answers(root, label, label.encode()))
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
    run_report(bundle, root)
    conn = sqlite3.connect(root / "aco.db")
    count = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    conn.close()
    assert count == 0  # failed admission registers nothing


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
