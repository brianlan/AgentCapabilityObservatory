import logging
import sqlite3
import uuid

TASK_CONTENT = {"prompt": "What is 2+2?", "expected_answer": "4"}
MGMT_TOKEN = "test-management-token"

# every management-surface operation; the session surface must expose none
MGMT_OPERATIONS = [
    ("post", "/v1/versions"),
    ("post", "/v1/experiments"),
    ("get", "/v1/trials/{trial_id}"),
    ("get", "/v1/trials/{trial_id}/runs"),
    ("post", "/v1/trials/{trial_id}/session-token"),
    ("post", "/v1/trials/{trial_id}/verifications"),
    ("get", "/v1/trials/{trial_id}/verifications"),
    ("post", "/v1/experiments/{experiment_id}/cancel"),
    ("post", "/v1/experiments/{experiment_id}/resume"),
    ("get", "/v1/experiments/{experiment_id}"),
    ("get", "/v1/results"),
    ("get", "/dashboard"),
]


def setup_experiment(client, register, task_name="arith", config_name="cfg-a"):
    register("task", task_name, "v1", dict(TASK_CONTENT, prompt=f"task {task_name}"))
    register("config", config_name, "v1", {"harness": "opencode", "model": "model-a"})
    experiment = client.post("/v1/experiments", json={
        "task": {"name": task_name, "version": "v1"},
        "targets": [{"name": config_name, "version": "v1"}],
    }).json()
    return experiment


def setup_trial(client, register, task_name="arith", config_name="cfg-a"):
    """A planned trial plus its minted session capability (management side)."""
    experiment = setup_experiment(client, register, task_name, config_name)
    trial_id = experiment["trials"][0]["id"]
    resp = client.post(f"/v1/trials/{trial_id}/session-token")
    assert resp.status_code == 201, resp.text
    return trial_id, resp.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def plant_sealed_answer(client, tmp_path, trial_id, status="sealed"):
    """Register a final answer state directly (the supervisor does this in
    production); final state must end the session capability."""
    from aco import db, runs

    conn = db.connect(tmp_path / "aco.db")
    run_id = runs.create_run(conn, trial_id, {}, supervisor_pid=-1)
    conn.execute(
        "INSERT INTO sealed_answers (trial_id, run_id, receipt_id, digest, manifest,"
        " seal_trigger, trigger_at, frozen_at, copied_at, published_at, registered_at, status)"
        " VALUES (?, ?, ?, ?, '{}', 'submit', 'now', 'now', 'now', 'now', 'now', ?)",
        (trial_id, run_id, uuid.uuid4().hex, uuid.uuid4().hex, status),
    )
    conn.commit()
    conn.close()


def test_mint_stores_only_token_digest(client, register, tmp_path):
    trial_id, token = setup_trial(client, register)
    assert token
    row = sqlite3.connect(tmp_path / "aco.db").execute(
        "SELECT session_token_digest, session_token_expires_at FROM trials WHERE id = ?",
        (trial_id,),
    ).fetchone()
    assert row[0] != token and len(row[0]) == 64
    assert row[1]


def test_claim_returns_public_task_and_records_opened_once(client, register, session_client):
    trial_id, token = setup_trial(client, register)
    first = session_client.get("/v1/session/task", headers=auth(token))
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["trial_id"] == trial_id
    assert body["instruction"] == f"task arith"
    assert "expected_answer" not in first.text and "4" != body["instruction"]
    assert body["opened_at"]
    second = session_client.get("/v1/session/task", headers=auth(token))
    assert second.json()["opened_at"] == body["opened_at"]  # never reset
    assert second.json()["task"] == body["task"]


