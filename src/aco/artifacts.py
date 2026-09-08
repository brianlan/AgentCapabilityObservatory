"""Sealed Answers: freeze, publish, register, and recover (#14).

The official answer of a trial is the workspace snapshot ACO freezes itself
through the container pause/copy boundary — never Harbor's post-hoc artifact
collection (diagnostics only). Pipeline per trial, exactly once:

  trigger (submit | exit | timeout) -> pause container (freeze) -> docker cp
  into a private staging dir -> validate -> canonical manifest + digest ->
  same-filesystem atomic rename into answers/<digest>/ (read-only) ->
  transactional registration in SQLite.

Any retry or recovery points at the same published digest and receipt;
recovery never re-collects a restored run's workspace.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .db import utcnow

# V1 artifact contract for the fake target: only /workspace, ordinary files,
# bounded total size. Thresholds are fixed defaults; tuning is configuration's
# job later (issue out-of-scope: concrete freeze-error thresholds).
CONTRACT_ALLOWED_PATHS = ("/workspace",)
CONTRACT_MAX_TOTAL_BYTES = 64 * 1024 * 1024

_MANIFEST = "manifest.json"


class SealError(Exception):
    """Validation or pipeline failure; leaves an explainable state behind."""


@dataclass
class ArtifactContract:
    """What may be collected: allowed container paths, required outputs,
    allowed file types, and a total size ceiling."""

    allowed_paths: tuple[str, ...] = CONTRACT_ALLOWED_PATHS
    required_outputs: tuple[str, ...] = ()
    allowed_types: tuple[str, ...] = ("regular",)
    max_total_bytes: int = CONTRACT_MAX_TOTAL_BYTES

    def validate(self) -> None:
        for path in self.allowed_paths + self.required_outputs:
            if not path.startswith("/") or os.path.normpath(path) != path:
                raise SealError(f"contract path must be absolute and normalized: {path!r}")
        for output in self.required_outputs:
            if not any(output == p or output.startswith(p.rstrip("/") + "/")
                       for p in self.allowed_paths):
                raise SealError(f"required output {output!r} outside allowed paths")


def pause_container(container_id: str) -> None:
    subprocess.run(["docker", "pause", container_id], check=True,
                   capture_output=True, timeout=20)


def unpause_container(container_id: str) -> None:
    subprocess.run(["docker", "unpause", container_id], check=True,
                   capture_output=True, timeout=20)


def copy_from_container(container_id: str, container_path: str, dest: Path) -> None:
    subprocess.run(
        ["docker", "cp", f"{container_id}:{container_path}", str(dest)],
        check=True, capture_output=True, timeout=120,
    )


def collect_workspace(container_id: str, dest: Path, contract: ArtifactContract) -> None:
    """Copy each allowed path out of the (paused) container into dest."""
    dest.mkdir(parents=True)
    for allowed in contract.allowed_paths:
        target = dest / allowed.strip("/").replace("/", "_")
        copy_from_container(container_id, allowed, target)


def snapshot_baseline(container_id: str, baseline_dir: Path) -> bool:
    """Best-effort pre-agent copy of the allowed paths; False when the
    workspace does not exist yet (nothing to diff against)."""
    try:
        collect_workspace(container_id, baseline_dir, ArtifactContract())
        return True
    except Exception:  # noqa: BLE001 — baseline is diagnostic metadata only
        return False


def file_entry(path: Path, root: Path) -> dict:
    data = path.read_bytes()
    binary = b"\x00" in data[:8192]
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "type": "binary" if binary else "regular",
    }


def validate_snapshot(staging: Path, contract: ArtifactContract) -> None:
    """Trust-boundary check on the copied bytes: ordinary files only, no
    escapes, within the declared size ceiling."""
    total = 0
    for path in sorted(staging.rglob("*")):
        if path.is_symlink():
            raise SealError(f"symlink in snapshot is not an ordinary file: {path}")
        if path.is_dir():
            continue
        if not (path.is_file() and not path.is_block_device() and not path.is_char_device()
                and not path.is_socket() and not path.is_fifo()):
            raise SealError(f"special file rejected: {path}")
        resolved = path.resolve()
        if not resolved.is_relative_to(staging.resolve()):
            raise SealError(f"path escape rejected: {path}")
        total += path.stat().st_size
        if total > contract.max_total_bytes:
            raise SealError(f"snapshot exceeds contract limit {contract.max_total_bytes} bytes")
    for required in contract.required_outputs:
        rel = required.strip("/")
        if not (staging / rel).is_file():
            raise SealError(f"required output missing: {required}")


def diff_changes(final: list[dict], baseline: list[dict]) -> dict:
    """Classify additions, modifications, and deletions against the
    pre-agent baseline snapshot."""
    before = {e["path"]: e for e in baseline}
    after = {e["path"]: e for e in final}
    return {
        "added": sorted(p for p in after if p not in before),
        "modified": sorted(p for p in after if p in before and before[p]["sha256"] != after[p]["sha256"]),
        "deleted": sorted(p for p in before if p not in after),
    }


def build_manifest(staging: Path, trial_id: str, run_id: str, trigger: str,
                   baseline: Path | None) -> dict:
    """Canonical manifest of the frozen snapshot; the digest of this JSON is
    the answer digest."""
    entries = [file_entry(p, staging) for p in sorted(staging.rglob("*")) if p.is_file()]
    baseline_entries = []
    if baseline is not None and baseline.is_dir():
        baseline_entries = [file_entry(p, baseline) for p in sorted(baseline.rglob("*")) if p.is_file()]
    return {
        "trial_id": trial_id,
        "run_id": run_id,
        "trigger": trigger,
        "files": entries,
        "changes": diff_changes(entries, baseline_entries),
        "total_bytes": sum(e["bytes"] for e in entries),
    }


def manifest_digest(manifest: dict) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _load_manifest(path: Path) -> dict | None:
    """Parse and schema-check a manifest file; None when unreadable, missing
    required fields, or carrying an unsupported trigger — malformed metadata
    must become an anomaly, never reach registration (#14)."""
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    for key in ("trial_id", "run_id", "trigger"):
        if not isinstance(manifest.get(key), str) or not manifest[key]:
            return None
    if manifest["trigger"] not in ("submit", "exit", "timeout"):
        return None
    if not isinstance(manifest.get("files"), list):
        return None
    for entry in manifest["files"]:
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("path"), str) or not entry["path"]
                or not isinstance(entry.get("bytes"), int) or isinstance(entry["bytes"], bool)
                or not isinstance(entry.get("sha256"), str)
                or entry.get("type") not in ("regular", "binary")):
            return None
    if not isinstance(manifest.get("changes"), dict):
        return None
    if not isinstance(manifest.get("total_bytes"), int) or isinstance(manifest["total_bytes"], bool):
        return None
    return manifest


