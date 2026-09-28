"""Billing specialist -- an A2A agent host for invoices, payments and refunds."""

from typing import Annotated

from agent_framework import Agent, tool
from agent_framework.openai import OpenAIChatClient
from agentmesh import AgentSpec, ServiceSettings, run
from pydantic import Field

INSTRUCTIONS = (
    "You resolve billing issues for a bank: invoices, payments and refunds. "
    "Use your tools to look up and act on the customer's account. "
    "State clearly what you did and the resulting account state."
)


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


def build(_: ServiceSettings) -> AgentSpec:
    tools = [check_invoice_status, issue_refund]
    return AgentSpec(
        agent=Agent(
            client=OpenAIChatClient(),
            name="billing_agent",
            description="Resolves billing issues: invoice status, payments and refunds.",
            instructions=INSTRUCTIONS,
            tools=tools,
        ),
        tools=tools,
        tags=["billing"],
    )


if __name__ == "__main__":
    run(build)
