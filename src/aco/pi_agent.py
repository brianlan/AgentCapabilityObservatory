"""ACO Pi adapter at Harbor's installed-agent seam (#37, ADR 0002).

Runs the pinned Pi 0.84.1 inside the trial container against the first real
TargetProfile (``ark-agent-plan/glm-5.3-flash:max``). Unlike Harbor's stock Pi
agent, the rendered custom-model config carries full model metadata (reasoning
flag and ``thinkingLevelMap`` including ``max``), undeclared skills/extensions/
prompt templates/themes are disabled, the host ``~/.pi`` is never mounted, and
the Pi JSON transcript is parsed into trial observation and provider-failure
classification (a provider failure is never a capability fail).

Everything the agent needs reaches it through the supervisor environment:
ACO_BASE_URL/ACO_SESSION_TOKEN (Session surface, ADR 0001), ACO_DATA_ROOT,
ACO_RUN_ID, ACO_TARGET_PROFILE (the normalized TargetProfile), and the
credential: ``ark-agent-plan-main`` resolves from ARK_AGENT_PLAN_API_KEY in
that trusted environment. The key value is only ever injected into this one
trial's Pi process — never rendered into config, database, or logs.
"""

import base64
import json
import os
import sqlite3
import urllib.request
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.base import (
    BaseInstalledAgent,
    NonZeroAgentExitCodeError,
)
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .models import TargetProfile

PI_PACKAGE = "@earendil-works/pi-coding-agent"
PI_VERSION = "0.84.1"
ADAPTER_VERSION = "0.1.1"
PI_IMAGE_TAG = f"aco-pi-agent:{PI_VERSION}"
# pinned base for the trial image: node ships the runtime pi needs; the pi
# package itself is installed at image build and verified at agent setup
NODE_IMAGE = (
    "node:22-bookworm-slim"
    "@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5"
)
PI_CONFIG_DIR = "/tmp/aco-pi-agent"  # trial-local, starts empty (ADR 0002)

# first real Target knowledge (issue #37): the only provider/model/credential
# this adapter knows how to render; anything else fails explicitly
ARK_PROVIDER = "ark-agent-plan"
ARK_CREDENTIAL_REF = "ark-agent-plan-main"
ARK_CREDENTIAL_ENV = "ARK_AGENT_PLAN_API_KEY"
ARK_BASE_URL_ENV = "ARK_AGENT_PLAN_BASE_URL"
ARK_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/plan/v3"
ARK_MODEL = "glm-5.3-flash"
# Pi's canonical levels -> provider reasoning effort; null = unsupported
ARK_THINKING_LEVEL_MAP = {
    "off": None, "minimal": None, "low": None, "medium": None,
    "high": "high", "xhigh": None, "max": "max",
}
SUPPORTED_THINKING = tuple(level for level, mapped in ARK_THINKING_LEVEL_MAP.items() if mapped)
MODEL_METADATA = {
    "name": "GLM 5.3 Flash",
    "reasoning": True,
    "input": ["text"],
    "contextWindow": 262144,
    "maxTokens": 8192,
}
DEFAULT_AGENT_TIMEOUT_SEC = 120

# pi talks to the per-trial gateway (#38) directly; inherited proxy vars
# would hijack the connection (host-injected proxies are unreachable from
# the container network namespace)
_UNSET_PROXIES = (
    "env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy"
    " -u ALL_PROXY -u all_proxy -u NO_PROXY -u no_proxy"
)
# isolation flags: no undeclared extensions/prompt templates/themes, no
# workspace context files, no saved session, no startup network ops.
# Skills are handled separately: the default set is empty -> --no-skills;
# declared skills are mounted read-only and loaded explicitly (#39).
_PI_CONTAINER_SKILL_ROOT = "/opt/aco-skills"
_PI_ISOLATION_FLAGS = (
    "--no-extensions --no-prompt-templates"
    " --no-themes --no-context-files --no-session --offline"
)


