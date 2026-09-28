# Copyright (c) Microsoft. All rights reserved. (sample structure based on MAF docs)
"""
Same banking balance-inquiry agent as balance_inquiry_agent.py, but built via
create_harness_agent() instead of the bare Agent() constructor.

create_harness_agent() wires in the "batteries included" providers on top of
the same chat client/tools: automatic tool-call looping, per-call history
persistence, context-window compaction, todo/mode tracking, file-based
session memory, and OpenTelemetry -- most of which Agent() leaves for you to
wire up yourself. See create_harness_agent's docstring for the full list.

Assumes you already have these set in your environment (or a .env file):
    OTEL_EXPORTER_OTLP_ENDPOINT=...
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf   # or grpc
    OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(instanceID:apiKey)>"
    OTEL_SERVICE_NAME=maf-demo-agent
    OPENAI_API_KEY=...                          # or swap to AzureOpenAIChatClient below

Run:
    pip install -r requirements.txt
    python balance_inquiry_harness_agent.py
"""

import asyncio
import logging
from typing import Annotated

from agent_framework import create_harness_agent, tool
from agent_framework.observability import configure_otel_providers, get_tracer
from agent_framework.openai import OpenAIChatClient
from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pydantic import Field

load_dotenv()  # Load .env file

logger = logging.getLogger(__name__)

_ACCOUNT_BALANCES = {
    "ACC-1001": 4523.10,
    "ACC-1002": 187.55,
    "ACC-1003": 92110.00,
}


@tool(approval_mode="never_require")
async def get_account_balance(
    account_id: Annotated[str, Field(description="Bank account ID to look up, e.g. 'ACC-1001'.")],
) -> str:
    """Get the current balance for a given bank account."""
    # Stand-in for a real core-banking API call — this shows up as its own
    # `execute_tool get_account_balance` span in your trace.
    balance = _ACCOUNT_BALANCES.get(account_id)
    if balance is None:
        return f"No account found with ID {account_id}."
    return f"Account {account_id} has a balance of ${balance:,.2f}."


async def main() -> None:
    # Turn on observability. This reads OTEL_EXPORTER_OTLP_* env vars
    # automatically and wires up tracer/meter/logger providers.
    # Set ENABLE_SENSITIVE_DATA=true in your env if you also want
    # prompts/responses/tool args captured as span attributes
    # (dev/debug only — don't do this in production without redaction,
    # since account numbers/balances would land in span attributes as-is).
    configure_otel_providers()

    agent = create_harness_agent(
        OpenAIChatClient(),
        tools=get_account_balance,
        name="BankingAgent",
        agent_instructions=(
            "You are a helpful banking assistant. You can look up account "
            "balances via the get_account_balance tool. Only look up "
            "accounts the user explicitly gives you an ID for."
        ),
        id="banking-harness-agent",
    )

    account_id = "ACC-1002"

    # The span name/attributes describe the real business operation, not a
    # hardcoded demo label — that's what makes it filterable in Grafana/App
    # Insights by account_id later, not just by a one-off trace_id.
    with get_tracer().start_as_current_span(
        "banking.balance_inquiry", kind=SpanKind.CLIENT
    ) as span:
        span.set_attribute("banking.account_id", account_id)

        session = agent.create_session()
        response = await agent.run(
            f"What's my balance on account {account_id}?", session=session
        )
        # Trace/span IDs go through structured logging (auto-correlated by
        # the OTel logging instrumentation configure_otel_providers wires up)
        # instead of a bare print, so every log line — not just this one — is
        # already tied back to the trace.
        logger.info("balance inquiry complete", extra={"account_id": account_id})
        print(f"\nAgent: {response.text}")

    # Short-lived CLI processes can exit before the batch span processor's
    # periodic export interval fires, silently dropping the last batch.
    # Force a flush once, at real process shutdown — not per-request, and
    # not inside a long-running service (there it belongs in your
    # lifespan/shutdown hook instead).
    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
