"""End-to-end admission: the synthetic fixture through every real gate (#20).

Runs the real pipeline — real container sealing, the real verifier container,
and a real image-layer scan — against the committed synthetic fixture.
Skipped when no Docker daemon is reachable.
"""

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from aco import admission, db

FIXTURE = Path(__file__).parent.parent / "fixtures" / "tasks" / "synthetic-add"


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0


pytestmark = pytest.mark.skipif(not docker_available(), reason="docker daemon not reachable")


@pytest.fixture()
def bundle(tmp_path) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURE, target)
    for p in target.rglob("*"):
        p.chmod(0o755 if p.is_dir() else 0o644)
    return target


def test_full_admission_of_synthetic_fixture(bundle, tmp_path):
    root = tmp_path / "data"
    code = admission.main(["admit", str(bundle), "--data-root", str(root)])
    assert code == 0

    reports = list((root / "admission").glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["all_passed"] is True
    assert all(gate["ok"] for gate in report["gates"].values())
    assert report["gates"]["oracle"]["detail"] == \
        "reference answer passed 3/3 with identical sealed content"
    assert report["gates"]["cheats"]["cases"] == [
        "deleted-feature", "fabricated-results",
        "hardcoded-public-sample", "modified-visible-tests",
    ]

    # the task and scorer versions are registered through the normal registry
    conn = sqlite3.connect(root / "aco.db")
    rows = conn.execute("SELECT kind, name, version FROM versions ORDER BY kind").fetchall()
    conn.close()
    assert rows == [("scorer", "synthetic-add-verifier", "v1"), ("task", "synthetic-add", "v1")]

    # the report records task/verifier/environment/contract digests + provenance
    assert all(report["digests"][key] for key in
               ("verifier_bundle", "environment", "artifact_contract"))
    assert report["task"]["provenance"]["license"] == "MIT"

    # rerun is idempotent: same digests, registration stays idempotent
    code_again = admission.main(["admit", str(bundle), "--data-root", str(root)])
    assert code_again == 0
    conn = sqlite3.connect(root / "aco.db")
    count = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    conn.close()
    assert count == 2

    # Core promotion refuses without an explicit human reviewer (CLI: exit 2)
    assert admission.main(["promote", str(reports[0]), "--to", "core"]) == 2


def test_leak_scan_rejects_bundled_reference(bundle, tmp_path):
    shutil.copy(bundle / "private/reference/workspace/answer.txt",
                bundle / "public/environment/answer.txt")
    root = tmp_path / "data"
    assert admission.main(["admit", str(bundle), "--data-root", str(root)]) == 1
    report = json.loads(next((root / "admission").glob("*.json")).read_text())
    leak = next(c for c in report["gates"]["static"]["checks"]
                if c["name"] == "no_hidden_assets_in_public_bundle")
    assert leak["ok"] is False
    assert "answer.txt" in leak["detail"]
