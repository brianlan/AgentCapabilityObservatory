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
- Every trial termination is funnelled through finish_trial (ADR 0003):
  submit, exit, timeout, cancel, recovery — first legal trigger wins, and
  the Experiment completes itself once all its trials are terminal.

Trial state machine: planned -> claimed -> running -> sealed | anomaly |
cancelled. Experiment state machine: planned -> running -> paused |
completed | cancelled. Scoring state stays independent.

All decisions land in the append-only lifecycle_events table.
"""

import json
import os
import signal
import sqlite3
from datetime import datetime, timedelta

from . import artifacts, runs
from .db import utcnow

TRIAL_TERMINAL = ("sealed", "anomaly", "cancelled")


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


def finish_trial(conn: sqlite3.Connection, trial_id: str, trigger: str, outcome: str,
                 run_id: str | None = None, detail: str | None = None) -> bool:
    """The single funnel to a terminal trial state (ADR 0003).

    Sealing itself stays with the caller (only the supervisor owns the
    container); this owns the state transition, the append-only event with
    the winning trigger, and Experiment completion. First legal termination
    wins: a trial already in a terminal state is never re-finished, so a
    losing trigger can neither rewrite history nor resurrect the trial.
    """
    if outcome not in TRIAL_TERMINAL:
        raise ValueError(f"invalid trial outcome: {outcome!r}")
    cur = conn.execute(
        "UPDATE trials SET status = ? WHERE id = ? AND status NOT IN ('sealed', 'anomaly', 'cancelled')",
        (outcome, trial_id),
    )
    if cur.rowcount == 0:
        return False  # already terminal: the losing trigger is a recorded no-op
    experiment_id = conn.execute(
        "SELECT experiment_id FROM trials WHERE id = ?", (trial_id,)
    ).fetchone()["experiment_id"]
    if outcome == "sealed":
        # Keep the terminal state and its first scoring request atomic. A
        # crash after sealing but before this funnel is repaired by manager
        # startup reconciliation.
        from .verification import enqueue_default_verification
        enqueue_default_verification(conn, trial_id)
    _record_event(conn, experiment_id, trial_id, "trial_finished", reason=trigger,
                  outcome=outcome, run_id=run_id, detail=detail)
    complete_experiment_if_done(conn, experiment_id)
    conn.commit()
    return True


def complete_experiment_if_done(conn: sqlite3.Connection, experiment_id: str) -> bool:
    """All trials terminal -> the Experiment is completed. Cancelled
    experiments stay cancelled (terminal, never completed over it)."""
    open_trials = conn.execute(
        "SELECT COUNT(*) FROM trials WHERE experiment_id = ?"
        " AND status NOT IN ('sealed', 'anomaly', 'cancelled')", (experiment_id,),
    ).fetchone()[0]
    if open_trials:
        return False
    cur = conn.execute(
        "UPDATE experiments SET status = 'completed' WHERE id = ?"
        " AND status IN ('planned', 'running', 'paused')", (experiment_id,),
    )
    if cur.rowcount == 0:
        return False
    _record_event(conn, experiment_id, None, "completed", reason="all_trials_terminal")
    return True


def mark_trial_running(conn: sqlite3.Connection, trial_id: str) -> None:
    """claimed -> running when the agent actually started (#16 state machine)."""
    conn.execute("UPDATE trials SET status = 'running' WHERE id = ? AND status = 'claimed'",
                 (trial_id,))


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

    # 0. a crash between sealing and the state write can leave a terminal
    # answer on a non-terminal trial: finish it through the funnel first —
    # cancel never disqualifies an existing official answer
    for row in conn.execute(
        "SELECT s.trial_id, s.status FROM sealed_answers s JOIN trials t ON t.id = s.trial_id"
        " WHERE t.experiment_id = ? AND s.status IN ('sealed', 'anomaly')"
        " AND t.status NOT IN ('sealed', 'anomaly', 'cancelled')", (experiment_id,),
    ).fetchall():
        finish_trial(conn, row["trial_id"], "explicit_cancel", row["status"])

    # untouched: trials with an outcome this cancel neither starts nor
    # changes — already sealed, anomalous, or cancelled
    summary["untouched"] = conn.execute(
        "SELECT COUNT(*) FROM trials t WHERE t.experiment_id = ?"
        " AND t.status IN ('sealed', 'anomaly', 'cancelled')"
        " AND t.id NOT IN (SELECT r.trial_id FROM trial_runs r"
        "                   WHERE r.status IN ('launching', 'running'))",
        (experiment_id,),
    ).fetchone()[0]

    # 1. trials that never produced a launch intent (planned, or claimed but
    # crashed-before-spawn) -> cancelled: they can never run again
    cur = conn.execute(
        "UPDATE trials SET status = 'cancelled' WHERE experiment_id = ? AND status = 'planned'",
        (experiment_id,),
    )
    summary["cancelled"] = cur.rowcount
    cur = conn.execute(
        "UPDATE trials SET status = 'cancelled' WHERE experiment_id = ? AND status = 'claimed'"
        " AND id NOT IN (SELECT trial_id FROM trial_runs)",
        (experiment_id,),
    )
    summary["cancelled"] += cur.rowcount

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
    # only signal a process the recorded identity still vouches for (#16
    # reopen): a reused PID must never receive our SIGTERM
    if runs.supervisor_matches(run):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass  # already gone
    else:
        runs.add_phase(conn, run["run_id"], "supervisor_identity_unverified",
                       pid=pid, recorded_start=run["supervisor_start"])
    from .supervisor import cleanup_container
    cleanup_container(run["run_id"])
    sealed = conn.execute(
        "SELECT status FROM sealed_answers WHERE trial_id = ?", (trial_id,)
    ).fetchone()
    if sealed is None:
        artifacts.mark_anomaly(conn, trial_id, run["run_id"],
                               "cancelled before the answer was sealed", trigger="cancel")
    # the terminal trial state goes through the same funnel as every other
    # trigger: a sealed answer keeps its official eligibility (outcome
    # sealed); a never-sealed run becomes a cancelled diagnostic (outcome
    # cancelled). Either way the run stops as a diagnostic, the receipt
    # stays untouched.
    runs.finish_run(conn, run["run_id"], "error", runs.EXIT_CANCELLED,
                    "cancelled by explicit experiment cancel")
    outcome = sealed["status"] if sealed is not None and sealed["status"] == "sealed" else "cancelled"
    finish_trial(conn, trial_id, "explicit_cancel", outcome, run_id=run["run_id"],
                 detail="cancelled by explicit experiment cancel")
    experiment_id = conn.execute(
        "SELECT experiment_id FROM trials WHERE id = ?", (trial_id,)
    ).fetchone()["experiment_id"]
    _record_event(conn, experiment_id, trial_id, "stopped", reason="explicit_cancel",
                  run_id=run["run_id"], sealed=sealed["status"] if sealed else None)


def resume_experiment(conn: sqlite3.Connection, experiment_id: str) -> tuple[int, dict]:
    """Resume a restart-paused plan: paused -> running. Cancelled experiments
    stay cancelled — an explicit cancel can never be resumed away."""
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
    conn.execute("UPDATE experiments SET status = 'running' WHERE id = ? AND status = 'paused'",
                 (experiment_id,))
    resumed = conn.execute(
        "SELECT COUNT(*) FROM trials WHERE experiment_id = ? AND status = 'planned'",
        (experiment_id,),
    ).fetchone()[0]
    _record_event(conn, experiment_id, None, "resumed", reason="explicit_resume",
                  resumed=resumed)
    conn.commit()
    return 200, {"status": "running", "resumed": resumed}


def pause_unstarted_on_restart(conn: sqlite3.Connection) -> int:
    """Manager-startup: hold unstarted plans until an explicit resume.

    Planned and running experiments that still have planned (never
    attempted) trials are paused; trials are left untouched — the claim
    filter stops the manager from starting them while paused.
    """
    paused = 0
    rows = conn.execute(
        "SELECT DISTINCT e.id FROM experiments e JOIN trials t ON t.experiment_id = e.id"
        " WHERE e.status IN ('planned', 'running') AND t.status = 'planned'"
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
    neither restarts them nor treats them as lost. Identity-aware (#16
    reopen): only a run whose recorded (pid, start tick) still matches
    /proc is adopted — a reused PID is never adopted (recovery terminalizes
    it instead)."""
    adopted = 0
    for run in runs.unfinished_runs(conn):
        if not runs.supervisor_matches(run):
            continue
        pid = run["supervisor_pid"]
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
    """Batch-progress counts: plan, execution, cancellation, and anomaly
    coverage. `attempted` counts every trial that ever produced a launch
    intent (a trial_runs row) — a trial cancelled before launch was never
    attempted."""
    counts = {"planned": 0, "claimed": 0, "running": 0,
              "sealed": 0, "anomaly": 0, "cancelled": 0}
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
    attempted = conn.execute(
        "SELECT COUNT(*) FROM trials t JOIN trial_runs r ON r.trial_id = t.id"
        " WHERE t.experiment_id = ?",
        (experiment_id,),
    ).fetchone()[0]
    from .verification import default_verification_progress
    return {**counts, "sealed": sealed, "anomaly": anomaly,
            "attempted": attempted, **default_verification_progress(conn, experiment_id)}
