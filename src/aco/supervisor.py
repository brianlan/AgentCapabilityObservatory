"""Independent supervisor process: runs one trial via Harbor and records evidence (#13).

The FastAPI process only plans; this process owns long-running execution and
container control. Only public Harbor entry points are used (Trial.create,
add_hook, verifier-off config, extra compose file) with a pinned Harbor
version. Harbor's raw outcome is diagnostics only — never a score (#15 owns
scoring).
"""

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import urllib.parse
from pathlib import Path

from . import artifacts, db, environments, lifecycle, pi_agent, runs, skills
from .app import fetch_version
from .models import TargetProfile, parse_config_content

# pinned at adoption; prototype verified the installed package against this
# source commit byte-for-byte (prototypes/harbor-freeze evidence).
HARBOR_VERSION = "0.22.0"
HARBOR_SOURCE_COMMIT = "71c39eafbd134d43ae3f489b5e6488b2a157de65"
ADAPTER_VERSION = pi_agent.ADAPTER_VERSION

# fixed digest used by the prototype; the fake agent needs nothing newer
IMAGE = "python@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254"
RUN_LABEL = "aco.run"
DEFAULT_AGENT_TIMEOUT_SEC = 20
TRIAL_GRACE_SEC = 90
SUBMIT_POLL_SEC = 0.5

# the host-side route the gateway (infrastructure, not the evaluated
# container) may use to reach host services such as the session listener (#38)
HOST_ROUTE_HOST = "host.docker.internal"
# network policy evidence version (#38)
NETWORK_POLICY_VERSION = 1
# the single allowlisted target: the per-trial gateway service, pinned to a
# static IP inside a private benchmark subnet (RFC 2544 — docker never
# allocates it, the internet never routes it). The /24 is chosen per run so
# leaked networks from crashed trials cannot collide; DNS inside the
# restricted netns is unreliable (docker's DNAT rewrites the resolver port
# ahead of the nftables DNS allowance), so the allow path needs no names (#38)
GATEWAY_SERVICE = "aco-gateway"
GATEWAY_SUBNET_PREFIX = "198.19"
GATEWAY_HOST_OCTET = 10


def _gateway_octet(run_id: str) -> int:
    # deterministic per-run third octet: crashed trials leak their subnet
    # until cleanup runs, and a fixed subnet would make every leak fatal
    return hashlib.md5(run_id.encode()).digest()[0]


def gateway_subnet(run_id: str) -> str:
    return f"{GATEWAY_SUBNET_PREFIX}.{_gateway_octet(run_id)}.0/24"


def gateway_ip(run_id: str) -> str:
    return f"{GATEWAY_SUBNET_PREFIX}.{_gateway_octet(run_id)}.{GATEWAY_HOST_OCTET}"


def _trial_network_name(run_id: str) -> str:
    return f"aco-{run_id[:12]}__env_default"


def _remove_stale_trial_networks(run_id: str | None = None) -> list[str]:
    """Remove compose networks of dead ACO trials (#38): the fixed per-run
    gateway IP needs a free subnet, and interrupted runs (supervisor crash,
    stack teardown) leak theirs. Networks are ours by construction:
    aco-<run12>__env_default. A network with attached containers belongs to
    a live sibling trial (octet collisions happen, ~1/256 per pair) — it is
    preserved and returned so callers can name it in diagnostics (#52)."""
    args = ["docker", "network", "ls", "--format", "{{.Name}}"]
    result = subprocess.run(args, text=True, capture_output=True, timeout=20, check=False)
    preserved = []
    for name in result.stdout.split():
        if not (name.startswith("aco-") and name.endswith("__env_default")):
            continue
        if run_id is not None and name == _trial_network_name(run_id):
            continue  # the current trial's network is removed by its owner
        attached = command("docker", "network", "inspect", "--format",
                           "{{len .Containers}}", name, check=False)
        if attached.stdout.strip() != "0":
            # live sibling: removing it would error a healthy in-flight trial (#52)
            preserved.append(name)
            continue
        command("docker", "network", "rm", name, check=False)
    return preserved


def _subnet_blocked_error(run_id: str, preserved: list[str]) -> RuntimeError:
    """Explicit diagnostic when our octet is held by a live sibling (#52):
    names the blocked subnet and the surviving networks instead of a bare
    docker overlap error."""
    return RuntimeError(
        f"gateway subnet {gateway_subnet(run_id)} is held by live sibling"
        f" network(s) {preserved or '[]'}; the stale sweep left them"
        f" untouched to avoid erroring healthy trials")


def _container_reachable_url(url: str) -> str:
    """Rewrite a loopback host-side URL into the gateway container's
    host-gateway route (#38). Non-loopback URLs pass through unchanged."""
    parts = urllib.parse.urlsplit(url)
    if parts.hostname in ("127.0.0.1", "localhost", "::1"):
        netloc = f"{HOST_ROUTE_HOST}:{parts.port}" if parts.port else HOST_ROUTE_HOST
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
    return url


