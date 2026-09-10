"""ACO API: versioned registry, evaluation plans, trial sessions.

Two ASGI surfaces share the same domain modules and SQLite database
(ADR 0001): the authenticated Management surface (registry, planning,
lifecycle, session-token minting, dashboard) and the untrusted-agent-facing
Session surface (three trial-scoped operations, `aco.api.session`).
"""

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, lifecycle, runs
from .models import (
    AssetRef,
    ConfigContent,
    ExperimentCreate,
    ExperimentOut,
    ScorerContent,
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
    elif reg.kind == "scorer":
        try:
            ScorerContent.model_validate(reg.content)
        except ValueError as exc:
            raise AppError(422, "invalid_content", f"scorer content invalid: {exc}") from exc

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


def _idempotency_row(conn: sqlite3.Connection, principal: str, key: str):
    return conn.execute(
        "SELECT * FROM management_idempotency WHERE principal = ? AND idempotency_key = ?",
        (principal, key),
    ).fetchone()


def create_experiment(
    conn: sqlite3.Connection, req: ExperimentCreate,
    principal: str | None = None, idempotency_key: str | None = None,
) -> tuple[dict, int]:
    """Create the evaluation plan; returns (experiment, status).

    With (principal, idempotency_key) the server owns idempotency (#35): the
    same key + canonical request digest replays the original experiment (200),
    the same key with a different body is a 409. Without a key every call
    creates a new experiment — no implicit idempotency.
    """
    request_digest = None
    if idempotency_key is not None:
        request_digest = hashlib.sha256(canonical(req.model_dump()).encode()).hexdigest()
        row = _idempotency_row(conn, principal, idempotency_key)
        if row is not None:
            if row["request_digest"] != request_digest:
                raise AppError(409, "idempotency_conflict",
                               "this Idempotency-Key was already used with a different request")
            return get_experiment(conn, row["experiment_id"]), 200

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
    # back the whole plan — no half-created experiments or trials, and (#35) no
    # orphaned idempotency key. Parent rows are inserted before the FK-bearing
    # idempotency row (SQLite foreign keys are immediate, even in-transaction).
    try:
        with conn:
            conn.execute(
                "INSERT INTO experiments (id, status, requested, created_at) VALUES (?, 'planned', ?, ?)",
                (experiment_id, canonical(requested), db.utcnow()),
            )
            if idempotency_key is not None:
                conn.execute(
                    "INSERT INTO management_idempotency"
                    " (principal, idempotency_key, request_digest, experiment_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (principal, idempotency_key, request_digest, experiment_id, db.utcnow()),
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
    except sqlite3.IntegrityError:
        # Multi-worker race (#35): UNIQUE(principal, idempotency_key) is the
        # authoritative guard; the loser's whole transaction (key + plan) was
        # rolled back, so map it onto the winner's experiment — or 409 if the
        # winner carried a different body.
        if idempotency_key is None:
            raise
        row = _idempotency_row(conn, principal, idempotency_key)
        if row is not None and row["request_digest"] == request_digest:
            return get_experiment(conn, row["experiment_id"]), 200
        raise AppError(409, "idempotency_conflict",
                       "this Idempotency-Key was already used with a different request") from None
    return get_experiment(conn, experiment_id), 202


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
        progress=lifecycle.progress(conn, experiment_id),
    ).model_dump()


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _add_error_handlers(app: FastAPI) -> None:
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


def create_management_app(data_root: str | None = None, token: str | None = None) -> FastAPI:
    """Authenticated management surface (ADR 0001): never exposed to evaluated
    containers. Every route except /healthz requires the management bearer."""
    root = Path(data_root or os.environ.get("ACO_DATA_ROOT", "data")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    conn = db.connect(root / "aco.db")
    db.migrate(conn)

    token = token or os.environ.get("ACO_MANAGEMENT_TOKEN", "")
    if not token:
        # fail closed: a management surface without a credential must not start
        raise RuntimeError(
            "the management surface requires a bearer credential: set ACO_MANAGEMENT_TOKEN"
        )

    app = FastAPI(title="Agent Capability Observatory — Management API", version="0.2.0")
    _add_error_handlers(app)

    @app.middleware("http")
    async def management_auth(request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        supplied = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        # constant-time compare: this is the trust boundary for the whole surface
        if not secrets.compare_digest(supplied, token):
            return error_response(401, "unauthorized", "valid management token required")
        # principal identity for server-side idempotency (#35): sha256 of the
        # supplied bearer — the plaintext never lands in the database
        request.state.principal = hashlib.sha256(supplied.encode()).hexdigest()
        return await call_next(request)

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

    @app.post(
        "/v1/experiments",
        status_code=202,
        responses={
            200: {"description": "Idempotent replay: same principal, key, and request digest", "model": ExperimentOut},
            409: {"description": "Same Idempotency-Key with a different request body"},
        },
    )
    async def post_experiment(
        req: ExperimentCreate, request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        """Create an evaluation plan.

        With an ``Idempotency-Key`` header, the server owns idempotency (#35):
        the same authenticated principal retrying the same request returns the
        original experiment (200); the same key with a different body is a 409
        conflict. WITHOUT the header every call creates a new experiment —
        the API never pretends an unkeyed request is idempotent.
        """
        record, status = create_experiment(
            conn, req, principal=request.state.principal, idempotency_key=idempotency_key,
        )
        return JSONResponse(status_code=status, content=record)

    @app.get("/v1/experiments/{experiment_id}", response_model=ExperimentOut)
    async def get_experiment_route(experiment_id: str):
        return get_experiment(conn, experiment_id)

    # management path: explicit, persistent, idempotent plan changes (#16)
    @app.post("/v1/experiments/{experiment_id}/cancel")
    async def cancel_experiment_route(experiment_id: str):
        status_code, summary = lifecycle.cancel_experiment(conn, experiment_id)
        return JSONResponse(status_code=status_code, content=summary)

    @app.post("/v1/experiments/{experiment_id}/resume")
    async def resume_experiment_route(experiment_id: str):
        status_code, body = lifecycle.resume_experiment(conn, experiment_id)
        return JSONResponse(status_code=status_code, content=body)

    @app.get("/v1/trials/{trial_id}", response_model=TrialOut)
    async def get_trial(trial_id: str):
        row = conn.execute(f"{TRIAL_SELECT} WHERE t.id = ?", (trial_id,)).fetchone()
        if row is None:
            raise AppError(404, "not_found", f"trial {trial_id} does not exist")
        return trial_out(row)

    # management path: launch intent, run phases, and raw exit diagnostics
    @app.get("/v1/trials/{trial_id}/runs")
    async def get_trial_runs(trial_id: str):
        if conn.execute("SELECT 1 FROM trials WHERE id = ?", (trial_id,)).fetchone() is None:
            raise AppError(404, "not_found", f"trial {trial_id} does not exist")
        return [runs.run_out(row) for row in runs.list_runs(conn, trial_id)]

    # management path: mints a trial-scoped token (plaintext returned once);
    # only trials that can still run receive a capability (#12)
    @app.post("/v1/trials/{trial_id}/session-token", status_code=201)
    async def post_session_token(trial_id: str):
        from .api.session import mint_session_token
        return mint_session_token(conn, trial_id)

    # independent scoring of the sealed answer (#15)
    from .api.verifications import register_routes

    register_routes(app, conn)

    # results query and aggregation (#19)
    from .api.results import register_routes as register_results_routes

    register_results_routes(app, conn)

    # management dashboard: server-rendered, read-only audit pages (#18)
    from .web import register_routes as register_dashboard_routes

    register_dashboard_routes(app, conn, root)

    return app


def create_session_app(data_root: str | None = None) -> FastAPI:
    """Untrusted-agent-facing Session surface (ADR 0001): exactly the three
    trial-scoped operations, each bound to one trial by a bearer token."""
    root = Path(data_root or os.environ.get("ACO_DATA_ROOT", "data")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    conn = db.connect(root / "aco.db")
    db.migrate(conn)

    app = FastAPI(title="Agent Capability Observatory — Session API", version="0.2.0")
    _add_error_handlers(app)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    from .api.session import register_session_routes

    register_session_routes(app, conn)
    return app


management_app = create_management_app()
session_app = create_session_app()
