"""Test-only fake agent (#13): controlled stimulus, NOT a production harness.

Scenarios come from the task instruction text (the same prompt the agent
fetches from the Session API):

- ``FAKE:submit``     claim via Session API, write files, submit end-intent
- ``FAKE:exit``       claim, write files, then die with a non-zero exit
- ``FAKE:background`` claim, write files, start a detached background writer,
                      then return (foreground exit while background writes)
- ``FAKE:sleep``      claim, write files, then hang far past the agent
                      timeout, so Harbor itself raises AgentTimeoutError
- anything else       claim, write files, return (control)

Requires ACO_BASE_URL and ACO_SESSION_TOKEN in the supervisor environment
(the manager mints the trial-scoped token and passes it down).
"""

import os
import shlex

from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.agents.nop import NopAgent


def _session_request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    import json
    import urllib.request

    request = urllib.request.Request(
        os.environ["ACO_BASE_URL"].rstrip("/") + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def scenario_of(instruction: str) -> str:
    prefix = instruction.strip().split("\n", 1)[0].strip()
    return prefix[5:].strip() if prefix.upper().startswith("FAKE:") else "control"


class FakeAgent(NopAgent):
    @staticmethod
    def name():
        return "aco-fake-agent"

    async def run(self, instruction, environment, context):
        scenario = scenario_of(instruction)
        token = os.environ["ACO_SESSION_TOKEN"]

        # 1. claim the unique task through the restricted Session API
        task = _session_request("GET", "/v1/session/task", token)
        prompt = task["instruction"]

        # 2. write ordinary files into the workspace (created on demand; the
        # prebuilt harbor image ships without it)
        result = await environment.exec(
            "mkdir -p /workspace && printf '%s' "
            + shlex.quote(prompt)
            + " > /workspace/answer.txt"
        )
        if result.return_code:
            raise NonZeroAgentExitCodeError(f"write answer.txt failed: {result.stderr}")

        if scenario == "background":
            # detached from the agent process: survives the foreground exit
            await environment.exec(
                "mkdir -p /workspace && setsid sh -c"
                " 'for i in $(seq 1 100); do echo $i >> /workspace/bg.txt; sleep 0.2; done'"
                " >/dev/null 2>&1 </dev/null &"
            )
            return

        if scenario == "exit":
            # foreground failure without submitting an end-intent
            raise NonZeroAgentExitCodeError("fake agent asked to exit")

        if scenario == "submit":
            # end-intent only: the official answer is the workspace snapshot
            # the supervisor seals; the session never carries an answer body
            _session_request(
                "POST", "/v1/session/submit", token,
                {"idempotency_key": f"fake-{task['trial_id']}"},
            )

        if scenario == "submit-late-write":
            # keeps writing after the submit intent: the watchdog must stop
            # the container before the first late write lands (>= 2s after
            # the POST, vs a 0.5s poll) — the sealed answer excludes them
            _session_request(
                "POST", "/v1/session/submit", token,
                {"idempotency_key": f"fake-{task['trial_id']}"},
            )
            await environment.exec("sleep 2")
            for i in range(50):
                await environment.exec(
                    "mkdir -p /workspace && echo '"
                    + str(i) + "' >> /workspace/late.txt"
                )
                await environment.exec("sleep 0.2")

        if scenario == "sleep":
            # hang past the [agent] timeout_sec: Harbor raises
            # AgentTimeoutError — the unified timeout verdict path (#16)
            await environment.exec("sleep 300")
            return
