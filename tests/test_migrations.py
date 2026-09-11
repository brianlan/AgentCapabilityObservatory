import sqlite3

from aco import db


def test_migrate_from_empty_database(tmp_path):
    conn = db.connect(tmp_path / "aco.db")
    db.migrate(conn)
    versions = [row[0] for row in conn.execute("SELECT version FROM schema_version ORDER BY version")]
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"versions", "experiments", "trials", "submissions", "sealed_answers",
            "verifications", "verification_attempts", "management_idempotency",
            "schema_version"} <= tables
    # 0007 (#12 reopen): submit is intent-only — no session-supplied answer column
    columns = {row[1] for row in conn.execute("PRAGMA table_info(submissions)")}
    assert "answer" not in columns
    # 0009 (#35): server-side idempotency is keyed per management principal
    key_cols = {row[1] for row in conn.execute("PRAGMA table_info(management_idempotency)")}
    assert {"principal", "idempotency_key", "request_digest", "experiment_id", "created_at"} <= key_cols
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(management_idempotency)")}
    assert "sqlite_autoindex_management_idempotency_1" in indexes  # UNIQUE(principal, idempotency_key)
    # 0010 (#16 reopen): seal_trigger covers the full finish_trial trigger vocabulary
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sealed_answers'").fetchone()[0]
    for trigger in ("submit", "exit", "timeout", "cancel",
                    "supervisor_lost", "supervisor_crash",
                    "contract_invalid", "unsupported_target", "recovery"):
        assert f"'{trigger}'" in sql


def test_migrate_rerun_preserves_data(tmp_path):
    conn = db.connect(tmp_path / "aco.db")
    db.migrate(conn)
    conn.execute(
        "INSERT INTO versions (id, kind, name, version, content, created_at)"
        " VALUES ('d1', 'task', 't', 'v1', '{}', 'now')"
    )
    conn.commit()
    db.migrate(conn)  # second run must be a no-op
    assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1
    assert [row[0] for row in conn.execute("SELECT version FROM schema_version")] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]


# --- concurrent first-start + explicit-root policy (#62) ---

_EXPECTED = list(range(1, 16))


def test_concurrent_first_start_migrations_both_succeed(tmp_path):
    """Two processes migrating the same fresh data root simultaneously: both
    succeed, each schema_version row exactly once (#62). Before the
    BEGIN IMMEDIATE + insert-first fix, the loser died at import with
    `UNIQUE constraint failed: schema_version.version`."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    src = Path(db.__file__).resolve().parent.parent  # .../src
    env = {**os.environ, "PYTHONPATH": str(src), "ACO_DATA_ROOT": str(tmp_path)}
    # rendezvous: bare Popen racing is usually serialized by interpreter
    # startup — both processes must be alive before either migrates
    code = (
        "import os, time\n"
        "from aco.app import create_management_app\n"
        "root = os.environ['ACO_DATA_ROOT']\n"
        "open(os.path.join(root, 'ready-' + os.environ['WORKER_I']), 'w').close()\n"
        "for _ in range(500):\n"
        "    if (os.path.exists(os.path.join(root, 'ready-0'))\n"
        "            and os.path.exists(os.path.join(root, 'ready-1'))):\n"
        "        break\n"
        "    time.sleep(0.02)\n"
        "create_management_app()\n"
    )
    procs = [subprocess.Popen(
        [sys.executable, "-c", code],
        env={**env, "WORKER_I": str(i)}, cwd=tmp_path) for i in range(2)]
    for proc in procs:
        assert proc.wait(timeout=60) == 0, "concurrent first-start crashed"
    conn = db.connect(tmp_path / "aco.db")
    versions = [row[0] for row in conn.execute(
        "SELECT version FROM schema_version ORDER BY version")]
    assert versions == _EXPECTED


def test_import_has_no_filesystem_side_effects(tmp_path):
    """Importing aco.* modules (tests, supervisor's fetch_version, cli)
    must not create or migrate a CWD-relative ./data root (#62)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    src = Path(db.__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(src)}
    env.pop("ACO_DATA_ROOT", None)
    proc = subprocess.run(
        [sys.executable, "-c",
         "import aco.app, aco.supervisor, aco.execution, aco.cli"],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "data").exists()


def test_missing_root_fails_fast(monkeypatch):
    """No data_root argument and no ACO_DATA_ROOT: refuse to start instead of
    silently creating ./data (#62)."""
    import pytest

    from aco.app import create_management_app, create_session_app
    monkeypatch.delenv("ACO_DATA_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="ACO_DATA_ROOT"):
        create_management_app(token="t")
    with pytest.raises(RuntimeError, match="ACO_DATA_ROOT"):
        create_session_app()
