"""Deployment configuration for the judge service.

Everything that varies per environment -- where it binds, how long it waits for
a trace to settle, which judge implementation to use -- comes from the
environment. The rubric itself is code, in ``rubric.py``.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class JudgeSettings(BaseSettings):
    """Where the judge listens, and how it decides."""

    model_config = SettingsConfigDict(extra="ignore")

    host: str = Field(default="0.0.0.0", validation_alias="JUDGE_HOST")
    # 4318 is the OTLP/HTTP convention. The collector dials this, so keeping
    # the well-known port means its exporter config needs no explanation.
    port: int = Field(default=4318, validation_alias="JUDGE_PORT")

    service_name: str = Field(default="judge-service", validation_alias="OTEL_SERVICE_NAME")

    # "mock" scores without a model call -- deterministic per trace_id, so a
    # demo produces a stable, varied score distribution with no spend.
    # "llm" runs the real rubric through a chat client.
    mode: str = Field(default="mock", validation_alias="JUDGE_MODE")

    # A trace arrives as one or more OTLP batches. Wait until a trace_id has
    # been quiet this long before scoring it, so a trace split across batches
    # is judged once, whole, rather than twice in pieces.
    idle_seconds: float = Field(default=5.0, validation_alias="JUDGE_TRACE_IDLE_SECONDS")

    # Hard ceiling, so a trace that keeps dribbling in spans is still judged
    # eventually instead of being buffered forever.
    max_age_seconds: float = Field(default=120.0, validation_alias="JUDGE_TRACE_MAX_AGE_SECONDS")

    # Traces held in memory at once; beyond this the oldest are judged early.
    # A judge is not a storage system -- Grafana already has the full trace.
    max_buffered_traces: int = Field(default=1000, validation_alias="JUDGE_MAX_BUFFERED_TRACES")

    # Scores at or above this count as a pass, and set gen_ai.evaluation.score.label.
    pass_threshold: float = Field(default=3.0, validation_alias="JUDGE_PASS_THRESHOLD")

    # How many verdicts /evaluations replays. Debug affordance only.
    recent_size: int = Field(default=50, validation_alias="JUDGE_RECENT_SIZE")

    @property
    def use_llm(self) -> bool:
        return self.mode.strip().lower() == "llm"
