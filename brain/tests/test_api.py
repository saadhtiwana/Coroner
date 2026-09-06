from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coroner_brain import CONTRACT_VERSION, __version__
from coroner_brain.api import Services, app, build_response, deliver, get_services
from coroner_brain.approval import ApprovalPipeline, verify
from coroner_brain.config import Settings
from coroner_brain.contract import Contract
from coroner_brain.diagnosis import Outcome
from coroner_brain.graph import DiagnosisPipeline
from coroner_brain.inflight import MemoryStore
from coroner_brain.ledger import Ledger
from coroner_brain.llm import ScriptedClient
from coroner_brain.sink import Notice

SECRET = b"test-secret"


def settings(ledger: Ledger, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "api_key": None,
        "base_url": "",
        "model": "scripted",
        "ledger_path": ledger.path,
        "abstention_threshold": 0.5,
        "max_validation_retries": 1,
        "model_deadline_seconds": 180.0,
        "sink": "stdout",
        "public_url": "http://brain.test",
        "approval_ttl_seconds": 600,
        "promoted_types": frozenset(),
        "redis_url": None,
        "approval_secret": SECRET,
        "approval_secret_generated": False,
        "slack_bot_token": "",
        "slack_channel": "",
        "slack_signing_secret": "",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class Recording:
    name = "recording"

    def __init__(self) -> None:
        self.notices: list[Notice] = []

    def deliver(self, notice: Notice) -> None:
        self.notices.append(notice)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 6, 14, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def services(
    ledger: Ledger, answers: list[str], clock: Clock | None = None, **overrides: object
) -> tuple[Services, Recording]:
    cfg = settings(ledger, **overrides)
    store = MemoryStore()
    approvals = ApprovalPipeline(
        store=store,
        ledger=ledger,
        secret=cfg.approval_secret,
        ttl_seconds=cfg.approval_ttl_seconds,
        now=clock,
    )
    sink = Recording()
    client = ScriptedClient(answers)
    svc = Services(settings=cfg, ledger=ledger, sink=sink, store=store, approvals=approvals)
    svc._pipeline = DiagnosisPipeline(client=client, ledger=ledger)
    return svc, sink


def good_answer(payload: dict[str, object]) -> str:
    container = payload["container"]
    assert isinstance(container, dict)
    return json.dumps(
        {
            "root_cause": "The image cannot be pulled.",
            "explanation": "The kubelet reported a pull failure.",
            "proposed_action": "Fix the image reference.",
            "confidence": 0.9,
            "evidence": [
                {"source": "container", "field": "container.image", "value": container["image"]}
            ],
            "competing_hypothesis": "",
        }
    )


@pytest.fixture
def api(
    ledger: Ledger, imagepull: Contract
) -> Iterator[tuple[TestClient, Services, Recording, Clock]]:
    clock = Clock()
    svc, sink = services(
        ledger,
        [good_answer(imagepull.model_dump(mode="json"))],
        clock,
        promoted_types=frozenset({"ImagePullBackOff"}),
    )
    app.dependency_overrides[get_services] = lambda: svc
    try:
        yield TestClient(app), svc, sink, clock
    finally:
        app.dependency_overrides.clear()


def test_healthz_reports_versions_without_revealing_credentials(
    api: tuple[TestClient, Services, Recording, Clock],
) -> None:
    client, _, _, _ = api
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["contract_version"] == CONTRACT_VERSION
    assert isinstance(body["credentials_present"], bool)
    assert body["sink"] == "recording"
    assert body["inflight_store"] == "memory"
    assert body["promoted_types"] == ["ImagePullBackOff"]
    assert "api_key" not in json.dumps(body).lower()
    assert "secret" not in json.dumps(body).lower()


def test_response_separates_observed_from_inferred(imagepull: Contract, ledger: Ledger) -> None:
    answer = good_answer(imagepull.model_dump(mode="json"))
    pipeline = DiagnosisPipeline(client=ScriptedClient([answer]), ledger=ledger)
    result = build_response(pipeline, imagepull, 0.5)

    assert result.outcome is Outcome.DIAGNOSED
    assert result.approvable is True
    assert result.confidence_ceiling is not None
    assert result.confidence_final is not None
    assert result.confidence_final <= result.confidence_ceiling
    assert result.evidence
    assert result.context_hash


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


# ---------------------------------------------------------- the whole flow


def test_diagnose_parks_delivers_and_returns(
    api: tuple[TestClient, Services, Recording, Clock], imagepull: Contract
) -> None:
    client, svc, sink, clock = api
    response = client.post("/diagnose", json=imagepull.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "DIAGNOSED"
    assert body["approvable"] is True
    assert body["mode"] == "live"
    assert body["delivered"] is True

    # Ledger first, then parked, then delivered with the deadline.
    assert svc.ledger.get(imagepull.incident_id) is not None
    assert [p.incident_id for p in svc.approvals.pending()] == [imagepull.incident_id]
    assert len(sink.notices) == 1
    notice = sink.notices[0]
    assert notice.offers_approval
    assert notice.deadline == clock.now + timedelta(seconds=600)

    listed = client.get("/incidents/pending").json()
    assert listed[0]["incident_id"] == imagepull.incident_id


def test_approval_resumes_the_graph_and_mints_a_verifiable_token(
    api: tuple[TestClient, Services, Recording, Clock], imagepull: Contract
) -> None:
    client, svc, _, _ = api
    verdict = client.post("/diagnose", json=imagepull.model_dump(mode="json")).json()

    decided = client.post(
        f"/incidents/{imagepull.incident_id}/decision", json={"decision": "approved"}
    )
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["decision"] == "approved"
    assert body["action"] == "Fix the image reference."
    assert body["approval_token"].startswith("v1.")
    assert verify(
        SECRET,
        body["approval_token"],
        incident_id=imagepull.incident_id,
        context_hash=verdict["context_hash"],
        decision="approved",
        action="Fix the image reference.",
        decided_at=body["decided_at"],
    )
    # A token does not verify for a different action or a different secret.
    assert not verify(
        SECRET,
        body["approval_token"],
        incident_id=imagepull.incident_id,
        context_hash=verdict["context_hash"],
        decision="approved",
        action="delete the namespace",
        decided_at=body["decided_at"],
    )
    assert not verify(
        b"other",
        body["approval_token"],
        incident_id=imagepull.incident_id,
        context_hash=verdict["context_hash"],
        decision="approved",
        action="Fix the image reference.",
        decided_at=body["decided_at"],
    )

    row = client.get(f"/incidents/{imagepull.incident_id}").json()
    assert row["decision"] == "approved"
    assert row["decision_action"] == "Fix the image reference."
    assert row["approval_token"] == body["approval_token"]
    assert svc.approvals.pending() == []

    # Decided once. Section 5.4.
    again = client.post(
        f"/incidents/{imagepull.incident_id}/decision", json={"decision": "rejected", "reason": "x"}
    )
    assert again.status_code == 409


def test_rejection_requires_a_reason(
    api: tuple[TestClient, Services, Recording, Clock], imagepull: Contract
) -> None:
    client, _, _, _ = api
    client.post("/diagnose", json=imagepull.model_dump(mode="json"))
    url = f"/incidents/{imagepull.incident_id}/decision"
    assert client.post(url, json={"decision": "rejected"}).status_code == 422
    assert client.post(url, json={"decision": "rejected", "reason": "  "}).status_code == 422
    ok = client.post(url, json={"decision": "rejected", "reason": "the tag is fine, creds"})
    assert ok.status_code == 200
    assert ok.json()["approval_token"] == "", "a rejection authorises nothing"
    row = client.get(f"/incidents/{imagepull.incident_id}").json()
    assert row["decision"] == "rejected"
    assert row["decision_reason"] == "the tag is fine, creds"
    assert row["approval_token"] is None


def test_edit_records_the_corrected_action_as_what_executes(
    api: tuple[TestClient, Services, Recording, Clock], imagepull: Contract
) -> None:
    client, _, _, _ = api
    client.post("/diagnose", json=imagepull.model_dump(mode="json"))
    url = f"/incidents/{imagepull.incident_id}/decision"
    assert client.post(url, json={"decision": "edited"}).status_code == 422
    ok = client.post(url, json={"decision": "edited", "action": "attach pull secret ghcr-ro"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["action"] == "attach pull secret ghcr-ro"
    assert body["approval_token"]
    row = client.get(f"/incidents/{imagepull.incident_id}").json()
    assert row["decision"] == "edited"
    assert row["decision_action"] == "attach pull secret ghcr-ro"
    assert row["proposed_action"] == "Fix the image reference."


def test_expiry_is_recorded_by_the_clock_and_blocks_a_late_approval(
    api: tuple[TestClient, Services, Recording, Clock], imagepull: Contract
) -> None:
    client, svc, _, clock = api
    client.post("/diagnose", json=imagepull.model_dump(mode="json"))
    clock.now += timedelta(seconds=601)

    late = client.post(
        f"/incidents/{imagepull.incident_id}/decision", json={"decision": "approved"}
    )
    assert late.status_code == 409
    row = client.get(f"/incidents/{imagepull.incident_id}").json()
    assert row["decision"] == "expired"
    assert row["approval_token"] is None
    assert svc.approvals.pending() == []


def test_sweeper_expires_overdue_incidents(
    api: tuple[TestClient, Services, Recording, Clock], imagepull: Contract
) -> None:
    client, svc, _, clock = api
    client.post("/diagnose", json=imagepull.model_dump(mode="json"))
    assert svc.approvals.expire_overdue() == []
    clock.now += timedelta(seconds=601)
    assert svc.approvals.expire_overdue() == [imagepull.incident_id]
    assert client.get(f"/incidents/{imagepull.incident_id}").json()["decision"] == "expired"


def test_shadow_mode_parks_nothing_and_rates(imagepull: Contract, ledger: Ledger) -> None:
    """Section 5.5: a label without the action. Nothing to approve exists."""
    svc, sink = services(ledger, [good_answer(imagepull.model_dump(mode="json"))])
    app.dependency_overrides[get_services] = lambda: svc
    try:
        client = TestClient(app)
        body = client.post("/diagnose", json=imagepull.model_dump(mode="json")).json()
        assert body["approvable"] is True, "the verdict itself clears the threshold"
        assert body["mode"] == "shadow"
        assert svc.approvals.pending() == []
        assert not sink.notices[0].offers_approval

        url = f"/incidents/{imagepull.incident_id}"
        denied = client.post(f"{url}/decision", json={"decision": "approved"})
        assert denied.status_code == 404, "nothing is parked, so nothing can be approved"

        rated = client.post(f"{url}/rating", json={"rating": "would_approve"})
        assert rated.status_code == 200
        assert client.post(f"{url}/rating", json={"rating": "unsure"}).status_code == 409
        row = client.get(url).json()
        assert row["shadow_rating"] == "would_approve"
        assert row["decision"] is None
    finally:
        app.dependency_overrides.clear()


def test_actual_cause_is_recorded_for_an_abstention(crashloop: Contract, ledger: Ledger) -> None:
    stripped = crashloop.model_copy(deep=True)
    stripped.logs.available = False
    stripped.logs.empty = True
    stripped.logs.content = ""
    svc, _ = services(ledger, [])
    app.dependency_overrides[get_services] = lambda: svc
    try:
        client = TestClient(app)
        body = client.post("/diagnose", json=stripped.model_dump(mode="json")).json()
        assert body["outcome"] == "INSUFFICIENT_CONTEXT"
        url = f"/incidents/{stripped.incident_id}"
        assert client.post(f"{url}/actual-cause", json={"actual_cause": ""}).status_code == 422
        ok = client.post(f"{url}/actual-cause", json={"actual_cause": "db password rotated"})
        assert ok.status_code == 200
        assert client.get(url).json()["actual_cause"] == "db password rotated"
        assert client.get("/incidents/inc-nope").status_code == 404
        assert (
            client.post("/incidents/inc-nope/rating", json={"rating": "unsure"}).status_code == 404
        )
    finally:
        app.dependency_overrides.clear()


def test_a_failing_sink_does_not_lose_the_verdict(imagepull: Contract, ledger: Ledger) -> None:
    class Broken:
        name = "broken"

        def deliver(self, notice: Notice) -> None:
            raise RuntimeError("slack is down")

    svc, _ = services(ledger, [good_answer(imagepull.model_dump(mode="json"))])
    svc.sink = Broken()
    verdict = build_response(svc.pipeline, imagepull, 0.5)
    out = deliver(verdict, imagepull, svc)
    assert out.delivered is False
    assert out.outcome is Outcome.DIAGNOSED
    assert ledger.get(imagepull.incident_id) is not None


def test_diagnose_without_credentials_is_a_503(ledger: Ledger, imagepull: Contract) -> None:
    cfg = settings(ledger)
    store = MemoryStore()
    approvals = ApprovalPipeline(store=store, ledger=ledger, secret=SECRET, ttl_seconds=60)
    svc = Services(settings=cfg, ledger=ledger, sink=Recording(), store=store, approvals=approvals)
    app.dependency_overrides[get_services] = lambda: svc
    try:
        response = TestClient(app).post("/diagnose", json=imagepull.model_dump(mode="json"))
        assert response.status_code == 503
    finally:
        app.dependency_overrides.clear()


def test_ledger_path_is_created(tmp_path: Path) -> None:
    assert Ledger(tmp_path / "nested" / "ledger.sqlite3").count() == 0


def test_build_sink_defaults_to_stdout_and_refuses_half_configured_slack(ledger: Ledger) -> None:
    from coroner_brain.api import build_sink
    from coroner_brain.slack import SlackSink

    assert build_sink(settings(ledger)).name == "stdout"
    with pytest.raises(ValueError, match="CORONER_SLACK_CHANNEL"):
        build_sink(settings(ledger, sink="slack", slack_bot_token="x", slack_signing_secret="y"))
    with pytest.raises(ValueError, match="unknown sink"):
        build_sink(settings(ledger, sink="pager"))
    sink = build_sink(
        settings(
            ledger, sink="slack", slack_bot_token="x", slack_channel="C1", slack_signing_secret="y"
        )
    )
    assert isinstance(sink, SlackSink)


def test_approved_rows_flow_to_execution_and_resolution(
    api: tuple[TestClient, Services, Recording, Clock], imagepull: Contract
) -> None:
    """The agent's side of the ledger: read the approval, say what it did."""
    client, _, _, _ = api
    client.post("/diagnose", json=imagepull.model_dump(mode="json"))
    assert client.get("/incidents/approved").json() == [], "undecided rows are not approved"

    client.post(f"/incidents/{imagepull.incident_id}/decision", json={"decision": "approved"})
    rows = client.get("/incidents/approved").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["approval_token"].startswith("v1.")
    assert row["decision_action"] == "Fix the image reference."
    assert json.loads(row["contract_json"])["incident_id"] == imagepull.incident_id

    # Resolution before execution is refused.
    early = client.post(
        f"/incidents/{imagepull.incident_id}/resolution",
        json={"ready_within_sla": True, "stayed_ready": True, "resolved": True},
    )
    assert early.status_code == 409

    # A proposal is recorded and drops out of the default listing, but an
    # executing agent still sees it.
    proposed = client.post(
        f"/incidents/{imagepull.incident_id}/execution",
        json={"status": "proposed", "detail": "would patch", "plan": {"kind": "set-image"}},
    )
    assert proposed.status_code == 200
    assert client.get("/incidents/approved").json() == []
    assert len(client.get("/incidents/approved?execute=true").json()) == 1

    executed = client.post(
        f"/incidents/{imagepull.incident_id}/execution",
        json={"status": "executed", "detail": "patched"},
    )
    assert executed.status_code == 200
    assert client.get("/incidents/approved?execute=true").json() == []
    again = client.post(
        f"/incidents/{imagepull.incident_id}/execution",
        json={"status": "proposed", "detail": "x"},
    )
    assert again.status_code == 409, "execution status does not go backwards"

    resolved = client.post(
        f"/incidents/{imagepull.incident_id}/resolution",
        json={
            "ready_within_sla": True,
            "stayed_ready": False,
            "resolved": False,
            "detail": "flapped",
        },
    )
    assert resolved.status_code == 200
    stored = client.get(f"/incidents/{imagepull.incident_id}").json()
    assert stored["execution_status"] == "executed"
    assert stored["resolved_within_sla"] == 0
    assert "flapped" in stored["resolution_detail"]
    twice = client.post(
        f"/incidents/{imagepull.incident_id}/resolution",
        json={"ready_within_sla": True, "stayed_ready": True, "resolved": True},
    )
    assert twice.status_code == 409


def test_execution_needs_an_authorising_decision(
    api: tuple[TestClient, Services, Recording, Clock], imagepull: Contract
) -> None:
    client, _, _, _ = api
    client.post("/diagnose", json=imagepull.model_dump(mode="json"))
    client.post(
        f"/incidents/{imagepull.incident_id}/decision",
        json={"decision": "rejected", "reason": "wrong"},
    )
    denied = client.post(
        f"/incidents/{imagepull.incident_id}/execution", json={"status": "executed"}
    )
    assert denied.status_code == 409
    assert (
        client.post("/incidents/inc-nope/execution", json={"status": "executed"}).status_code == 404
    )
