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
import queue
import re
import threading
import time
from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from coroner_brain import prompts
from coroner_brain.contract import Contract
from coroner_brain.diagnosis import Diagnosis, Outcome
from coroner_brain.evidence import EvidenceClass, ceiling, classify_evidence, gate
from coroner_brain.ledger import Ledger, LedgerEntry
from coroner_brain.llm import LLMClient, Usage
from coroner_brain.schema import strict_schema
from coroner_brain.tracing import annotate, span
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
    started_at: float
    discard_reason: str
    prompt_tokens: int
    completion_tokens: int


def _context_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


class ModelUnavailableError(RuntimeError):
    """The model did not answer in time, or the call failed."""


# The provider's 429 body says how long to wait, for example "Please try
# again in 2.219999999s" or "in 1m3.5s". Measured 2026-09-06: the on-demand
# tier allows 8000 tokens a minute and one contract prompt is about 5000, so
# a second call inside the same minute is refused with a wait of seconds.
_RETRY_AFTER = re.compile(
    r"(?:try again in|retry[- ]after)\s+(?:(\d+)m)?(\d+(?:\.\d+)?)\s*s", re.IGNORECASE
)

# Never wait on a retry-after longer than this even if the deadline allows,
# so a provider asking for minutes does not hold a diagnosis hostage.
MAX_RETRY_AFTER = 90.0


def retry_after_seconds(error: BaseException) -> float | None:
    """Seconds the provider asked us to wait, from a rate limit error, or None."""
    text = str(error)
    if "rate limit" not in text.lower() and "429" not in text:
        return None
    match = _RETRY_AFTER.search(text)
    if match is None:
        return 5.0
    minutes = int(match.group(1) or 0)
    return minutes * 60 + float(match.group(2))