def gateway_config(root: Path, run_id: str) -> dict:
    """Per-trial gateway wiring (#38): fixed upstreams from the trusted
    supervisor environment. The provider upstream comes from the profile's
    base URL (mock in tests), the session upstream from the session API URL
    the manager was started with. The gateway IP/subnet are per-run."""
    return {
        "module": Path(pi_agent.__file__).with_name("gateway.py"),
        "evidence_dir": root / "gateway-logs",
        "provider_upstream": _container_reachable_url(pi_agent.ark_base_url()),
        "session_upstream": _container_reachable_url(os.environ["ACO_BASE_URL"]),
        "subnet": gateway_subnet(run_id),
        "ip": gateway_ip(run_id),
    }

FAKE_HARNESS = "fake"
PI_HARNESS = "pi"
# the fake target calls no model; anything model-shaped is unsupported, never
# silently downgraded (issue acceptance: explicit failure)
FAKE_MODEL_VALUES = {"", "none"}
# every field a TargetProfile (v1 or normalized legacy) may carry; anything
# else fails the key walk below (#36)
KNOWN_PROFILE_KEYS = frozenset(TargetProfile.model_fields)


class UnsupportedTarget(Exception):
    pass


def _validate_fake_profile(parsed: TargetProfile) -> None:
    if parsed.model not in FAKE_MODEL_VALUES:
        raise UnsupportedTarget(
            f"fake target does not support model={parsed.model!r};"
            " the fake agent calls no model in V1"
        )
    # declared-is-enforced (#36 reopen): the fake path controls none of
    # these conditions, so a declared value would enter the fingerprint
    # without ever taking effect — reject, never silently ignore
    for field in ("provider", "thinking", "skills", "credentials",
                  "harness_version", "adapter_version", "provider_api_style",
                  "prompt_digest", "environment"):
        if getattr(parsed, field):
            raise UnsupportedTarget(
                f"fake target does not support {field}={getattr(parsed, field)!r};"
                " only a bare fake profile executes in V1"
            )
    if parsed.assistance_mode != "none":
        raise UnsupportedTarget(
            f"fake target does not support assistance_mode="
            f"{parsed.assistance_mode!r}; only 'none' executes in V1"
        )
    if parsed.resources is not None:
        raise UnsupportedTarget(
            f"fake target does not support resources={parsed.resources.model_dump()!r};"
            " the fake run has no controllable execution conditions in V1"
        )


def translate_profile(profile: dict) -> tuple[str, int]:
    """Return (harness, agent timeout) for an executable profile or raise
    UnsupportedTarget. Unknown fields fail explicitly on every path (#36)."""
    for key in sorted(profile):
        if key not in KNOWN_PROFILE_KEYS:
            raise UnsupportedTarget(
                f"unsupported target profile field {key!r};"
                f" supported harnesses: {FAKE_HARNESS!r}, {PI_HARNESS!r}"
            )
    try:
        parsed = parse_config_content(profile)
    except ValueError as exc:
        raise UnsupportedTarget(f"invalid target profile: {exc}") from exc
    if parsed.harness == FAKE_HARNESS:
        _validate_fake_profile(parsed)
        return FAKE_HARNESS, DEFAULT_AGENT_TIMEOUT_SEC
    if parsed.harness == PI_HARNESS:
        try:
            return PI_HARNESS, pi_agent.validate_profile(parsed)
        except ValueError as exc:
            raise UnsupportedTarget(str(exc)) from exc
    raise UnsupportedTarget(
        f"unsupported harness {parsed.harness!r}: only {FAKE_HARNESS!r} and"
        f" {PI_HARNESS!r} execute in V1"
    )


def effective_conditions(profile: dict) -> dict:
    """The execution conditions the supervisor will actually apply to this
    profile — materialized into every trial's request snapshot at experiment
    creation (#36 reopen), so a trial carries the effective contract, not
    just declared fields. Unsupported/invalid profiles raise
    UnsupportedTarget; the API maps that to 422 before any plan exists."""
    harness, agent_timeout_sec = translate_profile(profile)
    parsed = parse_config_content(profile)
    effective = {
        "harness": harness,
        "agent_timeout_sec": agent_timeout_sec,
        "network_policy": "offline" if harness == FAKE_HARNESS else "allowlist",
    }
    if parsed.resources is not None:
        for key in ("cpus", "memory_mb"):
            value = getattr(parsed.resources, key)
            if value is not None:
                effective[key] = value
    return effective


def command(*args, check=True):
    return subprocess.run(args, text=True, capture_output=True, timeout=20, check=check)


def resolve_skill_mounts(conn: sqlite3.Connection, parsed: TargetProfile,
                         root: Path) -> list[dict]:
    """Resolve each declared SkillVersionRef to its imported, verified bytes
    (#39). Returns one entry per skill with requested vs observed digests and
    the host_dir to mount; verified=False means the trial must not run
    (execution anomaly, no paid call). The fake harness never reaches this:
    it rejects any skill."""
    state = []
    for ref in parsed.skills:
        entry = {"name": ref.name, "version": ref.version,
                 "requested": None, "observed": None, "verified": False}
        if not skills.NAME_RE.fullmatch(ref.name):
            # only trusted-side misregistration can reach this; refuse to
            # build any container path from a non-renderable name (#39)
            state.append({**entry, "observed": "unsafe_name"})
            continue
        row = fetch_version(conn, "skill", ref.name, ref.version)
        if row is None:
            state.append({**entry, "observed": "version_not_found"})
            continue
        entry["requested"] = json.loads(row["content"])["bundle"]["digest"]
        host_dir = root / "skills" / row["id"]
        if not host_dir.is_dir():
            state.append({**entry, "observed": "bundle_missing"})
            continue
        try:
            observed = skills.tree_digest(host_dir)["digest"]
        except skills.SkillImportError as exc:
            state.append({**entry, "observed": f"unreadable: {exc}"})
            continue
        state.append({**entry, "observed": observed, "host_dir": str(host_dir),
                      "verified": observed == entry["requested"]})
    return state


