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

# The runtime's other wording for the same init kill, recorded live.
PROCREADY = "error during container init: procReady not received"


def test_classifies_each_recorded_failure(
    crashloop: Contract, imagepull: Contract, oomkilled: Contract, oom_init: Contract
) -> None:
    assert classify_evidence(imagepull) is EvidenceClass.IMAGE_PULL_WITH_REGISTRY_ERROR
    assert classify_evidence(crashloop) is EvidenceClass.CRASHLOOP_WITH_FATAL_LOG
    assert classify_evidence(oomkilled) is EvidenceClass.OOM_WITH_LIMITS
    # The StartError/128 shape has its own row. Before it did, it fell through
    # to the running OOM ceiling and a diagnosis resting on one runtime message
    # and no logs was capped at 0.90, the same as a kernel verdict.
    assert classify_evidence(oom_init) is EvidenceClass.OOM_DURING_INIT_WITH_MARKER


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

    # An init kill rests on a runtime message with no kernel verdict and no
    # logs, so it sits below the running OOM. Without the message there is
    # nothing at all, so that shape sits at the abstention floor.
    assert ceiling(EvidenceClass.OOM_DURING_INIT_WITH_MARKER) < ceiling(
        EvidenceClass.OOM_WITH_LIMITS
    )
    assert ceiling(EvidenceClass.OOM_DURING_INIT_WITHOUT_MARKER) == ceiling(
        EvidenceClass.CRASHLOOP_LOGS_UNAVAILABLE
    )


def test_init_oom_ceiling_is_explicit_and_below_the_running_oom(oom_init: Contract) -> None:
    """The Phase 3 finding.

    A 2Mi init kill with empty logs scored 0.90 with seven valid citations,
    because there was no row for StartError/128 and it fell through to the
    running OOM ceiling. The diagnosis was right, but the ceiling was not
    earned by the evidence class, which is the point of a ceiling.
    """
    assert oom_init.failure_type == "OOMKilledDuringInit"
    assert oom_init.logs.available
    assert oom_init.logs.empty
    evidence_class = classify_evidence(oom_init)
    assert evidence_class is EvidenceClass.OOM_DURING_INIT_WITH_MARKER
    assert ceiling(evidence_class) == 0.80
    abstain, _ = gate(oom_init, evidence_class)
    assert not abstain


def test_init_oom_without_any_marker_abstains_before_the_model(oom_init: Contract) -> None:
    """If neither the message nor an event names memory, nothing does."""
    stripped = oom_init.model_copy(deep=True)
    assert stripped.container.last_terminated is not None
    stripped.container.last_terminated.message = PROCREADY
    stripped.events = [e for e in stripped.events if "oom" not in e.message.lower()]

    evidence_class = classify_evidence(stripped)
    assert evidence_class is EvidenceClass.OOM_DURING_INIT_WITHOUT_MARKER
    abstain, reason = gate(stripped, evidence_class)
    assert abstain
    assert "names memory" in reason


def test_init_oom_marker_may_come_from_an_earlier_event(oom_init: Contract) -> None:
    """Recorded live: the runtime's wording changes between restarts of the same kill."""
    reworded = oom_init.model_copy(deep=True)
    assert reworded.container.last_terminated is not None
    reworded.container.last_terminated.message = PROCREADY
    assert any("oom-killed" in e.message.lower() for e in reworded.events)
    assert classify_evidence(reworded) is EvidenceClass.OOM_DURING_INIT_WITH_MARKER


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
