# Lifecycle Operations: Safe Stop, Cancel, Resume, and Recovery (#16)

How ACO behaves when processes stop, plans are cancelled, or the service
restarts — and what a human must do explicitly.

## Core rule

ACO never implicitly re-runs an attempted trial. Closing a CLI, killing a
client, or losing a watcher changes nothing server-side. Only these
management calls change a plan:

```text
POST /v1/experiments/{id}/cancel   # idempotent; terminal, cannot be undone
POST /v1/experiments/{id}/resume   # releases a restart-paused plan
```

## State machines (#16 reopen)

```text
Trial:       planned -> claimed -> running -> sealed | anomaly | cancelled
Experiment:  planned -> running -> paused | completed | cancelled
```

Every trial termination is funnelled through one `finish_trial(trigger)`
interface (ADR 0003): submit, exit, timeout, cancel, supervisor loss/crash,
pre-agent launch failures, startup recovery. First legal trigger wins — a
trial already terminal is never re-finished, and a losing trigger can neither
rewrite history nor resurrect the trial. Scoring state stays independent.

When every trial of an experiment is terminal, the experiment auto-completes.
A cancelled experiment stays cancelled forever; `resume` only releases a
restart-pause, never an explicit cancel.

For a sealed answer whose immutable TaskVersion declares `default_scorer`,
the same trusted transaction queues one `initial:<scorer-version-id>`
verification. The execution manager runs it through the normal independent
verifier path. Legacy or synthetic tasks without that declaration have no
required initial verification and remain compatible with the lifecycle. The
`initial:` idempotency namespace is reserved for this trusted path; manual
verification requests use ordinary keys.

## Timeout unification

Harbor's own agent timeout and the ACO outer deadline land on the same
verdict path: the answer is sealed, and a `timeout_verdict` event records the
planned deadline (agent start + agent timeout), the actual freeze time, and
the tolerance verdict. Within tolerance the answer stays sealed and eligible
for the capability curve; over tolerance it becomes a diagnostic anomaly. A
timeout never bypasses eligibility and never produces a plain `agent_error`.

## Submit intent

A submit is intent-only (#12). The supervisor watches for the persisted
submit intent and stops the agent container the moment it appears (a 0.5s
poll: writes racing that window land in the frozen post-stop state), and the
sealed answer is the frozen post-stop state. The submitted answer is the
single official answer.

## Cancel semantics

| Trial state at cancel            | Outcome                                                        |
| -------------------------------- | -------------------------------------------------------------- |
| `planned` (never attempted)      | `cancelled` — never runs                                       |
| `claimed`, no run row (crashed before spawn) | `cancelled` — the launch intent never existed |
| run in flight                    | supervisor SIGTERMed, agent container removed; sealed answer stays official, otherwise the trial gets a diagnostic anomaly |
| run finished, answer sealed      | untouched (first legal termination already won)                |
| anomalous                        | untouched                                                      |

Repeating `cancel` returns the persisted summary without new side effects.
A late agent submit against a cancelled trial is rejected (`409
trial_cancelled`) — a late request can never extend a deadline.

## Restart reconciliation (manager startup)

On every manager start, in order:

1. **Stale claims**: a `claimed` trial with no run row resets to `planned`
   (the run row is the launch intent; no row means no side effect happened).
   Any trial with a run row is never reset — an attempted trial is never
   silently re-answered.
2. **Interrupted seals**: staging/published answers are finished or flagged
   from disk truth (`artifacts.recover`, #14). Disk truth wins: a seal a
   dying supervisor left recoverable is published, not downgraded.
3. **Lost supervisors**: unfinished runs whose supervisor pid is gone are
   finished as `supervisor_lost`; the trial terminalises through the funnel
   with the answer row's outcome — a seal recovered in step 2 stays
   officially `sealed`, anything else becomes a diagnostic anomaly.
4. **Terminal reconciliation**: a crash between sealing and the state write
   leaves a terminal answer on a non-terminal trial; startup moves each such
   trial through the same funnel (never re-collects, never creates a run).
5. **Initial scoring reconciliation**: sealed trials with a declared default
   scorer but no initial verification are queued idempotently. This repeats
   the seal transaction's enqueue step and closes a manager/supervisor crash
   window. Anomaly, cancelled, and unverifiable answers are skipped.
6. **Adoption**: unfinished runs with a live supervisor pid are recorded as
   `adopted` events; the supervisor keeps running and records its own
   outcome. No restart, no second attempt.
7. **Stuck scoring**: verifications left `running` by a manager crash return
   to `queued` (scoring calls no model; requeueing is not an agent rerun).
8. **Pause**: every experiment that still has planned trials is set to
   `paused` — unstarted plans wait for an explicit `resume`.

## Manual recovery flow

1. Restart the API and the execution manager against the same data root.
2. Inspect `GET /v1/experiments/{id}` — `progress` shows plan, execution,
   cancellation, anomaly, and initial-scoring counts
   (`verification_required`, `verification_terminal`,
   `verification_pending`, `verification_succeeded`, and
   `verification_errors`); `attempted` counts every trial that ever produced
   a launch intent (a run row), so a trial cancelled before launch was never
   attempted. `aco run --wait` waits for `verification_pending` to reach zero
   while preserving legacy tasks with no required scorer.
3. Decide explicitly per experiment:
   - continue: `POST /v1/experiments/{id}/resume`;
   - stop: `POST /v1/experiments/{id}/cancel`.
4. Trials that were mid-flight during the outage either finish by their
   surviving supervisor, or appear as `supervisor_lost`/anomaly diagnostics.
   None of them re-runs automatically.

## Event trail

Every decision above appends a row to `lifecycle_events`
(experiment/trial, event, machine-readable reason, JSON detail). Query it to
explain any state:

```sql
SELECT created_at, experiment_id, trial_id, event, reason, detail
FROM lifecycle_events ORDER BY id;
```
