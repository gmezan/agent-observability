# Copyright (c) Microsoft. All rights reserved. (sample structure based on MAF docs)
"""
Minimal Microsoft Agent Framework agent with OpenTelemetry observability enabled.

Assumes you already have these set in your environment (or a .env file):
    OTEL_EXPORTER_OTLP_ENDPOINT=...
    OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf   # or grpc
    OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(instanceID:apiKey)>"
    OTEL_SERVICE_NAME=maf-demo-agent
    OPENAI_API_KEY=...                          # or swap to AzureOpenAIChatClient below

Run:
    pip install -r requirements.txt
    python maf_agent_observability.py
"""

import asyncio
from typing import Annotated

from agent_framework import Agent, tool
from agent_framework.observability import configure_otel_providers, get_tracer
from agent_framework.openai import OpenAIChatCompletionClient
from dotenv import load_dotenv
from opentelemetry.trace import SpanKind
from opentelemetry.trace.span import format_trace_id
from pydantic import Field

load_dotenv()  # Load .env file

@tool(approval_mode="never_require")
async def get_weather(
    location: Annotated[str, Field(description="City to get the weather for.")],
) -> str:
    """Get the current weather for a given location."""
    # Stand-in for a real API call — this shows up as its own
    # `execute_tool get_weather` span in your trace.
    return f"The weather in {location} is sunny with a high of 22°C."


async def main() -> None:
    # 1. Turn on observability. This reads OTEL_EXPORTER_OTLP_* env vars
    #    automatically and wires up tracer/meter/logger providers.
    #    Set ENABLE_SENSITIVE_DATA=true in your env if you also want
    #    prompts/responses/tool args captured as span attributes
    #    (dev/demo only — don't do this in production without redaction).
    configure_otel_providers()

    agent = Agent(
        client=OpenAIChatCompletionClient(),
        tools=get_weather,
        name="WeatherAgent",
        instructions="You are a helpful weather assistant.",
        id="weather-agent",
    )

    # Wrapping the whole run in a manual span is optional, but it gives you
    # a single trace_id to search for in Grafana/App Insights afterward.
    with get_tracer().start_as_current_span("Scenario: Weather Chat", kind=SpanKind.CLIENT) as span:
        print(f"Trace ID: {format_trace_id(span.get_span_context().trace_id)}")

        thread = agent.create_session()
        response = await agent.run("What's the weather like in Lima?", session=thread)
        print(f"\nAgent: {response.text}")


if __name__ == "__main__":
    asyncio.run(main())