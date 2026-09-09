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


class TrialOut(BaseModel):
    id: str
    experiment_id: str
    repetition: int
    plan_order: int
    status: str
    task: VersionRef
    config: VersionRef
    requested: dict[str, Any]
    runtime_observation: dict[str, Any] | None = None


class ExperimentOut(BaseModel):
    id: str
    status: str
    requested: dict[str, Any]
    created_at: str
    trials: list[TrialOut]
    progress: dict[str, int]  # plan/cancel/anomaly coverage counts (#16)


class SubmitRequest(BaseModel):
    answer: Any
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