def _manifest_mismatch(manifest: dict, content_root: Path,
                       trial_id: str, run_id: str) -> str | None:
    """Cross-check a manifest against the complete on-disk tree it claims to
    describe.

    Returns an anomaly detail when disk facts contradict the manifest —
    wrong trial/run metadata, non-relative or escaping entry paths, missing
    or symlinked entries, changed size or content, or extra unmanifested
    files — None when the tree is exactly the manifest's content (#14).
    """
    if manifest.get("trial_id") != trial_id or manifest.get("run_id") != run_id:
        return "manifest trial/run metadata does not match the recovered location"
    expected: dict[str, dict] = {}
    for entry in manifest.get("files", []):
        rel = PurePosixPath(entry["path"])
        if rel.is_absolute() or ".." in rel.parts:
            return f"manifest entry path escapes the answer directory: {entry['path']!r}"
        path = content_root / rel
        if path.is_symlink() or not path.is_file():
            return f"manifest entry missing or not a regular file: {entry['path']}"
        expected[rel.as_posix()] = entry
    for p in content_root.rglob("*"):
        rel = p.relative_to(content_root).as_posix()
        if rel == _MANIFEST:
            continue
        if p.is_symlink():
            return f"symlink inside the recovered content: {rel}"
        if p.is_file() and rel not in expected:
            return f"unmanifested content in the answer: {rel}"
    for rel, entry in expected.items():
        data = (content_root / rel).read_bytes()
        if (len(data) != entry.get("bytes")
                or hashlib.sha256(data).hexdigest() != entry.get("sha256")):
            return f"manifest entry content mismatch: {rel}"
    return None


