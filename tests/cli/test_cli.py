"""CLI tests (#17): run against a real API server on a local port.

Covers argument→payload mapping, register/run/status/cancel/resume success
and HTTP errors, the --wait Ctrl-C regression (watcher stops, no cancel
request is ever sent), and credential-free stable JSON output.
"""

import json
import os
import threading
import time

import pytest
import uvicorn

from aco.app import create_management_app
from aco.cli import main

MGMT_TOKEN = "test-management-token"
MGMT_AUTH = {"Authorization": f"Bearer {MGMT_TOKEN}"}
os.environ.setdefault("ACO_MANAGEMENT_TOKEN", MGMT_TOKEN)  # the CLI resolves its token from the env


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    """One live management API for the whole module (real SQLite, real HTTP)."""
    root = tmp_path_factory.mktemp("aco-cli-api")
    app = create_management_app(data_root=str(root), token=MGMT_TOKEN)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ACO_CLI_STATE", str(tmp_path / "cli.json"))
    return tmp_path / "cli.json"


def write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(json.dumps(content))
    return str(path)


def test_register_and_run_map_args_to_payload(api, state, tmp_path, capsys):
    task_file = write(tmp_path, "task.json", {"prompt": "Do the thing", "tests": []})
    config_file = write(tmp_path, "config.json", {"harness": "fake", "model": "fake-model"})
    assert main(["register", "task", "demo-task", "v1", task_file, "--api-url", api]) == 0
    assert main(["register", "config", "demo-cfg", "v1", config_file, "--api-url", api]) == 0
    # idempotent re-registration of identical content succeeds
    assert main(["register", "task", "demo-task", "v1", task_file, "--api-url", api]) == 0

    assert main(["run", "--task", "demo-task@v1", "--target", "demo-cfg@v1",
                 "--repetitions", "3", "--api-url", api]) == 0
    out = capsys.readouterr().out
    assert "计划样本数: 3" in out
    experiment_id = out.split("Experiment ")[1].split("（")[0]

    # the created plan is server-readable and matches the CLI arguments
    from urllib.request import Request, urlopen
    doc = json.loads(urlopen(
        Request(f"{api}/v1/experiments/{experiment_id}", headers=MGMT_AUTH)).read())
    assert doc["requested"]["task"] == {"name": "demo-task", "version": "v1"}
    assert doc["requested"]["targets"] == [{"name": "demo-cfg", "version": "v1"}]
    assert doc["requested"]["repetitions"] == 3
    assert len(doc["trials"]) == 3


def test_run_with_suite_and_idempotency_key_replays(api, state, tmp_path, capsys):
    write(tmp_path, "task.json", {"prompt": "T", "tests": []})
    write(tmp_path, "config.json", {"harness": "fake", "model": "fake-model"})
    assert main(["register", "task", "s-task", "v1", str(tmp_path / "task.json"), "--api-url", api]) == 0
    assert main(["register", "suite", "s-suite", "v1",
                 write(tmp_path, "suite.json", {"tasks": [{"name": "s-task", "version": "v1"}]}),
                 "--api-url", api]) == 0
    assert main(["register", "config", "s-cfg", "v1", str(tmp_path / "config.json"), "--api-url", api]) == 0

    common = ["run", "--suite", "s-suite@v1", "--target", "s-cfg@v1", "--api-url", api]
    assert main([*common, "--idempotency-key", "key-1"]) == 0
    first = capsys.readouterr().out
    assert "计划样本数: ≥ 1" in first
    exp_id = first.split("Experiment ")[1].split("（")[0]

    assert main([*common, "--idempotency-key", "key-1"]) == 0
    second = capsys.readouterr().out
    assert f"已存在（幂等键 key-1）：Experiment {exp_id}" in second

    # a different key creates a distinct experiment
    assert main([*common, "--idempotency-key", "key-2"]) == 0
    other = capsys.readouterr().out.split("Experiment ")[1].split("（")[0]
    assert other != exp_id


