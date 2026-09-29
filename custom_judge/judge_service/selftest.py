"""Offline check of the whole judge path, with no collector and no agents.

Feeds the service hand-built OTLP protobuf bytes shaped like a real two-host
agent trace (triage -> billing, with a tool call), then asserts on what comes
out the far end: the reconstructed conversation, the verdict, and -- the part
worth protecting -- that the eval span is a *root of its own trace* carrying a
Link back to the source, rather than a child of anything.

That last property is the one that silently regresses. It holds today because
``emit_evaluation`` detaches the ambient OTel context first; delete that and
every assertion here still passes except the two that matter, so they are
written explicitly and the emit is deliberately performed from *inside* an
unrelated span.

Run it against the built image -- the deps are already installed there:

    docker build -t judge-selftest ./judge_service
    docker run --rm -v "$PWD/judge_service/selftest.py:/tmp/selftest.py:ro" \\
        --entrypoint python judge-selftest /tmp/selftest.py
"""
import asyncio
import gzip
import json
import os

os.environ.setdefault("OTEL_SERVICE_NAME", "judge-service")

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

exporter = InMemorySpanExporter()
provider = TracerProvider(resource=Resource.create({"service.name": "judge-service"}))
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource as PbResource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span as PbSpan, Status

from judge.otlp import decode_traces, decompress, empty_response
from judge.summary import summarize
from judge.judges import MockJudge
from judge.emit import emit_evaluation

TRACE_ID = bytes.fromhex("4bf92f3577b34da6a3ce929d0e0e4736")
ROOT_SPAN = bytes.fromhex("00f067aa0ba902b7")


def kv(key, value):
    if isinstance(value, bool):
        return KeyValue(key=key, value=AnyValue(bool_value=value))
    if isinstance(value, int):
        return KeyValue(key=key, value=AnyValue(int_value=value))
    if isinstance(value, float):
        return KeyValue(key=key, value=AnyValue(double_value=value))
    return KeyValue(key=key, value=AnyValue(string_value=str(value)))


def span(name, span_id, parent, attrs, start, end, status=0):
    return PbSpan(
        trace_id=TRACE_ID,
        span_id=bytes.fromhex(span_id),
        parent_span_id=bytes.fromhex(parent) if parent else b"",
        name=name,
        start_time_unix_nano=start,
        end_time_unix_nano=end,
        attributes=[kv(k, v) for k, v in attrs.items()],
        status=Status(code=status),
    )


INPUT_MSGS = json.dumps([
    {"role": "user", "parts": [{"type": "text", "content": "I was double charged on ACC-4471. Refund me 42.50 USD."}]}
])
OUTPUT_MSGS = json.dumps([
    {"role": "assistant", "parts": [{"type": "text", "content": "I have refunded $42.50 to ACC-4471."}],
     "finish_reason": "stop"}
])

BASE = 1_700_000_000_000_000_000

triage = ResourceSpans(
    resource=PbResource(attributes=[kv("service.name", "triage-agent")]),
    scope_spans=[ScopeSpans(spans=[
        span("POST /", "00f067aa0ba902b7", "", {"http.request.method": "POST"}, BASE, BASE + 5_000_000_000),
        span("invoke_agent triage_agent", "1111111111111111", "00f067aa0ba902b7",
             {"gen_ai.agent.name": "triage_agent"}, BASE, BASE + 4_900_000_000),
        span("chat gpt-4.1-mini", "2222222222222222", "1111111111111111",
             {"gen_ai.request.model": "gpt-4.1-mini", "gen_ai.input.messages": INPUT_MSGS,
              "gen_ai.usage.input_tokens": 900, "gen_ai.usage.output_tokens": 40},
             BASE, BASE + 1_000_000_000),
    ])],
)

billing = ResourceSpans(
    resource=PbResource(attributes=[kv("service.name", "billing-agent")]),
    scope_spans=[ScopeSpans(spans=[
        span("invoke_agent billing_agent", "3333333333333333", "1111111111111111",
             {"gen_ai.agent.name": "billing_agent"}, BASE + 1_100_000_000, BASE + 4_000_000_000),
        span("execute_tool issue_refund", "4444444444444444", "3333333333333333",
             {"gen_ai.tool.name": "issue_refund",
              "gen_ai.tool.call.arguments": '{"account_id": "ACC-4471", "amount_usd": 42.5}',
              "gen_ai.tool.call.result": "Refund of $42.50 issued to account ACC-4471."},
             BASE + 2_000_000_000, BASE + 2_300_000_000),
        span("chat gpt-4.1-mini", "5555555555555555", "3333333333333333",
             {"gen_ai.request.model": "gpt-4.1-mini", "gen_ai.output.messages": OUTPUT_MSGS,
              "gen_ai.usage.input_tokens": 1200, "gen_ai.usage.output_tokens": 60},
             BASE + 2_400_000_000, BASE + 3_900_000_000),
    ])],
)