def write_diagnostic_patch(baseline: Path | None, staging: Path, dest: Path) -> None:
    """Best-effort git patch of workspace changes, stored OUTSIDE the sealed
    answer (diagnostics only, never part of the official content)."""
    if baseline is None or not baseline.is_dir() or shutil.which("git") is None:
        return
    try:
        result = subprocess.run(
            ["git", "diff", "--no-index", "--binary", str(baseline), str(staging)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.stdout:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(result.stdout)
    except Exception:  # noqa: BLE001 — optional diagnostic, never blocks sealing
        pass


def publish(staging: Path, digest: str, answers_root: Path) -> Path:
    """Atomically rename staging into the content-addressed read-only
    location. Same filesystem by construction (both under the data root)."""
    answers_root.mkdir(parents=True, exist_ok=True)
    target = answers_root / digest
    if target.exists():
        shutil.rmtree(staging)  # already published (retry/recovery): keep original
        return target
    try:
        os.replace(staging, target)  # rename before chmod: a read-only source dir cannot be moved across parents
    except OSError:
        if target.exists():  # concurrent publisher won the race
            shutil.rmtree(staging)
            return target
        raise
    for path in (target, *target.rglob("*")):
        os.chmod(path, 0o444 if path.is_file() else 0o555)
    return target


def set_submission_status(conn: sqlite3.Connection, trial_id: str, status: str) -> None:
    conn.execute("UPDATE submissions SET status = ? WHERE trial_id = ?", (status, trial_id))
    conn.commit()


def register(conn: sqlite3.Connection, trial_id: str, run_id: str, receipt_id: str,
             manifest: dict, trigger: str, times: dict) -> str:
    """Transactional registration; repeats return the existing digest."""
    digest = manifest_digest(manifest)
    existing = conn.execute(
        "SELECT digest FROM sealed_answers WHERE trial_id = ?", (trial_id,)
    ).fetchone()
    if existing is not None:
        return existing["digest"]
    conn.execute(
        "INSERT INTO sealed_answers (trial_id, run_id, receipt_id, digest, manifest,"
        " seal_trigger, trigger_at, frozen_at, copied_at, published_at, registered_at, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sealed')",
        (trial_id, run_id, receipt_id, digest, json.dumps(manifest, sort_keys=True),
         trigger, times["trigger_at"], times["frozen_at"], times["copied_at"],
         times["published_at"], utcnow()),
    )
    conn.commit()
    return digest


def seal(conn: sqlite3.Connection, run: sqlite3.Row, container_id: str, trigger: str,
         root: Path, baseline_dir: Path | None = None) -> dict:
    """The single seal entry point for submit, exit, and timeout triggers.

    Freezes the workspace (pause -> copy -> unpause), publishes it
    content-addressed, and registers it. Idempotent: a second call for the
    same trial returns the registered digest without re-collecting. Any
    failure marks the submission 'error' and leaves an explainable state.
    """
    trial_id = run["trial_id"]
    run_id = run["run_id"]
    existing = conn.execute(
        "SELECT digest, receipt_id FROM sealed_answers WHERE trial_id = ?", (trial_id,)
    ).fetchone()
    if existing is not None:
        return {"receipt_id": existing["receipt_id"], "digest": existing["digest"], "status": "sealed"}

    try:
        submission = conn.execute(
            "SELECT receipt_id FROM submissions WHERE trial_id = ?", (trial_id,)
        ).fetchone()
        receipt_id = submission["receipt_id"] if submission else uuid.uuid4().hex

        times = {"trigger_at": utcnow()}
        contract = ArtifactContract()
        contract.validate()
        set_submission_status(conn, trial_id, "sealing")
        staging = root / "sealing" / run_id / "staging"
        try:
            pause_container(container_id)
            times["frozen_at"] = utcnow()
            collect_workspace(container_id, staging, contract)
            times["copied_at"] = utcnow()
        except Exception as exc:
            raise SealError(f"freeze failed: {exc}") from exc
        finally:
            try:
                unpause_container(container_id)  # Harbor teardown resumes; diagnostics may differ
            except Exception:  # noqa: BLE001 — container may already be gone
                pass

        validate_snapshot(staging, contract)
        baseline = baseline_dir if baseline_dir is not None and baseline_dir.is_dir() else None
        manifest = build_manifest(staging, trial_id, run_id, trigger, baseline)
        write_diagnostic_patch(baseline, staging, root / "runs" / run_id / "diagnostics.patch")
        digest = manifest_digest(manifest)
        (staging / _MANIFEST).write_text(json.dumps(manifest, sort_keys=True))
        published = publish(staging, digest, root / "answers")
        times["published_at"] = utcnow()
        digest = register(conn, trial_id, run_id, receipt_id, manifest, trigger, times)
        set_submission_status(conn, trial_id, "sealed")
        return {"receipt_id": receipt_id, "digest": digest, "status": "sealed",
                "path": str(published)}
    except Exception:
        set_submission_status(conn, trial_id, "error")
        raise


def recover(conn: sqlite3.Connection, root: Path) -> None:
    """Manager-startup recovery of known intermediate states, from disk truth
    and existing manifests only — never re-collects a workspace.

    - staging with a completed manifest: finish publish + registration
    - staging without a manifest: incomplete copy, freeze unprovable -> anomaly
    - published but unregistered: register from the on-disk manifest
    - registered but published content missing: mark anomaly
    """
    sealing_root = root / "sealing"
    answers_root = root / "answers"
    if sealing_root.is_dir():
        for run_dir in sorted(sealing_root.iterdir()):
            if not run_dir.is_dir():
                continue
            run_id = run_dir.name
            run = conn.execute(
                "SELECT trial_id FROM trial_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:  # unknown run: orphan staging, remove
                shutil.rmtree(run_dir, ignore_errors=True)
                continue
            trial_id = run["trial_id"]
            if conn.execute("SELECT 1 FROM sealed_answers WHERE trial_id = ?", (trial_id,)).fetchone():
                shutil.rmtree(run_dir, ignore_errors=True)  # already sealed elsewhere
                continue
            staging = run_dir / "staging"
            if not staging.is_dir():
                # nothing left to seal (published elsewhere or never copied):
                # the published-but-unregistered pass below handles real content
                shutil.rmtree(run_dir, ignore_errors=True)
                continue
            manifest_path = staging / _MANIFEST
            if manifest_path.is_file():
                manifest = _load_manifest(manifest_path)
                if manifest is None:
                    shutil.rmtree(run_dir, ignore_errors=True)
                    mark_anomaly(conn, trial_id, run_id,
                                 "staging manifest is unreadable or malformed")
                    continue
                mismatch = _manifest_mismatch(manifest, staging, trial_id, run_id)
                if mismatch:
                    shutil.rmtree(run_dir, ignore_errors=True)
                    mark_anomaly(conn, trial_id, run_id, mismatch)
                    continue
                digest = manifest_digest(manifest)
                submission = conn.execute(
                    "SELECT receipt_id FROM submissions WHERE trial_id = ?", (trial_id,)
                ).fetchone()
                receipt_id = submission["receipt_id"] if submission else uuid.uuid4().hex
                times = {key: manifest.get(key) or utcnow()
                         for key in ("trigger_at", "frozen_at", "copied_at")}
                times["published_at"] = utcnow()
                publish(staging, digest, answers_root)
                register(conn, trial_id, run_id, receipt_id, manifest,
                         manifest.get("trigger", "exit"), times)
                set_submission_status(conn, trial_id, "sealed")
                shutil.rmtree(run_dir, ignore_errors=True)
            else:
                # copy never completed: cannot prove a timely freeze
                shutil.rmtree(run_dir, ignore_errors=True)
                mark_anomaly(conn, trial_id, run_id,
                              "incomplete staging copy: freeze could not be proven")

    if answers_root.is_dir():
        for answer_dir in sorted(answers_root.iterdir()):
            manifest_path = answer_dir / _MANIFEST
            if not answer_dir.is_dir() or not manifest_path.is_file():
                continue
            manifest = _load_manifest(manifest_path)
            if manifest is None:
                continue  # nothing trustworthy to attribute or register
            trial_id = manifest.get("trial_id")
            if not trial_id or conn.execute(
                "SELECT 1 FROM sealed_answers WHERE trial_id = ?", (trial_id,)
            ).fetchone():
                continue
            run = conn.execute(
                "SELECT run_id FROM trial_runs WHERE trial_id = ?", (trial_id,)
            ).fetchone()
            if run is None:
                continue
            mismatch = _manifest_mismatch(manifest, answer_dir, trial_id, run["run_id"])
            if mismatch or manifest_digest(manifest) != answer_dir.name:
                # disk facts contradict the manifest: register nothing
                detail = ("published content fails recovery validation: "
                          + (mismatch or "directory name does not match the manifest digest"))
                mark_anomaly(conn, trial_id, run["run_id"], detail)
                continue
            submission = conn.execute(
                "SELECT receipt_id FROM submissions WHERE trial_id = ?", (trial_id,)
            ).fetchone()
            receipt_id = submission["receipt_id"] if submission else uuid.uuid4().hex
            times = {key: manifest.get(key) or utcnow()
                     for key in ("trigger_at", "frozen_at", "copied_at", "published_at")}
            register(conn, trial_id, run["run_id"], receipt_id, manifest,
                     manifest.get("trigger", "exit"), times)
            set_submission_status(conn, trial_id, "sealed")

    for row in conn.execute(
        "SELECT trial_id, run_id, digest FROM sealed_answers WHERE status = 'sealed'"
    ).fetchall():
        answer_dir = root / "answers" / row["digest"]
        if not answer_dir.is_dir():
            detail = "registered answer content missing from disk"
        else:
            manifest = _load_manifest(answer_dir / _MANIFEST)
            mismatch = (_manifest_mismatch(manifest, answer_dir, row["trial_id"], row["run_id"])
                        if manifest is not None else None)
            if mismatch:
                detail = f"registered answer fails recovery validation: {mismatch}"
            elif manifest is None or manifest_digest(manifest) != row["digest"]:
                detail = "registered answer manifest does not match the registered digest"
            else:
                detail = None
        if detail:
            conn.execute(
                "UPDATE sealed_answers SET status = 'anomaly', anomaly = ? WHERE trial_id = ?",
                (detail, row["trial_id"]),
            )
            set_submission_status(conn, row["trial_id"], "error")
    conn.commit()


def mark_anomaly(conn: sqlite3.Connection, trial_id: str, run_id: str,
                 detail: str, trigger: str = "exit") -> None:
    """Record an execution-condition anomaly for a trial that never sealed.

    The receipt stays stable: reuse the submission's receipt when one exists,
    generate one only for exits with no submission (#14).
    """
    if conn.execute("SELECT 1 FROM sealed_answers WHERE trial_id = ?", (trial_id,)).fetchone():
        return  # already sealed or anomaly recorded: the receipt must not change
    submission = conn.execute(
        "SELECT receipt_id FROM submissions WHERE trial_id = ?", (trial_id,)
    ).fetchone()
    receipt_id = submission["receipt_id"] if submission else uuid.uuid4().hex
    conn.execute(
        "INSERT INTO sealed_answers (trial_id, run_id, receipt_id, digest, manifest,"
        " seal_trigger, trigger_at, frozen_at, copied_at, published_at, registered_at,"
        " status, anomaly) VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, 'anomaly', ?)",
        (trial_id, run_id, receipt_id, "{}", trigger, utcnow(), utcnow(), utcnow(), utcnow(), utcnow(), detail),
    )
    set_submission_status(conn, trial_id, "error")


def get_receipt(conn: sqlite3.Connection, trial_id: str) -> dict | None:
    """The stable, repeatedly queryable publish receipt."""
    row = conn.execute(
        "SELECT receipt_id, digest, status, anomaly, seal_trigger,"
        " trigger_at, registered_at FROM sealed_answers WHERE trial_id = ?", (trial_id,)
    ).fetchone()
    if row is None:
        return None
    return dict(row)