class DiagnosisPipeline:
    """Builds and runs the graph."""

    def __init__(
        self,
        *,
        client: LLMClient,
        ledger: Ledger,
        abstention_threshold: float = 0.5,
        max_retries: int = 1,
        model_deadline: float = 180.0,
        clock: Callable[[], float] = time.monotonic,
        price_input_per_m: float = 0.0,
        price_output_per_m: float = 0.0,
    ) -> None:
        self._client = client
        self._ledger = ledger
        self._threshold = abstention_threshold
        self._max_retries = max_retries
        self._deadline = model_deadline
        self._clock = clock
        self._sleep: Callable[[float], None] = time.sleep
        self._price_in = price_input_per_m
        self._price_out = price_output_per_m
        self._schema = strict_schema(Diagnosis)
        self._graph = self._build()

    # ---------------------------------------------------------------- nodes

    def classify(self, state: State) -> State:
        contract = state["contract"]
        with span("graph.classify", incident_id=contract.incident_id) as current:
            payload = contract.model_dump(mode="json")
            evidence_class = classify_evidence(contract).value
            current.set_attribute("evidence_class", evidence_class)
            current.set_attribute("failure_type", contract.failure_type)
            current.set_attribute("logs_available", contract.logs.available)
            current.set_attribute("logs_empty", contract.logs.empty)
            return {
                "payload": payload,
                "context_hash": _context_hash(payload),
                "evidence_class": evidence_class,
                "attempts": 0,
                "validation_failures": [],
                "started_at": self._clock(),
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }

    def evidence_gate(self, state: State) -> State:
        """Deterministic. Runs before any model call and may end the run."""
        contract = state["contract"]
        evidence_class = EvidenceClass(state["evidence_class"])
        with span(
            "graph.evidence_gate",
            evidence_class=evidence_class.value,
            ceiling=ceiling(evidence_class),
        ) as current:
            abstain, reason = gate(contract, evidence_class)
            current.set_attribute("abstained_before_model", abstain)
            if abstain:
                current.set_attribute("abstain_reason", reason)
                return {"outcome": Outcome.INSUFFICIENT_CONTEXT.value, "abstain_reason": reason}
            return {"outcome": ""}

    def diagnose(self, state: State) -> State:
        attempts = state.get("attempts", 0) + 1
        with span("graph.diagnose", attempt=attempts, retry=attempts > 1) as current:
            result = self._diagnose(state, attempts)
            current.set_attribute("outcome", result.get("outcome", ""))
            confidence = result.get("confidence_model")
            if confidence is not None:
                current.set_attribute("confidence_model", confidence)
            reason = result.get("discard_reason")
            if reason:
                current.set_attribute("discard_reason", reason)
            return result

    def _diagnose(self, state: State, attempts: int) -> State:
        contract = state["contract"]
        payload = state["payload"]

        remaining = self._deadline - (self._clock() - state.get("started_at", self._clock()))
        if remaining <= 0:
            return {
                "attempts": attempts,
                "outcome": Outcome.DISCARDED.value,
                "discard_reason": (
                    f"the model deadline of {self._deadline:.0f}s was already spent before "
                    f"attempt {attempts}"
                ),
            }

        try:
            raw = self._call_with_deadline(
                remaining,
                system=prompts.system_prompt(contract.failure_type),
                user=prompts.user_prompt(
                    prompts.render_contract(payload), state.get("validation_failures") or None
                ),
            )
        except ModelUnavailableError as exc:
            # Neither a diagnosis nor an abstention: nothing was reasoned.
            # Recorded as its own outcome so the gap is visible and the
            # incident is excluded from accuracy rather than counted as
            # anything.
            return {
                "attempts": attempts,
                "outcome": Outcome.DISCARDED.value,
                "discard_reason": str(exc),
                **self._spent(state),
            }

        spent = self._spent(state)
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
                **spent,
            }

        return {
            "diagnosis": parsed,
            "attempts": attempts,
            "confidence_model": parsed.confidence,
            **spent,
        }

    def _spent(self, state: State) -> State:
        """Tokens so far plus the client's last call, summed over retries."""
        usage = getattr(self._client, "last_usage", None) or Usage()
        return {
            "prompt_tokens": state.get("prompt_tokens", 0) + usage.prompt_tokens,
            "completion_tokens": state.get("completion_tokens", 0) + usage.completion_tokens,
        }

    def cost_usd(self, prompt_tokens: int, completion_tokens: int) -> float:
        return round(
            prompt_tokens * self._price_in / 1_000_000
            + completion_tokens * self._price_out / 1_000_000,
            6,
        )

    def _call_with_deadline(self, remaining: float, *, system: str, user: str) -> str:
        """Run the model call on a thread and give up at the deadline.

        The client has its own timeout and retry budget, but a socket that
        never answers, or a retry-after the client honours, can outlast them.
        The thread is a daemon so a call that is never answered cannot keep
        the process alive; the pipeline stops waiting for it here.
        """
        result: queue.Queue[tuple[str | None, BaseException | None]] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                with span("model.complete", model_id=self._client.model_id) as current:
                    text = self._client.complete_json(
                        system=system, user=user, schema=self._schema, schema_name="diagnosis"
                    )
                    usage = getattr(self._client, "last_usage", None)
                    if usage is not None:
                        current.set_attribute("prompt_tokens", usage.prompt_tokens)
                        current.set_attribute("completion_tokens", usage.completion_tokens)
                    result.put((text, None))
            except BaseException as exc:  # reported to the caller, not swallowed
                result.put((None, exc))

        started = self._clock()
        waited_for_limit = 0.0
        while True:
            budget = remaining - (self._clock() - started)
            if budget <= 0:
                raise ModelUnavailableError(
                    f"the model did not answer within the {self._deadline:.0f}s deadline"
                    + (
                        f" after waiting {waited_for_limit:.0f}s on rate limits"
                        if waited_for_limit
                        else ""
                    )
                )
            threading.Thread(target=run, name="coroner-model-call", daemon=True).start()
            try:
                raw, error = result.get(timeout=budget)
            except queue.Empty:
                raise ModelUnavailableError(
                    f"the model did not answer within the {self._deadline:.0f}s deadline"
                ) from None
            if error is None:
                return raw or ""

            # A rate limit with a short wait is not a failure; it is the
            # provider pacing us. Wait as asked, inside the deadline, and go
            # again. Anything else, or a wait that would blow the deadline,
            # is recorded as the discard it is.
            wait = retry_after_seconds(error)
            budget = remaining - (self._clock() - started)
            if wait is None or wait > MAX_RETRY_AFTER or wait + 1.0 > budget:
                raise ModelUnavailableError(
                    f"the model call failed: {type(error).__name__}: {error}"
                ) from error
            waited_for_limit += wait + 0.5
            self._sleep(wait + 0.5)

    def discarded(self, state: State) -> State:
        return {"outcome": Outcome.DISCARDED.value}

    def validate_node(self, state: State) -> State:
        diagnosis = state.get("diagnosis")
        with span("graph.validate", attempt=state.get("attempts", 0)) as current:
            if diagnosis is None:
                current.set_attribute("validation_ok", False)
                current.set_attribute(
                    "validation_failures", "; ".join(state.get("validation_failures") or [])
                )
                return {}

            report: ValidationReport = validate(diagnosis, state["payload"])
            current.set_attribute("validation_ok", report.ok)
            current.set_attribute("citations", len(diagnosis.evidence))
            if not report.ok:
                current.set_attribute("validation_failures", "; ".join(report.failures))
                return {"validation_failures": report.failures}

            # Section 4.2 control 3: the ceiling is deterministic and the model
            # may only lower its confidence, never raise it.
            cap = ceiling(EvidenceClass(state["evidence_class"]))
            final = min(diagnosis.confidence, cap)
            current.set_attribute("confidence_model", diagnosis.confidence)
            current.set_attribute("confidence_ceiling", cap)
            current.set_attribute("confidence_final", final)
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

    def _after_diagnose(self, state: State) -> str:
        return "discarded" if state.get("outcome") == Outcome.DISCARDED.value else "validate"

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
        graph.add_node("discarded", self.discarded)

        graph.add_edge(START, "classify")
        graph.add_edge("classify", "evidence_gate")
        graph.add_conditional_edges(
            "evidence_gate",
            self._after_gate,
            {"diagnose": "diagnose", "insufficient": "insufficient"},
        )
        graph.add_conditional_edges(
            "diagnose",
            self._after_diagnose,
            {"validate": "validate", "discarded": "discarded"},
        )
        graph.add_conditional_edges(
            "validate",
            self._after_validate,
            {"diagnose": "diagnose", "diagnosed": "diagnosed", "insufficient": "insufficient"},
        )
        graph.add_edge("diagnosed", END)
        graph.add_edge("insufficient", END)
        graph.add_edge("discarded", END)
        return graph.compile()

    # ------------------------------------------------------------------ run

    def run(self, contract: Contract) -> State:
        """Run the pipeline and record the result before returning it."""
        with span(
            "graph.run", incident_id=contract.incident_id, failure_type=contract.failure_type
        ):
            final: State = self._graph.invoke({"contract": contract})
            with span("ledger.record"):
                self._record(contract, final)
            annotate(
                outcome=final.get("outcome", ""),
                evidence_class=final.get("evidence_class", ""),
                confidence_final=final.get("confidence_final"),
                validation_retried=final.get("attempts", 0) > 1,
                attempts=final.get("attempts", 0),
                prompt_tokens=final.get("prompt_tokens", 0),
                completion_tokens=final.get("completion_tokens", 0),
            )
            return final

    def _record(self, contract: Contract, state: State) -> None:
        diagnosis = state.get("diagnosis")
        abstained = state.get("outcome") == Outcome.INSUFFICIENT_CONTEXT.value
        discarded = state.get("outcome") == Outcome.DISCARDED.value
        if discarded:
            diagnosis = None
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
            confidence_model=None if discarded else state.get("confidence_model"),
            confidence_final=None if abstained or discarded else state.get("confidence_final"),
            validation_failures=state.get("validation_failures") or [],
            validation_retries=max(0, state.get("attempts", 0) - 1),
            contract_json=json.dumps(state.get("payload") or {}, sort_keys=True, default=str),
            discard_reason=state.get("discard_reason", ""),
            prompt_tokens=state.get("prompt_tokens", 0),
            completion_tokens=state.get("completion_tokens", 0),
            cost_usd=self.cost_usd(
                state.get("prompt_tokens", 0), state.get("completion_tokens", 0)
            ),
            latency_ms_total=int((self._clock() - state.get("started_at", self._clock())) * 1000),
        )
        self._ledger.record(entry)