def build_task_dir(work_dir: Path, instruction: str, agent_timeout_sec: int, run_id: str,
                   harness: str = FAKE_HARNESS,
                   skill_mounts: list[dict] | None = None,
                   allowed_hosts: list[str] | None = None,
                   gateway: dict | None = None,
                   environment_dir: Path | None = None) -> Path:
    """Minimal Harbor task dir: registry prompt as instruction, pinned image,
    compose override with our identification label.

    When the task version declares a registered environment asset, its
    verified ``workspace/`` subtree is copied into the image build context
    and COPYed into /workspace at image build — the registered bytes are
    materialized before any agent call (#20 reopen).

    The fake target stays fully offline (network_mode none). The pi target
    runs under Harbor's egress-control sidecar: task.toml declares
    network_mode allowlist whose only target is the per-trial gateway
    service, the override adds NO explicit networking on main (so the
    sidecar owns main's network namespace and enforces the allowlist
    in-kernel), and the gateway — ACO infrastructure, not evaluated code —
    exposes the session surface and the single fixed provider route (#38)."""
    task_dir = work_dir / "task"
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "instruction.md").write_text(instruction)
    # the verified environment bytes, copied per run and bind-mounted at
    # /workspace: the registered tree is materialized before any agent call
    # (#20 reopen). A copy — never a mount of the store itself, and no
    # Dockerfile bake: harbor uses the prebuilt docker_image when one is
    # declared, so a build-context COPY would be dead code.
    workspace_mount = None
    if environment_dir is not None:
        materialized = task_dir / "workspace"
        environments.materialize(environment_dir, materialized)
        workspace_mount = f'      - "{materialized}:/workspace"\n'
    image = pi_agent.PI_IMAGE_TAG if harness == PI_HARNESS else IMAGE
    if harness == PI_HARNESS:
        hosts = ", ".join(f'"{h}"' for h in (allowed_hosts or [gateway_ip(run_id)]))
        (task_dir / "task.toml").write_text(
            f'schema_version = "1.4"\n[environment]\ndocker_image = "{image}"\n'
            f'network_mode = "allowlist"\nallowed_hosts = [{hosts}]\n'
            f'[agent]\ntimeout_sec = {agent_timeout_sec}\n'
        )
        (task_dir / "environment" / "Dockerfile").write_text(pi_agent.render_dockerfile())
        provider = urllib.parse.urlsplit(gateway["provider_upstream"])
        # pi renders baseUrl = gateway IP + provider path; the gateway
        # forwards the full path to the fixed upstream. Static IP: no DNS on
        # the allow path.
        gateway_env = (
            f"      ACO_GATEWAY_EVIDENCE: \"/evidence/{run_id}.jsonl\"\n"
            f"      ACO_GATEWAY_PROVIDER_UPSTREAM: \"{gateway['provider_upstream']}\"\n"
            f"      ACO_GATEWAY_SESSION_UPSTREAM: \"{gateway['session_upstream']}\"\n"
        )
        subnet, ip = gateway["subnet"], gateway["ip"]
        compose = task_dir / "offline.yaml"
        # declared skills mount read-only into the evaluated container only (#39);
        # the materialized environment owns /workspace (#20 reopen)
        skill_volume_lines = "".join(
            f'      - "{mount["host_dir"]}:{pi_agent._PI_CONTAINER_SKILL_ROOT}/{mount["name"]}:ro"\n'
            for mount in (skill_mounts or []))
        main_volumes = ""
        if workspace_mount or skill_mounts:
            main_volumes = ("    volumes:\n" + (workspace_mount or "")
                            + skill_volume_lines)
        compose.write_text(
            "services:\n"
            f"  {GATEWAY_SERVICE}:\n"
            f"    image: {IMAGE}\n"
            "    command: [\"python\", \"/aco/gateway.py\"]\n"
            "    volumes:\n"
            f"      - \"{gateway['module']}:/aco/gateway.py:ro\"\n"
            f"      - \"{gateway['evidence_dir']}:/evidence\"\n"
            "    environment:\n"
            + gateway_env +
            "    extra_hosts:\n"
            f"      - \"{HOST_ROUTE_HOST}:host-gateway\"\n"
            "    networks:\n"
            "      default:\n"
            f"        ipv4_address: {ip}\n"
            "  main:\n"
            "    depends_on:\n"
            f"      {GATEWAY_SERVICE}:\n"
            "        condition: service_started\n"
            + main_volumes
            + "    labels:\n"
            f"      {RUN_LABEL}: {run_id}\n"
            "networks:\n"
            "  default:\n"
            "    ipam:\n"
            "      config:\n"
            f"        - subnet: {subnet}\n"
        )
        assert provider.hostname  # the provider upstream must be absolute
        return task_dir
    (task_dir / "task.toml").write_text(
        f'schema_version = "1.4"\n[environment]\ndocker_image = "{image}"\n'
        f'network_mode = "public"\n[agent]\ntimeout_sec = {agent_timeout_sec}\n'
    )
    (task_dir / "environment" / "Dockerfile").write_text(f"FROM {IMAGE}\n")
    compose = task_dir / "offline.yaml"
    compose.write_text(
        "services:\n"
        "  main:\n"
        "    network_mode: none\n"
        + ("    volumes:\n" + workspace_mount if workspace_mount else "")
        + "    labels:\n"
        f"      {RUN_LABEL}: {run_id}\n"
    )
    return task_dir


