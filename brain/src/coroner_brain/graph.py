"""The diagnosis state machine.

Nodes: classify, evidence_gate, diagnose, validate, and the two terminals
DIAGNOSED and INSUFFICIENT_CONTEXT.

Two properties are structural rather than advisory. evidence_gate runs before
any model call and can end the run without one, so a context already known to
be empty is never handed to a model. validate runs after generation in code, so
a diagnosis whose citations do not verify never reaches a human.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from coroner_brain import prompts
from coroner_brain.contract import Contract
from coroner_brain.diagnosis import Diagnosis, Outcome
from coroner_brain.evidence import EvidenceClass, ceiling, classify_evidence, gate
from coroner_brain.ledger import Ledger, LedgerEntry
from coroner_brain.llm import LLMClient
from coroner_brain.schema import strict_schema
from coroner_brain.validate import ValidationReport, validate


class State(TypedDict, total=False):
    contract: Contract
    payload: dict[str, Any]
    evidence_class: str
    outcome: str
    abstain_reason: str
    diagnosis: Diagnosis | None
    confidence_model: float | None
    confidence_final: float | None
    validation_failures: list[str]
    attempts: int
    context_hash: str


def _context_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


class DiagnosisPipeline:
    """Builds and runs the graph."""

    def __init__(
        self,
        *,
        client: LLMClient,
        ledger: Ledger,
        abstention_threshold: float = 0.5,
        max_retries: int = 1,
    ) -> None:
        self._client = client
        self._ledger = ledger
        self._threshold = abstention_threshold
        self._max_retries = max_retries
        self._schema = strict_schema(Diagnosis)
        self._graph = self._build()

    # ---------------------------------------------------------------- nodes

    def classify(self, state: State) -> State:
        contract = state["contract"]
        payload = contract.model_dump(mode="json")
        return {
            "payload": payload,
            "context_hash": _context_hash(payload),
            "evidence_class": classify_evidence(contract).value,
            "attempts": 0,
            "validation_failures": [],
        }

    def evidence_gate(self, state: State) -> State:
        """Deterministic. Runs before any model call and may end the run."""
        contract = state["contract"]
        evidence_class = EvidenceClass(state["evidence_class"])
        abstain, reason = gate(contract, evidence_class)
        if abstain:
            return {"outcome": Outcome.INSUFFICIENT_CONTEXT.value, "abstain_reason": reason}
        return {"outcome": ""}

    def diagnose(self, state: State) -> State:
        contract = state["contract"]
        payload = state["payload"]
        raw = self._client.complete_json(
            system=prompts.system_prompt(contract.failure_type),
            user=prompts.user_prompt(
                json.dumps(payload, indent=2), state.get("validation_failures") or None
            ),
            schema=self._schema,
            schema_name="diagnosis",
        )
        attempts = state.get("attempts", 0) + 1

        try:
            parsed = Diagnosis.model_validate_json(raw)
        except ValidationError as exc:
            # Malformed or schema-violating output is treated exactly like a
            # failed citation check: retry once, then abstain. A model that
            # returns bad JSON degrades to abstention, never to a wrong answer.
            return {
                "diagnosis": None,
                "attempts": attempts,
                "validation_failures": [
                    f"model output did not parse against the schema: {exc.error_count()} error(s)"
                ],
            }

        return {"diagnosis": parsed, "attempts": attempts, "confidence_model": parsed.confidence}

    def validate_node(self, state: State) -> State:
        diagnosis = state.get("diagnosis")
        if diagnosis is None:
            return {}

        report: ValidationReport = validate(diagnosis, state["payload"])
        if not report.ok:
            return {"validation_failures": report.failures}

        # Section 4.2 control 3: the ceiling is deterministic and the model may
        # only lower its confidence, never raise it.
        cap = ceiling(EvidenceClass(state["evidence_class"]))
        final = min(diagnosis.confidence, cap)
        return {"validation_failures": [], "confidence_final": final}

    def diagnosed(self, state: State) -> State:
        return {"outcome": Outcome.DIAGNOSED.value}

    def insufficient(self, state: State) -> State:
        if state.get("abstain_reason"):
            return {"outcome": Outcome.INSUFFICIENT_CONTEXT.value}
        failures = state.get("validation_failures") or []
        reason = (
            "The diagnosis could not be verified against the collected evidence after "
            f"{state.get('attempts', 0)} attempt(s): {'; '.join(failures)}"
            if failures
            else "The evidence does not determine a cause."
        )
        return {"outcome": Outcome.INSUFFICIENT_CONTEXT.value, "abstain_reason": reason}

    # ----------------------------------------------------------------- edges

    def _after_gate(self, state: State) -> str:
        return "insufficient" if state.get("outcome") else "diagnose"

    def _after_validate(self, state: State) -> str:
        if state.get("diagnosis") is None or state.get("validation_failures"):
            if state.get("attempts", 0) <= self._max_retries:
                return "diagnose"
            return "insufficient"
        if (state.get("confidence_final") or 0.0) < self._threshold:
            return "insufficient"
        return "diagnosed"

    def _build(self) -> Any:  # noqa: ANN401 - langgraph's compiled graph type is internal
        graph = StateGraph(State)
        graph.add_node("classify", self.classify)
        graph.add_node("evidence_gate", self.evidence_gate)
        graph.add_node("diagnose", self.diagnose)
        graph.add_node("validate", self.validate_node)
        graph.add_node("diagnosed", self.diagnosed)
        graph.add_node("insufficient", self.insufficient)

        graph.add_edge(START, "classify")
        graph.add_edge("classify", "evidence_gate")
        graph.add_conditional_edges(
            "evidence_gate",
            self._after_gate,
            {"diagnose": "diagnose", "insufficient": "insufficient"},
        )
        graph.add_edge("diagnose", "validate")
        graph.add_conditional_edges(
            "validate",
            self._after_validate,
            {"diagnose": "diagnose", "diagnosed": "diagnosed", "insufficient": "insufficient"},
        )
        graph.add_edge("diagnosed", END)
        graph.add_edge("insufficient", END)
        return graph.compile()

    # ------------------------------------------------------------------ run

    def run(self, contract: Contract) -> State:
        """Run the pipeline and record the result before returning it."""
        final: State = self._graph.invoke({"contract": contract})
        self._record(contract, final)
        return final

    def _record(self, contract: Contract, state: State) -> None:
        diagnosis = state.get("diagnosis")
        abstained = state.get("outcome") == Outcome.INSUFFICIENT_CONTEXT.value
        entry = LedgerEntry(
            incident_id=contract.incident_id,
            failure_type=contract.failure_type,
            contract_version=contract.contract_version,
            context_hash=state.get("context_hash", ""),
            evidence_class=state.get("evidence_class", ""),
            model_id=self._client.model_id,
            prompt_version=prompts.PROMPT_VERSION,
            outcome=state.get("outcome", ""),
            abstained=abstained,
            abstain_reason=state.get("abstain_reason", ""),
            root_cause="" if abstained or not diagnosis else diagnosis.root_cause,
            explanation="" if abstained or not diagnosis else diagnosis.explanation,
            proposed_action="" if abstained or not diagnosis else diagnosis.proposed_action,
            competing_hypothesis=""
            if abstained or not diagnosis
            else diagnosis.competing_hypothesis,
            evidence=[]
            if abstained or not diagnosis
            else [c.model_dump() for c in diagnosis.evidence],
            confidence_model=state.get("confidence_model"),
            confidence_final=None if abstained else state.get("confidence_final"),
            validation_failures=state.get("validation_failures") or [],
            validation_retries=max(0, state.get("attempts", 0) - 1),
            contract_json=json.dumps(state.get("payload") or {}, sort_keys=True, default=str),
        )
        self._ledger.record(entry)
