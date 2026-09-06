"""The Slack sink.

Opt-in through configuration and never a prerequisite for seeing the system
work; section 7.2. It renders the same Observed and Inferred blocks as
stdout, visually separated, and the same decision affordances: approve,
reject, and edit buttons only when approval is on offer, rating buttons in
shadow mode, and a prompt for the actual cause on abstention. Below the
threshold there is no approve button at all.

Slack interactions arrive at the brain's webhook (see api.py), which is the
one place a decision is applied whatever the transport. This module only
renders and posts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from coroner_brain.sink import (
    Notice,
    render_inferred,
    render_observed,
    render_status,
)

API = "https://slack.com/api"

# Slack limits a section's text to 3000 characters. Observed can be long
# when logs are present; it is cut from the top so the end of the log, where
# the fatal line is, survives.
SECTION_LIMIT = 2900

# Action ids the webhook dispatches on. The value carried is the incident id.
ACTION_APPROVE = "coroner_approve"
ACTION_REJECT = "coroner_reject"
ACTION_EDIT = "coroner_edit"
ACTION_RATE_APPROVE = "coroner_rate_would_approve"
ACTION_RATE_REJECT = "coroner_rate_would_reject"
ACTION_RATE_UNSURE = "coroner_rate_unsure"
ACTION_ACTUAL_CAUSE = "coroner_actual_cause"

RATING_ACTIONS = {
    ACTION_RATE_APPROVE: "would_approve",
    ACTION_RATE_REJECT: "would_reject",
    ACTION_RATE_UNSURE: "unsure",
}


class SlackError(RuntimeError):
    """Slack answered, and the answer was not ok."""


@dataclass(frozen=True)
class SlackConfig:
    bot_token: str
    channel: str
    signing_secret: str


class SlackClient:
    """The three Web API calls the sink and the webhook need."""

    def __init__(self, bot_token: str, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(timeout=15.0)
        self._headers = {
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self._http.post(f"{API}/{method}", headers=self._headers, json=body)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise SlackError(f"{method}: {data.get('error') if isinstance(data, dict) else data}")
        return data

    def post_message(self, channel: str, text: str, blocks: list[dict[str, Any]]) -> str:
        data = self.call("chat.postMessage", {"channel": channel, "text": text, "blocks": blocks})
        return str(data.get("ts", ""))

    def update_message(
        self, channel: str, ts: str, text: str, blocks: list[dict[str, Any]]
    ) -> None:
        self.call("chat.update", {"channel": channel, "ts": ts, "text": text, "blocks": blocks})

    def open_view(self, trigger_id: str, view: dict[str, Any]) -> None:
        self.call("views.open", {"trigger_id": trigger_id, "view": view})

    def respond(self, response_url: str, body: dict[str, Any]) -> None:
        """Post to an interaction's response_url, which needs no token."""
        response = self._http.post(response_url, json=body)
        response.raise_for_status()


# ------------------------------------------------------------- verification

# Slack signs each request as v0:<timestamp>:<body> with the signing secret.
# A request older than this is refused even with a good signature, which is
# Slack's own guidance against replay.
MAX_REQUEST_AGE_SECONDS = 300


def verify_signature(
    signing_secret: str, timestamp: str, body: bytes, signature: str, now: float | None = None
) -> bool:
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - sent_at) > MAX_REQUEST_AGE_SECONDS:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def sign_request(signing_secret: str, timestamp: str, body: bytes) -> str:
    """What Slack would send. Tests use it to build valid requests."""
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()


# ----------------------------------------------------------------- rendering


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _code(lines: list[str], limit: int = SECTION_LIMIT) -> str:
    body = "\n".join(lines)
    if len(body) > limit:
        body = "[cut]\n" + body[-(limit - 6) :]
    return f"```{body}```"


def _button(text: str, action_id: str, value: str, style: str | None = None) -> dict[str, Any]:
    button: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "action_id": action_id,
        "value": value,
    }
    if style:
        button["style"] = style
    return button