def image_digest(image_ref: str) -> str:
    """The image's content digest (`docker inspect` RepoDigests/Id), the
    observed half of the environment verification (#36 reopen)."""
    result = command("docker", "image", "inspect", "--format", "{{.Id}}", image_ref)
    return result.stdout.strip()


def discover_container(run_id: str) -> str:
    ids = command(
        "docker", "ps", "-q",
        "--filter", f"label={RUN_LABEL}={run_id}",
        "--filter", "label=com.docker.compose.service=main",
    ).stdout.split()
    if len(ids) != 1:
        raise RuntimeError(f"expected exactly one agent container, found {len(ids)}")
    return ids[0]


def cleanup_container(run_id: str) -> None:
    ids = command("docker", "ps", "-aq", "--filter", f"label={RUN_LABEL}={run_id}", check=False).stdout.split()
    for cid in ids:
        command("docker", "rm", "-f", cid, check=False)


def container_security_summary(container_id: str) -> dict:
    """Runtime evidence: the agent container must stay unprivileged and
    isolated (no docker socket, no host network)."""
    inspect = json.loads(command("docker", "inspect", container_id).stdout)[0]
    host_config = inspect["HostConfig"]
    mounts = [str(m.get("Source", "")) for m in inspect.get("Mounts", [])]
    mounts += host_config.get("Binds") or []
    return {
        "privileged": bool(host_config.get("Privileged")),
        "network_mode": host_config.get("NetworkMode"),
        "docker_socket_mounted": any("docker.sock" in source for source in mounts),
    }


def _has_submission(conn: sqlite3.Connection, trial_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM submissions WHERE trial_id = ?", (trial_id,)
    ).fetchone() is not None


def _target_failure(conn: sqlite3.Connection, run_id: str) -> dict | None:
    """The adapter's recorded execution-condition failure, if any (#37)."""
    row = conn.execute("SELECT phases FROM trial_runs WHERE run_id = ?", (run_id,)).fetchone()
    phases = json.loads(row["phases"]) if row and row["phases"] else []
    return next((p for p in phases if p.get("event") == "target_failure"), None)


def _finish_trial_from_answer(conn: sqlite3.Connection, trial_id: str,
                              run_id: str, trigger: str) -> None:
    """Route the trial's terminal state through the one funnel, using the
    outcome the seal actually recorded (sealed or anomaly). First legal
    trigger wins inside finish_trial; a losing trigger cannot rewrite."""
    row = conn.execute(
        "SELECT status FROM sealed_answers WHERE trial_id = ?", (trial_id,)
    ).fetchone()
    outcome = row["status"] if row is not None and row["status"] in ("sealed", "anomaly") else "anomaly"
    lifecycle.finish_trial(conn, trial_id, trigger, outcome, run_id=run_id)


def _fail_before_agent_start(conn: sqlite3.Connection, run: sqlite3.Row,
                             trigger: str, detail: str) -> None:
    """A run that can never start the agent (invalid contract, unsupported
    target) is terminal: a diagnostic anomaly — never a rerun, never a
    capability sample (#16)."""
    artifacts.mark_anomaly(conn, run["trial_id"], run["run_id"],
                           f"{trigger}: {detail}", trigger=trigger)
    lifecycle.finish_trial(conn, run["trial_id"], trigger, "anomaly",
                           run_id=run["run_id"], detail=detail)


def _seal_after_run(conn: sqlite3.Connection, run: sqlite3.Row, container_id: str | None,
                    trigger: str, root: Path, baseline_dir: Path,
                    contract: artifacts.ArtifactContract) -> None:
    """Freeze the workspace through the single seal entry point, using the
    contract resolved from the TaskVersion before the agent started. The seal
    happens while the container still exists — Harbor's later artifact
    collection is diagnostics only and never the official answer (#14)."""
    if container_id is None:
        # sealing cannot even be attempted (e.g. timeout before container
        # discovery): persist an explainable anomaly instead of leaving the
        # submission in limbo (#14)
        detail = "sealing skipped: no agent container discovered"
        artifacts.mark_anomaly(conn, run["trial_id"], run["run_id"], detail, trigger=trigger)
        runs.add_phase(conn, run["run_id"], "seal_failed", detail=detail)
        return
    try:
        receipt = artifacts.seal(conn, run, container_id, trigger, root, baseline_dir, contract)
    except Exception as exc:  # noqa: BLE001 — sealing failure is recorded, never fatal
        # no official answer exists: leave an execution-condition anomaly
        # behind (explainable state, never a capability sample) (#14)
        artifacts.mark_anomaly(conn, run["trial_id"], run["run_id"],
                               f"sealing failed: {type(exc).__name__}: {exc}", trigger=trigger)
        runs.add_phase(conn, run["run_id"], "seal_failed", detail=f"{type(exc).__name__}: {exc}")
        return
    runs.add_phase(conn, run["run_id"], "sealed", receipt_id=receipt["receipt_id"],
                   answer_digest=receipt["digest"])


