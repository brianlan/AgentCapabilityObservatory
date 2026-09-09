# Verifier Contract (#15)

How ACO runs an independent, offline verifier container against a Sealed
Answer, and what a verifier bundle must look like.

## Trust boundaries

- The **agent** and the **submission content** never produce scores. Only the
  trusted management side (ACO manager + API) parses verifier output.
- The verifier container is untrusted code execution: it must not reach the
  network, the application, the SQLite database, model/API credentials, or
  the Docker socket.

## Scorer version registration

A scorer version is registered like any other version (`POST /v1/versions`,
`kind: "scorer"`):

```json
{
  "image": "python@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254",
  "entrypoint": ["python", "/verifier/run.py"],
  "result_schema": "aco.verification-result/v1"
}
```

- `image` must be digest-pinned (`name@sha256:...`).
- The registration carries one asset: `{"name": "bundle", "digest": "<sha256>"}`.
  Compute it with:

  ```
  PYTHONPATH=src python -m aco.verification.runner <bundle-dir>
  ```

- The bundle files are placed by the operator at
  `<data-root>/verifiers/<scorer-version-id>/`. The runner hashes the bundle
  per execution and refuses to score on mismatch — a tampered or misplaced
  bundle becomes an `infra_error`, never a score.

## Container isolation (actual docker arguments)

```
docker run --cidfile <work>/container.id \
  --network none --read-only --tmpfs /tmp \
  -v <answers>/<digest>:/answer:ro \
  -v <verifiers>/<version-id>:/verifier:ro \
  -v <work>/output:/output \
  <image> <entrypoint...>
```

- No network, read-only root filesystem, no environment variables passed.
- The sealed answer and the verifier bundle are read-only mounts; only
  `output/` (fresh per execution) is writable. Stale or planted result files
  cannot reach the authoritative parse.
- Runtime evidence (network mode, mounts, env keys, docker socket) is
  captured from `docker inspect` of the actual container and stored on the
  verification record.

## Result file

The verifier writes `/output/result.json`:

```json
{
  "schema": "aco.verification-result/v1",
  "pass": false,
  "submetrics": {"match": 1.0}
}
```

- `schema` must equal the registered `result_schema`.
- `pass` must be a boolean. `false` is a valid score (a failing answer).
- `submetrics` is optional: names mapping to numbers (no booleans).

Everything else — non-zero exit, timeout (60s), missing/malformed/schema-
mismatched result — is recorded as an **error** (`verifier_error`,
`invalid_output`, `infra_error`) with `pass = NULL`. Scoring errors never
enter the capability denominator.

## Records

Each execution appends one row to `verifications` (queued → running →
succeeded/error). Re-scoring with a new scorer version appends; old records
stay readable. The same request (same trial + idempotency key + payload) is
idempotent. Same-version successful verdicts that disagree are flagged
`stable = false` on query — no highest result is ever selected.

## Fixture

`tests/fixtures/verifier/run.py` is a minimal deterministic verifier:
it compares `/answer/workspace/answer.txt` against `expected` from
`/verifier/config.json` (a missing answer file is a deterministic
`pass = false`). `probe.py` additionally attempts network access and writes
to the answer/bundle mounts, reporting each outcome — used to prove the
isolation guarantees in `tests/e2e/test_verification.py`.
