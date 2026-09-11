"""Local singleton execution manager with a default single slot (#13).

Claims planned trials from SQLite, persists the launch intent, spawns one
independent supervisor subprocess per run, and records diagnostics when a
supervisor dies. The FastAPI process never runs trials; this process never
serves requests.
"""

import argparse
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import artifacts, db, lifecycle, runs, verification
from .supervisor import TRIAL_GRACE_SEC, cleanup_container, translate_profile

POLL_INTERVAL_SEC = 1.0
SUPERVISOR_TIMEOUT_SEC = 300  # fallback ceiling for profiles that cannot be translated
# headroom above the supervisor's own trial ceiling for cold image pulls,
# harness and gateway startup (#16 reopen): the manager watchdog must never
# be the reason a slow-but-healthy preparation dies mid-pull
PREP_GRACE_SEC = 180


def supervisor_watchdog_sec(profile: dict) -> int:
    """Manager watchdog derived from the effective trial timeout (#16 reopen):
    the supervisor enforces agent timeout + TRIAL_GRACE itself; the manager
    waits that out plus the prep allowance. Untranslatable profiles keep the
    legacy ceiling — the supervisor fails them explicitly on its own."""
    try:
        _, agent_timeout_sec = translate_profile(profile)
    except Exception:  # noqa: BLE001 — any unusable profile falls back
        return SUPERVISOR_TIMEOUT_SEC
    return agent_timeout_sec + TRIAL_GRACE_SEC + PREP_GRACE_SEC


def mint_token(api_url: str, trial_id: str, token: str) -> str | None:
    """Trial-scoped token via the authenticated management API; None if the
    API is unreachable or rejects the mint (a finished trial can never run)."""
    try:
        request = urllib.request.Request(
            api_url.rstrip("/") + f"/v1/trials/{trial_id}/session-token", data=b"", method="POST",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())["token"]
    except Exception as exc:  # noqa: BLE001 — daemon retries on the next poll
        print(f"token mint for {trial_id} failed: {exc}", file=sys.stderr)
        return None


def claim_next_planned(conn) -> str | None:
    """Atomically move the oldest planned trial of an active experiment to
    claimed; single winner. Paused/cancelled/completed experiments never
    start work. First claim flips the experiment planned -> running."""
    row = conn.execute(
        "UPDATE trials SET status = 'claimed'"
        " WHERE id = (SELECT t.id FROM trials t JOIN experiments e ON e.id = t.experiment_id"
        "             WHERE t.status = 'planned' AND e.status IN ('planned', 'running')"
        "             ORDER BY t.rowid LIMIT 1)"
        " RETURNING id, experiment_id"
    ).fetchone()
    if row is None:
        conn.commit()
        return None
    conn.execute(
        "UPDATE experiments SET status = 'running' WHERE id = ? AND status = 'planned'",
        (row["experiment_id"],),
    )
    conn.commit()
    return row["id"]


def recover_stale_claims(conn) -> None:
    """Manager crash between claim and spawn leaves claimed trials without a
    run; reset only those. A trial with a launch intent (any trial_runs row,
    active or finished) is never reset — an attempted trial can never be
    silently re-answered (#16 reopen)."""
    conn.execute(
        "UPDATE trials SET status = 'planned' WHERE status = 'claimed' AND id NOT IN"
        " (SELECT trial_id FROM trial_runs)"
    )
    conn.commit()


