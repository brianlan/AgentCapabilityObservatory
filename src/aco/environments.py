"""Content-addressed store for agent-visible task environments (#20 reopen).

One directory per environment digest under ``<data_root>/environments/``.
Admission publishes the public bundle here exactly once (atomic, verified,
idempotent); the supervisor and the Session API resolve registered task
versions from this store — never from the author's private source tree.

The instruction has exactly one declared source per task version:

- ``content["instruction"] = {"asset": "environment", "path": ...}`` — a
  path inside the registered immutable public environment asset, or
- ``content["prompt"]`` — a non-empty string, the single source for
  synthetic versions registered outside admission.

Anything else (both present, neither present, missing asset bytes, digest
mismatch, path traversal, empty file) raises :class:`EnvironmentInvalid`,
which the supervisor turns into a pre-agent execution anomaly and the
Session API refuses to serve — never an empty workspace or empty prompt.
"""

import os
import shutil
import uuid
from pathlib import Path

from .verification import runner

ENVIRONMENT_ROOT = "environments"
INSTRUCTION_ASSET = "environment"


class EnvironmentInvalid(Exception):
    """The registered environment or instruction source is unusable."""


def store_root(data_root: Path) -> Path:
    return Path(data_root) / ENVIRONMENT_ROOT


def _verify(dir_path: Path, expected_digest: str) -> None:
    observed = runner.bundle_digest(dir_path)
    if observed != expected_digest:
        raise EnvironmentInvalid(
            f"environment bytes do not match the registered digest:"
            f" expected={expected_digest} observed={observed}")


def publish(data_root: Path, src: Path, expected_digest: str) -> Path:
    """Publish the public environment tree into the store, atomically.

    Digest is verified on the source, on the staging copy, and on the
    published directory; an already-present directory with matching content
    makes this a no-op (idempotent re-import)."""
    dest = store_root(data_root) / expected_digest
    if dest.is_dir():
        _verify(dest, expected_digest)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.parent / f".import-{uuid.uuid4().hex}"
    try:
        shutil.copytree(src, staging)
        _verify(staging, expected_digest)
        try:
            os.replace(staging, dest)  # atomic publish; same filesystem
        except OSError:
            # a concurrent import published identical content first
            if runner.bundle_digest(dest) != expected_digest:
                raise EnvironmentInvalid(
                    f"environment already present with different content: {dest}")
            shutil.rmtree(staging, ignore_errors=True)
        _verify(dest, expected_digest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return dest


def resolve(data_root: Path, digest: str) -> Path:
    """The stored environment directory for a registered digest, re-verified."""
    dest = store_root(data_root) / digest
    if not dest.is_dir():
        raise EnvironmentInvalid(f"environment asset missing from store: {digest}")
    _verify(dest, digest)
    return dest


def resolve_instruction(task_content: dict, task_assets: list[dict],
                        data_root: Path) -> tuple[str, Path | None]:
    """The instruction text from its single declared source.

    Returns ``(instruction_text, environment_store_dir_or_None)``. Raises
    :class:`EnvironmentInvalid` when the source is absent, ambiguous,
    tampered with, or empty."""
    instruction = task_content.get("instruction")
    prompt = task_content.get("prompt")
    if instruction is not None and prompt is not None:
        raise EnvironmentInvalid("task version declares both instruction asset and prompt")
    if isinstance(instruction, dict):
        if instruction.get("asset") != INSTRUCTION_ASSET:
            raise EnvironmentInvalid(
                f"instruction asset must be {INSTRUCTION_ASSET!r}: {instruction.get('asset')!r}")
        rel = instruction.get("path")
        if not isinstance(rel, str) or not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise EnvironmentInvalid(f"instruction path invalid: {rel!r}")
        digest = next((a["digest"] for a in task_assets if a.get("name") == INSTRUCTION_ASSET), None)
        if not digest:
            raise EnvironmentInvalid("task version has no environment asset for the instruction")
        env_dir = resolve(data_root, digest)
        file_path = env_dir / rel
        if not file_path.is_file():
            raise EnvironmentInvalid(f"instruction file missing from environment: {rel}")
        text = file_path.read_text()
        if not text.strip():
            raise EnvironmentInvalid(f"instruction file is empty: {rel}")
        return text, env_dir
    if isinstance(prompt, str) and prompt.strip():
        return prompt, None
    raise EnvironmentInvalid("task version has no usable instruction source (neither instruction asset nor prompt)")


def materialize(env_dir: Path, target_dir: Path) -> None:
    """Copy the environment's verified ``workspace/`` subtree into a per-run
    directory that is bind-mounted at /workspace — the registered bytes,
    before any agent call (#20 reopen). A copy, never a mount of the store
    itself: the agent's writes must not touch the shared store."""
    shutil.copytree(env_dir / "workspace", target_dir, dirs_exist_ok=True)
