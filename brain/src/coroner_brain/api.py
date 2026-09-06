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

import json
import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from coroner_brain import CONTRACT_VERSION, __version__
from coroner_brain.approval import ApprovalError, ApprovalPipeline
from coroner_brain.config import Settings, load_settings
from coroner_brain.contract import Contract
from coroner_brain.diagnosis import Outcome
from coroner_brain.graph import DiagnosisPipeline
from coroner_brain.inflight import InFlightStore, build_store
from coroner_brain.ledger import (
    AlreadyLabelledError,
    Ledger,
    NotExecutableError,
    NotRatableError,
    UnknownIncidentError,
)
from coroner_brain.llm import LLMClient, OpenAICompatibleClient
from coroner_brain.sink import Mode, Notice, Sink, StdoutSink, notice_from_row
from coroner_brain.slack import (
    ACTION_ACTUAL_CAUSE,
    ACTION_APPROVE,
    ACTION_EDIT,
    ACTION_REJECT,
    RATING_ACTIONS,
    VIEW_ACTUAL_CAUSE,
    VIEW_EDIT,
    VIEW_REJECT,
    SlackConfig,
    SlackSink,
    actual_cause_view,
    edit_view,
    reject_view,
    verify_signature,
)
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


class ExecutionRequest(BaseModel):
    status: Literal["proposed", "refused", "executed", "failed"]
    detail: str = ""
    plan: dict[str, Any] | None = None


class ResolutionRequest(BaseModel):
    ready_within_sla: bool
    stayed_ready: bool
    resolved: bool
    detail: str = ""


class ApprovedResponse(BaseModel):
    """What the agent needs to verify the token and plan the action."""

    incident_id: str
    failure_type: str
    context_hash: str
    decision: str
    decision_action: str
    decision_at: str
    approval_token: str
    contract_json: str
    execution_status: str = ""


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
                model_deadline=self.settings.model_deadline_seconds,
                price_input_per_m=self.settings.price_input_per_m,
                price_output_per_m=self.settings.price_output_per_m,
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


@app.get("/incidents/approved")
def approved(services: ServicesDep, execute: bool = False) -> list[ApprovedResponse]:
    """Rows the agent may act on. The agent verifies the token itself."""
    return [
        ApprovedResponse(
            incident_id=str(r["incident_id"]),
            failure_type=str(r["failure_type"]),
            context_hash=str(r.get("context_hash") or ""),
            decision=str(r.get("decision") or ""),
            decision_action=str(r.get("decision_action") or ""),
            decision_at=str(r.get("decision_at") or ""),
            approval_token=str(r.get("approval_token") or ""),
            contract_json=str(r.get("contract_json") or ""),
            execution_status=str(r.get("execution_status") or ""),
        )
        for r in services.ledger.approved(include_proposed=execute)
    ]


@app.post("/incidents/{incident_id}/execution")
def execution(incident_id: str, body: ExecutionRequest, services: ServicesDep) -> LabelResponse:
    """What the agent did with the approval."""
    detail = body.detail
    if body.plan is not None:
        detail = json.dumps({"detail": body.detail, "plan": body.plan}, sort_keys=True)
    try:
        services.ledger.record_execution(incident_id, status=body.status, detail=detail)
    except UnknownIncidentError as exc:
        raise HTTPException(status_code=404, detail="no such incident") from exc
    except NotExecutableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AlreadyLabelledError as exc:
        raise HTTPException(
            status_code=409, detail=f"{exc.field} already recorded as {exc.existing!r}"
        ) from exc
    log.info("incident %s execution %s", incident_id, body.status)
    return LabelResponse(incident_id=incident_id, field="execution_status", value=body.status)


