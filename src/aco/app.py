"""ACO API: versioned registry, evaluation plans, trial sessions."""

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db
from .models import (
    AssetRef,
    ConfigContent,
    ExperimentCreate,
    ExperimentOut,
    SubmitRequest,
    SuiteContent,
    TrialOut,
    VersionRecord,
    VersionRef,
    VersionRegistration,
)

logger = logging.getLogger("aco")

# ponytail: V1 default is one answer slot per trial; add config knob when a
# multi-slot need actually exists.
SINGLE_ANSWER_SLOT = 1

# ponytail: 24h short-lived session token; tune when a real execution window exists.
SESSION_TOKEN_TTL = timedelta(hours=24)


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def version_digest(kind: str, name: str, version: str, content: dict, assets: list[AssetRef]) -> str:
    payload = {
        "kind": kind,
        "name": name,
        "version": version,
        "content": content,
        "assets": sorted((a.model_dump() for a in assets), key=lambda a: (a["name"], a["digest"])),
    }
    return hashlib.sha256(canonical(payload).encode()).hexdigest()


def fetch_version(conn: sqlite3.Connection, kind: str, name: str, version: str):
    return conn.execute(
        "SELECT * FROM versions WHERE kind = ? AND name = ? AND version = ?",
        (kind, name, version),
    ).fetchone()


def resolve_version(conn: sqlite3.Connection, kind: str, ref: VersionRef):
    row = fetch_version(conn, kind, ref.name, ref.version)
    if row is None:
        raise AppError(422, "version_not_found", f"{kind} version {ref.name}@{ref.version} is not registered")
    return row


def version_record(row) -> dict:
    return VersionRecord(
        id=row["id"],
        kind=row["kind"],
        name=row["name"],
        version=row["version"],
        content=json.loads(row["content"]),
        assets=[AssetRef(**a) for a in json.loads(row["assets"])],
        created_at=row["created_at"],
    ).model_dump()


def register_version(conn: sqlite3.Connection, reg: VersionRegistration) -> tuple[dict, int]:
    """Return (record, status): 201 on first registration, 200 idempotent."""
    if reg.kind == "suite":
        try:
            suite = SuiteContent.model_validate(reg.content)
        except ValueError as exc:
            raise AppError(422, "invalid_content", f"suite content invalid: {exc}") from exc
        for task_ref in suite.tasks:
            resolve_version(conn, "task", task_ref)
    elif reg.kind == "config":
        try:
            ConfigContent.model_validate(reg.content)
        except ValueError as exc:
            raise AppError(422, "invalid_content", f"config content invalid: {exc}") from exc

    digest = version_digest(reg.kind, reg.name, reg.version, reg.content, reg.assets)
    row = fetch_version(conn, reg.kind, reg.name, reg.version)
    if row is not None:
        if row["id"] == digest:
            return version_record(row), 200
        raise AppError(409, "version_conflict", f"{reg.kind} {reg.name}@{reg.version} already exists with different content")
    try:
        with conn:
            conn.execute(
                "INSERT INTO versions (id, kind, name, version, content, assets, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (digest, reg.kind, reg.name, reg.version, canonical(reg.content),
                 canonical([a.model_dump() for a in reg.assets]), db.utcnow()),
            )
    except sqlite3.IntegrityError:
        # UNIQUE(kind, name, version) is the authoritative guard; this branch
        # only maps the multi-worker registration race to a correct idempotent
        # 200 instead of a spurious 500.
        row = fetch_version(conn, reg.kind, reg.name, reg.version)
        if row is not None and row["id"] == digest:
            return version_record(row), 200
        raise AppError(409, "version_conflict", f"{reg.kind} {reg.name}@{reg.version} already exists with different content") from None
    return version_record(fetch_version(conn, reg.kind, reg.name, reg.version)), 201


