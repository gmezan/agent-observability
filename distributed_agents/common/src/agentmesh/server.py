"""Serve an Agent Framework agent over the standard A2A protocol."""

import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from urllib.parse import urljoin

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from agent_framework import BaseAgent, FunctionTool
from agent_framework.a2a import A2AExecutor
from fastapi import FastAPI

from .observability import flush_telemetry, instrument_app
from .settings import ServiceSettings

logger = logging.getLogger(__name__)

TEXT_MODES = ["text/plain"]


def build_agent_card(agent: BaseAgent, settings: ServiceSettings, skills: Sequence[AgentSkill]) -> AgentCard:
    """Describe this agent for A2A discovery.

    The interface URL is what peers dial, so it comes from configuration --
    a card advertising ``localhost`` is useless to anyone on another host.
    """
    return AgentCard(
        name=agent.name,
        description=agent.description,
        version=settings.version,
        default_input_modes=TEXT_MODES,
        default_output_modes=TEXT_MODES,
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[
            AgentInterface(url=urljoin(settings.public_url, settings.rpc_path), protocol_binding="JSONRPC")
        ],
        skills=list(skills),
    )


def skills_from_tools(tools: Sequence[FunctionTool], tags: Sequence[str]) -> list[AgentSkill]:
    """Derive card skills from the agent's tools.

    Keeps the advertised capabilities in step with what the agent can actually
    do, instead of a hand-maintained list that silently drifts.
    """
    return [
        AgentSkill(id=t.name, name=t.name, description=t.description or "", tags=list(tags)) for t in tools
    ]


def create_app(agent: BaseAgent, settings: ServiceSettings, skills: Sequence[AgentSkill]) -> FastAPI:
    """Build the FastAPI app exposing `agent` over A2A JSON-RPC."""
    card = build_agent_card(agent, settings, skills)

    handler = DefaultRequestHandler(
        # Non-streaming: the executor emits one consolidated text artifact
        # instead of one A2A part per token chunk.
        agent_executor=A2AExecutor(agent, stream=False),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        logger.info("agent serving over a2a", extra={"agent": agent.name, "url": settings.public_url})
        yield
        flush_telemetry()

    app = FastAPI(title=agent.name, version=settings.version, lifespan=lifespan)
    app.add_api_route("/healthz", lambda: {"status": "ok", "agent": agent.name}, methods=["GET"])

    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, settings.rpc_path),
    )

    instrument_app(app)
    return app


def serve(agent: BaseAgent, settings: ServiceSettings, skills: Sequence[AgentSkill]) -> None:
    """Run the agent's A2A service until the process is stopped."""
    uvicorn.run(create_app(agent, settings, skills), host=settings.host, port=settings.port)