@app.post("/incidents/{incident_id}/resolution")
def resolution(incident_id: str, body: ResolutionRequest, services: ServicesDep) -> LabelResponse:
    """Section 5.2: did the workload recover and stay up after the action."""
    detail = json.dumps(
        {
            "ready_within_sla": body.ready_within_sla,
            "stayed_ready": body.stayed_ready,
            "detail": body.detail,
        },
        sort_keys=True,
    )
    try:
        services.ledger.record_resolution(incident_id, resolved=body.resolved, detail=detail)
    except UnknownIncidentError as exc:
        raise HTTPException(status_code=404, detail="no such incident") from exc
    except NotExecutableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AlreadyLabelledError as exc:
        raise HTTPException(
            status_code=409, detail=f"{exc.field} already recorded as {exc.existing!r}"
        ) from exc
    log.info("incident %s resolved=%s", incident_id, body.resolved)
    return LabelResponse(
        incident_id=incident_id, field="resolved_within_sla", value=str(body.resolved).lower()
    )


@app.get("/incidents/{incident_id}")
def incident(incident_id: str, services: ServicesDep) -> dict[str, Any]:
    """The ledger row. This is what the agent reads before it executes."""
    row = services.ledger.get(incident_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such incident")
    return row


# ------------------------------------------------------------ slack webhook


@app.post("/slack/interactions")
async def slack_interactions(
    request: Request,
    services: ServicesDep,
    x_slack_signature: Annotated[str, Header()] = "",
    x_slack_request_timestamp: Annotated[str, Header()] = "",
) -> JSONResponse:
    """Slack's interactivity endpoint: buttons and modal submissions.

    Every request is verified against the signing secret before the payload
    is read. The decision itself goes through the same approval graph as the
    JSON endpoint; this handler only translates Slack's shapes and updates
    the message afterwards.
    """
    sink = services.sink
    if not isinstance(sink, SlackSink):
        raise HTTPException(status_code=404, detail="the slack sink is not configured")

    body = await request.body()
    if not verify_signature(
        sink.config.signing_secret, x_slack_request_timestamp, body, x_slack_signature
    ):
        raise HTTPException(status_code=401, detail="bad slack signature")

    form = parse_qs(body.decode())
    try:
        payload = json.loads(form.get("payload", ["{}"])[0])
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="payload is not json") from exc

    kind = payload.get("type")
    if kind == "block_actions":
        return _slack_block_action(payload, services, sink)
    if kind == "view_submission":
        return _slack_view_submission(payload, services, sink)
    return JSONResponse({})


def _slack_block_action(
    payload: dict[str, Any], services: Services, sink: SlackSink
) -> JSONResponse:
    actions = payload.get("actions") or []
    if not actions:
        return JSONResponse({})
    action_id = str(actions[0].get("action_id", ""))
    incident_id = str(actions[0].get("value", ""))
    container = payload.get("container") or {}
    where = {
        "incident_id": incident_id,
        "channel": str(container.get("channel_id", "")),
        "ts": str(container.get("message_ts", "")),
    }
    trigger_id = str(payload.get("trigger_id", ""))
    user = str(
        (payload.get("user") or {}).get("username") or (payload.get("user") or {}).get("id") or ""
    )

    if action_id == ACTION_APPROVE:
        try:
            services.approvals.decide(incident_id, "approved")
        except ApprovalError as exc:
            _slack_note(sink, where, f"Could not approve: {exc.detail}")
            return JSONResponse({})
        log.info("incident %s approved via slack by %s", incident_id, user)
        _slack_refresh(services, sink, where)
        return JSONResponse({})

    if action_id in RATING_ACTIONS:
        try:
            services.ledger.label(incident_id, shadow_rating=RATING_ACTIONS[action_id])
        except (UnknownIncidentError, AlreadyLabelledError, NotRatableError) as exc:
            _slack_note(sink, where, f"Could not record the rating: {exc}")
            return JSONResponse({})
        _slack_refresh(services, sink, where)
        return JSONResponse({})

    if action_id == ACTION_REJECT:
        sink.client.open_view(trigger_id, reject_view(where))
        return JSONResponse({})
    if action_id == ACTION_EDIT:
        row = services.ledger.get(incident_id) or {}
        sink.client.open_view(trigger_id, edit_view(where, str(row.get("proposed_action") or "")))
        return JSONResponse({})
    if action_id == ACTION_ACTUAL_CAUSE:
        sink.client.open_view(trigger_id, actual_cause_view(where))
        return JSONResponse({})
    return JSONResponse({})


