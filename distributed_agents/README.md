# Distributed agents

The same triage / billing / tech support scenario as the single-host version, but
each agent now runs as **its own service, in its own container, with its own OTEL
service name**, and they talk to each other over the **A2A protocol** instead of
in-process function calls.

The point of the exercise is the observability: one customer request that crosses
three hosts still shows up in Tempo as **one trace**.

```
                 ┌──────────────────────────────────────────────┐
  curl ────────► │  triage-agent            :8080               │
                 │  no domain tools; discovers peers at boot    │
                 └───────┬──────────────────────────┬───────────┘
                         │ A2A + traceparent        │ A2A + traceparent
                         ▼                          ▼
          ┌──────────────────────┐      ┌──────────────────────────┐
          │ billing-agent  :8081 │      │ tech-agent         :8082 │
          │ check_invoice_status │      │ check_service_status     │
          │ issue_refund         │      │ restart_device           │
          └──────────────────────┘      └──────────────────────────┘
```

Triage has **no hardcoded knowledge of its specialists**. At boot it resolves the
agent card of every URL in `A2A_PEER_URLS` and turns each one into a delegation
tool, using the remote agent's own name and description. Adding a fourth
specialist is a URL in `docker-compose.yml`, not a code change.

## Layout

```
distributed_agents/
├── common/                  # "agentmesh": the shared plumbing, installed into every image
│   └── src/agentmesh/
│       ├── settings.py      # env-driven deployment config (bind, public URL, peers)
│       ├── observability.py # OTEL providers + the instrumentation that carries traceparent
│       ├── server.py        # agent card, A2A routes, FastAPI app, uvicorn entrypoint
│       └── discovery.py     # A2ACardResolver -> A2AAgent -> tool
├── billing_agent/           # one folder = one host = one image
├── tech_agent/
├── triage_agent/
└── docker-compose.yml
```

Each service's `main.py` contains only domain code: the agent's name, its
instructions and its tools. Everything about *being a service* — how it binds,
how it's discovered, how it's traced — lives in `common/`.

A service is therefore a `main()` that wires four things together:

```python
def main() -> None:
    settings = ServiceSettings()
    setup_observability(settings.service_name)

    tools = [check_invoice_status, issue_refund]
    agent = Agent(client=OpenAIChatClient(), name="billing_agent", ..., tools=tools)

    serve(agent, settings, skills_from_tools(tools, tags=["billing"]))
```

Startup order is a readability convention, not a correctness requirement:
`configure_otel_providers()` may be called after the agent exists. The Agent
Framework resolves its tracer lazily, per span, and `HTTPXClientInstrumentor`
patches httpx at class level, so objects built beforehand are still traced
(both verified — see below).

## Prerequisites

Docker, and the repo-root `.env` (Compose reads `../.env`). All three services
share it, so the model and the OTLP destination are configured in exactly one
place:

```bash
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_CHAT_MODEL=gpt-4.1-mini
AZURE_OPENAI_API_VERSION=2024-10-21

OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp-gateway-<region>.grafana.net/otlp
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20<token>
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
ENABLE_SENSITIVE_DATA=true
```

`OTEL_SERVICE_NAME` from `.env` is deliberately overridden per service in
`docker-compose.yml` — that's what makes each agent a distinct node in the Tempo
service graph.

## Start

```bash
cd distributed_agents

# build + start; --wait blocks until all three healthchecks pass
docker compose up -d --build --wait

docker compose ps
docker compose logs -f triage-agent
```

Triage `depends_on` both specialists being **healthy**, because it resolves their
agent cards during startup — if they aren't listening yet, it fails to boot.

Tear down with:

```bash
docker compose down
```

## Try it

Two headers are mandatory on every A2A call:

- `Content-Type: application/json`
- `A2A-Version: 1.0` — **omit it and the request is rejected.** The SDK defaults
  the header to `0.3` and the handler answers
  `-32009 A2A version '0.3' is not supported by this handler`.

