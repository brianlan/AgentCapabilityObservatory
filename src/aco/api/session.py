"""Trial-scoped Session surface (#12): least privilege, idempotent end-intent.

Every request is bound to exactly one trial by a bearer token whose plaintext
is returned once at mint time; only its sha256 digest is stored. The
capability expires when the trial finishes (ADR 0001): a finished trial
(cancelled, or with a sealed/anomalous answer) is derived, not written, so
revocation cannot be forgotten on any termination path.

Responses never carry hidden tests, scores, or authoritative target-write
surfaces; logs never carry token material.
"""

import hashlib
import json
import logging
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import environments
from ..app import AppError, canonical
from ..db import utcnow
from ..models import SubmitRequest

logger = logging.getLogger("aco")

# ponytail: 24h short-lived session token; tune when a real execution window exists.
SESSION_TOKEN_TTL = timedelta(hours=24)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def trial_terminated(conn: sqlite3.Connection, trial_id: str) -> bool:
    """A trial whose answer state is final can never run again: cancelled, or
    holding a sealed/anomalous answer. Derived from the same tables the
    supervisor and lifecycle write, so no revocation write can be missed."""
    trial = conn.execute("SELECT status FROM trials WHERE id = ?", (trial_id,)).fetchone()
    if trial is None or trial["status"] == "cancelled":
        return True
    return conn.execute(
        "SELECT 1 FROM sealed_answers WHERE trial_id = ?", (trial_id,)
    ).fetchone() is not None


def mint_session_token(conn: sqlite3.Connection, trial_id: str) -> dict:
    """Management-side mint: plaintext returned once, digest stored forever."""
    if conn.execute("SELECT 1 FROM trials WHERE id = ?", (trial_id,)).fetchone() is None:
        raise AppError(404, "not_found", f"trial {trial_id} does not exist")
    if trial_terminated(conn, trial_id):
        # cancelled / sealed / anomalous trials can never run: no capability
        logger.info("session_token_mint_rejected_finished trial_id=%s", trial_id)
        raise AppError(409, "trial_not_runnable", "this trial can no longer run")
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + SESSION_TOKEN_TTL).isoformat()
    with conn:
        conn.execute(
            "UPDATE trials SET session_token_digest = ?, session_token_expires_at = ? WHERE id = ?",
            (token_digest(token), expires_at, trial_id),
        )
    logger.info("session_token_minted trial_id=%s", trial_id)
    return {"trial_id": trial_id, "token": token, "expires_at": expires_at}


def trial_from_token(conn: sqlite3.Connection, request: Request) -> sqlite3.Row:
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    row = None
    if token:
        row = conn.execute(
            "SELECT * FROM trials WHERE session_token_digest = ?", (token_digest(token),)
        ).fetchone()
        if row is not None and (row["session_token_expires_at"] or "") <= utcnow():
            row = None
    if row is None:
        logger.info("session_auth_failed")  # audit event without any token material
        raise AppError(401, "unauthorized", "valid session token required")
    if trial_terminated(conn, row["id"]):
        # the capability expired when the trial finished: no late task reads,
        # no late submissions, no post-hoc status reads
        logger.info("session_capability_expired trial_id=%s", row["id"])
        raise AppError(401, "session_expired", "this trial has finished; the session capability has expired")
    return row