def _slack_view_submission(
    payload: dict[str, Any], services: Services, sink: SlackSink
) -> JSONResponse:
    view = payload.get("view") or {}
    callback = str(view.get("callback_id", ""))
    try:
        where = json.loads(view.get("private_metadata") or "{}")
    except json.JSONDecodeError:
        where = {}
    incident_id = str(where.get("incident_id", ""))
    values = ((view.get("state") or {}).get("values") or {}).get("text") or {}
    text = str((values.get("value") or {}).get("value") or "").strip()

    def reject_with(message: str) -> JSONResponse:
        # Slack renders this under the input without closing the modal.
        return JSONResponse({"response_action": "errors", "errors": {"text": message}})

    if not text:
        return reject_with("This cannot be empty.")

    try:
        if callback == VIEW_REJECT:
            services.approvals.decide(incident_id, "rejected", reason=text)
        elif callback == VIEW_EDIT:
            services.approvals.decide(incident_id, "edited", action=text)
        elif callback == VIEW_ACTUAL_CAUSE:
            services.ledger.label(incident_id, actual_cause=text)
        else:
            return JSONResponse({"response_action": "clear"})
    except ApprovalError as exc:
        return reject_with(exc.detail)
    except (UnknownIncidentError, AlreadyLabelledError, NotRatableError) as exc:
        return reject_with(str(exc))

    _slack_refresh(services, sink, where)
    return JSONResponse({"response_action": "clear"})


def _slack_refresh(services: Services, sink: SlackSink, where: dict[str, Any]) -> None:
    """Re-render the delivered message from the ledger row as the record."""
    incident_id = str(where.get("incident_id", ""))
    channel, ts = str(where.get("channel", "")), str(where.get("ts", ""))
    row = services.ledger.get(incident_id)
    if row is None or not channel or not ts:
        return
    settings = services.settings
    mode: Mode = "live" if settings.is_promoted(str(row["failure_type"])) else "shadow"
    notice = notice_from_row(
        row, threshold=settings.abstention_threshold, mode=mode, public_url=settings.public_url
    )
    if notice is None:
        return
    try:
        sink.refresh(channel, ts, notice, row)
    except Exception:  # the label is recorded; a failed redraw must not undo that
        log.exception("could not update slack message for %s", incident_id)


def _slack_note(sink: SlackSink, where: dict[str, Any], text: str) -> None:
    channel = str(where.get("channel", ""))
    if not channel:
        return
    try:
        sink.client.post_message(channel, text, [])
    except Exception:
        log.exception("could not post slack note")


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
    except NotRatableError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ------------------------------------------------------------------ helpers


def build_response(
    pipeline: DiagnosisPipeline, contract: Contract, threshold: float
) -> DiagnoseResponse:
    """Run the pipeline and shape the verdict. Separated so tests can drive it."""
    from coroner_brain.evidence import EvidenceClass, ceiling

    state = pipeline.run(contract)
    outcome = Outcome(state.get("outcome") or Outcome.INSUFFICIENT_CONTEXT.value)
    abstained = outcome is Outcome.INSUFFICIENT_CONTEXT
    discarded = outcome is Outcome.DISCARDED
    diagnosis = None if discarded else state.get("diagnosis")
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
        confidence_model=None if discarded else state.get("confidence_model"),
        confidence_final=None if abstained or discarded else final,
        confidence_ceiling=ceiling(EvidenceClass(evidence_class)) if evidence_class else None,
        abstained=abstained,
        abstain_reason=state.get("abstain_reason", ""),
        discarded=discarded,
        discard_reason=state.get("discard_reason", ""),
        # Section 4.2 control 4: below the threshold there is no approve
        # affordance at all, so a weak diagnosis cannot be approved by reflex.
        approvable=(not abstained)
        and (not discarded)
        and (final is not None)
        and final >= threshold,
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
