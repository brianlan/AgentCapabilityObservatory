"""Focused deterministic tests for Pi trace compaction (#pi-trace).

The main test fails if the compacted trace loses the tail (last meaningful
events) or emits malformed/truncated JSONL: every output line must parse and
the final assistant message_end must survive verbatim.
"""
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

from aco import db, runs, supervisor


def _raiser(exc):
    def f(*args, **kwargs):
        raise exc
    return f


def _make_run(tmp_path, trial_id="trial-1"):
    conn = db.connect(tmp_path / "aco.db")
    db.migrate(conn)
    conn.execute("INSERT OR IGNORE INTO versions (id, kind, name, version, content, created_at)"
                 " VALUES ('task', 'task', 'task', 'v1', '{}', 'now'),"
                 " ('cfg', 'config', 'cfg', 'v1', '{}', 'now')")
    conn.execute("INSERT OR IGNORE INTO experiments (id, status, requested, created_at)"
                 " VALUES (?, 'planned', '{}', 'now')", (f"exp-{trial_id}",))
    conn.execute("INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
                 " repetition, plan_order, requested) VALUES (?, ?, 'task', 'cfg', 1, 1, '{}')",
                 (trial_id, f"exp-{trial_id}"))
    conn.commit()
    return conn, runs.create_run(conn, trial_id, {}, supervisor_pid=1)