def _record_timeout_verdict(conn: sqlite3.Connection, run: sqlite3.Row,
                            agent_started_at: dict | None, agent_timeout_sec: int) -> None:
    """Deadline, actual freeze, and tolerance verdict for a timeout seal (#16).

    The planned deadline anchors at the observed agent start plus the agent
    timeout; an answer frozen beyond that plus the trial grace window is a
    diagnostic anomaly, never a capability-curve sample.
    """
    from datetime import datetime, timedelta, timezone

    from . import lifecycle

    if agent_started_at is None:
        return  # the agent never started; no deadline to compare against
    started = datetime.fromisoformat(agent_started_at["at"])
    deadline = started + timedelta(seconds=agent_timeout_sec)
    trial = conn.execute(
        "SELECT experiment_id FROM trials WHERE id = ?", (run["trial_id"],)
    ).fetchone()
    lifecycle.record_timeout_verdict(
        conn, trial["experiment_id"], run["trial_id"], run["run_id"],
        deadline, timedelta(seconds=TRIAL_GRACE_SEC),
    )


def _retrigger_answer(conn: sqlite3.Connection, trial_id: str, run_id: str) -> None:
    """Correct an AGENT_END-hook seal to 'timeout' when the deadline won the
    termination race (#16 reopen). The hook seals with the best trigger known
    at that moment ('exit' when nothing was submitted) — but a harbor agent
    timeout fires AGENT_END too, and the first legal termination reason is
    the timeout, not the mechanical agent end. Content, receipt, and a
    submit-won seal are never touched."""
    conn.execute(
        "UPDATE sealed_answers SET seal_trigger = 'timeout' WHERE trial_id = ?"
        " AND status = 'sealed' AND seal_trigger = 'exit'", (trial_id,))
    conn.commit()


def _network_deny_probe(container_id: str) -> dict:
    """Auditable enforcement evidence (#38): from inside the agent container,
    a connection to a non-allowlisted IP must fail. Blocked = the egress
    sidecar's nftables policy is live in the container's network namespace;
    an unexpected success means the restriction is not enforcing, and the
    run must never reach a model call."""
    probe = (
        "node -e \"fetch('http://1.1.1.1/',{signal:AbortSignal.timeout(4000)})"
        ".then(() => process.exit(0), () => process.exit(1))\""
    )
    result = command("docker", "exec", container_id, "sh", "-c", probe, check=False)
    if result.returncode == 0:
        raise RuntimeError(
            "network isolation probe unexpectedly connected to 1.1.1.1;"
            " egress allowlist is not enforcing — refusing to run the agent")
    return {"target": "1.1.1.1", "result": "blocked"}