def render_agent_flags(profile: TargetProfile) -> str:
    """Isolation flags for one run: default disables all skill loading
    (reproducible empty baseline); a declared set loads exactly those,
    in config order, via explicit --skill paths (#39)."""
    if not profile.skills:
        return f"--no-skills {_PI_ISOLATION_FLAGS}"
    explicit = " ".join(
        f"--skill {_PI_CONTAINER_SKILL_ROOT}/{ref.name}" for ref in profile.skills)
    return f"{_PI_ISOLATION_FLAGS} {explicit}"


def ark_base_url() -> str:
    """The provider upstream the gateway forwards to (#38): the profile's
    fixed endpoint (mock in tests), from the trusted supervisor environment."""
    return os.environ.get(ARK_BASE_URL_ENV, ARK_DEFAULT_BASE_URL).rstrip("/")


def is_real_ark_endpoint() -> bool:
    """True when the effective provider upstream is the real Ark host — the
    billing path, regardless of credential presence (#38 reopen)."""
    from urllib.parse import urlsplit

    return urlsplit(ark_base_url()).hostname == urlsplit(ARK_DEFAULT_BASE_URL).hostname


def provider_base_url() -> str:
    """The base URL rendered into pi's config: the per-trial gateway when the
    supervisor provides one (#38), the profile endpoint otherwise (unit
    renders without a gateway). The key travels as env either way."""
    return os.environ.get("ACO_PROVIDER_BASE_URL", ark_base_url()).rstrip("/")


def render_dockerfile() -> str:
    """The pinned trial image: node at a digest, pi at an exact version,
    python3 for the declared task environment (first-batch tasks run their
    checks with python — provided at build time, never at run time, #38
    reopen).

    WORKDIR /workspace: the artifact contract seals /workspace (#14) and the
    agent's deliverables land there — the dir must exist before the agent
    runs, never depend on agent behavior (#38; harbor's task template does
    the same)."""
    return (
        f"FROM {NODE_IMAGE}\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends python3 \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
        f"RUN npm install -g --ignore-scripts {PI_PACKAGE}@{PI_VERSION}\n"
        "WORKDIR /workspace"
    )


def render_models_json(profile: TargetProfile, base_url: str) -> dict[str, Any]:
    """Frozen non-sensitive provider config; the API key is an env reference
    resolved by pi at call time — the value never enters the rendered file."""
    if profile.provider != ARK_PROVIDER:
        raise ValueError(f"unsupported provider {profile.provider!r}; only {ARK_PROVIDER!r} renders")
    if profile.provider_api_style != "openai-responses":
        raise ValueError(
            f"unsupported provider_api_style {profile.provider_api_style!r};"
            " only 'openai-responses' renders"
        )
    if profile.model != ARK_MODEL:
        raise ValueError(f"unsupported model {profile.model!r}; only {ARK_MODEL!r} renders")
    if profile.thinking not in SUPPORTED_THINKING:
        raise ValueError(
            f"unsupported thinking level {profile.thinking!r};"
            f" supported: {', '.join(SUPPORTED_THINKING)}"
        )
    return {
        "providers": {
            profile.provider: {
                "baseUrl": base_url,
                "apiKey": f"${ARK_CREDENTIAL_ENV}",
                "api": profile.provider_api_style,
                "models": [{
                    "id": profile.model,
                    **MODEL_METADATA,
                    "thinkingLevelMap": dict(ARK_THINKING_LEVEL_MAP),
                }],
            }
        }
    }


