# Observability + LLM-as-a-Judge

This is the consolidating reference for how this repo's agents are instrumented, how traces
flow out of the process, and how an LLM-as-judge evaluation loop attaches its verdicts back
onto those traces. It links out to two concrete architecture options — pick one, or read both
to see the tradeoff.

- [Architecture A — with Azure AI Foundry evaluations + App Insights](./architecture-azure-evaluations.md)
- [Architecture B — custom judge, no App Insights](./architecture-custom-judge.md)
- [Scaling for high-volume traffic](./scaling-high-volume.md) — both architectures above
  describe the pattern that works at moderate volume; this doc covers the two gaps that show
  up under real production traffic (trace-aware Collector routing, and buffering in front of
  the judge with Azure Event Hub) and when to actually reach for them.

## 1. What's already instrumented in this repo

`configure_otel_providers()` (from `agent_framework.observability`, called once at process
startup in `balance_inquiry_agent.py`, `balance_inquiry_harness_agent.py`, and
`banking_support_multiagent.py`) wires up the OpenTelemetry SDK's tracer/meter/logger
providers from `OTEL_EXPORTER_OTLP_*` env vars.

On top of that, both `Agent` and `create_harness_agent(...)` already include
`AgentTelemetryLayer` in their class stack (confirmed via `Agent.__mro__` — it's not a
harness-only feature). This means, with zero extra code:

- Every `agent.run(...)` call produces an `invoke_agent <name>` span.
- Every tool call produces a nested `execute_tool <name>` span.
- The underlying chat completion produces its own span (via `ChatTelemetryLayer` on the
  `OpenAIChatClient`/`OpenAIChatCompletionClient` classes).
- All of the above nest under whatever span is "current" via normal OTel context
  propagation — this is why wrapping a call in
  `with get_tracer().start_as_current_span("banking.balance_inquiry", ...)` makes every
  span underneath it part of the same trace, without any manual parent-linking.

What the SDK does **not** do for you:

- Process-wide setup (`configure_otel_providers()` itself — a one-time call, not per-agent).
- Business-meaningful span names/attributes. Auto-generated spans are named generically per
  GenAI semantic conventions (`invoke_agent <name>`); they don't know a given call is a
  "balance inquiry" or which account it's for. That's why `balance_inquiry_agent.py` wraps
  the call in its own span with a static name (`banking.balance_inquiry`) and a dynamic
  attribute (`banking.account_id`) — see the note below on span-name cardinality.
- Flushing on short-lived process exit (`force_flush()` on the tracer provider — needed
  because `BatchSpanProcessor` exports on a timer, and a CLI script can exit before that
  timer fires).

**Span naming rule:** span names must stay static/low-cardinality (`banking.balance_inquiry`),
never interpolate a variable into the name itself (`f"balance_inquiry:{account_id}"`) — that
breaks aggregation-by-operation-name in Grafana/Tempo. Variable data belongs in attributes.

## 2. In-process SDK vs. a real OTel Collector

The in-process SDK is *not* a mini collector — it has no receiver concept, since spans
originate directly from your own code calling the tracing API rather than arriving over the
network. But it shares the same two remaining building blocks:

| Concept | In-process SDK | Standalone Collector |
|---|---|---|
| Receiver | n/a — spans are created directly by your code | `otlp` receiver, listens on a port |
| Processor | `SpanProcessor` (e.g. `BatchSpanProcessor`), with an `on_end()` hook per span | `attributes`, `redaction`, `tail_sampling`, etc. |
| Exporter | `SpanExporter` (OTLP, console, ...) | same OTLP exporters, plus vendor-specific ones (e.g. `azuremonitor`) |

For this pipeline we move redaction, enrichment, and sampling **out of the app process and
into a standalone Collector**, rather than writing a custom in-process `SpanProcessor`. Two
reasons:

1. It decouples that logic from agent request latency entirely — the app does one OTLP
   export and returns; fan-out/sampling/redaction happen in a separate process.
2. Trace-consistent sampling ("only forward some traces downstream, but never a *partial*
   trace") needs to buffer all of a trace's spans until it's complete before deciding —
   that's exactly what the Collector's `tail_sampling` processor already does, so there's no
   custom code to write or maintain for it.

## 3. The evaluation loop, in general terms

Regardless of which architecture option you pick, the shape is the same:

1. The app emits 100% of its traces to the Collector over OTLP.
2. The Collector always forwards 100% of traces to **Grafana Tempo**, for visualization —
   this leg is identical in both architectures.
3. The Collector's `tail_sampling` processor selects a subset of *complete* traces (by
   `trace_id`, so no fragments) and forwards only those down a second pipeline, toward
   whatever runs the judge.
