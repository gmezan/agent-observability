"""The judge service: an OTLP receiver with an opinion.

    collector --OTLP/HTTP--> POST /v1/traces --> TraceBuffer --> summarize
                                                                     |
                                                                 Judge.evaluate
                                                                     |
                                                  new eval trace + Link --> Grafana

The POST handler does as little as possible -- decode, buffer, acknowledge --
because OTLP export is a synchronous call on the collector's pipeline. Judging
happens on a background loop, so a slow model call cannot apply backpressure to
the collector and, through it, to the agents.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, Response

from .buffer import TraceBuffer
from .emit import emit_evaluation
from .judges import build_judge
from .observability import flush_telemetry, instrument_app, setup_observability
from .otlp import Span, decode_traces, decompress, empty_response
from .rubric import RUBRIC_VERSION, SAMPLE_SUMMARY, build_prompt
from .settings import JudgeSettings
from .summary import summarize

logger = logging.getLogger(__name__)

OTLP_PROTOBUF = "application/x-protobuf"


def create_app(settings: JudgeSettings) -> FastAPI:
    """Build the judge's HTTP app: an OTLP receiver plus a little introspection."""
    judge = build_judge(settings.use_llm)
    buffer = TraceBuffer(
        idle_seconds=settings.idle_seconds,
        max_age_seconds=settings.max_age_seconds,
        max_traces=settings.max_buffered_traces,
    )
    # Purely a debug affordance: lets `curl /evaluations` prove the pipeline
    # end to end without opening Grafana. Bounded, in-memory, never the record.
    recent: deque[dict] = deque(maxlen=settings.recent_size)

    async def evaluate(spans: list[Span]) -> None:
        """Score one settled trace and publish the verdict as its own trace."""
        summary = summarize(spans)

        if not summary.judgeable:
            # Two causes, and the log has to distinguish them: either
            # ENABLE_SENSITIVE_DATA is off, so every trace arrives stripped of
            # message content and *nothing* will ever be judged -- or the
            # collector forwarded a trace that was never a conversation, which
            # its sampling policy is supposed to filter out. Both look like
            # silence from the judge, so name the services and spans involved.
            logger.warning(
                "skipping trace with no scoreable content",
                extra={
                    "trace_id": summary.trace_id,
                    "span_count": summary.span_count,
                    "services": list(summary.services),
                    "agents": list(summary.agents),
                    "has_input": bool(summary.user_input),
                    "has_output": bool(summary.final_output),
                },
            )
            return

        verdicts = await judge.evaluate(summary)
        eval_trace_id = emit_evaluation(
            summary,
            verdicts,
            judge_mode=judge.name,
            threshold=settings.pass_threshold,
        )
        recent.appendleft(
            {
                "source_trace_id": summary.trace_id,
                "eval_trace_id": eval_trace_id,
                "root_service": summary.root_service,
                "agents": list(summary.agents),
                "models": list(summary.models),
                "tools": [call.name for call in summary.tool_calls],
                "user_input": summary.user_input,
                "final_output": summary.final_output,
                "scores": [
                    {
                        "name": verdict.name,
                        "score": verdict.score,
                        "label": verdict.label(settings.pass_threshold),
                        "explanation": verdict.explanation,
                    }
                    for verdict in verdicts
                ],
            }
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        # One long-lived task, started outside any request, so the eval spans
        # it creates begin with an empty OTel context -- see emit.py.
        worker = asyncio.create_task(buffer.run(evaluate), name="judge-buffer")
        logger.info(
            "judge listening for otlp traces",
            extra={"mode": judge.name, "rubric": RUBRIC_VERSION, "port": settings.port},
        )
        try:
            yield
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            flush_telemetry()

    app = FastAPI(title="judge-service", version=RUBRIC_VERSION, lifespan=lifespan)

    @app.post("/v1/traces")
    async def receive_traces(request: Request) -> Response:
        """The OTLP/HTTP trace endpoint the collector exports to.

        Accepts and acknowledges; it never scores inline. A malformed body is a
        400 so the collector surfaces it instead of retrying forever.
        """
        try:
            body = decompress(await request.body(), request.headers.get("content-encoding"))
            spans = decode_traces(body)
        except Exception as error:
            logger.warning("rejected otlp export", extra={"error": str(error)})
            return Response(content=str(error), status_code=400, media_type="text/plain")

        await buffer.add(spans)
        logger.debug("accepted otlp export", extra={"span_count": len(spans)})
        return Response(content=empty_response(), media_type=OTLP_PROTOBUF)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "mode": judge.name, "rubric": RUBRIC_VERSION}

    @app.get("/evaluations")
    async def evaluations(limit: int = 10) -> dict:
        """Recent verdicts, newest first -- the demo's proof of life."""
        return {"count": len(recent), "evaluations": list(recent)[: max(1, limit)]}

    @app.get("/rubric")
    async def rubric() -> dict:
        """The exact prompt this judge would send, rendered against a sample trace.

        Owning the rubric is the point of Architecture B; being able to read it
        without grepping the image is what makes that ownership real.
        """
        return {"version": RUBRIC_VERSION, "mode": judge.name, "prompt_template": build_prompt(SAMPLE_SUMMARY)}

    instrument_app(app)
    return app


def main() -> None:
    settings = JudgeSettings()
    setup_observability(settings.service_name)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
