# Two-model stack

Runs the triage / billing / tech mesh **twice — once per model** so the same
request can be compared across `gpt-4.1` and `gpt-4.1-mini`. The two instances
are identical apart from `AZURE_OPENAI_CHAT_MODEL`, so any difference in the
traces is the model.

This is a **deployment only**: it never builds. `distributed_agents/` still owns
the code, the Dockerfiles and the single-instance stack; this folder just starts
the images it produces, twice, with different environment.

| Instance | Model | triage | billing | tech |
| --- | --- | --- | --- | --- |
| `gpt41` | `gpt-4.1` | `:8090` | `:8091` | `:8092` |
| `gpt41-mini` | `gpt-4.1-mini` | `:8080` | `:8081` | `:8082` |

## Run

Build the images once (only needed after a code change):

```bash
cd distributed_agents && docker compose build
```

That tags them `distributed-agents-{billing,tech,triage}-agent:latest`, which is
what this stack expects. Then:

```bash
cd scripts/two-model-stack
docker compose up -d --wait        # all six
docker compose ps
docker compose logs -f triage-agent-gpt41
docker compose down
```

The ports overlap with `distributed_agents/`'s own stack, so stop that one first
if it's running (`cd distributed_agents && docker compose down`).

To bring up a single instance:

```bash
docker compose up -d --wait billing-agent-gpt41 tech-agent-gpt41 triage-agent-gpt41
```

Credentials and the OTLP destination come from the repo-root `.env`, read as
`../../.env`.

## Generate traffic

```bash
python scripts/simulate_traffic.py          # from the repo root; both instances
```

See [`../simulate_traffic.py`](../simulate_traffic.py) for its options.

## Telling the two instances apart in Tempo

Every span carries its instance's model in the resource, so a dashboard can
split on it without parsing `chat` span names:

| Attribute | `gpt-4.1` instance | `gpt-4.1-mini` instance |
| --- | --- | --- |
| `service.name` | `triage-agent-gpt41`, … | `triage-agent-gpt41-mini`, … |
| `service.instance.id` | `gpt-4.1` | `gpt-4.1-mini` |
| `llm.model` | `gpt-4.1` | `gpt-4.1-mini` |

`service.instance.id` is the OTEL-native way to distinguish two deployments of
the same service; `llm.model` is a convenience so panels can group by model
directly.

Each triage only discovers **its own** specialists via `A2A_PEER_URLS`, so a
trace never straddles the two models.

## Notes

- `pull_policy: missing` is set on every service. These are local build
  artifacts, not registry images; without it Compose tries to pull and fails
  with a misleading `pull access denied / repository does not exist` instead of
  saying the image isn't built.
- Image tags are overridable — `BILLING_AGENT_IMAGE`, `TECH_AGENT_IMAGE`,
  `TRIAGE_AGENT_IMAGE` — if you push them to a registry under different names.
- The compose project is named `agents-two-models`, separate from
  `distributed-agents`, so the two stacks never adopt each other's containers.
