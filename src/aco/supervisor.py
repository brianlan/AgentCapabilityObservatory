"""Independent supervisor process: runs one trial via Harbor and records evidence (#13).

The FastAPI process only plans; this process owns long-running execution and
container control. Only public Harbor entry points are used (Trial.create,
add_hook, verifier-off config, extra compose file) with a pinned Harbor
version. Harbor's raw outcome is diagnostics only — never a score (#15 owns
scoring).
"""

import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import artifacts, db, lifecycle, runs

# pinned at adoption; prototype verified the installed package against this
# source commit byte-for-byte (prototypes/harbor-freeze evidence).
HARBOR_VERSION = "0.22.0"
HARBOR_SOURCE_COMMIT = "71c39eafbd134d43ae3f489b5e6488b2a157de65"
ADAPTER_VERSION = "0.1.0"

# fixed digest used by the prototype; the fake agent needs nothing newer
IMAGE = "python@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254"
RUN_LABEL = "aco.run"
DEFAULT_AGENT_TIMEOUT_SEC = 20
TRIAL_GRACE_SEC = 90
SUBMIT_POLL_SEC = 0.5

FAKE_HARNESS = "fake"
# the fake target calls no model; anything model-shaped is unsupported, never
# silently downgraded (issue acceptance: explicit failure)
FAKE_MODEL_VALUES = {"", "none"}
# complete accepted fake-profile shape; anything else fails explicitly
FAKE_KNOWN_KEYS = ("harness", "model", "provider", "skills", "credentials", "inference")


class UnsupportedTarget(Exception):
    pass


def translate_profile(profile: dict) -> int:
    """Return the agent timeout for the fake target or raise UnsupportedTarget."""
    for key in sorted(profile):
        if key not in FAKE_KNOWN_KEYS:
            raise UnsupportedTarget(
                f"unsupported target profile field {key!r} for {FAKE_HARNESS!r} in V1;"
                " only a bare fake profile executes"
            )
    if profile.get("harness") != FAKE_HARNESS:
        raise UnsupportedTarget(
            f"unsupported harness {profile.get('harness')!r}: only {FAKE_HARNESS!r} executes in V1"
        )
    if profile.get("model") not in FAKE_MODEL_VALUES:
        raise UnsupportedTarget(
            f"fake target does not support model={profile.get('model')!r};"
            " the fake agent calls no model in V1"
        )
    for key in ("provider", "skills", "credentials", "inference"):
        if profile.get(key):
            raise UnsupportedTarget(
                f"fake target does not support {key}={profile.get(key)!r};"
                " only a bare fake profile executes in V1"
            )
    return DEFAULT_AGENT_TIMEOUT_SEC


def command(*args, check=True):
    return subprocess.run(args, text=True, capture_output=True, timeout=20, check=check)


def build_task_dir(work_dir: Path, instruction: str, agent_timeout_sec: int, run_id: str) -> Path:
    """Minimal Harbor task dir: registry prompt as instruction, pinned image,
    offline compose override with our identification label."""
    task_dir = work_dir / "task"
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "instruction.md").write_text(instruction)
    (task_dir / "task.toml").write_text(
        f'schema_version = "1.4"\n[environment]\ndocker_image = "{IMAGE}"\n'
        f'network_mode = "public"\n[agent]\ntimeout_sec = {agent_timeout_sec}\n'
    )
    (task_dir / "environment" / "Dockerfile").write_text(f"FROM {IMAGE}\n")
    compose = task_dir / "offline.yaml"
    compose.write_text(
        "services:\n"
        "  main:\n"
        "    network_mode: none\n"
        "    labels:\n"
        f"      {RUN_LABEL}: {run_id}\n"
    )
    return task_dir


def discover_container(run_id: str) -> str:
    ids = command(
        "docker", "ps", "-q",
        "--filter", f"label={RUN_LABEL}={run_id}",
        "--filter", "label=com.docker.compose.service=main",
    ).stdout.split()
    if len(ids) != 1:
        raise RuntimeError(f"expected exactly one agent container, found {len(ids)}")
    return ids[0]


def cleanup_container(run_id: str) -> None:
    ids = command("docker", "ps", "-aq", "--filter", f"label={RUN_LABEL}={run_id}", check=False).stdout.split()
    for cid in ids:
        command("docker", "rm", "-f", cid, check=False)


def container_security_summary(container_id: str) -> dict:
    """Runtime evidence: the agent container must stay unprivileged and
    isolated (no docker socket, no host network)."""
    inspect = json.loads(command("docker", "inspect", container_id).stdout)[0]
    host_config = inspect["HostConfig"]
    mounts = [str(m.get("Source", "")) for m in inspect.get("Mounts", [])]
    mounts += host_config.get("Binds") or []
    return {
        "privileged": bool(host_config.get("Privileged")),
        "network_mode": host_config.get("NetworkMode"),
        "docker_socket_mounted": any("docker.sock" in source for source in mounts),
    }


