"""Trial image contract tests (#38 reopen).

The pinned pi image is built OUTSIDE the trial hot path by `aco
build-agent-image`; trial start only resolves its immutable digest, the task
runs digest-pinned, and
an automated (CI) environment can never reach the real provider endpoint —
regardless of allow_paid_run or credential presence.
"""

import asyncio
import json
import sqlite3
import subprocess

import pytest

from aco import artifacts, db, pi_agent, runs
from aco.supervisor import PI_HARNESS, build_task_dir, execute_run


PROMPT = "write the answer file"
IMAGE_DIGEST = "sha256:" + "a" * 64

PI_PROFILE = {
    "schema_version": 1,
    "harness": "pi",
    "harness_version": pi_agent.PI_VERSION,
    "model": "glm-5.3-flash",
    "thinking": "max",
    "provider": "ark-agent-plan",
    "provider_api_style": "openai-responses",
    "adapter_version": pi_agent.ADAPTER_VERSION,
    "credentials": ["ark-agent-plan-main"],
    "environment": IMAGE_DIGEST,
}


@pytest.fixture()
def conn(tmp_path):
    conn = db.connect(tmp_path / "aco.db")
    db.migrate(conn)
    yield conn
    conn.close()


def register_pi_trial(conn):
    """One experiment/trial whose config version is a valid pi profile."""
    contract = artifacts.ArtifactContract(required_outputs=("/workspace/answer.txt",))
    content = {"prompt": PROMPT,
               "contract": {"required_outputs": ["/workspace/answer.txt"]},
               "contract_digest": artifacts.contract_digest(contract)}
    conn.execute(
        "INSERT INTO versions (id, kind, name, version, content, assets, created_at)"
        " VALUES ('v-task', 'task', 'task', 'v1', ?, '[]', 'now')",
        (json.dumps(content),))
    conn.execute(
        "INSERT INTO versions (id, kind, name, version, content, created_at)"
        " VALUES ('v-cfg', 'config', 'cfg', 'v1', ?, 'now')",
        (json.dumps(PI_PROFILE),))
    conn.execute(
        "INSERT INTO experiments (id, status, requested, created_at)"
        " VALUES ('e1', 'planned', '{}', 'now')")
    conn.execute(
        "INSERT INTO trials (id, experiment_id, task_version_id, config_version_id,"
        " repetition, plan_order, requested) VALUES ('t1', 'e1', 'v-task', 'v-cfg', 1, 1, '{}')")
    conn.commit()


def start_run(conn) -> sqlite3.Row:
    run_id = runs.create_run(conn, "t1", PI_PROFILE, supervisor_pid=-1)
    conn.commit()
    return conn.execute("SELECT * FROM trial_runs WHERE run_id = ?", (run_id,)).fetchone()


def run_phases(conn, run_id) -> list[dict]:
    row = conn.execute("SELECT phases FROM trial_runs WHERE run_id = ?", (run_id,)).fetchone()
    return json.loads(row["phases"] or "[]")


def test_trial_start_never_builds_the_agent_image(tmp_path, conn, monkeypatch):
    """Cold trial start resolves the prebuilt image digest only: no docker
    build, no npm install, no registry access (#38 reopen). A missing
    prebuild is a terminal execution anomaly, not a build attempt."""
    register_pi_trial(conn)
    run = start_run(conn)
    # this test exercises the prebuild contract, not the CI gate: point the
    # endpoint at an explicit mock (the PR's own rule for automated
    # environments) so the missing-prebuild path is what fails, with CI=true
    # left in place exactly as GitHub Actions provides it (reviewer fix)
    monkeypatch.setenv(pi_agent.ARK_BASE_URL_ENV, "http://127.0.0.1:9/ark")

    def forbidden_build(*_args, **_kwargs):
        raise AssertionError("trial start must never build the agent image")

    monkeypatch.setattr(pi_agent, "build_image", forbidden_build)
    monkeypatch.setattr(pi_agent, "image_digest",
                        lambda ref: (_ for _ in ()).throw(
                            RuntimeError(f"image inspect failed for {ref}")))

    asyncio.run(execute_run(conn, run, tmp_path))

    row = conn.execute("SELECT status, exit_kind, exit_detail FROM trial_runs WHERE run_id = ?",
                       (run["run_id"],)).fetchone()
    assert row["status"] == "error" and row["exit_kind"] == "environment_invalid"
    assert "not prebuilt" in row["exit_detail"]
    assert "aco build-agent-image" in row["exit_detail"]
    assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "anomaly"


