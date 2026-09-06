"""The verdict returned to the agent and handed to every sink."""

from __future__ import annotations

from pydantic import BaseModel

from coroner_brain.diagnosis import Citation, Outcome


class DiagnoseResponse(BaseModel):
    """The verdict.

    Observed facts stay separate from inferred ones all the way to the caller.
    Section 4.2 control 5: a human must be able to evaluate the proposal against
    raw evidence without trusting the narrative.
    """

    incident_id: str
    failure_type: str
    outcome: Outcome
    evidence_class: str

    # Hash of the exact evidence the diagnosis rested on. The approval token
    # binds to it, so an approval cannot be replayed against other evidence.
    context_hash: str = ""

    root_cause: str = ""
    explanation: str = ""
    proposed_action: str = ""
    competing_hypothesis: str = ""
    evidence: list[Citation] = []

    confidence_model: float | None = None
    confidence_final: float | None = None
    confidence_ceiling: float | None = None

    abstained: bool = False
    abstain_reason: str = ""
    discarded: bool = False
    discard_reason: str = ""
    approvable: bool = False
    validation_failures: list[str] = []

    # Set on the way out. mode is shadow until the failure type is promoted
    # (section 5.5); delivered reports whether the sink accepted the message.
    mode: str = ""
    delivered: bool = False
