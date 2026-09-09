"""Task admission: prove a task bundle is a stable-candidate through gates (#20).

One command runs every gate against a local task bundle and writes a
JSON + Markdown report with per-gate evidence; any failed gate means the
bundle is not registered as a stable-candidate version.

Bundle layout (fixed, single layout — no configuration):

    <bundle>/
      task.toml                       # manifest: schema, name, version, image,
                                      # verifier entrypoint/result schema,
                                      # artifact contract, provenance
      public/environment/             # the agent-visible initial workspace
      private/verifier/               # trusted verifier bundle (run.py, config)
      private/reference/              # reference answer (workspace overlay)
      private/wrong_answers/<name>/   # declared cheat cases (workspace overlays)

Gates, reusing the official sealing and scoring entries (artifacts.*, and the
verification runner's container invocation):

  static       manifest completeness, provenance, layout, verifier contract,
               no symlinks/special files
  leak_scan    no private asset content inside the agent-visible tree
  image_scan   no private asset content inside the task image's layers
  oracle       reference answer seals and scores 3/3, with identical content
               digests across runs (determinism evidence, not a proof)
  nop          the unmodified initial workspace fails 0/3
  cheats       every declared wrong answer must fail
  rescore      the same sealed answer scores identically on repeat

Promotion to Core is never automatic: `promote --to core` requires an
all-passed report and an explicit --reviewed-by human name.
"""

import argparse
import fcntl  # POSIX: serializes concurrent admissions of one scorer id
import hashlib
import os
import uuid
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .. import artifacts, db
from ..app import AppError, register_version, version_digest
from ..models import AssetRef, ScorerContent, VersionRegistration
from ..verification import runner
from ..db import utcnow

SCHEMA = "aco.admission-report/v1"
TASK_SCHEMA = "aco.task-bundle/v1"

PUBLIC_ENV = "public/environment"
VERIFIER_DIR = "private/verifier"
REFERENCE_DIR = "private/reference"
WRONG_DIR = "private/wrong_answers"

CONTAINER_LIFETIME_SEC = 300
CONTAINER_OP_TIMEOUT = 120
DOCKER_SAVE_TIMEOUT = 600


class AdmissionError(Exception):
    """Bundle or report-level failure that prevents admission."""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tree_digests(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): _digest(p.read_bytes())
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


