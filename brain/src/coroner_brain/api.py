"""HTTP surface for the brain.

POST /diagnose accepts an evidence contract, runs the pipeline, writes the
ledger, parks the verdict for approval when approval is on offer, delivers it
to the configured sink, and returns it to the agent. The ledger write happens
before delivery, so a sink failure cannot lose the record; section 5.1.

The decision endpoints resume the parked incident. They are the same
endpoints whatever the sink: stdout renders them as curl lines, Slack will
call them from its interaction handler.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from coroner_brain import CONTRACT_VERSION, __version__
from coroner_brain.approval import ApprovalError, ApprovalPipeline
from coroner_brain.config import Settings, load_settings
from coroner_brain.contract import Contract
from coroner_brain.diagnosis import Outcome
from coroner_brain.graph import DiagnosisPipeline
from coroner_brain.inflight import InFlightStore, build_store
from coroner_brain.ledger import AlreadyLabelledError, Ledger, UnknownIncidentError
from coroner_brain.llm import LLMClient, OpenAICompatibleClient
from coroner_brain.sink import Mode, Notice, Sink, StdoutSink
from coroner_brain.slack import SlackConfig, SlackSink
from coroner_brain.verdict import DiagnoseResponse

log = logging.getLogger("coroner.brain")

# How often parked incidents are checked against their deadline.
EXPIRY_SWEEP_SECONDS = 30.0


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    contract_version: str
    model: str
    credentials_present: bool
    sink: str
    inflight_store: str
    promoted_types: list[str]


class DecisionRequest(BaseModel):
    decision: Literal["approved", "rejected", "edited"]
    # Required for a rejection. Section 5.2: the most valuable label.
    reason: str = ""
    # Required for an edit: the corrected action, which is what will execute.
    action: str = ""


class DecisionResponse(BaseModel):
    incident_id: str
    decision: str
    decided_at: str
    action: str = ""
    approval_token: str = ""


class RatingRequest(BaseModel):
    rating: Literal["would_approve", "would_reject", "unsure"]


class ActualCauseRequest(BaseModel):
    actual_cause: str = Field(min_length=1, max_length=2000)


class LabelResponse(BaseModel):
    incident_id: str
    field: str
    value: str


class PendingResponse(BaseModel):
    incident_id: str
    failure_type: str
    proposed_action: str
    confidence_final: float
    deadline: str


# ------------------------------------------------------------------ services


@dataclass
class Services:
    """Everything an endpoint needs, built once. Tests build their own."""

    settings: Settings
    ledger: Ledger
    sink: Sink
    store: InFlightStore
    approvals: ApprovalPipeline
    client: LLMClient | None = None
    _pipeline: DiagnosisPipeline | None = field(default=None, repr=False)

    @property
    def pipeline(self) -> DiagnosisPipeline:
        if self._pipeline is None:
            if self.client is None:
                if not self.settings.has_credentials:
                    raise HTTPException(status_code=503, detail="no model credentials configured")
                self.client = OpenAICompatibleClient(
                    api_key=self.settings.api_key or "",
                    base_url=self.settings.base_url,
                    model=self.settings.model,
                )
            self._pipeline = DiagnosisPipeline(
                client=self.client,
                ledger=self.ledger,
                abstention_threshold=self.settings.abstention_threshold,
                max_retries=self.settings.max_validation_retries,
            )
        return self._pipeline


def build_sink(settings: Settings) -> Sink:
    """Pick the sink from configuration. stdout needs nothing and is the default."""
    if settings.sink == "stdout":
        return StdoutSink()
    if settings.sink == "slack":
        missing = [
            name
            for name, value in (
                ("CORONER_SLACK_BOT_TOKEN", settings.slack_bot_token),
                ("CORONER_SLACK_CHANNEL", settings.slack_channel),
                ("CORONER_SLACK_SIGNING_SECRET", settings.slack_signing_secret),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"CORONER_SINK=slack needs {', '.join(missing)}")
        return SlackSink(
            SlackConfig(
                bot_token=settings.slack_bot_token,
                channel=settings.slack_channel,
                signing_secret=settings.slack_signing_secret,
            )
        )
    raise ValueError(f"unknown sink {settings.sink!r}; expected stdout or slack")


def build_services(settings: Settings, client: LLMClient | None = None) -> Services:
    ledger = Ledger(settings.ledger_path)
    store = build_store(settings.redis_url)
    if store.name == "memory":
        log.warning("no CORONER_REDIS_URL: in-flight incidents will not survive a restart")
    if settings.approval_secret_generated:
        log.warning(
            "no CORONER_APPROVAL_SECRET: approval tokens are signed with a per-process secret "
            "that no agent can verify"
        )
    approvals = ApprovalPipeline(
        store=store,
        ledger=ledger,
        secret=settings.approval_secret,
        ttl_seconds=settings.approval_ttl_seconds,
    )
    return Services(
        settings=settings,
        ledger=ledger,
        sink=build_sink(settings),
        store=store,
        approvals=approvals,
        client=client,
    )


@lru_cache(maxsize=1)
def get_services() -> Services:
    return build_services(load_settings())


ServicesDep = Annotated[Services, Depends(get_services)]


# ------------------------------------------------------------------ lifespan


class Sweeper:
    """Records expired for parked incidents whose deadline has passed."""

    def __init__(self, approvals: ApprovalPipeline, interval: float) -> None:
        self._approvals = approvals
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="coroner-expiry", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                for incident_id in self._approvals.expire_overdue():
                    log.info("incident %s expired without a decision", incident_id)
            except Exception:  # the sweep must survive a transient store error
                log.exception("expiry sweep failed")


@asynccontextmanager
async def lifespan(_: FastAPI) -> Any:  # noqa: ANN401 - FastAPI's lifespan contract
    services = get_services()
    sweeper = Sweeper(services.approvals, EXPIRY_SWEEP_SECONDS)
    sweeper.start()
    log.info(
        "coroner-brain ready: sink=%s store=%s promoted=%s",
        services.sink.name,
        services.store.name,
        sorted(services.settings.promoted_types) or "none (all types in shadow)",
    )
    try:
        yield
    finally:
        sweeper.stop()


app = FastAPI(
    title="coroner-brain",
    version=__version__,
    summary="Reasoning service for Coroner",
    lifespan=lifespan,
)


# ----------------------------------------------------------------- endpoints


@app.get("/healthz")
def healthz(services: ServicesDep) -> Health:
    """Liveness. Reports whether credentials are present without revealing them."""
    settings = services.settings
    return Health(
        status="ok",
        version=__version__,
        contract_version=CONTRACT_VERSION,
        model=settings.model,
        credentials_present=settings.has_credentials,
        sink=services.sink.name,
        inflight_store=services.store.name,
        promoted_types=sorted(settings.promoted_types),
    )


@app.post("/diagnose")
def diagnose(contract: Contract, services: ServicesDep) -> DiagnoseResponse:
    response = build_response(services.pipeline, contract, services.settings.abstention_threshold)
    return deliver(response, contract, services)


@app.post("/incidents/{incident_id}/decision")
def decide(incident_id: str, body: DecisionRequest, services: ServicesDep) -> DecisionResponse:
    """Resume the parked incident with a human decision."""
    try:
        outcome = services.approvals.decide(
            incident_id, body.decision, reason=body.reason, action=body.action
        )
    except ApprovalError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    log.info("incident %s %s", incident_id, outcome.decision)
    return DecisionResponse(
        incident_id=incident_id,
        decision=outcome.decision,
        decided_at=outcome.decided_at.isoformat(),
        action=outcome.action,
        approval_token=outcome.approval_token,
    )


@app.post("/incidents/{incident_id}/rating")
def rate(incident_id: str, body: RatingRequest, services: ServicesDep) -> LabelResponse:
    """Shadow mode label. Section 5.5: a judgement, never an action."""
    _label(services.ledger, incident_id, shadow_rating=body.rating)
    return LabelResponse(incident_id=incident_id, field="shadow_rating", value=body.rating)


@app.post("/incidents/{incident_id}/actual-cause")
def actual_cause(
    incident_id: str, body: ActualCauseRequest, services: ServicesDep
) -> LabelResponse:
    """What actually happened, from whoever resolved it. Section 5.3."""
    _label(services.ledger, incident_id, actual_cause=body.actual_cause.strip())
    return LabelResponse(incident_id=incident_id, field="actual_cause", value=body.actual_cause)


@app.get("/incidents/pending")
def pending(services: ServicesDep) -> list[PendingResponse]:
    return [
        PendingResponse(
            incident_id=p.incident_id,
            failure_type=p.failure_type,
            proposed_action=p.proposed_action,
            confidence_final=p.confidence_final,
            deadline=p.deadline.isoformat(),
        )
        for p in services.approvals.pending()
    ]


@app.get("/incidents/{incident_id}")
def incident(incident_id: str, services: ServicesDep) -> dict[str, Any]:
    """The ledger row. This is what the agent reads before it executes."""
    row = services.ledger.get(incident_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such incident")
    return row


def _label(
    ledger: Ledger,
    incident_id: str,
    *,
    shadow_rating: str | None = None,
    actual_cause: str | None = None,
) -> None:
    try:
        ledger.label(incident_id, shadow_rating=shadow_rating, actual_cause=actual_cause)
    except UnknownIncidentError as exc:
        raise HTTPException(status_code=404, detail="no such incident") from exc
    except AlreadyLabelledError as exc:
        raise HTTPException(
            status_code=409, detail=f"{exc.field} already recorded as {exc.existing!r}"
        ) from exc


# ------------------------------------------------------------------ helpers


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
        context_hash=state.get("context_hash", ""),
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


def deliver(response: DiagnoseResponse, contract: Contract, services: Services) -> DiagnoseResponse:
    """Park the verdict if it offers approval, then hand it to the sink.

    The ledger row already exists. A sink that fails is logged and the
    verdict is still returned to the agent with delivered false: the record
    is safe, the human did not see it, and both facts are reported rather
    than one hiding the other.
    """
    settings = services.settings
    mode: Mode = "live" if settings.is_promoted(contract.failure_type) else "shadow"
    parked = services.approvals.register(contract, response, mode)
    notice = Notice(
        contract=contract,
        verdict=response,
        mode=mode,
        deadline=parked.deadline if parked else None,
        public_url=settings.public_url,
    )
    try:
        services.sink.deliver(notice)
    except Exception:  # a sink failure must not lose the verdict
        log.exception("sink %s failed for incident %s", services.sink.name, response.incident_id)
        return response.model_copy(update={"delivered": False, "mode": mode})
    return response.model_copy(update={"delivered": True, "mode": mode})
