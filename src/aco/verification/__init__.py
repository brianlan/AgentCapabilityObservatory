"""Independent verification of Sealed Answers (#15).

See runner.py: the trusted management side runs one verifier container per
scoring record with the answer and verifier bundle mounted read-only, no
network, and a fresh private output directory. Every execution, error, and
result appends a verifications row — never an overwrite.
"""

from .runner import (
    bundle_digest,
    claim_next_queued,
    default_verification_progress,
    enqueue_default_verification,
    execute_verification,
    reconcile_default_verifications,
    requeue_stuck_running,
    run_pending,
)

__all__ = [
    "bundle_digest", "claim_next_queued", "default_verification_progress",
    "enqueue_default_verification", "execute_verification",
    "reconcile_default_verifications", "requeue_stuck_running", "run_pending",
]