def test_missing_invalid_and_expired_tokens_rejected(client, register, session_client, tmp_path):
    setup_trial(client, register)
    assert session_client.get("/v1/session/task").status_code == 401
    assert session_client.get("/v1/session/task", headers=auth("not-a-token")).status_code == 401
    conn = sqlite3.connect(tmp_path / "aco.db")
    conn.execute("UPDATE trials SET session_token_expires_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    _, token = setup_trial(client, register, task_name="arith2", config_name="cfg-b")
    assert session_client.get("/v1/session/task", headers=auth(token)).status_code == 200
    # expire the second trial's token explicitly
    conn.execute(
        "UPDATE trials SET session_token_expires_at = '2000-01-01T00:00:00+00:00'"
        " WHERE task_version_id = (SELECT id FROM versions WHERE name = 'arith2')"
    )
    conn.commit()
    assert session_client.get("/v1/session/task", headers=auth(token)).status_code == 401


def test_cross_trial_isolation_and_no_enumeration(client, register, session_client):
    trial_a, token_a = setup_trial(client, register, task_name="task-a")
    trial_b, token_b = setup_trial(client, register, task_name="task-b", config_name="cfg-z")
    body = session_client.get("/v1/session/task", headers=auth(token_a)).json()
    assert body["trial_id"] == trial_a
    assert "task-b" not in str(body)
    # session API takes no trial identifier: the other trial is not addressable
    for path in ("/v1/session/task", "/v1/session/submission"):
        assert f"trial_id={trial_b}" not in path
    assert session_client.get(
        f"/v1/session/trials/{trial_b}/submit", headers=auth(token_a)
    ).status_code == 404
    other = session_client.post("/v1/session/submit", headers=auth(token_b),
                                json={"idempotency_key": "k"})
    assert other.status_code == 202 and other.json()["receipt_id"]
    still_a = session_client.get("/v1/session/submission", headers=auth(token_a))
    assert still_a.status_code == 404  # B's intent never leaks into A's session


def test_submit_intent_only_idempotency_matrix(client, register, session_client):
    _, token = setup_trial(client, register)
    payload = {"idempotency_key": "k1"}
    first = session_client.post("/v1/session/submit", headers=auth(token), json=payload)
    assert first.status_code == 202, first.text
    receipt = first.json()["receipt_id"]
    assert first.json()["status"] == "accepted"
    retry = session_client.post("/v1/session/submit", headers=auth(token), json=payload)
    assert retry.status_code == 200 and retry.json()["receipt_id"] == receipt
    other_key = session_client.post("/v1/session/submit", headers=auth(token),
                                    json={"idempotency_key": "k2"})
    assert other_key.status_code == 409
    assert other_key.json()["error"]["code"] == "already_submitted"


def test_submit_body_with_answer_rejected_by_schema(client, register, session_client):
    """The official answer is the sealed workspace, never a session-supplied
    body (#12 reopen): an `answer` field is a schema violation, not data."""
    _, token = setup_trial(client, register)
    rejected = session_client.post("/v1/session/submit", headers=auth(token),
                                   json={"answer": {"text": "4"}, "idempotency_key": "k9"})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "validation_error"
    # the rejected request stored nothing: an intent-only retry still works
    accepted = session_client.post("/v1/session/submit", headers=auth(token),
                                   json={"idempotency_key": "k9"})
    assert accepted.status_code == 202


def test_submission_status_endpoint(client, register, session_client):
    _, token = setup_trial(client, register)
    assert session_client.get("/v1/session/submission", headers=auth(token)).status_code == 404
    receipt = session_client.post("/v1/session/submit", headers=auth(token),
                                  json={"idempotency_key": "k1"}).json()["receipt_id"]
    body = session_client.get("/v1/session/submission", headers=auth(token)).json()
    assert body["receipt_id"] == receipt and body["status"] == "accepted"
    assert "answer" not in body  # no answer, score, or hidden feedback


def test_no_token_leakage_in_logs_or_errors(client, register, session_client, caplog):
    with caplog.at_level(logging.INFO, logger="aco"):
        _, token = setup_trial(client, register)
        session_client.get("/v1/session/task", headers=auth(token))
        session_client.get("/v1/session/task", headers=auth("forged-token-value"))
        accepted = session_client.post("/v1/session/submit", headers=auth(token),
                                       json={"idempotency_key": "k1"})
        rejected = session_client.post("/v1/session/submit", headers=auth(token),
                                       json={"answer": {"secret": "forty-two"}, "idempotency_key": "k2"})
    assert accepted.status_code == 202 and rejected.status_code == 422
    log_text = " ".join(r.getMessage() for r in caplog.records)
    assert token not in log_text and "forged-token-value" not in log_text
    assert "forty-two" not in log_text  # no rejected answer payload in logs
    assert token not in rejected.text and "secret" not in rejected.text


def test_session_surface_exposes_no_management_operation(client, register, session_client):
    """ADR 0001: two separate surfaces. A valid session token finds no
    management route on the session listener; the management listener has no
    session routes at all."""
    experiment = setup_experiment(client, register, "iso-task", "iso-cfg")
    trial_id = experiment["trials"][0]["id"]
    _, token = setup_trial(client, register, task_name="iso2-task", config_name="iso2-cfg")
    for method, path in MGMT_OPERATIONS:
        req_path = (path.format(trial_id=trial_id, experiment_id=experiment["id"]))
        kwargs = {"json": {}} if method == "post" else {}
        resp = getattr(session_client, method)(req_path, headers=auth(token), **kwargs)
        assert resp.status_code == 404, f"{method.upper()} {req_path}: {resp.status_code}"
    # and vice versa: the management listener has no session operations
    assert client.get("/v1/session/task").status_code == 404
    assert client.post("/v1/session/submit", json={"idempotency_key": "k"}).status_code == 404
    assert client.get("/v1/session/submission").status_code == 404


def test_management_surface_rejects_other_credentials(client, register, session_client, tmp_path):
    """No token, a session token, or a wrong management token can call any
    management operation (#12 reopen): server-side auth is enforced, not
    ignored."""
    from fastapi.testclient import TestClient

    from aco.app import create_management_app
    experiment = setup_experiment(client, register, "auth-task", "auth-cfg")
    trial_id = experiment["trials"][0]["id"]
    _, session_token = setup_trial(client, register, task_name="auth2-task", config_name="auth2-cfg")
    bare = TestClient(create_management_app(data_root=str(tmp_path), token=MGMT_TOKEN))
    for headers in ({}, auth(session_token), {"Authorization": "Bearer wrong-management-token"}):
        for method, path in MGMT_OPERATIONS:
            req_path = path.format(trial_id=trial_id, experiment_id=experiment["id"])
            kwargs = {"json": {}} if method == "post" else {}
            resp = getattr(bare, method)(req_path, headers=headers, **kwargs)
            assert resp.status_code == 401, f"{method.upper()} {req_path}: {resp.status_code}"


def test_healthz_stays_open_for_liveness(client):
    from fastapi.testclient import TestClient

    from aco.app import create_management_app
    bare = TestClient(create_management_app(data_root=None, token="test-management-token"))
    assert bare.get("/healthz").status_code == 200  # no Authorization header sent
    assert bare.get("/v1/experiments/nope").status_code == 401


def test_finished_trials_cannot_mint_a_capability(client, register, tmp_path):
    """cancelled, sealed, and anomalous trials are final: no new session
    token (#12 reopen)."""
    # cancelled via the real lifecycle path
    cancelled = setup_experiment(client, register, "mint-cancel", "mint-cfg")
    client.post(f"/v1/experiments/{cancelled['id']}/cancel")
    resp = client.post(f"/v1/trials/{cancelled['trials'][0]['id']}/session-token")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "trial_not_runnable"

    # sealed and anomalous via the supervisor's registration tables
    for status in ("sealed", "anomaly"):
        trial_id, _ = setup_trial(client, register, f"mint-{status}", f"mint-{status}-cfg")
        plant_sealed_answer(client, tmp_path, trial_id, status=status)
        resp = client.post(f"/v1/trials/{trial_id}/session-token")
        assert resp.status_code == 409 and resp.json()["error"]["code"] == "trial_not_runnable"


def test_capability_expires_when_trial_finishes(client, register, session_client, tmp_path):
    """A token that outlives its trial is dead: no late task reads, no late
    submission (not even an idempotent replay), no status reads (#12 reopen,
    ADR 0001)."""
    trial_id, token = setup_trial(client, register)
    assert session_client.get("/v1/session/task", headers=auth(token)).status_code == 200
    intent = session_client.post("/v1/session/submit", headers=auth(token),
                                 json={"idempotency_key": "k1"})
    assert intent.status_code == 202
    plant_sealed_answer(client, tmp_path, trial_id, status="sealed")

    for method, path, payload in (
        ("get", "/v1/session/task", None),
        ("post", "/v1/session/submit", {"idempotency_key": "k1"}),  # same key replay
        ("post", "/v1/session/submit", {"idempotency_key": "k2"}),  # new intent
        ("get", "/v1/session/submission", None),
    ):
        kwargs = {"json": payload} if payload is not None else {}
        resp = getattr(session_client, method)(path, headers=auth(token), **kwargs)
        assert resp.status_code == 401, f"{method.upper()} {path}: {resp.status_code}"
        assert resp.json()["error"]["code"] == "session_expired"


def _trial_token_for_existing(client, task_name, config_name):
    """Mint a session capability for already-registered versions (no
    re-registration: same name@version with different content conflicts)."""
    experiment = client.post("/v1/experiments", json={
        "task": {"name": task_name, "version": "v1"},
        "targets": [{"name": config_name, "version": "v1"}],
    }).json()
    trial_id = experiment["trials"][0]["id"]
    resp = client.post(f"/v1/trials/{trial_id}/session-token")
    assert resp.status_code == 201, resp.text
    return trial_id, resp.json()["token"]


def test_instruction_served_from_registered_environment_asset(client, register,
                                                              session_client, tmp_path):
    """When the task version declares an instruction asset, the Session API
    serves the instruction from the registered immutable public asset, not
    from a prompt field (#20 reopen)."""
    from aco import environments
    from aco.verification import runner

    env_dir = tmp_path / "env-src"
    (env_dir / "workspace").mkdir(parents=True)
    (env_dir / "workspace" / "README.md").write_text("FAKE:submit\nfrom the registered asset\n")
    digest = runner.bundle_digest(env_dir)
    environments.publish(tmp_path, env_dir, digest)

    register("task", "asset-task", "v1",
             {"instruction": {"asset": "environment", "path": "workspace/README.md"}},
             assets=[{"name": "environment", "digest": digest}])
    register("config", "asset-cfg", "v1", {"harness": "fake", "model": "none"})
    _, token = _trial_token_for_existing(client, "asset-task", "asset-cfg")

    body = session_client.get("/v1/session/task", headers=auth(token))
    assert body.status_code == 200, body.text
    assert body.json()["instruction"] == "FAKE:submit\nfrom the registered asset\n"


def test_broken_instruction_source_is_refused_not_emptied(client, register,
                                                          session_client):
    """A version with neither an instruction asset nor a prompt has no usable
    instruction source: the Session API refuses — it never serves an empty
    prompt (#20 reopen)."""
    register("task", "broken-task", "v1", {"expected_answer": "4"})
    register("config", "broken-cfg", "v1", {"harness": "fake", "model": "none"})
    _, token = _trial_token_for_existing(client, "broken-task", "broken-cfg")

    resp = session_client.get("/v1/session/task", headers=auth(token))
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "task_environment_invalid"


def test_tampered_environment_asset_is_refused(client, register, session_client,
                                               tmp_path):
    """A store entry that no longer matches the registered digest is
    refused at session time — the agent never receives unverified bytes
    (#20 reopen)."""
    from aco import environments
    from aco.verification import runner

    env_dir = tmp_path / "env-src"
    (env_dir / "workspace").mkdir(parents=True)
    (env_dir / "workspace" / "README.md").write_text("FAKE:submit\noriginal\n")
    digest = runner.bundle_digest(env_dir)
    environments.publish(tmp_path, env_dir, digest)
    (environments.store_root(tmp_path) / digest / "workspace" / "README.md").write_text("tampered")

    register("task", "tampered-task", "v1",
             {"instruction": {"asset": "environment", "path": "workspace/README.md"}},
             assets=[{"name": "environment", "digest": digest}])
    register("config", "tampered-cfg", "v1", {"harness": "fake", "model": "none"})
    _, token = _trial_token_for_existing(client, "tampered-task", "tampered-cfg")

    resp = session_client.get("/v1/session/task", headers=auth(token))
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "task_environment_invalid"
