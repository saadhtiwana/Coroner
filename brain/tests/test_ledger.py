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


def test_labels_are_written_once(ledger: Ledger) -> None:
    """Section 5.4: no record is mutated after its label is written."""
    import pytest

    from coroner_brain.ledger import AlreadyLabelledError, UnknownIncidentError

    ledger.record(_entry())
    ledger.label("inc-1", decision="rejected", decision_reason="wrong service")
    with pytest.raises(AlreadyLabelledError):
        ledger.label("inc-1", decision="approved")
    row = ledger.get("inc-1")
    assert row is not None
    assert row["decision"] == "rejected"

    ledger.label("inc-1", shadow_rating="unsure")
    with pytest.raises(AlreadyLabelledError):
        ledger.label("inc-1", shadow_rating="would_approve")

    with pytest.raises(UnknownIncidentError):
        ledger.label("inc-does-not-exist", decision="approved")


def test_edit_records_the_corrected_action_and_the_token(ledger: Ledger) -> None:
    ledger.record(_entry(proposed_action="raise the limit to 64Mi"))
    ledger.label(
        "inc-1",
        decision="edited",
        decision_action="raise the limit to 256Mi",
        approval_token="tok",
    )
    row = ledger.get("inc-1")
    assert row is not None
    assert row["decision"] == "edited"
    assert row["decision_action"] == "raise the limit to 256Mi"
    assert row["proposed_action"] == "raise the limit to 64Mi", "the proposal is kept as proposed"
    assert row["approval_token"] == "tok"


def test_a_version_one_ledger_is_migrated_in_place(tmp_path: object) -> None:
    """A file written before the decision columns existed keeps its rows."""
    import sqlite3
    from pathlib import Path

    from coroner_brain.ledger import SCHEMA_VERSION

    assert isinstance(tmp_path, Path)
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_meta (version INTEGER NOT NULL);
        INSERT INTO schema_meta (version) VALUES (1);
        CREATE TABLE diagnoses (
            incident_id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL,
            failure_type TEXT NOT NULL, contract_version TEXT NOT NULL,
            context_hash TEXT NOT NULL, evidence_class TEXT NOT NULL,
            model_id TEXT NOT NULL, prompt_version TEXT NOT NULL, outcome TEXT NOT NULL,
            abstained INTEGER NOT NULL, abstain_reason TEXT NOT NULL DEFAULT '',
            root_cause TEXT NOT NULL DEFAULT '', explanation TEXT NOT NULL DEFAULT '',
            proposed_action TEXT NOT NULL DEFAULT '',
            competing_hypothesis TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '[]', confidence_model REAL,
            confidence_final REAL, validation_failures TEXT NOT NULL DEFAULT '[]',
            validation_retries INTEGER NOT NULL DEFAULT 0,
            decision TEXT, decision_at TEXT, decision_reason TEXT, shadow_rating TEXT,
            actual_cause TEXT, resolved_within_sla INTEGER
        );
        INSERT INTO diagnoses (incident_id, recorded_at, failure_type, contract_version,
            context_hash, evidence_class, model_id, prompt_version, outcome, abstained)
        VALUES ('inc-old', 't', 'OOMKilled', '1', 'h', 'oom_with_limits', 'm', '1', 'DIAGNOSED', 0);
        """
    )
    conn.commit()
    conn.close()

    ledger = Ledger(path)
    assert ledger.schema_version() == SCHEMA_VERSION
    row = ledger.get("inc-old")
    assert row is not None
    assert row["decision_action"] is None
    ledger.label("inc-old", decision="approved", approval_token="tok")
    row = ledger.get("inc-old")
    assert row is not None
    assert row["approval_token"] == "tok"
    # Reopening does not re-run the migration or fail on existing columns.
    assert Ledger(path).schema_version() == SCHEMA_VERSION