async def execute_run(conn: sqlite3.Connection, run: sqlite3.Row, root: Path) -> None:
    run_id = run["run_id"]
    trial = conn.execute("SELECT * FROM trials WHERE id = ?", (run["trial_id"],)).fetchone()
    task_content = json.loads(
        conn.execute("SELECT content FROM versions WHERE id = ?", (trial["task_version_id"],)).fetchone()["content"]
    )
    profile = json.loads(
        conn.execute("SELECT content FROM versions WHERE id = ?", (trial["config_version_id"],)).fetchone()["content"]
    )

    # resolve the task-declared artifact contract ONCE, before any side
    # effect: it governs baseline, collection, validation, manifest, and
    # recovery; missing/invalid/mismatched contract fails before the agent
    # starts — never a silent default (#14 reopen)
    try:
        contract = artifacts.resolve_task_contract(task_content)
    except artifacts.SealError as exc:
        runs.finish_run(conn, run_id, "error", runs.EXIT_CONTRACT_INVALID,
                        f"task artifact contract invalid: {exc}")
        _fail_before_agent_start(conn, run, "contract_invalid", str(exc))
        return

    # resolve the instruction from its single declared source and verify the
    # registered environment bytes against the content-addressed store before
    # any container build or paid call — never an empty workspace/prompt
    # fallback (#20 reopen)
    try:
        task_assets = json.loads(conn.execute(
            "SELECT assets FROM versions WHERE id = ?", (trial["task_version_id"],)
        ).fetchone()["assets"])
        instruction, env_dir = environments.resolve_instruction(task_content, task_assets, root)
    except environments.EnvironmentInvalid as exc:
        runs.finish_run(conn, run_id, "error", runs.EXIT_ENVIRONMENT_INVALID,
                        f"task environment invalid: {exc}")
        _fail_before_agent_start(conn, run, "environment_invalid", str(exc))
        return

    # fail before any side effect on unsupported profiles
    try:
        harness, agent_timeout_sec = translate_profile(profile)
    except UnsupportedTarget as exc:
        runs.finish_run(conn, run_id, "error", runs.EXIT_UNSUPPORTED_TARGET, str(exc))
        _fail_before_agent_start(conn, run, "unsupported_target", str(exc))
        return

    image_ref = IMAGE
    agent_import_path = "aco.fake_agent:FakeAgent"
    skill_mounts: list[dict] = []
    env_overrides: dict = {}
    if harness == PI_HARNESS:
        parsed_profile = parse_config_content(profile)
        # the declared prompt digest is a controlled condition (#36 reopen):
        # the instruction the agent will run on must be exactly the bytes the
        # profile pinned, checked before any container or paid call
        instruction = task_content.get("prompt", "")
        requested_prompt = parsed_profile.prompt_digest
        observed_prompt = ("sha256:"
                           + hashlib.sha256(instruction.encode()).hexdigest())
        if requested_prompt != observed_prompt:
            detail = (f"prompt digest mismatch: requested={requested_prompt}"
                      f" observed={observed_prompt}")
            runs.finish_run(conn, run_id, "error", runs.EXIT_HARNESS_FAILURE, detail)
            # target_failure: the sealed_answers trigger vocabulary (0012)
            # has no dedicated environment trigger yet (#57's 0015 adds one)
            _fail_before_agent_start(conn, run, "target_failure", detail)
            return
        skill_state = resolve_skill_mounts(
            conn, parsed_profile, root)
        runs.add_phase(conn, run_id, "skills", skills=skill_state,
                       verified=all(s["verified"] for s in skill_state))
        if not all(s["verified"] for s in skill_state):
            # declared skill bytes diverge from the registered digest (#39):
            # terminal execution anomaly before any container or paid call
            bad = next(s for s in skill_state if not s["verified"])
            runs.finish_run(conn, run_id, "error", runs.EXIT_HARNESS_FAILURE,
                            f"skill {bad['name']}@{bad['version']} failed verification:"
                            f" requested={bad['requested']} observed={bad['observed']}")
            _fail_before_agent_start(conn, run, "skill_mismatch",
                                     f"{bad['name']}@{bad['version']}:"
                                     f" {bad['observed']}")
            return
        # verified bytes are the mount source; :ro makes them exactly what
        # the container sees (#39)
        skill_mounts = [{"name": s["name"], "host_dir": s["host_dir"]}
                        for s in skill_state]
        image_ref = await asyncio.to_thread(pi_agent.ensure_image)
        # the declared environment digest is verified against the image that
        # will actually run (#36 reopen): a rebuilt image with different
        # bytes must never pass as the same controlled condition
        observed_image = await asyncio.to_thread(image_digest, image_ref)
        requested_image = parsed_profile.environment
        runs.add_phase(conn, run_id, "image_digest", requested=requested_image,
                       observed=observed_image)
        if observed_image != requested_image:
            detail = (f"environment image digest mismatch:"
                      f" requested={requested_image} observed={observed_image}")
            runs.finish_run(conn, run_id, "error", runs.EXIT_HARNESS_FAILURE, detail)
            _fail_before_agent_start(conn, run, "target_failure", detail)
            return
        agent_import_path = "aco.pi_agent:PiAgent"
        # declared resources become enforced environment overrides (#36 reopen)
        if parsed_profile.resources is not None:
            if parsed_profile.resources.cpus is not None:
                env_overrides["override_cpus"] = parsed_profile.resources.cpus
            if parsed_profile.resources.memory_mb is not None:
                env_overrides["override_memory_mb"] = parsed_profile.resources.memory_mb
        # the adapter reads these (same process): profile, db root, run id
        os.environ["ACO_TARGET_PROFILE"] = json.dumps(profile, sort_keys=True)
        os.environ["ACO_DATA_ROOT"] = str(root)
        os.environ["ACO_RUN_ID"] = run_id

    from harbor.models.trial.config import (
        AgentConfig,
        EnvironmentConfig,
        TaskConfig,
        TrialConfig,
        VerifierConfig,
    )
    from harbor.trial.hooks import TrialEvent
    from harbor.trial.trial import Trial

    work_dir = root / "runs" / run_id
    baseline_dir = root / "sealing" / run_id / "baseline"
    gateway = None
    allowed_hosts = None
    if harness == PI_HARNESS:
        # all provider and session traffic from the evaluated container
        # traverses the per-trial gateway; the adapter renders its provider
        # base URL against the gateway (#38)
        gateway = gateway_config(root, run_id)
        gateway["evidence_dir"].mkdir(parents=True, exist_ok=True)
        upstream_path = urllib.parse.urlsplit(gateway["provider_upstream"]).path
        os.environ["ACO_PROVIDER_BASE_URL"] = (
            f"http://{gateway['ip']}{upstream_path}")
        allowed_hosts = [gateway["ip"]]
        # auditable network policy for this trial: the allowlist is exactly
        # the gateway; enforcement is harbor's egress sidecar, verified
        # in-kernel by the deny probe at agent start
        runs.add_phase(conn, run_id, "network_policy",
                       policy_version=NETWORK_POLICY_VERSION, policy="allowlist",
                       allowed_targets=allowed_hosts,
                       provider_upstream_host=urllib.parse.urlsplit(
                           gateway["provider_upstream"]).hostname,
                       session_upstream_host=urllib.parse.urlsplit(
                           gateway["session_upstream"]).hostname,
                       enforcement="harbor-egress-sidecar", harbor_version=HARBOR_VERSION)
    task_dir = build_task_dir(work_dir, instruction, agent_timeout_sec,
                              run_id, harness=harness, skill_mounts=skill_mounts,
                              allowed_hosts=allowed_hosts, gateway=gateway,
                              environment_dir=env_dir)
    trials_dir = work_dir / "trials"
    container_id = None
    agent_started_at: dict | None = None

    async def on_agent_start(_event):
        nonlocal container_id, agent_started_at
        container_id = await asyncio.to_thread(discover_container, run_id)
        runs.mark_running(conn, run_id, container_id=container_id, image=image_ref)
        lifecycle.mark_trial_running(conn, run["trial_id"])  # claimed -> running (#16)
        security = await asyncio.to_thread(container_security_summary, container_id)
        agent_started_at = runs.add_phase(conn, run_id, "agent_start", container_id=container_id,
                                          container_security=security)
        # pre-agent baseline for the manifest's added/modified/deleted diff
        await asyncio.to_thread(artifacts.snapshot_baseline, container_id, baseline_dir, contract)
        if harness == PI_HARNESS:
            # enforcement evidence BEFORE any model call: a non-allowlisted
            # target must be unreachable from the agent container (#38)
            probe = await asyncio.to_thread(_network_deny_probe, container_id)
            runs.add_phase(conn, run_id, "network_deny_probe", **probe)

    submit_stopped = False
    agent_ended = False

    async def on_agent_end(_event):
        # the seal lives here because the container only exists while harbor's
        # run() is in flight — harbor reaps it afterwards. The trigger is the
        # best knowledge at this moment; handle_timeout corrects it to
        # 'timeout' when the deadline actually won the termination race.
        nonlocal agent_ended
        agent_ended = True
        runs.add_phase(conn, run_id, "agent_end")
        trigger = "submit" if _has_submission(conn, run["trial_id"]) else "exit"
        await asyncio.to_thread(_seal_after_run, conn, run, container_id,
                                trigger, root, baseline_dir, contract)

    trial_config = TrialConfig(
        task=TaskConfig(path=task_dir),
        trial_name=f"aco-{run_id[:12]}",
        trials_dir=trials_dir,
        agent=AgentConfig(import_path=agent_import_path),
        environment=EnvironmentConfig(extra_docker_compose=[task_dir / "offline.yaml"],
                                      **env_overrides),
        # harbor scoring is disabled permanently; ACO owns all official results
        verifier=VerifierConfig(disable=True),
        artifacts=["/workspace"],
    )
    try:
        trial_obj = await Trial.create(trial_config)
    except RuntimeError as exc:
        if "overlaps with other one" not in str(exc):
            raise  # a leaked network from a crashed earlier trial (#38)
        preserved = await asyncio.to_thread(_remove_stale_trial_networks, run_id)
        if preserved:
            # our octet collides with a live sibling trial (#52); auditable
            runs.add_phase(conn, run_id, "network_retry", preserved=preserved,
                           subnet=gateway_subnet(run_id))
        try:
            trial_obj = await Trial.create(trial_config)
        except RuntimeError as retry_exc:
            if "overlaps with other one" not in str(retry_exc):
                raise
            raise _subnet_blocked_error(run_id, preserved) from retry_exc
    trial_obj.add_hook(TrialEvent.AGENT_START, on_agent_start)
    trial_obj.add_hook(TrialEvent.AGENT_END, on_agent_end)

    runs.observe_run(conn, run_id, ADAPTER_VERSION, HARBOR_VERSION, str(trials_dir))

    submit_stopped = False

    async def watch_submit():
        """First legal trigger wins (#16 reopen): the moment a persisted
        submit intent exists, stop the agent container. Writes racing the
        SUBMIT_POLL_SEC window land in the frozen post-stop state; nothing
        written after the stop can reach the sealed answer."""
        nonlocal submit_stopped
        while True:
            await asyncio.sleep(SUBMIT_POLL_SEC)
            # agent_ended check: once the agent ended, the AGENT_END hook owns
            # the terminal seal — stopping a sealing container would only
            # misattribute the run's exit
            if _has_submission(conn, run["trial_id"]) and not agent_ended:
                runs.add_phase(conn, run_id, "submit_watch_fired")
                if container_id is not None:
                    await asyncio.to_thread(
                        command, "docker", "stop", "-t", "1", container_id, check=False)
                    submit_stopped = True
                return

    async def handle_timeout(source: str, detail: str) -> None:
        """One path for the ACO outer deadline and Harbor's own agent
        timeout (#16 reopen): seal, planned deadline / actual freeze /
        tolerance verdict, terminal run and trial."""
        runs.add_phase(conn, run_id, "trial_timeout", source=source)
        await asyncio.to_thread(_seal_after_run, conn, run, container_id,
                                "timeout", root, baseline_dir, contract)
        await asyncio.to_thread(_retrigger_answer, conn, run["trial_id"], run_id)
        _record_timeout_verdict(conn, run, agent_started_at, agent_timeout_sec)
        runs.finish_run(conn, run_id, "error", runs.EXIT_TIMEOUT, detail)
        await asyncio.to_thread(_finish_trial_from_answer, conn, run["trial_id"], run_id, "timeout")

    run_task = asyncio.create_task(trial_obj.run())
    watcher = asyncio.create_task(watch_submit())
    try:
        try:
            result = await asyncio.wait_for(run_task, timeout=agent_timeout_sec + TRIAL_GRACE_SEC)
        except asyncio.TimeoutError:
            await handle_timeout("outer_deadline",
                                 f"trial exceeded {agent_timeout_sec + TRIAL_GRACE_SEC}s")
            return
        except Exception as exc:  # noqa: BLE001 — Harbor may raise the agent timeout
            if "AgentTimeoutError" in f"{type(exc).__name__}: {exc}":
                await handle_timeout("harbor_agent_timeout", str(exc))
                return
            raise

        exception = result.exception_info.exception_type if result.exception_info else None
        if exception and "AgentTimeoutError" in str(exception):
            # Harbor's own agent timeout lands on the same verdict path as the
            # outer deadline — it never bypasses eligibility (#16 reopen)
            await handle_timeout("harbor_agent_timeout", str(exception))
            return

        trigger = "submit" if _has_submission(conn, run["trial_id"]) else "exit"
        if trigger == "submit" and (exception or submit_stopped):
            # the supervisor ended the run: the submit watchdog stopped the
            # container (a harbor-stopped container raises no exception), or
            # the agent died after submit — submit is the termination reason
            exit_kind, detail = runs.EXIT_SUBMIT, "agent ended by submit-won container stop"
        else:
            exit_kind, detail = (runs.EXIT_AGENT_ERROR, exception) if exception else (runs.EXIT_NORMAL, None)
        # an execution-condition failure the adapter recorded (#37) outranks
        # the mechanical exit: never a capability fail, always an anomaly
        failure = _target_failure(conn, run_id)
        if failure is not None:
            failure_class = failure.get("failure_class", "")
            exit_kind = (runs.EXIT_PROVIDER_FAILURE if failure_class.startswith("provider")
                         else runs.EXIT_HARNESS_FAILURE)
            detail = f"{failure_class}: {failure.get('detail')}"
        runs.add_phase(conn, run_id, "trial_finished", exception=exception,
                       verifier_scored=result.verifier_result is not None)
        runs.finish_run(conn, run_id, "finished", exit_kind, detail)
        await asyncio.to_thread(_finish_trial_from_answer, conn, run["trial_id"], run_id, trigger)
    finally:
        watcher.cancel()
        if container_id is None:
            # never discovered: no seal can reference it; remove labeled leftovers
            cleanup_container(run_id)
        # belt for interrupted harbor runs: compose down normally removes the
        # per-trial network; crashes (killed supervisor, failed up) leak it
        # and the leaked subnet blocks future trials (#38)
        await asyncio.to_thread(
            command, "docker", "network", "rm", _trial_network_name(run_id),
            check=False)


