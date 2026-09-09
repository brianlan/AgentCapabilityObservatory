"""Server-rendered management dashboard (#18).

Read-only HTML pages inside the same FastAPI app: batch list, batch detail,
and trial detail. Pages render what the domain queries return — eligibility
and verification summaries come from the same tables the API reads; no
capability rule, eligibility rule, or "best result" selection is re-implemented
here or in the browser. Artifact downloads address database IDs only; host
paths are never accepted from the URL.
"""

import json
import re

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from starlette.templating import Jinja2Templates

from .. import runs
from ..api.verifications import list_verifications
from ..app import AppError, TRIAL_SELECT, get_experiment
from pathlib import Path

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))  # autoescape on by default

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")  # digests are content addresses, not paths


def batch_rows(conn) -> list[dict]:
    """One read model row per experiment: plan/execution/seal/verification progress."""
    experiments = conn.execute(
        "SELECT id, status, created_at FROM experiments ORDER BY created_at, id"
    ).fetchall()
    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT experiment_id, status, COUNT(*) AS n FROM trials GROUP BY experiment_id, status"
    ):
        counts.setdefault(row["experiment_id"], {})[row["status"]] = row["n"]
    seals: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT t.experiment_id AS eid, sa.status AS status, COUNT(*) AS n"
        " FROM sealed_answers sa JOIN trials t ON t.id = sa.trial_id"
        " GROUP BY t.experiment_id, sa.status"
    ):
        seals.setdefault(row["eid"], {})[row["status"]] = row["n"]
    scores: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT t.experiment_id AS eid, v.status AS status, COUNT(*) AS n"
        " FROM verifications v JOIN trials t ON t.id = v.trial_id"
        " GROUP BY t.experiment_id, v.status"
    ):
        scores.setdefault(row["eid"], {})[row["status"]] = row["n"]
    return [
        {
            "id": e["id"],
            "status": e["status"],
            "created_at": e["created_at"],
            "trials_total": sum(counts.get(e["id"], {}).values()),
            "trial_counts": counts.get(e["id"], {}),
            "sealed": seals.get(e["id"], {}).get("sealed", 0),
            "anomalies": seals.get(e["id"], {}).get("anomaly", 0),
            "scored": scores.get(e["id"], {}).get("succeeded", 0),
            "score_errors": scores.get(e["id"], {}).get("error", 0),
        }
        for e in experiments
    ]


def trial_summary(conn, experiment_id: str) -> list[dict]:
    """Trials of one experiment with per-trial verification counters."""
    rows = conn.execute(
        f"{TRIAL_SELECT} WHERE t.experiment_id = ? ORDER BY t.plan_order", (experiment_id,)
    ).fetchall()
    stats: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT trial_id, status, COUNT(*) AS n FROM verifications GROUP BY trial_id, status"
    ):
        s = stats.setdefault(row["trial_id"], {"total": 0, "succeeded": 0, "error": 0, "open": 0})
        s["total"] += row["n"]
        if row["status"] in ("succeeded", "error"):
            s[row["status"]] += row["n"]
        else:
            s["open"] += row["n"]
    sealed = {
        r["trial_id"]: r["status"]
        for r in conn.execute("SELECT trial_id, status FROM sealed_answers")
    }
    return [
        {
            "id": r["id"],
            "plan_order": r["plan_order"],
            "repetition": r["repetition"],
            "status": r["status"],
            "task": f'{r["task_name"]}@{r["task_version"]}',
            "config": f'{r["config_name"]}@{r["config_version"]}',
            "seal": sealed.get(r["id"]),
            "verifications": stats.get(r["id"], {"total": 0, "succeeded": 0, "error": 0, "open": 0}),
        }
        for r in rows
    ]


def _run_view(run: dict) -> dict:
    """Requested launch profile next to what was actually observed, each
    observation annotated with where the recorded value came from (the
    supervisor's own record: observe_run / mark_running / label discovery).
    Unknowns stay unknown."""
    observed = [
        ("adapter 版本", run["adapter_version"], "监督进程上报（observe_run）"),
        ("harbor 版本", run["harbor_version"], "监督进程上报（固定版本）"),
        ("容器镜像", run["image"], "启动配置 + 运行时记录（mark_running）"),
        ("容器 ID", run["container_id"], "按运行标签从容器运行时发现（discover_container）"),
    ]
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "requested_profile": json.dumps(run["requested_profile"], ensure_ascii=False, indent=2),
        "observed": observed,
        "phases": run["phases"],
        "exit_kind": run["exit_kind"],
        "exit_detail": run["exit_detail"],
        "log_dir": run["log_dir"],
        "launched_at": run["launched_at"],
        "finished_at": run["finished_at"],
    }


