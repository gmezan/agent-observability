"""Reconstruct the conversation a trace represents, so it can be scored.

A judge cannot score a span tree; it scores "the customer asked X, the system
did Y, and answered Z". Recovering that from OTLP is the real work of this
service, and it is entirely a function of the GenAI semantic conventions the
Agent Framework already emits:

* ``invoke_agent <name>``  -> which agent, on which host
* ``chat <model>``         -> gen_ai.input.messages / gen_ai.output.messages
* ``execute_tool <name>``  -> gen_ai.tool.call.arguments / .result

The message attributes only exist when ``ENABLE_SENSITIVE_DATA=true``. Without
it the trace still arrives and is still summarised -- there is simply no text
to score, which is why ``TraceSummary.judgeable`` exists.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .otlp import Span

INPUT_MESSAGES = "gen_ai.input.messages"
OUTPUT_MESSAGES = "gen_ai.output.messages"


@dataclass(frozen=True)
class ToolCall:
    """One ``execute_tool`` span, flattened."""

    name: str
    service: str
    arguments: str
    result: str
    failed: bool


@dataclass(frozen=True)
class TraceSummary:
    """Everything the judge is allowed to see about one trace."""

    trace_id: str
    root_span_id: str
    root_service: str
    services: tuple[str, ...]
    agents: tuple[str, ...]
    models: tuple[str, ...]
    tool_calls: tuple[ToolCall, ...]
    user_input: str
    final_output: str
    duration_ms: float
    span_count: int
    error_count: int
    input_tokens: int
    output_tokens: int

    @property
    def judgeable(self) -> bool:
        """False when the trace carries no text -- nothing to form an opinion about.

        Usually means ENABLE_SENSITIVE_DATA is off, or the trace is a health
        probe that slipped through sampling.
        """
        return bool(self.user_input and self.final_output)


def _texts_from_parts(parts: Any) -> list[str]:
    """Pull human-readable text out of a semconv message's ``parts``.

    Shapes vary across semconv revisions -- a part may carry ``content`` or
    ``text``, and a message's content may be a bare string instead of a parts
    list -- so this stays permissive rather than asserting one layout and
    losing the text when the convention moves.
    """
    if isinstance(parts, str):
        return [parts]
    if not isinstance(parts, list):
        return []

    texts: list[str] = []
    for part in parts:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            # Skip tool_call parts: those are reported separately, from their
            # own execute_tool spans, with the result attached.
            if part.get("type") in ("tool_call", "tool_call_response"):
                continue
            value = part.get("content") or part.get("text")
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())
    return texts


def _messages(raw: Any, roles: tuple[str, ...]) -> list[str]:
    """Decode a gen_ai.*.messages attribute and return the text for `roles`."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return [raw] if raw.strip() else []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    texts: list[str] = []
    for message in raw:
        if not isinstance(message, dict):
            continue
        if str(message.get("role", "")).lower() not in roles:
            continue
        texts.extend(_texts_from_parts(message.get("parts", message.get("content"))))
    return texts


def _stringify(value: Any, limit: int = 600) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def summarize(spans: list[Span]) -> TraceSummary:
    """Fold one trace's spans into the conversation they describe."""
    ordered = sorted(spans, key=lambda s: s.start_ns)
    root = next((s for s in ordered if s.is_root), ordered[0])

    agents: list[str] = []
    models: list[str] = []
    tool_calls: list[ToolCall] = []
    user_texts: list[str] = []
    assistant_texts: list[str] = []
    input_tokens = 0
    output_tokens = 0

    for span in ordered:
        attributes = span.attributes

        agent_name = attributes.get("gen_ai.agent.name")
        if agent_name and agent_name not in agents:
            agents.append(str(agent_name))

        model = attributes.get("gen_ai.request.model") or attributes.get("gen_ai.response.model")
        if model and model not in models:
            models.append(str(model))

        input_tokens += int(attributes.get("gen_ai.usage.input_tokens") or 0)
        output_tokens += int(attributes.get("gen_ai.usage.output_tokens") or 0)

        tool_name = attributes.get("gen_ai.tool.name")
        if tool_name:
            tool_calls.append(
                ToolCall(
                    name=str(tool_name),
                    service=span.service_name,
                    arguments=_stringify(attributes.get("gen_ai.tool.call.arguments"), limit=300),
                    result=_stringify(attributes.get("gen_ai.tool.call.result"), limit=300),
                    failed=span.failed,
                )
            )

        user_texts.extend(_messages(attributes.get(INPUT_MESSAGES), ("user",)))
        assistant_texts.extend(_messages(attributes.get(OUTPUT_MESSAGES), ("assistant", "model")))

    # The first user turn is the actual request; later chat spans replay it
    # alongside tool results, so taking the last one would score the wrong
    # thing. The last assistant turn is the answer that reached the customer.
    user_input = _stringify(user_texts[0] if user_texts else "", limit=1200)
    final_output = _stringify(assistant_texts[-1] if assistant_texts else "", limit=1200)

    start = min(s.start_ns for s in ordered)
    end = max(s.end_ns for s in ordered)

    return TraceSummary(
        trace_id=root.trace_id,
        root_span_id=root.span_id,
        root_service=root.service_name,
        services=tuple(sorted({s.service_name for s in ordered})),
        agents=tuple(agents),
        models=tuple(models),
        tool_calls=tuple(tool_calls),
        user_input=user_input,
        final_output=final_output,
        duration_ms=(end - start) / 1_000_000,
        span_count=len(ordered),
        error_count=sum(1 for s in ordered if s.failed),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
