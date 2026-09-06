"""Pipeline tests.

Every model response is scripted, so these run offline and deterministically.
"""

from __future__ import annotations

import json

import pytest

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


def test_gate_abstains_on_an_organically_thin_incident(
    starterror_procready: Contract, ledger: Ledger
) -> None:
    """The first abstention not constructed by hand.

    Every earlier abstention test strips the logs from a contract that had
    them. This contract arrived from the cluster already empty: exit 128,
    reason StartError, a termination message that names nothing, and a log
    stream that exists but holds no bytes. The gate must abstain without
    spending a model call, and the reason must say which fact was missing.
    """
    assert starterror_procready.failure_type == "CrashLoopBackOff"
    assert starterror_procready.logs.available
    assert starterror_procready.logs.empty

    client = ScriptedClient([_answer(root_cause="should never be produced")])
    state = _pipeline(client, ledger).run(starterror_procready)

    assert client.calls == []
    assert state["outcome"] == Outcome.INSUFFICIENT_CONTEXT.value
    assert "produced no log output" in state["abstain_reason"]
    assert "128" in state["abstain_reason"]

    row = ledger.get(starterror_procready.incident_id)
    assert row is not None
    assert row["abstained"] == 1
    assert row["evidence_class"] == "crashloop_logs_unavailable"


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


# ------------------------------------------------------------- the deadline


class HangingClient:
    """Never answers. Stands in for a socket that is never closed."""

    model_id = "hanging"

    def __init__(self) -> None:
        import threading

        self.release = threading.Event()
        self.calls = 0

    def complete_json(self, **_: object) -> str:
        self.calls += 1
        self.release.wait(timeout=5)
        return "{}"


class FailingClient:
    model_id = "failing"

    def complete_json(self, **_: object) -> str:
        raise ConnectionError("connection reset by peer")


def test_a_hung_model_call_is_discarded_at_the_deadline(
    imagepull: Contract, ledger: Ledger
) -> None:
    """The evaluation must not stall on one incident. Recorded live: 999 s."""
    import time

    client = HangingClient()
    pipeline = DiagnosisPipeline(client=client, ledger=ledger, model_deadline=0.2)
    started = time.monotonic()
    state = pipeline.run(imagepull)
    elapsed = time.monotonic() - started
    client.release.set()

    assert elapsed < 2.0, f"the pipeline waited {elapsed:.1f}s past a 0.2s deadline"
    assert state["outcome"] == Outcome.DISCARDED.value
    assert "did not answer" in state["discard_reason"]
    assert client.calls == 1, "a timed out call is not retried; the budget is spent"

    row = ledger.get(imagepull.incident_id)
    assert row is not None
    assert row["outcome"] == "DISCARDED"
    assert row["discard_reason"] == state["discard_reason"]
    assert row["abstained"] == 0
    assert row["confidence_final"] is None
    assert row["root_cause"] == ""


def test_a_failing_model_call_is_discarded_with_the_error(
    oomkilled: Contract, ledger: Ledger
) -> None:
    state = DiagnosisPipeline(client=FailingClient(), ledger=ledger).run(oomkilled)
    assert state["outcome"] == Outcome.DISCARDED.value
    assert "ConnectionError" in state["discard_reason"]
    assert "connection reset" in state["discard_reason"]


def test_the_deadline_covers_the_retry(oomkilled: Contract, ledger: Ledger) -> None:
    """A slow first answer that fails validation leaves no time for a second."""
    clock = {"t": 0.0}
    bad = _answer(evidence=[{"source": "logs", "field": "logs.nope", "value": "x"}])
    client = ScriptedClient([bad, bad])

    class SlowFirst:
        model_id = "slow"

        def complete_json(self, **kw: object) -> str:
            clock["t"] += 100.0
            return client.complete_json(**kw)  # type: ignore[arg-type]

    pipeline = DiagnosisPipeline(
        client=SlowFirst(),
        ledger=ledger,
        model_deadline=90.0,
        clock=lambda: clock["t"],
    )
    state = pipeline.run(oomkilled)
    assert state["outcome"] == Outcome.DISCARDED.value
    assert "already spent" in state["discard_reason"]
    assert len(client.calls) == 1


def test_discarded_is_neither_approvable_nor_ratable(imagepull: Contract, ledger: Ledger) -> None:
    from coroner_brain.api import build_response
    from coroner_brain.ledger import NotRatableError
    from coroner_brain.sink import Notice, render_text

    pipeline = DiagnosisPipeline(client=FailingClient(), ledger=ledger)
    verdict = build_response(pipeline, imagepull, 0.5)
    assert verdict.outcome is Outcome.DISCARDED
    assert verdict.discarded
    assert not verdict.approvable
    assert not verdict.abstained
    assert verdict.confidence_final is None

    text = render_text(
        Notice(
            contract=imagepull, verdict=verdict, mode="live", deadline=None, public_url="http://b"
        )
    )
    assert "DISCARDED" in text
    assert "did not answer" in text
    assert '"decision": "approved"' not in text
    assert "would_approve" not in text

    import pytest

    with pytest.raises(NotRatableError):
        ledger.label(imagepull.incident_id, shadow_rating="unsure")
    ledger.label(imagepull.incident_id, actual_cause="the tag was wrong")


def test_a_rate_limit_with_a_short_wait_is_retried_inside_the_deadline(
    oomkilled: Contract, ledger: Ledger
) -> None:
    """Measured live: 8000 tokens a minute, a 5000 token prompt, 429 with "try again in 2.2s"."""
    from coroner_brain.graph import retry_after_seconds

    class RateLimited:
        model_id = "limited"

        def __init__(self) -> None:
            self.calls = 0

        def complete_json(self, **_: object) -> str:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    "Error code: 429 - rate limit reached ... Please try again in 2.219999999s"
                )
            return _answer(
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

    client = RateLimited()
    slept: list[float] = []
    pipeline = DiagnosisPipeline(client=client, ledger=ledger, model_deadline=30.0)
    pipeline._sleep = slept.append
    state = pipeline.run(oomkilled)

    assert state["outcome"] == Outcome.DIAGNOSED.value
    assert client.calls == 2
    assert slept == [pytest.approx(2.72, abs=0.01)]

    assert retry_after_seconds(RuntimeError("429 ... try again in 1m3.5s")) == 63.5
    assert retry_after_seconds(RuntimeError("rate limited: retry after 600s")) == 600.0
    assert retry_after_seconds(RuntimeError("429 rate limit")) == 5.0
    assert retry_after_seconds(RuntimeError("connection reset")) is None


def test_a_rate_limit_that_outlasts_the_deadline_is_discarded(
    oomkilled: Contract, ledger: Ledger
) -> None:
    class AlwaysLimited:
        model_id = "limited"

        def complete_json(self, **_: object) -> str:
            raise RuntimeError("Error code: 429 - rate limit ... Please try again in 2m0s")

    pipeline = DiagnosisPipeline(client=AlwaysLimited(), ledger=ledger, model_deadline=30.0)
    pipeline._sleep = lambda _: None
    state = pipeline.run(oomkilled)
    assert state["outcome"] == Outcome.DISCARDED.value
    assert "429" in state["discard_reason"]
