# Build context is custom_judge/judge_service/ -- the service is self-contained
# and shares no code with the agents it grades, on purpose: it must be
# deployable without the agent images, and it must not drift when they change.
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependencies resolve from the manifest alone, so this layer stays cached
# while the rubric and the judge logic are iterated on.
COPY pyproject.toml /app/
RUN mkdir -p /app/src/judge && touch /app/src/judge/__init__.py \
    && uv pip install --system --prerelease=allow .

COPY src/ /app/src/
RUN uv pip install --system --no-deps .

RUN useradd --create-home --uid 1000 judge
USER judge

# OTLP/HTTP's conventional port: the collector's otlphttp exporter appends
# /v1/traces to whatever endpoint it is given.
EXPOSE 4318
CMD ["python", "-m", "judge.main"]
