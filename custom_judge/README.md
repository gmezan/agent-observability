# Custom judge

[Architecture B](../docs/architecture-custom-judge.md), running. The distributed
agents from [`distributed_agents/`](../distributed_agents/) keep working exactly
as before — one customer request still crosses three hosts and shows up in Tempo
as one trace — but now every trace passes through a **standalone OTel Collector**
that fans it two ways, and a sampled subset is **scored by a judge service** whose
verdict comes back to Grafana as its own, linked trace.

```
                        ┌──────────────── 100% ────────────────┐
                        │                                      ▼
 triage :8180 ─┐        │                              Grafana Tempo
 billing :8181 ├─ OTLP ─┴─► collector :4318 ─┐               ▲
 tech    :8182 ┘         (redact, sample)    │               │
                                             └─ sampled ─► judge :8185
                                               complete      (score)
                                               traces only
```

The one change to the agents is an environment variable: their
`OTEL_EXPORTER_OTLP_ENDPOINT` points at the collector instead of at Grafana.
No code changes, no rebuild — this stack runs the **same images**
`distributed_agents/` builds, the way [`scripts/two-model-stack`](../scripts/two-model-stack/)
does. One stack, one model (`gpt-4.1-mini`).

## What is actually new here

| Piece | Where | Status |
| --- | --- | --- |
| Collector with a two-pipeline fan-out | `collector/otelcol-config.yaml` | real |
| Value redaction before egress | `redaction` processor | real |
| Trace-complete, content-aware sampling | `tail_sampling` processor | real |
| OTLP/HTTP receiver inside the judge | `judge_service/src/judge/otlp.py` | real |
| Trace → conversation reconstruction | `judge_service/src/judge/summary.py` | real |
| Rubric and judge prompt | `judge_service/src/judge/rubric.py` | real |
| **Scoring** | `judge_service/src/judge/judges.py` | **mocked** (`MockJudge`) |
| `gen_ai.evaluation` span + Link back | `judge_service/src/judge/emit.py` | real |

Only the scoring is a stand-in. Everything the scoring sits inside — ingest,
reassembly, correlation, export — is the real thing, which is the part that is
actually hard to get right. Flip `JUDGE_MODE=llm` to have `LLMJudge` run the
same rubric through an actual model.

## Prerequisites

The agent images, built once by the stack that owns them:

```bash
cd distributed_agents && docker compose build
```

And two extra variables in the repo-root `.env`, on top of what the agents
already need:

```bash
GRAFANA_OTLP_ENDPOINT=https://otlp-gateway-<region>.grafana.net/otlp
GRAFANA_OTLP_AUTHORIZATION="Basic <token>"
```

These point at the **same** Grafana destination as `OTEL_EXPORTER_OTLP_*`, just
split in two. The SDK takes one packed `Authorization=Basic ...` string; the
Collector's exporter takes a header *map*. Neither can read the other's format,
so the value is carried twice rather than parsed at runtime.

## Run

```bash
cd custom_judge
docker compose up -d --build --wait
docker compose ps
```

Ports are deliberately in the `81xx` range, clear of `distributed_agents/`
(`808x`) and `two-model-stack` (`808x`/`809x`), so this stack runs **alongside**
them instead of fighting for ports.

| Service | URL |
| --- | --- |
| triage agent | `http://localhost:8180` |
| billing agent | `http://localhost:8181` |
| tech agent | `http://localhost:8182` |
| judge | `http://localhost:8185` |
| collector OTLP | `localhost:4317` (gRPC) / `4318` (HTTP) |
| collector health | `http://localhost:13133` |

Tear down with `docker compose down`.

## Try it

Send a request the same way as in `distributed_agents/`, pinning a trace id so
you know what to search for:

```bash
TRACE=$(python3 -c "import secrets;print(secrets.token_hex(16))")
SPAN=$(python3 -c "import secrets;print(secrets.token_hex(8))")
echo "source trace: $TRACE"

curl -s -X POST http://localhost:8180/ \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -H "traceparent: 00-$TRACE-$SPAN-01" \
  -d '{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"I was double charged on account ACC-4471. Check my invoice and refund me 42.50 USD."}]}}}' \
  | jq -r '.result.task.history[] | select(.role=="ROLE_AGENT") | .parts[].text'
```

