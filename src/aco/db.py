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
        # The schema_version INSERT runs BEFORE the migration SQL inside
        # BEGIN IMMEDIATE, so the UNIQUE(version) constraint is the
        # serialization point of concurrent first-start migrations (#62):
        # the loser blocks on the write lock (busy_timeout), then its INSERT
        # fails with IntegrityError and it skips — the migration SQL itself
        # never re-runs on an already-migrated database.
        # ponytail: single atomic script per migration; split files only if a
        # migration ever needs to be split.
        script = (
            f"BEGIN IMMEDIATE;\n"
            f"INSERT INTO schema_version (version, applied_at) VALUES ({version!r}, '{utcnow()}');\n"
            f"{path.read_text()}\n"
            f"COMMIT;"
        )
        # a table-rebuild migration (0013) must run with FK enforcement off;
        # PRAGMAs are no-ops inside a transaction, so they wrap the script
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            try:
                conn.executescript(script)
            except sqlite3.IntegrityError:
                # another process applied this version first (#62)
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
