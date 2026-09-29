"""OpenTelemetry setup for the judge.

Deliberately *not* the agents' ``agentmesh.observability``: the judge has the
opposite wiring. An agent exports to the collector and needs traceparent
propagation across A2A hops; the judge exports **straight to Grafana** and must
never send its eval traces back through the collector, or they would be sampled
and fed to itself -- a loop that generates evaluations of evaluations.

That is why ``OTEL_EXPORTER_OTLP_ENDPOINT`` for this container points at
Grafana Cloud, while the agents' points at the collector.
"""

import logging

from agent_framework.observability import configure_otel_providers
from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry._logs import get_logger_provider
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor

logger = logging.getLogger(__name__)


def setup_observability(service_name: str) -> None:
    """Install providers, exporters and log correlation.

    Uses the Agent Framework's own configurator rather than hand-rolling the
    SDK so that ``JUDGE_MODE=llm`` gets gen_ai spans for the judge's own model
    call for free -- the judge's cost and latency become as observable as the
    agents' are.
    """
    configure_otel_providers(service_name=service_name)
    LoggingInstrumentor().instrument(set_logging_format=True, log_level=logging.INFO)
    logger.info("observability configured", extra={"service_name": service_name})


def instrument_app(app: FastAPI) -> None:
    """Trace the judge's HTTP surface, minus the parts that are pure noise.

    ``/v1/traces`` is excluded on purpose. It fires once per collector export,
    says nothing an operator wants, and -- being the busiest path in the
    service -- would swamp the eval traces that are the actual product.
    """
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="healthz,v1/traces",
        exclude_spans=["receive", "send"],
    )


def flush_telemetry() -> None:
    """Drain batched spans/metrics before exit, so the last verdicts survive SIGTERM."""
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider(), get_logger_provider()):
        force_flush = getattr(provider, "force_flush", None)
        if force_flush is not None:
            force_flush()