def _has_submission(conn: sqlite3.Connection, trial_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM submissions WHERE trial_id = ?", (trial_id,)
    ).fetchone() is not None


def _finish_trial_from_answer(conn: sqlite3.Connection, trial_id: str,
                              run_id: str, trigger: str) -> None:
    """Route the trial's terminal state through the one funnel, using the
    outcome the seal actually recorded (sealed or anomaly). First legal
    trigger wins inside finish_trial; a losing trigger cannot rewrite."""
    row = conn.execute(
        "SELECT status FROM sealed_answers WHERE trial_id = ?", (trial_id,)
    ).fetchone()
    outcome = row["status"] if row is not None and row["status"] in ("sealed", "anomaly") else "anomaly"
    lifecycle.finish_trial(conn, trial_id, trigger, outcome, run_id=run_id)


def _fail_before_agent_start(conn: sqlite3.Connection, run: sqlite3.Row,
                             trigger: str, detail: str) -> None:
    """A run that can never start the agent (invalid contract, unsupported
    target) is terminal: a diagnostic anomaly — never a rerun, never a
    capability sample (#16)."""
    artifacts.mark_anomaly(conn, run["trial_id"], run["run_id"],
                           f"{trigger}: {detail}", trigger=trigger)
    lifecycle.finish_trial(conn, run["trial_id"], trigger, "anomaly",
                           run_id=run["run_id"], detail=detail)


def _seal_after_run(conn: sqlite3.Connection, run: sqlite3.Row, container_id: str | None,
                    trigger: str, root: Path, baseline_dir: Path,
                    contract: artifacts.ArtifactContract) -> None:
    """Freeze the workspace through the single seal entry point, using the
    contract resolved from the TaskVersion before the agent started. The seal
    happens while the container still exists — Harbor's later artifact
    collection is diagnostics only and never the official answer (#14)."""
    if container_id is None:
        # sealing cannot even be attempted (e.g. timeout before container
        # discovery): persist an explainable anomaly instead of leaving the
        # submission in limbo (#14)
        detail = "sealing skipped: no agent container discovered"
        artifacts.mark_anomaly(conn, run["trial_id"], run["run_id"], detail, trigger=trigger)
        runs.add_phase(conn, run["run_id"], "seal_failed", detail=detail)
        return
    try:
        receipt = artifacts.seal(conn, run, container_id, trigger, root, baseline_dir, contract)
    except Exception as exc:  # noqa: BLE001 — sealing failure is recorded, never fatal
        # no official answer exists: leave an execution-condition anomaly
        # behind (explainable state, never a capability sample) (#14)
        artifacts.mark_anomaly(conn, run["trial_id"], run["run_id"],
                               f"sealing failed: {type(exc).__name__}: {exc}", trigger=trigger)
        runs.add_phase(conn, run["run_id"], "seal_failed", detail=f"{type(exc).__name__}: {exc}")
        return
    runs.add_phase(conn, run["run_id"], "sealed", receipt_id=receipt["receipt_id"],
                   answer_digest=receipt["digest"])


def _record_timeout_verdict(conn: sqlite3.Connection, run: sqlite3.Row,
                            agent_started_at: dict | None, agent_timeout_sec: int) -> None:
    """Deadline, actual freeze, and tolerance verdict for a timeout seal (#16).

    The planned deadline anchors at the observed agent start plus the agent
    timeout; an answer frozen beyond that plus the trial grace window is a
    diagnostic anomaly, never a capability-curve sample.
    """
    from datetime import datetime, timedelta, timezone

    from . import lifecycle

    if agent_started_at is None:
        return  # the agent never started; no deadline to compare against
    started = datetime.fromisoformat(agent_started_at["at"])
    deadline = started + timedelta(seconds=agent_timeout_sec)
    trial = conn.execute(
        "SELECT experiment_id FROM trials WHERE id = ?", (run["trial_id"],)
    ).fetchone()
    lifecycle.record_timeout_verdict(
        conn, trial["experiment_id"], run["trial_id"], run["run_id"],
        deadline, timedelta(seconds=TRIAL_GRACE_SEC),
    )


def _retrigger_answer(conn: sqlite3.Connection, trial_id: str, run_id: str) -> None:
    """Correct an AGENT_END-hook seal to 'timeout' when the deadline won the
    termination race (#16 reopen). The hook seals with the best trigger known
    at that moment ('exit' when nothing was submitted) — but a harbor agent
    timeout fires AGENT_END too, and the first legal termination reason is
    the timeout, not the mechanical agent end. Content, receipt, and a
    submit-won seal are never touched."""
    conn.execute(
        "UPDATE sealed_answers SET seal_trigger = 'timeout' WHERE trial_id = ?"
        " AND status = 'sealed' AND seal_trigger = 'exit'", (trial_id,))
    conn.commit()