4. The judge — an LLM, prompted to score the sampled trace (relevance, correctness,
   task adherence, etc.) — runs asynchronously, after the original trace already completed.
5. The judge's verdict is emitted as a span using the OpenTelemetry GenAI semantic convention
   prefix `gen_ai.evaluation.*` (e.g. `gen_ai.evaluation.name`, `gen_ai.evaluation.score.value`)
   — currently an experimental part of the GenAI semconv, expect attribute names to evolve.
6. That span belongs to its **own, independent trace** — it is *not* injected back into the
   original trace. See the correlation approach below for why, and how it still shows up
   next to the original trace in Grafana.

### ⚠️ Important: do not inject the eval span into the original trace

**Confirmed:** Grafana Tempo will *accept* a span that arrives after the rest of its trace was
already closed/flushed — trace storage has no hard "closed" state, so ingestion itself won't
reject it. But accepting it is not the same as it being free:

- Once a trace's blocks are already cut and flushed, a later span for the same `trace_id`
  forces the ingester to open a **new** live trace entry for that ID, which eventually flushes
  into a **separate block**. The one logical trace now lives fragmented across multiple blocks.
- At query time, Tempo has to check more blocks (more bloom-filter lookups) to reassemble a
  single `trace_id` — a cost paid on every read, until compaction eventually merges those
  blocks back together.
- This pipeline does this *routinely*, for every sampled trace, indefinitely — not as a rare
  one-off correction. At volume, that's a continuous stream of small orphan blocks that
  compaction constantly has to work through: real, ongoing storage and query-performance
  overhead, not a one-time cost. This is exactly the "long-running traces" failure mode
  Tempo's own docs warn about.

**Do this instead — give the evaluation its own trace, and correlate, don't merge:**

1. The judge service starts a normal, independent span/trace for the evaluation (e.g.
   `gen_ai.evaluate`) — it opens and closes normally, flushes normally, with zero long-running-
   trace side effects.
2. It carries a **plain span attribute** pointing back to the source:
   `gen_ai.evaluation.source_trace_id = <original trace_id hex>` (and optionally
   `gen_ai.evaluation.source_span_id`). This alone is enough to pivot between the two via
   TraceQL, e.g. `{ .gen_ai.evaluation.source_trace_id = "..." }`.
3. It also carries a formal **OTel Span Link** to the original span's `SpanContext` — the
   primitive OTel defines specifically for "this span is causally related to another trace,
   but is not its child":

   ```python
   from opentelemetry.trace import Link, SpanContext, TraceFlags
   from opentelemetry.trace.span import format_trace_id

   original_ctx = SpanContext(
       trace_id=original_trace_id,
       span_id=original_span_id,
       is_remote=True,
       trace_flags=TraceFlags(TraceFlags.SAMPLED),
   )
   with tracer.start_as_current_span("gen_ai.evaluate", links=[Link(original_ctx)]) as eval_span:
       eval_span.set_attribute(
           "gen_ai.evaluation.source_trace_id", format_trace_id(original_trace_id)
       )
       ...
   ```

   Tempo/Grafana render span links as a navigable "Links" reference in the trace view, so this
   isn't just metadata — clicking it jumps straight to the original trace, without ever
   touching that trace's storage.
4. Optionally, configure Grafana's Tempo datasource with a **derived-field trace link** off
   `gen_ai.evaluation.source_trace_id`, so clicking that attribute value in the eval span's
   detail pane also jumps to the original trace — the same "click to see it" UX, at zero
   storage cost.

This also simplifies the judge service's code: no manual `is_remote=True` parent-`SpanContext`
construction to fake continuity of the original trace — just a normal span, with a `Link` and
an attribute.

## 4. The one decision that changes the architecture

**Do you only need App Insights if you're going to use it.** Concretely: Azure AI Foundry's
built-in judge (`agent_framework.foundry.evaluate_traces()`) works by *querying App Insights*
for traces by `trace_id`/`response_id` — that's its one dependency on App Insights. If your
judge is a custom service instead, there is no reason to stage sampled traces in App Insights
first; route the `tail_sampling` pipeline's second leg directly to your judge's own OTLP
receiver. See the two architecture docs linked above for the full diagrams and component
list of each option.
