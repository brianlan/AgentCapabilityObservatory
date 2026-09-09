"""Experiment lifecycle: explicit cancel, resume, restart reconciliation (#16).

ACO never implicitly re-runs an attempted trial. The invariants this module
owns:

- Only an explicit cancel changes the plan; a stopped client changes nothing.
- cancel is persistent and idempotent: unstarted planned trials become
  cancelled, in-flight runs are stopped and their partial answers are kept as
  diagnostics (or stay sealed when the seal legitimately won the race), and
  finished or sealed trials are never retroactively disqualified.
- A service restart pauses unstarted plans until an explicit resume; live
  supervisors are adopted (monitored, never restarted); "record missing" is
  never read as "safe to rerun".
- Timeout answers carry a planned deadline, the actual freeze time, and a
  tolerance verdict; only within-tolerance answers stay eligible for the
  capability curve.

All decisions land in the append-only lifecycle_events table.
"""

import json
import os
import signal
import sqlite3
from datetime import datetime, timedelta

from . import artifacts, runs
from .db import utcnow


def _app_error(status: int, code: str, message: str):
    # lazy import: app.py has a module-level create_app() side effect, so it
    # must never be imported by the manager's import chain
    from .app import AppError
    return AppError(status, code, message)


def _record_event(conn: sqlite3.Connection, experiment_id: str, trial_id: str | None,
                  event: str, reason: str | None = None, **detail) -> None:
    conn.execute(
        "INSERT INTO lifecycle_events (experiment_id, trial_id, event, reason, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (experiment_id, trial_id, event, reason,
         json.dumps(detail, sort_keys=True) if detail else None, utcnow()),
    )


def _unfinished_runs(conn: sqlite3.Connection, experiment_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT r.* FROM trial_runs r JOIN trials t ON t.id = r.trial_id"
        " WHERE t.experiment_id = ? AND r.status IN ('launching', 'running')",
        (experiment_id,),
    ).fetchall()


