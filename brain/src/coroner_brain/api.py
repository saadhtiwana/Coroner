"""HTTP surface for the brain.

Scaffold only: the diagnosis graph is not implemented. The next milestone is
agent-side detection, which does not call this service at all.
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from coroner_brain import CONTRACT_VERSION, __version__


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    contract_version: str


app = FastAPI(
    title="coroner-brain",
    version=__version__,
    summary="Reasoning service for Coroner",
)


@app.get("/healthz")
def healthz() -> Health:
    """Liveness. Deliberately does no dependency checking."""
    return Health(status="ok", version=__version__, contract_version=CONTRACT_VERSION)