def test_pi_trace_compaction_keeps_head_tail_summary_drops_updates(tmp_path, monkeypatch):
    conn, run_id = _make_run(tmp_path)
    secret = "secret-provider-token"
    marker = "final-assistant-marker"
    monkeypatch.setenv("ARK_AGENT_PLAN_API_KEY", secret)
    # tiny side bound: deterministic head/tail eviction without huge fixtures
    monkeypatch.setattr(supervisor, "PI_TRACE_SIDE_BYTES", 4 * 1024)

    raw = [b'{"type":"session_start","run":"first"}\n']
    raw += [b'{"type":"message_update","delta":"' + str(i).encode() + b'"}\n'
            for i in range(400)]
    raw += [b'{"type":"tool_result","i":' + str(i).encode()
            + b',"payload":"' + b"p" * 240 + b'"}\n' for i in range(60)]
    raw += [b'{"type":"tool_call","huge":"' + b"x" * 200_000 + b'"}\n',
            b'{"type":"message_end","text":"' + secret.encode() + b' middle"}\n',
            json.dumps({"type": "message_end",
                        "message": {"role": "assistant", "stopReason": "stopEndTurn",
                                    "text": marker}}).encode() + b"\n",
            b'{"type":"message_end","trunc']  # partial final line: timeout kill

    def fake_command(*args, check=True):
        open(args[-1], "wb").write(b"".join(raw))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(supervisor, "command", fake_command)
    supervisor._capture_pi_trace("container-1", tmp_path, run_id, conn, "timeout")
    supervisor._capture_pi_trace("container-1", tmp_path, run_id, conn, "agent_end")

    trace_dir = tmp_path / "pi-traces" / run_id
    trace_path = trace_dir / "transcript.jsonl"
    data = trace_path.read_bytes()
    assert data.endswith(b"\n")
    assert len(data) <= supervisor.PI_TRACE_MAX_BYTES
    assert secret.encode() not in data
    assert b"x" * 200_000 not in data
    assert stat.S_IMODE(os.stat(trace_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(trace_dir).st_mode) == 0o700
    assert not list(trace_dir.glob(".transcript*"))  # raw copy stayed ephemeral

    parsed = [json.loads(line) for line in data.splitlines()]  # malformed => fail
    summary = parsed[-1]
    assert summary["type"] == "aco_trace_summary"
    assert summary["last_assistant_stop_reason"] == "stopEndTurn"
    assert summary["event_counts"] == {"session_start": 1, "message_update": 400,
                                       "tool_result": 60, "tool_call": 1,
                                       "message_end": 2}
    assert summary["malformed_lines"] == 1
    assert summary["omitted_events"] > 400  # all deltas plus evicted middles

    kinds = [p.get("type") for p in parsed]
    assert "message_update" not in kinds  # dropped, but still counted above
    assert kinds[0] == "session_start"  # first meaningful event kept
    assert parsed[1] == {"type": "tool_result", "i": 0, "payload": "p" * 240}
    assert "aco_trace_gap" in kinds  # the evicted middle is explicit
    assert kinds[-2] == "message_end"  # tail survived: the lost-tail killer
    assert parsed[-2]["message"]["text"] == marker
    # chronology: once the head filled, everything spilled to the tail in
    # order — the oversized stub can never slot back into the head
    indices = [p["i"] for p in parsed if p.get("type") == "tool_result"]
    assert indices == sorted(set(indices)) and indices[0] == 0
    assert parsed[-4] == {"type": "tool_call", "oversized": True}
    assert parsed[-3]["text"] == "[REDACTED] middle"

    # duplicate capture (timeout + agent_end race, sequential here) records once
    phases = json.loads(runs.get_run(conn, run_id)["phases"])
    assert sum(p["event"] == "pi_trace_captured" for p in phases) == 1
    conn.close()


def test_pi_trace_no_gap_marker_when_tail_continues_head(tmp_path, monkeypatch):
    """Spill with no eviction: head + tail are contiguous, so the gap marker
    must be absent and omitted_events 0 — the marker only marks evicted
    middle events."""
    conn, run_id = _make_run(tmp_path)
    monkeypatch.setattr(supervisor, "PI_TRACE_SIDE_BYTES", 4 * 1024)
    raw = b"".join(b'{"type":"tool_result","i":' + str(i).encode()
                   + b',"payload":"' + b"p" * 240 + b'"}\n' for i in range(20))

    def fake_command(*args, check=True):
        open(args[-1], "wb").write(raw)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(supervisor, "command", fake_command)
    supervisor._capture_pi_trace("container-1", tmp_path, run_id, conn, "timeout")

    parsed = [json.loads(line)
              for line in (tmp_path / "pi-traces" / run_id
                           / "transcript.jsonl").read_bytes().splitlines()]
    assert [p["i"] for p in parsed[:-1]] == list(range(20))  # contiguous, ordered
    assert not any(p.get("type") == "aco_trace_gap" for p in parsed)
    assert parsed[-1]["omitted_events"] == 0
    conn.close()


def test_pi_trace_directory_private_before_raw_copy(tmp_path, monkeypatch):
    """The trace dir must be 0700 — including a pre-existing one — before the
    unredacted raw transcript lands, so the temp is unreachable regardless of
    the mode docker cp gives it."""
    conn, run_id = _make_run(tmp_path)
    trace_dir = tmp_path / "pi-traces" / run_id
    trace_dir.mkdir(parents=True)
    trace_dir.chmod(0o755)  # legacy default-perms dir must be corrected
    mode_at_copy = []

    def fake_command(*args, check=True):
        mode_at_copy.append(stat.S_IMODE(os.stat(trace_dir).st_mode))
        open(args[-1], "wb").write(b'{"type":"session_start"}\n')
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(supervisor, "command", fake_command)
    supervisor._capture_pi_trace("container-1", tmp_path, run_id, conn, "timeout")

    assert mode_at_copy == [0o700]  # private before the copy, not just after
    assert stat.S_IMODE(os.stat(trace_dir).st_mode) == 0o700
    conn.close()


def test_pi_trace_capture_survives_recording_and_cleanup_failures(tmp_path, monkeypatch):
    """Strictly best-effort: add_phase failures, unlink cleanup errors, and
    pre-try filesystem probes must never raise out of capture, and no
    exception body ever reaches the recorded phases."""
    def fake_command(payload):
        def _f(*args, check=True):
            open(args[-1], "wb").write(payload)
            return SimpleNamespace(returncode=0, stderr="")
        return _f
    good_raw = b'{"type":"session_start"}\n'

    # 1. success-path phase recording fails: trace stays published, no raise
    conn, run_id = _make_run(tmp_path, "trial-r1")
    monkeypatch.setattr(supervisor, "command", fake_command(good_raw))
    monkeypatch.setattr(runs, "add_phase",
                        _raiser(RuntimeError("BOOM secret message")))
    supervisor._capture_pi_trace("c1", tmp_path, run_id, conn, "timeout")
    parsed = [json.loads(line) for line in
              (tmp_path / "pi-traces" / run_id / "transcript.jsonl")
              .read_bytes().splitlines()]
    assert parsed[-1]["type"] == "aco_trace_summary"

    # 2. cleanup unlink fails after a capture failure: no raise
    monkeypatch.undo()
    monkeypatch.setattr(supervisor, "command",
                        lambda *a, **k: SimpleNamespace(returncode=1, stderr=""))
    monkeypatch.setattr(Path, "unlink", _raiser(OSError("BOOM unlink")))
    conn2, run_id2 = _make_run(tmp_path, "trial-r2")
    supervisor._capture_pi_trace("c1", tmp_path, run_id2, conn2, "timeout")
    monkeypatch.undo()

    # 3. the pre-try existence probe fails: no raise
    conn3, run_id3 = _make_run(tmp_path, "trial-r3")
    monkeypatch.setattr(Path, "exists", _raiser(OSError("BOOM exists")))
    supervisor._capture_pi_trace("c1", tmp_path, run_id3, conn3, "timeout")

    # only exception class names are recorded, never exception bodies;
    # scenario 2 records nothing at all — its failure record was the failure
    assert runs.get_run(conn2, run_id2)["phases"] is None
    assert "BOOM" not in (runs.get_run(conn3, run_id3)["phases"] or "[]")
    conn.close()
    conn2.close()
    conn3.close()


def test_pi_trace_summary_bounded_under_adversarial_event_types(tmp_path, monkeypatch):
    """Giant or high-cardinality event type names (and stop reasons) must not
    blow the summary line past the trace bound."""
    src = tmp_path / "raw"
    out = tmp_path / "out.jsonl"
    raw = [json.dumps({"type": "A" * 100_000, "big": "x" * 100_000}).encode() + b"\n",
           json.dumps({"type": "message_end",
                       "message": {"role": "assistant",
                                   "stopReason": "S" * 50_000}}).encode() + b"\n"]
    raw += [json.dumps({"type": f"k{i:03d}" + "n" * 100, "v": i}).encode() + b"\n"
            for i in range(50)]
    src.write_bytes(b"".join(raw))

    summary = supervisor._compact_pi_trace(src, out)

    lines = out.read_bytes().splitlines()
    assert max(len(line) for line in lines) <= 65_537  # every line bounded
    assert out.stat().st_size <= supervisor.PI_TRACE_MAX_BYTES
    assert summary["last_assistant_stop_reason"] == "S" * 128  # clamped
    counts = summary["event_counts"]
    assert len([k for k in counts if k != "other"]) == 32  # cardinality cap
    assert counts["other"] == 20  # remaining distinct kinds bucketed
    assert all(len(k) <= 64 for k in counts)
    assert summary["event_counts"]["message_end"] == 1  # normal kinds unchanged

    src.write_text(json.dumps({"type": "message_end", "message": {
        "role": "assistant", "stopReason": {"unexpected": "x" * 100_000}}}) + "\n")
    assert supervisor._compact_pi_trace(src, out)["last_assistant_stop_reason"] is None

    monkeypatch.setenv("ARK_AGENT_PLAN_API_KEY", "secret-provider-token")
    src.write_text(json.dumps({"type": "secret-provider-token", "message": {
        "role": "assistant", "stopReason": "secret-provider-token"}}) + "\n" +
        json.dumps({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "secret-provider-token"}}) + "\n")
    supervisor._compact_pi_trace(src, out)
    assert b"secret-provider-token" not in out.read_bytes()


def test_pi_trace_capture_failure_is_diagnostic_only(tmp_path, monkeypatch):
    conn, run_id = _make_run(tmp_path)

    def failing_command(*args, check=True):
        return SimpleNamespace(returncode=1, stderr="no such container")

    monkeypatch.setattr(supervisor, "command", failing_command)
    supervisor._capture_pi_trace(None, tmp_path, run_id, conn, "timeout")
    supervisor._capture_pi_trace("container-1", tmp_path, run_id, conn, "timeout")

    phases = json.loads(runs.get_run(conn, run_id)["phases"])
    assert [p["event"] for p in phases] == ["pi_trace_capture_failed"]
    assert not list((tmp_path / "pi-traces" / run_id).glob("*"))
    conn.close()
