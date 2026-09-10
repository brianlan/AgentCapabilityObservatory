# Centralize Trial finalization

Submission, agent exit, timeout, cancellation, sealing, token invalidation, and Experiment completion all affect one Trial outcome. Independent transitions allow later events to overwrite the first legal outcome, leave completed Trials reclaimable, and admit writes made after submission.

ACO therefore routes every termination through one `finish_trial(trigger)` interface. It owns first-trigger-wins arbitration, stopping writes, sealing with the TaskVersion's Artifact Contract, revoking the session capability, setting the Trial terminal state, and completing the Experiment when appropriate. Trial execution ends as `sealed`, `anomaly`, or `cancelled`; scoring remains separate. A Finish Request carries only an idempotency key, and the Sealed Answer remains the sole answer.
