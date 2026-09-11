"""Content-addressed skill bundles (#39).

A skill is a directory (entry ``SKILL.md``) whose immutable content is
imported by the trusted management side: the registry stores a ``skill``
version (digest + size + file count) and the bytes land under
``<data-root>/skills/<version-id>/``. Trials mount that directory
read-only and the pi adapter loads it via explicit ``--skill``; nothing
is discovered from the host (#39). There is no marketplace, no runtime
download, no dependency resolution — all out of scope.

Import validation fails closed: name/version charset, entry file, no
symlinks or special files (which also closes path escape), a size cap,
and a credential-pattern scan.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

from .app import AppError, register_version
from .models import AssetRef, VersionRegistration

SKILL_ENTRY = "SKILL.md"
SKILL_MAX_BYTES = 2 * 1024 * 1024
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# skills are content, not a channel for credentials (#39 reviewer checklist)
_CREDENTIAL_PATTERNS = (
    ("openai-style key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("github token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("aws access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


class SkillImportError(ValueError):
    """A skill bundle failed import validation."""


_BUNDLE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_skill_registration(reg: VersionRegistration) -> None:
    """Strict shared schema for every SkillVersion registration entry
    (#39 reopen): the generic Registry API must not accept a skill record
    the trusted import path could never produce — a malformed row would
    crash the supervisor at resolve time or mount nothing runnable."""
    def reject(detail: str) -> None:
        raise AppError(422, "invalid_content", f"skill content invalid: {detail}")

    if not NAME_RE.match(reg.name):
        reject(f"name {reg.name!r} must match {NAME_RE.pattern}")
    if not VERSION_RE.match(reg.version):
        reject(f"version {reg.version!r} must match {VERSION_RE.pattern}")
    content = reg.content
    if not isinstance(content, dict) or set(content) != {"schema_version", "entry", "bundle"}:
        reject("content must be exactly schema_version, entry, bundle")
    if content["schema_version"] != 1 or content["entry"] != SKILL_ENTRY:
        reject(f"schema_version must be 1 and entry must be {SKILL_ENTRY}")
    bundle = content["bundle"]
    if not isinstance(bundle, dict) or set(bundle) != {"digest", "bytes", "files"}:
        reject("bundle must be exactly digest, bytes, files")
    if not isinstance(bundle["digest"], str) or not _BUNDLE_DIGEST_RE.match(bundle["digest"]):
        reject("bundle.digest must be 64 lowercase hex chars")
    for size_field in ("bytes", "files"):
        value = bundle[size_field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            reject(f"bundle.{size_field} must be a positive integer")
    assets = reg.assets
    if len(assets) != 1 or assets[0].name != "bundle" or assets[0].digest != bundle["digest"]:
        reject("assets must be exactly one 'bundle' asset whose digest equals bundle.digest")


def tree_digest(bundle: Path) -> dict:
    """Digest every file under bundle: sha256 over sorted (path, file-hash).

    Rejects symlinks (the path-escape vector) and special files."""
    digest = hashlib.sha256()
    total = 0
    count = 0
    for path in sorted(bundle.rglob("*")):
        rel = path.relative_to(bundle).as_posix()
        if path.is_symlink():
            raise SkillImportError(f"symlink not allowed in skill bundle: {rel}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise SkillImportError(f"special file not allowed in skill bundle: {rel}")
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(f"{rel}\0{file_hash}\0".encode())
        total += path.stat().st_size
        count += 1
    return {"digest": digest.hexdigest(), "bytes": total, "files": count}


def scan_credentials(bundle: Path) -> None:
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        text = path.read_bytes().decode("utf-8", errors="ignore")
        for label, pattern in _CREDENTIAL_PATTERNS:
            if pattern.search(text):
                raise SkillImportError(
                    f"credential-like content in skill bundle: "
                    f"{path.relative_to(bundle).as_posix()} matches {label}")


def import_skill(bundle: Path, data_root: Path, name: str, version: str,
                 conn) -> dict:
    """Validate + register a skill bundle; returns the version record.

    Registration is idempotent for identical content (200) and conflicts
    on same name@version with different content (409), reusing the normal
    registry path. Bytes are copied to <data-root>/skills/<version-id>/
    and verified against the registered digest."""
    if not NAME_RE.match(name):
        raise SkillImportError(
            f"invalid skill name {name!r}; must match {NAME_RE.pattern}")
    if not VERSION_RE.match(version):
        raise SkillImportError(
            f"invalid skill version {version!r}; must match {VERSION_RE.pattern}")
    bundle = Path(bundle)
    if not bundle.is_dir():
        raise SkillImportError(f"skill bundle is not a directory: {bundle}")
    entry = bundle / SKILL_ENTRY
    if entry.is_symlink() or not entry.is_file():
        raise SkillImportError(f"skill bundle must contain a real {SKILL_ENTRY}")
    info = tree_digest(bundle)
    if info["bytes"] > SKILL_MAX_BYTES:
        raise SkillImportError(
            f"skill bundle too large: {info['bytes']} > {SKILL_MAX_BYTES} bytes")
    scan_credentials(bundle)

    record, _status = register_version(conn, VersionRegistration(
        kind="skill", name=name, version=version,
        content={"schema_version": 1, "entry": SKILL_ENTRY, "bundle": info},
        assets=[AssetRef(name="bundle", digest=info["digest"])],
    ))

    dest = Path(data_root) / "skills" / record["id"]
    if dest.exists():
        existing = tree_digest(dest)
        if existing["digest"] != info["digest"]:
            raise SkillImportError(
                "existing skill bytes do not match the registered digest")
        return record
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.with_name(f".{dest.name}.staging")  # ponytail: local trusted-side copy; rename-conflict path covers the negligible race
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(bundle, staging, symlinks=False)
    if tree_digest(staging)["digest"] != info["digest"]:
        shutil.rmtree(staging, ignore_errors=True)
        raise SkillImportError("copied skill bytes do not match the registered digest")
    try:
        staging.rename(dest)
    except OSError:
        # concurrent import of the same version: the winner's copy stays
        shutil.rmtree(staging, ignore_errors=True)
        if tree_digest(dest)["digest"] != info["digest"]:
            raise SkillImportError(
                "existing skill bytes do not match the registered digest") from None
    return record
