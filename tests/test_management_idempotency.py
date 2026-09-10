"""Server-authoritative idempotent Experiment creation (#35).

One (management principal, Idempotency-Key) maps to exactly one plan: the
same key + canonical body replays the original experiment (200), a different
body under the same key is a 409, concurrent requests cannot each create a
plan, and the plaintext credential never enters digests, logs, or responses.
"""

import json
import sqlite3
import threading
import time
from urllib.request import Request, urlopen

import pytest
import uvicorn
from fastapi.testclient import TestClient

from aco.app import create_management_app

MGMT_TOKEN = "test-management-token"
MGMT_AUTH = {"Authorization": f"Bearer {MGMT_TOKEN}"}
TASK_CONTENT = {"prompt": "P", "tests": []}
CONFIG_CONTENT = {"harness": "fake", "model": "fake-model"}


@pytest.fixture
def register_exp(client, register):
    """One task + one config registered; returns a minimal experiment body."""
    register("task", "i-task", "v1", dict(TASK_CONTENT))
    register("config", "i-cfg", "v1", dict(CONFIG_CONTENT))
    return {"task": {"name": "i-task", "version": "v1"},
            "targets": [{"name": "i-cfg", "version": "v1"}], "repetitions": 1}


def test_same_key_serial_replay_returns_same_experiment(client, register_exp, tmp_path):
    body = register_exp
    first = client.post("/v1/experiments", json=body, headers={"Idempotency-Key": "k"})
    assert first.status_code == 202, first.text
    replay = client.post("/v1/experiments", json=body, headers={"Idempotency-Key": "k"})
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert [t["id"] for t in replay.json()["trials"]] == [t["id"] for t in first.json()["trials"]]

    # exactly one experiment, one trial set, one key row tied to it
    conn = sqlite3.connect(tmp_path / "aco.db")
    assert conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 1
    row = conn.execute(
        "SELECT request_digest, experiment_id, created_at FROM management_idempotency"
    ).fetchone()
    assert row[1] == first.json()["id"] and row[0] and row[2]


@pytest.mark.parametrize("mutation", [
    {"repetitions": 2},
    {"targets": [{"name": "i-cfg-2", "version": "v1"}]},
])
def test_same_key_different_request_conflicts(client, register, register_exp, mutation):
    register("config", "i-cfg-2", "v1", dict(CONFIG_CONTENT))
    body = register_exp
    client.post("/v1/experiments", json=body, headers={"Idempotency-Key": "k"})
    resp = client.post("/v1/experiments", json=dict(body, **mutation),
                       headers={"Idempotency-Key": "k"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "idempotency_conflict"


def test_without_key_every_call_creates_new_plan(client, register_exp):
    # no key: the API never pretends the call is idempotent (#35 contract)
    a = client.post("/v1/experiments", json=register_exp)
    b = client.post("/v1/experiments", json=register_exp)
    assert a.status_code == 202 and b.status_code == 202
    assert a.json()["id"] != b.json()["id"]


def test_different_principals_use_same_key_independently(tmp_path):
    root = str(tmp_path)
    client_a = TestClient(create_management_app(data_root=root, token="token-A"),
                          headers={"Authorization": "Bearer token-A"})
    client_b = TestClient(create_management_app(data_root=root, token="token-B"),
                          headers={"Authorization": "Bearer token-B"})
    for kind, name, content in [("task", "p-task", TASK_CONTENT), ("config", "p-cfg", CONFIG_CONTENT)]:
        resp = client_a.post("/v1/versions", json={
            "kind": kind, "name": name, "version": "v1", "content": content, "assets": []})
        assert resp.status_code == 201, resp.text
    body = {"task": {"name": "p-task", "version": "v1"},
            "targets": [{"name": "p-cfg", "version": "v1"}], "repetitions": 1}

    ra = client_a.post("/v1/experiments", json=body, headers={"Idempotency-Key": "shared"})
    rb = client_b.post("/v1/experiments", json=body, headers={"Idempotency-Key": "shared"})
    assert ra.status_code == 202 and rb.status_code == 202, (ra.text, rb.text)
    assert ra.json()["id"] != rb.json()["id"]

    conn = sqlite3.connect(tmp_path / "aco.db")
    assert conn.execute("SELECT COUNT(DISTINCT principal) FROM management_idempotency").fetchone()[0] == 2
    # the stored principal is a digest, never the bearer itself
    principals = [r[0] for r in conn.execute("SELECT principal FROM management_idempotency")]
    assert "token-A" not in principals and "token-B" not in principals


def test_concurrent_same_key_creates_exactly_one_plan(tmp_path):
    """Real HTTP concurrency: two simultaneous creations under one key must
    yield one plan — via replay or the UNIQUE(principal, key) race guard."""
    root = str(tmp_path)
    app = create_management_app(data_root=root, token=MGMT_TOKEN)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        while not server.started:
            time.sleep(0.01)
        url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"

        for kind, name, content in [("task", "c-task", TASK_CONTENT), ("config", "c-cfg", CONFIG_CONTENT)]:
            body = json.dumps({"kind": kind, "name": name, "version": "v1",
                               "content": content, "assets": []}).encode()
            req = Request(f"{url}/v1/versions", data=body, method="POST",
                          headers={"Content-Type": "application/json", **MGMT_AUTH})
            assert urlopen(req).status == 201
        plan = json.dumps({"task": {"name": "c-task", "version": "v1"},
                           "targets": [{"name": "c-cfg", "version": "v1"}],
                           "repetitions": 1}).encode()

        barrier = threading.Barrier(2)
        results, errors = [], []

        def fire():
            try:
                barrier.wait(timeout=10)
                req = Request(f"{url}/v1/experiments", data=plan, method="POST",
                              headers={"Content-Type": "application/json",
                                       "Idempotency-Key": "race", **MGMT_AUTH})
                with urlopen(req) as resp:
                    results.append((resp.status, json.loads(resp.read())))
            except Exception as exc:  # surfaced below for a clear failure
                errors.append(exc)

        threads = [threading.Thread(target=fire) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert not errors, errors

        assert {r[1]["id"] for r in results} == {results[0][1]["id"]}
        assert {r[0] for r in results} <= {200, 202}
        conn = sqlite3.connect(tmp_path / "aco.db")
        assert conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM management_idempotency").fetchone()[0] == 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)