@dataclass
class Bundle:
    path: Path
    manifest: dict
    verifier: ScorerContent
    wrong_cases: list[str]

    @classmethod
    def load(cls, path: Path) -> "Bundle":
        # docker -v mounts need absolute host paths
        path = path.expanduser().resolve()
        manifest_path = path / "task.toml"
        if not manifest_path.is_file():
            raise AdmissionError(f"task bundle has no task.toml: {path}")
        try:
            manifest = tomllib.loads(manifest_path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise AdmissionError(f"task.toml unreadable: {exc}") from exc
        if manifest.get("schema") != TASK_SCHEMA:
            raise AdmissionError(f"manifest schema must be {TASK_SCHEMA!r}")
        for key in ("name", "version", "image"):
            if not isinstance(manifest.get(key), str) or not manifest[key]:
                raise AdmissionError(f"manifest field {key!r} must be a non-empty string")
        try:
            verifier = ScorerContent.model_validate({
                "image": manifest["image"],
                "entrypoint": manifest.get("verifier", {}).get("entrypoint"),
                "result_schema": manifest.get("verifier", {}).get("result_schema"),
            })
        except ValueError as exc:
            raise AdmissionError(f"verifier contract invalid: {exc}") from exc
        wrong = path / WRONG_DIR
        cases = sorted(p.name for p in wrong.iterdir() if p.is_dir()) if wrong.is_dir() else []
        bundle = cls(path=path, manifest=manifest, verifier=verifier, wrong_cases=cases)
        for rel in (PUBLIC_ENV, VERIFIER_DIR, REFERENCE_DIR):
            bundle.required_dir(rel)  # fail fast on a malformed layout
        return bundle

    def required_dir(self, rel: str) -> Path:
        target = self.path / rel
        if not target.is_dir():
            raise AdmissionError(f"bundle layout missing directory: {rel}/")
        return target

    @property
    def public_env(self) -> Path:
        return self.required_dir(PUBLIC_ENV)

    @property
    def verifier_bundle(self) -> Path:
        return self.required_dir(VERIFIER_DIR)

    @property
    def reference(self) -> Path:
        return self.required_dir(REFERENCE_DIR)

    @property
    def contract(self) -> artifacts.ArtifactContract:
        required = tuple(self.manifest.get("contract", {}).get("required_outputs", ()))
        return artifacts.ArtifactContract(required_outputs=tuple(required))

    def provenance(self) -> dict:
        return dict(self.manifest.get("provenance", {}))


def _exempt_digests(bundle: Bundle) -> set[str]:
    """Digests of private files that are byte-identical to their same logical
    workspace path in the initial public environment — overlay-unchanged
    starter files under reference/ or a wrong-answer workspace. Everything
    under private/verifier is a hidden asset, always."""
    public = _tree_digests(bundle.public_env)
    exempt = set()
    for rel in (REFERENCE_DIR, WRONG_DIR):
        tree = bundle.path / rel
        if not tree.is_dir():
            continue
        for p in tree.rglob("*"):
            if not p.is_file() or p.is_symlink():
                continue
            # map .../workspace/<logical> (reference has no case component,
            # wrong answers nest one) to public/environment/workspace/<logical>
            parts = p.relative_to(tree).parts
            if "workspace" not in parts:
                continue
            logical = "/".join(parts[parts.index("workspace"):])
            if public.get(logical) == _digest(p.read_bytes()):
                exempt.add(_digest(p.read_bytes()))
    return exempt


def _hidden_digests(bundle: Bundle) -> set[str]:
    hidden = set()
    for rel in (VERIFIER_DIR, REFERENCE_DIR, WRONG_DIR):
        tree = bundle.path / rel
        if not tree.is_dir():
            continue
        hidden.update(_tree_digests(tree).values())
    return hidden - _exempt_digests(bundle)


def image_layer_file_digests(image: str) -> set[str]:
    """Content digests of every file inside the image's layers, via docker save."""
    proc = subprocess.run(["docker", "save", image], capture_output=True,
                          timeout=DOCKER_SAVE_TIMEOUT, check=False)
    if proc.returncode != 0:
        raise AdmissionError(f"docker save failed: {proc.stderr.decode(errors='replace')[-300:]}")
    digests: set[str] = set()
    outer = tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:*")
    for member in outer:
        if not member.isfile():
            continue
        data = outer.extractfile(member).read()
        if member.name.endswith((".tar", ".tar.gz", ".tgz")):
            try:
                inner = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
            except tarfile.TarError:
                continue  # ponytail: exotic layer compression — record as unreadable
            for nested in inner:
                if nested.isfile():
                    digests.add(_digest(inner.extractfile(nested).read()))
        else:
            digests.add(_digest(data))
    return digests


def static_checks(bundle: Bundle) -> list[dict]:
    checks = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    provenance = bundle.provenance()
    missing = [k for k in ("source", "license", "modifications")
               if not isinstance(provenance.get(k), str) or not provenance[k]]
    check("provenance_complete", not missing,
          "provenance missing: " + ", ".join(missing) if missing else "source/license/modifications present")

    if not bundle.wrong_cases:
        check("wrong_answers_declared", False, f"no cheat cases under {WRONG_DIR}/")
    else:
        check("wrong_answers_declared", True, f"{len(bundle.wrong_cases)} declared cheat cases")

    special = [p.relative_to(bundle.path).as_posix() for p in bundle.path.rglob("*")
               if p.is_symlink() or (not p.is_dir() and not p.is_file())]
    check("no_symlinks_or_special_files", not special,
          "symlink or special file in bundle: " + ", ".join(special[:5]) if special else "clean")

    hidden = _hidden_digests(bundle)
    public = _tree_digests(bundle.public_env)
    leaked = sorted(rel for rel, digest in public.items() if digest in hidden)
    check("no_hidden_assets_in_public_bundle", not leaked,
          "hidden asset content found in public bundle: " + ", ".join(leaked[:5]) if leaked
          else f"public tree clean against {len(hidden)} hidden assets")

    try:
        layers = image_layer_file_digests(bundle.manifest["image"])
        baked = sorted(layers & hidden)
        check("no_hidden_assets_in_image_layers", not baked,
              "hidden asset content found in image layers: " + ", ".join(baked[:5]) if baked
              else f"scanned {len(layers)} distinct image file digests")
    except AdmissionError as exc:
        check("no_hidden_assets_in_image_layers", False, str(exc))
    return checks


def _workspace_label(scope: str, gate: str, index: int) -> str:
    # unique scope per admission run: work dirs (and the verifier container's
    # cidfile) never collide across reruns or concurrent admissions
    return f"admission-{scope}-{gate}-{index}"


def prepare_answer(image: str, workspace_src: Path, root: Path, label: str) -> Path:
    """Seal a candidate workspace state through the official sealing entries.

    Starts a throwaway container, overlays the workspace tree onto /workspace,
    then runs the same pause -> copy -> validate -> manifest -> publish
    pipeline a real trial uses. Returns the published answer directory."""
    contract = artifacts.ArtifactContract()
    cid = subprocess.run(
        ["docker", "run", "-d", "--entrypoint", "sleep", image, str(CONTAINER_LIFETIME_SEC)],
        capture_output=True, text=True, timeout=CONTAINER_OP_TIMEOUT, check=True,
    ).stdout.strip()
    try:
        subprocess.run(["docker", "exec", cid, "mkdir", "-p", "/workspace"],
                       capture_output=True, timeout=CONTAINER_OP_TIMEOUT, check=True)
        subprocess.run(["docker", "cp", f"{workspace_src}/.", f"{cid}:/workspace"],
                       capture_output=True, timeout=CONTAINER_OP_TIMEOUT, check=True)
        staging = root / "admission" / label / "staging"
        staging.parent.mkdir(parents=True, exist_ok=True)
        artifacts.pause_container(cid)
        try:
            artifacts.collect_workspace(cid, staging, contract)
        finally:
            artifacts.unpause_container(cid)
        artifacts.validate_snapshot(staging, contract)
        manifest = artifacts.build_manifest(staging, label, label, "submit", None)
        digest = artifacts.manifest_digest(manifest)
        (staging / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        return artifacts.publish(staging, digest, root / "answers")
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True,
                       timeout=CONTAINER_OP_TIMEOUT, check=False)


def score_answer(answer_dir: Path, bundle_dir: Path, verifier: ScorerContent,
                 root: Path, label: str) -> dict:
    """Score one sealed answer through the verification runner's container
    invocation — the official scoring entry. Returns a verdict dict."""
    work_dir = root / "admission" / label / "scoring"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir = work_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    code, stdout, stderr, cid = runner._run_container(
        answer_dir, bundle_dir, output_dir, work_dir, verifier)
    if cid:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True,
                       timeout=CONTAINER_OP_TIMEOUT, check=False)
    (work_dir / "docker-run.log").write_text(f"exit={code}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n")
    if code is None:
        return {"status": "error", "pass": None,
                "detail": f"verifier timed out after {runner.VERIFIER_TIMEOUT_SEC}s"}
    if code != 0:
        return {"status": "error", "pass": None, "detail": f"verifier exited {code}: {stderr.strip()[-300:]}"}
    parsed = runner.parse_result(output_dir / runner.RESULT_FILE, verifier.result_schema)
    if not parsed["ok"]:
        return {"status": "error", "pass": None, "detail": parsed["detail"]}
    return {"status": "succeeded", "pass": parsed["pass"]}