def test_ci_hard_rejects_real_ark_before_any_model_call(tmp_path, conn, monkeypatch):
    """In CI the real Ark endpoint is refused outright — with allow_paid_run
    semantics bypassed and a credential configured, the endpoint (the
    billing path) is rejected before any container or model call (#38
    reopen). Automated environments use explicit mock endpoints only."""
    register_pi_trial(conn)
    run = start_run(conn)
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv(pi_agent.ARK_BASE_URL_ENV, raising=False)  # default = real Ark
    assert pi_agent.is_real_ark_endpoint()

    def forbidden_build(*_args, **_kwargs):
        raise AssertionError("CI must never get near a real provider call")

    monkeypatch.setattr(pi_agent, "build_image", forbidden_build)

    asyncio.run(execute_run(conn, run, tmp_path))

    row = conn.execute("SELECT status, exit_kind, exit_detail FROM trial_runs WHERE run_id = ?",
                       (run["run_id"],)).fetchone()
    assert row["status"] == "error" and row["exit_kind"] == "environment_invalid"
    assert "CI" in row["exit_detail"]
    # rejected before the image or any network phase was even attempted
    assert run_phases(conn, run["run_id"]) == [] or all(
        p["event"] not in ("image_digest", "network_policy", "agent_start")
        for p in run_phases(conn, run["run_id"]))
    assert conn.execute("SELECT status FROM trials WHERE id = 't1'").fetchone()[0] == "anomaly"


def test_ci_allows_mock_endpoint(tmp_path, conn, monkeypatch):
    """The CI reject keys on the endpoint, not on CI alone: an explicit mock
    upstream passes the gate and proceeds past it (to the image resolution,
    which is stubbed here)."""
    register_pi_trial(conn)
    run = start_run(conn)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv(pi_agent.ARK_BASE_URL_ENV, "http://127.0.0.1:9/ark")
    assert not pi_agent.is_real_ark_endpoint()
    monkeypatch.setattr(pi_agent, "image_digest",
                        lambda ref: (_ for _ in ()).throw(RuntimeError("no image here")))

    asyncio.run(execute_run(conn, run, tmp_path))

    row = conn.execute("SELECT exit_kind, exit_detail FROM trial_runs WHERE run_id = ?",
                       (run["run_id"],)).fetchone()
    # past the CI gate; fails later on the missing prebuild instead
    assert row["exit_kind"] == "environment_invalid" and "CI" not in row["exit_detail"]


def test_task_toml_pins_the_verified_digest(tmp_path, monkeypatch):
    """The pi task runs exactly the verified image: task.toml carries the
    content digest that was checked against the profile's declared
    environment (#36 reopen, #38 reopen)."""
    from aco.supervisor import build_task_dir, gateway_config

    monkeypatch.setenv("ARK_AGENT_PLAN_BASE_URL", "https://ark.example.com/api/v3")
    monkeypatch.setenv("ACO_BASE_URL", "http://127.0.0.1:8100")
    task_dir = build_task_dir(tmp_path, PROMPT, 30, "run1", harness=PI_HARNESS,
                              gateway=gateway_config(tmp_path, "run1"),
                              image_digest_ref=IMAGE_DIGEST)
    toml = (task_dir / "task.toml").read_text()
    assert f'docker_image = "{IMAGE_DIGEST}"' in toml
    # and no Dockerfile is shipped in the task dir: harbor runs the declared
    # image, so a build path in the trial hot path must not even exist
    assert not (task_dir / "environment" / "Dockerfile").exists()


def test_gateway_ca_bundle_is_mounted_when_configured(tmp_path, monkeypatch):
    from aco.supervisor import build_task_dir, gateway_config

    ca_bundle = tmp_path / "operator-ca.pem"
    ca_bundle.write_text("test ca")
    monkeypatch.setenv("ACO_GATEWAY_CA_BUNDLE", str(ca_bundle))
    monkeypatch.setenv("ACO_BASE_URL", "http://127.0.0.1:8100")
    task_dir = build_task_dir(
        tmp_path / "run", PROMPT, 30, "run-ca", harness=PI_HARNESS,
        gateway=gateway_config(tmp_path, "run-ca"), image_digest_ref=IMAGE_DIGEST,
    )
    compose = (task_dir / "offline.yaml").read_text()
    assert f'{ca_bundle}:/etc/aco/gateway-ca.pem:ro' in compose
    assert 'ACO_GATEWAY_CA_BUNDLE: "/etc/aco/gateway-ca.pem"' in compose


def test_build_agent_image_cli_prints_digest(monkeypatch, capsys):
    """`aco build-agent-image` prebuilds outside any trial and prints the
    immutable digest (#38 reopen)."""
    from aco.cli import build_parser

    monkeypatch.setattr(pi_agent, "build_image", lambda: IMAGE_DIGEST)
    args = build_parser().parse_args(["build-agent-image"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.strip() == IMAGE_DIGEST


def test_trial_cleanup_preserves_shared_prebuilt_image(monkeypatch):
    """Harbor cleanup removes this run's containers only, never the image."""
    from aco import supervisor

    commands = []

    def fake_command(*args, **kwargs):
        commands.append(args)
        if args[:3] == ("docker", "ps", "-aq"):
            return subprocess.CompletedProcess(args, 0, stdout="container\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(supervisor, "command", fake_command)
    supervisor.cleanup_container("run-1")

    assert ("docker", "rm", "-f", "container") in commands
    assert not any(command[:3] == ("docker", "image", "rm") for command in commands)
