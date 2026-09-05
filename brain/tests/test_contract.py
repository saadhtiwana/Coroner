"""Guards against silent drift between the Go and Python contract declarations.

The agent and the brain declare the same schema twice, in two languages. Nothing
in the type system connects them, so these tests parse the Go source and compare
its JSON tags against the Pydantic field names.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coroner_brain import CONTRACT_VERSION
from coroner_brain.contract import Contract

_REPO_ROOT = Path(__file__).resolve().parents[2]
GO_CONTRACT = _REPO_ROOT / "agent" / "internal" / "contract" / "contract.go"

GO_TO_PY = {
    "Contract": Contract,
}


def _json_tags(go_source: str, struct: str) -> set[str]:
    """Return the json tag names declared on a Go struct, ignoring options."""
    match = re.search(rf"type {struct} struct \{{(.*?)\n\}}", go_source, re.S)
    if match is None:
        pytest.fail(f"struct {struct} not found in {GO_CONTRACT}")
    tags = re.findall(r'json:"([^"]+)"', match.group(1))
    return {tag.split(",", 1)[0] for tag in tags}


def test_go_contract_source_is_present() -> None:
    assert GO_CONTRACT.is_file(), f"expected the Go contract at {GO_CONTRACT}"


@pytest.mark.parametrize("struct", sorted(GO_TO_PY))
def test_top_level_fields_match_go_declaration(struct: str) -> None:
    go_fields = _json_tags(GO_CONTRACT.read_text(), struct)
    py_fields = set(GO_TO_PY[struct].model_fields)
    assert go_fields == py_fields, (
        f"{struct} drifted: only in Go {sorted(go_fields - py_fields)}, "
        f"only in Python {sorted(py_fields - go_fields)}"
    )


def test_contract_version_constants_agree() -> None:
    go_version = re.search(r'const Version = "([^"]+)"', GO_CONTRACT.read_text())
    assert go_version is not None
    assert go_version.group(1) == CONTRACT_VERSION


def test_contract_round_trips() -> None:
    now = datetime.now(UTC)
    payload = {
        "contract_version": CONTRACT_VERSION,
        "incident_id": "inc-1",
        "collected_at": now,
        "pod": {
            "namespace": "default",
            "name": "orders-api-0",
            "uid": "9e163c88-727f-4399-9b23-008a89f88eac",
            "node_name": "coroner-worker2",
            "phase": "Running",
            "created_at": now,
            "age_seconds": 162.0,
        },
        "container": {
            "name": "crasher",
            "image": "redis:alpine",
            "image_id": "docker.io/library/redis@sha256:deadbeef",
            "ready": False,
            "restart_count": 4,
            "waiting_reason": "CrashLoopBackOff",
            "last_terminated": {
                "exit_code": 1,
                "reason": "Error",
                "started_at": now,
                "finished_at": now,
            },
            "crashes_per_minute": 1.48,
        },
        "logs": {
            "available": True,
            "empty": False,
            "from_previous": True,
            "truncated": False,
            "content": "[fatal] could not initialise connection pool",
        },
        "node": {
            "name": "coroner-worker2",
            "ready": True,
            "memory_pressure": False,
            "disk_pressure": False,
            "pid_pressure": False,
        },
    }
    contract = Contract.model_validate(payload)
    assert contract.container.last_terminated is not None
    assert contract.container.last_terminated.exit_code == 1
    assert Contract.model_validate(contract.model_dump(mode="json")) == contract
