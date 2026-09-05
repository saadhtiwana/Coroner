"""Pydantic mirror of the agent's evidence contract.

Kept structurally identical to agent/internal/contract/contract.go. The two are
separate declarations in separate languages, so a schema drift test is the only
thing keeping them honest; that test lives in tests/test_contract.py.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Owner(BaseModel):
    """The controlling workload. Remediation targets this, not the pod."""

    kind: str
    name: str
    image: str
    revision: str = ""


class Terminated(BaseModel):
    """The previous container's exit.

    Exit codes observed in the recorded fixtures: 1 (generic application
    failure), 137 (OOMKilled), 128 (StartError, container init OOM-killed).
    """

    exit_code: int
    reason: str
    signal: int = 0
    message: str = ""

    # Null when the container never started. Kubernetes reports a zero
    # timestamp there; the agent sends null rather than a date in 1970.
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Pod(BaseModel):
    namespace: str
    name: str
    uid: str
    node_name: str
    phase: str
    created_at: datetime
    age_seconds: float


class Container(BaseModel):
    name: str
    image: str
    image_id: str
    ready: bool
    restart_count: int
    command: list[str] = Field(default_factory=list)
    args: list[str] = Field(default_factory=list)

    waiting_reason: str = ""
    waiting_message: str = ""
    last_terminated: Terminated | None = None

    memory_limit: str = ""
    memory_request: str = ""
    cpu_limit: str = ""
    cpu_request: str = ""

    env_names: list[str] = Field(default_factory=list)
    has_liveness_probe: bool = False
    has_readiness_probe: bool = False

    crashes_per_minute: float = 0.0


class Event(BaseModel):
    """Retains the fields kubectl uses to render "x5 over 2m42s"."""

    type: str
    reason: str
    message: str

    # Both representations are preserved; either may be the populated one.
    count: int = 0
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    series_count: int = 0
    series_last_observed: datetime | None = None

    # Normalized across both, plus the reconstructed kubectl phrasing.
    occurrences: int = 1
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    aggregated: str = ""


class Logs(BaseModel):
    """Previous-container output.

    ``available`` and ``empty`` are deliberately distinct: "the container wrote
    nothing" and "the runtime discarded the container" are different facts and
    drive different confidence ceilings.
    """

    available: bool
    empty: bool
    from_previous: bool
    truncated: bool
    content: str


class NodeSummary(BaseModel):
    name: str
    ready: bool
    memory_pressure: bool
    disk_pressure: bool
    pid_pressure: bool


class Contract(BaseModel):
    """The complete evidence set for a single incident."""

    contract_version: str
    incident_id: str
    collected_at: datetime

    # The classified shape, not the pod's surface waiting reason.
    failure_type: str

    pod: Pod
    owner: Owner | None = None
    container: Container
    events: list[Event] = Field(default_factory=list)
    logs: Logs
    node: NodeSummary

    redacted_count: int = 0
    redacted_kinds: list[str] = Field(default_factory=list)
