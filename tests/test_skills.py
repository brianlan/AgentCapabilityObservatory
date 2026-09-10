"""Content-addressed SkillVersion + explicit pi skill loading (#39)."""

import shutil
import sqlite3

import pytest

from aco.app import AppError
from aco import skills

SKILL_MD = "---\nname: demo\ndescription: adds two numbers\n---\n\nAdd the numbers.\n"


@pytest.fixture
def conn(tmp_path):
    from aco import db
    conn = db.connect(tmp_path / "aco.db")
    db.migrate(conn)
    return conn


@pytest.fixture
def bundle(tmp_path):
    b = tmp_path / "demo-skill"
    (b / "assets").mkdir(parents=True)
    (b / "SKILL.md").write_text(SKILL_MD)
    (b / "assets" / "table.md").write_text("| a | b |\n|---|---|\n")
    return b


def make_profile(skills_list):
    return {
        "schema_version": 1, "harness": "pi", "harness_version": "0.84.1",
        "model": "glm-5.3-flash", "thinking": "max", "provider": "ark-agent-plan",
        "provider_api_style": "openai-responses", "adapter_version": "0.1.0",
        "credentials": ["ark-agent-plan-main"],
        "skills": skills_list,
    }


def test_import_registers_digest_and_copies_bytes(conn, tmp_path, bundle):
    record = skills.import_skill(bundle, tmp_path, "demo", "v1", conn)
    assert record["kind"] == "skill"
    assert record["content"]["entry"] == "SKILL.md"
    digest = record["content"]["bundle"]["digest"]
    assert digest == record["assets"][0]["digest"]
    copied = tmp_path / "skills" / record["id"]
    assert (copied / "SKILL.md").read_text() == SKILL_MD
    assert (copied / "assets" / "table.md").exists()


def test_reimport_identical_content_is_idempotent(conn, tmp_path, bundle):
    first = skills.import_skill(bundle, tmp_path, "demo", "v1", conn)
    second = skills.import_skill(bundle, tmp_path, "demo", "v1", conn)
    assert first["id"] == second["id"]


def test_same_name_version_different_content_conflicts(conn, tmp_path, bundle):
    skills.import_skill(bundle, tmp_path, "demo", "v1", conn)
    (bundle / "SKILL.md").write_text(SKILL_MD + "\nchanged\n")
    with pytest.raises(AppError) as exc:
        skills.import_skill(bundle, tmp_path, "demo", "v1", conn)
    assert exc.value.status == 409


def test_missing_entry_rejected(conn, tmp_path):
    b = tmp_path / "empty"
    b.mkdir()
    with pytest.raises(skills.SkillImportError, match="SKILL.md"):
        skills.import_skill(b, tmp_path, "demo", "v1", conn)


def test_symlink_rejected(conn, tmp_path, bundle):
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    (bundle / "link.md").symlink_to(outside)
    with pytest.raises(skills.SkillImportError, match="symlink"):
        skills.import_skill(bundle, tmp_path, "demo", "v1", conn)


def test_oversized_bundle_rejected(conn, tmp_path, bundle):
    (bundle / "blob.bin").write_bytes(b"x" * (skills.SKILL_MAX_BYTES + 1))
    with pytest.raises(skills.SkillImportError, match="too large"):
        skills.import_skill(bundle, tmp_path, "demo", "v1", conn)


@pytest.mark.parametrize("secret", [
    "token = sk-abcdefghijklmnopqrstuvwx\n",
    "token = ghp_" + "a" * 36 + "\n",
    "key = AKIAIOSFODNN7EXAMPLE\n",
    "-----BEGIN RSA PRIVATE KEY-----\n",
])
def test_credential_content_rejected(conn, tmp_path, bundle, secret):
    (bundle / "notes.md").write_text(secret)
    with pytest.raises(skills.SkillImportError, match="credential"):
        skills.import_skill(bundle, tmp_path, "demo", "v1", conn)


def test_invalid_name_rejected(conn, tmp_path, bundle):
    with pytest.raises(skills.SkillImportError, match="name"):
        skills.import_skill(bundle, tmp_path, "../escape", "v1", conn)


# ------------------------------------------------- config references (#39)

PI_CONFIG = make_profile([])


def test_v1_config_requires_registered_skill_version(client, register):
    register("config", "pi-skill", "v1",
             make_profile([{"name": "ghost", "version": "v9"}]), expect=(422,))
    resp = client.post("/v1/versions", json={
        "kind": "config", "name": "pi-skill", "version": "v1",
        "content": make_profile([{"name": "ghost", "version": ""}])})
    assert resp.status_code == 422


def test_v1_config_resolves_declared_skills(client, register):
    register("skill", "a-skill", "v1", {
        "schema_version": 1, "entry": "SKILL.md",
        "bundle": {"digest": "a" * 64, "bytes": 1, "files": 1}})
    register("skill", "b-skill", "v1", {
        "schema_version": 1, "entry": "SKILL.md",
        "bundle": {"digest": "b" * 64, "bytes": 1, "files": 1}})
    register("config", "pi-ordered", "v1", make_profile(
        [{"name": "b-skill", "version": "v1"}, {"name": "a-skill", "version": "v1"}]))


def test_skill_order_and_content_change_fingerprint(client, register, tmp_path, bundle):
    from aco.models import parse_config_content

    conn = sqlite3.connect(tmp_path / "aco.db")
    conn.row_factory = sqlite3.Row
    register("skill", "demo", "v1", {
        "schema_version": 1, "entry": "SKILL.md",
        "bundle": {"digest": "d" * 64, "bytes": 10, "files": 1}})
    profile_a = parse_config_content(make_profile([{"name": "demo", "version": "v1"}]))
    profile_b = parse_config_content(make_profile([]))
    profile_c = parse_config_content(make_profile(
        [{"name": "demo", "version": "v1"}, {"name": "other", "version": "v1"}]))
    from aco.app import trial_fingerprint
    assert trial_fingerprint(profile_a) != trial_fingerprint(profile_b)
    assert trial_fingerprint(profile_a) != trial_fingerprint(profile_c)
