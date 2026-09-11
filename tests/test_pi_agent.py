"""Unit tests for the ACO Pi adapter (#37).

No docker and no real provider: the renderer and transcript parser are pure
functions, and PiAgent.run is exercised against a stub environment plus a
local stub Session API. The credential value is always a dummy — tests assert
it never leaks into rendered config, commands, or the database.
"""

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from aco import db
from aco.pi_agent import (
    ARK_CREDENTIAL_ENV,
    ARK_MODEL,
    ARK_PROVIDER,
    PI_VERSION,
    PiAgent,
    parse_transcript,
    render_agent_flags,
    render_models_json,
    validate_profile,
)
from aco.supervisor import UnsupportedTarget, translate_profile

PI_PROFILE = {
    "schema_version": 1,
    "harness": "pi",
    "harness_version": PI_VERSION,
    "model": ARK_MODEL,
    "thinking": "max",
    "provider": ARK_PROVIDER,
    "provider_api_style": "openai-responses",
    "adapter_version": "0.1.0",
    "environment": "sha256:" + "b" * 64,
    "credentials": ["ark-agent-plan-main"],
}

pytestmark = pytest.mark.anyio


def _pi_profile(**overrides) -> dict:
    profile = {**PI_PROFILE, **overrides}
    return {k: v for k, v in profile.items() if v is not None}


class TestRenderModelsJson:
    def test_renders_full_metadata_with_env_key_reference(self):
        config = render_models_json(_TargetProfile(), "http://mock:1")
        provider = config["providers"][ARK_PROVIDER]
        assert provider["baseUrl"] == "http://mock:1"
        assert provider["apiKey"] == f"${ARK_CREDENTIAL_ENV}"  # reference, never a value
        assert provider["api"] == "openai-responses"
        model = provider["models"][0]
        assert model["id"] == ARK_MODEL
        assert model["reasoning"] is True
        assert model["thinkingLevelMap"]["max"] == "max"

    def test_rejects_unsupported_render_inputs(self):
        with pytest.raises(ValueError, match="provider"):
            render_models_json(_TargetProfile(provider="other"), "http://m")
        with pytest.raises(ValueError, match="provider_api_style"):
            render_models_json(_TargetProfile(provider_api_style="openai-completions"), "http://m")
        with pytest.raises(ValueError, match="model"):
            render_models_json(_TargetProfile(model="other-model"), "http://m")
        with pytest.raises(ValueError, match="thinking"):
            render_models_json(_TargetProfile(thinking="low"), "http://m")


def _TargetProfile(**overrides):
    from aco.models import TargetProfile
    return TargetProfile.model_validate({**PI_PROFILE, **overrides})


class TestValidateProfile:
    def test_pinned_profile_passes_and_honors_resource_timeout(self):
        assert validate_profile(_TargetProfile()) == 120
        from aco.models import ExecutionPolicy
        assert validate_profile(_TargetProfile(
            resources=ExecutionPolicy(timeout_sec=77))) == 77

    def test_declared_conditions_are_required_or_rejected(self):
        # TaskVersion owns the instruction; historical prompt_digest values
        # are accepted but do not affect target validation or fingerprints.
        validate_profile(_TargetProfile(prompt_digest=None))
        with pytest.raises(ValueError, match="environment"):
            validate_profile(_TargetProfile(environment=None))
        # and a declared network policy is never enforceable on pi: the
        # egress allowlist is ACO infrastructure, not a profile choice
        from aco.models import ExecutionPolicy
        with pytest.raises(ValueError, match="network"):
            validate_profile(_TargetProfile(
                resources=ExecutionPolicy(network="offline")))

    def test_every_deviation_fails_explicitly(self):
        with pytest.raises(ValueError, match="harness_version"):
            validate_profile(_TargetProfile(harness_version="0.85.1"))
        with pytest.raises(ValueError, match="adapter_version"):
            validate_profile(_TargetProfile(adapter_version="9.9.9"))
        with pytest.raises(ValueError, match="assistance_mode"):
            validate_profile(_TargetProfile(assistance_mode="human"))
        with pytest.raises(ValueError, match="credentials"):
            validate_profile(_TargetProfile(credentials=["other-ref"]))
        with pytest.raises(ValueError, match="credentials"):
            validate_profile(_TargetProfile(credentials=[]))
        # declared skills are now valid (#39): the pinned set loads explicitly
        validate_profile(_TargetProfile(
            skills=[{"name": "demo", "version": "v1"}]))
        # and the supervisor surface wraps it as UnsupportedTarget
        with pytest.raises(UnsupportedTarget, match="harness_version"):
            translate_profile(_pi_profile(harness_version="0.85.1"))

    def test_default_flags_disable_all_skill_loading(self):
        flags = render_agent_flags(_TargetProfile())
        assert "--no-skills" in flags
        assert "--no-extensions" in flags and "--no-session" in flags
        assert "--skill" not in flags

    def test_declared_skills_load_explicitly_in_order(self):
        flags = render_agent_flags(_TargetProfile(skills=[
            {"name": "b-skill", "version": "v1"},
            {"name": "a-skill", "version": "v1"}]))
        # explicit paths only: discovery stays off, order follows config
        assert "--no-skills" not in flags
        assert "--skill /opt/aco-skills/b-skill --skill /opt/aco-skills/a-skill" in flags
        assert "--no-extensions" in flags and "--no-session" in flags

    def test_supervisor_dispatches_both_harnesses(self):
        assert translate_profile(_pi_profile())[0] == "pi"
        assert translate_profile({"harness": "fake", "model": "none"})[0] == "fake"
        with pytest.raises(UnsupportedTarget, match="unsupported harness 'other'"):
            translate_profile({"harness": "other", "model": "x"})


