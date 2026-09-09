"""Isolation-probe verifier fixture (#15).

Behaves like a verifier but first tries everything the container must NOT be
able to do; every attempt's outcome is reported in the result. The
management side (and the e2e test) asserts all probes failed and the answer
bytes are unchanged:

- network: outbound TCP connect (must fail: --network none)
- answer_write: append to the sealed answer (must fail: read-only mount)
- verifier_write: write into the verifier bundle (must fail: read-only mount)
- output_write: write the result (must succeed: the only writable mount)
"""

import json
import socket
from pathlib import Path

SCHEMA = "aco.verification-result/v1"


def main() -> None:
    probes = {}

    try:
        connection = socket.create_connection(("example.com", 80), timeout=3)
        connection.close()
        probes["network"] = "connected"
    except OSError as exc:
        probes["network"] = f"failed: {type(exc).__name__}"

    try:
        with open("/answer/workspace/answer.txt", "a") as handle:
            handle.write("TAMPERED")
        probes["answer_write"] = "wrote"
    except OSError as exc:
        probes["answer_write"] = f"failed: {type(exc).__name__}"

    try:
        Path("/verifier/probe.txt").write_text("TAMPERED")
        probes["verifier_write"] = "wrote"
    except OSError as exc:
        probes["verifier_write"] = f"failed: {type(exc).__name__}"

    output = Path("/output/result.json")
    # optimistic: a failed write leaves no result file at all, which the
    # management side records as an invalid_output error
    probes["output_write"] = "wrote"
    output.write_text(json.dumps({"schema": SCHEMA, "pass": True, "probes": probes}))


if __name__ == "__main__":
    main()