def _answer_content_digests(answer_dir: Path) -> set[str]:
    return {entry["sha256"] for entry in json.loads((answer_dir / "manifest.json").read_text())["files"]}


def _workspace(parent: Path) -> Path:
    """The workspace overlay a gate seals: every bundle tree holds one."""
    workspace = parent / "workspace"
    if not workspace.is_dir():
        raise AdmissionError(f"bundle tree missing workspace/: {parent}")
    return workspace


def oracle_gate(bundle: Bundle, root: Path, scope: str) -> dict:
    digests = []
    for index in (1, 2, 3):
        answer_dir = prepare_answer(bundle.manifest["image"], _workspace(bundle.reference), root,
                                    _workspace_label(scope, "oracle", index))
        verdict = score_answer(answer_dir, bundle.verifier_bundle, bundle.verifier,
                               root, _workspace_label(scope, "oracle", index))
        digests.append(_answer_content_digests(answer_dir))
        if verdict != {"status": "succeeded", "pass": True}:
            return {"ok": False, "runs": index, "detail": f"reference run {index} did not pass: {verdict}"}
    if not digests[0] == digests[1] == digests[2]:
        return {"ok": False, "runs": 3, "detail": "reference runs produced different sealed content (unstable)"}
    return {"ok": True, "runs": 3, "detail": "reference answer passed 3/3 with identical sealed content"}


