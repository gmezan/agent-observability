# agent-observability

Demo material for a talk on instrumenting LLM agents with OpenTelemetry, and
on evaluating them with an LLM-as-judge pipeline — built on the [Microsoft
Agent Framework](https://github.com/microsoft/agent-framework) (MAF), traced
into [Grafana Tempo](https://grafana.com/oss/tempo/).

Each folder is a self-contained, runnable stage, roughly in the order you'd
walk through them:

| Stage | What it demonstrates |
| --- | --- |
| [`single_host_agents/`](single_host_agents/) | One process, zero infra: how much tracing `Agent`/`create_harness_agent()` give you for free, and a multi-agent handoff workflow in a single trace. |
| [`distributed_agents/`](distributed_agents/) | The same scenario split across three services talking over **A2A**, correlated into **one trace** via W3C `traceparent` propagation. |
| [`scripts/two-model-stack/`](scripts/two-model-stack/) | The distributed stack run **twice**, once per model (`gpt-4.1` vs `gpt-4.1-mini`), so traces can be compared side by side. |
| [`custom_judge/`](custom_judge/) | [Architecture B](docs/architecture-custom-judge.md) running: a standalone OTel Collector fans out 100% of traces to Tempo and a sampled subset to a **judge service** that scores them and emits a linked `gen_ai.evaluation` trace. |

The scenario is the same throughout: a banking support bot (triage, billing,
tech support) handles balance inquiries, refunds, and outage/restart requests
— simple enough to read in one sitting, with enough tool calls and handoffs
to produce traces worth looking at.

## Start here: the docs

[`docs/`](docs/) is the consolidating reference — read
[`docs/observability-llm-judge.md`](docs/observability-llm-judge.md) first:

- [Observability + LLM-as-a-Judge](docs/observability-llm-judge.md) — what the
  Agent Framework instruments for free, in-process SDK vs. a standalone
  Collector, and why an evaluation verdict must be a *new, linked* trace
  rather than injected into the original one.
- [Architecture A — Azure AI Foundry evaluations + App Insights](docs/architecture-azure-evaluations.md)
- [Architecture B — custom judge, no App Insights](docs/architecture-custom-judge.md) — the one actually implemented, in `custom_judge/`.
- [Scaling for high-volume traffic](docs/scaling-high-volume.md) — trace-aware
  Collector routing (`tail_sampling` needs every span of a trace on the same
  instance) and buffering the judge behind Azure Event Hub.

## Prerequisites

- Python 3.10+ and [`uv`](https://docs.astral.sh/uv/) for `single_host_agents/`.
- Docker + Docker Compose for `distributed_agents/`, `scripts/two-model-stack/`
  and `custom_judge/`.
- A Grafana Cloud (or any OTLP-compatible) endpoint, and an Azure OpenAI (or
  OpenAI-compatible) deployment.
- Copy `.env.sample` to `.env` at the repo root and fill it in — every stack
  reads from this one file.

```bash
cp .env.sample .env
# fill in AZURE_OPENAI_* and OTEL_EXPORTER_OTLP_* (and GRAFANA_OTLP_* if you'll run custom_judge/)
```

## Quickstart

```bash
# 1. Single process, no containers
uv sync
uv run python single_host_agents/balance_inquiry_agent.py

# 2. Three services, one trace across hosts
cd distributed_agents && docker compose up -d --build --wait
# see distributed_agents/README.md for curl examples and trace correlation details

# 3. Same scenario, both models side by side
cd scripts/two-model-stack && docker compose up -d --wait

# 4. Collector + judge pipeline, scoring sampled traces
cd custom_judge && docker compose up -d --build --wait
```

Generate sustained traffic against any of the container stacks with
[`scripts/simulate_traffic.py`](scripts/simulate_traffic.py):

```bash
python scripts/simulate_traffic.py --target gpt-4.1-mini   # or gpt-4.1, custom-judge
```

## What's real vs. mocked

Everything is real — instrumentation, propagation, Collector pipelines,
sampling, trace→conversation reconstruction, span emission and linking —
**except the judge's scoring itself**: `custom_judge/` ships a deterministic
`MockJudge` by default (`JUDGE_MODE=mock`), with `LLMJudge` implemented and
wired behind `JUDGE_MODE=llm` but not what the stack was demoed with. See
[`custom_judge/README.md`](custom_judge/README.md#what-this-does-not-do) for
the full list of honest limits.

## Screenshots

`assets/` has example captures from Grafana: [`dashboard.png`](assets/dashboard.png),
[`agent-trace.png`](assets/agent-trace.png), and
[`multiagent-trace.png`](assets/multiagent-trace.png) (a cross-host trace from
`distributed_agents/`).
