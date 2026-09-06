"""The diagnosis schema the model must produce, and the pipeline's outcome."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Outcome(StrEnum):
    """Terminal states of the graph.

    INSUFFICIENT_CONTEXT is a success, not an error path. Section 4.2 control 1:
    it is reported as its own outcome rather than hidden inside a failure count.
    """

    DIAGNOSED = "DIAGNOSED"
    INSUFFICIENT_CONTEXT = "INSUFFICIENT_CONTEXT"

    # The model did not answer inside the deadline, or the call failed. This
    # is neither a diagnosis nor an abstention: nothing was reasoned. The row
    # is recorded so the gap is visible, and it is excluded from accuracy.
    DISCARDED = "DISCARDED"


class Citation(BaseModel):
    """One claim traced to one collected field.

    ``field`` is a dotted path into the contract that was actually sent, and
    ``value`` must appear in what that path resolves to. Both are checked in
    code after generation; neither is taken on trust.
    """

    source: str = Field(
        description="Which part of the evidence: logs, events, container, pod, node, owner"
    )
    field: str = Field(
        description="Dotted path into the contract, for example container.last_terminated.exit_code"
    )
    value: str = Field(description="The value at that path, verbatim")


class Diagnosis(BaseModel):
    """The model's structured output. Every field is required."""

    root_cause: str = Field(description="One sentence naming the cause")
    explanation: str = Field(description="Two to four sentences of reasoning over the evidence")
    proposed_action: str = Field(
        description="One concrete remediation, targeting the owning workload"
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Self-assessed confidence")
    evidence: list[Citation] = Field(description="Every claim above, traced to collected fields")
    competing_hypothesis: str = Field(
        default="",
        description="A different cause the same evidence would support, or empty if none",
    )
