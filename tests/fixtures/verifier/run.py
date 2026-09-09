"""Minimal deterministic verifier fixture (#15).

Bundle contract (mounted read-only at /verifier):
- config.json: {"expected": "<exact answer.txt content>", "submetrics": {...}?}

Input (mounted read-only at /answer): a sealed answer snapshot; the fixture
task's answer is the file workspace/answer.txt. A missing answer file is a
valid failing verdict (pass=false), not a scoring error.

Output: /output/result.json with schema "aco.verification-result/v1".
"""

import json
from pathlib import Path

SCHEMA = "aco.verification-result/v1"


def main() -> None:
    config = json.loads(Path("/verifier/config.json").read_text())
    try:
        content = Path("/answer/workspace/answer.txt").read_text()
    except OSError:
        content = None  # missing output: deterministic failing verdict
    result = {
        "schema": SCHEMA,
        "pass": content == config["expected"],
    }
    if config.get("submetrics"):
        result["submetrics"] = config["submetrics"]
    Path("/output/result.json").write_text(json.dumps(result))


if __name__ == "__main__":
    main()
