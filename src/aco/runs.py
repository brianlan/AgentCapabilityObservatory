"""Trial-run records: launch intent, observations, and diagnostics (#13).

Shared by the execution manager, the supervisor, and the read-only API
endpoint. Request-side config is written once at launch; observation columns
stay NULL until actually observed.
"""

import json
import sqlite3
import uuid

from .db import utcnow

# terminal exit kinds are diagnostics, not scores; harbor's own outcome is raw
EXIT_NORMAL = "normal"
EXIT_SUBMIT = "submit"
EXIT_AGENT_ERROR = "agent_error"
EXIT_TIMEOUT = "timeout"
EXIT_SUPERVISOR_LOST = "supervisor_lost"
EXIT_UNSUPPORTED_TARGET = "unsupported_target"
EXIT_CONTRACT_INVALID = "contract_invalid"
EXIT_CANCELLED = "cancelled"


def new_run_id() -> str:
    return uuid.uuid4().hex


def create_run(
    conn: sqlite3.Connection,
    trial_id: str,
    requested_profile: dict,
    supervisor_pid: int,
) -> str:
    """Persist the launch intent BEFORE any side effect (subprocess/container)."""
    run_id = new_run_id()
    conn.execute(
        "INSERT INTO trial_runs (run_id, trial_id, status, requested_profile,"
        " launched_at, supervisor_pid, created_at)"
        " VALUES (?, ?, 'launching', ?, ?, ?, ?)",
        (run_id, trial_id, json.dumps(requested_profile, sort_keys=True),
         utcnow(), supervisor_pid, utcnow()),
    )
    conn.commit()
    return run_id


def mark_running(
    conn: sqlite3.Connection,
    run_id: str,
    container_id: str | None = None,
    image: str | None = None,
) -> None:
    conn.execute(
        "UPDATE trial_runs SET status = 'running', container_id = COALESCE(?, container_id),"
        " image = COALESCE(?, image) WHERE run_id = ?",
        (container_id, image, run_id),
    )
    conn.commit()


def add_phase(conn: sqlite3.Connection, run_id: str, event: str, **detail) -> dict:
    row = conn.execute("SELECT phases FROM trial_runs WHERE run_id = ?", (run_id,)).fetchone()
    phases = json.loads(row["phases"]) if row["phases"] else []
    entry = {"event": event, "at": utcnow(), **detail}
    phases.append(entry)
    conn.execute("UPDATE trial_runs SET phases = ? WHERE run_id = ?",
                 (json.dumps(phases), run_id))
    conn.commit()
    return entry


def observe_run(
    conn: sqlite3.Connection,
    run_id: str,
    adapter_version: str,
    harbor_version: str,
    log_dir: str,
) -> None:
    conn.execute(
        "UPDATE trial_runs SET adapter_version = ?, harbor_version = ?, log_dir = ?"
        " WHERE run_id = ?",
        (adapter_version, harbor_version, log_dir, run_id),
    )
    conn.commit()


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: str,
    exit_kind: str,
    exit_detail: str | None = None,
) -> bool:
    """Record the terminal outcome. First legal termination wins (#16): a
    run already finished or errored is never rewritten by a later
    finisher (e.g. a supervisor's timeout landing after a cancel)."""
    cur = conn.execute(
        "UPDATE trial_runs SET status = ?, exit_kind = ?, exit_detail = ?, finished_at = ?"
        " WHERE run_id = ? AND status IN ('launching', 'running')",
        (status, exit_kind, exit_detail, utcnow(), run_id),
    )
    conn.commit()
    return cur.rowcount > 0


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM trial_runs WHERE run_id = ?", (run_id,)).fetchone()


def list_runs(conn: sqlite3.Connection, trial_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM trial_runs WHERE trial_id = ? ORDER BY launched_at", (trial_id,)
    ).fetchall()


def unfinished_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM trial_runs WHERE status IN ('launching', 'running')"
    ).fetchall()


def run_out(row: sqlite3.Row) -> dict:
    return {
        "run_id": row["run_id"],
        "trial_id": row["trial_id"],
        "status": row["status"],
        "requested_profile": json.loads(row["requested_profile"]),
        "launched_at": row["launched_at"],
        "supervisor_pid": row["supervisor_pid"],
        "adapter_version": row["adapter_version"],
        "harbor_version": row["harbor_version"],
        "container_id": row["container_id"],
        "image": row["image"],
        "phases": json.loads(row["phases"]) if row["phases"] else [],
        "exit_kind": row["exit_kind"],
        "exit_detail": row["exit_detail"],
        "log_dir": row["log_dir"],
        "finished_at": row["finished_at"],
    }
