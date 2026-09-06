"""The accuracy ledger from docs/DESIGN.md section 5.

Append-only SQLite, written before any output is produced so a delivery failure
cannot lose the record. The human's decision is written back to the same row as
the ground-truth label.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3

# Columns added after version 1, applied with ALTER TABLE on an existing file
# so a ledger recorded under the old schema keeps its rows. Append-only
# applies to rows; the schema may grow.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        # The action as approved or edited. For an edit this is the human's
        # corrected text, which is what the agent must execute, not the
        # model's proposal.
        "ALTER TABLE diagnoses ADD COLUMN decision_action TEXT",
        # HMAC over the approval, keyed to this diagnosis. The agent will not
        # execute an action whose token it cannot verify. Section 1.
        "ALTER TABLE diagnoses ADD COLUMN approval_token TEXT",
    ),
    3: (
        # The evidence contract exactly as diagnosed, so the row is the
        # section 5.3 corpus entry: a real outcome paired with the evidence
        # held at the time. It also lets any sink re-render the message
        # from the row alone after a decision.
        "ALTER TABLE diagnoses ADD COLUMN contract_json TEXT NOT NULL DEFAULT ''",
    ),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS diagnoses (
    incident_id       TEXT PRIMARY KEY,
    recorded_at       TEXT NOT NULL,
    failure_type      TEXT NOT NULL,
    contract_version  TEXT NOT NULL,
    context_hash      TEXT NOT NULL,
    evidence_class    TEXT NOT NULL,
    model_id          TEXT NOT NULL,
    prompt_version    TEXT NOT NULL,
    outcome           TEXT NOT NULL,
    abstained         INTEGER NOT NULL,
    abstain_reason    TEXT NOT NULL DEFAULT '',
    root_cause        TEXT NOT NULL DEFAULT '',
    explanation       TEXT NOT NULL DEFAULT '',
    proposed_action   TEXT NOT NULL DEFAULT '',
    competing_hypothesis TEXT NOT NULL DEFAULT '',
    evidence_json     TEXT NOT NULL DEFAULT '[]',
    confidence_model  REAL,
    confidence_final  REAL,
    validation_failures TEXT NOT NULL DEFAULT '[]',
    validation_retries INTEGER NOT NULL DEFAULT 0,

    -- Ground truth, written back when a human responds. Section 5.2.
    decision          TEXT,
    decision_at       TEXT,
    decision_reason   TEXT,
    shadow_rating     TEXT,
    actual_cause      TEXT,
    resolved_within_sla INTEGER,

    -- Schema 2 and 3. Listed here for fresh files; added by migration to
    -- files created under an earlier schema.
    decision_action   TEXT,
    approval_token    TEXT,
    contract_json     TEXT NOT NULL DEFAULT ''
);
"""


class UnknownIncidentError(KeyError):
    """No ledger row exists for the incident."""


class AlreadyLabelledError(ValueError):
    """The label was already written and the ledger does not overwrite."""

    def __init__(self, incident_id: str, field: str, existing: str) -> None:
        super().__init__(f"{incident_id} already has {field}={existing!r}")
        self.incident_id = incident_id
        self.field = field
        self.existing = existing


@dataclass
class LedgerEntry:
    incident_id: str
    failure_type: str
    contract_version: str
    context_hash: str
    evidence_class: str
    model_id: str
    prompt_version: str
    outcome: str
    abstained: bool
    abstain_reason: str = ""
    root_cause: str = ""
    explanation: str = ""
    proposed_action: str = ""
    competing_hypothesis: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    confidence_model: float | None = None
    confidence_final: float | None = None
    validation_failures: list[str] = field(default_factory=list)
    validation_retries: int = 0
    contract_json: str = ""


class Ledger:
    """Append-only incident ledger.

    Not a metrics platform. It exists to answer one question, whether Coroner is
    right, and is deliberately a file that can be copied and inspected with
    standard tools. See docs/DESIGN.md 5.4 and 6.3.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT version FROM schema_meta").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))
                self._apply_migrations(conn, 1)
            else:
                self._apply_migrations(conn, int(row["version"]))

    @staticmethod
    def _apply_migrations(conn: sqlite3.Connection, current: int) -> None:
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(diagnoses)")}
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS.get(version, ()):
                column = statement.split("ADD COLUMN", 1)[1].split()[0]
                if column not in existing:
                    conn.execute(statement)
        if current != SCHEMA_VERSION:
            conn.execute("UPDATE schema_meta SET version = ?", (SCHEMA_VERSION,))

    def schema_version(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT version FROM schema_meta").fetchone()[0])

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record(self, entry: LedgerEntry) -> None:
        """Write a diagnosis. Called before any output is produced."""
        payload = asdict(entry)
        payload["recorded_at"] = datetime.now(UTC).isoformat()
        payload["abstained"] = int(entry.abstained)
        payload["evidence_json"] = json.dumps(entry.evidence)
        payload["validation_failures"] = json.dumps(entry.validation_failures)
        payload.pop("evidence")

        columns = ", ".join(payload)
        placeholders = ", ".join(f":{name}" for name in payload)
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO diagnoses ({columns}) VALUES ({placeholders})",
                payload,
            )

    def label(
        self,
        incident_id: str,
        *,
        decision: str | None = None,
        decision_reason: str | None = None,
        decision_action: str | None = None,
        approval_token: str | None = None,
        shadow_rating: str | None = None,
        actual_cause: str | None = None,
        resolved_within_sla: bool | None = None,
    ) -> None:
        """Write back the human's judgement.

        decision and shadow_rating are separate columns and never merged. A
        rating is hypothetical and carries no risk; an approval does. The gap
        between them is a measurement worth keeping. See docs/DESIGN.md 5.5.

        Each label is written once. Section 5.4: no record is mutated after
        its label is written, so a second decision or a second rating for the
        same incident is refused rather than overwriting the first.
        """
        current = self.get(incident_id)
        if current is None:
            raise UnknownIncidentError(incident_id)
        if decision is not None and current.get("decision"):
            raise AlreadyLabelledError(incident_id, "decision", str(current["decision"]))
        if shadow_rating is not None and current.get("shadow_rating"):
            raise AlreadyLabelledError(incident_id, "shadow_rating", str(current["shadow_rating"]))
        if actual_cause is not None and current.get("actual_cause"):
            raise AlreadyLabelledError(incident_id, "actual_cause", str(current["actual_cause"]))

        updates: dict[str, Any] = {}
        if decision is not None:
            updates["decision"] = decision
            updates["decision_at"] = datetime.now(UTC).isoformat()
        if decision_reason is not None:
            updates["decision_reason"] = decision_reason
        if decision_action is not None:
            updates["decision_action"] = decision_action
        if approval_token is not None:
            updates["approval_token"] = approval_token
        if shadow_rating is not None:
            updates["shadow_rating"] = shadow_rating
        if actual_cause is not None:
            updates["actual_cause"] = actual_cause
        if resolved_within_sla is not None:
            updates["resolved_within_sla"] = int(resolved_within_sla)
        if not updates:
            return

        assignments = ", ".join(f"{name} = :{name}" for name in updates)
        updates["incident_id"] = incident_id
        with self._connect() as conn:
            conn.execute(
                f"UPDATE diagnoses SET {assignments} WHERE incident_id = :incident_id",
                updates,
            )

    def get(self, incident_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM diagnoses WHERE incident_id = ?", (incident_id,)
            ).fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[0])