def trial_detail(conn, root: Path, trial_id: str) -> dict:
    """The full evidence chain of one trial, for rendering only."""
    row = conn.execute(f"{TRIAL_SELECT} WHERE t.id = ?", (trial_id,)).fetchone()
    if row is None:
        raise AppError(404, "not_found", f"trial {trial_id} does not exist")
    submission = conn.execute(
        "SELECT receipt_id, status, created_at FROM submissions WHERE trial_id = ?", (trial_id,)
    ).fetchone()
    sealed = conn.execute("SELECT * FROM sealed_answers WHERE trial_id = ?", (trial_id,)).fetchone()
    manifest = json.loads(sealed["manifest"]) if sealed else None
    patch_available = bool(sealed) and (root / "runs" / sealed["run_id"] / "diagnostics.patch").is_file()
    verifications = list_verifications(conn, trial_id)
    timeline = [
        ("任务打开", row["opened_at"]),
        ("提交意图", submission["created_at"] if submission else None),
        ("封存触发", sealed["trigger_at"] if sealed else None),
        ("冻结（提交暂停）", sealed["frozen_at"] if sealed else None),
        ("复制完成", sealed["copied_at"] if sealed else None),
        ("发布", sealed["published_at"] if sealed else None),
        ("封存注册", sealed["registered_at"] if sealed else None),
    ]
    return {
        "id": trial_id,
        "experiment_id": row["experiment_id"],
        "status": row["status"],
        "plan_order": row["plan_order"],
        "repetition": row["repetition"],
        "task": {"name": row["task_name"], "version": row["task_version"]},
        "config": {"name": row["config_name"], "version": row["config_version"]},
        "requested": json.dumps(json.loads(row["requested"]), ensure_ascii=False, indent=2),
        # eligibility is displayed from the stored facts, never recomputed
        "seal": dict(sealed) if sealed else None,
        # the current schema records no human-assistance flag: it stays
        # explicitly unknown, never inferred as "none"
        "human_assistance": "未知（当前 schema 未记录人工辅助标志）",
        "manifest_files": manifest["files"] if manifest else [],
        "manifest_changes": manifest.get("changes") if manifest else None,
        "runs": [_run_view(r) for r in (runs.run_out(r) for r in runs.list_runs(conn, trial_id))],
        "submission": dict(submission) if submission else None,
        "timeline": timeline,
        "verifications": verifications,
        "patch_available": patch_available,
    }


def register_routes(app: FastAPI, conn, root: Path) -> None:
    @app.get("/dashboard")
    async def dashboard_home(request: Request):
        return templates.TemplateResponse(
            request, "batch_list.html", {"rows": batch_rows(conn)}
        )

    @app.get("/dashboard/experiments/{experiment_id}")
    async def dashboard_experiment(request: Request, experiment_id: str):
        experiment = get_experiment(conn, experiment_id)  # same query service as the API
        return templates.TemplateResponse(
            request, "batch_detail.html",
            {"experiment": experiment, "trials": trial_summary(conn, experiment_id)},
        )

    @app.get("/dashboard/trials/{trial_id}")
    async def dashboard_trial(request: Request, trial_id: str):
        data = trial_detail(conn, root, trial_id)
        return templates.TemplateResponse(request, "trial_detail.html", {"t": data})

    # artifact reads address database IDs only (#18): the manifest is served
    # from the sealed_answers row; a host path in the URL can never reach disk
    @app.get("/dashboard/artifacts/answers/{digest}/manifest")
    async def dashboard_manifest(digest: str):
        if not _DIGEST_RE.fullmatch(digest):
            raise AppError(404, "not_found", "no sealed answer with this id")
        row = conn.execute(
            "SELECT manifest FROM sealed_answers WHERE digest = ?", (digest,)
        ).fetchone()
        if row is None:
            raise AppError(404, "not_found", "no sealed answer with this id")
        return Response(
            content=row["manifest"], media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="manifest-{digest[:12]}.json"'},
        )

    @app.get("/dashboard/artifacts/trials/{trial_id}/patch")
    async def dashboard_patch(trial_id: str):
        sealed = conn.execute(
            "SELECT run_id FROM sealed_answers WHERE trial_id = ?", (trial_id,)
        ).fetchone()
        if sealed is None:
            raise AppError(404, "not_found", "no sealed answer for this trial")
        path = (root / "runs" / sealed["run_id"] / "diagnostics.patch").resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise AppError(404, "not_found", "diagnostic patch not available")
        return FileResponse(
            path, media_type="text/x-diff", filename="diagnostics.patch",
        )