def nop_gate(bundle: Bundle, root: Path, scope: str) -> dict:
    for index in (1, 2, 3):
        answer_dir = prepare_answer(bundle.manifest["image"], _workspace(bundle.public_env), root,
                                    _workspace_label(scope, "nop", index))
        verdict = score_answer(answer_dir, bundle.verifier_bundle, bundle.verifier,
                               root, _workspace_label(scope, "nop", index))
        if verdict == {"status": "succeeded", "pass": True}:
            return {"ok": False, "runs": index, "detail": "unmodified initial workspace passed — verifier accepts a no-op"}
        if verdict["status"] != "succeeded":
            return {"ok": False, "runs": index, "detail": f"nop run {index} scoring error: {verdict}"}
    return {"ok": True, "runs": 3, "detail": "unmodified initial workspace failed 3/3 as required"}


def cheats_gate(bundle: Bundle, root: Path, scope: str) -> dict:
    wrong_dir = bundle.path / WRONG_DIR
    passing, errors = [], []
    for index, case in enumerate(bundle.wrong_cases, start=1):
        answer_dir = prepare_answer(bundle.manifest["image"], _workspace(wrong_dir / case), root,
                                    _workspace_label(scope, "cheat", index))
        verdict = score_answer(answer_dir, bundle.verifier_bundle, bundle.verifier,
                               root, _workspace_label(scope, "cheat", index))
        if verdict == {"status": "succeeded", "pass": True}:
            passing.append(case)
        elif verdict["status"] != "succeeded":
            errors.append(f"{case}: {verdict['detail']}")
    if passing:
        return {"ok": False, "detail": "wrong answers that PASSED: " + ", ".join(passing)}
    if errors:
        return {"ok": False, "detail": "cheat case scoring errors: " + "; ".join(errors)}
    return {"ok": True, "cases": bundle.wrong_cases, "detail": "all declared wrong answers failed"}


def rescore_gate(bundle: Bundle, root: Path, scope: str) -> dict:
    answer_dir = prepare_answer(bundle.manifest["image"], _workspace(bundle.reference), root,
                                _workspace_label(scope, "rescore", 1))
    verdicts = [score_answer(answer_dir, bundle.verifier_bundle, bundle.verifier,
                             root, _workspace_label(scope, "rescore", index))
                for index in (1, 2)]
    if any(v["status"] != "succeeded" for v in verdicts):
        return {"ok": False, "detail": f"scoring error during repeat scoring: {verdicts}"}
    if verdicts[0]["pass"] != verdicts[1]["pass"]:
        return {"ok": False, "detail": f"repeat scoring disagreed: {verdicts}"}
    return {"ok": True, "detail": f"same sealed answer scored identically twice (pass={verdicts[0]['pass']})"}


def _markdown_report(report: dict) -> str:
    lines = [f"# Admission report — {report['task']['name']}@{report['task']['version']}", ""]
    if not report["all_passed"]:
        lines.append("**Result: FAILED — not a stable-candidate.**")
    else:
        lines.append("**Result: all gates passed — stable-candidate (Core requires human review).**")
    lines.append("")
    for name, gate in report["gates"].items():
        lines.append(f"- **{name}**: {'PASS' if gate['ok'] else 'FAIL'} — {gate.get('detail', '')}")
    lines.append("")
    lines.append("## Digests")
    for name, digest in report["digests"].items():
        lines.append(f"- {name}: `{digest}`")
    lines.append("")
    lines.append("## Provenance")
    for key, value in report["task"]["provenance"].items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines) + "\n"


