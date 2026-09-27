"""Discover peer agents over A2A and expose them as delegation tools."""

import asyncio
import logging

import httpx
from a2a.client import A2ACardResolver
from a2a.types import AgentCard
from agent_framework import FunctionTool
from agent_framework.a2a import A2AAgent

logger = logging.getLogger(__name__)


async def _fetch_cards(urls: list[str], timeout: float) -> list[AgentCard]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resolvers = [A2ACardResolver(httpx_client=client, base_url=url) for url in urls]
        return list(await asyncio.gather(*(r.get_agent_card() for r in resolvers)))


def discover_peer_tools(urls: list[str], timeout: float) -> list[FunctionTool]:
    """Resolve each peer's agent card and wrap it as a callable tool.

    The tool's name and description come from the remote card, so this host
    never hardcodes what its peers are called or what they do -- adding a new
    specialist is a URL in ``A2A_PEER_URLS``, not a code change.

    Runs before the server's event loop starts, so failures surface as a clean
    boot error rather than a runtime tool failure. The throwaway httpx client
    used for discovery is closed here; each ``A2AAgent`` lazily creates its own
    for the serving loop.
    """
    if not urls:
        return []

    cards = asyncio.run(_fetch_cards(urls, timeout))
    tools = []
    for card in cards:
        peer = A2AAgent(agent_card=card, timeout=timeout)
        logger.info("discovered peer agent", extra={"peer": card.name})
        tools.append(peer.as_tool(arg_description="The customer request to hand off, with all relevant context."))
    return tools