payload = ExportTraceServiceRequest(resource_spans=[triage, billing]).SerializeToString()

# --- 1. gzip round-trip, as the collector's otlphttp exporter sends it -------
decoded = decode_traces(decompress(gzip.compress(payload), "gzip"))
assert len(decoded) == 6, len(decoded)
assert empty_response() == b""
print(f"OK  decode: {len(decoded)} spans, gzip handled")

# --- 2. summarize ------------------------------------------------------------
s = summarize(decoded)
assert s.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736", s.trace_id
assert s.root_span_id == "00f067aa0ba902b7", s.root_span_id
assert s.root_service == "triage-agent", s.root_service
assert "double charged" in s.user_input, s.user_input
assert "refunded $42.50" in s.final_output, s.final_output
assert [c.name for c in s.tool_calls] == ["issue_refund"], s.tool_calls
assert s.agents == ("triage_agent", "billing_agent"), s.agents
assert s.models == ("gpt-4.1-mini",), s.models
assert s.input_tokens == 2100 and s.output_tokens == 100
assert s.services == ("billing-agent", "triage-agent")
assert abs(s.duration_ms - 5000.0) < 1, s.duration_ms
assert s.judgeable
print(f"OK  summarize: input={s.user_input[:40]!r} output={s.final_output[:40]!r} tools={[c.name for c in s.tool_calls]}")

# --- 3. judge ----------------------------------------------------------------
verdicts = asyncio.run(MockJudge().evaluate(s))
assert len(verdicts) == 3, verdicts
assert {v.name for v in verdicts} == {"relevance", "task_adherence", "tool_call_accuracy"}
assert all(1.0 <= v.score <= 5.0 for v in verdicts)
# deterministic on trace_id
again = asyncio.run(MockJudge().evaluate(s))
assert [v.score for v in verdicts] == [v.score for v in again], "mock judge is not deterministic"
print("OK  judge:", {v.name: v.score for v in verdicts})

# --- 4. emit, from inside an unrelated ambient span --------------------------
# This is the trap the detach in emit.py exists for: if the eval span inherited
# ambient context it would join this trace instead of starting its own.
outer = trace.get_tracer("test")
with outer.start_as_current_span("ambient-request") as ambient:
    ambient_trace_id = ambient.get_span_context().trace_id
    eval_trace_id = emit_evaluation(s, verdicts, judge_mode="mock", threshold=3.0)

spans = exporter.get_finished_spans()
by_name = {sp.name: sp for sp in spans}
root = by_name["gen_ai.evaluate"]
evals = [sp for sp in spans if sp.name == "gen_ai.evaluation"]

assert len(evals) == 3, len(evals)
assert eval_trace_id != s.trace_id, "eval span must not reuse the source trace id"
assert root.context.trace_id != ambient_trace_id, "eval trace leaked into the ambient trace"
assert root.parent is None, f"eval root must be a root span, got parent {root.parent}"

link = root.links[0]
assert f"{link.context.trace_id:032x}" == s.trace_id, "link points at the wrong trace"
assert f"{link.context.span_id:016x}" == s.root_span_id, "link points at the wrong span"
assert link.context.is_remote and link.context.trace_flags.sampled

a = root.attributes
assert a["gen_ai.evaluation.source_trace_id"] == s.trace_id
assert a["gen_ai.evaluation.source_span_id"] == s.root_span_id
assert a["judge.mode"] == "mock"
assert a["judge.source.tool_calls"] == 1

for sp in evals:
    ea = sp.attributes
    assert ea["gen_ai.evaluation.name"] in {"relevance", "task_adherence", "tool_call_accuracy"}
    assert isinstance(ea["gen_ai.evaluation.score.value"], float)
    assert ea["gen_ai.evaluation.score.label"] in {"pass", "fail"}
    assert ea["gen_ai.evaluation.explanation"]
    assert sp.links and f"{sp.links[0].context.trace_id:032x}" == s.trace_id
    assert sp.parent.span_id == root.context.span_id, "evaluation must nest under gen_ai.evaluate"

print(f"OK  emit: eval trace {eval_trace_id} links -> source {s.trace_id}")
print(f"OK  emit: {len(evals)} gen_ai.evaluation children, independent root, ambient trace not joined")

# --- 5. non-judgeable trace --------------------------------------------------
stripped = [d for d in decoded if "gen_ai.input.messages" not in d.attributes and "gen_ai.output.messages" not in d.attributes]
assert not summarize(stripped).judgeable, "trace with no messages must not be judgeable"
print("OK  guard: trace without message content is not judgeable")

print("\nALL SELFTEST CHECKS PASSED")
