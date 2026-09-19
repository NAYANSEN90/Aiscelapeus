"""OpenTelemetry wiring for the triage agent.

Mentor gap #1: a life-critical LLM pipeline with no tracing cannot be audited or
debugged. Every stage of a turn -- STT, Moss retrieval, LLM, TTS, escalation --
opens a span under a single session-scoped correlation ID, so one incident can be
replayed end to end.

Exports to OTLP when OTEL_EXPORTER_OTLP_ENDPOINT is set (Arize Phoenix, LangSmith,
Grafana Tempo, Jaeger -- anything speaking OTLP/HTTP). Falls back to a compact
console exporter so traces are visible with zero setup during a demo.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, Status, StatusCode

from .config import TelemetryConfig

logger = logging.getLogger(__name__)

_INITIALISED = False
_TRACER: trace.Tracer | None = None


class _CompactConsoleExporter(SpanExporter):
    """One readable line per span instead of a page of JSON.

    The default ConsoleSpanExporter drowns the agent log during a live demo.
    """

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for span in spans:
            if span.end_time is None or span.start_time is None:
                duration_ms = 0.0
            else:
                duration_ms = (span.end_time - span.start_time) / 1_000_000
            attrs = span.attributes or {}
            interesting = {
                k: v
                for k, v in attrs.items()
                if k.startswith(("moss.", "llm.", "triage.", "session.", "stt.", "tts."))
            }
            detail = " ".join(f"{k}={v}" for k, v in interesting.items())
            logger.info("trace %-28s %7.2fms %s", span.name, duration_ms, detail)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def setup_telemetry(config: TelemetryConfig) -> trace.Tracer:
    """Idempotently install the global tracer provider."""
    global _INITIALISED, _TRACER

    if _INITIALISED and _TRACER is not None:
        return _TRACER

    resource = Resource.create(
        {
            "service.name": config.service_name,
            "service.namespace": "aiscelapeus",
            "deployment.environment": config.environment,
        }
    )
    provider = TracerProvider(resource=resource)

    if config.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{config.otlp_endpoint}/v1/traces"))
        )
        logger.info("OpenTelemetry exporting to %s", config.otlp_endpoint)

    if config.console_export:
        provider.add_span_processor(SimpleSpanProcessor(_CompactConsoleExporter()))

    trace.set_tracer_provider(provider)
    _TRACER = trace.get_tracer("aiscelapeus.agent")
    _INITIALISED = True
    return _TRACER


def tracer() -> trace.Tracer:
    if _TRACER is None:
        return trace.get_tracer("aiscelapeus.agent")
    return _TRACER


def prompt_hash(prompt: str) -> str:
    """Stable short hash so a trace can be tied back to the exact prompt text.

    We log the hash, never the prompt body -- prompts carry patient utterances.
    """
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """Open a span, record wall-clock duration, mark exceptions as errors."""
    started = time.perf_counter()
    with tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        try:
            yield current
        except Exception as exc:  # noqa: BLE001 - we re-raise after recording
            current.record_exception(exc)
            current.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        finally:
            current.set_attribute(
                "duration_ms", round((time.perf_counter() - started) * 1000, 3)
            )


def record_llm_usage(
    current: Span,
    *,
    model: str,
    system_prompt: str,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    """Attach the attributes an LLM observability platform expects."""
    current.set_attribute("llm.model", model)
    current.set_attribute("llm.prompt_hash", prompt_hash(system_prompt))
    if prompt_tokens is not None:
        current.set_attribute("llm.usage.prompt_tokens", prompt_tokens)
    if completion_tokens is not None:
        current.set_attribute("llm.usage.completion_tokens", completion_tokens)
    if prompt_tokens is not None and completion_tokens is not None:
        current.set_attribute("llm.usage.total_tokens", prompt_tokens + completion_tokens)
