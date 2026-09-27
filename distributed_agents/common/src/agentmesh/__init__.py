"""Shared building blocks for distributed, A2A-connected Agent Framework agents."""

from .discovery import discover_peer_tools
from .observability import flush_telemetry, instrument_app, setup_observability
from .server import build_agent_card, create_app, serve, skills_from_tools
from .settings import ServiceSettings

__all__ = [
    "ServiceSettings",
    "build_agent_card",
    "create_app",
    "discover_peer_tools",
    "flush_telemetry",
    "instrument_app",
    "serve",
    "setup_observability",
    "skills_from_tools",
]