def create_experiment(conn: sqlite3.Connection, req: ExperimentCreate) -> dict:
    if (req.task is None) == (req.suite is None):
        raise AppError(422, "invalid_selection", "exactly one of 'task' or 'suite' is required")

    if req.suite is not None:
        suite_row = resolve_version(conn, "suite", req.suite)
        task_refs = SuiteContent.model_validate(json.loads(suite_row["content"])).tasks
    else:
        task_refs = [req.task]
    task_rows = [resolve_version(conn, "task", ref) for ref in task_refs]

    # dedupe targets preserving request order
    target_rows = []
    for ref in req.targets:
        row = resolve_version(conn, "config", ref)
        if all(r["id"] != row["id"] for r in target_rows):
            target_rows.append(row)

    experiment_id = uuid.uuid4().hex
    trial_rows = []
    for task_row in task_rows:
        for config_row in target_rows:
            for repetition in range(1, req.repetitions + 1):
                trial_rows.append((
                    uuid.uuid4().hex,
                    experiment_id,
                    task_row["id"],
                    config_row["id"],
                    repetition,
                    len(trial_rows) + 1,
                    json.loads(config_row["content"]),
                ))

    requested = req.model_dump()
    # Single transaction: validation failed earlier, so any failure here rolls
    # back the whole plan — no half-created experiments or trials.
    with conn:
        conn.execute(
            "INSERT INTO experiments (id, status, requested, created_at) VALUES (?, 'planned', ?, ?)",
            (experiment_id, canonical(requested), db.utcnow()),
        )
        conn.executemany(
            "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
            " repetition, plan_order, status, requested) VALUES (?, ?, ?, ?, ?, ?, 'planned', ?)",
            [
                (tid, eid, task_id, config_id, rep, order,
                 canonical({"task": {"name": task_row["name"], "version": task_row["version"]},
                            "config": config_content, "answer_slot": SINGLE_ANSWER_SLOT}))
                for tid, eid, task_id, config_id, rep, order, config_content in trial_rows
            ],
        )
    return get_experiment(conn, experiment_id)


def trial_out(row) -> dict:
    return TrialOut(
        id=row["id"],
        experiment_id=row["experiment_id"],
        repetition=row["repetition"],
        plan_order=row["plan_order"],
        status=row["status"],
        task=VersionRef(name=row["task_name"], version=row["task_version"]),
        config=VersionRef(name=row["config_name"], version=row["config_version"]),
        requested=json.loads(row["requested"]),
        runtime_observation=json.loads(row["runtime_observation"]) if row["runtime_observation"] else None,
    ).model_dump()


TRIAL_SELECT = (
    "SELECT t.*, tv.name AS task_name, tv.version AS task_version,"
    " cv.name AS config_name, cv.version AS config_version"
    " FROM trials t"
    " JOIN versions tv ON tv.id = t.task_version_id"
    " JOIN versions cv ON cv.id = t.config_version_id"
)


