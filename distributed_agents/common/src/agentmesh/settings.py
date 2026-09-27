"""Deployment configuration for an A2A agent service.

Everything here varies per host/environment and therefore comes from the
environment. What the agent *is* -- its name, instructions and tools -- is
domain code and lives in each service's own ``main.py``.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    """Where this agent binds, how peers reach it, and who it can delegate to."""

    model_config = SettingsConfigDict(extra="ignore")

    host: str = Field(default="0.0.0.0", validation_alias="AGENT_HOST")
    port: int = Field(default=8000, validation_alias="AGENT_PORT")

    # Advertised in the agent card as the address *other hosts* dial. Inside a
    # compose network that is the service DNS name, not localhost, so it has to
    # be injected rather than derived from host/port.
    public_url: str = Field(validation_alias="AGENT_PUBLIC_URL")

    version: str = Field(default="1.0.0", validation_alias="AGENT_VERSION")
    rpc_path: str = Field(default="/", validation_alias="A2A_RPC_PATH")

    # Comma-separated base URLs of A2A agents this one may delegate to.
    peers: str = Field(default="", validation_alias="A2A_PEER_URLS")

    peer_timeout_seconds: float = Field(default=60.0, validation_alias="A2A_PEER_TIMEOUT_SECONDS")

    service_name: str = Field(default="agent", validation_alias="OTEL_SERVICE_NAME")

    @property
    def peer_urls(self) -> list[str]:
        return [url.strip() for url in self.peers.split(",") if url.strip()]
