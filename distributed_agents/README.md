# Distributed agents

The same triage / billing / tech support scenario as the single-host version, but
each agent now runs as **its own service, in its own container, with its own OTEL
service name**, and they talk to each other over the **A2A protocol** instead of
in-process function calls.

The point of the exercise is the observability: one customer request that crosses
three hosts still shows up in Tempo as **one trace**.

The whole mesh is deployed **twice — once per model**, so the same request can be
compared across `gpt-4.1` and `gpt-4.1-mini` on latency, token usage and routing
quality. The two instances are identical apart from `AZURE_OPENAI_CHAT_MODEL`,
and each one only ever talks to its own specialists, so a trace never straddles
the two.

```
                    instance: gpt-4.1                     instance: gpt-4.1-mini
       ┌────────────────────────────────┐      ┌────────────────────────────────┐
  ───► │ triage-agent-gpt41       :8090 │  ───►│ triage-agent-gpt41-mini  :8080 │
       │ no domain tools; peers at boot │      │ no domain tools; peers at boot │
       └──────┬──────────────────┬──────┘      └──────┬──────────────────┬──────┘
              │ A2A + traceparent│                    │ A2A + traceparent│
              ▼                  ▼                    ▼                  ▼
   ┌──────────────────┐ ┌──────────────────┐ ┌────────────────┐ ┌────────────────┐
   │ billing-   :8091 │ │ tech-      :8092 │ │ billing- :8081 │ │ tech-    :8082 │
   │ agent-gpt41      │ │ agent-gpt41      │ │ agent-…-mini   │ │ agent-…-mini   │
   │ check_invoice_…  │ │ check_service_…  │ │ (same tools)   │ │ (same tools)   │
   │ issue_refund     │ │ restart_device   │ │                │ │                │
   └──────────────────┘ └──────────────────┘ └────────────────┘ └────────────────┘
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
└── docker-compose.yml       # each image deployed twice: one instance per model
```

One folder is still one image; the second instance reuses the same three images
and only changes environment, so "run gpt-4.1 as well" costs no extra code and no
extra build.

Each service's `main.py` contains only domain code: the agent's name, its
instructions and its tools. Everything about *being a service* — how it binds,
how it's discovered, how it's traced — lives in `common/`.

## Prerequisites

Docker, and the repo-root `.env` (Compose reads `../.env`). All six services
share it, so the credentials and the OTLP destination are configured in exactly
one place:

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
service graph. `AZURE_OPENAI_CHAT_MODEL` is overridden the same way, per
instance, so whatever it says in `.env` is irrelevant to Compose: the `-gpt41`
services always run `gpt-4.1` and the `-gpt41-mini` services always run
`gpt-4.1-mini`.

## Start

```bash
cd distributed_agents

# build + start both instances; --wait blocks until all six healthchecks pass
docker compose up -d --build --wait

docker compose ps
docker compose logs -f triage-agent-gpt41
```

Each triage `depends_on` **its own** two specialists being healthy, because it
resolves their agent cards during startup — if they aren't listening yet, it
fails to boot.

To bring up a single instance, name its services:

```bash
docker compose up -d --build --wait \
  billing-agent-gpt41 tech-agent-gpt41 triage-agent-gpt41
```

| Instance | Model | triage | billing | tech |
| --- | --- | --- | --- | --- |
| `gpt41` | `gpt-4.1` | `:8090` | `:8091` | `:8092` |
| `gpt41-mini` | `gpt-4.1-mini` | `:8080` | `:8081` | `:8082` |

Tear down with:

```bash
docker compose down
```

## Try it

The examples below target the **`gpt-4.1-mini` instance on `:8080`**; point them
at `:8090` to run the identical request through `gpt-4.1`.

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
  "supportedInterfaces": [{ "url": "http://triage-agent-gpt41-mini:8000/", "protocolBinding": "JSONRPC" }]
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
`http://localhost:8082/.well-known/agent-card.json`. The `gpt-4.1` specialists
are the same two agents on `:8091` and `:8092`.

### Compare the two models

