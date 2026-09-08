"""SQLite connection and versioned migration runner."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    # ponytail: check_same_thread=False + async endpoints keeps all access on
    # one thread; add a lock/connection pool only if sync endpoints ever land.
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # manager, supervisor, and API share the database file across processes
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations in order; re-running is a no-op."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_version")}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version in applied:
            continue
        # ponytail: single atomic script per migration; split files only if a
        # migration ever needs to be split.
        script = (
            f"BEGIN;\n{path.read_text()}\n"
            f"INSERT INTO schema_version (version, applied_at) VALUES ({version!r}, '{utcnow()}');\nCOMMIT;"
        )
        conn.executescript(script)
