"""The Slack interactions webhook, end to end through the HTTP surface.

Slack is replaced by a recording transport. The decision path is the same
approval graph the JSON endpoint uses; what is tested here is the
translation of Slack's shapes, the signature check, and the message update.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from coroner_brain.api import Services, app, get_services
from coroner_brain.approval import ApprovalPipeline
from coroner_brain.contract import Contract
from coroner_brain.graph import DiagnosisPipeline
from coroner_brain.inflight import MemoryStore
from coroner_brain.ledger import Ledger
from coroner_brain.llm import ScriptedClient
from coroner_brain.slack import (
    ACTION_APPROVE,
    ACTION_RATE_REJECT,
    ACTION_REJECT,
    VIEW_REJECT,
    SlackClient,
    SlackConfig,
    SlackSink,
    sign_request,
    verify_signature,
)
from tests.test_api import good_answer, settings

SIGNING = "slack-signing-secret"


class FakeSlack:
    """Records every Web API call and answers ok."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "ts": "1700000000.000100"})

    def named(self, method: str) -> list[dict[str, Any]]:
        return [body for path, body in self.calls if path == f"/api/{method}"]


@pytest.fixture
def slack_api(
    ledger: Ledger, imagepull: Contract
) -> Iterator[tuple[TestClient, Services, FakeSlack]]:
    fake = FakeSlack()
    client = SlackClient("xoxb-test", httpx.Client(transport=httpx.MockTransport(fake.handler)))
    sink = SlackSink(SlackConfig("xoxb-test", "C123", SIGNING), client)
    cfg = settings(
        ledger,
        sink="slack",
        slack_bot_token="xoxb-test",
        slack_channel="C123",
        slack_signing_secret=SIGNING,
        promoted_types=frozenset({"ImagePullBackOff"}),
    )
    store = MemoryStore()
    approvals = ApprovalPipeline(
        store=store, ledger=ledger, secret=cfg.approval_secret, ttl_seconds=600
    )
    svc = Services(settings=cfg, ledger=ledger, sink=sink, store=store, approvals=approvals)
    svc._pipeline = DiagnosisPipeline(
        client=ScriptedClient([good_answer(imagepull.model_dump(mode="json"))]), ledger=ledger
    )
    app.dependency_overrides[get_services] = lambda: svc
    try:
        yield TestClient(app), svc, fake
    finally:
        app.dependency_overrides.clear()


def _post_interaction(client: TestClient, payload: dict[str, Any], secret: str = SIGNING) -> Any:  # noqa: ANN401 - the test client's response type differs from httpx's
    body = urlencode({"payload": json.dumps(payload)}).encode()
    ts = str(int(time.time()))
    return client.post(
        "/slack/interactions",
        content=body,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sign_request(secret, ts, body),
        },
    )


def _block_action(action_id: str, incident_id: str) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "trigger_id": "trig.1",
        "user": {"id": "U1", "username": "oncall"},
        "container": {"channel_id": "C123", "message_ts": "1700000000.000100"},
        "actions": [{"action_id": action_id, "value": incident_id}],
    }


def _view_submission(callback_id: str, metadata: dict[str, Any], text: str) -> dict[str, Any]:
    return {
        "type": "view_submission",
        "view": {
            "callback_id": callback_id,
            "private_metadata": json.dumps(metadata),
            "state": {"values": {"text": {"value": {"value": text}}}},
        },
    }


def test_signature_verification() -> None:
    body = b"payload=%7B%7D"
    ts = str(int(time.time()))
    good = sign_request(SIGNING, ts, body)
    assert verify_signature(SIGNING, ts, body, good)
    assert not verify_signature(SIGNING, ts, body + b"x", good)
    assert not verify_signature("other", ts, body, good)
    assert not verify_signature(SIGNING, ts, body, "v0=00")
    stale = str(int(time.time()) - 600)
    assert not verify_signature(SIGNING, stale, body, sign_request(SIGNING, stale, body))
    assert not verify_signature("", ts, body, good)


def test_unsigned_requests_are_refused(
    slack_api: tuple[TestClient, Services, FakeSlack], imagepull: Contract
) -> None:
    client, _, _ = slack_api
    response = _post_interaction(client, _block_action(ACTION_APPROVE, "inc-x"), secret="wrong")
    assert response.status_code == 401
    assert client.post("/slack/interactions", content=b"payload=%7B%7D").status_code == 401


