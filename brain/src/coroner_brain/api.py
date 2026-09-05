"""HTTP surface for the brain.

POST /diagnose accepts an evidence contract and returns the pipeline's verdict.
There is no Slack integration yet; this milestone ends at the verdict.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from coroner_brain import CONTRACT_VERSION, __version__
from coroner_brain.config import Settings, load_settings
from coroner_brain.contract import Contract
from coroner_brain.diagnosis import Citation, Outcome
from coroner_brain.graph import DiagnosisPipeline
from coroner_brain.ledger import Ledger
from coroner_brain.llm import LLMClient, OpenAICompatibleClient


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    contract_version: str
    model: str
    credentials_present: bool


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
    approvable: bool = False
    validation_failures: list[str] = []


app = FastAPI(
    title="coroner-brain",
    version=__version__,
    summary="Reasoning service for Coroner",
)


@lru_cache(maxsize=1)
def _settings() -> Settings:
    return load_settings()


@lru_cache(maxsize=1)
def _pipeline() -> DiagnosisPipeline:
    settings = _settings()
    if not settings.has_credentials:
        raise HTTPException(status_code=503, detail="no model credentials configured")
    client: LLMClient = OpenAICompatibleClient(
        api_key=settings.api_key or "",
        base_url=settings.base_url,
        model=settings.model,
    )
    return DiagnosisPipeline(
        client=client,
        ledger=Ledger(settings.ledger_path),
        abstention_threshold=settings.abstention_threshold,
        max_retries=settings.max_validation_retries,
    )


@app.get("/healthz")
def healthz() -> Health:
    """Liveness. Reports whether credentials are present without revealing them."""
    settings = _settings()
    return Health(
        status="ok",
        version=__version__,
        contract_version=CONTRACT_VERSION,
        model=settings.model,
        credentials_present=settings.has_credentials,
    )


@app.post("/diagnose")
def diagnose(contract: Contract) -> DiagnoseResponse:
    return build_response(_pipeline(), contract, _settings().abstention_threshold)


def build_response(
    pipeline: DiagnosisPipeline, contract: Contract, threshold: float
) -> DiagnoseResponse:
    """Run the pipeline and shape the verdict. Separated so tests can drive it."""
    from coroner_brain.evidence import EvidenceClass, ceiling

    state = pipeline.run(contract)
    outcome = Outcome(state.get("outcome") or Outcome.INSUFFICIENT_CONTEXT.value)
    abstained = outcome is Outcome.INSUFFICIENT_CONTEXT
    diagnosis = state.get("diagnosis")
    final = state.get("confidence_final")
    evidence_class = state.get("evidence_class", "")

    return DiagnoseResponse(
        incident_id=contract.incident_id,
        failure_type=contract.failure_type,
        outcome=outcome,
        evidence_class=evidence_class,
        root_cause="" if abstained or not diagnosis else diagnosis.root_cause,
        explanation="" if abstained or not diagnosis else diagnosis.explanation,
        proposed_action="" if abstained or not diagnosis else diagnosis.proposed_action,
        competing_hypothesis="" if abstained or not diagnosis else diagnosis.competing_hypothesis,
        evidence=[] if abstained or not diagnosis else diagnosis.evidence,
        confidence_model=state.get("confidence_model"),
        confidence_final=None if abstained else final,
        confidence_ceiling=ceiling(EvidenceClass(evidence_class)) if evidence_class else None,
        abstained=abstained,
        abstain_reason=state.get("abstain_reason", ""),
        # Section 4.2 control 4: below the threshold there is no approve
        # affordance at all, so a weak diagnosis cannot be approved by reflex.
        approvable=(not abstained) and (final is not None) and final >= threshold,
        validation_failures=state.get("validation_failures") or [],
    )