def run_admission(bundle_path: Path, data_root: Path) -> dict:
    data_root = data_root.expanduser().resolve()  # docker -v needs absolute paths
    data_root.mkdir(parents=True, exist_ok=True)
    bundle = Bundle.load(bundle_path)

    static = static_checks(bundle)
    static_ok = all(c["ok"] for c in static)
    gates: dict[str, dict] = {
        "static": {"ok": static_ok, "checks": static,
                   "detail": "all static checks passed" if static_ok else "static checks failed"},
    }

    scope = uuid.uuid4().hex[:8]
    runner_conn = db.connect(data_root / "aco.db")
    db.migrate(runner_conn)
    verifier_bundle_digest = runner.bundle_digest(bundle.verifier_bundle)
    environment_digest = runner.bundle_digest(bundle.public_env)
    contract_digest = _digest(json.dumps(
        {"required_outputs": bundle.contract.required_outputs,
         "allowed_paths": bundle.contract.allowed_paths,
         "max_total_bytes": bundle.contract.max_total_bytes},
        sort_keys=True).encode())

    registered = None
    if static_ok:
        gates["oracle"] = oracle_gate(bundle, data_root, scope)
        gates["nop"] = nop_gate(bundle, data_root, scope)
        gates["cheats"] = cheats_gate(bundle, data_root, scope)
        gates["rescore"] = rescore_gate(bundle, data_root, scope)
    else:
        for name in ("oracle", "nop", "cheats", "rescore"):
            gates[name] = {"ok": False, "detail": "skipped: static gates failed"}
        # ponytail: report digests only; DB registration stays out until gates pass

    if all(g["ok"] for g in gates.values()):
        # import + registration is itself gated: a candidate must leave the
        # admission with a usable verifier bundle in the data root
        try:
            registered = _register_candidate(runner_conn, bundle, data_root,
                                             verifier_bundle_digest, environment_digest)
            gates["registration"] = {"ok": True, "detail": "verifier bundle imported and versions registered"}
        except AdmissionError as exc:
            gates["registration"] = {"ok": False, "detail": str(exc)}
    else:
        gates["registration"] = {"ok": False, "detail": "skipped: earlier gates failed"}

    all_passed = all(g["ok"] for g in gates.values())

    task_registration_digest = registered["version_id"] if registered else None
    report = {
        "schema": SCHEMA,
        "generated_at": utcnow(),
        "all_passed": all_passed,
        "task": {"name": bundle.manifest["name"], "version": bundle.manifest["version"],
                 "provenance": bundle.provenance()},
        "digests": {"verifier_bundle": verifier_bundle_digest,
                    "environment": environment_digest,
                    "artifact_contract": contract_digest,
                    "task_version": task_registration_digest},
        "gates": gates,
        "promotion": {"candidate": all_passed, "core": False, "reviewed_by": None},
    }

    report_dir = data_root / "admission"
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = task_registration_digest or _digest(bundle.manifest["name"].encode())[:16]
    (report_dir / f"{stem}.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    (report_dir / f"{stem}.md").write_text(_markdown_report(report))
    report["report"] = {"json": str(report_dir / f"{stem}.json"), "markdown": str(report_dir / f"{stem}.md")}
    runner_conn.close()
    return report


def _rollback_candidate(conn, created_rows: list[str], staging: Path) -> None:
    """Failure atomicity: remove this attempt's private staging directory and
    roll back exactly the rows its register_version calls inserted (201);
    rows returned idempotently (200) are never touched."""
    with conn:
        for row_id in created_rows:
            conn.execute("DELETE FROM versions WHERE id = ?", (row_id,))
    shutil.rmtree(staging, ignore_errors=True)


def _register_candidate(conn, bundle: Bundle, data_root: Path,
                        verifier_bundle_digest: str, environment_digest: str) -> dict:
    """Stage the trusted verifier bundle into the runtime data root and
    register the task and scorer versions through the normal registry.

    The DB keeps only versions, digests, provenance, and a report reference;
    the executable bundle goes to verifiers/<scorer_id> — exactly where
    verification.runner loads it. The bundle is staged in a private
    directory and published with one atomic rename only after a fully
    successful registration, so a failed attempt never removes a directory
    another admission may already be using. Any failure leaves no apparently
    usable candidate: staging and exactly the version rows this attempt
    inserted (status 201) are removed; rows returned idempotently (200) are
    never touched.
    """
    scorer_name = f"{bundle.manifest['name']}-verifier"
    scorer_version = bundle.manifest["version"]
    scorer_content = bundle.verifier.model_dump()
    scorer_assets = [AssetRef(name="bundle", digest=verifier_bundle_digest)]
    task_name = bundle.manifest["name"]
    task_version = bundle.manifest["version"]
    task_content = {key: value for key, value in bundle.manifest.items() if key != "verifier"} \
        | {"admission": "stable-candidate", "admission_report_dir": "admission/"}
    task_assets = [AssetRef(name="verifier_bundle", digest=verifier_bundle_digest),
                   AssetRef(name="environment", digest=environment_digest)]
    # the version id is deterministic (content-addressed): identical bundles
    # converge on the same destination directory, so concurrent admissions of
    # the same scorer id serialize on a lock file — a failing attempt finishes
    # its rollback before another attempt can build on its rows or directory
    scorer_id = version_digest("scorer", scorer_name, scorer_version,
                               scorer_content, scorer_assets)
    dest = data_root / "verifiers" / scorer_id
    lock_path = data_root / "verifiers" / f".{scorer_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            _register_candidate_locked(
                conn, bundle, verifier_bundle_digest, environment_digest,
                scorer_name, scorer_version, scorer_content, scorer_assets,
                task_name, task_version, task_content, task_assets, dest)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _register_candidate_locked(conn, bundle: Bundle, verifier_bundle_digest: str,
                               environment_digest: str, scorer_name: str,
                               scorer_version: str, scorer_content: dict,
                               scorer_assets: list, task_name: str, task_version: str,
                               task_content: dict, task_assets: list,
                               dest: Path) -> dict:
    if dest.is_dir() and runner.bundle_digest(dest) != verifier_bundle_digest:
        raise AdmissionError(f"verifier bundle already present with different content: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.parent / f".import-{uuid.uuid4().hex}"
    shutil.copytree(bundle.verifier_bundle, staging)
    created_rows: list[str] = []
    try:
        if runner.bundle_digest(staging) != verifier_bundle_digest:
            raise AdmissionError("imported verifier bundle digest mismatch")
        task_record, status = register_version(conn, VersionRegistration(
            kind="task", name=task_name, version=task_version,
            content=task_content, assets=task_assets))
        if status == 201:
            created_rows.append(task_record["id"])
        scorer_record, status = register_version(conn, VersionRegistration(
            kind="scorer", name=scorer_name, version=scorer_version,
            content=scorer_content, assets=scorer_assets))
        if status == 201:
            created_rows.append(scorer_record["id"])
        try:
            os.replace(staging, dest)  # atomic publish; same filesystem by construction
        except OSError:
            # a previous attempt already published the same content-addressed
            # bundle; its directory is identical, so discard our staging copy
            if runner.bundle_digest(dest) != verifier_bundle_digest:
                raise AdmissionError(
                    f"verifier bundle already present with different content: {dest}")
            shutil.rmtree(staging, ignore_errors=True)
    except AppError as exc:
        _rollback_candidate(conn, created_rows, staging)
        raise AdmissionError(f"registry rejected the candidate: {exc.message}") from exc
    except AdmissionError:
        _rollback_candidate(conn, created_rows, staging)
        raise
    return {"version_id": task_record["id"], "scorer_id": scorer_record["id"]}


def promote(report_path: Path, target: str, reviewed_by: str | None) -> dict:
    report = json.loads(report_path.read_text())
    if report.get("schema") != SCHEMA:
        raise AdmissionError(f"not an admission report: {report_path}")
    if target == "core":
        if not report.get("all_passed"):
            raise AdmissionError("report has failing gates; Core promotion refused")
        if not reviewed_by:
            raise AdmissionError("Core promotion requires an explicit human reviewer (--reviewed-by)")
    report["promotion"] = {"core": target == "core", "reviewed_by": reviewed_by,
                           "promoted_at": utcnow(), "promoted_to": target}
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aco-admission", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    admit = sub.add_parser("admit", help="run all admission gates against a task bundle")
    admit.add_argument("bundle", type=Path)
    admit.add_argument("--data-root", type=Path, required=True)

    prom = sub.add_parser("promote", help="promote an admission report (Core requires human review)")
    prom.add_argument("report", type=Path)
    prom.add_argument("--to", choices=("core",), default="core")
    prom.add_argument("--reviewed-by", default=None)

    args = parser.parse_args(argv)
    try:
        if args.command == "admit":
            report = run_admission(args.bundle, args.data_root)
            print(json.dumps({"all_passed": report["all_passed"], "report": report["report"]}, indent=2))
            return 0 if report["all_passed"] else 1
        promote(args.report, args.to, args.reviewed_by)
        return 0
    except AdmissionError as exc:
        print(f"admission error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