def test_run_replays_from_server_after_state_file_loss(api, state, tmp_path, capsys):
    """#35: the server owns idempotency — losing the local ledger must not
    duplicate the batch, and the same key with a different body must conflict."""
    write(tmp_path, "task.json", {"prompt": "T", "tests": []})
    write(tmp_path, "config.json", {"harness": "fake", "model": "fake-model"})
    assert main(["register", "task", "sl-task", "v1", str(tmp_path / "task.json"), "--api-url", api]) == 0
    assert main(["register", "config", "sl-cfg", "v1", str(tmp_path / "config.json"), "--api-url", api]) == 0

    common = ["run", "--task", "sl-task@v1", "--target", "sl-cfg@v1", "--api-url", api,
              "--idempotency-key", "lost"]
    assert main(common) == 0
    first = capsys.readouterr().out
    exp_id = first.split("Experiment ")[1].split("（")[0]

    state.unlink()  # ledger gone: the server is now the only idempotency authority
    assert main(common) == 0
    second = capsys.readouterr().out
    assert f"Experiment {exp_id}" in second
    assert second.split("Experiment ")[1].split("（")[0] == exp_id

    # same key, different request: the server answers 409, the CLI exits 1.
    # (the replay above re-saved the ledger; drop it again so the cache does
    # not short-circuit before the server ever sees the key)
    state.unlink()
    conflict = ["run", "--task", "sl-task@v1", "--target", "sl-cfg@v1",
                "--repetitions", "2", "--api-url", api, "--idempotency-key", "lost"]
    assert main(conflict) == 1
    assert "idempotency_conflict" in capsys.readouterr().err


