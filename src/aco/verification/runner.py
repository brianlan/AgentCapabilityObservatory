"""Verifier container execution, result parsing, and record keeping (#15).

One verifier container per scoring attempt: ``docker run --network none
--read-only`` with the sealed answer and the trusted verifier bundle mounted
read-only and a fresh private output directory. The container gets no
environment, no credentials, and no access to the application, SQLite, or
the Docker socket. Every actual container start appends a
verification_attempts row; the verifications row carries the idempotent
request and its current state — never a history overwrite.

Scoring errors are classified (verifier_error / invalid_output / infra_error)
and never recorded as ``pass = false``: a failing verdict is a succeeded
record with pass=0, a broken verifier is an error record with pass NULL.
"""

import hashlib
import json
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path

from .. import artifacts
from ..db import utcnow
from ..models import ScorerContent

# ponytail: fixed ceiling for the whole container run; tune when a real
# verifier needs longer than the deterministic fixture.
VERIFIER_TIMEOUT_SEC = 60

RESULT_FILE = "result.json"


def bundle_digest(bundle_dir: Path) -> str:
    """sha256 over the canonical file-entry manifest of the bundle — the
    same entry shape the sealed-answer manifest uses."""
    entries = [artifacts.file_entry(p, bundle_dir)
               for p in sorted(bundle_dir.rglob("*")) if p.is_file()]
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def claim_next_queued(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically move the oldest queued verification to running; single
    winner across processes. Each actual claim appends one attempt row —
    the per-execution history the verifications row itself cannot hold."""
    now = utcnow()
    row = conn.execute(
        "UPDATE verifications SET status = 'running', started_at = ?"
        " WHERE id = (SELECT id FROM verifications WHERE status = 'queued' ORDER BY rowid LIMIT 1)"
        " RETURNING *",
        (now,),
    ).fetchone()
    if row is not None:
        conn.execute(
            "INSERT INTO verification_attempts (id, verification_id, attempt_no,"
            " status, started_at) VALUES (?, ?,"
            " (SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM verification_attempts"
            "  WHERE verification_id = ?), 'running', ?)",
            (uuid.uuid4().hex, row["id"], row["id"], now),
        )
    conn.commit()
    return row


def _record(conn: sqlite3.Connection, verification_id: str, **fields) -> None:
    """Terminal outcome: append to the running attempt row and mirror the
    current state on the verifications row. Both tables share the outcome
    columns; the attempt's started_at and every earlier attempt's row stay
    untouched."""
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = (*fields.values(),)
    conn.execute(f"UPDATE verifications SET {assignments} WHERE id = ?",
                 (*values, verification_id))
    conn.execute(f"UPDATE verification_attempts SET {assignments}"
                 " WHERE verification_id = ? AND status = 'running'",
                 (*values, verification_id))
    conn.commit()


def _error(kind: str, detail: str, **extra) -> dict:
    return {"status": "error", "pass": None, "submetrics": None,
            "error_kind": kind, "error_detail": detail, **extra}


def _run_container(answer_dir: Path, bundle_dir: Path, output_dir: Path,
                   work_dir: Path, content: ScorerContent) -> tuple[int | None, str, str, str | None]:
    """Run the verifier container; returns (exit_code, stdout, stderr, container_id).

    Isolation is enforced by the actual docker arguments: no network, read-only
    root filesystem, answer and bundle mounted read-only, only the fresh output
    directory writable, and no environment passed through. No --rm: the
    stopped container stays for evidence capture and is removed by the caller.
    """
    cid_file = work_dir / "container.id"
    command = [
        "docker", "run", "--cidfile", str(cid_file),
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp",
        "-v", f"{answer_dir}:/answer:ro",
        "-v", f"{bundle_dir}:/verifier:ro",
        "-v", f"{output_dir}:/output",
        content.image, *content.entrypoint,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=VERIFIER_TIMEOUT_SEC, check=False)
    except subprocess.TimeoutExpired as exc:
        # TimeoutExpired may carry bytes even with text=True
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        return None, out, err, _read_cid(cid_file)
    return result.returncode, result.stdout, result.stderr, _read_cid(cid_file)


def _read_cid(cid_file: Path) -> str | None:
    try:
        return cid_file.read_text().strip() or None
    except OSError:
        return None


def container_evidence(container_id: str) -> dict:
    """Runtime security evidence from the actual container, not the config:
    network mode, privilege, mounts, and environment key names."""
    inspect = json.loads(
        subprocess.run(["docker", "inspect", container_id], capture_output=True,
                       text=True, timeout=20, check=True).stdout
    )[0]
    host = inspect["HostConfig"]
    binds = host.get("Binds") or []
    mounts = [str(m.get("Source", "")) for m in inspect.get("Mounts", [])]
    env = inspect.get("Config", {}).get("Env") or []
    sensitive = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "APIKEY", "DATABASE", "DB_", "ANTHROPIC", "OPENAI", "ACO_")
    return {
        "network_mode": host.get("NetworkMode"),
        "privileged": bool(host.get("Privileged")),
        "read_only_rootfs": bool(host.get("ReadonlyRootfs")),
        "docker_socket_mounted": any("docker.sock" in source for source in mounts + binds),
        "mounts": sorted(binds),
        "env_keys": sorted(e.split("=", 1)[0] for e in env),
        "sensitive_env_keys": sorted({name for name in (e.split("=", 1)[0] for e in env)
                                      if any(marker in name.upper() for marker in sensitive)}),
    }


def parse_result(path: Path, expected_schema: str) -> dict:
    """Parse the machine-readable verifier output; never trusts stale or
    malformed files. Returns {'ok': True, pass, submetrics} or {'ok': False,
    detail}."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "detail": f"verifier result unreadable: {exc}"}
    if not isinstance(data, dict) or data.get("schema") != expected_schema:
        return {"ok": False, "detail": "verifier result schema missing or mismatched"}
    if not isinstance(data.get("pass"), bool):
        return {"ok": False, "detail": "verifier result 'pass' must be a boolean"}
    submetrics = data.get("submetrics")
    if submetrics is not None and (
        not isinstance(submetrics, dict)
        or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in submetrics.values())
    ):
        return {"ok": False, "detail": "verifier submetrics must map names to numbers"}
    return {"ok": True, "pass": data["pass"], "submetrics": submetrics}


