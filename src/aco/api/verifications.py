"""Management-side verification API (#15).

Only the trusted management side holds these endpoints (no session-token
path): create a scoring job for a trial's registered Sealed Answer and read
the append-only verification records. Requests carry only verifier
references and an idempotency key — the answer is always resolved from the
sealed_answers registration, never from a host path.
"""

import hashlib
import json
import sqlite3
import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ..app import AppError, canonical
from ..db import utcnow
from ..models import ScorerContent, VerificationCreate


def attempt_out(row: sqlite3.Row) -> dict:
    """One actual verifier-container execution (#15 reopened): append-only
    per-attempt record with its own times and diagnostics."""
    return {
        "attempt_no": row["attempt_no"],
        "status": row["status"],
        "pass": bool(row["pass"]) if row["pass"] is not None else None,
        "submetrics": json.loads(row["submetrics"]) if row["submetrics"] else None,
        "error_kind": row["error_kind"],
        "error_detail": row["error_detail"],
        "raw_output_dir": row["raw_output_dir"],
        "scorer_digest": row["scorer_digest"],
        "image": row["image"],
        "evidence": json.loads(row["evidence"]) if row["evidence"] else None,
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def verification_out(row: sqlite3.Row, conflicting: set[str] | None = None,
                     attempts: list[sqlite3.Row] = ()) -> dict:
    return {
        "id": row["id"],
        "trial_id": row["trial_id"],
        "status": row["status"],
        "verifier": {"name": row["scorer_name"], "version": row["scorer_version"]},
        "pass": bool(row["pass"]) if row["pass"] is not None else None,
        "submetrics": json.loads(row["submetrics"]) if row["submetrics"] else None,
        "error_kind": row["error_kind"],
        "error_detail": row["error_detail"],
        "raw_output_dir": row["raw_output_dir"],
        "scorer_digest": row["scorer_digest"],
        "image": row["image"],
        "evidence": json.loads(row["evidence"]) if row["evidence"] else None,
        # no highest result is ever selected: conflicting same-version
        # successful verdicts are flagged, all records stay queryable
        "stable": (row["id"] not in conflicting) if conflicting else True,
        "attempts": [attempt_out(a) for a in attempts],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "created_at": row["created_at"],
    }


VERIFICATION_SELECT = (
    "SELECT v.*, sv.name AS scorer_name, sv.version AS scorer_version"
    " FROM verifications v JOIN versions sv ON sv.id = v.scorer_version_id"
)


def _conflicting_ids(rows: list[sqlite3.Row]) -> set[str]:
    """Verification ids whose same-version successful verdicts disagree."""
    by_version: dict[str, set[str]] = {}
    for row in rows:
        if row["status"] == "succeeded":
            verdict = json.dumps({"pass": row["pass"], "submetrics": row["submetrics"]},
                                 sort_keys=True)
            by_version.setdefault(row["scorer_version_id"], set()).add(verdict)
    conflicting = set()
    for row in rows:
        if row["status"] == "succeeded" and len(by_version.get(row["scorer_version_id"], ())) > 1:
            conflicting.add(row["id"])
    return conflicting


def _single_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    attempts = _attempts_by_verification(conn, row["trial_id"]).get(row["id"], [])
    return verification_out(row, None, attempts)


def create_verification(conn: sqlite3.Connection, trial_id: str, req: VerificationCreate) -> tuple[int, dict]:
    if conn.execute("SELECT 1 FROM trials WHERE id = ?", (trial_id,)).fetchone() is None:
        raise AppError(404, "not_found", f"trial {trial_id} does not exist")

    scorer = conn.execute(
        "SELECT * FROM versions WHERE kind = 'scorer' AND name = ? AND version = ?",
        (req.verifier.name, req.verifier.version),
    ).fetchone()
    if scorer is None:
        raise AppError(422, "version_not_found",
                       f"scorer version {req.verifier.name}@{req.verifier.version} is not registered")
    try:
        ScorerContent.model_validate(json.loads(scorer["content"]))
    except ValueError as exc:
        raise AppError(422, "invalid_content", f"registered scorer content invalid: {exc}") from exc

    sealed = conn.execute(
        "SELECT digest, status FROM sealed_answers WHERE trial_id = ?", (trial_id,)
    ).fetchone()
    if sealed is None or sealed["status"] != "sealed" or not sealed["digest"]:
        # insufficient artifacts are explicitly not verifiable (#15)
        raise AppError(422, "not_verifiable", "trial has no verifiable sealed answer")

    request_digest = hashlib.sha256(canonical({"verifier": req.verifier.model_dump()}).encode()).hexdigest()
    existing = conn.execute(
        "SELECT * FROM verifications WHERE trial_id = ? AND idempotency_key = ?",
        (trial_id, req.idempotency_key),
    ).fetchone()
    if existing is not None:
        if existing["request_digest"] == request_digest:
            row = conn.execute(f"{VERIFICATION_SELECT} WHERE v.id = ?", (existing["id"],)).fetchone()
            return 200, _single_out(conn, row)
        raise AppError(409, "idempotency_conflict", "same idempotency key with different payload")

    verification_id = uuid.uuid4().hex
    try:
        with conn:
            conn.execute(
                "INSERT INTO verifications (id, trial_id, idempotency_key, request_digest,"
                " scorer_version_id, status, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)",
                (verification_id, trial_id, req.idempotency_key, request_digest,
                 scorer["id"], utcnow()),
            )
    except sqlite3.IntegrityError:
        # multi-worker race on UNIQUE(trial_id, idempotency_key)
        existing = conn.execute(
            "SELECT * FROM verifications WHERE trial_id = ? AND idempotency_key = ?",
            (trial_id, req.idempotency_key),
        ).fetchone()
        if existing is not None and existing["request_digest"] == request_digest:
            row = conn.execute(f"{VERIFICATION_SELECT} WHERE v.id = ?", (existing["id"],)).fetchone()
            return 200, _single_out(conn, row)
        raise AppError(409, "idempotency_conflict", "same idempotency key with different payload") from None
    row = conn.execute(f"{VERIFICATION_SELECT} WHERE v.id = ?", (verification_id,)).fetchone()
    return 202, _single_out(conn, row)


def _attempts_by_verification(conn: sqlite3.Connection, trial_id: str) -> dict[str, list[sqlite3.Row]]:
    rows = conn.execute(
        "SELECT a.* FROM verification_attempts a"
        " JOIN verifications v ON v.id = a.verification_id"
        " WHERE v.trial_id = ? ORDER BY a.attempt_no", (trial_id,)
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["verification_id"], []).append(row)
    return grouped


def list_verifications(conn: sqlite3.Connection, trial_id: str) -> list[dict]:
    if conn.execute("SELECT 1 FROM trials WHERE id = ?", (trial_id,)).fetchone() is None:
        raise AppError(404, "not_found", f"trial {trial_id} does not exist")
    rows = conn.execute(
        f"{VERIFICATION_SELECT} WHERE v.trial_id = ? ORDER BY v.created_at, v.rowid", (trial_id,)
    ).fetchall()
    conflicting = _conflicting_ids(rows)
    attempts = _attempts_by_verification(conn, trial_id)
    return [verification_out(row, conflicting, attempts.get(row["id"], [])) for row in rows]


def register_routes(app: FastAPI, conn: sqlite3.Connection) -> None:
    @app.post("/v1/trials/{trial_id}/verifications", status_code=202)
    async def post_verification(trial_id: str, req: VerificationCreate):
        status_code, body = create_verification(conn, trial_id, req)
        return JSONResponse(status_code=status_code, content=body)

    @app.get("/v1/trials/{trial_id}/verifications")
    async def get_verifications(trial_id: str):
        return list_verifications(conn, trial_id)
