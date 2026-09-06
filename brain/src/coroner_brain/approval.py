"""The approval half of the state machine.

The diagnosis graph ends at a verdict. When that verdict offers approval, the
incident is parked in the in-flight store with a deadline and the graph is
resumed later by a human decision: approve, reject, or edit, or by the clock
with expire. Resumption loads the parked state, checks the decision against
it, writes the label to the ledger, and mints the approval token.

The token is what the agent verifies before executing anything. It is an
HMAC over the incident id, the context hash of the evidence the diagnosis
rested on, the decision, the exact action text, and the decision time, keyed
with a secret the agent shares. A token for one diagnosis cannot authorise
another, and an edited action produces a different token from the proposal
it replaced. Section 1: no mutation without a recorded approval keyed to a
specific diagnosis.

Resumption is by state held in the store rather than by a checkpointer.
The state is a few hundred bytes of JSON the ledger already understands; a
checkpoint would be an opaque blob beside it. See docs/DESIGN.md 6.13.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from coroner_brain.contract import Contract
from coroner_brain.inflight import InFlightStore
from coroner_brain.ledger import AlreadyLabelledError, Ledger, UnknownIncidentError
from coroner_brain.sink import Mode
from coroner_brain.verdict import DiagnoseResponse

log = logging.getLogger("coroner.brain.approval")

Decision = Literal["approved", "rejected", "edited", "expired"]
HUMAN_DECISIONS: frozenset[str] = frozenset({"approved", "rejected", "edited"})
Rating = Literal["would_approve", "would_reject", "unsure"]

TOKEN_VERSION = "v1"


class ApprovalError(Exception):
    """A decision that cannot be applied, with the HTTP status it deserves."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Pending:
    incident_id: str
    failure_type: str
    context_hash: str
    proposed_action: str
    confidence_final: float
    created_at: datetime
    deadline: datetime

    def to_record(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "failure_type": self.failure_type,
            "context_hash": self.context_hash,
            "proposed_action": self.proposed_action,
            "confidence_final": self.confidence_final,
            "created_at": self.created_at.isoformat(),
            "deadline": self.deadline.isoformat(),
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Pending:
        return cls(
            incident_id=str(record["incident_id"]),
            failure_type=str(record["failure_type"]),
            context_hash=str(record["context_hash"]),
            proposed_action=str(record["proposed_action"]),
            confidence_final=float(record["confidence_final"]),
            created_at=datetime.fromisoformat(str(record["created_at"])),
            deadline=datetime.fromisoformat(str(record["deadline"])),
        )


@dataclass(frozen=True)
class Resolution:
    incident_id: str
    decision: str
    decided_at: datetime
    action: str
    approval_token: str


class DecisionState(TypedDict, total=False):
    incident_id: str
    decision: str
    reason: str
    action: str
    pending: dict[str, Any]
    error_status: int
    error_detail: str
    decided_at: str
    resolved_action: str
    approval_token: str


def sign(
    secret: bytes,
    *,
    incident_id: str,
    context_hash: str,
    decision: str,
    action: str,
    decided_at: str,
) -> str:
    """The approval token. Every field that would change what is executed is in it."""
    message = "|".join([TOKEN_VERSION, incident_id, context_hash, decision, action, decided_at])
    digest = hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()
    return f"{TOKEN_VERSION}.{digest}"


def verify(
    secret: bytes,
    token: str,
    *,
    incident_id: str,
    context_hash: str,
    decision: str,
    action: str,
    decided_at: str,
) -> bool:
    expected = sign(
        secret,
        incident_id=incident_id,
        context_hash=context_hash,
        decision=decision,
        action=action,
        decided_at=decided_at,
    )
    return hmac.compare_digest(expected, token)


class ApprovalPipeline:
    """Parks approvable verdicts and resumes them with a decision."""

    def __init__(
        self,
        *,
        store: InFlightStore,
        ledger: Ledger,
        secret: bytes,
        ttl_seconds: int,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._secret = secret
        self._ttl = ttl_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._graph = self._build()

    # ---------------------------------------------------------------- park

    def register(self, contract: Contract, verdict: DiagnoseResponse, mode: Mode) -> Pending | None:
        """Park a verdict that offers approval. None when nothing can be approved."""
        if mode != "live" or not verdict.approvable or verdict.confidence_final is None:
            return None
        if contract.incident_id != verdict.incident_id:
            raise ApprovalError(500, "verdict and contract name different incidents")
        now = self._now()
        pending = Pending(
            incident_id=verdict.incident_id,
            failure_type=verdict.failure_type,
            context_hash=verdict.context_hash,
            proposed_action=verdict.proposed_action,
            confidence_final=verdict.confidence_final,
            created_at=now,
            deadline=now + timedelta(seconds=self._ttl),
        )
        # Kept past the deadline so the sweeper can record the expiry.
        self._store.put(pending.incident_id, pending.to_record(), self._ttl * 2 + 60)
        return pending

    def pending(self) -> list[Pending]:
        out = []
        for incident_id in self._store.ids():
            record = self._store.get(incident_id)
            if record is not None:
                out.append(Pending.from_record(record))
        return out

    # -------------------------------------------------------------- resume

    def decide(
        self, incident_id: str, decision: str, *, reason: str = "", action: str = ""
    ) -> Resolution:
        """Resume the parked incident with a decision."""
        final: DecisionState = self._graph.invoke(
            {"incident_id": incident_id, "decision": decision, "reason": reason, "action": action}
        )
        if final.get("error_status"):
            raise ApprovalError(final["error_status"], final.get("error_detail", ""))
        return Resolution(
            incident_id=incident_id,
            decision=final["decision"],
            decided_at=datetime.fromisoformat(final["decided_at"]),
            action=final.get("resolved_action", ""),
            approval_token=final.get("approval_token", ""),
        )

    def expire_overdue(self) -> list[str]:
        """The clock's decision: records expired for every pending incident past its deadline."""
        now = self._now()
        expired: list[str] = []
        for pending in self.pending():
            if now < pending.deadline:
                continue
            try:
                self.decide(pending.incident_id, "expired")
            except ApprovalError as exc:
                log.warning("could not expire %s: %s", pending.incident_id, exc.detail)
                self._store.delete(pending.incident_id)
                continue
            expired.append(pending.incident_id)
        return expired

    # --------------------------------------------------------------- nodes

    def load(self, state: DecisionState) -> DecisionState:
        record = self._store.get(state["incident_id"])
        if record is None:
            row = self._ledger.get(state["incident_id"])
            if row is None:
                return {"error_status": 404, "error_detail": "no such incident"}
            if row.get("decision"):
                return {
                    "error_status": 409,
                    "error_detail": f"already decided: {row['decision']}",
                }
            return {
                "error_status": 404,
                "error_detail": "no approval is pending for this incident",
            }
        return {"pending": record}

    def check(self, state: DecisionState) -> DecisionState:
        decision = state.get("decision", "")
        pending = Pending.from_record(state["pending"])
        now = self._now()

        if decision == "expired":
            if now < pending.deadline:
                return {"error_status": 409, "error_detail": "the deadline has not passed"}
            return {"decided_at": now.isoformat(), "resolved_action": ""}

        if decision not in HUMAN_DECISIONS:
            return {"error_status": 422, "error_detail": f"unknown decision {decision!r}"}
        if now >= pending.deadline:
            # The clock decided first. Record that, then refuse this one.
            self._record(pending, "expired", reason="", action="", decided_at=now.isoformat())
            return {"error_status": 409, "error_detail": "the approval window has expired"}
        if decision == "rejected" and not state.get("reason", "").strip():
            return {
                "error_status": 422,
                "error_detail": "a rejection needs a one-line reason; it is the most valuable "
                "label there is",
            }
        if decision == "edited" and not state.get("action", "").strip():
            return {"error_status": 422, "error_detail": "an edit needs the corrected action"}

        if decision == "approved":
            action = pending.proposed_action
        elif decision == "edited":
            action = state.get("action", "").strip()
        else:
            action = ""
        return {"decided_at": now.isoformat(), "resolved_action": action}

    def record(self, state: DecisionState) -> DecisionState:
        pending = Pending.from_record(state["pending"])
        token = self._record(
            pending,
            state["decision"],
            reason=state.get("reason", "").strip(),
            action=state.get("resolved_action", ""),
            decided_at=state["decided_at"],
        )
        return {"approval_token": token}

    def _record(
        self, pending: Pending, decision: str, *, reason: str, action: str, decided_at: str
    ) -> str:
        token = ""
        if decision in ("approved", "edited"):
            token = sign(
                self._secret,
                incident_id=pending.incident_id,
                context_hash=pending.context_hash,
                decision=decision,
                action=action,
                decided_at=decided_at,
            )
        try:
            self._ledger.label(
                pending.incident_id,
                decision=decision,
                decision_reason=reason or None,
                decision_action=action or None,
                approval_token=token or None,
            )
        except AlreadyLabelledError as exc:
            self._store.delete(pending.incident_id)
            raise ApprovalError(409, f"already decided: {exc.existing}") from exc
        except UnknownIncidentError as exc:
            self._store.delete(pending.incident_id)
            raise ApprovalError(404, "no such incident") from exc
        self._store.delete(pending.incident_id)
        return token

    # --------------------------------------------------------------- graph

    @staticmethod
    def _proceed(state: DecisionState) -> str:
        return "fail" if state.get("error_status") else "ok"

    def _build(self) -> Any:  # noqa: ANN401 - langgraph's compiled graph type is internal
        graph = StateGraph(DecisionState)
        graph.add_node("load", self.load)
        graph.add_node("check", self.check)
        graph.add_node("record", self.record)
        graph.add_edge(START, "load")
        graph.add_conditional_edges("load", self._proceed, {"ok": "check", "fail": END})
        graph.add_conditional_edges("check", self._proceed, {"ok": "record", "fail": END})
        graph.add_edge("record", END)
        return graph.compile()
