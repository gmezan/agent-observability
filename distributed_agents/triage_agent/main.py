"""Triage front door -- routes customer requests to specialist agents over A2A.

This host owns no domain tools of its own. At boot it resolves the agent card
of every peer in ``A2A_PEER_URLS`` and turns each one into a delegation tool,
so routing decisions are driven by what the specialists advertise about
themselves rather than by anything hardcoded here.
"""

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient
from agentmesh import AgentSpec, ServiceSettings, discover_peer_tools, run

INSTRUCTIONS = (
    "You are the first point of contact for customer support at a bank. "
    "Each of your tools is a specialist agent running on another host; the "
    "tool description says what that specialist handles. Read the customer's "
    "request, pick the specialist whose description matches, and hand off the "
    "request to it along with every detail the customer gave you (account "
    "IDs, amounts, device IDs, regions). "
    "Do not attempt to resolve the issue yourself and do not invent details. "
    "When the specialist replies, relay its answer back to the customer."
)


def build(settings: ServiceSettings) -> AgentSpec:
    tools = discover_peer_tools(settings.peer_urls, settings.peer_timeout_seconds)
    return AgentSpec(
        agent=Agent(
            client=OpenAIChatClient(),
            name="triage_agent",
            description="Front door for customer support; routes requests to the right specialist agent.",
            instructions=INSTRUCTIONS,
            tools=tools,
        ),
        tools=tools,
        tags=["triage", "routing"],
    )


if __name__ == "__main__":
    run(build)