class TestParseTranscript:
    def test_happy_session_observation(self):
        raw = "\n".join([
            json.dumps({"type": "session", "id": "s1"}),
            json.dumps({"type": "turn_end", "message": {
                "role": "assistant", "stopReason": "toolUse"},
                "toolResults": [{"toolCallId": "call_1", "toolName": "bash",
                                 "isError": False}]}),
            json.dumps({"type": "message_end", "message": {
                "role": "assistant", "provider": ARK_PROVIDER, "model": ARK_MODEL,
                "api": "openai-responses", "responseId": "resp_1", "stopReason": "stop",
                "usage": {"input": 11, "output": 4}}}),
        ])
        parsed = parse_transcript(raw)
        assert parsed["failure"] is None
        obs = parsed["observation"]
        assert obs["source"] == "pi_json_transcript"
        assert obs["provider"] == ARK_PROVIDER and obs["model"] == ARK_MODEL
        assert obs["response_id"] == "resp_1" and obs["stop_reason"] == "stop"
        assert obs["usage"] == {"input": 11, "output": 4}
        assert obs["tool_calls"] == ["bash"]

    def test_transient_retry_then_success_is_not_a_failure(self):
        raw = "\n".join([
            json.dumps({"type": "message_end", "message": {
                "role": "assistant", "stopReason": "error",
                "errorMessage": "Connection error."}}),
            json.dumps({"type": "auto_retry_start", "attempt": 1}),
            json.dumps({"type": "message_end", "message": {
                "role": "assistant", "stopReason": "stop",
                "usage": {"input": 5, "output": 2}}}),
        ])
        parsed = parse_transcript(raw)
        assert parsed["failure"] is None
        assert parsed["observation"]["stop_reason"] == "stop"

    @pytest.mark.parametrize("error,expected", [
        ("Provider returned 401 Unauthorized", "provider_auth"),
        ("invalid api key", "provider_auth"),
        ("HTTP 429 too many requests", "provider_transient"),
        ("Connection error.", "provider_unavailable"),
        ("Provider exploded oddly", "provider_transient"),  # unknown bucket
    ])
    def test_error_classification(self, error, expected):
        raw = json.dumps({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "error", "errorMessage": error}})
        assert parse_transcript(raw)["failure"] == expected

    def test_empty_transcript_is_not_a_provider_failure(self):
        assert parse_transcript("")["failure"] is None


# ---------------------------------------------------------------- agent tests

def _event_of_type(kind, **fields):
    payload = {"type": kind}
    payload.update(fields)
    return json.dumps(payload)


HAPPY_TRANSCRIPT = "\n".join([
    _event_of_type("turn_end", message={
        "role": "assistant", "provider": ARK_PROVIDER, "model": ARK_MODEL,
        "stopReason": "toolUse"},
        toolResults=[{"toolCallId": "call_1", "toolName": "bash",
                      "content": [{"type": "text", "text": "ok"}], "isError": False}]),
    _event_of_type("message_end", message={
        "role": "assistant", "provider": ARK_PROVIDER, "model": ARK_MODEL,
        "api": "openai-responses", "responseId": "resp_x", "stopReason": "stop",
        "usage": {"input": 7, "output": 3}}),
])

AUTH_FAIL_TRANSCRIPT = _event_of_type("message_end", message={
    "role": "assistant", "provider": ARK_PROVIDER, "model": ARK_MODEL,
    "stopReason": "error", "errorMessage": "Provider returned 401 Unauthorized"})


class _StubSession:
    """Local Session API stub: records submits, serves the unique task."""

    def __init__(self):
        self.submits = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"trial_id": "t1", "instruction": "solve the task"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.submits.append(json.loads(self.rfile.read(length) or "{}"))
                self.send_response(202)
                self.end_headers()
                self.wfile.write(b'{"receipt_id": "r1"}')

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _StubEnvironment:
    """Records exec commands; returns canned results per invocation."""

    def __init__(self, results):
        self.commands = []
        self.envs = []
        self.results = list(results)

    async def exec(self, command=None, env=None, **kwargs):
        self.commands.append(command)
        self.envs.append(env)
        return self.results.pop(0)


def _result(return_code=0, stdout="", stderr=""):
    return SimpleNamespace(return_code=return_code, stdout=stdout, stderr=stderr)