The answer comes back in a few seconds. **The verdict does not** — and that is
the design, not a delay to apologise for. The collector holds the trace for
`decision_wait` (20s) to be sure it is complete, then the judge debounces for
`JUDGE_TRACE_IDLE_SECONDS` (8s) in case it arrives in several batches. So give
it ~30 seconds, then:

```bash
curl -s http://localhost:8185/evaluations | jq
```

```json
{
  "count": 1,
  "evaluations": [
    {
      "source_trace_id": "c97a5f0d7775caed5d02d90c49c5e9c5",
      "eval_trace_id": "18279d43c3351f03addeda3b27821b29",
      "root_service": "triage-agent",
      "agents": ["triage_agent", "billing_agent"],
      "models": ["gpt-4.1-mini"],
      "tools": ["billing_agent", "check_invoice_status", "issue_refund"],
      "user_input": "I was double charged on account ACC-4471. Check my invoice and refund me 42.50 USD.",
      "final_output": "The latest invoice for account ACC-4471 is unpaid and due in 3 days. A refund of $42.50 has been issued...",
      "scores": [
        { "name": "relevance",          "score": 4.73, "label": "pass", "explanation": "..." },
        { "name": "task_adherence",     "score": 3.64, "label": "pass", "explanation": "..." },
        { "name": "tool_call_accuracy", "score": 5.0,  "label": "pass", "explanation": "..." }
      ]
    }
  ]
}
```

`/evaluations` is a debug affordance — a small in-memory ring buffer, so a demo
can prove the pipeline without opening Grafana. Grafana is the record.

Two more endpoints: `/healthz`, and `/rubric` — which renders the exact prompt
this judge would send, against a sample trace. Owning the rubric is the point of
Architecture B; being able to read it without grepping the image is what makes
that ownership real.

### Sustained traffic

```bash
python scripts/simulate_traffic.py --target custom-judge    # from the repo root
```

The simulator knows this stack as the `custom-judge` target.

## Reading it in Grafana

Two traces per request, in two different places:

1. **The agent trace** — `service.name = triage-agent | billing-agent | tech-agent`.
   Identical to `distributed_agents/`; the collector is transparent to it.
2. **The eval trace** — `service.name = judge-service`, a `gen_ai.evaluate` root
   with one `gen_ai.evaluation` child per evaluator.

To go from a verdict to the conversation it judged, click the **Links**
reference on the eval span — Tempo renders OTel span links as navigable. Or
query either direction in TraceQL:

```
{ .gen_ai.evaluation.source_trace_id = "c97a5f0d7775caed5d02d90c49c5e9c5" }
{ .gen_ai.evaluation.name = "task_adherence" && .gen_ai.evaluation.score.value < 3 }
```

The judge also emits a `gen_ai.evaluation.score` **histogram**, attributed by
evaluator, label, source service and model — because "was this trace good?" is a
trace question, but "is the system getting worse?" is a metric question, and a
trace store is the wrong tool for the second.

### Attributes on the eval span

```
gen_ai.evaluate                                    <- root, own trace, parent_id: null
  gen_ai.evaluation.source_trace_id   06b5d7cf...  <- pivot back to the agent trace
  gen_ai.evaluation.source_span_id    6e8d95c9...
  gen_ai.evaluation.source_service    triage-agent
  gen_ai.agent.name                   triage_agent, tech_support_agent
  gen_ai.request.model                gpt-4.1-mini
  judge.mode                          mock
  judge.rubric.version                1.0.0
  judge.source.{span_count,error_count,duration_ms,tool_calls}
  links -> [06b5d7cf... / 6e8d95c9...]             <- OTel Link to the source root span

  gen_ai.evaluation                                <- one child per evaluator
    gen_ai.evaluation.name            relevance
    gen_ai.evaluation.score.value     3.75
    gen_ai.evaluation.score.label     pass
    gen_ai.evaluation.explanation     ...
    gen_ai.evaluation.score.range.{min,max}  1.0 / 5.0
```