def cancel_experiment(conn: sqlite3.Connection, experiment_id: str) -> tuple[int, dict]:
    """Explicit cancel. Returns (status_code, summary); idempotent.

    First legal termination wins: a trial whose answer was already sealed
    (submit/exit/timeout) stays sealed; cancel only claims trials that never
    produced an official answer.
    """
    experiment = conn.execute(
        "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    if experiment is None:
        raise _app_error(404, "not_found", f"experiment {experiment_id} does not exist")

    if experiment["status"] == "cancelled":
        previous = conn.execute(
            "SELECT detail FROM lifecycle_events WHERE experiment_id = ? AND event = 'cancelled'"
            " AND trial_id IS NULL ORDER BY id DESC LIMIT 1", (experiment_id,),
        ).fetchone()
        return 200, json.loads(previous["detail"]) if previous and previous["detail"] else {"status": "cancelled"}

    summary = {"status": "cancelled", "cancelled": 0, "stopped": 0, "untouched": 0}

    # untouched first: trials with an outcome this cancel neither starts nor
    # changes — already finished, sealed, or anomalous
    summary["untouched"] = conn.execute(
        "SELECT COUNT(*) FROM trials t WHERE t.experiment_id = ? AND t.status != 'planned'"
        " AND t.id NOT IN (SELECT r.trial_id FROM trial_runs r"
        "                   WHERE r.status IN ('launching', 'running'))",
        (experiment_id,),
    ).fetchone()[0]

    # 1. unstarted planned trials -> cancelled (they can never run again)
    cur = conn.execute(
        "UPDATE trials SET status = 'cancelled' WHERE experiment_id = ? AND status = 'planned'",
        (experiment_id,),
    )
    summary["cancelled"] = cur.rowcount

    # 2. in-flight runs: stop the supervisor, remove the agent container, and
    # keep whatever answer state exists — sealed stays sealed, everything else
    # becomes a diagnostic anomaly, never a capability sample.
    for run in _unfinished_runs(conn, experiment_id):
        _stop_run(conn, run)
        summary["stopped"] += 1

    conn.execute("UPDATE experiments SET status = 'cancelled' WHERE id = ?", (experiment_id,))
    _record_event(conn, experiment_id, None, "cancelled", reason="explicit_cancel", **summary)
    conn.commit()
    return 200, summary


def _stop_run(conn: sqlite3.Connection, run: sqlite3.Row) -> None:
    """Stop one in-flight run and leave an explainable diagnostic behind.

    The seal exactly-once guard decides the outcome: if the supervisor's own
    seal already registered, the official answer stands; otherwise the trial
    gets a cancelled anomaly (diagnostics only, never the capability curve).
    """
    trial_id = run["trial_id"]
    pid = run["supervisor_pid"]
    # never signal placeholders: pid -1/0 would broadcast to a whole process group
    if pid is not None and pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass  # already gone
    from .supervisor import cleanup_container
    cleanup_container(run["run_id"])
    sealed = conn.execute(
        "SELECT status FROM sealed_answers WHERE trial_id = ?", (trial_id,)
    ).fetchone()
    if sealed is None:
        artifacts.mark_anomaly(conn, trial_id, run["run_id"],
                               "cancelled before the answer was sealed", trigger="exit")
        conn.execute("UPDATE trials SET status = 'cancelled' WHERE id = ?", (trial_id,))
    # a sealed answer keeps its official eligibility: the run stops as a
    # diagnostic, the trial's status and receipt stay untouched
    runs.finish_run(conn, run["run_id"], "error", runs.EXIT_CANCELLED,
                    "cancelled by explicit experiment cancel")
    experiment_id = conn.execute(
        "SELECT experiment_id FROM trials WHERE id = ?", (trial_id,)
    ).fetchone()["experiment_id"]
    _record_event(conn, experiment_id, trial_id, "stopped", reason="explicit_cancel",
                  run_id=run["run_id"], sealed=sealed["status"] if sealed else None)


def resume_experiment(conn: sqlite3.Connection, experiment_id: str) -> tuple[int, dict]:
    """Resume a restart-paused plan. Cancelled experiments stay cancelled."""
    experiment = conn.execute(
        "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    if experiment is None:
        raise _app_error(404, "not_found", f"experiment {experiment_id} does not exist")
    if experiment["status"] == "cancelled":
        raise _app_error(409, "experiment_cancelled",
                         "an explicitly cancelled experiment cannot be resumed")
    if experiment["status"] != "paused":
        # idempotent no-op: nothing was paused, nothing to resume
        return 200, {"status": experiment["status"], "resumed": 0}
    conn.execute("UPDATE experiments SET status = 'planned' WHERE id = ? AND status = 'paused'",
                 (experiment_id,))
    resumed = conn.execute(
        "SELECT COUNT(*) FROM trials WHERE experiment_id = ? AND status = 'planned'",
        (experiment_id,),
    ).fetchone()[0]
    _record_event(conn, experiment_id, None, "resumed", reason="explicit_resume",
                  resumed=resumed)
    conn.commit()
    return 200, {"status": "planned", "resumed": resumed}


def pause_unstarted_on_restart(conn: sqlite3.Connection) -> int:
    """Manager-startup: hold unstarted plans until an explicit resume.

    Only experiments that still have planned (never attempted) trials are
    paused; trials are left untouched — the claim filter stops the manager
    from starting them while paused.
    """
    paused = 0
    rows = conn.execute(
        "SELECT DISTINCT e.id FROM experiments e JOIN trials t ON t.experiment_id = e.id"
        " WHERE e.status = 'planned' AND t.status = 'planned'"
    ).fetchall()
    for row in rows:
        conn.execute("UPDATE experiments SET status = 'paused' WHERE id = ?", (row["id"],))
        _record_event(conn, row["id"], None, "restart_paused",
                      reason="manager_restart_holds_unstarted_plans")
        paused += 1
    conn.commit()
    return paused


def adopt_live_supervisors(conn: sqlite3.Connection) -> int:
    """Manager-startup: record adoption of supervisors that survived the
    restart and keep running. They record their own outcomes; the manager
    neither restarts them nor treats them as lost."""
    adopted = 0
    for run in runs.unfinished_runs(conn):
        pid = run["supervisor_pid"]
        if pid is None or pid <= 0 or not os.path.exists(f"/proc/{pid}"):
            continue
        adopted += 1
        trial = conn.execute(
            "SELECT experiment_id FROM trials WHERE id = ?", (run["trial_id"],)
        ).fetchone()
        runs.add_phase(conn, run["run_id"], "supervisor_adopted", pid=pid)
        _record_event(conn, trial["experiment_id"], run["trial_id"], "adopted",
                      reason="supervisor_survived_restart", run_id=run["run_id"], pid=pid)
    conn.commit()
    return adopted


def record_timeout_verdict(conn: sqlite3.Connection, experiment_id: str, trial_id: str,
                           run_id: str, deadline: datetime, tolerance: timedelta) -> str:
    """Tolerance-policy verdict for a timeout-sealed answer (#FR-timeout-sealing).

    Records the planned deadline, the actual freeze time, and the verdict.
    Within tolerance: the answer stays sealed and eligible for scoring. Over
    tolerance: the answer keeps its content and receipt but becomes an
    anomaly — a diagnostic, never a capability-curve sample.
    """
    row = conn.execute(
        "SELECT frozen_at, status FROM sealed_answers WHERE trial_id = ?", (trial_id,)
    ).fetchone()
    if row is None or not row["frozen_at"]:
        return "no_answer"
    frozen = datetime.fromisoformat(row["frozen_at"])
    overrun = frozen - deadline
    verdict = "within_tolerance" if overrun <= tolerance else "over_tolerance"
    detail = {"planned_deadline": deadline.isoformat(), "frozen_at": row["frozen_at"],
              "overrun_sec": overrun.total_seconds(), "verdict": verdict}
    if verdict == "over_tolerance" and row["status"] == "sealed":
        conn.execute(
            "UPDATE sealed_answers SET status = 'anomaly', anomaly = ? WHERE trial_id = ?",
            (f"answer frozen {overrun.total_seconds():.1f}s past the planned deadline"
             f" (tolerance {tolerance.total_seconds():.0f}s)", trial_id),
        )
    _record_event(conn, experiment_id, trial_id, "timeout_verdict",
                  reason=verdict, run_id=run_id, **detail)
    conn.commit()
    return verdict


def progress(conn: sqlite3.Connection, experiment_id: str) -> dict:
    """Batch-progress counts: plan, cancellation, and anomaly coverage."""
    counts = {"planned": 0, "claimed": 0, "cancelled": 0}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM trials WHERE experiment_id = ? GROUP BY status",
        (experiment_id,),
    ):
        counts[row["status"]] = row["n"]
    answers = conn.execute(
        "SELECT COALESCE(s.status, 'none') AS st, COUNT(*) AS n FROM trials t"
        " LEFT JOIN sealed_answers s ON s.trial_id = t.id"
        " WHERE t.experiment_id = ? GROUP BY st",
        (experiment_id,),
    ).fetchall()
    sealed = sum(r["n"] for r in answers if r["st"] == "sealed")
    anomaly = sum(r["n"] for r in answers if r["st"] == "anomaly")
    return {**counts, "sealed": sealed, "anomaly": anomaly,
            "attempted": counts["claimed"]}
