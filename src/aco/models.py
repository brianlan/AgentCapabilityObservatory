"""Pydantic request/response models. OpenAPI is generated from these."""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VersionRef(BaseModel):
    name: str
    version: str


class AssetRef(BaseModel):
    name: str
    digest: str


class ConfigContent(BaseModel):
    """Tested configuration. Credentials are logical references only, never values."""

    model_config = ConfigDict(extra="forbid")

    harness: str
    model: str
    provider: str | None = None
    skills: list[str] = Field(default_factory=list)  # default: no skills loaded
    credentials: list[str] = Field(default_factory=list)


class SkillVersionRef(BaseModel):
    """Ordered skill reference in a TargetProfile; skills default to empty
    (nothing is inferred from the host environment, #36)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str


class ExecutionPolicy(BaseModel):
    """Resource / timeout / network conditions of a target (#36)."""

    model_config = ConfigDict(extra="forbid")

    cpus: float | None = None
    memory_mb: int | None = None
    timeout_sec: int | None = None
    network: Literal["offline", "online"] | None = None


class TargetProfile(BaseModel):
    """Versioned, normalized, secret-free controlled conditions (#36).

    schema_version 1. Any change to a declared field changes the Trial
    fingerprint and therefore the comparable result series. Credential
    fields are logical references only — there is no field a secret value
    could even be placed in (extra=forbid)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    harness: str
    harness_version: str | None = None  # e.g. "0.84.1" for pi
    model: str
    thinking: str | None = None  # e.g. "max"
    provider: str | None = None  # e.g. "ark-agent-plan"
    provider_api_style: str | None = None
    adapter_version: str | None = None
    assistance_mode: Literal["none", "human"] = "none"
    prompt_digest: str | None = None  # content-addressed prompt reference
    environment: str | None = None  # agent environment image digest
    resources: ExecutionPolicy | None = None
    skills: list[SkillVersionRef] = Field(default_factory=list)  # ordered
    credentials: list[str] = Field(default_factory=list)  # refs only


def parse_config_content(content: dict) -> TargetProfile:
    """Registered config-version content -> normalized TargetProfile (#36).

    Content carrying schema_version uses the v1 profile path; anything else
    is the legacy fake-config shape, normalized into the same fields (skill
    names become unversioned refs, assistance_mode defaults to none). Unknown
    fields fail explicitly on both paths (extra=forbid) — never ignored."""
    if isinstance(content, dict) and "schema_version" in content:
        return TargetProfile.model_validate(content)
    legacy = ConfigContent.model_validate(content)
    return TargetProfile(
        harness=legacy.harness,
        model=legacy.model,
        provider=legacy.provider,
        skills=[SkillVersionRef(name=s, version="") for s in legacy.skills],
        credentials=legacy.credentials,
    )


# harnesses whose trials call a real paid provider (#38): creating an
# experiment with such a target requires the explicit allow_paid_run intent.
# Fail-closed by construction: new real harnesses must be added here to run.
REAL_PROVIDER_HARNESSES = frozenset({"pi"})


class SuiteContent(BaseModel):
    tasks: list[VersionRef] = Field(min_length=1)


class VersionRegistration(BaseModel):
    kind: Literal["task", "suite", "config", "scorer"]
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    content: dict[str, Any]
    assets: list[AssetRef] = Field(default_factory=list)


class VersionRecord(BaseModel):
    id: str
    kind: str
    name: str
    version: str
    content: dict[str, Any]
    assets: list[AssetRef]
    created_at: str


class ExperimentCreate(BaseModel):
    task: VersionRef | None = None
    suite: VersionRef | None = None
    targets: list[VersionRef] = Field(min_length=1)
    repetitions: int = Field(default=1, ge=1)
    allow_paid_run: bool = False  # explicit gate for real-provider targets (#38)


class TrialOut(BaseModel):
    id: str
    experiment_id: str
    repetition: int
    plan_order: int
    status: str
    task: VersionRef
    config: VersionRef
    requested: dict[str, Any]
    fingerprint: str | None = None  # pre-0011 rows backfill on read (#36)
    runtime_observation: dict[str, Any] | None = None


class ExperimentOut(BaseModel):
    id: str
    status: str
    requested: dict[str, Any]
    created_at: str
    trials: list[TrialOut]
    progress: dict[str, int]  # plan/cancel/anomaly coverage counts (#16)


class SubmitRequest(BaseModel):
    """End-intent only (#12): the official answer is the workspace snapshot
    sealed by the supervisor (#14), never a session-supplied body."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1)


# ponytail: no registry-with-port support in the repo pattern (localhost:5000/x);
# widen the character class if a private registry ever needs it
_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")


class ScorerContent(BaseModel):
    """Verifier bundle contract: digest-pinned image, entrypoint, and the
    machine-readable result schema the bundle must emit (#15)."""

    model_config = ConfigDict(extra="forbid")

    image: str  # anchored digest-pinned reference: repo@sha256:<64 hex>
    entrypoint: list[str] = Field(min_length=1)
    result_schema: str = Field(min_length=1)

    @field_validator("image")
    @classmethod
    def _digest_pinned(cls, value: str) -> str:
        if not _IMAGE_RE.match(value):
            raise ValueError("image must be digest-pinned (repo@sha256:<64 hex>)")
        return value


class VerificationCreate(BaseModel):
    """Create a scoring job for the trial's registered Sealed Answer."""

    verifier: VersionRef
    idempotency_key: str = Field(min_length=1)
