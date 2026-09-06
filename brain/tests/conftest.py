"""Shared fixtures.

The contracts under tests/contracts/ are real output from coroner-agent against
a live kind cluster, not hand-written examples.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coroner_brain.contract import Contract

CONTRACTS = Path(__file__).parent / "contracts"


def load_contract(name: str) -> Contract:
    return Contract.model_validate_json((CONTRACTS / f"{name}.json").read_text())


@pytest.fixture
def crashloop() -> Contract:
    return load_contract("crashloopbackoff")


@pytest.fixture
def imagepull() -> Contract:
    return load_contract("imagepullbackoff")


@pytest.fixture
def oomkilled() -> Contract:
    return load_contract("oomkilled")


@pytest.fixture
def oom_init() -> Contract:
    return load_contract("oomkilled-during-init")


@pytest.fixture
def starterror_procready() -> Contract:
    """Recorded live 2026-09-06 from the same 2Mi probe as oomkilled-during-init.

    The runtime worded this restart's kill as "procReady not received" rather
    than naming memory, so the agent of the day classified it as an ordinary
    CrashLoopBackOff with exit 128 and empty logs. It is the first organically
    thin incident the gate has seen.
    """
    return load_contract("starterror-procready")


@pytest.fixture
def ledger(tmp_path: Path) -> object:
    """A throwaway ledger.

    Imported inside the fixture so this file stays importable by test modules
    that do not use the ledger at all.
    """
    from coroner_brain.ledger import Ledger

    return Ledger(tmp_path / "ledger.sqlite3")