async def execute_run(conn: sqlite3.Connection, run: sqlite3.Row, root: Path) -> None:
    run_id = run["run_id"]
    trial = conn.execute("SELECT * FROM trials WHERE id = ?", (run["trial_id"],)).fetchone()
    task_content = json.loads(
        conn.execute("SELECT content FROM versions WHERE id = ?", (trial["task_version_id"],)).fetchone()["content"]
    )
    profile = json.loads(
        conn.execute("SELECT content FROM versions WHERE id = ?", (trial["config_version_id"],)).fetchone()["content"]
    )

    # resolve the task-declared artifact contract ONCE, before any side
    # effect: it governs baseline, collection, validation, manifest, and
    # recovery; missing/invalid/mismatched contract fails before the agent
    # starts — never a silent default (#14 reopen)
    try:
        contract = artifacts.resolve_task_contract(task_content)
    except artifacts.SealError as exc:
        runs.finish_run(conn, run_id, "error", runs.EXIT_CONTRACT_INVALID,
                        f"task artifact contract invalid: {exc}")
        _fail_before_agent_start(conn, run, "contract_invalid", str(exc))
        return

    # fail before any side effect on unsupported profiles
    try:
        agent_timeout_sec = translate_profile(profile)
    except UnsupportedTarget as exc:
        runs.finish_run(conn, run_id, "error", runs.EXIT_UNSUPPORTED_TARGET, str(exc))
        _fail_before_agent_start(conn, run, "unsupported_target", str(exc))
        return

    from harbor.models.trial.config import (
        AgentConfig,
        EnvironmentConfig,
        TaskConfig,
        TrialConfig,
        VerifierConfig,
    )
    from harbor.trial.hooks import TrialEvent
    from harbor.trial.trial import Trial

    work_dir = root / "runs" / run_id
    baseline_dir = root / "sealing" / run_id / "baseline"
    task_dir = build_task_dir(work_dir, task_content.get("prompt", ""), agent_timeout_sec, run_id)
    trials_dir = work_dir / "trials"
    container_id = None
    agent_started_at: dict | None = None

    async def on_agent_start(_event):
        nonlocal container_id, agent_started_at
        container_id = await asyncio.to_thread(discover_container, run_id)
        runs.mark_running(conn, run_id, container_id=container_id, image=IMAGE)
        lifecycle.mark_trial_running(conn, run["trial_id"])  # claimed -> running (#16)
        security = await asyncio.to_thread(container_security_summary, container_id)
        agent_started_at = runs.add_phase(conn, run_id, "agent_start", container_id=container_id,
                                          container_security=security)
        # pre-agent baseline for the manifest's added/modified/deleted diff
        await asyncio.to_thread(artifacts.snapshot_baseline, container_id, baseline_dir, contract)

    submit_stopped = False
    agent_ended = False

    async def on_agent_end(_event):
        # the seal lives here because the container only exists while harbor's
        # run() is in flight — harbor reaps it afterwards. The trigger is the
        # best knowledge at this moment; handle_timeout corrects it to
        # 'timeout' when the deadline actually won the termination race.
        nonlocal agent_ended
        agent_ended = True
        runs.add_phase(conn, run_id, "agent_end")
        trigger = "submit" if _has_submission(conn, run["trial_id"]) else "exit"
        await asyncio.to_thread(_seal_after_run, conn, run, container_id,
                                trigger, root, baseline_dir, contract)

    trial_obj = await Trial.create(TrialConfig(
        task=TaskConfig(path=task_dir),
        trial_name=f"aco-{run_id[:12]}",
        trials_dir=trials_dir,
        agent=AgentConfig(import_path="aco.fake_agent:FakeAgent"),
        environment=EnvironmentConfig(extra_docker_compose=[task_dir / "offline.yaml"]),
        # harbor scoring is disabled permanently; ACO owns all official results
        verifier=VerifierConfig(disable=True),
        artifacts=["/workspace"],
    ))
    trial_obj.add_hook(TrialEvent.AGENT_START, on_agent_start)
    trial_obj.add_hook(TrialEvent.AGENT_END, on_agent_end)

    runs.observe_run(conn, run_id, ADAPTER_VERSION, HARBOR_VERSION, str(trials_dir))

    submit_stopped = False

    async def watch_submit():
        """First legal trigger wins (#16 reopen): the moment a persisted
        submit intent exists, stop the agent container. Writes racing the
        SUBMIT_POLL_SEC window land in the frozen post-stop state; nothing
        written after the stop can reach the sealed answer."""
        nonlocal submit_stopped
        while True:
            await asyncio.sleep(SUBMIT_POLL_SEC)
            # agent_ended check: once the agent ended, the AGENT_END hook owns
            # the terminal seal — stopping a sealing container would only
            # misattribute the run's exit
            if _has_submission(conn, run["trial_id"]) and not agent_ended:
                runs.add_phase(conn, run_id, "submit_watch_fired")
                if container_id is not None:
                    await asyncio.to_thread(
                        command, "docker", "stop", "-t", "1", container_id, check=False)
                    submit_stopped = True
                return

    async def handle_timeout(source: str, detail: str) -> None:
        """One path for the ACO outer deadline and Harbor's own agent
        timeout (#16 reopen): seal, planned deadline / actual freeze /
        tolerance verdict, terminal run and trial."""
        runs.add_phase(conn, run_id, "trial_timeout", source=source)
        await asyncio.to_thread(_seal_after_run, conn, run, container_id,
                                "timeout", root, baseline_dir, contract)
        await asyncio.to_thread(_retrigger_answer, conn, run["trial_id"], run_id)
        _record_timeout_verdict(conn, run, agent_started_at, agent_timeout_sec)
        runs.finish_run(conn, run_id, "error", runs.EXIT_TIMEOUT, detail)
        await asyncio.to_thread(_finish_trial_from_answer, conn, run["trial_id"], run_id, "timeout")

    run_task = asyncio.create_task(trial_obj.run())
    watcher = asyncio.create_task(watch_submit())
    try:
        try:
            result = await asyncio.wait_for(run_task, timeout=agent_timeout_sec + TRIAL_GRACE_SEC)
        except asyncio.TimeoutError:
            await handle_timeout("outer_deadline",
                                 f"trial exceeded {agent_timeout_sec + TRIAL_GRACE_SEC}s")
            return
        except Exception as exc:  # noqa: BLE001 — Harbor may raise the agent timeout
            if "AgentTimeoutError" in f"{type(exc).__name__}: {exc}":
                await handle_timeout("harbor_agent_timeout", str(exc))
                return
            raise

        exception = result.exception_info.exception_type if result.exception_info else None
        if exception and "AgentTimeoutError" in str(exception):
            # Harbor's own agent timeout lands on the same verdict path as the
            # outer deadline — it never bypasses eligibility (#16 reopen)
            await handle_timeout("harbor_agent_timeout", str(exception))
            return

        trigger = "submit" if _has_submission(conn, run["trial_id"]) else "exit"
        if trigger == "submit" and (exception or submit_stopped):
            # the supervisor ended the run: the submit watchdog stopped the
            # container (a harbor-stopped container raises no exception), or
            # the agent died after submit — submit is the termination reason
            exit_kind, detail = runs.EXIT_SUBMIT, "agent ended by submit-won container stop"
        else:
            exit_kind, detail = (runs.EXIT_AGENT_ERROR, exception) if exception else (runs.EXIT_NORMAL, None)
        runs.add_phase(conn, run_id, "trial_finished", exception=exception,
                       verifier_scored=result.verifier_result is not None)
        runs.finish_run(conn, run_id, "finished", exit_kind, detail)
        await asyncio.to_thread(_finish_trial_from_answer, conn, run["trial_id"], run_id, trigger)
    finally:
        watcher.cancel()
        if container_id is None:
            # never discovered: no seal can reference it; remove labeled leftovers
            cleanup_container(run_id)


