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

SCHEMA_VERSION = 1

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
    resolved_within_sla INTEGER
);
"""


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
        shadow_rating: str | None = None,
        actual_cause: str | None = None,
        resolved_within_sla: bool | None = None,
    ) -> None:
        """Write back the human's judgement.

        decision and shadow_rating are separate columns and never merged. A
        rating is hypothetical and carries no risk; an approval does. The gap
        between them is a measurement worth keeping. See docs/DESIGN.md 5.5.
        """
        updates: dict[str, Any] = {"decision_at": datetime.now(UTC).isoformat()}
        if decision is not None:
            updates["decision"] = decision
        if decision_reason is not None:
            updates["decision_reason"] = decision_reason
        if shadow_rating is not None:
            updates["shadow_rating"] = shadow_rating
        if actual_cause is not None:
            updates["actual_cause"] = actual_cause
        if resolved_within_sla is not None:
            updates["resolved_within_sla"] = int(resolved_within_sla)

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