### Health and discovery

```bash
curl -s http://localhost:8080/healthz
# {"status":"ok","agent":"triage_agent"}

curl -s http://localhost:8080/.well-known/agent-card.json | jq
```

Triage's card is the proof that discovery worked — its skills are the *remote*
agents it found, not local functions:

```json
{
  "name": "triage_agent",
  "description": "Front door for customer support; routes requests to the right specialist agent.",
  "skills": [
    { "id": "billing_agent",      "description": "Resolves billing issues: invoice status, payments and refunds." },
    { "id": "tech_support_agent", "description": "Resolves technical issues: service outages and device restarts." }
  ],
  "supportedInterfaces": [{ "url": "http://triage-agent:8000/", "protocolBinding": "JSONRPC" }]
}
```

### Route a billing request through triage

```bash
curl -s -X POST http://localhost:8080/ \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-1",
        "role": "ROLE_USER",
        "parts": [{ "text": "I was double charged on account ACC-4471. Check my invoice and refund me 42.50 USD." }]
      }
    }
  }' | jq -r '.result.task.history[] | select(.role=="ROLE_AGENT") | .parts[].text'
```

```
The latest invoice on your account ACC-4471 is unpaid and due in 3 days. I have
processed a refund of $42.50 for the double charge you reported. If you need
further assistance, please let me know.
```

> The agents are configured non-streaming (`A2AExecutor(stream=False)`), so the
> answer arrives as a single `ROLE_AGENT` message in `result.task.history[]` —
> **not** in `task.artifacts`. Drop the `jq` filter to see the full task, including
> `status.state: TASK_STATE_COMPLETED`.

### Route a technical request through triage

```bash
curl -s -X POST http://localhost:8080/ \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d '{
    "jsonrpc": "2.0",
    "id": "2",
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-2",
        "role": "ROLE_USER",
        "parts": [{ "text": "My device DEV-88 in us-east is offline. Check for outages and restart it." }]
      }
    }
  }' | jq -r '.result.task.history[] | select(.role=="ROLE_AGENT") | .parts[].text'
```

```
There are no service outages in the us-east region; all systems are operational.
I have sent a restart command to your device DEV-88. It should be back online
shortly after about a minute of downtime.
```

### Call a specialist directly

Each specialist is a first-class A2A agent, reachable without going through
triage — useful for isolating whether a problem is routing or domain logic:

```bash
# billing
curl -s -X POST http://localhost:8081/ \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"3","method":"SendMessage","params":{"message":{"messageId":"msg-3","role":"ROLE_USER","parts":[{"text":"What is the status of my invoice for ACC-9001?"}]}}}' \
  | jq -r '.result.task.history[] | select(.role=="ROLE_AGENT") | .parts[].text'

# tech support
curl -s -X POST http://localhost:8082/ \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"4","method":"SendMessage","params":{"message":{"messageId":"msg-4","role":"ROLE_USER","parts":[{"text":"Is there an outage in eu-west?"}]}}}' \
  | jq -r '.result.task.history[] | select(.role=="ROLE_AGENT") | .parts[].text'
```

Their cards are at `http://localhost:8081/.well-known/agent-card.json` and
`http://localhost:8082/.well-known/agent-card.json`.

## Trace correlation

### How it works

On a single host, one request is one trace for free: the Agent Framework's spans
nest through Python contextvars. Across hosts that context doesn't travel — the
**W3C `traceparent` header** is the only thing tying the two processes together.
So every service does both halves of the handshake, wired in
`common/src/agentmesh/observability.py`:

| Leg | Instrumentation | Job |
| --- | --- | --- |
| Inbound | `FastAPIInstrumentor` | **extracts** `traceparent` from the incoming A2A POST and makes it the parent of everything the agent does |
| Outbound | `HTTPXClientInstrumentor` | **injects** `traceparent` into calls to peer agents (and to the model, since the OpenAI SDK is httpx-based too) |
| Logs | `LoggingInstrumentor` | stamps `trace_id` / `span_id` onto every log record, so logs in Loki join the trace in Tempo |

