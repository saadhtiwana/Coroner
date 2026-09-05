from __future__ import annotations

from coroner_brain.contract import Contract
from coroner_brain.evidence import (
    CEILINGS,
    EvidenceClass,
    ceiling,
    classify_evidence,
    gate,
    has_usable_logs,
)


def test_classifies_each_recorded_failure(
    crashloop: Contract, imagepull: Contract, oomkilled: Contract, oom_init: Contract
) -> None:
    assert classify_evidence(imagepull) is EvidenceClass.IMAGE_PULL_WITH_REGISTRY_ERROR
    assert classify_evidence(crashloop) is EvidenceClass.CRASHLOOP_WITH_FATAL_LOG
    assert classify_evidence(oomkilled) is EvidenceClass.OOM_WITH_LIMITS
    assert classify_evidence(oom_init) is EvidenceClass.OOM_WITH_LIMITS


def test_ceilings_encode_the_committed_predictions() -> None:
    # Section 2.4 predicted 90 percent for image pulls and roughly 60 percent
    # blended for crashloops. The ceilings must reflect that ordering.
    assert ceiling(EvidenceClass.IMAGE_PULL_WITH_REGISTRY_ERROR) > ceiling(
        EvidenceClass.CRASHLOOP_WITH_FATAL_LOG
    )
    assert ceiling(EvidenceClass.CRASHLOOP_WITH_FATAL_LOG) > ceiling(
        EvidenceClass.CRASHLOOP_LOGS_NO_ERROR
    )
    assert ceiling(EvidenceClass.CRASHLOOP_LOGS_NO_ERROR) > ceiling(
        EvidenceClass.CRASHLOOP_LOGS_UNAVAILABLE
    )
    assert all(0.0 <= v <= 1.0 for v in CEILINGS.values())


def test_usable_logs_distinguishes_unavailable_from_empty(
    crashloop: Contract, oom_init: Contract
) -> None:
    assert has_usable_logs(crashloop)
    # The init OOM retrieved a response but the container wrote nothing.
    assert oom_init.logs.available
    assert oom_init.logs.empty
    assert not has_usable_logs(oom_init)


def test_gate_abstains_on_crashloop_without_logs(crashloop: Contract) -> None:
    stripped = crashloop.model_copy(deep=True)
    stripped.logs.available = False
    stripped.logs.empty = True
    stripped.logs.content = ""

    abstain, reason = gate(stripped, classify_evidence(stripped))
    assert abstain
    assert "generic" in reason
    assert "unretrievable" in reason


def test_gate_allows_a_crashloop_that_has_a_fatal_line(crashloop: Contract) -> None:
    abstain, _ = gate(crashloop, classify_evidence(crashloop))
    assert not abstain


def test_gate_allows_image_pull_without_logs(imagepull: Contract) -> None:
    # Image pulls never produce logs, and their absence carries no information
    # because the container never started. The gate must not abstain on that.
    assert not imagepull.logs.available
    abstain, _ = gate(imagepull, classify_evidence(imagepull))
    assert not abstain
