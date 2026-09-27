# Build context is distributed_agents/ so the image can pick up common/.
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Shared library first: it changes far less often than agent code, so this
# layer stays cached when only the agent is edited.
COPY common/ /app/common/
RUN uv pip install --system --prerelease=allow ./common

COPY triage_agent/ /app/triage_agent/

RUN useradd --create-home --uid 1000 agent
USER agent

EXPOSE 8000
CMD ["python", "-m", "triage_agent.main"]