`OTEL_PROPAGATORS=tracecontext,baggage` is set explicitly in `docker-compose.yml`
rather than left to the SDK default.

One subtlety that makes this work: A2A's `DefaultRequestHandler` dispatches the
agent run on a background `asyncio` task. `asyncio.create_task` copies
contextvars, so the extracted span context survives the hop off the request
coroutine — the agent spans stay under the HTTP server span instead of becoming
orphan roots.

### Does A2A propagate traceparent by itself? No.

It's commonly claimed that the A2A SDKs propagate `traceparent` across the hop
automatically, so distributed tracing costs zero code. That is **not** true of
`a2a-sdk` 1.1.5: `a2a/utils/telemetry.py` imports only `opentelemetry.trace`,
and there is no `inject`, `extract` or propagator call anywhere in the package.
The SDK *creates spans*; it never touches trace context.

Propagation works here only because A2A rides on plain HTTP — httpx on the
client side, ASGI on the server side — and those two libraries are instrumented.
Measured on this stack, same request, same topology:

| Configuration | Cross-host trace | `invoke_agent` spans | Outcome |
| --- | --- | --- | --- |
| **Explicit instrumentors** (this repo) | one trace, 12 spans | present | clean nesting |
| A2A SDK's own tracing, no HTTP instrumentors | **two traces**, 66 + 65 spans | — | correlation silently lost |
| `opentelemetry-instrument` (zero-code) | one trace, 8 spans | **missing** | orphaned parents, `http receive` noise |

The middle row is the trap: with the SDK's own tracing enabled you get *more*
spans than the working setup, so it looks thoroughly instrumented right up until
you search Tempo and find the hop isn't there.

Zero-code auto-instrumentation does restore correlation, but it installs its own
tracer provider before the app starts, which costs the Agent Framework's
`invoke_agent` spans — the ones that make this *agent* observability rather than
HTTP observability. Hence three explicit instrumentor calls in
`observability.py`, which is the "install the instrumentor for whatever makes
the network call" advice, just written down where it can be seen.

### What the SDKs do and don't give you

`common/` is deliberately thin, but it isn't zero, and it's worth being precise
about why each piece can't just be an SDK call. Checked against the installed
versions (`agent-framework` 1.19.0, `a2a-sdk` 1.1.5):

| Concern | SDK facility used | What's left hand-written |
| --- | --- | --- |
| Providers, exporters, gen_ai spans | `configure_otel_providers()` (Agent Framework) | nothing — one call, env-driven |
| A2A routes + JSON-RPC dispatch | `add_a2a_routes_to_fastapi`, `create_agent_card_routes`, `create_jsonrpc_routes` | nothing — this *is* the documented pattern |
| Serving an agent over A2A | `A2AExecutor` (Agent Framework), `DefaultRequestHandler` (a2a-sdk) | nothing |
| Peer discovery | `A2ACardResolver` + `A2AAgent.as_tool()` | a loop over `A2A_PEER_URLS` |
| **traceparent across the hop** | `HTTPXClientInstrumentor`, `FastAPIInstrumentor` | 3 calls — no A2A/MAF equivalent exists |
| **Building the AgentCard / skills** | — | `build_agent_card()`, `skills_from_tools()` |

Two things are worth knowing before trying to delete more:

* **`a2a-sdk` 1.1.5 has no application class.** There is no `a2a.server.apps`
  package and no `A2AFastAPIApplication` / `A2AStarletteApplication`;
  `add_a2a_routes_to_fastapi(app, ...)` mutates a `FastAPI` instance *you* own
  and returns `None`. Owning the app is the point — it's what lets us add
  `/healthz`, a lifespan that flushes telemetry, and
  `FastAPIInstrumentor.instrument_app()`.
