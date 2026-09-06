"""Model client.

Groq exposes an OpenAI-compatible endpoint, so it is reached through the
OpenAI-compatible implementation rather than a bespoke one. The protocol exists
so the graph never depends on a vendor, and so tests can script responses
without a network call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Usage:
    """What one call cost. Tokens as the provider reported them; zero when
    the provider reported nothing."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
        )


@runtime_checkable
class LLMClient(Protocol):
    """Returns raw model output for a strict JSON schema request.

    Deliberately returns text rather than a parsed object. Malformed JSON is a
    validation failure handled by the graph's retry-then-abstain path, not an
    exception thrown from inside the client.

    A client may also expose ``last_usage``, the token count of the most
    recent call; the graph reads it when present so cost lands on the ledger
    row, and records zero when it is absent.
    """

    model_id: str

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> str: ...


class OpenAICompatibleClient:
    """Any OpenAI-compatible chat completions endpoint, including Groq."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 3000,
        timeout: float = 60.0,
        max_retries: int = 1,
    ) -> None:
        from openai import OpenAI

        # One retry, not the SDK's default of two, and a per-request timeout
        # well inside the pipeline's deadline. Recorded live: a single call
        # reached 999 seconds of wall clock through retries and backoff. The
        # graph's deadline is the bound that matters; these keep the client
        # from spending that budget on its own.
        self._client = OpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries
        )
        self.model_id = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self.last_usage = Usage()

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> str:
        response = self._client.chat.completions.create(
            model=self.model_id,
            # Temperature 0 for reproducibility and debuggability. It does not
            # reduce hallucination; the validator does that.
            temperature=self._temperature,
            max_completion_tokens=self._max_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        usage = getattr(response, "usage", None)
        self.last_usage = Usage(
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )
        return response.choices[0].message.content or ""


class ScriptedClient:
    """Returns queued responses in order. For tests, never for production."""

    def __init__(
        self, responses: list[str], model_id: str = "scripted", usage: Usage | None = None
    ) -> None:
        self._responses = list(responses)
        self.model_id = model_id
        self.calls: list[tuple[str, str]] = []
        # Tests can give a scripted client a fixed per-call usage so cost
        # arithmetic is exercised without a provider.
        self.last_usage = usage or Usage()

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> str:
        self.calls.append((system, user))
        if not self._responses:
            raise AssertionError("ScriptedClient ran out of responses")
        return self._responses.pop(0)
