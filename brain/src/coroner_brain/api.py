"""HTTP surface for the brain.

POST /diagnose accepts an evidence contract, runs the pipeline, writes the
ledger, delivers the verdict to the configured sink, and returns the verdict
to the agent. The ledger write happens before delivery, so a sink failure
cannot lose the record; section 5.1.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from coroner_brain import CONTRACT_VERSION, __version__
from coroner_brain.config import Settings, load_settings
from coroner_brain.contract import Contract
from coroner_brain.diagnosis import Outcome
from coroner_brain.graph import DiagnosisPipeline
from coroner_brain.ledger import Ledger
from coroner_brain.llm import LLMClient, OpenAICompatibleClient
from coroner_brain.sink import Mode, Notice, Sink, StdoutSink
from coroner_brain.verdict import DiagnoseResponse

log = logging.getLogger("coroner.brain")


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    contract_version: str
    model: str
    credentials_present: bool


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


@lru_cache(maxsize=1)
def _sink() -> Sink:
    return build_sink(_settings())


def build_sink(settings: Settings) -> Sink:
    """Pick the sink from configuration. stdout needs nothing and is the default."""
    if settings.sink == "stdout":
        return StdoutSink()
    raise HTTPException(status_code=503, detail=f"unknown sink {settings.sink!r}")


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
    settings = _settings()
    response = build_response(_pipeline(), contract, settings.abstention_threshold)
    return deliver(response, contract, settings, _sink())


def deliver(
    response: DiagnoseResponse, contract: Contract, settings: Settings, sink: Sink
) -> DiagnoseResponse:
    """Hand the verdict to the sink. The ledger row already exists.

    A sink that fails is logged and the verdict is still returned to the
    agent with delivered false: the record is safe, the human did not see
    it, and both facts are reported rather than one hiding the other.
    """
    mode: Mode = "live" if settings.is_promoted(contract.failure_type) else "shadow"
    notice = Notice(
        contract=contract,
        verdict=response,
        mode=mode,
        deadline=None,
        public_url=settings.public_url,
    )
    try:
        sink.deliver(notice)
    except Exception:  # a sink failure must not lose the verdict
        log.exception("sink %s failed for incident %s", sink.name, response.incident_id)
        return response.model_copy(update={"delivered": False, "mode": mode})
    return response.model_copy(update={"delivered": True, "mode": mode})


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
