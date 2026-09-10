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
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import artifacts, db, lifecycle, runs, verification
from .supervisor import cleanup_container

POLL_INTERVAL_SEC = 1.0
SUPERVISOR_TIMEOUT_SEC = 300  # generous ceiling; the supervisor enforces its own shorter one


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
    """Atomically move the oldest planned trial of a planned experiment to
    claimed; single winner. Paused/cancelled experiments never start work."""
    row = conn.execute(
        "UPDATE trials SET status = 'claimed'"
        " WHERE id = (SELECT t.id FROM trials t JOIN experiments e ON e.id = t.experiment_id"
        "             WHERE t.status = 'planned' AND e.status = 'planned' ORDER BY t.rowid LIMIT 1)"
        " RETURNING id"
    ).fetchone()
    conn.commit()
    return row["id"] if row else None


def recover_stale_claims(conn) -> None:
    """Manager crash between claim and spawn leaves claimed trials without a
    run; reset them so they are picked up again."""
    conn.execute(
        "UPDATE trials SET status = 'planned' WHERE status = 'claimed' AND id NOT IN"
        " (SELECT trial_id FROM trial_runs WHERE status IN ('launching', 'running'))"
    )
    conn.commit()


def reap_lost_supervisors(conn) -> None:
    """Supervisor died without recording an outcome: keep the intent, record
    the loss, and clean up only containers carrying our label."""
    for run in runs.unfinished_runs(conn):
        pid = run["supervisor_pid"]
        if os.path.exists(f"/proc/{pid}"):
            continue
        runs.add_phase(conn, run["run_id"], "supervisor_lost", pid=pid)
        runs.finish_run(conn, run["run_id"], "error", runs.EXIT_SUPERVISOR_LOST,
                        f"supervisor pid {pid} disappeared without recording an outcome")
        cleanup_container(run["run_id"])


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
        # API unreachable or trial not runnable: unclaim and retry on a later poll
        conn.execute("UPDATE trials SET status = 'planned' WHERE id = ?", (trial_id,))
        conn.commit()
        return False

    # ponytail: placeholder pid is replaced the moment Popen returns, so the
    # intent row always exists before any side effect; refine if a reap race
    # ever matters.
    run_id = runs.create_run(conn, trial_id, profile, supervisor_pid=-1)
    supervisor_log = (root / "runs" / run_id / "supervisor.log")
    supervisor_log.parent.mkdir(parents=True, exist_ok=True)
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
        stderr=open(supervisor_log, "w"),
    )
    conn.execute("UPDATE trial_runs SET supervisor_pid = ? WHERE run_id = ?", (proc.pid, run_id))
    conn.commit()

    deadline = time.monotonic() + SUPERVISOR_TIMEOUT_SEC
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    if proc.poll() is None:
        proc.kill()
        proc.wait()
    if runs.get_run(conn, run_id)["status"] in ("launching", "running"):
        runs.add_phase(conn, run_id, "supervisor_lost", pid=proc.pid)
        runs.finish_run(conn, run_id, "error", runs.EXIT_SUPERVISOR_LOST,
                        f"supervisor pid {proc.pid} exited without recording an outcome")
        cleanup_container(run_id)
    return True


def startup_recovery(conn, root: Path) -> None:
    """One-time reconciliation at manager startup (#16), in order: stale
    claims (launch intent is authoritative), lost supervisors, interrupted
    seals from disk truth (#14), adoption of live supervisors, requeue of
    stuck scoring, and pausing unstarted plans until explicit resume."""
    recover_stale_claims(conn)
    reap_lost_supervisors(conn)
    artifacts.recover(conn, root)
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
