# Operator runbook: one Pi sanity trial

This runbook uses an operator-selected private Pilot data root and an explicit
mock Ark endpoint. It does not make a real Ark call. The operator must supply
`ARK_AGENT_PLAN_API_KEY` to the manager environment; do not put that value in
version JSON, SQLite, logs, or checked-in files.

Set the paths once in a shell:

```bash
cd /path/to/AgentCapabilityObservatory
export ACO_SRC="${ACO_SRC:-$(git rev-parse --show-toplevel)}"
export ACO_PYTHON="${ACO_PYTHON:-python3.12}"
export ACO_PRIVATE_TASK_STORE="${ACO_PRIVATE_TASK_STORE:?set this to the canonical private task store}"
export ACO_DATA_ROOT="${ACO_DATA_ROOT:-$ACO_PRIVATE_TASK_STORE/operational-data}"
export ACO_API_URL=http://127.0.0.1:8000
export ACO_SESSION_URL=http://127.0.0.1:8001
export ACO_MANAGEMENT_TOKEN=<operator-management-token>
export ARK_AGENT_PLAN_API_KEY=<operator-supplied-value>
export PYTHONPATH="$ACO_SRC/src"
```

Prebuild the reusable Pi image before starting any trial. Record the printed
digest and use that exact value in the target profile:

```bash
PI_IMAGE_DIGEST="$($ACO_PYTHON -m aco.cli build-agent-image)"
cat > pi-target.json <<JSON
{
  "schema_version": 1,
  "harness": "pi",
  "harness_version": "0.84.1",
  "model": "glm-5.3-flash",
  "thinking": "max",
  "provider": "ark-agent-plan",
  "provider_api_style": "openai-responses",
  "adapter_version": "0.1.1",
  "assistance_mode": "none",
  "environment": "${PI_IMAGE_DIGEST}",
  "credentials": ["ark-agent-plan-main"]
}
JSON
```

Start these three processes in separate terminals, all using the same data
root. The manager is the only process that needs the provider credential. The
Session API is trial-scoped but must bind to a Docker-reachable interface for
the gateway; keep this surface behind firewall/access controls and off
untrusted external networks. Management stays on `127.0.0.1`.

```bash
# management API
ACO_DATA_ROOT="$ACO_DATA_ROOT" ACO_MANAGEMENT_TOKEN="$ACO_MANAGEMENT_TOKEN" \
  PYTHONPATH="$PYTHONPATH" $ACO_PYTHON -m uvicorn aco.app:management_app \
    --host 127.0.0.1 --port 8000

# restricted Session API
ACO_DATA_ROOT="$ACO_DATA_ROOT" PYTHONPATH="$PYTHONPATH" \
  $ACO_PYTHON -m uvicorn aco.app:session_app --host 0.0.0.0 --port 8001

# execution manager
ACO_DATA_ROOT="$ACO_DATA_ROOT" ACO_MANAGEMENT_TOKEN="$ACO_MANAGEMENT_TOKEN" \
  ARK_AGENT_PLAN_API_KEY="$ARK_AGENT_PLAN_API_KEY" \
  ARK_AGENT_PLAN_BASE_URL=http://host.docker.internal:<mock-port>/ark \
  PYTHONPATH="$PYTHONPATH" $ACO_PYTHON -m aco.execution \
    --data-root "$ACO_DATA_ROOT" --api-url "$ACO_API_URL" \
    --session-api-url "$ACO_SESSION_URL"
```

Wait for Management to answer its health check, then register the reusable
target. The manager may already be running; it polls for newly registered
versions.

```bash
until curl -fsS "$ACO_API_URL/healthz" >/dev/null; do sleep 1; done
curl -fsS "$ACO_API_URL/healthz" | jq
$ACO_PYTHON -m aco.cli register config pi-reusable v1 pi-target.json \
  --api-url "$ACO_API_URL" --token "$ACO_MANAGEMENT_TOKEN"
```

For automated or local smoke use, start the repository's `MockArk` fixture (or
an equivalent local mock) and substitute its port for `<mock-port>`. The mock
returns a deterministic Pi tool call and requires no real key. Keep
`CI=true` when validating the hard reject: the manager must reject the real
Ark default, while the explicit mock URL is allowed.

Run the admitted v2 sanity task. `--wait` now includes the required initial
verification, so it returns after the independent verifier is terminal:

```bash
$ACO_PYTHON -m aco.cli run --api-url "$ACO_API_URL" \
  --token "$ACO_MANAGEMENT_TOKEN" \
  --task py-pilot-page-fix@v2 --target pi-reusable@v1 \
  --allow-paid-run --wait --json > experiment.json
```

Confirm the sealed answer, automatic verifier verdict, and result series:

```bash
EXP_ID="$(jq -r .id experiment.json)"
TRIAL_ID="$(jq -r '.trials[0].id' experiment.json)"
curl -sS -H "Authorization: Bearer $ACO_MANAGEMENT_TOKEN" \
  "$ACO_API_URL/v1/experiments/$EXP_ID" | jq '.progress'
curl -sS -H "Authorization: Bearer $ACO_MANAGEMENT_TOKEN" \
  "$ACO_API_URL/v1/trials/$TRIAL_ID/verifications" | jq
curl -sS -H "Authorization: Bearer $ACO_MANAGEMENT_TOKEN" \
  "$ACO_API_URL/v1/results?scorer=py-pilot-page-fix-verifier@v2" | jq
```

The progress object should show `verification_pending: 0` and one terminal
initial verification. A verifier error is terminal for waiting and remains a
`score_error` in results; it is never represented as `pass=false`.