def main() -> int:
    import faulthandler
    import signal
    # SIGUSR1 dumps every thread's stack to stderr (supervisor.log) — the
    # supervisor is a black-box subprocess, this is the live-debug hatch
    faulthandler.register(signal.SIGUSR1, file=sys.stderr)
    parser = argparse.ArgumentParser(prog="aco-supervisor", description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--data-root", required=True)
    args = parser.parse_args()

    root = Path(args.data_root).expanduser()
    conn = db.connect(root / "aco.db")
    run = runs.get_run(conn, args.run_id)
    if run is None:
        print(f"run {args.run_id} does not exist", file=sys.stderr)
        return 2
    try:
        asyncio.run(execute_run(conn, run, root))
    except BaseException as exc:  # noqa: BLE001 — the supervisor log IS stderr
        import traceback
        traceback.print_exc()
        # leave diagnostics; the manager reaps leftovers (container/pid)
        if isinstance(exc, Exception) and runs.get_run(
                conn, args.run_id)["status"] in ("launching", "running"):
            runs.finish_run(conn, args.run_id, "error", runs.EXIT_AGENT_ERROR,
                            f"{type(exc).__name__}: {exc}")
        # the trial must land in a terminal state even when the supervisor
        # itself crashes: a diagnostic anomaly, never a rerun (#16). A seal
        # that already won stays sealed (mark_anomaly is exactly-once).
        artifacts.mark_anomaly(conn, run["trial_id"], args.run_id,
                               f"supervisor crash: {type(exc).__name__}: {exc}",
                               trigger="supervisor_crash")
        _finish_trial_from_answer(conn, run["trial_id"], args.run_id, "supervisor_crash")
        cleanup_container(args.run_id)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