def _terminalize_lost_run(conn, run: sqlite3.Row, trigger: str, detail: str) -> None:
    """One funnel for every manager-side supervisor loss (#16 reopen):
    watchdog kill, abnormal exit, lost supervisor, PID-reuse detection.
    Records the run outcome, cleans only our labelled container, leaves an
    execution-condition anomaly when no answer exists, and moves the trial
    through finish_trial — sealed stays sealed (ADR 0003), the Experiment
    becomes completable, and the trial can never be re-claimed."""
    runs.add_phase(conn, run["run_id"], "supervisor_lost", pid=run["supervisor_pid"],
                   trigger=trigger)
    runs.finish_run(conn, run["run_id"], "error", runs.EXIT_SUPERVISOR_LOST, detail)
    cleanup_container(run["run_id"])
    artifacts.mark_anomaly(conn, run["trial_id"], run["run_id"], detail, trigger=trigger)
    # the outcome follows the answer row, not the supervisor's death: a
    # seal recovered from disk truth stays officially sealed (ADR 0003)
    answer = conn.execute(
        "SELECT status FROM sealed_answers WHERE trial_id = ?", (run["trial_id"],)
    ).fetchone()
    outcome = answer["status"] if answer is not None and answer["status"] in ("sealed", "anomaly") else "anomaly"
    lifecycle.finish_trial(conn, run["trial_id"], trigger, outcome,
                           run_id=run["run_id"], detail=detail)


def reap_lost_supervisors(conn) -> None:
    """Supervisor died without recording an outcome: keep the intent, record
    the loss, clean up only containers carrying our label, and terminal the
    trial as an execution-condition anomaly — never a rerun, never a
    capability sample.

    Identity-aware (#16 reopen): a run is only considered alive when its
    recorded (pid, start tick) still matches /proc. A vanished process, a
    reused PID, or an unverifiable identity all mean our supervisor is gone
    — the run is terminalized with an explainable anomaly instead of
    lingering forever; the process at a reused PID is never touched."""
    for run in runs.unfinished_runs(conn):
        if runs.supervisor_matches(run):
            continue
        pid = run["supervisor_pid"]
        detail = (f"supervisor pid {pid} disappeared without recording an outcome"
                  if runs.proc_start_tick(pid) is None
                  else f"supervisor pid {pid} no longer matches the recorded process identity")
        runs.add_phase(conn, run["run_id"], "supervisor_unverified",
                       pid=pid, recorded_start=run["supervisor_start"])
        _terminalize_lost_run(conn, run, "supervisor_lost", detail)


def run_one(conn, root: Path, api_url: str, api_token: str, session_api_url: str) -> bool:
    """Execute at most one trial. Returns True if a trial was handled."""
    trial_id = claim_next_planned(conn)
    if trial_id is None:
        return False

    profile = json.loads(conn.execute(
        "SELECT content FROM versions WHERE id ="
        " (SELECT config_version_id FROM trials WHERE id = ?)", (trial_id,)
    ).fetchone()["content"])
    token = mint_token(api_url, trial_id, api_token)
    if token is None:
        # API unreachable or trial not runnable: unclaim and retry on a later
        # poll — but never resurrect a terminal state a concurrent cancel
        # already won (#16 reopen): only still-claimed trials of still-active
        # experiments may return to planned
        conn.execute(
            "UPDATE trials SET status = 'planned' WHERE id = ? AND status = 'claimed'"
            " AND experiment_id IN (SELECT id FROM experiments"
            "                        WHERE status IN ('planned', 'running'))",
            (trial_id,),
        )
        conn.commit()
        return False

    # ponytail: placeholder pid is replaced the moment Popen returns, so the
    # intent row always exists before any side effect; refine if a reap race
    # ever matters.
    run_id = runs.create_run(conn, trial_id, profile, supervisor_pid=-1)
    supervisor_log = (root / "runs" / run_id / "supervisor.log")
    supervisor_log.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(supervisor_log, "w")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "aco.supervisor", "--run-id", run_id, "--data-root", str(root)],
            env={
                **os.environ,
                # supervisors and their agents speak only to the Session surface;
                # the management surface stays out of the evaluated path (ADR 0001)
                "ACO_BASE_URL": session_api_url,
                "ACO_SESSION_TOKEN": token,
            },
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    except Exception as exc:  # noqa: BLE001 — Popen failure must leave a terminal state
        run = runs.get_run(conn, run_id)
        _terminalize_lost_run(conn, run, "supervisor_crash",
                              f"supervisor could not be spawned: {type(exc).__name__}: {exc}")
        return True
    finally:
        # the child owns its dup; the parent handle would leak one fd per trial
        log_fh.close()
    runs.record_supervisor(conn, run_id, proc.pid)

    deadline = time.monotonic() + supervisor_watchdog_sec(profile)
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    killed = False
    if proc.poll() is None:
        proc.kill()
        proc.wait()
        killed = True
    run = runs.get_run(conn, run_id)
    if run["status"] in ("launching", "running"):
        # supervisor exited (or was killed) without recording an outcome:
        # the same funnel as every other manager-side loss (#16 reopen)
        detail = (f"supervisor pid {proc.pid} exceeded the manager watchdog and was killed"
                  if killed else
                  f"supervisor pid {proc.pid} exited without recording an outcome"
                  f" (exit code {proc.returncode})")
        _terminalize_lost_run(conn, run, "supervisor_lost", detail)
    return True


