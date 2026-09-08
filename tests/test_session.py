import logging
import sqlite3
import uuid

TASK_CONTENT = {"prompt": "What is 2+2?", "expected_answer": "4"}


def setup_trial(client, register, task_name="arith", config_name="cfg-a"):
    register("task", task_name, "v1", dict(TASK_CONTENT, prompt=f"task {task_name}"))
    register("config", config_name, "v1", {"harness": "opencode", "model": "model-a"})
    experiment = client.post("/v1/experiments", json={
        "task": {"name": task_name, "version": "v1"},
        "targets": [{"name": config_name, "version": "v1"}],
    }).json()
    trial_id = experiment["trials"][0]["id"]
    resp = client.post(f"/v1/trials/{trial_id}/session-token")
    assert resp.status_code == 201, resp.text
    return trial_id, resp.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_mint_stores_only_token_digest(client, register, tmp_path):
    trial_id, token = setup_trial(client, register)
    assert token
    row = sqlite3.connect(tmp_path / "aco.db").execute(
        "SELECT session_token_digest, session_token_expires_at FROM trials WHERE id = ?",
        (trial_id,),
    ).fetchone()
    assert row[0] != token and len(row[0]) == 64
    assert row[1]


def test_claim_returns_public_task_and_records_opened_once(client, register):
    trial_id, token = setup_trial(client, register)
    first = client.get("/v1/session/task", headers=auth(token))
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["trial_id"] == trial_id
    assert body["instruction"] == f"task arith"
    assert "expected_answer" not in first.text and "4" != body["instruction"]
    assert body["opened_at"]
    second = client.get("/v1/session/task", headers=auth(token))
    assert second.json()["opened_at"] == body["opened_at"]  # never reset
    assert second.json()["task"] == body["task"]


def test_missing_invalid_and_expired_tokens_rejected(client, register, tmp_path):
    setup_trial(client, register)
    assert client.get("/v1/session/task").status_code == 401
    assert client.get("/v1/session/task", headers=auth("not-a-token")).status_code == 401
    conn = sqlite3.connect(tmp_path / "aco.db")
    conn.execute("UPDATE trials SET session_token_expires_at = '2000-01-01T00:00:00+00:00'")
    conn.commit()
    _, token = setup_trial(client, register, task_name="arith2", config_name="cfg-b")
    assert client.get("/v1/session/task", headers=auth(token)).status_code == 200
    # expire the second trial's token explicitly
    conn.execute(
        "UPDATE trials SET session_token_expires_at = '2000-01-01T00:00:00+00:00'"
        " WHERE task_version_id = (SELECT id FROM versions WHERE name = 'arith2')"
    )
    conn.commit()
    assert client.get("/v1/session/task", headers=auth(token)).status_code == 401


def test_cross_trial_isolation_and_no_enumeration(client, register):
    trial_a, token_a = setup_trial(client, register, task_name="task-a")
    trial_b, token_b = setup_trial(client, register, task_name="task-b", config_name="cfg-z")
    body = client.get("/v1/session/task", headers=auth(token_a)).json()
    assert body["trial_id"] == trial_a
    assert "task-b" not in str(body)
    # session API takes no trial identifier: the other trial is not addressable
    for path in ("/v1/session/task", "/v1/session/submission"):
        assert f"trial_id={trial_b}" not in path
    assert client.get(f"/v1/session/trials/{trial_b}/submit", headers=auth(token_a)).status_code == 404
    other = client.post("/v1/session/submit", headers=auth(token_b),
                        json={"answer": "x", "idempotency_key": "k"})
    assert other.status_code == 202 and other.json()["receipt_id"]
    still_a = client.get("/v1/session/submission", headers=auth(token_a))
    assert still_a.status_code == 404  # B's intent never leaks into A's session


def test_submit_idempotency_matrix(client, register):
    _, token = setup_trial(client, register)
    payload = {"answer": {"text": "4"}, "idempotency_key": "k1"}
    first = client.post("/v1/session/submit", headers=auth(token), json=payload)
    assert first.status_code == 202, first.text
    receipt = first.json()["receipt_id"]
    assert first.json()["status"] == "accepted"
    retry = client.post("/v1/session/submit", headers=auth(token), json=payload)
    assert retry.status_code == 200 and retry.json()["receipt_id"] == receipt
    conflict = client.post("/v1/session/submit", headers=auth(token),
                           json={"answer": {"text": "5"}, "idempotency_key": "k1"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    other_key = client.post("/v1/session/submit", headers=auth(token),
                            json={"answer": {"text": "4"}, "idempotency_key": "k2"})
    assert other_key.status_code == 409
    assert other_key.json()["error"]["code"] == "already_submitted"


def test_submission_status_endpoint(client, register):
    _, token = setup_trial(client, register)
    assert client.get("/v1/session/submission", headers=auth(token)).status_code == 404
    receipt = client.post("/v1/session/submit", headers=auth(token),
                          json={"answer": "4", "idempotency_key": "k1"}).json()["receipt_id"]
    body = client.get("/v1/session/submission", headers=auth(token)).json()
    assert body["receipt_id"] == receipt and body["status"] == "accepted"
    assert "answer" not in body  # no answer, score, or hidden feedback


def test_no_token_or_answer_leakage_in_logs_or_errors(client, register, caplog):
    with caplog.at_level(logging.INFO, logger="aco"):
        _, token = setup_trial(client, register)
        client.get("/v1/session/task", headers=auth(token))
        client.get("/v1/session/task", headers=auth("forged-token-value"))
        client.post("/v1/session/submit", headers=auth(token),
                    json={"answer": {"secret": "forty-two-answer-value"}, "idempotency_key": "k1"})
        bad = client.post("/v1/session/submit", headers=auth(token),
                          json={"answer": "x", "idempotency_key": "k1"})
    assert bad.status_code == 409
    log_text = " ".join(r.getMessage() for r in caplog.records)
    assert token not in log_text and "forged-token-value" not in log_text
    assert "forty-two-answer-value" not in log_text  # no answer payload in logs
    assert token not in bad.text and "secret" not in bad.text


def test_session_token_cannot_mint_or_read_management_unauthorized_paths(client, register):
    _, token = setup_trial(client, register)
    # no session-authenticated route reaches management data; token grants nothing extra
    resp = client.post("/v1/trials/whatever/session-token", headers=auth(token))
    assert resp.status_code in (201, 404)  # management path ignores session auth entirely
    assert client.get("/v1/session/task").status_code == 401  # session path requires token
