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
        "provider_api_style": "openai-responses", "adapter_version": "0.1.1",
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
    for name, digest in (("a-skill", "a" * 64), ("b-skill", "b" * 64)):
        register("skill", name, "v1",
                 {"schema_version": 1, "entry": "SKILL.md",
                  "bundle": {"digest": digest, "bytes": 1, "files": 1}},
                 assets=[{"name": "bundle", "digest": digest}])
    register("config", "pi-ordered", "v1", make_profile(
        [{"name": "b-skill", "version": "v1"}, {"name": "a-skill", "version": "v1"}]))


def test_skill_order_and_content_change_fingerprint(client, register, tmp_path, bundle):
    from aco.models import parse_config_content

    conn = sqlite3.connect(tmp_path / "aco.db")
    conn.row_factory = sqlite3.Row
    register("skill", "demo", "v1", {
        "schema_version": 1, "entry": "SKILL.md",
        "bundle": {"digest": "d" * 64, "bytes": 10, "files": 1}},
        assets=[{"name": "bundle", "digest": "d" * 64}])
    profile_a = parse_config_content(make_profile([{"name": "demo", "version": "v1"}]))
    profile_b = parse_config_content(make_profile([]))
    profile_c = parse_config_content(make_profile(
        [{"name": "demo", "version": "v1"}, {"name": "other", "version": "v1"}]))
    from aco.app import trial_fingerprint
    assert trial_fingerprint(profile_a) != trial_fingerprint(profile_b)
    assert trial_fingerprint(profile_a) != trial_fingerprint(profile_c)


# --- reopened scope (#39): one strict schema on every registration entry ---

_SKILL_C = {"schema_version": 1, "entry": "SKILL.md",
            "bundle": {"digest": "a" * 64, "bytes": 1, "files": 1}}


def test_api_rejects_malformed_skill_registration(client, register):
    """The generic Registry API cannot register a skill the import path
    could never produce; malformed content/assets get 422 (#39 reopen)."""
    cases = [
        ("missing entry", {"schema_version": 1,
                           "bundle": {"digest": "a" * 64, "bytes": 1, "files": 1}}),
        ("wrong entry", {"schema_version": 1, "entry": "OTHER.md",
                         "bundle": {"digest": "a" * 64, "bytes": 1, "files": 1}}),
        ("missing bundle", {"schema_version": 1, "entry": "SKILL.md"}),
        ("extra content field", {**_SKILL_C, "notes": "x"}),
        ("digest not hex", {**_SKILL_C, "bundle": {"digest": "zz", "bytes": 1, "files": 1}}),
        ("bytes not positive", {**_SKILL_C, "bundle": {"digest": "a" * 64, "bytes": 0, "files": 1}}),
        ("files missing", {**_SKILL_C, "bundle": {"digest": "a" * 64, "bytes": 1}}),
    ]
    for label, content in cases:
        resp = client.post("/v1/versions", json={
            "kind": "skill", "name": "demo", "version": "v1", "content": content})
        assert resp.status_code == 422, label
    for label, assets in [
        ("assets missing", []),
        ("assets digest mismatch", [{"name": "bundle", "digest": "b" * 64}]),
        ("two assets", [{"name": "bundle", "digest": "a" * 64}] * 2),
        ("asset wrong name", [{"name": "other", "digest": "a" * 64}]),
    ]:
        resp = client.post("/v1/versions", json={
            "kind": "skill", "name": "demo", "version": "v1",
            "content": _SKILL_C, "assets": assets})
        assert resp.status_code == 422, label
    for label, name, version in [
        ("invalid name", "Bad Name", "v1"), ("invalid version", "demo", "v 1")]:
        resp = client.post("/v1/versions", json={
            "kind": "skill", "name": name, "version": version,
            "content": _SKILL_C, "assets": [{"name": "bundle", "digest": "a" * 64}]})
        assert resp.status_code == 422, label
    # control: the conforming shape registers, and re-POST is idempotent
    register("skill", "demo", "v1", _SKILL_C,
             assets=[{"name": "bundle", "digest": "a" * 64}], expect=(201,))
    register("skill", "demo", "v1", _SKILL_C,
             assets=[{"name": "bundle", "digest": "a" * 64}], expect=(200,))


def test_config_rejects_duplicate_skill_names(client, register):
    """Same-name duplicates — same version or different versions — fail at
    config registration: one declared skill = one unique mount path."""
    register("skill", "dup", "v1", _SKILL_C,
             assets=[{"name": "bundle", "digest": "a" * 64}])
    register("skill", "dup", "v2", {**_SKILL_C, "bundle": {"digest": "b" * 64, "bytes": 2, "files": 1}},
             assets=[{"name": "bundle", "digest": "b" * 64}])
    register("skill", "other", "v1", {**_SKILL_C, "bundle": {"digest": "c" * 64, "bytes": 3, "files": 1}},
             assets=[{"name": "bundle", "digest": "c" * 64}])
    for label, refs in [
        ("same version twice", [{"name": "dup", "version": "v1"},
                                {"name": "dup", "version": "v1"}]),
        ("different versions", [{"name": "dup", "version": "v1"},
                                {"name": "dup", "version": "v2"}])]:
        resp = client.post("/v1/versions", json={
            "kind": "config", "name": "pi-dup", "version": "v1",
            "content": make_profile(refs)})
        assert resp.status_code == 422, label
    # distinct names, order preserved: still legal
    register("config", "pi-ok", "v1", make_profile(
        [{"name": "dup", "version": "v2"}, {"name": "other", "version": "v1"}]))
