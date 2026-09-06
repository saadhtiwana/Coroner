"""Tracing, and specifically that a trace explains a decision.

Timing alone would not be worth the dependency. These tests assert that the
facts that decided an outcome, the evidence class, the ceiling, the final
confidence, and whether validation retried, are on the spans.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from coroner_brain import tracing
from coroner_brain.contract import Contract
from coroner_brain.graph import DiagnosisPipeline
from coroner_brain.ledger import Ledger
from coroner_brain.llm import ScriptedClient, Usage


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    tracing.configure(exporter=exporter, force=True)
    try:
        yield exporter
    finally:
        exporter.clear()
        tracing.configure(mode="off", force=True)


def _by_name(exporter: InMemorySpanExporter) -> dict[str, dict[str, Any]]:
    """Every finished span's attributes, by span name."""
    return {s.name: dict(s.attributes or {}) for s in exporter.get_finished_spans()}


def _answer(**overrides: object) -> str:
    base: dict[str, object] = {
        "root_cause": "The container exceeded its memory limit.",
        "explanation": "The runtime reported the kill.",
        "proposed_action": "Raise the limit on the owning workload.",
        "confidence": 0.95,
        "evidence": [
            {
                "source": "container",
                "field": "container.last_terminated.reason",
                "value": "OOMKilled",
            }
        ],
        "competing_hypothesis": "",
    }
    base.update(overrides)
    return json.dumps(base)


def test_a_diagnosis_trace_carries_the_facts_that_decided_it(
    oomkilled: Contract, ledger: Ledger, spans: InMemorySpanExporter
) -> None:
    client = ScriptedClient([_answer()], usage=Usage(prompt_tokens=4000, completion_tokens=250))
    DiagnosisPipeline(client=client, ledger=ledger).run(oomkilled)

    named = _by_name(spans)
    assert {
        "graph.classify",
        "graph.evidence_gate",
        "graph.diagnose",
        "graph.validate",
        "graph.run",
    } <= set(named)

    classify = named["graph.classify"]
    assert classify["evidence_class"] == "oom_with_limits"
    assert classify["failure_type"] == "OOMKilled"

    gate = named["graph.evidence_gate"]
    assert gate["abstained_before_model"] is False
    assert gate["ceiling"] == 0.90

    model = named["model.complete"]
    assert model["prompt_tokens"] == 4000
    assert model["completion_tokens"] == 250

    # The model call runs on a worker thread. Its span must still be part of
    # this trace: an orphaned span times the call and explains nothing.
    finished = {s.name: s for s in spans.get_finished_spans()}
    model_span = finished["model.complete"]
    diagnose_span = finished["graph.diagnose"]
    assert model_span.context is not None
    assert model_span.context.trace_id == diagnose_span.context.trace_id
    assert model_span.parent is not None
    assert model_span.parent.span_id == diagnose_span.context.span_id

    validate = named["graph.validate"]
    assert validate["validation_ok"] is True
    assert validate["confidence_model"] == 0.95
    assert validate["confidence_ceiling"] == 0.90
    assert validate["confidence_final"] == 0.90

    run = named["graph.run"]
    assert run["outcome"] == "DIAGNOSED"
    assert run["evidence_class"] == "oom_with_limits"
    assert run["confidence_final"] == 0.90
    assert run["validation_retried"] is False
    assert "ledger.record" in named


def test_a_retry_is_visible_in_the_trace(
    oomkilled: Contract, ledger: Ledger, spans: InMemorySpanExporter
) -> None:
    bad = _answer(evidence=[{"source": "logs", "field": "logs.nope", "value": "x"}])
    client = ScriptedClient([bad, _answer()])
    DiagnosisPipeline(client=client, ledger=ledger).run(oomkilled)

    finished = spans.get_finished_spans()
    diagnoses = [s for s in finished if s.name == "graph.diagnose"]
    validations = [s for s in finished if s.name == "graph.validate"]
    assert len(diagnoses) == 2
    assert dict(diagnoses[0].attributes or {})["retry"] is False
    assert dict(diagnoses[1].attributes or {})["retry"] is True
    first_validation = dict(validations[0].attributes or {})
    assert first_validation["validation_ok"] is False
    assert "does not exist" in str(first_validation["validation_failures"])
    assert _by_name(spans)["graph.run"]["validation_retried"] is True


def test_an_abstention_at_the_gate_traces_no_model_call(
    crashloop: Contract, ledger: Ledger, spans: InMemorySpanExporter
) -> None:
    stripped = crashloop.model_copy(deep=True)
    stripped.logs.available = False
    stripped.logs.empty = True
    stripped.logs.content = ""
    DiagnosisPipeline(client=ScriptedClient([]), ledger=ledger).run(stripped)

    named = _by_name(spans)
    assert "model.complete" not in named, "the gate abstained; nothing should have been called"
    gate = named["graph.evidence_gate"]
    assert gate["abstained_before_model"] is True
    assert "no causal signal" in str(gate["abstain_reason"]).lower()
    assert named["graph.run"]["outcome"] == "INSUFFICIENT_CONTEXT"


def test_configure_modes(spans: InMemorySpanExporter) -> None:
    assert tracing.configure(mode="off", force=True) == "off"
    with tracing.span("ignored"):
        pass
    assert tracing.configure(mode="console", force=True) == "console"
    # Already configured, so a second call without force keeps the mode.
    assert tracing.configure(mode="otlp") == "console"