def get_experiment(conn: sqlite3.Connection, experiment_id: str) -> dict:
    row = conn.execute("SELECT * FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
    if row is None:
        raise AppError(404, "not_found", f"experiment {experiment_id} does not exist")
    trials = conn.execute(
        f"{TRIAL_SELECT} WHERE t.experiment_id = ? ORDER BY t.plan_order", (experiment_id,)
    ).fetchall()
    return ExperimentOut(
        id=row["id"],
        status=row["status"],
        requested=json.loads(row["requested"]),
        created_at=row["created_at"],
        trials=[TrialOut(**trial_out(t)) for t in trials],
    ).model_dump()


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


# --- Session API (#12): trial-scoped, least-privilege, idempotent end-intent ---


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint_session_token(conn: sqlite3.Connection, trial_id: str) -> dict:
    if conn.execute("SELECT 1 FROM trials WHERE id = ?", (trial_id,)).fetchone() is None:
        raise AppError(404, "not_found", f"trial {trial_id} does not exist")
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
        if row is not None and (row["session_token_expires_at"] or "") <= db.utcnow():
            row = None
    if row is None:
        # audit event without any token material
        logger.info("session_auth_failed")
        raise AppError(401, "unauthorized", "valid session token required")
    return row


def claim_session_task(conn: sqlite3.Connection, trial: sqlite3.Row) -> dict:
    now = db.utcnow()
    # atomic: only the first claim sets opened_at; repeats never reset it
    with conn:
        conn.execute(
            "UPDATE trials SET opened_at = ? WHERE id = ? AND opened_at IS NULL", (now, trial["id"])
        )
    row = conn.execute(f"{TRIAL_SELECT} WHERE t.id = ?", (trial["id"],)).fetchone()
    task = conn.execute(
        "SELECT name, version, content FROM versions WHERE id = ?", (row["task_version_id"],)
    ).fetchone()
    first_claim = row["opened_at"] == now
    logger.info("session_task_claimed trial_id=%s first_claim=%s", row["id"], first_claim)
    # minimal public surface: instruction only; hidden tests/answers never leave the registry
    return {
        "trial_id": row["id"],
        "status": row["status"],
        "task": {"name": task["name"], "version": task["version"]},
        "instruction": json.loads(task["content"]).get("prompt"),
        "opened_at": row["opened_at"],
    }


def submit_session(conn: sqlite3.Connection, trial: sqlite3.Row, req: SubmitRequest) -> tuple[int, dict]:
    request_digest = hashlib.sha256(
        canonical({"answer": req.answer, "idempotency_key": req.idempotency_key}).encode()
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
                " status, answer, created_at) VALUES (?, ?, ?, ?, 'accepted', ?, ?)",
                (trial["id"], req.idempotency_key, request_digest, receipt_id,
                 canonical(req.answer), db.utcnow()),
            )
    except sqlite3.IntegrityError:
        # multi-worker race on the one-intent-per-trial constraint; retry sees
        # the stored receipt on the next identical request
        logger.info("session_submit_race trial_id=%s", trial["id"])
        raise AppError(409, "already_submitted", "trial already has a submission") from None
    logger.info("session_submit_accepted trial_id=%s receipt_id=%s", trial["id"], receipt_id)
    # 202: intent accepted only — not sealed, not scored
    return 202, {"receipt_id": receipt_id, "status": "accepted"}


def session_submission(conn: sqlite3.Connection, trial: sqlite3.Row) -> dict:
    row = conn.execute(
        "SELECT receipt_id, status, created_at FROM submissions WHERE trial_id = ?", (trial["id"],)
    ).fetchone()
    if row is None:
        raise AppError(404, "not_found", "no submission for this trial")
    return {"receipt_id": row["receipt_id"], "status": row["status"], "submitted_at": row["created_at"]}


def create_app(data_root: str | None = None) -> FastAPI:
    root = Path(data_root or os.environ.get("ACO_DATA_ROOT", "data")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    conn = db.connect(root / "aco.db")
    db.migrate(conn)

    app = FastAPI(title="Agent Capability Observatory", version="0.1.0")

    @app.exception_handler(AppError)
    async def app_error_handler(_request, exc: AppError):
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request, exc: RequestValidationError):
        detail = "; ".join(
            f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        return error_response(422, "validation_error", detail)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request, exc: StarletteHTTPException):
        return error_response(exc.status_code, "http_error", str(exc.detail))

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post(
        "/v1/versions",
        status_code=201,
        responses={200: {"description": "Idempotent re-registration of identical content", "model": VersionRecord}},
    )
    async def post_version(reg: VersionRegistration):
        record, status = register_version(conn, reg)
        return JSONResponse(status_code=status, content=record)

    @app.post("/v1/experiments", status_code=202, response_model=ExperimentOut)
    async def post_experiment(req: ExperimentCreate):
        return create_experiment(conn, req)

    @app.get("/v1/experiments/{experiment_id}", response_model=ExperimentOut)
    async def get_experiment_route(experiment_id: str):
        return get_experiment(conn, experiment_id)

    @app.get("/v1/trials/{trial_id}", response_model=TrialOut)
    async def get_trial(trial_id: str):
        row = conn.execute(f"{TRIAL_SELECT} WHERE t.id = ?", (trial_id,)).fetchone()
        if row is None:
            raise AppError(404, "not_found", f"trial {trial_id} does not exist")
        return trial_out(row)

    # management path: mints a trial-scoped token (plaintext returned once)
    @app.post("/v1/trials/{trial_id}/session-token", status_code=201)
    async def post_session_token(trial_id: str):
        return mint_session_token(conn, trial_id)

    # session path: bearer token binds every request to exactly one trial
    @app.get("/v1/session/task")
    async def get_session_task(request: Request):
        return claim_session_task(conn, trial_from_token(conn, request))

    @app.post("/v1/session/submit", status_code=202)
    async def post_session_submit(request: Request, req: SubmitRequest):
        status_code, body = submit_session(conn, trial_from_token(conn, request), req)
        return JSONResponse(status_code=status_code, content=body)

    @app.get("/v1/session/submission")
    async def get_session_submission(request: Request):
        return session_submission(conn, trial_from_token(conn, request))

    return app


app = create_app()
