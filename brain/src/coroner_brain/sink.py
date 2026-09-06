"""Output sinks.

A sink is where a human sees a verdict. stdout is the default and needs no
configuration. Slack is one implementation among others and is never a
prerequisite for seeing the system work; docs/DESIGN.md section 7.2 makes that
the single most important evaluability requirement.

Every sink renders the same two blocks. Observed is verbatim collected fact
with no model involvement. Inferred is model output. Section 4.2 control 5:
the human can always judge the proposal against raw facts without trusting the
narrative. Below the abstention threshold, and in shadow mode, no approve
affordance is rendered at all; not disabled, absent. Control 4.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TextIO

from coroner_brain.contract import Contract, Event
from coroner_brain.diagnosis import Citation, Outcome
from coroner_brain.verdict import DiagnoseResponse

Mode = Literal["shadow", "live"]

# Log lines shown in the observed block. The contract holds up to 200 lines;
# a human reading a terminal or a Slack message needs the end, where the
# fatal line is, not the whole body.
OBSERVED_LOG_LINES = 20


@dataclass(frozen=True)
class Notice:
    """One verdict, ready for delivery."""

    contract: Contract
    verdict: DiagnoseResponse

    # shadow: the type has not cleared its section 2.4 prediction, so the
    # message carries a rating control and no approve button. live: the type
    # is promoted and an approvable verdict may be approved.
    mode: Mode

    # When a live, approvable verdict expires undecided. None otherwise.
    deadline: datetime | None

    # Base URL the decision endpoints are reachable at, for instructions.
    public_url: str

    @property
    def offers_approval(self) -> bool:
        """The approve affordance exists only here. Everywhere else it is absent."""
        return self.mode == "live" and self.verdict.approvable


class Sink(Protocol):
    name: str

    def deliver(self, notice: Notice) -> None: ...


# ------------------------------------------------------------------ rendering


def render_observed(contract: Contract) -> list[str]:
    """Verbatim collected facts. Nothing here came from a model."""
    pod = contract.pod
    c = contract.container
    lines = [
        f"pod           {pod.namespace}/{pod.name}",
        f"node          {pod.node_name or '(unscheduled)'}   phase {pod.phase}   "
        f"age {_age(pod.age_seconds)}",
    ]
    if contract.owner is not None:
        rev = f"   revision {contract.owner.revision}" if contract.owner.revision else ""
        lines.append(f"owner         {contract.owner.kind}/{contract.owner.name}{rev}")
    else:
        lines.append("owner         none (bare pod)")
    lines.append(f"container     {c.name}   image {c.image}   restarts {c.restart_count}")
    if c.waiting_reason:
        lines.append(f"state         waiting: {c.waiting_reason}")
        if c.waiting_message:
            lines.append(f"              {c.waiting_message}")
    t = c.last_terminated
    if t is not None:
        lines.append(f"last exit     code {t.exit_code}   reason {t.reason or '(none)'}")
        if t.message:
            lines.append(f"              {t.message}")
    if c.memory_limit or c.memory_request:
        lines.append(
            f"memory        limit {c.memory_limit or '(none)'}   "
            f"request {c.memory_request or '(none)'}"
        )
    if c.restart_count > 0:
        lines.append(f"crash rate    {c.crashes_per_minute:.2f} per minute")
    n = contract.node
    if n.name:
        pressure = [
            name
            for name, flag in (
                ("memory", n.memory_pressure),
                ("disk", n.disk_pressure),
                ("pid", n.pid_pressure),
            )
            if flag
        ]
        lines.append(
            f"node status   ready {str(n.ready).lower()}   "
            f"pressure {', '.join(pressure) if pressure else 'none'}"
        )

    warnings = [e for e in contract.events if e.type == "Warning"]
    if warnings:
        lines.append("events (Warning)")
        lines.extend(f"  {_event(e)}" for e in warnings)
    else:
        lines.append("events        no Warning events")

    logs = contract.logs
    if not logs.available:
        lines.append("logs          unavailable: nothing could be retrieved")
    elif logs.empty:
        src = "previous" if logs.from_previous else "current"
        lines.append(f"logs          retrieved from the {src} container and empty")
    else:
        src = "previous" if logs.from_previous else "current"
        body = logs.content.rstrip("\n").split("\n")
        shown = body[-OBSERVED_LOG_LINES:]
        note = f", last {len(shown)} of {len(body)} lines" if len(body) > len(shown) else ""
        trunc = ", tail truncated by the agent" if logs.truncated else ""
        lines.append(f"logs          {src} container{note}{trunc}")
        lines.extend(f"  | {line}" for line in shown)
    if contract.redacted_count:
        lines.append(
            f"redacted      {contract.redacted_count} value(s) withheld by the agent: "
            f"{', '.join(contract.redacted_kinds)}"
        )
    return lines


def render_inferred(verdict: DiagnoseResponse) -> list[str]:
    """Model output. Everything here is a claim, not a fact."""
    if verdict.abstained:
        return [
            f"outcome       {verdict.outcome.value}   evidence class {verdict.evidence_class}",
            "root cause    not determinable from the collected evidence",
            f"reason        {verdict.abstain_reason}",
            "proposal      none. Nothing can be approved.",
        ]
    lines = [
        f"outcome       {verdict.outcome.value}   evidence class {verdict.evidence_class}",
        f"confidence    {_conf(verdict.confidence_final)}   "
        f"(model {_conf(verdict.confidence_model)}, ceiling {_conf(verdict.confidence_ceiling)})",
        f"root cause    {verdict.root_cause}",
        f"explanation   {verdict.explanation}",
        f"proposal      {verdict.proposed_action}",
    ]
    if verdict.competing_hypothesis:
        lines.append(f"competing     {verdict.competing_hypothesis}")
    if verdict.evidence:
        lines.append("cited         " + "; ".join(c.field for c in verdict.evidence))
    return lines


def render_decision(notice: Notice) -> list[str]:
    """The affordance block. Approve appears only when it may."""
    v = notice.verdict
    url = f"{notice.public_url}/incidents/{v.incident_id}"
    post = f"curl -s -X POST {url}"

    if v.abstained:
        return [
            "no proposal to approve. Coroner abstained.",
            "when the incident is resolved, record the actual cause so the abstention",
            "can be scored (section 5.3):",
            f"  {post}/actual-cause -H 'content-type: application/json' "
            '-d \'{"actual_cause": "..."}\'',
            "was abstaining the right call?",
            f"  {post}/rating -H 'content-type: application/json' "
            '-d \'{"rating": "would_approve"}\'   '
            "(would_approve, would_reject, unsure)",
        ]

    if notice.mode == "shadow":
        return [
            f"shadow mode for {v.failure_type}: no approval is offered and nothing will execute.",
            "would you have approved this? The answer is a label, not an action (section 5.5):",
            f"  {post}/rating -H 'content-type: application/json' "
            '-d \'{"rating": "would_approve"}\'   '
            "(would_approve, would_reject, unsure)",
        ]

    if not v.approvable:
        return [
            f"not approvable: confidence {_conf(v.confidence_final)} is below the threshold.",
            "no approval is offered. Nothing will execute.",
        ]

    deadline = notice.deadline.astimezone(UTC).strftime("%H:%M:%SZ") if notice.deadline else "-"
    return [
        f"approvable. Decide before {deadline}; after that it is recorded as expired.",
        f"  approve  {post}/decision -H 'content-type: application/json' "
        '-d \'{"decision": "approved"}\'',
        f"  reject   {post}/decision -H 'content-type: application/json' "
        '-d \'{"decision": "rejected", "reason": "one line on what is wrong"}\'',
        f"  edit     {post}/decision -H 'content-type: application/json' "
        '-d \'{"decision": "edited", "action": "the corrected action"}\'',
    ]


def render_text(notice: Notice) -> str:
    """The whole message as plain text, for stdout and for logs."""
    v = notice.verdict
    c = notice.contract
    rule = "-" * 72
    head = f"CORONER  {v.incident_id}  {v.failure_type}  {c.pod.namespace}/{c.pod.name}"
    parts = [
        "=" * 72,
        head,
        rule,
        "OBSERVED   collected by the agent, verbatim, no model involvement",
        *(f"  {line}" for line in render_observed(c)),
        rule,
        "INFERRED   model output, every citation checked against the evidence above",
        *(f"  {line}" for line in render_inferred(v)),
        rule,
        "DECISION",
        *(f"  {line}" for line in render_decision(notice)),
        "=" * 72,
    ]
    return "\n".join(parts) + "\n"


# ------------------------------------------------------------- from a row


def verdict_from_row(row: dict[str, Any], threshold: float) -> DiagnoseResponse:
    """Rebuild the verdict a ledger row recorded."""
    from coroner_brain.evidence import EvidenceClass, ceiling

    evidence_class = str(row.get("evidence_class") or "")
    final = row.get("confidence_final")
    abstained = bool(row.get("abstained"))
    return DiagnoseResponse(
        incident_id=str(row["incident_id"]),
        failure_type=str(row["failure_type"]),
        outcome=Outcome(str(row["outcome"])),
        evidence_class=evidence_class,
        context_hash=str(row.get("context_hash") or ""),
        root_cause=str(row.get("root_cause") or ""),
        explanation=str(row.get("explanation") or ""),
        proposed_action=str(row.get("proposed_action") or ""),
        competing_hypothesis=str(row.get("competing_hypothesis") or ""),
        evidence=[Citation.model_validate(c) for c in json.loads(row.get("evidence_json") or "[]")],
        confidence_model=row.get("confidence_model"),
        confidence_final=final,
        confidence_ceiling=ceiling(EvidenceClass(evidence_class)) if evidence_class else None,
        abstained=abstained,
        abstain_reason=str(row.get("abstain_reason") or ""),
        approvable=(not abstained) and final is not None and float(final) >= threshold,
        validation_failures=list(json.loads(row.get("validation_failures") or "[]")),
    )


def notice_from_row(
    row: dict[str, Any], *, threshold: float, mode: Mode, public_url: str
) -> Notice | None:
    """Rebuild the delivered notice from a ledger row. None if the row predates
    schema 3 and holds no contract."""
    raw = row.get("contract_json") or ""
    if not raw:
        return None
    return Notice(
        contract=Contract.model_validate_json(raw),
        verdict=verdict_from_row(row, threshold),
        mode=mode,
        deadline=None,
        public_url=public_url,
    )


def render_status(row: dict[str, Any]) -> list[str]:
    """What has been recorded against the row since delivery."""
    lines: list[str] = []
    decision = row.get("decision")
    if decision:
        when = str(row.get("decision_at") or "")[:19].replace("T", " ")
        if decision == "rejected":
            lines.append(f"rejected at {when}: {row.get('decision_reason') or ''}")
        elif decision == "edited":
            lines.append(f"edited at {when}, action to execute: {row.get('decision_action') or ''}")
        elif decision == "expired":
            lines.append(f"expired at {when} with no decision")
        else:
            lines.append(f"{decision} at {when}")
    if row.get("shadow_rating"):
        lines.append(f"rated: {row['shadow_rating']}")
    if row.get("actual_cause"):
        lines.append(f"actual cause recorded: {row['actual_cause']}")
    return lines


# --------------------------------------------------------------------- sinks


class StdoutSink:
    """The default. Prints the message and returns."""

    name = "stdout"

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream

    def deliver(self, notice: Notice) -> None:
        stream = self._stream or sys.stdout
        stream.write(render_text(notice))
        stream.flush()


# ------------------------------------------------------------------- helpers


def _conf(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _age(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _event(e: Event) -> str:
    agg = f" {e.aggregated}" if e.aggregated else ""
    return f"{e.type} {e.reason}{agg}   {e.message}"
