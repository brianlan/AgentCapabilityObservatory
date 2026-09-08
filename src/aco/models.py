"""Pydantic request/response models. OpenAPI is generated from these."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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