def main() -> int:
    parser = argparse.ArgumentParser(prog="aco-supervisor", description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--data-root", required=True)
    args = parser.parse_args()

    root = Path(args.data_root).expanduser()
    conn = db.connect(root / "aco.db")
    run = runs.get_run(conn, args.run_id)
    if run is None:
        print(f"run {args.run_id} does not exist", file=sys.stderr)
        return 2
    try:
        asyncio.run(execute_run(conn, run, root))
    except Exception as exc:  # noqa: BLE001 — supervisor records its own crash
        # leave diagnostics; the manager reaps leftovers (container/pid)
        if runs.get_run(conn, args.run_id)["status"] in ("launching", "running"):
            runs.finish_run(conn, args.run_id, "error", runs.EXIT_AGENT_ERROR,
                            f"{type(exc).__name__}: {exc}")
        # the trial must land in a terminal state even when the supervisor
        # itself crashes: a diagnostic anomaly, never a rerun (#16). A seal
        # that already won stays sealed (mark_anomaly is exactly-once).
        artifacts.mark_anomaly(conn, run["trial_id"], args.run_id,
                               f"supervisor crash: {type(exc).__name__}: {exc}",
                               trigger="supervisor_crash")
        _finish_trial_from_answer(conn, run["trial_id"], args.run_id, "supervisor_crash")
        cleanup_container(args.run_id)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