def test_delivery_posts_and_approve_button_resumes_the_graph(
    slack_api: tuple[TestClient, Services, FakeSlack], imagepull: Contract
) -> None:
    client, svc, fake = slack_api
    verdict = client.post("/diagnose", json=imagepull.model_dump(mode="json")).json()
    assert verdict["delivered"] is True
    posted = fake.named("chat.postMessage")
    assert len(posted) == 1
    assert posted[0]["channel"] == "C123"
    assert ACTION_APPROVE in json.dumps(posted[0]["blocks"])

    response = _post_interaction(client, _block_action(ACTION_APPROVE, imagepull.incident_id))
    assert response.status_code == 200
    row = svc.ledger.get(imagepull.incident_id)
    assert row is not None
    assert row["decision"] == "approved"
    assert row["approval_token"].startswith("v1.")
    assert svc.approvals.pending() == []

    # The message was redrawn as the record: status present, buttons gone.
    updated = fake.named("chat.update")
    assert len(updated) == 1
    assert updated[0]["ts"] == "1700000000.000100"
    blocks = json.dumps(updated[0]["blocks"])
    assert "approved at" in blocks
    assert ACTION_APPROVE not in blocks
    assert "403 Forbidden" in blocks, "the observed block survives the redraw"

    # A second click finds the decision already made and posts a note.
    _post_interaction(client, _block_action(ACTION_APPROVE, imagepull.incident_id))
    notes = fake.named("chat.postMessage")
    assert any("already decided" in n["text"] for n in notes[1:])


def test_reject_opens_a_modal_and_the_submission_records_the_reason(
    slack_api: tuple[TestClient, Services, FakeSlack], imagepull: Contract
) -> None:
    client, svc, fake = slack_api
    client.post("/diagnose", json=imagepull.model_dump(mode="json"))

    response = _post_interaction(client, _block_action(ACTION_REJECT, imagepull.incident_id))
    assert response.status_code == 200
    opened = fake.named("views.open")
    assert len(opened) == 1
    view = opened[0]["view"]
    assert view["callback_id"] == VIEW_REJECT
    metadata = json.loads(view["private_metadata"])
    assert metadata["incident_id"] == imagepull.incident_id

    empty = _post_interaction(client, _view_submission(VIEW_REJECT, metadata, "   "))
    assert empty.json()["response_action"] == "errors"
    row = svc.ledger.get(imagepull.incident_id)
    assert row is not None
    assert row["decision"] is None, "an empty reason records nothing"

    done = _post_interaction(
        client, _view_submission(VIEW_REJECT, metadata, "creds problem, not the tag")
    )
    assert done.json()["response_action"] == "clear"
    row = svc.ledger.get(imagepull.incident_id)
    assert row is not None
    assert row["decision"] == "rejected"
    assert row["decision_reason"] == "creds problem, not the tag"
    assert row["approval_token"] is None
    updated = fake.named("chat.update")
    assert "rejected at" in json.dumps(updated[-1]["blocks"])


def test_rating_button_labels_without_approving(
    slack_api: tuple[TestClient, Services, FakeSlack], imagepull: Contract
) -> None:
    client, svc, fake = slack_api
    svc.settings = settings(
        svc.ledger,
        sink="slack",
        slack_bot_token="xoxb-test",
        slack_channel="C123",
        slack_signing_secret=SIGNING,
    )
    client.post("/diagnose", json=imagepull.model_dump(mode="json"))
    assert svc.approvals.pending() == [], "shadow mode parks nothing"

    response = _post_interaction(client, _block_action(ACTION_RATE_REJECT, imagepull.incident_id))
    assert response.status_code == 200
    row = svc.ledger.get(imagepull.incident_id)
    assert row is not None
    assert row["shadow_rating"] == "would_reject"
    assert row["decision"] is None
    assert "rated: would_reject" in json.dumps(fake.named("chat.update")[-1]["blocks"])


def test_webhook_is_absent_without_the_slack_sink(ledger: Ledger, imagepull: Contract) -> None:
    from tests.test_api import services

    svc, _ = services(ledger, [])
    app.dependency_overrides[get_services] = lambda: svc
    try:
        response = _post_interaction(TestClient(app), _block_action(ACTION_APPROVE, "inc-x"))
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()
