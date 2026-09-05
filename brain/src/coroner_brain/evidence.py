"""Deterministic evidence classification and confidence ceilings.

This module runs before any model call. It decides what class of evidence an
incident carries, what confidence that class can support at most, and whether
the context is empty enough that asking a model at all would only be inviting
it to fill a void. See docs/DESIGN.md sections 4.2 and 2.4.
"""

from __future__ import annotations

from enum import StrEnum

from coroner_brain.contract import Contract

# Exit codes that identify a specific failure. Anything else is generic and
# carries no causal information on its own.
INFORMATIVE_EXIT_CODES = frozenset({137, 139, 143})

# Substrings that suggest a log body actually names a failure rather than just
# recording startup progress. Deliberately broad: the cost of treating a
# useless log as useful is a capped-confidence diagnosis, while the cost of
# treating a useful log as useless is a wrongly abstained incident.
FATAL_MARKERS: tuple[str, ...] = (
    "fatal",
    "error",
    "panic",
    "exception",
    "traceback",
    "refused",
    "timeout",
    "timed out",
    "cannot",
    "could not",
    "unable",
    "failed",
    "denied",
    "unauthorized",
    "forbidden",
    "no such",
    "not found",
)


class EvidenceClass(StrEnum):
    """What the collected evidence can support, independent of the model."""

    IMAGE_PULL_WITH_REGISTRY_ERROR = "image_pull_with_registry_error"
    IMAGE_PULL_WITHOUT_DETAIL = "image_pull_without_detail"
    OOM_WITH_LIMITS = "oom_with_limits"
    OOM_WITHOUT_LIMITS = "oom_without_limits"
    CRASHLOOP_WITH_FATAL_LOG = "crashloop_with_fatal_log"
    CRASHLOOP_LOGS_NO_ERROR = "crashloop_logs_no_error"
    CRASHLOOP_LOGS_UNAVAILABLE = "crashloop_logs_unavailable"
    UNKNOWN = "unknown"


# Ceilings from docs/DESIGN.md 4.2 control 3. The model may lower its own
# confidence but never raise it above these.
CEILINGS: dict[EvidenceClass, float] = {
    EvidenceClass.IMAGE_PULL_WITH_REGISTRY_ERROR: 0.95,
    EvidenceClass.IMAGE_PULL_WITHOUT_DETAIL: 0.50,
    EvidenceClass.OOM_WITH_LIMITS: 0.90,
    EvidenceClass.OOM_WITHOUT_LIMITS: 0.60,
    EvidenceClass.CRASHLOOP_WITH_FATAL_LOG: 0.80,
    EvidenceClass.CRASHLOOP_LOGS_NO_ERROR: 0.40,
    EvidenceClass.CRASHLOOP_LOGS_UNAVAILABLE: 0.15,
    EvidenceClass.UNKNOWN: 0.30,
}


def ceiling(evidence_class: EvidenceClass) -> float:
    return CEILINGS[evidence_class]


def has_usable_logs(contract: Contract) -> bool:
    """True when a log body was retrieved and is not blank.

    available and empty are separate facts in the contract. Both mean the same
    thing here, which is that there is no application-authored text to reason
    over, but they are recorded distinctly so the abstention reason can say
    which one happened.
    """
    return (
        contract.logs.available and not contract.logs.empty and bool(contract.logs.content.strip())
    )


def has_fatal_marker(contract: Contract) -> bool:
    body = contract.logs.content.lower()
    return any(marker in body for marker in FATAL_MARKERS)


def registry_error_present(contract: Contract) -> bool:
    """True when an event or the waiting message carries the pull failure text.

    For image pulls Kubernetes performed the failing operation itself and
    reported why, so this is the difference between a near-deterministic
    diagnosis and a guess about which of several pull failures occurred.
    """
    haystacks = [contract.container.waiting_message.lower()]
    haystacks += [e.message.lower() for e in contract.events]
    return any(
        ("failed to pull" in h or "errimagepull" in h or "failed to resolve" in h)
        for h in haystacks
    )


def classify_evidence(contract: Contract) -> EvidenceClass:
    """Classify what the evidence can support. Deterministic, no model call."""
    failure = contract.failure_type

    if failure == "ImagePullBackOff":
        return (
            EvidenceClass.IMAGE_PULL_WITH_REGISTRY_ERROR
            if registry_error_present(contract)
            else EvidenceClass.IMAGE_PULL_WITHOUT_DETAIL
        )

    if failure in ("OOMKilled", "OOMKilledDuringInit"):
        has_limits = bool(contract.container.memory_limit)
        return EvidenceClass.OOM_WITH_LIMITS if has_limits else EvidenceClass.OOM_WITHOUT_LIMITS

    if failure == "CrashLoopBackOff":
        if not has_usable_logs(contract):
            return EvidenceClass.CRASHLOOP_LOGS_UNAVAILABLE
        return (
            EvidenceClass.CRASHLOOP_WITH_FATAL_LOG
            if has_fatal_marker(contract)
            else EvidenceClass.CRASHLOOP_LOGS_NO_ERROR
        )

    return EvidenceClass.UNKNOWN


def gate(contract: Contract, evidence_class: EvidenceClass) -> tuple[bool, str]:
    """Decide whether to abstain before spending a model call.

    Returns (abstain, reason). Section 4.2 control 1: a CrashLoopBackOff whose
    previous logs could not be retrieved and whose exit code is generic carries
    no causal signal at all. Asking a model to explain it would only be giving
    it the opportunity to invent one.
    """
    terminated = contract.container.last_terminated
    exit_code = terminated.exit_code if terminated else None
    generic_exit = exit_code is None or exit_code not in INFORMATIVE_EXIT_CODES

    if (
        contract.failure_type == "CrashLoopBackOff"
        and not has_usable_logs(contract)
        and generic_exit
    ):
        if not contract.logs.available:
            detail = "previous and current container logs were both unretrievable"
        else:
            detail = "the container produced no log output"
        return True, (
            f"No causal signal is present. The failure is CrashLoopBackOff with exit code "
            f"{exit_code if exit_code is not None else 'unknown'}, which is generic, and "
            f"{detail}. Nothing in the collected evidence names a cause."
        )

    if CEILINGS[evidence_class] < ABSTAIN_BELOW:
        return True, (
            f"Evidence class {evidence_class.value} supports at most "
            f"{CEILINGS[evidence_class]:.2f} confidence, below the abstention threshold."
        )

    return False, ""


# Kept separate from Settings so the gate stays importable without configuration.
ABSTAIN_BELOW = 0.20
