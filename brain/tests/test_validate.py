"""Tests for the citation validator.

This is section 4.2 control 2, the highest-value hallucination control, and it
is code rather than prompting. These tests are the proof it works.
"""

from __future__ import annotations

import json

from coroner_brain.contract import Contract
from coroner_brain.diagnosis import Citation, Diagnosis
from coroner_brain.validate import collected_text, resolve_path, validate


def _diagnosis(**overrides: object) -> Diagnosis:
    base = {
        "root_cause": "placeholder",
        "explanation": "placeholder",
        "proposed_action": "placeholder",
        "confidence": 0.5,
        "evidence": [],
        "competing_hypothesis": "",
    }
    base.update(overrides)
    return Diagnosis.model_validate(base)


def test_resolve_path_finds_real_fields(crashloop: Contract) -> None:
    payload = crashloop.model_dump(mode="json")
    found, value = resolve_path(payload, "container.last_terminated.exit_code")
    assert found
    assert value == 1

    found, value = resolve_path(payload, "events[0].reason")
    assert found
    assert value == "Scheduled"


def test_resolve_path_rejects_invented_fields(crashloop: Contract) -> None:
    payload = crashloop.model_dump(mode="json")
    for invented in [
        "container.memory_usage_bytes",
        "pod.replicas",
        "events[99].message",
        "logs.stack_trace",
        "",
    ]:
        found, _ = resolve_path(payload, invented)
        assert not found, f"{invented!r} should not resolve"


def test_accepts_a_truthful_diagnosis(crashloop: Contract) -> None:
    payload = crashloop.model_dump(mode="json")
    diagnosis = _diagnosis(
        root_cause="The container cannot reach its database.",
        explanation="The previous container logged a refused connection and exited nonzero.",
        proposed_action="Confirm the database service endpoint before restarting the Deployment.",
        evidence=[
            Citation(source="container", field="container.last_terminated.exit_code", value="1"),
            Citation(source="container", field="container.last_terminated.reason", value="Error"),
        ],
    )
    report = validate(diagnosis, payload)
    assert report.ok, report.failures


def test_rejects_an_empty_evidence_array(crashloop: Contract) -> None:
    report = validate(_diagnosis(evidence=[]), crashloop.model_dump(mode="json"))
    assert not report.ok
    assert any("empty" in f for f in report.failures)


def test_rejects_a_citation_to_a_field_that_does_not_exist(crashloop: Contract) -> None:
    diagnosis = _diagnosis(
        evidence=[Citation(source="container", field="container.memory_usage_bytes", value="512Mi")]
    )
    report = validate(diagnosis, crashloop.model_dump(mode="json"))
    assert not report.ok
    assert any("does not exist" in f for f in report.failures)


def test_rejects_a_citation_whose_value_is_wrong(crashloop: Contract) -> None:
    # The path is real; the value is not what the contract holds.
    diagnosis = _diagnosis(
        evidence=[
            Citation(source="container", field="container.last_terminated.exit_code", value="137")
        ]
    )
    report = validate(diagnosis, crashloop.model_dump(mode="json"))
    assert not report.ok
    assert any("does not match" in f for f in report.failures)


def test_rejects_a_fabricated_quoted_log_line(crashloop: Contract) -> None:
    diagnosis = _diagnosis(
        root_cause='The container logged "OutOfMemoryError: Java heap space" before exiting.',
        evidence=[
            Citation(source="container", field="container.last_terminated.exit_code", value="1")
        ],
    )
    report = validate(diagnosis, crashloop.model_dump(mode="json"))
    assert not report.ok
    assert any("does not appear in the collected evidence" in f for f in report.failures)


def test_accepts_a_quote_that_is_genuinely_present(crashloop: Contract) -> None:
    diagnosis = _diagnosis(
        root_cause='The log ends with "could not initialise connection pool after 1 attempt".',
        evidence=[Citation(source="logs", field="logs.content", value="connection refused")],
    )
    report = validate(diagnosis, crashloop.model_dump(mode="json"))
    assert report.ok, report.failures


def test_collected_text_excludes_env_values(crashloop: Contract) -> None:
    # Env values are never collected, so they cannot be cited or quoted. This
    # guards the structural guarantee rather than the redactor.
    text = collected_text(crashloop.model_dump(mode="json"))
    assert "sup3rs3cret" not in text


# --------------------------------------------------------------- adversarial


