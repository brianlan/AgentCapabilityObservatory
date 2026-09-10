import sqlite3

from aco import db


def test_migrate_from_empty_database(tmp_path):
    conn = db.connect(tmp_path / "aco.db")
    db.migrate(conn)
    versions = [row[0] for row in conn.execute("SELECT version FROM schema_version ORDER BY version")]
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8]
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"versions", "experiments", "trials", "submissions", "sealed_answers",
            "verifications", "verification_attempts", "schema_version"} <= tables
    # 0007 (#12 reopen): submit is intent-only — no session-supplied answer column
    columns = {row[1] for row in conn.execute("PRAGMA table_info(submissions)")}
    assert "answer" not in columns


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
    assert [row[0] for row in conn.execute("SELECT version FROM schema_version")] == [1, 2, 3, 4, 5, 6, 7, 8]