`gen_ai.evaluation.*` is **experimental** in the GenAI semantic conventions —
expect these names to move. `judge.*` is this repo's own namespace, kept
separate on purpose so it is obvious which half is a standard and which half is
a local decision. `score.range.*` is local too: a bare `3.75` is
uninterpretable without the scale it came from.

## Three decisions worth defending

### 1. The eval span is a new trace, not an addition to the old one

The tempting version is to reuse the original `trace_id` so the verdict appears
inside the conversation it scores. Tempo will even accept it — trace storage has
no "closed" state.

But the original trace's blocks are already cut by then, so a late span forces a
**new** live entry for that id, which flushes to a **separate block**. One
logical trace, fragmented; every read then pays extra bloom-filter lookups until
compaction catches up. Once, that is nothing. Once per sampled trace, forever,
it is a steady drip of orphan blocks — exactly the long-running-trace failure
mode Tempo's docs warn about.

So: a normal independent trace, plus an OTel `Link`, which is the primitive
defined for "causally related to another trace, but not its child". Same
one-click navigation in Grafana, no storage cost. See
[the correlation section](../docs/observability-llm-judge.md#-important-do-not-inject-the-eval-span-into-the-original-trace).

There is a subtle way to lose this by accident. If `emit_evaluation` runs while
*any* span is active — inside an instrumented request handler, say — the
"independent" root silently becomes that span's child, and the eval quietly
joins the wrong trace. `emit.py` attaches an empty context first to prevent it,
and `selftest.py` emits from inside an unrelated span specifically to catch a
regression.

### 2. Sampling belongs in the collector, and has to be content-aware

Head sampling cannot do this: the decision needs the whole trace, and at the
first span you do not yet know whether the request errored, ran long, or was
even a conversation. `tail_sampling` buffers by `trace_id` and decides at the
end, so the judge is never handed a half-trace missing the answer it is meant to
score.

The policy is `is-an-agent-conversation AND (errored OR slow OR baseline sample)`.
That first term matters more than it looks: without it the judge also receives
boot-time agent-card lookups and bare HTTP calls — traces containing no
conversation at all. Harmless with the mock, a straight bill with `JUDGE_MODE=llm`.
It is expressed by requiring some span in the trace to carry `gen_ai.agent.name`.

`JUDGE_SAMPLING_PERCENTAGE` defaults to **100** so a handful of demo requests
actually produce verdicts. In production that is the cost dial — 5–25 is
typical, since a judge costs a model call per trace. The property `tail_sampling`
provides, trace *completeness*, holds at any rate.

### 3. The judge exports straight to Grafana, never back through the collector

It looks tidier to have one egress path. It is also a loop: the judge's eval
traces would be received by the collector, sampled, exported to the judge, and
evaluated — producing evaluations of evaluations, indefinitely.

Hence the judge's `OTEL_EXPORTER_OTLP_ENDPOINT` is left pointing at Grafana
while the agents' is overridden to the collector, and hence the
`filter/not-judge` processor on the judge pipeline, which drops anything whose
`service.name` is the judge. Nothing matches it today. It is there for the day
someone "makes the config uniform".

## Configuration

Collector — `collector/otelcol-config.yaml`, all via env:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GRAFANA_OTLP_ENDPOINT` | *required* | Grafana Cloud OTLP gateway |
| `GRAFANA_OTLP_AUTHORIZATION` | *required* | `Basic <token>`, as a header value |
| `JUDGE_OTLP_ENDPOINT` | `http://judge:4318` | Where sampled traces go |
| `JUDGE_SERVICE_NAME` | `judge-service` | Loop-guard filter target |
| `JUDGE_SAMPLING_PERCENTAGE` | `100` | Baseline sample rate |
| `OTELCOL_VERSION` | `0.137.0` | Collector image tag |

Judge — `judge_service/src/judge/settings.py`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `JUDGE_MODE` | `mock` | `mock` or `llm` |
| `JUDGE_PORT` | `4318` | OTLP/HTTP bind port |
| `JUDGE_TRACE_IDLE_SECONDS` | `8` | Quiet period before a trace is scored |
| `JUDGE_TRACE_MAX_AGE_SECONDS` | `120` | Hard ceiling on buffering |
| `JUDGE_MAX_BUFFERED_TRACES` | `1000` | Backpressure limit |
| `JUDGE_PASS_THRESHOLD` | `3.0` | Score at/above which the label is `pass` |
| `JUDGE_RECENT_SIZE` | `50` | Size of the `/evaluations` ring buffer |

`ENABLE_SENSITIVE_DATA=true` is **required**, and is forced on in
`docker-compose.yml`. Without it `gen_ai.input.messages` /
`gen_ai.output.messages` are never recorded, so the judge receives traces with
no conversation in them and logs `skipping trace with no scoreable content`.
Redaction of that text happens in the collector, not in the agents.

## Tests

The judge path runs offline, with no collector, no agents and no model:

```bash
cd custom_judge
docker compose build judge
docker run --rm -v "$PWD/judge_service/selftest.py:/tmp/selftest.py:ro" \
  --entrypoint python custom-judge-judge:latest /tmp/selftest.py
```

It builds OTLP protobuf bytes shaped like a real two-host agent trace and
asserts on decode (including gzip), conversation reconstruction, judge
determinism, and every property of the eval span that matters — independent
root, correct Link target, full attribute set.

Validate the collector config without starting anything:

```bash
docker run --rm -v "$PWD/collector/otelcol-config.yaml:/etc/otelcol/config.yaml:ro" \
  -e GRAFANA_OTLP_ENDPOINT=https://example.net/otlp \
  -e GRAFANA_OTLP_AUTHORIZATION="Basic x" \
  -e JUDGE_OTLP_ENDPOINT=http://judge:4318 \
  -e JUDGE_SERVICE_NAME=judge-service \
  -e JUDGE_SAMPLING_PERCENTAGE=100 \
  otel/opentelemetry-collector-contrib:0.137.0 validate --config=/etc/otelcol/config.yaml
```

## Debugging

| Symptom | Where to look |
| --- | --- |
| No verdicts at all | `docker compose logs judge` — is anything arriving? Then `docker compose logs collector` for export errors to `http://judge:4318`. |
| `skipping trace with no scoreable content` | `ENABLE_SENSITIVE_DATA` is off, or a non-conversation trace got past sampling. The log names the services and spans involved. |
| Verdicts arrive but not in Grafana | The judge exports on its **own** OTLP config, from `.env` — not via the collector. Check `OTEL_EXPORTER_OTLP_*` inside the judge container. |
| Traces in Grafana but none to the judge | Sampling. Raise `JUDGE_SAMPLING_PERCENTAGE`, and check the trace really has a `gen_ai.agent.name` span. |
| Want to see the exact spans | `ENABLE_CONSOLE_EXPORTERS=true` on the judge, then `docker compose logs --no-log-prefix judge` — the default prefix corrupts the JSON. |

## What this does not do

Honest limits, since the point of the repo is to be read:

- **The scores are fabricated.** `MockJudge` is deterministic on `trace_id` and
  reacts to real signals (errors, failed tools, missing tool calls, empty
  answers) so the distribution is shaped like something worth putting on a
  dashboard — but it is not an opinion about quality. `JUDGE_MODE=llm` is
  implemented and wired, but is not what this stack was verified with.
- **The buffer is in-process memory.** A judge restart drops whatever it was
  holding. That is a deliberate trade — Grafana already has the trace; only the
  verdict is lost — and it is the exact seam where a real queue belongs. See
  [scaling-high-volume.md](../docs/scaling-high-volume.md).
- **One judge replica.** Two would each receive a share of any given trace's
  spans, and each would score a fragment. Sharding by `trace_id` in front of the
  judge is the fix, and is not implemented.
- **Redaction is a starting point,** not a compliance control: four regexes over
  attribute values. Real PII handling needs a real policy.
