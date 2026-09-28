# Copyright (c) Microsoft. All rights reserved. (sample structure based on MAF docs)
"""
Multi-agent handoff workflow with distributed tracing.
 
Demonstrates:
  - Three agents, each with their own tools
  - HandoffBuilder orchestration: agents decide at runtime who should
    handle the request, rather than a fixed pipeline
  - A single distributed trace spanning multiple agent invocations and
    tool calls, all correlated under one trace_id
 
Uses the same env vars as maf_agent_observability.py:
    OTEL_EXPORTER_OTLP_ENDPOINT=...
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
    OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(instanceID:apiKey)>"
    OTEL_SERVICE_NAME=maf-demo-multiagent
 
    OPENAI_API_KEY=<your DeepSeek API key>
    OPENAI_BASE_URL=https://api.deepseek.com
    OPENAI_CHAT_COMPLETION_MODEL=deepseek-chat
 
    ENABLE_SENSITIVE_DATA=true    # optional, to see prompts/tool args in spans
 
Extra install for this script:
    pip install agent-framework-orchestrations --pre
 
Run:
    pip install -r requirements.txt
    python maf_multiagent_observability.py
"""
 
import asyncio
from typing import Annotated
 
from agent_framework import Agent, tool
from agent_framework.observability import configure_otel_providers, get_tracer
from agent_framework.openai import OpenAIChatClient
from agent_framework.orchestrations import HandoffBuilder
from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from opentelemetry.trace.span import format_trace_id
from pydantic import Field
 
# --------------------------------------------------------------------------
# Tools -- each one shows up as its own `execute_tool <name>` span, nested
# under whichever agent's `invoke_agent` span called it.
# --------------------------------------------------------------------------
 
load_dotenv()  # Load .env file

@tool(approval_mode="never_require")
async def check_invoice_status(
    account_id: Annotated[str, Field(description="Customer account ID.")],
) -> str:
    """Check whether the customer's latest invoice is paid."""
    return f"Account {account_id}: latest invoice is UNPAID, due in 3 days."
 
 
@tool(approval_mode="never_require")
async def issue_refund(
    account_id: Annotated[str, Field(description="Customer account ID.")],
    amount_usd: Annotated[float, Field(description="Amount to refund, in USD.")],
) -> str:
    """Issue a refund to the customer's account."""
    return f"Refund of ${amount_usd:.2f} issued to account {account_id}."
 
 
@tool(approval_mode="never_require")
async def check_service_status(
    region: Annotated[str, Field(description="Service region, e.g. 'us-east'.")],
) -> str:
    """Check whether there's a known outage in a region."""
    return f"No known outages in {region}. All systems operational."
 
 
@tool(approval_mode="never_require")
async def restart_device(
    device_id: Annotated[str, Field(description="Device identifier to restart.")],
) -> str:
    """Trigger a remote restart of a customer device."""
    return f"Restart command sent to device {device_id}. Expect ~60s downtime."
 
 
# --------------------------------------------------------------------------
# Agents -- one chat client shared across all three; each Agent invocation
# gets its own `invoke_agent <name>` root-ish span, and the handoff between
# them is what turns this into a *distributed* trace instead of three
# separate ones.
# --------------------------------------------------------------------------
 
 
def build_agents() -> tuple[Agent, Agent, Agent]:
    # Handoff tool calls short-circuit the normal turn flow (control jumps
    # to another agent mid-conversation), so HandoffBuilder requires every
    # participant to persist its own call history locally, to stay in sync
    # with the service-side state across that jump.
    client = OpenAIChatClient()  # reads OPENAI_* env vars
 
    triage_agent = Agent(
        client=client,
        name="triage_agent",
        instructions=(
            "You are the first point of contact for customer support. "
            "Identify whether the request is a billing issue or a "
            "technical issue, then hand off to the right specialist. "
            "Do not try to resolve the issue yourself."
        ),
        require_per_service_call_history_persistence=True,
    )
 
    billing_agent = Agent(
        client=client,
        name="billing_agent",
        instructions="You resolve billing issues: invoices, payments, refunds.",
        tools=[check_invoice_status, issue_refund],
        require_per_service_call_history_persistence=True,
    )
 
    tech_agent = Agent(
        client=client,
        name="tech_support_agent",
        instructions="You resolve technical issues: outages, device restarts.",
        tools=[check_service_status, restart_device],
        require_per_service_call_history_persistence=True,
    )
 
    return triage_agent, billing_agent, tech_agent
 
 
