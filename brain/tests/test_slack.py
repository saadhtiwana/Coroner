"""The Slack sink renders the same facts as stdout and posts them.

Slack itself is replaced by an httpx transport that records what was sent,
so nothing here touches the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from coroner_brain.contract import Contract
from coroner_brain.diagnosis import Citation, Outcome
from coroner_brain.sink import Notice
from coroner_brain.slack import (
    ACTION_APPROVE,
    ACTION_RATE_APPROVE,
    SlackClient,
    SlackConfig,
    SlackSink,
    render_blocks,
)
from coroner_brain.verdict import DiagnoseResponse

FABRICATED = "the model says the database is on fire"


def _verdict(contract: Contract, **overrides: object) -> DiagnoseResponse:
    base: dict[str, object] = {
        "incident_id": contract.incident_id,
        "failure_type": contract.failure_type,
        "outcome": Outcome.DIAGNOSED,
        "evidence_class": "image_pull_with_registry_error",
        "root_cause": FABRICATED,
        "explanation": FABRICATED,
        "proposed_action": FABRICATED,
        "evidence": [Citation(source="container", field="container.image", value="x")],
        "confidence_model": 0.9,
        "confidence_final": 0.9,
        "confidence_ceiling": 0.95,
        "approvable": True,
    }
    base.update(overrides)
    return DiagnoseResponse.model_validate(base)


def _notice(contract: Contract, verdict: DiagnoseResponse, mode: str) -> Notice:
    return Notice(
        contract=contract,
        verdict=verdict,
        mode="live" if mode == "live" else "shadow",
        deadline=datetime(2026, 9, 6, 14, 30, tzinfo=UTC),
        public_url="http://brain.test",
    )


def _actions(blocks: list[dict[str, Any]]) -> list[str]:
    return [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]


def _text(blocks: list[dict[str, Any]]) -> str:
    return json.dumps(blocks)


def test_observed_and_inferred_are_separate_blocks(imagepull: Contract) -> None:
    blocks = render_blocks(_notice(imagepull, _verdict(imagepull), "live"))
    texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    observed = texts[1]
    inferred = texts[3]
    assert "403 Forbidden" in observed
    assert FABRICATED not in observed
    assert FABRICATED in inferred
    assert blocks[texts.index(observed) + 2]["type"] == "divider" or any(
        b["type"] == "divider" for b in blocks
    )


def test_live_approvable_has_approve_reject_edit(imagepull: Contract) -> None:
    blocks = render_blocks(_notice(imagepull, _verdict(imagepull), "live"))
    actions = _actions(blocks)
    assert ACTION_APPROVE in actions
    assert "coroner_reject" in actions
    assert "coroner_edit" in actions
    assert "14:30:00Z" in _text(blocks)


def test_below_threshold_has_no_buttons_at_all(imagepull: Contract) -> None:
    """Section 4.2 control 4: absent, not disabled."""
    weak = _verdict(imagepull, confidence_final=0.4, approvable=False)
    blocks = render_blocks(_notice(imagepull, weak, "live"))
    assert _actions(blocks) == []
    assert not any(b["type"] == "actions" for b in blocks)
    assert "not approvable" in _text(blocks)


def test_shadow_mode_has_rating_buttons_and_no_approve(imagepull: Contract) -> None:
    blocks = render_blocks(_notice(imagepull, _verdict(imagepull), "shadow"))
    actions = _actions(blocks)
    assert ACTION_RATE_APPROVE in actions
    assert ACTION_APPROVE not in actions
    assert "shadow mode" in _text(blocks)


def test_abstention_asks_for_the_actual_cause(crashloop: Contract) -> None:
    abstained = _verdict(
        crashloop,
        outcome=Outcome.INSUFFICIENT_CONTEXT,
        evidence_class="crashloop_logs_unavailable",
        root_cause="",
        explanation="",
        proposed_action="",
        evidence=[],
        confidence_final=None,
        abstained=True,
        abstain_reason="No causal signal is present.",
        approvable=False,
    )
    blocks = render_blocks(_notice(crashloop, abstained, "live"))
    actions = _actions(blocks)
    assert "coroner_actual_cause" in actions
    assert ACTION_APPROVE not in actions


def test_a_decided_message_has_status_and_no_buttons(imagepull: Contract) -> None:
    blocks = render_blocks(
        _notice(imagepull, _verdict(imagepull), "live"), status=["approved at 2026-09-06"]
    )
    assert _actions(blocks) == []
    assert "approved at 2026-09-06" in _text(blocks)


def test_long_observed_is_cut_from_the_top(crashloop: Contract) -> None:
    big = crashloop.model_copy(deep=True)
    # The observed block keeps the last 20 log lines; make those lines long.
    big.logs.content = "\n".join(f"line {i} " + "x" * 400 for i in range(30)) + "\n[fatal] the end"
    blocks = render_blocks(_notice(big, _verdict(big), "shadow"))
    observed = [b["text"]["text"] for b in blocks if b["type"] == "section"][1]
    assert len(observed) <= 3000
    assert "[fatal] the end" in observed
    assert observed.startswith("```[cut]")


def test_sink_posts_to_chat_post_message(imagepull: Contract) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        assert request.headers["Authorization"] == "Bearer xoxb-test"
        return httpx.Response(200, json={"ok": True, "ts": "1.2"})

    client = SlackClient("xoxb-test", httpx.Client(transport=httpx.MockTransport(handler)))
    sink = SlackSink(SlackConfig("xoxb-test", "C123", "sig"), client)
    sink.deliver(_notice(imagepull, _verdict(imagepull), "live"))

    assert calls[0][0] == "/api/chat.postMessage"
    body = calls[0][1]
    assert body["channel"] == "C123"
    assert "ImagePullBackOff" in body["text"]
    assert ACTION_APPROVE in json.dumps(body["blocks"])


def test_a_not_ok_answer_is_an_error(imagepull: Contract) -> None:
    import pytest

    from coroner_brain.slack import SlackError

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    client = SlackClient("xoxb-test", httpx.Client(transport=httpx.MockTransport(handler)))
    sink = SlackSink(SlackConfig("xoxb-test", "C123", "sig"), client)
    with pytest.raises(SlackError, match="channel_not_found"):
        sink.deliver(_notice(imagepull, _verdict(imagepull), "live"))
