from __future__ import annotations

from coroner_brain.ledger import Ledger, LedgerEntry


def _entry(**overrides: object) -> LedgerEntry:
    base: dict[str, object] = {
        "incident_id": "inc-1",
        "failure_type": "CrashLoopBackOff",
        "contract_version": "1",
        "context_hash": "abc123",
        "evidence_class": "crashloop_with_fatal_log",
        "model_id": "test-model",
        "prompt_version": "1",
        "outcome": "DIAGNOSED",
        "abstained": False,
    }
    base.update(overrides)
    return LedgerEntry(**base)  # type: ignore[arg-type]


def test_records_and_reads_back(ledger: Ledger) -> None:
    ledger.record(_entry(root_cause="db unreachable", confidence_final=0.8))
    row = ledger.get("inc-1")
    assert row is not None
    assert row["root_cause"] == "db unreachable"
    assert row["confidence_final"] == 0.8
    assert row["recorded_at"]


def test_rating_and_approval_are_separate_columns(ledger: Ledger) -> None:
    """Section 5.5: a hypothetical judgement and a real approval never merge."""
    ledger.record(_entry())
    ledger.label("inc-1", shadow_rating="would_approve")
    row = ledger.get("inc-1")
    assert row is not None
    assert row["shadow_rating"] == "would_approve"
    assert row["decision"] is None, "a shadow rating must not populate the approval column"

    ledger.label("inc-1", decision="approved", decision_reason="looks right")
    row = ledger.get("inc-1")
    assert row is not None
    assert row["decision"] == "approved"
    assert row["shadow_rating"] == "would_approve", "the rating must survive a later approval"


def test_actual_cause_supports_the_abstention_metric(ledger: Ledger) -> None:
    """Section 5.3: abstention correctness is labelled from outside."""
    ledger.record(_entry(outcome="INSUFFICIENT_CONTEXT", abstained=True))
    ledger.label("inc-1", actual_cause="the database password had rotated")
    row = ledger.get("inc-1")
    assert row is not None
    assert row["actual_cause"] == "the database password had rotated"
    assert row["abstained"] == 1


def test_resolution_is_recorded_separately_from_approval(ledger: Ledger) -> None:
    """Approval measures persuasiveness; resolution measures correctness."""
    ledger.record(_entry())
    ledger.label("inc-1", decision="approved", resolved_within_sla=False)
    row = ledger.get("inc-1")
    assert row is not None
    assert row["decision"] == "approved"
    assert row["resolved_within_sla"] == 0


def test_survives_reopening(tmp_path_factory: object, ledger: Ledger) -> None:
    ledger.record(_entry())
    reopened = Ledger(ledger.path)
    assert reopened.count() == 1
    assert reopened.get("inc-1") is not None
