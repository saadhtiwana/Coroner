from __future__ import annotations

import json

from fastapi.testclient import TestClient

from coroner_brain import CONTRACT_VERSION, __version__
from coroner_brain.api import app, build_response
from coroner_brain.contract import Contract
from coroner_brain.diagnosis import Outcome
from coroner_brain.graph import DiagnosisPipeline
from coroner_brain.ledger import Ledger
from coroner_brain.llm import ScriptedClient

client = TestClient(app)


def test_healthz_reports_versions_without_revealing_credentials() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["contract_version"] == CONTRACT_VERSION
    assert isinstance(body["credentials_present"], bool)
    # The response says whether a credential exists, never what it is.
    assert "api_key" not in json.dumps(body).lower()


def test_response_separates_observed_from_inferred(imagepull: Contract, ledger: Ledger) -> None:
    payload = imagepull.model_dump(mode="json")
    answer = json.dumps(
        {
            "root_cause": "The image cannot be pulled.",
            "explanation": "The kubelet reported a pull failure.",
            "proposed_action": "Fix the image reference.",
            "confidence": 0.9,
            "evidence": [
                {
                    "source": "container",
                    "field": "container.image",
                    "value": payload["container"]["image"],
                }
            ],
            "competing_hypothesis": "",
        }
    )
    pipeline = DiagnosisPipeline(client=ScriptedClient([answer]), ledger=ledger)
    result = build_response(pipeline, imagepull, 0.5)

    assert result.outcome is Outcome.DIAGNOSED
    assert result.approvable is True
    assert result.confidence_ceiling is not None
    assert result.confidence_final is not None
    assert result.confidence_final <= result.confidence_ceiling
    assert result.evidence


def test_abstention_is_not_approvable(crashloop: Contract, ledger: Ledger) -> None:
    """Section 4.2 control 4: no approve affordance below the threshold."""
    stripped = crashloop.model_copy(deep=True)
    stripped.logs.available = False
    stripped.logs.empty = True
    stripped.logs.content = ""

    pipeline = DiagnosisPipeline(client=ScriptedClient([]), ledger=ledger)
    result = build_response(pipeline, stripped, 0.5)

    assert result.outcome is Outcome.INSUFFICIENT_CONTEXT
    assert result.abstained is True
    assert result.approvable is False
    assert result.root_cause == ""
    assert result.evidence == []
    assert result.abstain_reason


def test_diagnose_delivers_to_the_sink_after_the_ledger_write(
    imagepull: Contract, ledger: Ledger
) -> None:
    from coroner_brain.api import deliver
    from coroner_brain.config import Settings
    from coroner_brain.sink import Notice

    class Recording:
        name = "recording"

        def __init__(self) -> None:
            self.notices: list[Notice] = []

        def deliver(self, notice: Notice) -> None:
            self.notices.append(notice)

    payload = imagepull.model_dump(mode="json")
    answer = json.dumps(
        {
            "root_cause": "The image cannot be pulled.",
            "explanation": "The kubelet reported a pull failure.",
            "proposed_action": "Fix the image reference.",
            "confidence": 0.9,
            "evidence": [
                {
                    "source": "container",
                    "field": "container.image",
                    "value": payload["container"]["image"],
                }
            ],
            "competing_hypothesis": "",
        }
    )
    pipeline = DiagnosisPipeline(client=ScriptedClient([answer]), ledger=ledger)
    verdict = build_response(pipeline, imagepull, 0.5)
    assert ledger.get(imagepull.incident_id) is not None, "ledger row must exist before delivery"

    settings = Settings(
        api_key=None,
        base_url="",
        model="m",
        ledger_path=ledger.path,
        abstention_threshold=0.5,
        max_validation_retries=1,
        sink="recording",
        public_url="http://brain.test",
        approval_ttl_seconds=60,
        promoted_types=frozenset(),
        redis_url=None,
    )
    sink = Recording()
    out = deliver(verdict, imagepull, settings, sink)
    assert out.delivered is True
    assert out.mode == "shadow", "nothing is promoted by default"
    assert len(sink.notices) == 1
    assert sink.notices[0].mode == "shadow"
    assert not sink.notices[0].offers_approval

    promoted = Settings(**{**settings.__dict__, "promoted_types": frozenset({"ImagePullBackOff"})})
    out = deliver(verdict, imagepull, promoted, sink)
    assert out.mode == "live"
    assert sink.notices[-1].offers_approval


def test_a_failing_sink_does_not_lose_the_verdict(imagepull: Contract, ledger: Ledger) -> None:
    from coroner_brain.api import deliver
    from coroner_brain.config import Settings
    from coroner_brain.sink import Notice

    class Broken:
        name = "broken"

        def deliver(self, notice: Notice) -> None:
            raise RuntimeError("slack is down")

    pipeline = DiagnosisPipeline(client=ScriptedClient([]), ledger=ledger)
    stripped = imagepull.model_copy(deep=True)
    stripped.failure_type = "CrashLoopBackOff"
    stripped.logs.available = False
    verdict = build_response(pipeline, stripped, 0.5)
    settings = Settings(
        api_key=None,
        base_url="",
        model="m",
        ledger_path=ledger.path,
        abstention_threshold=0.5,
        max_validation_retries=1,
        sink="broken",
        public_url="http://brain.test",
        approval_ttl_seconds=60,
        promoted_types=frozenset(),
        redis_url=None,
    )
    out = deliver(verdict, stripped, settings, Broken())
    assert out.delivered is False
    assert out.outcome is Outcome.INSUFFICIENT_CONTEXT
    assert ledger.get(stripped.incident_id) is not None