def _seed_db(tmp_path):
    conn = db.connect(tmp_path / "aco.db")
    db.migrate(conn)
    conn.execute(
        "INSERT INTO versions (id, kind, name, version, content, created_at)"
        " VALUES ('v-task', 'task', 'task', 'v1', '{}', 'now'),"
        " ('v-cfg', 'config', 'cfg', 'v1', '{}', 'now')")
    conn.execute(
        "INSERT INTO experiments (id, status, requested, created_at)"
        " VALUES ('e1', 'running', '{}', 'now')")
    conn.execute(
        "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
        " repetition, plan_order, requested) VALUES ('t1', 'e1', 'v-task', 'v-cfg', 1, 1, '{}')")
    conn.commit()
    return conn


@pytest.fixture()
def agent_env(tmp_path, monkeypatch):
    session = _StubSession()
    conn = _seed_db(tmp_path)
    from aco import runs
    run_id = runs.create_run(conn, "t1", {}, supervisor_pid=-1)
    monkeypatch.setenv("ACO_BASE_URL", session.url)
    monkeypatch.setenv("ACO_SESSION_TOKEN", "stub-token")
    monkeypatch.setenv("ACO_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("ACO_RUN_ID", run_id)
    monkeypatch.setenv("ACO_TARGET_PROFILE", json.dumps(_pi_profile(), sort_keys=True))
    monkeypatch.setenv(ARK_CREDENTIAL_ENV, "dummy-key-value")
    yield SimpleNamespace(session=session, conn=conn, run_id=run_id, tmp_path=tmp_path)
    session.close()
    conn.close()


def _agent(tmp_path):
    return PiAgent(logs_dir=tmp_path, model_name=f"{ARK_PROVIDER}/{ARK_MODEL}")


def _db_row(conn, sql, args=()):
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, args).fetchone()


class TestPiAgentRun:
    async def test_success_claims_runs_submits_and_records_observation(self, agent_env):
        # direct run() call: config write, pi session, transcript fetch
        environment = _StubEnvironment([_result(),
                                        _result(),
                                        _result(stdout=HAPPY_TRANSCRIPT)])
        await _agent(agent_env.tmp_path).run("unused", environment, None)

        # submit intent recorded with the trial-scoped idempotency key
        assert agent_env.session.submits == [{"idempotency_key": "pi-t1"}]
        # observation written from the transcript with its source
        row = _db_row(agent_env.conn, "SELECT runtime_observation FROM trials WHERE id='t1'")
        observation = json.loads(row["runtime_observation"])
        assert observation["source"] == "pi_json_transcript"
        assert observation["usage"] == {"input": 7, "output": 3}
        assert observation["tool_calls"] == ["bash"]
        assert observation["stop_reason"] == "stop"
        # the pi invocation carries the pinned model selection and isolation
        pi_command = environment.commands[1]
        assert "--provider ark-agent-plan" in pi_command
        assert f"--model {ARK_MODEL}" in pi_command
        assert "--thinking max" in pi_command
        for flag in ("--no-skills", "--no-extensions", "--no-prompt-templates",
                     "--no-session", "--offline"):
            assert flag in pi_command
        # the key value travels only as exec env, never in any command string
        assert "dummy-key-value" not in pi_command
        assert "dummy-key-value" not in environment.commands[0]
        assert environment.envs[1] == {ARK_CREDENTIAL_ENV: "dummy-key-value"}
        # rendered config carries the env reference, never the value
        import base64
        encoded = environment.commands[0].split("printf '%s' ")[1].split(" |")[0]
        models_json = json.loads(base64.b64decode(encoded))
        assert models_json["providers"][ARK_PROVIDER]["apiKey"] == f"${ARK_CREDENTIAL_ENV}"

    async def test_provider_failure_is_an_anomaly_never_a_seal(self, agent_env):
        environment = _StubEnvironment([_result(),
                                        _result(return_code=1),
                                        _result(stdout=AUTH_FAIL_TRANSCRIPT)])
        with pytest.raises(Exception, match="provider failure \\(provider_auth\\)"):
            await _agent(agent_env.tmp_path).run("unused", environment, None)

        answer = _db_row(agent_env.conn,
                         "SELECT status, anomaly FROM sealed_answers WHERE trial_id='t1'")
        assert answer["status"] == "anomaly"
        assert "provider_auth" in answer["anomaly"]
        phases = json.loads(_db_row(agent_env.conn,
                            "SELECT phases FROM trial_runs WHERE run_id=?",
                            (agent_env.run_id,))["phases"])
        assert phases[-1]["event"] == "target_failure"
        assert phases[-1]["failure_class"] == "provider_auth"
        # no submit intent: the run never claims success
        assert agent_env.session.submits == []

    async def test_credential_missing_fails_before_any_model_call(self, agent_env, monkeypatch):
        monkeypatch.delenv(ARK_CREDENTIAL_ENV)
        environment = _StubEnvironment([])
        with pytest.raises(Exception, match="ARK_AGENT_PLAN_API_KEY"):
            await _agent(agent_env.tmp_path).run("unused", environment, None)

        # nothing executed: no config write, no pi invocation
        assert environment.commands == []
        answer = _db_row(agent_env.conn,
                         "SELECT status FROM sealed_answers WHERE trial_id='t1'")
        assert answer["status"] == "anomaly"
        assert agent_env.session.submits == []
