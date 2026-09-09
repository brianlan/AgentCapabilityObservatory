"""Synthetic verifier fixture (#20): scored entirely on the sealed workspace.

Checks, all declared by the task's interface (no hidden requirements):
- answer.txt holds the sample output (required output, contract-enforced)
- solution.py defines add and satisfies properties implied by the interface
  (addition) that go beyond the single public sample — hardcoding the sample
  does not pass
- the visible test file is unmodified
Any workspace result.json is ignored: only the verifier's own /output is read.
"""

import hashlib
import json
from pathlib import Path

SCHEMA = "aco.verification-result/v1"


def main() -> None:
    config = json.loads(Path("/verifier/config.json").read_text())
    workspace = Path("/answer/workspace")
    failures = []

    try:
        answer = (workspace / "answer.txt").read_text()
    except OSError:
        answer = None
    if answer != config["expected_answer"]:
        failures.append("answer.txt missing or wrong")

    try:
        namespace = {}
        exec(compile((workspace / "solution.py").read_text(), "solution.py", "exec"), namespace)
        for a, b, expected in ((2, 2, 4), (-1, 1, 0), (0, 0, 0)):
            if namespace["add"](a, b) != expected:
                failures.append(f"add({a}, {b}) != {expected}")
    except Exception as exc:  # noqa: BLE001 — untrusted solution content
        failures.append(f"solution.py unusable: {type(exc).__name__}")

    try:
        visible_tests = (workspace / "test_solution.py").read_bytes()
    except OSError:
        visible_tests = b""
    if hashlib.sha256(visible_tests).hexdigest() != config["visible_test_digest"]:
        failures.append("visible test file was modified")

    result = {"schema": SCHEMA, "pass": not failures}
    if failures:
        result["failures"] = failures
    Path("/output/result.json").write_text(json.dumps(result))


if __name__ == "__main__":
    main()
