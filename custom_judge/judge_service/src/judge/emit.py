"""Emit a verdict as its own trace, linked back to the trace it scored.

This is the one piece the architecture doc is emphatic about, so it is worth
restating here where the code is: the eval span is **not** injected into the
original trace.

Re-using the original ``trace_id`` does work -- Tempo accepts a span that
arrives after the rest of its trace was flushed. But it forces the ingester to
open a second live entry for that id, which flushes to a separate block, so one
logical trace ends up fragmented across blocks; every read then costs extra
bloom lookups until compaction catches up. Done once, that is nothing. Done for
every sampled trace, forever, it is a steady drip of orphan blocks.

So: a normal, independent trace, plus an OTel ``Link`` -- the primitive defined
for exactly this ("causally related to another trace, but not its child").
Grafana renders links as a clickable reference, so the operator still gets
one-click navigation in both directions, at zero storage cost.
"""

from __future__ import annotations

import logging

from opentelemetry import context, metrics, trace
from opentelemetry.trace import Link, SpanContext, TraceFlags
from opentelemetry.trace.span import format_trace_id

from .judges import Verdict
from .rubric import RUBRIC_VERSION, SCORE_MAX, SCORE_MIN
from .summary import TraceSummary

logger = logging.getLogger(__name__)

tracer = trace.get_tracer("judge.evaluation")
meter = metrics.get_meter("judge.evaluation")

# A score is only meaningful as a distribution -- "was this one trace good?" is
# a trace question, "is the system getting worse?" is a metric question. Keep
# the attributes low-cardinality: never the trace id.
_score_histogram = meter.create_histogram(
    name="gen_ai.evaluation.score",
    description="Judge score per evaluated trace, by evaluator",
    unit="1",
)


def _source_link(summary: TraceSummary) -> Link:
    """A Link to the root span of the trace being scored.

    ``is_remote=True`` because this context was reconstructed from the wire,
    and the SAMPLED flag because the trace demonstrably was sampled -- it is
    the reason the collector forwarded it here at all.
    """
    return Link(
        SpanContext(
            trace_id=int(summary.trace_id, 16),
            span_id=int(summary.root_span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        ),
        attributes={"gen_ai.evaluation.link.role": "evaluated_trace"},
    )


def emit_evaluation(summary: TraceSummary, verdicts: list[Verdict], *, judge_mode: str, threshold: float) -> str:
    """Publish one evaluation as a new trace. Returns its trace id, in hex.

    Structure: one ``gen_ai.evaluate`` root per evaluated trace, with one
    ``gen_ai.evaluation`` child per evaluator. Both names are static -- the
    evaluator goes in ``gen_ai.evaluation.name``, never in the span name, or
    aggregation-by-operation in Tempo breaks.
    """
    link = _source_link(summary)

    # Detach whatever context is ambient before opening the root span. Without
    # this, a caller that happens to be inside a span -- an instrumented OTLP
    # POST handler, a retry wrapper -- would silently make the "independent"
    # eval trace a child of it, which is precisely the merge this design avoids.
    token = context.attach(context.Context())
    try:
        with tracer.start_as_current_span("gen_ai.evaluate", links=[link]) as root:
            root.set_attributes(
                {
                    # The pivot back to the evaluated trace, as a plain
                    # attribute as well as the Link: TraceQL can filter on an
                    # attribute, and Grafana can turn it into a derived field.
                    "gen_ai.evaluation.source_trace_id": summary.trace_id,
                    "gen_ai.evaluation.source_span_id": summary.root_span_id,
                    # Who and what was judged -- enough to slice a dashboard by
                    # model or by agent without joining back to the source trace.
                    "gen_ai.evaluation.source_service": summary.root_service,
                    "gen_ai.agent.name": ", ".join(summary.agents) or "unknown",
                    "gen_ai.request.model": ", ".join(summary.models) or "unknown",
                    # Repo-local namespace, deliberately not gen_ai.*: these
                    # describe the judge run, not the GenAI operation.
                    "judge.mode": judge_mode,
                    "judge.rubric.version": RUBRIC_VERSION,
                    "judge.evaluator.count": len(verdicts),
                    "judge.source.span_count": summary.span_count,
                    "judge.source.error_count": summary.error_count,
                    "judge.source.duration_ms": round(summary.duration_ms, 2),
                    "judge.source.tool_calls": len(summary.tool_calls),
                }
            )

            for verdict in verdicts:
                label = verdict.label(threshold)
                with tracer.start_as_current_span("gen_ai.evaluation", links=[link]) as span:
                    span.set_attributes(
                        {
                            # gen_ai.evaluation.* is experimental in the GenAI
                            # semconv; expect these names to move.
                            "gen_ai.evaluation.name": verdict.name,
                            "gen_ai.evaluation.score.value": verdict.score,
                            "gen_ai.evaluation.score.label": label,
                            "gen_ai.evaluation.explanation": verdict.explanation,
                            # Not semconv, but a bare 4.2 is uninterpretable
                            # without knowing the scale it came from.
                            "gen_ai.evaluation.score.range.min": SCORE_MIN,
                            "gen_ai.evaluation.score.range.max": SCORE_MAX,
                            "gen_ai.evaluation.source_trace_id": summary.trace_id,
                            "judge.rubric.version": RUBRIC_VERSION,
                        }
                    )

                _score_histogram.record(
                    verdict.score,
                    attributes={
                        "gen_ai.evaluation.name": verdict.name,
                        "gen_ai.evaluation.score.label": label,
                        "gen_ai.evaluation.source_service": summary.root_service,
                        "gen_ai.request.model": ", ".join(summary.models) or "unknown",
                        "judge.rubric.version": RUBRIC_VERSION,
                    },
                )

            eval_trace_id = format_trace_id(root.get_span_context().trace_id)
    finally:
        context.detach(token)

    logger.info(
        "evaluated trace",
        extra={
            "source_trace_id": summary.trace_id,
            "eval_trace_id": eval_trace_id,
            "scores": {v.name: v.score for v in verdicts},
        },
    )
    return eval_trace_id


__all__ = ["emit_evaluation"]
