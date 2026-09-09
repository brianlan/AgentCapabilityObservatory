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

## Cancel semantics

| Trial state at cancel            | Outcome                                                        |
| -------------------------------- | -------------------------------------------------------------- |
| `planned` (never attempted)      | `cancelled` — never runs                                       |
| claimed, run in flight           | supervisor SIGTERMed, agent container removed; sealed answer stays official, otherwise the trial gets a diagnostic anomaly |
| run finished, answer sealed      | untouched (first legal termination already won)                |
| anomalous                        | untouched                                                      |

Repeating `cancel` returns the persisted summary without new side effects.
A late agent submit against a cancelled trial is rejected (`409
trial_cancelled`) — a late request can never extend a deadline.

## Restart reconciliation (manager startup)

On every manager start, in order:

1. **Stale claims**: a `claimed` trial with no run row resets to `planned`
   (the run row is the launch intent; no row means no side effect happened).
2. **Lost supervisors**: unfinished runs whose supervisor pid is gone are
   finished as `supervisor_lost` diagnostics; their agent containers are
   removed.
3. **Interrupted seals**: staging/published answers are finished or flagged
   from disk truth (`artifacts.recover`, #14).
4. **Adoption**: unfinished runs with a live supervisor pid are recorded as
   `adopted` events; the supervisor keeps running and records its own
   outcome. No restart, no second attempt.
5. **Stuck scoring**: verifications left `running` by a manager crash return
   to `queued` (scoring calls no model; requeueing is not an agent rerun).
6. **Pause**: every experiment that still has planned trials is set to
   `paused` — unstarted plans wait for an explicit `resume`.

## Manual recovery flow

1. Restart the API and the execution manager against the same data root.
2. Inspect `GET /v1/experiments/{id}` — `progress` shows plan, attempted,
   cancelled, sealed, and anomaly counts.
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