def validate_profile(profile: TargetProfile) -> int:
    """Explicit checks for the first real target; returns the agent timeout.

    The declared-is-enforced contract (#36 reopen): every controlled field
    this profile declares must actually take effect, and anything the pi
    path cannot enforce is rejected before the paid call —
    - harness_version / adapter_version: pinned and rendered into the image;
    - environment: verified against the built image's observed digest;
    - resources.timeout_sec: the agent timeout in task.toml;
    - resources.cpus / memory_mb: Harbor environment overrides;
    - resources.network: NEVER declarable — the egress policy is ACO
      infrastructure (the fixed per-trial gateway allowlist, #38), so a
      declared offline/online value would be an unenforced claim.
    """
    if profile.harness_version != PI_VERSION:
        raise ValueError(f"unsupported harness_version {profile.harness_version!r}; pin {PI_VERSION!r}")
    if profile.adapter_version != ADAPTER_VERSION:
        raise ValueError(f"unsupported adapter_version {profile.adapter_version!r}; pin {ADAPTER_VERSION!r}")
    if profile.assistance_mode != "none":
        raise ValueError(f"unsupported assistance_mode {profile.assistance_mode!r}; only 'none' executes")
    if profile.credentials != [ARK_CREDENTIAL_REF]:
        raise ValueError(
            f"unsupported credentials {profile.credentials!r};"
            f" exactly [{ARK_CREDENTIAL_REF!r}] is supported"
        )
    if profile.environment is None:
        raise ValueError(
            "environment is required: the pinned agent image digest is a"
            " controlled condition verified at run time")
    if profile.resources is not None and profile.resources.network is not None:
        raise ValueError(
            f"unsupported resources.network {profile.resources.network!r};"
            " the egress policy is the fixed per-trial gateway allowlist"
            " and cannot be overridden")
    if profile.resources is not None and profile.resources.timeout_sec is not None:
        return profile.resources.timeout_sec
    return DEFAULT_AGENT_TIMEOUT_SEC


# ---------------------------------------------------------------- transcript

def parse_transcript(raw: str) -> dict[str, Any]:
    """Parse a pi --mode json transcript into observation + failure class.

    Returns {"observation": {...}, "failure": str | None}. The observation
    carries only what the transcript actually shows; anything missing stays
    unknown (absent). Failure classes follow the issue's diagnostics:
    provider_auth / provider_unavailable / provider_transient. A session that
    ended cleanly is never a provider failure, even if an earlier attempt
    retried through a transient error.
    """
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    usage = {"input": 0, "output": 0}
    tool_calls: list[str] = []
    final: dict[str, Any] | None = None
    final_error: str | None = None
    for event in events:
        kind = event.get("type")
        if kind == "message_end":
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                continue
            final = message  # last assistant message wins
            message_usage = message.get("usage") or {}
            usage["input"] += message_usage.get("input", 0)
            usage["output"] += message_usage.get("output", 0)
        elif kind == "turn_end":
            for tool_result in event.get("toolResults") or []:
                if tool_result.get("toolName"):
                    tool_calls.append(tool_result["toolName"])
        elif kind == "auto_retry_end" and event.get("finalError"):
            final_error = event["finalError"]  # only set when retries gave up

    observation: dict[str, Any] = {"source": "pi_json_transcript"}
    if final is not None:
        observation.update({
            "provider": final.get("provider"),
            "model": final.get("model"),
            "api": final.get("api"),
            "response_id": final.get("responseId"),
            "stop_reason": final.get("stopReason"),
        })
    observation["usage"] = usage
    observation["tool_calls"] = tool_calls

    error = None
    if final is not None and final.get("stopReason") == "error":
        error = final.get("errorMessage") or final_error
    elif final is None and final_error:
        error = final_error
    if error is not None:
        observation["error"] = error
    failure = _classify_provider_error(error) if error is not None else None
    return {"observation": observation, "failure": failure}


def _classify_provider_error(error: str) -> str:
    """Map a transcript error to the issue's provider diagnostics."""
    lowered = error.lower()
    if any(needle in lowered for needle in (
        "401", "403", "unauthorized", "invalid api key", "api key", "not logged in",
        "authentication", "forbidden",
    )):
        return "provider_auth"
    if any(needle in lowered for needle in (
        "429", "rate limit", "too many requests", "quota", "usage limit",
    )):
        return "provider_transient"
    if any(needle in lowered for needle in (
        "connection", "unreachable", "econnrefused", "dns", "timeout",
        "overloaded", "500", "502", "503", "unavailable",
    )):
        return "provider_unavailable"
    return "provider_transient"  # provider reported an error we cannot bucket better


# ------------------------------------------------------------------ the agent

def _session_request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        os.environ["ACO_BASE_URL"].rstrip("/") + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


