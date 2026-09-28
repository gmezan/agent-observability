"""Technical support specialist -- an A2A agent host for outages and devices."""

from typing import Annotated

from agent_framework import Agent, tool
from agent_framework.openai import OpenAIChatClient
from agentmesh import ServiceSettings, serve, setup_observability, skills_from_tools
from pydantic import Field

INSTRUCTIONS = (
    "You resolve technical issues for a bank's customers: service outages and "
    "device problems. Use your tools to diagnose and act, then tell the "
    "customer what you found and what you did."
)


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


def main() -> None:
    settings = ServiceSettings()
    setup_observability(settings.service_name)

    tools = [check_service_status, restart_device]
    agent = Agent(
        client=OpenAIChatClient(),
        name="tech_support_agent",
        description="Resolves technical issues: service outages and device restarts.",
        instructions=INSTRUCTIONS,
        tools=tools,
    )

    serve(agent, settings, skills_from_tools(tools, tags=["technical-support"]))


if __name__ == "__main__":
    main()