Run the same prompt through both front doors and diff the answers, the tool
calls and the wall time:

```bash
for port in 8080 8090; do
  curl -s -X POST "http://localhost:$port/" \
    -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
    -d '{"jsonrpc":"2.0","id":"6","method":"SendMessage","params":{"message":{"messageId":"msg-6","role":"ROLE_USER","parts":[{"text":"Something is wrong with my service but I am not sure what. Account ACC-1001, device DEV-77."}]}}}' \
    | jq -r '.result.task.history[] | select(.role=="ROLE_AGENT") | .parts[].text'
done
```

Deliberately vague requests are the interesting ones: routing is the decision the
two models are most likely to disagree on, and the disagreement is visible in the
trace as a different `execute_tool` span.

For a continuous comparison rather than a one-off, see
[`scripts/simulate_traffic.py`](../scripts/simulate_traffic.py) — it drives both
instances with randomly timed, randomly chosen prompts:

```bash
python scripts/simulate_traffic.py            # from the repo root
```

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
[triage-agent-gpt41-mini] POST /                <- server span, extracts traceparent
 └─ invoke_agent triage_agent
     ├─ chat gpt-4.1-mini                       <- routing decision
     ├─ execute_tool tech_support_agent
     │   └─ POST                                <- httpx client span, injects traceparent
     │       └─ [tech-agent-gpt41-mini] POST /  <== CROSSES HOST BOUNDARY
     │           └─ invoke_agent tech_support_agent
     │               ├─ chat gpt-4.1-mini
     │               ├─ execute_tool check_service_status
     │               ├─ execute_tool restart_device
     │               └─ chat gpt-4.1-mini
     └─ chat gpt-4.1-mini                       <- relays the specialist's answer
```

The same request on `:8090` produces the same shape with `gpt-4.1` in the `chat`
spans and `-gpt41` service names.

### Telling the two instances apart

Every span from an instance carries its model in the resource, so a dashboard can
split on it without parsing the `chat` span names:

| Attribute | `gpt-4.1` instance | `gpt-4.1-mini` instance |
| --- | --- | --- |
| `service.name` | `triage-agent-gpt41`, … | `triage-agent-gpt41-mini`, … |
| `service.instance.id` | `gpt-4.1` | `gpt-4.1-mini` |
| `llm.model` | `gpt-4.1` | `gpt-4.1-mini` |

`service.instance.id` is the OTEL-native way to distinguish two deployments of
the same service; `llm.model` is a convenience so panels can group by model
directly.

## Debugging switches

Both are environment variables; set them in `docker-compose.yml` and
`docker compose up -d` to apply.

| Variable | Default | Effect |
| --- | --- | --- |
| `ENABLE_CONSOLE_EXPORTERS` | unset | Print every span as JSON to stdout. Read them with `docker compose logs --no-log-prefix triage-agent-gpt41` — the default log prefix corrupts the JSON. |
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

Not read by `ServiceSettings`, but set per instance in `docker-compose.yml`:

| Variable | Purpose |
| --- | --- |
| `AZURE_OPENAI_CHAT_MODEL` | The model this instance runs. Overrides `.env`. |
| `OTEL_RESOURCE_ATTRIBUTES` | Carries `service.instance.id` and `llm.model` for the instance. |

## Adding another specialist

1. Copy `tech_agent/` to `<name>_agent/`, and in `main.py` change the agent's
   name, description, instructions and tools.
2. Copy its `Dockerfile`, updating the two `<name>_agent` paths.
3. Add a service block to `docker-compose.yml` **per instance**, reusing the
   `*agent` anchor and the instance's env anchors
   (`<<: [*agent-env, *gpt41-env]`), with its own `OTEL_SERVICE_NAME` and
   `AGENT_PUBLIC_URL`.
4. Append its URL to that instance's triage `A2A_PEER_URLS` and add it to that
   triage's `depends_on`.

No change to `triage_agent/main.py`: the new agent's card supplies the tool name
and description the router reasons over, so write the agent's `description`
carefully — it *is* the routing prompt.