def reconcile_terminal_trials(conn) -> int:
    """A crash between sealing and the trial-state write leaves a terminal
    answer on a non-terminal trial. Recovery trusts the answer rows (disk +
    database truth), never re-collects, and moves each such trial through
    the same finish_trial funnel (#16 reopen)."""
    reconciled = 0
    rows = conn.execute(
        "SELECT s.trial_id, s.status FROM sealed_answers s JOIN trials t ON t.id = s.trial_id"
        " WHERE s.status IN ('sealed', 'anomaly')"
        " AND t.status NOT IN ('sealed', 'anomaly', 'cancelled')"
    ).fetchall()
    for row in rows:
        if lifecycle.finish_trial(conn, row["trial_id"], "startup_recovery", row["status"]):
            reconciled += 1
    # experiments whose trials all finished across the crash still complete
    for row in conn.execute(
        "SELECT DISTINCT t.experiment_id AS eid FROM trials t"
        " JOIN experiments e ON e.id = t.experiment_id"
        " WHERE e.status IN ('planned', 'running', 'paused')"
        " AND t.status NOT IN ('planned', 'claimed', 'running')"
    ).fetchall():
        lifecycle.complete_experiment_if_done(conn, row["eid"])
    conn.commit()
    return reconciled


def startup_recovery(conn, root: Path) -> None:
    """One-time reconciliation at manager startup (#16), in order: stale
    claims (launch intent is authoritative), interrupted seals from disk
    truth (#14 — disk truth wins before supervisor reaping flags an anomaly),
    lost supervisors, terminal-answer reconciliation, adoption of live
    supervisors, requeue of stuck scoring, and pausing unstarted plans until
    explicit resume."""
    recover_stale_claims(conn)
    artifacts.recover(conn, root)
    reap_lost_supervisors(conn)
    reconcile_terminal_trials(conn)
    lifecycle.adopt_live_supervisors(conn)  # surviving supervisors keep running, never rerun (#16)
    verification.requeue_stuck_running(conn)  # scoring has no side effects; requeue is safe (#16)
    lifecycle.pause_unstarted_on_restart(conn)  # unstarted plans wait for explicit resume (#16)


def main() -> int:
    parser = argparse.ArgumentParser(prog="aco-execution", description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--api-url", required=True,
                        help="base URL of the management API (session-token minting)")
    parser.add_argument("--api-token", default=os.environ.get("ACO_MANAGEMENT_TOKEN"),
                        help="management bearer credential (env ACO_MANAGEMENT_TOKEN)")
    parser.add_argument("--session-api-url", required=True,
                        help="base URL of the Session API passed to supervisors and agents")
    args = parser.parse_args()

    root = Path(args.data_root).expanduser()
    lock_path = root / "manager.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another execution manager already holds the lock", file=sys.stderr)
        return 3

    conn = db.connect(root / "aco.db")
    db.migrate(conn)  # idempotent; manager may start before the API's first migrate
    startup_recovery(conn, root)
    print(f"execution manager watching {root}", flush=True)
    while True:
        reap_lost_supervisors(conn)
        # verifications first: re-scoring stays responsive even while a long
        # trial run blocks the single loop (one of each per iteration)
        handled = verification.run_pending(conn, root)
        handled = run_one(conn, root, args.api_url, args.api_token, args.session_api_url) or handled
        if not handled:
            time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main())
