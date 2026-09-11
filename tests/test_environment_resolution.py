"""Environment store and instruction single-source tests (#20 reopen).

The registered TaskVersion must be rebuildable from the data root alone:
the content-addressed environment store publishes verified bytes once,
the instruction has exactly one declared source, and a missing/tampered
environment fails as a pre-agent execution anomaly — never an empty
workspace or empty prompt.
"""

import asyncio
import json
import os
import shutil
import sqlite3
import uuid

import pytest

from aco import artifacts, db, environments, runs
from aco.environments import EnvironmentInvalid
from aco.supervisor import execute_run
from aco.verification import runner


README = "FAKE:submit\nimplement add(a, b) in solution.py\n"
ENV_FILES = {"workspace/README.md": README, "workspace/starter.py": "a, b = 2, 4\n"}


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "aco.db")
    db.migrate(conn)
    yield conn
    conn.close()


def make_env(tmp_path) -> str:
    """A source environment tree + its published store entry; returns digest."""
    src = tmp_path / "env-src"
    for rel, text in ENV_FILES.items():
        target = src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    digest = runner.bundle_digest(src)
    environments.publish(tmp_path, src, digest)
    return digest


def register_task_version(conn, digest, *, with_prompt=False):
    contract = artifacts.ArtifactContract(required_outputs=("/workspace/answer.txt",))
    content = {"contract": {"required_outputs": ["/workspace/answer.txt"]},
               "contract_digest": artifacts.contract_digest(contract)}
    if with_prompt:
        content["prompt"] = "inline prompt"
    else:
        content["instruction"] = {"asset": "environment", "path": "workspace/README.md"}
    assets = [{"name": "environment", "digest": digest}]
    conn.execute(
        "INSERT INTO versions (id, kind, name, version, content, assets, created_at)"
        " VALUES ('v-task', 'task', 'task', 'v1', ?, ?, 'now')",
        (json.dumps(content), json.dumps(assets)))
    conn.execute(
        "INSERT INTO versions (id, kind, name, version, content, created_at)"
        " VALUES ('v-cfg', 'config', 'cfg', 'v1', '{\"harness\": \"fake\", \"model\": \"none\"}', 'now')")
    conn.execute(
        "INSERT INTO experiments (id, status, requested, created_at)"
        " VALUES ('e1', 'planned', '{}', 'now')")
    conn.execute(
        "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
        " repetition, plan_order, requested) VALUES ('t1', 'e1', 'v-task', 'v-cfg', 1, 1, '{}')")
    conn.commit()


def start_run(conn) -> sqlite3.Row:
    run_id = runs.create_run(conn, "t1", {}, supervisor_pid=os.getpid())
    conn.commit()
    return conn.execute("SELECT * FROM trial_runs WHERE run_id = ?", (run_id,)).fetchone()


def test_publish_is_idempotent_and_verifies(tmp_path):
    src = tmp_path / "env-src"
    (src / "workspace").mkdir(parents=True)
    (src / "workspace" / "README.md").write_text(README)
    digest = runner.bundle_digest(src)
    first = environments.publish(tmp_path, src, digest)
    assert first == environments.store_root(tmp_path) / digest
    assert environments.publish(tmp_path, src, digest) == first  # no-op re-import


def test_publish_rejects_digest_mismatch(tmp_path):
    src = tmp_path / "env-src"
    (src / "workspace").mkdir(parents=True)
    (src / "workspace" / "README.md").write_text(README)
    with pytest.raises(EnvironmentInvalid):
        environments.publish(tmp_path, src, "0" * 64)


def test_resolve_rejects_missing_and_tampered_store(tmp_path):
    with pytest.raises(EnvironmentInvalid):
        environments.resolve(tmp_path, "1" * 64)  # missing store entry
    src = tmp_path / "env-src2"
    (src / "workspace").mkdir(parents=True)
    (src / "workspace" / "README.md").write_text("tampered")
    digest = runner.bundle_digest(src)
    environments.publish(tmp_path, src, digest)
    (environments.store_root(tmp_path) / digest / "workspace" / "README.md").write_text("tampered!")
    with pytest.raises(EnvironmentInvalid):
        environments.resolve(tmp_path, digest)  # tampered store entry