class PiAgent(BaseInstalledAgent):
    """Narrow ACO adapter: renders the frozen Pi runtime from the
    TargetProfile, runs one fresh session per trial, records observation and
    provider diagnostics. Harbor keeps container execution; the ACO supervisor
    keeps termination, sealing, and scoring."""

    _observed_version: str | None = None

    @staticmethod
    def name() -> str:
        return "aco-pi-agent"

    @override
    def get_version_command(self) -> str | None:
        return "pi --version"

    @override
    def parse_version(self, stdout: str) -> str:
        return stdout.strip().splitlines()[-1].strip()

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        # the pinned image already carries pi; setup only verifies the exact
        # version so a drifted image fails loudly before any model call
        result = await self.exec_as_agent(environment, command="pi --version")
        self._observed_version = self.parse_version(result.stdout or "")
        if self._observed_version != PI_VERSION:
            raise RuntimeError(
                f"pinned pi {PI_VERSION} required, image ships {self._observed_version!r}")

    def _credential(self, profile: TargetProfile) -> str:
        """Resolve the profile's credential ref from the trusted supervisor
        environment. The value never lands in config, database, or logs."""
        if profile.credentials != [ARK_CREDENTIAL_REF]:
            raise NonZeroAgentExitCodeError(
                f"unsupported credential ref(s) {profile.credentials!r}")
        value = os.environ.get(ARK_CREDENTIAL_ENV)
        if not value:
            raise NonZeroAgentExitCodeError(
                f"credential ref {ARK_CREDENTIAL_REF!r} is not configured:"
                f" set {ARK_CREDENTIAL_ENV} in the trusted manager environment"
                " (explicit failure before any model call)"
            )
        return value

    @override
    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None:
        profile = TargetProfile.model_validate(json.loads(os.environ["ACO_TARGET_PROFILE"]))

        # 1. claim the trial's unique task through the restricted Session API
        token = os.environ["ACO_SESSION_TOKEN"]
        task = _session_request("GET", "/v1/session/task", token)
        trial_id = task["trial_id"]
        prompt = task["instruction"]

        # 2. resolve the credential and render the frozen config into a
        # trial-local empty Pi home; the file carries the env reference,
        # never the key value. Failures here are explicit, recorded, and
        # never reach a model call.
        try:
            key = self._credential(profile)
            models_json = render_models_json(profile, provider_base_url())
        except NonZeroAgentExitCodeError as exc:
            self._record_failure(trial_id, "credential_missing", str(exc))
            raise
        except ValueError as exc:
            self._record_failure(trial_id, "harness_startup", f"config render failed: {exc}")
            raise
        encoded_config = base64.b64encode(json.dumps(models_json).encode()).decode()
        await self.exec_as_agent(
            environment,
            command=(f"mkdir -p {PI_CONFIG_DIR} && chmod 700 {PI_CONFIG_DIR} &&"
                     f" umask 177 && printf '%s' {encoded_config}"
                     f" | base64 -d > {PI_CONFIG_DIR}/models.json"),
        )

        # 3. one fresh session: the TaskVersion instruction only — no identity text.
        # The key travels as exec env (Harbor redacts sensitive env in logs),
        # never inside the command string. pi's transcript is redirected to a
        # file and fetched separately: harbor's exec raises on non-zero exit
        # and truncates embedded output, so the file is the reliable source.
        encoded_prompt = base64.b64encode(prompt.encode()).decode()
        try:
            await self.exec_as_agent(
                environment,
                command=(
                    f"printf '%s' {encoded_prompt} | base64 -d > /tmp/aco-prompt.txt && "
                    f"{_UNSET_PROXIES} PI_CODING_AGENT_DIR={PI_CONFIG_DIR} pi"
                    f" --provider {profile.provider} --model {profile.model}"
                    f" --thinking {profile.thinking} --print --mode json"
                    f" {render_agent_flags(profile)}"
                    f" \"$(cat /tmp/aco-prompt.txt)\" </dev/null"
                    f" > /tmp/aco-pi.jsonl 2> /tmp/aco-pi.stderr"
                ),
                env={ARK_CREDENTIAL_ENV: key},
            )
            return_code = 0
        except RuntimeError:  # non-zero pi exit: the transcript still landed
            return_code = 1
        fetched = await self.exec_as_agent(
            environment, command="cat /tmp/aco-pi.jsonl")
        transcript = fetched.stdout or ""

        # 4. record evidence and classify BEFORE any terminal handling so a
        # provider failure can never win a capability seal (seal is exactly-once)
        parsed = parse_transcript(transcript)
        observation = dict(parsed["observation"])
        if self._observed_version:
            observation["harness_version"] = self._observed_version
        self._record_observation(trial_id, observation)

        if parsed["failure"] is not None:
            self._record_failure(trial_id, parsed["failure"],
                                 parsed["observation"].get("error"))
            raise NonZeroAgentExitCodeError(
                f"provider failure ({parsed['failure']}): {parsed['observation'].get('error')}")
        if return_code != 0:
            raise NonZeroAgentExitCodeError(
                f"pi exited {return_code} without a provider error"
                f" (agent failure); transcript tail: {transcript[-400:]}")

        # 5. end-intent on success: the workspace snapshot is the answer
        _session_request("POST", "/v1/session/submit", token,
                         {"idempotency_key": f"pi-{trial_id}"})

    def _record_observation(self, trial_id: str, observation: dict) -> None:
        conn = _open_db()
        try:
            conn.execute("UPDATE trials SET runtime_observation = ? WHERE id = ?",
                         (json.dumps(observation, sort_keys=True), trial_id))
            conn.commit()
        finally:
            conn.close()

    def _record_failure(self, trial_id: str, failure: str, error: str | None) -> None:
        from . import artifacts, runs

        conn = _open_db()
        try:
            run_id = os.environ["ACO_RUN_ID"]
            runs.add_phase(conn, run_id, "target_failure",
                           failure_class=failure, detail=error)
            artifacts.mark_anomaly(conn, trial_id, run_id,
                                   f"{failure}: {error}",
                                   trigger="target_failure")
        finally:
            conn.close()