def claim_session_task(conn: sqlite3.Connection, trial: sqlite3.Row,
                       data_root: Path) -> dict:
    now = utcnow()
    # atomic: only the first claim sets opened_at; repeats never reset it
    with conn:
        conn.execute(
            "UPDATE trials SET opened_at = ? WHERE id = ? AND opened_at IS NULL", (now, trial["id"])
        )
    row = conn.execute(
        "SELECT t.*, tv.name AS task_name, tv.version AS task_version FROM trials t"
        " JOIN versions tv ON tv.id = t.task_version_id WHERE t.id = ?",
        (trial["id"],),
    ).fetchone()
    task = conn.execute(
        "SELECT name, version, content, assets FROM versions WHERE id = ?", (row["task_version_id"],)
    ).fetchone()
    first_claim = row["opened_at"] == now
    logger.info("session_task_claimed trial_id=%s first_claim=%s", row["id"], first_claim)
    # the instruction comes from its single declared source — the registered
    # immutable public asset when the version declares one, else the version's
    # prompt field (#20 reopen); a broken source is refused, never an empty
    # prompt. Hidden tests/answers never leave the registry either way.
    try:
        instruction, _ = environments.resolve_instruction(
            json.loads(task["content"]), json.loads(task["assets"]), data_root)
    except environments.EnvironmentInvalid as exc:
        raise AppError(500, "task_environment_invalid", str(exc)) from exc
    return {
        "trial_id": row["id"],
        "status": row["status"],
        "task": {"name": task["name"], "version": task["version"]},
        "instruction": instruction,
        "opened_at": row["opened_at"],
    }


def submit_session(conn: sqlite3.Connection, trial: sqlite3.Row, req: SubmitRequest) -> tuple[int, dict]:
    request_digest = hashlib.sha256(
        canonical({"idempotency_key": req.idempotency_key}).encode()
    ).hexdigest()
    existing = conn.execute(
        "SELECT * FROM submissions WHERE trial_id = ?", (trial["id"],)
    ).fetchone()
    if existing is not None:
        if existing["idempotency_key"] == req.idempotency_key:
            if existing["request_digest"] == request_digest:
                logger.info("session_submit_replayed trial_id=%s", trial["id"])
                return 200, {"receipt_id": existing["receipt_id"], "status": existing["status"]}
            raise AppError(409, "idempotency_conflict", "same idempotency key with different payload")
        raise AppError(409, "already_submitted", "trial already has a submission")
    receipt_id = uuid.uuid4().hex
    try:
        with conn:
            conn.execute(
                "INSERT INTO submissions (trial_id, idempotency_key, request_digest, receipt_id,"
                " status, created_at) VALUES (?, ?, ?, ?, 'accepted', ?)",
                (trial["id"], req.idempotency_key, request_digest, receipt_id, utcnow()),
            )
    except sqlite3.IntegrityError:
        # multi-worker race on the one-intent-per-trial constraint; retry sees
        # the stored receipt on the next identical request
        logger.info("session_submit_race trial_id=%s", trial["id"])
        raise AppError(409, "already_submitted", "trial already has a submission") from None
    logger.info("session_submit_accepted trial_id=%s receipt_id=%s", trial["id"], receipt_id)
    # 202: intent accepted only — the official answer is sealed from the
    # workspace by the supervisor (#14); not sealed, not scored
    return 202, {"receipt_id": receipt_id, "status": "accepted"}


def session_submission(conn: sqlite3.Connection, trial: sqlite3.Row) -> dict:
    row = conn.execute(
        "SELECT receipt_id, status, created_at FROM submissions WHERE trial_id = ?", (trial["id"],)
    ).fetchone()
    if row is None:
        raise AppError(404, "not_found", "no submission for this trial")
    # 'sealed' is never session-visible: the capability expires the moment the
    # seal completes. 'sealing'/'error' are visible while the trial lives.
    return {"receipt_id": row["receipt_id"], "status": row["status"], "submitted_at": row["created_at"]}


def register_session_routes(app, conn: sqlite3.Connection, data_root: Path) -> None:
    """The only operations an evaluated session can reach (#12)."""

    @app.get("/v1/session/task")
    async def get_session_task(request: Request):
        return claim_session_task(conn, trial_from_token(conn, request), data_root)

    @app.post("/v1/session/submit", status_code=202)
    async def post_session_submit(request: Request, req: SubmitRequest):
        status_code, body = submit_session(conn, trial_from_token(conn, request), req)
        return JSONResponse(status_code=status_code, content=body)

    @app.get("/v1/session/submission")
    async def get_session_submission(request: Request):
        return session_submission(conn, trial_from_token(conn, request))