def test_instruction_single_source_rules(tmp_path):
    digest = make_env(tmp_path)
    assets = [{"name": "environment", "digest": digest}]
    # asset form resolves the registered file
    text, env_dir = environments.resolve_instruction(
        {"instruction": {"asset": "environment", "path": "workspace/README.md"}}, assets, tmp_path)
    assert text == README and env_dir == environments.store_root(tmp_path) / digest
    # prompt form stays the single source for synthetic versions
    assert environments.resolve_instruction({"prompt": "inline"}, [], tmp_path) == ("inline", None)
    for bad_content, bad_assets in (
        ({"prompt": "a", "instruction": {"asset": "environment", "path": "workspace/README.md"}}, assets),
        ({}, assets),
        ({"prompt": ""}, []),
        ({"instruction": {"asset": "environment", "path": "../escape"}}, assets),
        ({"instruction": {"asset": "other", "path": "workspace/README.md"}}, assets),
        ({"instruction": {"asset": "environment", "path": "workspace/missing.md"}}, assets),
    ):
        with pytest.raises(EnvironmentInvalid):
            environments.resolve_instruction(bad_content, bad_assets, tmp_path)


def test_tampered_store_fails_before_agent_start(tmp_path, conn):
    """Missing/tampered environment bytes must become a pre-agent execution
    anomaly — never an empty workspace/prompt fallback (#20 reopen)."""
    digest = make_env(tmp_path)
    register_task_version(conn, digest)
    (environments.store_root(tmp_path) / digest / "workspace" / "README.md").write_text("tampered!")
    run = start_run(conn)

    asyncio.run(execute_run(conn, run, tmp_path))

    row = conn.execute("SELECT status, exit_kind, exit_detail FROM trial_runs WHERE run_id = ?",
                       (run["run_id"],)).fetchone()
    assert row["status"] == "error" and row["exit_kind"] == "environment_invalid"
    assert "environment" in row["exit_detail"]
    anomaly = conn.execute("SELECT seal_trigger, status FROM sealed_answers WHERE trial_id = 't1'").fetchone()
    assert anomaly["seal_trigger"] == "environment_invalid" and anomaly["status"] == "anomaly"
    assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "anomaly"
    assert conn.execute("SELECT status FROM experiments WHERE id = 'e1'").fetchone()[0] == "completed"
    # no agent-facing phase was ever recorded
    phases = [p["event"] for p in json.loads(
        conn.execute("SELECT phases FROM trial_runs WHERE run_id = ?", (run["run_id"],)).fetchone()["phases"] or "[]")]
    assert "agent_start" not in phases


def test_missing_store_fails_before_agent_start(tmp_path, conn):
    digest = make_env(tmp_path)
    register_task_version(conn, digest)
    shutil.rmtree(environments.store_root(tmp_path) / digest)
    run = start_run(conn)

    asyncio.run(execute_run(conn, run, tmp_path))

    row = conn.execute("SELECT status, exit_kind FROM trial_runs WHERE run_id = ?",
                       (run["run_id"],)).fetchone()
    assert row["status"] == "error" and row["exit_kind"] == "environment_invalid"


def test_seal_trigger_check_accepts_new_pre_agent_families(tmp_path, conn):
    """Migration 0015: the pre-agent anomaly families #39 and #20 write must
    satisfy the sealed_answers CHECK (skill_mismatch was missing before)."""
    register_task_version(conn, make_env(tmp_path))
    conn.execute(
        "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
        " repetition, plan_order, requested) VALUES ('t2', 'e1', 'v-task', 'v-cfg', 2, 2, '{}')")
    run_ids = [runs.create_run(conn, trial_id, {}, supervisor_pid=os.getpid())
               for trial_id in ("t1", "t2")]
    for trial_id, run_id, trigger in (("t1", run_ids[0], "skill_mismatch"),
                                      ("t2", run_ids[1], "environment_invalid")):
        conn.execute(
            "INSERT INTO sealed_answers (trial_id, run_id, receipt_id, digest, manifest,"
            " seal_trigger, trigger_at, frozen_at, copied_at, published_at, registered_at,"
            " status, anomaly) VALUES (?, ?, ?, '', '{}', ?, 'now', 'now', 'now', 'now', 'now',"
            " 'anomaly', 'detail')", (trial_id, run_id, uuid.uuid4().hex, trigger))
    conn.commit()