def _execute(conn: sqlite3.Connection, row: sqlite3.Row, root: Path) -> dict:
    """Run one verification to a terminal outcome (succeeded or error dict)."""
    trial_id = row["trial_id"]
    verification_id = row["id"]

    sealed = conn.execute(
        "SELECT digest, status FROM sealed_answers WHERE trial_id = ?", (trial_id,)
    ).fetchone()
    if sealed is None or sealed["status"] != "sealed" or not sealed["digest"]:
        return _error("infra_error", "sealed answer is no longer verifiable")
    answer_dir = root / "answers" / sealed["digest"]
    if not answer_dir.is_dir():
        return _error("infra_error", f"sealed answer content missing: {sealed['digest']}")

    scorer = conn.execute(
        "SELECT id, name, version, content, assets FROM versions WHERE id = ?",
        (row["scorer_version_id"],),
    ).fetchone()
    if scorer is None:
        return _error("infra_error", "scorer version no longer registered")
    try:
        content = ScorerContent.model_validate(json.loads(scorer["content"]))
    except ValueError as exc:
        return _error("infra_error", f"registered scorer content invalid: {exc}")

    bundle_dir = root / "verifiers" / scorer["id"]
    if not bundle_dir.is_dir():
        return _error("infra_error", "verifier bundle missing")
    observed_digest = bundle_digest(bundle_dir)
    declared = [a["digest"] for a in json.loads(scorer["assets"]) if a.get("name") == "bundle"]
    if not declared:
        return _error("infra_error", "scorer version does not declare a 'bundle' asset digest")
    if declared[0] != observed_digest:
        return _error("infra_error",
                      f"verifier bundle digest mismatch: declared {declared[0]}, observed {observed_digest}")

    attempt = conn.execute(
        "SELECT attempt_no FROM verification_attempts"
        " WHERE verification_id = ? AND status = 'running'", (verification_id,)
    ).fetchone()
    work_dir = root / "verifications" / verification_id / f"attempt-{attempt['attempt_no']}"
    output_dir = work_dir / "output"
    # fresh output per execution: stale or planted files can never be parsed;
    # per-attempt directories keep earlier attempts' diagnostics intact
    shutil.rmtree(work_dir, ignore_errors=True)
    output_dir.mkdir(parents=True)

    code, stdout, stderr, container_id = _run_container(
        answer_dir, bundle_dir, output_dir, work_dir, content
    )
    (work_dir / "docker-run.log").write_text(f"exit={code}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n")

    evidence = None
    if container_id:
        try:
            evidence = container_evidence(container_id)
        except Exception:  # noqa: BLE001 — evidence capture must not flip a verdict
            evidence = None
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, timeout=20, check=False)

    base = {"raw_output_dir": str(output_dir), "scorer_digest": observed_digest,
            "image": content.image, "evidence": json.dumps(evidence) if evidence else None}

    if code is None:
        return _error("verifier_error", f"verifier timed out after {VERIFIER_TIMEOUT_SEC}s", **base)
    if code != 0:
        return _error("verifier_error",
                      f"verifier exited {code}: {stderr.strip()[-500:]}", **base)

    parsed = parse_result(output_dir / RESULT_FILE, content.result_schema)
    if not parsed["ok"]:
        return _error("invalid_output", parsed["detail"], **base)
    submetrics = parsed["submetrics"]
    return {"status": "succeeded", "pass": parsed["pass"],
            "submetrics": json.dumps(submetrics) if submetrics is not None else None,
            "error_kind": None, "error_detail": None, **base}


