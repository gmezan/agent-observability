"""Hold spans by trace_id until the trace stops growing, then release it once.

The collector's ``tail_sampling`` processor decides per trace, but it still
*delivers* over OTLP, and a large trace can arrive as several batches -- so
"one export" and "one trace" are not the same thing. Judging on arrival would
score the same conversation two or three times, each time on a fragment.

So the judge debounces: a trace is released once it has been quiet for
``idle_seconds``, or when it hits ``max_age_seconds`` and has to be judged as
it stands. This is deliberately in-memory and single-process. A judge that
misses a verdict has lost an opinion, not a trace -- Grafana already holds the
trace itself. The moment losing verdicts matters, this is the seam where a real
queue belongs (see docs/scaling-high-volume.md).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .otlp import Span

logger = logging.getLogger(__name__)


@dataclass
class _Pending:
    spans: list[Span] = field(default_factory=list)
    first_seen: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)


class TraceBuffer:
    """Accumulate spans per trace and emit each trace exactly once."""

    def __init__(self, idle_seconds: float, max_age_seconds: float, max_traces: int) -> None:
        self._idle = idle_seconds
        self._max_age = max_age_seconds
        self._max_traces = max_traces
        self._pending: dict[str, _Pending] = {}
        self._lock = asyncio.Lock()

    async def add(self, spans: list[Span]) -> None:
        """Record spans against their traces. Cheap: called on the OTLP hot path."""
        if not spans:
            return
        now = time.monotonic()
        async with self._lock:
            for span in spans:
                entry = self._pending.get(span.trace_id)
                if entry is None:
                    entry = self._pending[span.trace_id] = _Pending()
                entry.spans.append(span)
                entry.last_seen = now

    async def take_due(self) -> list[list[Span]]:
        """Remove and return every trace that has settled or timed out."""
        now = time.monotonic()
        async with self._lock:
            due = [
                trace_id
                for trace_id, entry in self._pending.items()
                if now - entry.last_seen >= self._idle or now - entry.first_seen >= self._max_age
            ]

            # Backpressure: if traffic outruns the judge, shed the oldest
            # traces by judging them early rather than growing without bound.
            overflow = len(self._pending) - len(due) - self._max_traces
            if overflow > 0:
                remaining = sorted(
                    (t for t in self._pending if t not in set(due)),
                    key=lambda t: self._pending[t].first_seen,
                )
                logger.warning("trace buffer over capacity, releasing early", extra={"count": overflow})
                due.extend(remaining[:overflow])

            return [self._pending.pop(trace_id).spans for trace_id in due]

    async def run(self, handle, tick_seconds: float = 1.0) -> None:
        """Poll for settled traces forever, handing each to `handle`.

        Started as a single background task at app startup, not per request --
        which also means its OTel context is empty, so the eval spans it
        creates are not accidentally parented to an inbound OTLP POST.
        """
        while True:
            try:
                await asyncio.sleep(tick_seconds)
                for spans in await self.take_due():
                    try:
                        await handle(spans)
                    except Exception:
                        # One unscoreable trace must not kill the loop and
                        # stall every trace behind it.
                        logger.exception("failed to evaluate trace", extra={"trace_id": spans[0].trace_id})
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("trace buffer loop error")
