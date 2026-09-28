"""One entrypoint for an agent host: config -> telemetry -> agent -> serve.

Every service boots the same way and in an order that matters, so the order
lives here once rather than being repeated (and eventually broken) in each
service's ``main.py``.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from agent_framework import BaseAgent, FunctionTool

from .observability import setup_observability
from .server import serve, skills_from_tools
from .settings import ServiceSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentSpec:
    """What a service contributes: an agent, and the tools to advertise as skills.

    ``tools`` is passed explicitly rather than read back off the agent because
    the Agent Framework keeps them in an internal options dict; depending on
    that shape would couple this package to a private detail.
    """

    agent: BaseAgent
    tools: Sequence[FunctionTool] = field(default_factory=tuple)
    tags: Sequence[str] = field(default_factory=tuple)


#: Builds the agent for this host. Receives settings because some agents need
#: them -- triage turns ``peer_urls`` into its delegation tools.
AgentBuilder = Callable[[ServiceSettings], AgentSpec]


def run(build: AgentBuilder) -> None:
    """Boot one agent host.

    Takes a *builder* rather than a ready-made agent so that telemetry is
    guaranteed to be configured first. That ordering is not cosmetic: the
    Agent Framework resolves a tracer when the agent is constructed, so an
    agent built before :func:`setup_observability` keeps a no-op tracer and
    its ``invoke_agent`` / ``chat`` / ``execute_tool`` spans never reach the
    exporter -- the trace still looks populated because the HTTP spans are
    there, which makes the failure easy to miss. Passing a callable makes the
    order structural instead of a comment.
    """
    settings = ServiceSettings()
    setup_observability(settings.service_name)

    spec = build(settings)
    logger.info("agent built", extra={"agent": spec.agent.name, "tools": len(spec.tools)})

    serve(spec.agent, settings, skills_from_tools(spec.tools, tags=spec.tags))
