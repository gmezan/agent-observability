"""The scoring rubric: what gets measured, on what scale, and the judge prompt.

This is the part Architecture B buys you. With a managed evaluator the metrics
and their definitions are the vendor's; here they are three editable constants,
versioned with the code that emits them.

``RUBRIC_VERSION`` is stamped on every eval span. Scores are only comparable
within a version -- change a definition and the old numbers mean something
else, so a dashboard needs to be able to tell them apart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .summary import ToolCall, TraceSummary

RUBRIC_VERSION = "1.0.0"

SCORE_MIN = 1.0
SCORE_MAX = 5.0


@dataclass(frozen=True)
class Evaluator:
    """One named dimension the judge scores a trace on."""

    name: str
    question: str


EVALUATORS: tuple[Evaluator, ...] = (
    Evaluator(
        name="relevance",
        question=(
            "Does the final answer address what the customer actually asked, without "
            "drifting into unrelated topics or padding?"
        ),
    ),
    Evaluator(
        name="task_adherence",
        question=(
            "Did the system complete every action the customer requested? A customer who "
            "asks for two things and gets one has not been served."
        ),
    ),
    Evaluator(
        name="tool_call_accuracy",
        question=(
            "Were the right tools called, with arguments matching what the customer said, "
            "and were their results reflected faithfully in the answer? Invented account "
            "ids, amounts or outcomes are the failure to look for."
        ),
    ),
)

SYSTEM_PROMPT = (
    "You are a strict evaluator of a customer-support agent system. You are shown one "
    "completed interaction: the customer's request, the tools the system called, and the "
    f"answer it returned. Score each named dimension from {SCORE_MIN:.0f} (unacceptable) to "
    f"{SCORE_MAX:.0f} (flawless). Judge only what the evidence shows -- do not assume an "
    "action happened because the answer claims it did; check the tool calls. Reply with "
    "JSON only, as an object mapping each dimension name to "
    '{"score": <number>, "explanation": "<one sentence citing specific evidence>"}.'
)


def render_trace(summary: TraceSummary) -> str:
    """Render a trace as the evidence block shown to the judge.

    Deliberately plain text rather than raw spans: the judge scores a
    conversation, and giving a model a span tree invites it to comment on the
    instrumentation instead of the answer.
    """
    if summary.tool_calls:
        tools = "\n".join(
            f"  - {call.name} (on {call.service}){' [FAILED]' if call.failed else ''}\n"
            f"      arguments: {call.arguments or '(not captured)'}\n"
            f"      result:    {call.result or '(not captured)'}"
            for call in summary.tool_calls
        )
    else:
        tools = "  (none)"

    return (
        f"CUSTOMER REQUEST:\n  {summary.user_input}\n\n"
        f"AGENTS INVOLVED: {', '.join(summary.agents) or 'unknown'}\n"
        f"TOOL CALLS:\n{tools}\n\n"
        f"FINAL ANSWER:\n  {summary.final_output}\n\n"
        f"(trace took {summary.duration_ms:.0f} ms across {summary.span_count} spans, "
        f"{summary.error_count} of them errored)"
    )


def build_prompt(summary: TraceSummary) -> str:
    """The full user message sent to the judge model."""
    dimensions = "\n".join(f"- {evaluator.name}: {evaluator.question}" for evaluator in EVALUATORS)
    return (
        f"Score the following interaction on these dimensions:\n{dimensions}\n\n"
        f"{render_trace(summary)}\n\n"
        f"Respond with JSON only, keys: {json.dumps([e.name for e in EVALUATORS])}."
    )


#: A stand-in trace, used only to render the prompt on ``GET /rubric`` so the
#: rubric can be read without grepping the image or waiting for live traffic.
SAMPLE_SUMMARY = TraceSummary(
    trace_id="0" * 32,
    root_span_id="0" * 16,
    root_service="triage-agent",
    services=("triage-agent", "billing-agent"),
    agents=("triage_agent", "billing_agent"),
    models=("gpt-4.1-mini",),
    tool_calls=(
        ToolCall(
            name="issue_refund",
            service="billing-agent",
            arguments='{"account_id": "ACC-4471", "amount_usd": 42.5}',
            result="Refund of $42.50 issued to account ACC-4471.",
            failed=False,
        ),
    ),
    user_input="I was double charged on ACC-4471. Check my invoice and refund me 42.50 USD.",
    final_output="Your latest invoice is unpaid and due in 3 days; I have refunded $42.50.",
    duration_ms=4210.0,
    span_count=12,
    error_count=0,
    input_tokens=1840,
    output_tokens=260,
)
