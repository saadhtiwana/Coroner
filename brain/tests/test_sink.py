"""The stdout sink and the rendering every sink shares.

Two properties are checked structurally. The observed block contains only
collected text, so a model claim can never appear there. The approve
affordance is absent, not disabled, whenever it must not be offered.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

from coroner_brain.contract import Contract
from coroner_brain.diagnosis import Citation, Outcome
from coroner_brain.sink import (
    Notice,
    StdoutSink,
    render_decision,
    render_inferred,
    render_observed,
    render_text,
)
from coroner_brain.verdict import DiagnoseResponse

FABRICATED = "the model says the database is on fire"


def _verdict(contract: Contract, **overrides: object) -> DiagnoseResponse:
    base: dict[str, object] = {
        "incident_id": contract.incident_id,
        "failure_type": contract.failure_type,
        "outcome": Outcome.DIAGNOSED,
        "evidence_class": "crashloop_with_fatal_log",
        "root_cause": FABRICATED,
        "explanation": FABRICATED,
        "proposed_action": FABRICATED,
        "competing_hypothesis": "",
        "evidence": [
            Citation(source="container", field="container.last_terminated.exit_code", value="1")
        ],
        "confidence_model": 0.9,
        "confidence_final": 0.8,
        "confidence_ceiling": 0.8,
        "approvable": True,
    }
    base.update(overrides)
    return DiagnoseResponse.model_validate(base)


def _notice(contract: Contract, verdict: DiagnoseResponse, mode: str = "live") -> Notice:
    return Notice(
        contract=contract,
        verdict=verdict,
        mode="live" if mode == "live" else "shadow",
        deadline=datetime(2026, 9, 6, 14, 30, tzinfo=UTC),
        public_url="http://brain.test",
    )


def test_observed_block_carries_only_collected_text(crashloop: Contract) -> None:
    observed = "\n".join(render_observed(crashloop))
    assert "connection refused" in observed
    assert "could not initialise connection pool" in observed
    assert crashloop.pod.name in observed
    assert "exit" in observed.lower()
    assert FABRICATED not in observed
    # The observed renderer does not even receive the verdict.
    assert "root cause" not in observed


def test_observed_distinguishes_unavailable_from_empty_logs(
    imagepull: Contract, oom_init: Contract
) -> None:
    assert "unavailable" in "\n".join(render_observed(imagepull))
    assert "empty" in "\n".join(render_observed(oom_init))


def test_inferred_block_is_the_model_output(crashloop: Contract) -> None:
    inferred = "\n".join(render_inferred(_verdict(crashloop)))
    assert FABRICATED in inferred
    assert "0.80" in inferred
    assert "container.last_terminated.exit_code" in inferred


def test_live_approvable_offers_approval(crashloop: Contract) -> None:
    notice = _notice(crashloop, _verdict(crashloop), "live")
    assert notice.offers_approval
    decision = "\n".join(render_decision(notice))
    assert '"decision": "approved"' in decision
    assert '"decision": "rejected", "reason"' in decision
    assert '"decision": "edited"' in decision
    assert "14:30:00Z" in decision


def test_below_threshold_has_no_approve_affordance_at_all(crashloop: Contract) -> None:
    """Section 4.2 control 4: absent, not disabled."""
    weak = _verdict(crashloop, confidence_final=0.4, approvable=False)
    notice = _notice(crashloop, weak, "live")
    assert not notice.offers_approval
    text = render_text(notice)
    assert "approved" not in text
    assert "not approvable" in text
    assert "0.40" in text


def test_shadow_mode_rates_and_never_approves(crashloop: Contract) -> None:
    """Section 5.5: a label without the action."""
    notice = _notice(crashloop, _verdict(crashloop), "shadow")
    assert not notice.offers_approval
    text = render_text(notice)
    assert "would_approve" in text
    assert '"decision": "approved"' not in text
    assert "shadow mode" in text


def test_abstention_asks_for_the_actual_cause(crashloop: Contract) -> None:
    abstained = _verdict(
        crashloop,
        outcome=Outcome.INSUFFICIENT_CONTEXT,
        evidence_class="crashloop_logs_unavailable",
        root_cause="",
        explanation="",
        proposed_action="",
        evidence=[],
        confidence_final=None,
        abstained=True,
        abstain_reason="No causal signal is present.",
        approvable=False,
    )
    notice = _notice(crashloop, abstained, "live")
    text = render_text(notice)
    assert "actual_cause" in text
    assert "No causal signal is present." in text
    assert '"decision": "approved"' not in text
    assert FABRICATED not in text


def test_stdout_sink_writes_the_whole_message(imagepull: Contract) -> None:
    out = io.StringIO()
    notice = _notice(
        imagepull, _verdict(imagepull, evidence_class="image_pull_with_registry_error")
    )
    StdoutSink(out).deliver(notice)
    text = out.getvalue()
    assert text.index("OBSERVED") < text.index("INFERRED") < text.index("DECISION")
    assert "403 Forbidden" in text
    assert imagepull.incident_id in text
