"""Pipeline tests.

Every model response is scripted, so these run offline and deterministically.
"""

from __future__ import annotations

import json

from coroner_brain.contract import Contract
from coroner_brain.diagnosis import Outcome
from coroner_brain.evidence import EvidenceClass, ceiling
from coroner_brain.graph import DiagnosisPipeline
from coroner_brain.ledger import Ledger
from coroner_brain.llm import ScriptedClient


def _answer(**overrides: object) -> str:
    base: dict[str, object] = {
        "root_cause": "placeholder",
        "explanation": "placeholder",
        "proposed_action": "placeholder",
        "confidence": 0.9,
        "evidence": [],
        "competing_hypothesis": "",
    }
    base.update(overrides)
    return json.dumps(base)


def _pipeline(client: ScriptedClient, ledger: Ledger, **kwargs: object) -> DiagnosisPipeline:
    return DiagnosisPipeline(client=client, ledger=ledger, **kwargs)  # type: ignore[arg-type]


def test_gate_abstains_without_spending_a_model_call(crashloop: Contract, ledger: Ledger) -> None:
    """Section 4.2 control 1, enforced structurally.

    A context already known to carry no causal signal must never reach the
    model. The proof is that the scripted client is never called.
    """
    stripped = crashloop.model_copy(deep=True)
    stripped.logs.available = False
    stripped.logs.empty = True
    stripped.logs.content = ""

    client = ScriptedClient([_answer(root_cause="should never be produced")])
    state = _pipeline(client, ledger).run(stripped)

    assert client.calls == [], "the model was called for a context known to be empty"
    assert state["outcome"] == Outcome.INSUFFICIENT_CONTEXT.value
    assert "no causal signal" in state["abstain_reason"].lower()

    row = ledger.get(stripped.incident_id)
    assert row is not None
    assert row["abstained"] == 1
    assert row["outcome"] == Outcome.INSUFFICIENT_CONTEXT.value


def test_truthful_diagnosis_is_accepted(imagepull: Contract, ledger: Ledger) -> None:
    payload = imagepull.model_dump(mode="json")
    client = ScriptedClient(
        [
            _answer(
                root_cause="The image cannot be pulled from the registry.",
                explanation="The kubelet reported a pull failure for the configured image.",
                proposed_action="Correct the image reference or attach a pull secret.",
                confidence=0.9,
                evidence=[
                    {
                        "source": "container",
                        "field": "container.image",
                        "value": payload["container"]["image"],
                    },
                    {
                        "source": "container",
                        "field": "container.waiting_reason",
                        "value": "ImagePullBackOff",
                    },
                ],
            )
        ]
    )
    state = _pipeline(client, ledger).run(imagepull)

    assert state["outcome"] == Outcome.DIAGNOSED.value
    assert len(client.calls) == 1
    assert not state["validation_failures"]


def test_fabricated_citation_is_retried_then_abstains(oomkilled: Contract, ledger: Ledger) -> None:
    """The diagnosis never reaches a human. One retry, then abstain."""
    fabricated = _answer(
        root_cause="The application leaked memory.",
        evidence=[{"source": "logs", "field": "logs.stack_trace", "value": "at com.example.Main"}],
    )
    client = ScriptedClient([fabricated, fabricated])
    state = _pipeline(client, ledger).run(oomkilled)

    assert len(client.calls) == 2, "expected exactly one retry"
    assert state["outcome"] == Outcome.INSUFFICIENT_CONTEXT.value
    assert state["validation_failures"]

    row = ledger.get(oomkilled.incident_id)
    assert row is not None
    assert row["root_cause"] == "", "a rejected diagnosis must not be recorded as a finding"
    assert json.loads(row["validation_failures"])


def test_retry_can_succeed(oomkilled: Contract, ledger: Ledger) -> None:
    payload = oomkilled.model_dump(mode="json")
    good = _answer(
        root_cause="The container exceeded its memory limit.",
        confidence=0.9,
        evidence=[
            {
                "source": "container",
                "field": "container.last_terminated.reason",
                "value": "OOMKilled",
            }
        ],
    )
    bad = _answer(evidence=[{"source": "logs", "field": "logs.nope", "value": "x"}])
    client = ScriptedClient([bad, good])
    state = _pipeline(client, ledger).run(oomkilled)

    assert len(client.calls) == 2
    assert state["outcome"] == Outcome.DIAGNOSED.value
    assert payload["container"]["last_terminated"]["reason"] == "OOMKilled"


def test_malformed_json_degrades_to_abstention(oomkilled: Contract, ledger: Ledger) -> None:
    """A model that returns unparseable output must never produce a diagnosis."""
    client = ScriptedClient(["{not json at all", "still {{ not json"])
    state = _pipeline(client, ledger).run(oomkilled)

    assert state["outcome"] == Outcome.INSUFFICIENT_CONTEXT.value
    assert state.get("diagnosis") is None
    assert any("did not parse" in f for f in state["validation_failures"])


def test_confidence_ceiling_lowers_but_never_raises(oomkilled: Contract, ledger: Ledger) -> None:
    """Section 4.2 control 3: final = min(model, ceiling)."""
    overconfident = _answer(
        root_cause="The container exceeded its memory limit.",
        confidence=1.0,
        evidence=[
            {
                "source": "container",
                "field": "container.last_terminated.reason",
                "value": "OOMKilled",
            }
        ],
    )
    state = _pipeline(ScriptedClient([overconfident]), ledger).run(oomkilled)

    cap = ceiling(EvidenceClass(state["evidence_class"]))
    assert state["confidence_model"] == 1.0
    assert state["confidence_final"] == cap
    assert state["confidence_final"] < state["confidence_model"]


def test_model_may_lower_its_own_confidence(oomkilled: Contract, ledger: Ledger) -> None:
    humble = _answer(
        root_cause="The container exceeded its memory limit.",
        confidence=0.55,
        evidence=[
            {
                "source": "container",
                "field": "container.last_terminated.reason",
                "value": "OOMKilled",
            }
        ],
    )
    state = _pipeline(ScriptedClient([humble]), ledger).run(oomkilled)
    assert state["confidence_final"] == 0.55


def test_ledger_is_written_before_output_for_every_outcome(
    imagepull: Contract, crashloop: Contract, ledger: Ledger
) -> None:
    payload = imagepull.model_dump(mode="json")
    good = _answer(
        confidence=0.9,
        evidence=[
            {
                "source": "container",
                "field": "container.image",
                "value": payload["container"]["image"],
            }
        ],
    )
    _pipeline(ScriptedClient([good]), ledger).run(imagepull)

    stripped = crashloop.model_copy(deep=True)
    stripped.logs.available = False
    stripped.logs.empty = True
    stripped.logs.content = ""
    _pipeline(ScriptedClient([]), ledger).run(stripped)

    assert ledger.count() == 2
    for incident in (imagepull.incident_id, stripped.incident_id):
        row = ledger.get(incident)
        assert row is not None
        assert row["model_id"]
        assert row["prompt_version"]
        assert row["context_hash"]
        assert row["evidence_class"]