def test_stripped_logs_cannot_support_a_fabricated_log_citation(oomkilled: Contract) -> None:
    """The proof that the control works.

    Hand the validator a contract whose logs have been removed, together with a
    diagnosis that cites a log line anyway. Every fabricated citation must be
    rejected. If this test ever passes trivially, the control is not doing
    anything.
    """
    stripped = oomkilled.model_copy(deep=True)
    stripped.logs.available = False
    stripped.logs.empty = True
    stripped.logs.content = ""
    payload = stripped.model_dump(mode="json")

    fabrications = [
        Citation(source="logs", field="logs.content", value="OutOfMemoryError: heap exhausted"),
        Citation(source="logs", field="logs.previous_content", value="allocating 512MiB"),
        Citation(source="logs", field="logs.stack_trace", value="at com.example.Main"),
        Citation(source="container", field="container.memory_usage_peak", value="1.2Gi"),
    ]

    for fabricated in fabrications:
        diagnosis = _diagnosis(
            root_cause="The application leaked memory until the kernel killed it.",
            explanation='The log shows "OutOfMemoryError: heap exhausted" immediately before exit.',
            evidence=[fabricated],
        )
        report = validate(diagnosis, payload)
        assert not report.ok, f"validator accepted a fabricated citation: {fabricated!r}"

    # And a diagnosis citing only real fields still passes, so the validator is
    # rejecting fabrication rather than rejecting everything.
    honest = _diagnosis(
        root_cause="The container exceeded its memory limit.",
        explanation="The runtime reported the termination reason and the exit code directly.",
        evidence=[
            Citation(
                source="container", field="container.last_terminated.reason", value="OOMKilled"
            ),
            Citation(source="container", field="container.memory_limit", value="128Mi"),
        ],
    )
    assert validate(honest, payload).ok, validate(honest, payload).failures


# ------------------------------------------------------ values as the model saw them


def test_accepts_a_null_owner_cited_as_null(imagepull: Contract) -> None:
    """Recorded live against a bare pod.

    The contract the model received said ``"owner": null``. It cited exactly
    that and was rejected twice, because the validator rendered ``None`` as an
    empty string, and a correct image pull diagnosis was routed to abstention.
    """
    payload = imagepull.model_dump(mode="json")
    assert payload["owner"] is None
    diagnosis = _diagnosis(
        root_cause="The image cannot be pulled and there is no controller to patch.",
        evidence=[
            Citation(
                source="container", field="container.waiting_reason", value="ImagePullBackOff"
            ),
            Citation(source="owner", field="owner", value="null"),
        ],
    )
    report = validate(diagnosis, payload)
    assert report.ok, report.failures


def test_booleans_and_lists_are_compared_as_json(crashloop: Contract) -> None:
    payload = crashloop.model_dump(mode="json")
    diagnosis = _diagnosis(
        evidence=[
            Citation(source="node", field="node.memory_pressure", value="false"),
            Citation(source="container", field="container.ready", value="false"),
            Citation(
                source="container",
                field="container.command",
                value=json.dumps(payload["container"]["command"]),
            ),
        ],
    )
    report = validate(diagnosis, payload)
    assert report.ok, report.failures

    # A wrong boolean is still wrong.
    wrong = _diagnosis(
        evidence=[Citation(source="node", field="node.memory_pressure", value="true")]
    )
    assert not validate(wrong, payload).ok


def test_path_split_across_source_and_field_still_resolves(imagepull: Contract) -> None:
    """Recorded live: the model put ``events[2]`` in source and ``message`` in field.

    The path is the same path. It must resolve, and the value must still be
    checked against what that path holds.
    """
    payload = imagepull.model_dump(mode="json")
    right = _diagnosis(
        evidence=[
            Citation(source="events[2]", field="message", value=payload["events"][2]["message"]),
            Citation(source="container", field="waiting_reason", value="ImagePullBackOff"),
        ]
    )
    assert validate(right, payload).ok, validate(right, payload).failures

    wrong_value = _diagnosis(
        evidence=[Citation(source="container", field="waiting_reason", value="CrashLoopBackOff")]
    )
    report = validate(wrong_value, payload)
    assert not report.ok
    assert any("does not match" in f for f in report.failures)

    invented = _diagnosis(evidence=[Citation(source="logs", field="stack_trace", value="x")])
    report = validate(invented, payload)
    assert not report.ok
    assert any("does not exist" in f for f in report.failures)


def test_a_citation_copied_in_escaped_form_still_verifies(crashloop: Contract) -> None:
    """Recorded live: the model cited the JSON-escaped log text it was shown."""
    payload = crashloop.model_dump(mode="json")
    escaped_log = "[error]   dial tcp 10.96.31.14:5432: connect: connection refused\\n[fatal]"
    escaped_args = 'echo \\"[startup] orders-api booting\\"\\necho'
    diagnosis = _diagnosis(
        evidence=[
            Citation(source="logs", field="logs.content", value=escaped_log),
            Citation(source="container", field="container.args[0]", value=escaped_args),
        ]
    )
    report = validate(diagnosis, payload)
    assert report.ok, report.failures

    # Unescaping does not make a wrong value right.
    wrong = _diagnosis(
        evidence=[Citation(source="logs", field="logs.content", value="OutOfMemory\\nheap")]
    )
    assert not validate(wrong, payload).ok
