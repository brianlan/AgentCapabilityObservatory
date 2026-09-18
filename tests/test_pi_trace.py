import json
from types import SimpleNamespace

from aco import db, runs, supervisor


def test_pi_trace_capture_is_bounded_redacted_and_idempotent(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "aco.db")
    db.migrate(conn)
    conn.execute("INSERT INTO versions (id, kind, name, version, content, created_at)"
                 " VALUES ('task', 'task', 'task', 'v1', '{}', 'now'),"
                 " ('cfg', 'config', 'cfg', 'v1', '{}', 'now')")
    conn.execute("INSERT INTO experiments (id, status, requested, created_at)"
                 " VALUES ('exp', 'planned', '{}', 'now')")
    conn.execute("INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
                 " repetition, plan_order, requested) VALUES ('trial-1', 'exp', 'task', 'cfg', 1, 1, '{}')")
    conn.commit()
    run_id = runs.create_run(conn, "trial-1", {}, supervisor_pid=1)
    secret = "secret-provider-token"
    monkeypatch.setenv("ARK_AGENT_PLAN_API_KEY", secret)
    payload = (b'{"type":"message_end","text":"' + secret.encode() + b'"}\n'
               + b"x" * (supervisor.PI_TRACE_MAX_BYTES + 10))

    def fake_command(*args, check=True):
        output = args[-1]
        open(output, "wb").write(payload)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(supervisor, "command", fake_command)
    supervisor._capture_pi_trace("container-1", tmp_path, run_id, conn, "timeout")
    supervisor._capture_pi_trace("container-1", tmp_path, run_id, conn, "agent_end")

    trace = (tmp_path / "pi-traces" / run_id / "transcript.jsonl").read_bytes()
    assert len(trace) == supervisor.PI_TRACE_MAX_BYTES
    assert secret.encode() not in trace
    phases = json.loads(runs.get_run(conn, run_id)["phases"])
    assert sum(p["event"] == "pi_trace_captured" for p in phases) == 1
    conn.close()