def render_blocks(notice: Notice, status: list[str] | None = None) -> list[dict[str, Any]]:
    """Block Kit for one verdict.

    With status lines present the message is a record of a decision already
    made: no buttons are rendered at all.
    """
    v = notice.verdict
    c = notice.contract
    incident = v.incident_id
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Coroner: {v.failure_type} in {c.pod.namespace}/{c.pod.name}",
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"incident `{incident}`"}],
        },
        _section("*Observed*  collected by the agent, verbatim, no model involvement"),
        _section(_code(render_observed(c))),
        {"type": "divider"},
        _section("*Inferred*  model output, every citation checked against the evidence above"),
        _section(_code(render_inferred(v))),
        {"type": "divider"},
    ]

    if status:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "\n".join(status)}]}
        )
        return blocks

    if v.discarded:
        blocks.append(
            _section(
                "*Decision*  the model did not answer, so no diagnosis exists. Nothing to "
                "approve or rate. Recorded as DISCARDED and excluded from accuracy."
            )
        )
        return blocks

    if v.abstained:
        blocks.append(
            _section(
                "*Decision*  Coroner abstained. Nothing can be approved. When the incident is "
                "resolved, record the actual cause so the abstention can be scored, and say "
                "whether abstaining was the right call."
            )
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _button("Record actual cause", ACTION_ACTUAL_CAUSE, incident),
                    _button("Right to abstain", ACTION_RATE_APPROVE, incident),
                    _button("Should have diagnosed", ACTION_RATE_REJECT, incident),
                    _button("Unsure", ACTION_RATE_UNSURE, incident),
                ],
            }
        )
        return blocks

    if notice.mode == "shadow":
        blocks.append(
            _section(
                f"*Decision*  shadow mode for {v.failure_type}: no approval is offered and "
                "nothing will execute. Would you have approved this? The answer is a label, "
                "not an action."
            )
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _button("Would approve", ACTION_RATE_APPROVE, incident),
                    _button("Would reject", ACTION_RATE_REJECT, incident),
                    _button("Unsure", ACTION_RATE_UNSURE, incident),
                ],
            }
        )
        return blocks

    if not notice.offers_approval:
        # Section 4.2 control 4. The button is absent, not disabled.
        blocks.append(
            _section(
                f"*Decision*  not approvable: confidence {v.confidence_final:.2f} is below "
                "the threshold. No approval is offered. Nothing will execute."
            )
        )
        return blocks

    deadline = notice.deadline.strftime("%H:%M:%SZ") if notice.deadline else "-"
    blocks.append(
        _section(
            f"*Decision*  approvable. Decide before {deadline}; after that it is recorded "
            "as expired. Rejecting asks for one line on what is wrong."
        )
    )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                _button("Approve", ACTION_APPROVE, incident, style="primary"),
                _button("Reject", ACTION_REJECT, incident, style="danger"),
                _button("Edit action", ACTION_EDIT, incident),
            ],
        }
    )
    return blocks


def summary_text(notice: Notice) -> str:
    """Notification text, shown where blocks are not rendered."""
    v = notice.verdict
    head = (
        f"Coroner: {v.failure_type} in {notice.contract.pod.namespace}/{notice.contract.pod.name}"
    )
    if v.discarded:
        return f"{head}: discarded, the model did not answer"
    if v.abstained:
        return f"{head}: insufficient context"
    return f"{head}: {v.root_cause}"


def _input_view(
    *, callback_id: str, title: str, label: str, placeholder: str, metadata: dict[str, Any]
) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": callback_id,
        "private_metadata": json.dumps(metadata),
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": "Record"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "text",
                "label": {"type": "plain_text", "text": label},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "multiline": True,
                    "placeholder": {"type": "plain_text", "text": placeholder},
                },
            }
        ],
    }


VIEW_REJECT = "coroner_reject_view"
VIEW_EDIT = "coroner_edit_view"
VIEW_ACTUAL_CAUSE = "coroner_actual_cause_view"


def reject_view(metadata: dict[str, Any]) -> dict[str, Any]:
    return _input_view(
        callback_id=VIEW_REJECT,
        title="Reject diagnosis",
        label="What is wrong with it? One line.",
        placeholder="the tag is right, the node is missing a pull secret",
        metadata=metadata,
    )


def edit_view(metadata: dict[str, Any], proposed: str) -> dict[str, Any]:
    view = _input_view(
        callback_id=VIEW_EDIT,
        title="Edit action",
        label="The action to execute instead. This is what will run.",
        placeholder=proposed[:150] or "the corrected action",
        metadata=metadata,
    )
    view["blocks"][0]["element"]["initial_value"] = proposed[:3000]
    return view


def actual_cause_view(metadata: dict[str, Any]) -> dict[str, Any]:
    return _input_view(
        callback_id=VIEW_ACTUAL_CAUSE,
        title="Actual cause",
        label="What actually happened? One line.",
        placeholder="the database password had rotated",
        metadata=metadata,
    )


# ---------------------------------------------------------------------- sink


class SlackSink:
    name = "slack"

    def __init__(self, config: SlackConfig, client: SlackClient | None = None) -> None:
        self.config = config
        self.client = client or SlackClient(config.bot_token)

    def deliver(self, notice: Notice) -> None:
        self.client.post_message(self.config.channel, summary_text(notice), render_blocks(notice))

    def refresh(self, channel: str, ts: str, notice: Notice, row: dict[str, Any]) -> None:
        """Re-render a delivered message as the record of what was decided."""
        status = render_status(row) or ["recorded"]
        self.client.update_message(channel, ts, summary_text(notice), render_blocks(notice, status))
