"""OpenTelemetry tracing for the brain.

A trace should explain a decision, not just time it. Every span here carries
the facts that decided the outcome: the evidence class, the ceiling, the
final confidence, whether validation retried and why, the outcome, and what
the call cost. Reading the trace of one incident answers "why did Coroner
say that" without opening the ledger.

The console exporter is the default so tracing works with no backend at all;
spans go to stderr, so stdout still carries only what the sink writes. Set
CORONER_TRACING=otlp with OTEL_EXPORTER_OTLP_ENDPOINT to ship them, or
CORONER_TRACING=off to disable.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import context, propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Span, SpanKind, StatusCode

SERVICE = "coroner-brain"

# The provider this module hands out tracers from. It is kept here as well as
# installed globally, because the global refuses to be replaced once set, and
# because a test needs to install an in-memory exporter and assert on the
# spans. A trace that claims to explain a decision is worth testing.
_provider: TracerProvider | None = None
_mode = ""


def configure(
    mode: str | None = None,
    endpoint: str | None = None,
    exporter: SpanExporter | None = None,
    force: bool = False,
) -> str:
    """Install the tracer provider and return the mode in effect.

    Called once at startup. ``exporter`` and ``force`` exist for tests.
    """
    global _provider, _mode
    mode = (mode or os.environ.get("CORONER_TRACING", "console")).strip().lower()
    if _provider is not None and not force:
        return _mode
    if mode == "off" and exporter is None:
        _provider, _mode = None, "off"
        return _mode

    provider = TracerProvider(
        resource=Resource.create({"service.name": SERVICE, "service.version": _version()})
    )
    if exporter is not None:
        mode = "custom"
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif mode == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        target = f"{endpoint.rstrip('/')}/v1/traces" if endpoint else None
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=target)))
    else:
        mode = "console"
        provider.add_span_processor(
            SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr, formatter=_one_line))
        )
    trace.set_tracer_provider(provider)
    _provider, _mode = provider, mode
    return mode


def _version() -> str:
    from coroner_brain import __version__

    return __version__


def _one_line(span: Any) -> str:  # noqa: ANN401 - a ReadableSpan, kept loose for the exporter
    """One span per line: name, duration, and the attributes that explain it."""
    started = span.start_time or 0
    ended = span.end_time or started
    ms = (ended - started) / 1_000_000
    attrs = " ".join(f"{k}={v}" for k, v in sorted((span.attributes or {}).items()))
    ctx = span.get_span_context()
    return f"span {span.name} {ms:.1f}ms trace={ctx.trace_id:032x} {attrs}\n"


def tracer() -> trace.Tracer:
    if _provider is not None:
        return _provider.get_tracer(SERVICE)
    return trace.get_tracer(SERVICE)


@contextmanager
def span(
    name: str,
    kind: SpanKind = SpanKind.INTERNAL,
    **attributes: Any,  # noqa: ANN401 - attribute values are scalars
) -> Iterator[Span]:
    """A span whose attributes explain the step. An exception marks it failed."""
    with tracer().start_as_current_span(name, kind=kind) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        try:
            yield current
        except Exception as exc:
            current.record_exception(exc)
            current.set_status(StatusCode.ERROR, str(exc))
            raise


def annotate(**attributes: Any) -> None:  # noqa: ANN401 - attribute values are scalars
    """Add facts to the current span as they become known."""
    current = trace.get_current_span()
    for key, value in attributes.items():
        if value is not None:
            current.set_attribute(key, value)


def attach_incoming(headers: Mapping[str, str]) -> object:
    """Continue the agent's trace from the request headers."""
    return context.attach(propagate.extract(dict(headers)))


def capture() -> object:
    """The current trace context, for handing to a worker thread.

    OpenTelemetry's context is thread-local. A span started on a thread
    without this becomes the root of its own trace, which is how the model
    call was orphaned once: the timing was right and the parent was gone.
    """
    return context.get_current()


def resume(captured: object) -> object:
    """Continue a captured context on this thread. Returns a detach token."""
    return context.attach(captured)  # type: ignore[arg-type]


def detach(token: object) -> None:
    context.detach(token)  # type: ignore[arg-type]
