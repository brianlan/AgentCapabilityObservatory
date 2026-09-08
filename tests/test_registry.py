import hashlib
import json
import sqlite3

from aco.app import canonical


def test_register_task_computes_content_digest(client, register):
    content = {"prompt": "Solve 2+2", "answer": "4"}
    resp = register("task", "arith", "v1", content)
    assert resp.status_code == 201
    body = resp.json()
    expected = hashlib.sha256(canonical(
        {"kind": "task", "name": "arith", "version": "v1", "content": content, "assets": []}
    ).encode()).hexdigest()
    assert body["id"] == expected


def test_register_idempotent_for_same_content(client, register):
    first = register("scorer", "exact-match", "v1", {"matcher": "exact"})
    second = register("scorer", "exact-match", "v1", {"matcher": "exact"})
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_register_conflict_on_same_identity_different_content(client, register):
    register("task", "arith", "v1", {"prompt": "a"})
    resp = register("task", "arith", "v1", {"prompt": "b"}, expect=(409,))
    assert resp.json()["error"]["code"] == "version_conflict"


def test_register_suite_with_missing_task_reference(client, register):
    resp = register("suite", "math-pack", "v1",
                    {"tasks": [{"name": "ghost", "version": "v1"}]}, expect=(422,))
    assert resp.json()["error"]["code"] == "version_not_found"


def test_config_rejects_credential_values(client, register):
    # extra=forbid: value-bearing fields never enter the database.
    content = {"harness": "opencode", "model": "gpt-x", "credentials_value": "sk-secret"}
    resp = register("config", "codex-main", "v1", content, expect=(422,))
    assert resp.json()["error"]["code"] == "invalid_content"


def test_config_stores_only_credential_references(client, tmp_path, register):
    content = {"harness": "opencode", "model": "gpt-x",
               "credentials": ["openai-prod-key", "proxy-token"]}
    body = register("config", "codex-main", "v1", content).json()
    assert body["content"]["credentials"] == ["openai-prod-key", "proxy-token"]
    # no credential value exists anywhere in the database file
    db_path = None
    # the client's app used tmp_path via fixture; find its data root db
    # (fixture-scoped tmp_path is the data root)
    for candidate in tmp_path.rglob("*.db"):
        db_path = candidate
    assert db_path is not None
    raw = sqlite3.connect(db_path).execute("SELECT content FROM versions").fetchall()
    assert all("sk-" not in row[0] and "secret" not in row[0] for row in raw)
