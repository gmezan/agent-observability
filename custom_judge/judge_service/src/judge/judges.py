"""The judge itself: a protocol, a mocked implementation, and a real one.

``MockJudge`` is the default and is what this repo demonstrates -- it produces a
verdict with no model call, no spend and no latency, which is what makes the
pipeline demoable. It is a stand-in for the scoring, *not* for the plumbing:
everything around it (OTLP receive, trace reassembly, rubric, eval span, span
link) is the real thing.

``LLMJudge`` is the same interface backed by an actual chat client, selected
with ``JUDGE_MODE=llm``. It exists so the seam is visible: swapping the scoring
is one env var, because nothing else in the service knows how a score is
produced.
"""

from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Protocol

from .rubric import EVALUATORS, SCORE_MAX, SCORE_MIN, SYSTEM_PROMPT, build_prompt
from .summary import TraceSummary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Verdict:
    """One evaluator's opinion of one trace."""

    name: str
    score: float
    explanation: str

    def label(self, threshold: float) -> str:
        return "pass" if self.score >= threshold else "fail"


class Judge(Protocol):
    """Score a trace on every evaluator in the rubric."""

    name: str

    async def evaluate(self, summary: TraceSummary) -> list[Verdict]: ...


def _clamp(score: float) -> float:
    return round(min(SCORE_MAX, max(SCORE_MIN, score)), 2)


class MockJudge:
    """Deterministic scores derived from the trace, without a model call.

    Deterministic on ``trace_id``: re-judging a trace gives the same verdict,
    so a dashboard does not shimmer between reloads during a demo.

    The scores are fabricated but not arbitrary -- they react to real signals in
    the trace (errored spans, failed tools, an empty answer, a request that
    asked for an action no tool performed). That keeps the resulting
    distribution shaped like something worth putting on a dashboard, with a
    realistic minority of failures, instead of uniform noise.
    """

    name = "mock"

    #: Verbs that imply the customer wanted something *done*, not just looked up.
    _ACTION_WORDS = ("refund", "restart", "cancel", "reset", "credit", "replace")

    async def evaluate(self, summary: TraceSummary) -> list[Verdict]:
        rng = random.Random(summary.trace_id)
        request = summary.user_input.lower()
        tool_names = {call.name for call in summary.tool_calls}
        failed_tools = [call for call in summary.tool_calls if call.failed]

        verdicts: list[Verdict] = []
        for evaluator in EVALUATORS:
            # Well-behaved traces cluster high; the spread is what produces the
            # occasional sub-threshold score a "pass rate" panel needs.
            score = rng.gauss(4.4, 0.45)
            reasons: list[str] = []

            if summary.error_count:
                score -= 1.6
                reasons.append(f"{summary.error_count} span(s) ended in error")

            if evaluator.name == "tool_call_accuracy":
                if failed_tools:
                    score -= 1.2
                    reasons.append(f"tool {failed_tools[0].name} failed")
                elif not summary.tool_calls:
                    score -= 0.8
                    reasons.append("answered without calling any tool")
                else:
                    reasons.append(f"called {', '.join(sorted(tool_names))} with arguments matching the request")

            if evaluator.name == "task_adherence":
                wanted = [word for word in self._ACTION_WORDS if word in request]
                if wanted and not summary.tool_calls:
                    score -= 1.5
                    reasons.append(f"customer asked to {wanted[0]} but no tool was called")
                elif wanted:
                    reasons.append(f"the requested {wanted[0]} was carried out via tool calls")
                else:
                    reasons.append("the request was informational and was answered")

            if evaluator.name == "relevance":
                if not summary.final_output:
                    score -= 2.0
                    reasons.append("no answer was returned to the customer")
                else:
                    reasons.append(f"the answer speaks to the request across {len(summary.agents) or 1} agent(s)")

            verdicts.append(
                Verdict(
                    name=evaluator.name,
                    score=_clamp(score),
                    explanation="; ".join(reasons) + " (mock judge: score is synthetic)",
                )
            )
        return verdicts


class LLMJudge:
    """Score a trace by prompting a chat client with the rubric.

    Uses the same ``OpenAIChatClient`` and the same credentials as the agents
    under evaluation, purely to keep the demo to one set of env vars. In
    production the judge should be a *different*, usually stronger model than
    the one it grades -- a model is a poor critic of its own output.

    Its own call is traced like any other, so the eval trace in Grafana shows
    the judge's latency and token cost next to the verdict it produced.
    """

    name = "llm"

    def __init__(self) -> None:
        # Imported lazily so the mock path does not pay for the model SDK, and
        # so a missing credential only breaks the mode that needs it.
        from agent_framework.openai import OpenAIChatClient

        self._client = OpenAIChatClient()

    async def evaluate(self, summary: TraceSummary) -> list[Verdict]:
        response = await self._client.get_response(
            build_prompt(summary),
            instructions=SYSTEM_PROMPT,
            temperature=0.0,
        )
        return self._parse(str(response))

    @staticmethod
    def _parse(text: str) -> list[Verdict]:
        """Read the model's JSON, tolerating a fenced code block around it.

        A dimension the model omitted or mangled is skipped rather than
        defaulted: a fabricated score is worse than a missing one, because it
        is indistinguishable from a real one on a dashboard.
        """
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError(f"judge returned no JSON object: {text[:200]!r}")
        payload = json.loads(match.group(0))

        verdicts: list[Verdict] = []
        for evaluator in EVALUATORS:
            entry = payload.get(evaluator.name)
            if not isinstance(entry, dict) or "score" not in entry:
                logger.warning("judge omitted dimension", extra={"dimension": evaluator.name})
                continue
            verdicts.append(
                Verdict(
                    name=evaluator.name,
                    score=_clamp(float(entry["score"])),
                    explanation=str(entry.get("explanation", "")).strip(),
                )
            )
        if not verdicts:
            raise ValueError("judge returned no usable dimensions")
        return verdicts


def build_judge(use_llm: bool) -> Judge:
    """Pick the scoring implementation. The only place the two are named."""
    judge: Judge = LLMJudge() if use_llm else MockJudge()
    logger.info("judge ready", extra={"judge_mode": judge.name})
    return judge