def test_status_human_and_json(api, state, capsys):
    from urllib.request import Request, urlopen

    body = json.dumps({"task": {"name": "s-task", "version": "v1"},
                       "targets": [{"name": "s-cfg", "version": "v1"}], "repetitions": 1}).encode()
    req = Request(f"{api}/v1/experiments", data=body, method="POST",
                  headers={"Content-Type": "application/json", **MGMT_AUTH})
    exp_id = json.loads(urlopen(req).read())["id"]

    assert main(["status", exp_id, "--api-url", api]) == 0
    human = capsys.readouterr().out
    assert f"Experiment {exp_id}" in human and "计划 1" in human

    assert main(["status", exp_id, "--api-url", api, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["id"] == exp_id
    assert {"planned", "claimed", "cancelled", "sealed", "anomaly", "attempted"} <= set(doc["progress"])


def test_http_error_exit_code_and_message(api, state, capsys):
    assert main(["status", "no-such-experiment", "--api-url", api]) == 1
    captured = capsys.readouterr()
    assert "404 [not_found]" in captured.err
    assert captured.out == ""


def test_cancel_then_resume_rejected(api, state, tmp_path, capsys):
    write(tmp_path, "t.json", {"prompt": "C", "tests": []})
    write(tmp_path, "c.json", {"harness": "fake", "model": "fake-model"})
    main(["register", "task", "c-task", "v1", str(tmp_path / "t.json"), "--api-url", api])
    main(["register", "config", "c-cfg", "v1", str(tmp_path / "c.json"), "--api-url", api])
    main(["run", "--task", "c-task@v1", "--target", "c-cfg@v1", "--api-url", api])
    exp_id = capsys.readouterr().out.split("Experiment ")[1].split("（")[0]

    assert main(["cancel", exp_id, "--api-url", api]) == 0
    summary = capsys.readouterr().out
    assert "已取消" in summary

    assert main(["resume", exp_id, "--api-url", api]) == 1
    err = capsys.readouterr().err
    assert "experiment_cancelled" in err  # resume on cancelled is a clear error

    # resume on a merely-running (not restart-paused) plan is a harmless no-op
    main(["run", "--task", "c-task@v1", "--target", "c-cfg@v1", "--api-url", api])
    other = capsys.readouterr().out.split("Experiment ")[1].split("（")[0]
    assert main(["resume", other, "--api-url", api]) == 0
    assert "'resumed': 0" in capsys.readouterr().out  # no-op on a running plan


def test_ctrl_c_stops_watcher_without_cancelling(api, state, tmp_path, capsys, monkeypatch):
    write(tmp_path, "t.json", {"prompt": "W", "tests": []})
    write(tmp_path, "c.json", {"harness": "fake", "model": "fake-model"})
    main(["register", "task", "w-task", "v1", str(tmp_path / "t.json"), "--api-url", api])
    main(["register", "config", "w-cfg", "v1", str(tmp_path / "c.json"), "--api-url", api])
    main(["run", "--task", "w-task@v1", "--target", "w-cfg@v1", "--api-url", api])
    exp_id = capsys.readouterr().out.split("Experiment ")[1].split("（")[0]

    real_sleep = time.sleep
    polls = {"n": 0}

    def interrupting_sleep(_seconds):
        polls["n"] += 1
        if polls["n"] >= 1:
            raise KeyboardInterrupt

    monkeypatch.setattr("aco.cli.time.sleep", interrupting_sleep)
    assert main(["run", "--task", "w-task@v1", "--target", "w-cfg@v1",
                 "--api-url", api, "--wait"]) == 130
    out = capsys.readouterr().out
    exp_id = out.split("Experiment ")[1].split("（")[0]
    assert f"aco cancel {exp_id}" in out  # the message points at explicit cancel

    # regression: the watcher never cancelled anything — plan is untouched
    from urllib.request import Request, urlopen
    doc = json.loads(urlopen(
        Request(f"{api}/v1/experiments/{exp_id}", headers=MGMT_AUTH)).read())
    assert doc["progress"]["planned"] == 1
    assert doc["progress"]["cancelled"] == 0
    real_sleep(0)


def test_wait_json_stdout_is_single_parseable_document(api, state, tmp_path, capsys, monkeypatch):
    """--wait --json: stdout carries exactly one JSON value even across polls
    and a Ctrl-C interruption; progress and the notice stay on stderr."""
    write(tmp_path, "t.json", {"prompt": "WJ", "tests": []})
    write(tmp_path, "c.json", {"harness": "fake", "model": "fake-model"})
    main(["register", "task", "wj-task", "v1", str(tmp_path / "t.json"), "--api-url", api])
    main(["register", "config", "wj-cfg", "v1", str(tmp_path / "c.json"), "--api-url", api])
    main(["run", "--task", "wj-task@v1", "--target", "wj-cfg@v1", "--api-url", api])
    capsys.readouterr()
    # register/run above print human lines; --wait --json itself must not

    real_sleep = time.sleep
    calls = {"n": 0}

    def one_poll_then_interrupt(_seconds):
        calls["n"] += 1
        if calls["n"] >= 2:  # first sleep passes → one real GET poll happens
            raise KeyboardInterrupt

    monkeypatch.setattr("aco.cli.time.sleep", one_poll_then_interrupt)
    assert main(["run", "--task", "wj-task@v1", "--target", "wj-cfg@v1",
                 "--api-url", api, "--wait", "--json"]) == 130
    captured = capsys.readouterr()
    doc = json.loads(captured.out)  # the COMPLETE stdout is one JSON document
    assert {"id", "status", "requested", "created_at", "trials", "progress"} == set(doc)
    assert doc["progress"]["cancelled"] == 0  # no cancel request was ever sent
    assert "aco cancel" in captured.err       # interruption notice on stderr
    assert calls["n"] >= 2                    # at least one real poll occurred
    real_sleep(0)


def test_wait_json_terminal_output_is_single_document(api, state, tmp_path, capsys):
    """A batch whose trials are all terminal finishes immediately; stdout is
    still exactly one JSON document."""
    from urllib.request import Request, urlopen

    write(tmp_path, "t.json", {"prompt": "WT", "tests": []})
    write(tmp_path, "c.json", {"harness": "fake", "model": "fake-model"})
    main(["register", "task", "wt-task", "v1", str(tmp_path / "t.json"), "--api-url", api])
    main(["register", "config", "wt-cfg", "v1", str(tmp_path / "c.json"), "--api-url", api])
    main(["run", "--task", "wt-task@v1", "--target", "wt-cfg@v1", "--api-url", api,
          "--idempotency-key", "wt-wait"])
    captured = capsys.readouterr()
    exp_id = captured.out.split("Experiment ")[1].split("（")[0]
    main(["cancel", exp_id, "--api-url", api])  # single trial → cancelled → terminal
    capsys.readouterr()

    # replay the same key with --wait: the watcher starts on the already
    # terminal (cancelled) batch and finishes immediately
    assert main(["run", "--task", "wt-task@v1", "--target", "wt-cfg@v1",
                 "--api-url", api, "--wait", "--json",
                 "--idempotency-key", "wt-wait"]) == 0
    out = capsys.readouterr().out
    doc = json.loads(out)  # one document, not a concatenation
    assert doc["progress"]["cancelled"] == 1
    assert doc["id"] == exp_id  # idempotency replay targeted the same batch


def test_output_never_contains_credentials(api, state, capsys):
    secret = "super-secret-token-value"
    assert main(["status", "no-such-experiment", "--api-url", api,
                 "--token", secret]) == 1
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err

    assert main(["status", "no-such-experiment", "--api-url", api,
                 "--token", secret, "--json"]) == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


def test_json_run_output_is_stable(api, state, tmp_path, capsys):
    write(tmp_path, "t.json", {"prompt": "J", "tests": []})
    write(tmp_path, "c.json", {"harness": "fake", "model": "fake-model"})
    main(["register", "task", "j-task", "v1", str(tmp_path / "t.json"), "--api-url", api])
    main(["register", "config", "j-cfg", "v1", str(tmp_path / "c.json"), "--api-url", api])
    capsys.readouterr()  # discard human-readable register output
    assert main(["run", "--task", "j-task@v1", "--target", "j-cfg@v1",
                 "--api-url", api, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert {"id", "status", "requested", "created_at", "trials", "progress"} == set(doc)
    assert main(["status", doc["id"], "--api-url", api, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == doc
