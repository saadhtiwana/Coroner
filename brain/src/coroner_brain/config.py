"""Runtime configuration for the brain.

The credential is read from the environment, with brain/.env loaded when
present. brain/.env is gitignored; brain/.env.example documents the names and
carries no values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Groq exposes an OpenAI-compatible endpoint, so it is reached through the
# OpenAI-compatible client rather than a bespoke one. See docs/DESIGN.md 6.7.
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"

# The largest reasoning model Groq serves. Of the models available, the others
# are speech, text to speech, safety classifiers, or an agentic system whose
# nondeterminism the validator would have to fight.
DEFAULT_MODEL = "openai/gpt-oss-120b"

# Below this, the eventual Slack message carries no approve button at all.
# Section 4.2 control 4: the affordance is absent, not disabled.
ABSTENTION_THRESHOLD = 0.5

# A response that fails validation is retried once, then abstains.
MAX_VALIDATION_RETRIES = 1

# Output sink. stdout is the default and needs no configuration; Slack is
# opt-in and is never a prerequisite for seeing the system work. Section 7.2.
DEFAULT_SINK = "stdout"

# How long an approvable diagnosis waits for a decision before it is recorded
# as expired. Section 5.2 tracks expiry as its own label.
APPROVAL_TTL_SECONDS = 1800

# Where the decision endpoints are reachable from a human's terminal. Only
# used to render instructions; the brain does not call itself.
DEFAULT_PUBLIC_URL = "http://localhost:8000"


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    base_url: str
    model: str
    ledger_path: Path
    abstention_threshold: float
    max_validation_retries: int

    sink: str
    public_url: str
    approval_ttl_seconds: int

    # Failure types promoted out of shadow mode. Section 5.5: a type stays in
    # shadow, rating only, until it clears its prediction over 20 incidents.
    # Empty means every type is in shadow, which is the honest default for a
    # system with no accuracy data yet.
    promoted_types: frozenset[str]

    # Optional. Without it in-flight incidents live in process memory and do
    # not survive a restart, which is logged at startup.
    redis_url: str | None

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key)

    def is_promoted(self, failure_type: str) -> bool:
        return failure_type in self.promoted_types


def load_settings(env_file: Path | None = None) -> Settings:
    """Read settings from the environment, loading brain/.env when it exists."""
    if env_file is None:
        env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)

    return Settings(
        api_key=os.environ.get("GROQ_API_KEY") or None,
        base_url=os.environ.get("CORONER_LLM_BASE_URL", DEFAULT_BASE_URL),
        model=os.environ.get("CORONER_MODEL") or DEFAULT_MODEL,
        ledger_path=Path(os.environ.get("CORONER_LEDGER_PATH", "coroner-ledger.sqlite3")),
        abstention_threshold=float(
            os.environ.get("CORONER_ABSTENTION_THRESHOLD", ABSTENTION_THRESHOLD)
        ),
        max_validation_retries=int(
            os.environ.get("CORONER_MAX_VALIDATION_RETRIES", MAX_VALIDATION_RETRIES)
        ),
        sink=os.environ.get("CORONER_SINK", DEFAULT_SINK).strip().lower() or DEFAULT_SINK,
        public_url=os.environ.get("CORONER_PUBLIC_URL", DEFAULT_PUBLIC_URL).rstrip("/"),
        approval_ttl_seconds=int(
            os.environ.get("CORONER_APPROVAL_TTL_SECONDS", APPROVAL_TTL_SECONDS)
        ),
        promoted_types=_csv(os.environ.get("CORONER_PROMOTED_TYPES", "")),
        redis_url=os.environ.get("CORONER_REDIS_URL") or None,
    )


def _csv(raw: str) -> frozenset[str]:
    return frozenset(part.strip() for part in raw.split(",") if part.strip())