* **No SDK builds an `AgentCard` from an agent.** `a2a.utils` exposes only
  constants and proto helpers; `minimal_agent_card()` is a client-side stub with
  empty skills. So the card is assembled field by field.

### Pin a trace id to find it in Tempo

Supply your own `traceparent` and you know exactly what to search for. The format
is `00-<32 hex>-<16 hex>-01`, and it must be **real hex** — an invalid id is
silently dropped and the agent just starts a fresh trace.

```bash
curl -s -X POST http://localhost:8080/ \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01' \
  -d '{"jsonrpc":"2.0","id":"5","method":"SendMessage","params":{"message":{"messageId":"msg-5","role":"ROLE_USER","parts":[{"text":"Refund 42.50 USD on ACC-4471."}]}}}' | jq
```

Then look up `4bf92f3577b34da6a3ce929d0e0e4736` in Tempo.

### What you should see

One trace, twelve spans, two services — the specialist's server span parented by
the caller's client span:

```
[triage] POST /                                 <- server span, extracts traceparent
 └─ invoke_agent triage_agent
     ├─ chat gpt-4.1-mini                       <- routing decision
     ├─ execute_tool tech_support_agent
     │   └─ POST                                <- httpx client span, injects traceparent
     │       └─ [tech] POST /                   <== CROSSES HOST BOUNDARY
     │           └─ invoke_agent tech_support_agent
     │               ├─ chat gpt-4.1-mini
     │               ├─ execute_tool check_service_status
     │               ├─ execute_tool restart_device
     │               └─ chat gpt-4.1-mini
     └─ chat gpt-4.1-mini                       <- relays the specialist's answer
```

## Debugging switches

Both are environment variables; set them in `docker-compose.yml` and
`docker compose up -d` to apply.

| Variable | Default | Effect |
| --- | --- | --- |
| `ENABLE_CONSOLE_EXPORTERS` | unset | Print every span as JSON to stdout. Read them with `docker compose logs --no-log-prefix triage-agent` — the default log prefix corrupts the JSON. |
| `OTEL_INSTRUMENTATION_A2A_SDK_ENABLED` | `false` | Set to `true` to trace the A2A SDK's own queue/dispatcher internals. Useful for protocol-level debugging, but it buries the twelve agent spans under ~150 total. |

The FastAPI instrumentation also excludes the ASGI `receive`/`send` spans and the
`/healthz` route, which otherwise add constant noise to every trace.

## Configuration reference

Read by `ServiceSettings` in `common/src/agentmesh/settings.py`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENT_HOST` | `0.0.0.0` | Bind address |
| `AGENT_PORT` | `8000` | Bind port |
| `AGENT_PUBLIC_URL` | *required* | Address **other hosts** dial; advertised in the agent card. Inside Compose this is the service DNS name, not `localhost`. |
| `AGENT_VERSION` | `1.0.0` | Version published in the agent card |
| `A2A_RPC_PATH` | `/` | Path the JSON-RPC endpoint is mounted at |
| `A2A_PEER_URLS` | *empty* | Comma-separated base URLs to discover and delegate to. Only triage sets it. |
| `A2A_PEER_TIMEOUT_SECONDS` | `60` | Timeout for card resolution and delegated calls |
| `OTEL_SERVICE_NAME` | `agent` | Service name in Tempo; set per service |

## Adding another specialist

1. Copy `tech_agent/` to `<name>_agent/`, and in `main.py` change the agent's
   name, description, instructions and tools.
2. Copy its `Dockerfile`, updating the two `<name>_agent` paths.
3. Add a service block to `docker-compose.yml` reusing the `*agent` and
   `*agent-env` anchors, with its own `OTEL_SERVICE_NAME` and `AGENT_PUBLIC_URL`.
4. Append its URL to triage's `A2A_PEER_URLS` and add it to triage's
   `depends_on`.

No change to `triage_agent/main.py`: the new agent's card supplies the tool name
and description the router reasons over, so write the agent's `description`
carefully — it *is* the routing prompt.