def image_digest(image_ref: str) -> str:
    """The image's immutable content digest (docker inspect .Id, the config
    digest `sha256:<64 hex>`) — the observed half of the environment
    verification (#36 reopen) and the digest the trial pins (#38 reopen).
    Raises RuntimeError when the image does not exist locally."""
    import subprocess

    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image_ref],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"image inspect failed for {image_ref}: {result.stderr[-400:]}")
    return result.stdout.strip()


def build_image() -> str:
    """Pre-build the pinned trial image; return its immutable content digest.

    Runs OUTSIDE the trial hot path (#38 reopen): `aco build-agent-image`
    exposes this operation to operators. A trial start only inspects the
    result — no docker build, no npm, no registry access there. docker's layer
    cache makes an unchanged rebuild a no-op, and there is deliberately no
    tag-exists short-circuit: a stale image must never survive a Dockerfile
    change (e.g. the WORKDIR fix, #38). --network host is build-time only (npm
    registry reachability on hosts with an unreachable daemon proxy); the
    trial runtime itself stays restricted (#38).
    """
    import subprocess
    import tempfile

    # A hung operator-side build must expire rather than leave the build
    # process blocked indefinitely.
    timeout = float(os.environ.get("ACO_AGENT_IMAGE_BUILD_TIMEOUT", "1800"))
    with tempfile.TemporaryDirectory(prefix="aco-pi-image-") as temp:
        (Path(temp) / "Dockerfile").write_text(render_dockerfile())
        try:
            build = subprocess.run(
                ["docker", "build", "--network", "host", "-t", PI_IMAGE_TAG, temp],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"agent image build exceeded {timeout:.0f}s — check registry/"
                "npm reachability or run `aco build-agent-image` manually") from None
    if build.returncode != 0:
        raise RuntimeError(f"pinned pi image build failed: {build.stderr[-800:]}")
    return image_digest(PI_IMAGE_TAG)


def _open_db() -> sqlite3.Connection:
    from . import db
    return db.connect(os.environ["ACO_DATA_ROOT"] + "/aco.db")