def should_terminate(conversation) -> bool:
    """Stop once a specialist has actually resolved the request.
 
    Only inspects agent-authored messages -- never the original seed user
    message, which can itself contain trigger words (e.g. the demo prompt
    below literally says "I'd like a refund"). Matching against the raw
    conversation tail, including the user's own turn, causes the workflow
    to terminate on turn one before any agent -- or the LLM -- ever runs,
    which is exactly what produces a trace with no invoke_agent/chat spans
    and a suspiciously instant total duration.
    """
    for message in reversed(conversation):
        role = str(getattr(message, "role", "")).lower()
        if "user" in role:
            # Reached the user's turn without finding an agent reply after
            # it -- nothing to check yet.
            return False
        text = (getattr(message, "text", "") or "").lower()
        if any(phrase in text for phrase in ("refund of", "restart command sent", "resolved")):
            return True
        # Only the most recent turn matters; stop at the first message we
        # examine (an agent reply) that doesn't match.
        return False
    return False
 
 
async def main() -> None:
    # Same one-liner as the single-agent script. It's process-wide, so it
    # covers every agent, tool call, and handoff below automatically --
    # you don't instrument each agent separately.
    configure_otel_providers()
 
    triage_agent, billing_agent, tech_agent = build_agents()
 
    workflow = (
        HandoffBuilder(
            name="customer_support_handoff",
            participants=[triage_agent, billing_agent, tech_agent],
            termination_condition=should_terminate,
        )
        .with_start_agent(triage_agent)
        # Explicit handoff edges: without these, triage has nowhere to
        # route to and the workflow gets stuck (agent-framework 1.19.0 /
        # orchestrations 1.2.0 require this even when participants are
        # already listed above).
        .add_handoff(triage_agent, [billing_agent, tech_agent])
        .build()
    )
 
    # Wrapping the whole run in one root span is what makes this a single
    # *distributed* trace: every agent invocation and tool call that
    # happens underneath, across all three agents, gets correlated under
    # this one trace_id. Without it, you'd still get spans, just without
    # a shared root tying triage -> billing/tech together as one flow.
    with get_tracer().start_as_current_span(
        "Scenario: Support Handoff", kind=SpanKind.CLIENT
    ) as span:
        print(f"Trace ID: {format_trace_id(span.get_span_context().trace_id)}")
 
        user_message = (
            "Hi, I was charged twice for my subscription this month and "
            "I'd like a refund of $19.99 for account ACC-4471."
        )
        print(f"\nUser: {user_message}\n")
 
        # This version's Workflow.run() takes stream=True and returns an
        # iterable ResponseStream, rather than exposing a separate
        # run_stream() method. Each yielded event carries a `.type`
        # ("output" for a final/terminal message, "intermediate" for
        # in-progress agent/tool activity) and a `.data` payload.
        stream = workflow.run(user_message, stream=True)
        async for event in stream:
            if getattr(event, "type", None) not in ("output", "intermediate"):
                continue
            data = getattr(event, "data", None)
            # `data` shapes vary (a Message, a list[Message], raw text);
            # handle the common cases so the console output stays readable.
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                text = getattr(item, "text", None)
                author = getattr(item, "author_name", None) or getattr(item, "role", None)
                if text:
                    print(f"[{author or 'agent'}]: {text}\n")
 
        result = await stream.get_final_response()
        print(f"Final state: {result.get_final_state()}")
 
    print(
        "\n\nDone. Search the Trace ID above in Grafana Tempo or "
        "Application Insights to see the full distributed trace: "
        "triage_agent -> handoff -> billing_agent/tech_support_agent -> "
        "execute_tool spans, all under one trace."
    )
 
    # Belt-and-suspenders: force any batched spans out before the process
    # exits, so a short-lived script can't outrun the exporter's normal
    # periodic flush interval and silently drop trailing spans.
    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()
 
 
if __name__ == "__main__":
    asyncio.run(main())