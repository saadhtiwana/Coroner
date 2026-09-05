"""Mechanical verification of a diagnosis against the evidence actually sent.

Section 4.2 control 2. This is the highest-value hallucination control because
it targets the specific mechanism of the failure: an invented log line or a
misremembered field value does not survive a substring check against the
contract. It is code that runs after generation and does not depend on the
model's cooperation.

A diagnosis that fails here never reaches a human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from coroner_brain.diagnosis import Diagnosis

# Quoted spans in the prose are checked against the collected text. Anything
# shorter than this is too likely to be an ordinary word in quotes to be worth
# failing a diagnosis over.
MIN_QUOTED_LENGTH = 12

# Above this length a field is treated as free text, where a citation may
# quote an excerpt. At or below it the field is a scalar and the citation must
# match exactly: accepting a substring there let a cited exit code of "137" pass
# against an actual value of "1", because "1" is a substring of "137".
SCALAR_MAX_LENGTH = 40

_QUOTED = re.compile(rf'"([^"]{{{MIN_QUOTED_LENGTH},}})"')
_INDEX = re.compile(r"^(.*)\[(\d+)\]$")


@dataclass
class ValidationReport:
    ok: bool
    failures: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return "; ".join(self.failures)


def resolve_path(payload: dict[str, Any], path: str) -> tuple[bool, Any]:
    """Resolve a dotted path with optional list indices into the contract.

    Returns (found, value). A path the model invented resolves to not-found,
    which is the point.
    """
    current: Any = payload
    for raw_segment in path.split("."):
        segment = raw_segment.strip()
        if not segment:
            return False, None

        index: int | None = None
        match = _INDEX.match(segment)
        if match:
            segment, index = match.group(1), int(match.group(2))

        if segment:
            if not isinstance(current, dict) or segment not in current:
                return False, None
            current = current[segment]

        if index is not None:
            if not isinstance(current, list) or index >= len(current):
                return False, None
            current = current[index]

    return True, current


def collected_text(payload: dict[str, Any]) -> str:
    """Every piece of free text the agent actually collected, concatenated.

    A quoted string in the diagnosis must appear somewhere in here. This is what
    catches a fabricated stack trace or a plausible-sounding error line the
    workload never emitted.
    """
    parts: list[str] = []
    logs = payload.get("logs") or {}
    parts.append(str(logs.get("content") or ""))

    container = payload.get("container") or {}
    parts.append(str(container.get("waiting_message") or ""))
    parts.append(str(container.get("waiting_reason") or ""))
    parts.append(str(container.get("image") or ""))
    terminated = container.get("last_terminated") or {}
    parts.append(str(terminated.get("message") or ""))
    parts.append(str(terminated.get("reason") or ""))
    for key in ("command", "args"):
        for item in container.get(key) or []:
            parts.append(str(item))

    for event in payload.get("events") or []:
        parts.append(str(event.get("message") or ""))
        parts.append(str(event.get("reason") or ""))

    owner = payload.get("owner") or {}
    for key in ("name", "image", "kind"):
        parts.append(str(owner.get(key) or ""))

    pod = payload.get("pod") or {}
    for key in ("name", "namespace", "node_name"):
        parts.append(str(pod.get(key) or ""))

    return "\n".join(parts)


def _normalise(value: str) -> str:
    return " ".join(value.split()).strip().lower()


def _value_matches(cited: str, actual: str) -> bool:
    """Compare a cited value against what the contract holds.

    Scalars must match exactly. Only genuinely long free text, such as a log
    body or an event message, may be cited by excerpt. The comparison is never
    run the other way around: treating a shorter actual value as a match for a
    longer cited one would accept an exit code of "137" against a real "1".
    """
    if cited == actual:
        return True
    if len(actual) > SCALAR_MAX_LENGTH:
        return cited in actual
    return False


def validate(diagnosis: Diagnosis, payload: dict[str, Any]) -> ValidationReport:
    """Check every citation and every quoted string against the contract sent."""
    failures: list[str] = []

    if not diagnosis.evidence:
        failures.append("evidence array is empty; a diagnosis must cite the fields it rests on")

    for citation in diagnosis.evidence:
        found, value = resolve_path(payload, citation.field)
        if not found:
            failures.append(f"cited field {citation.field!r} does not exist in the contract")
            continue

        cited = _normalise(citation.value)
        actual = _normalise("" if value is None else str(value))
        if not cited:
            failures.append(f"citation for {citation.field!r} has an empty value")
            continue
        if not _value_matches(cited, actual):
            failures.append(
                f"cited value for {citation.field!r} does not match the contract "
                f"(cited {citation.value[:60]!r})"
            )

    haystack = _normalise(collected_text(payload))
    prose = " ".join([diagnosis.root_cause, diagnosis.explanation, diagnosis.proposed_action])
    for quoted in _QUOTED.findall(prose):
        if _normalise(quoted) not in haystack:
            failures.append(
                f"quoted text {quoted[:60]!r} does not appear in the collected evidence"
            )

    return ValidationReport(ok=not failures, failures=failures)
