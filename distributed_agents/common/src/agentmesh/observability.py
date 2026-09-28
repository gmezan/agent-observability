"""OpenTelemetry wiring shared by every agent host.

Single-host agents get one trace for free: everything runs in one process, so
the span context lives in a contextvar and nesting just works. Once agents are
split across hosts, the only thing tying them together is the W3C
``traceparent`` header, so each host has to do two things:

* **extract** it from inbound A2A HTTP requests (FastAPI instrumentation), and
* **inject** it into outbound A2A/model HTTP calls (httpx instrumentation).

With both sides instrumented, a tool call that crosses a host boundary keeps
the same ``trace_id`` and Tempo renders triage -> billing/tech as one trace.
"""

import logging

from agent_framework.observability import configure_otel_providers
from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry._logs import get_logger_provider
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor

logger = logging.getLogger(__name__)


def setup_observability(service_name: str) -> None:
    """Configure providers and process-wide HTTP instrumentation.

    Call once at startup, before any agent or HTTP client is constructed.
    """
    # Reads OTEL_EXPORTER_OTLP_* / ENABLE_SENSITIVE_DATA from the environment
    # and installs the tracer, meter and logger providers, plus the Agent
    # Framework's own gen_ai instrumentation (invoke_agent / chat /
    # execute_tool spans).
    configure_otel_providers(service_name=service_name)

    # Outbound leg: injects traceparent into A2A calls to peer agents (and
    # into model calls, since the OpenAI SDK is httpx-based too).
    HTTPXClientInstrumentor().instrument()

    # Stamps trace_id/span_id onto every log record, so logs shipped via OTLP
    # can be pivoted to the corresponding Tempo trace. This installs the root
    # log format, so nothing may call logging.basicConfig() before it.
    LoggingInstrumentor().instrument(set_logging_format=True, log_level=logging.INFO)

    logger.info("observability configured", extra={"service_name": service_name})


def instrument_app(app: FastAPI) -> None:
    """Inbound leg: extracts traceparent from incoming A2A requests.

    The extracted context becomes the active span for the request, and because
    the A2A request handler dispatches agent execution with
    ``asyncio.create_task`` -- which copies the current contextvars -- the
    agent's spans are parented to the caller's span rather than starting a new
    trace.

    ASGI ``http receive``/``http send`` spans are dropped: they add two spans
    per request that say nothing about the agent, and the point of this trace
    is the agent's reasoning, not the transport's framing.
    """
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz", exclude_spans=["receive", "send"])


def flush_telemetry() -> None:
    """Drain batched spans/metrics/logs before the process exits.

    Containers get a SIGTERM and a short grace period; without an explicit
    flush the last batch -- usually the request you were debugging -- is lost.
    """
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider(), get_logger_provider()):
        force_flush = getattr(provider, "force_flush", None)
        if force_flush is not None:
            force_flush()