def execute_verification(conn: sqlite3.Connection, row: sqlite3.Row, root: Path) -> None:
    """Drive one claimed (running) verification to a terminal record.

    No failure path escapes: any unexpected exception becomes an error
    record — scoring errors are never capability failures.
    """
    try:
        outcome = _execute(conn, row, root)
    except Exception as exc:  # noqa: BLE001 — the record must always land
        outcome = _error("infra_error", f"{type(exc).__name__}: {exc}")
    _record(conn, row["id"], finished_at=utcnow(), **outcome)


def run_pending(conn: sqlite3.Connection, root: Path) -> bool:
    """Execute at most one queued verification. Returns True if handled."""
    row = claim_next_queued(conn)
    if row is None:
        return False
    execute_verification(conn, row, root)
    return True


def requeue_stuck_running(conn: sqlite3.Connection) -> int:
    """Manager-startup recovery (#16): scoring rows left in 'running' by a
    manager crash go back to 'queued', and the interrupted attempt is
    finalized as an infra error — with its own started_at and record intact.
    The re-queued execution appends a new attempt; a verifier container never
    calls a model, so requeueing is not an agent rerun."""
    conn.execute(
        "UPDATE verification_attempts SET status = 'error', error_kind = 'infra_error',"
        " error_detail = 'interrupted by manager restart', finished_at = ?"
        " WHERE status = 'running'", (utcnow(),),
    )
    cur = conn.execute(
        "UPDATE verifications SET status = 'queued', started_at = NULL WHERE status = 'running'"
    )
    conn.commit()
    return cur.rowcount


if __name__ == "__main__":  # operator helper: compute a bundle digest for registration
    import sys

    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <bundle-dir>", file=sys.stderr)
        raise SystemExit(2)
    print(bundle_digest(Path(sys.argv[1])))
