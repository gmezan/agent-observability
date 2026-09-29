"""The judge's own OTLP receiver: protobuf on the wire, dataclasses in the code.

The collector speaks OTLP and nothing else, so anything that wants sampled
traces has to terminate OTLP itself. That is one POST handler and this module:
decode ``ExportTraceServiceRequest`` into plain ``Span`` objects and hand them
to the rest of the service, which then never touches protobuf again.

Only traces are decoded. The collector's judge pipeline exports traces alone --
metrics and logs go to Grafana and are of no use to a judge.
"""

from __future__ import annotations

import gzip
import zlib
from dataclasses import dataclass
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

# opentelemetry.proto.trace.v1.Status.StatusCode
STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2


@dataclass(frozen=True)
class SpanEvent:
    """A timestamped event on a span (the GenAI conventions use these for messages)."""

    name: str
    attributes: dict[str, Any]


@dataclass(frozen=True)
class Span:
    """One decoded span, with ids as hex strings the way Tempo displays them."""

    trace_id: str
    span_id: str
    parent_span_id: str
    name: str
    service_name: str
    start_ns: int
    end_ns: int
    status_code: int
    attributes: dict[str, Any]
    events: tuple[SpanEvent, ...]

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000_000

    @property
    def is_root(self) -> bool:
        """True when nothing in this trace is this span's parent.

        The collector only forwards complete traces, so an empty parent id here
        really is the root, not just the first span that happened to arrive.
        """
        return not self.parent_span_id

    @property
    def failed(self) -> bool:
        return self.status_code == STATUS_ERROR


def _any_value(value: AnyValue) -> Any:
    """Unwrap an OTLP AnyValue into the nearest Python equivalent."""
    kind = value.WhichOneof("value")
    if kind is None:
        return None
    if kind == "array_value":
        return [_any_value(item) for item in value.array_value.values]
    if kind == "kvlist_value":
        return {kv.key: _any_value(kv.value) for kv in value.kvlist_value.values}
    if kind == "bytes_value":
        return value.bytes_value.hex()
    return getattr(value, kind)


def _attributes(pairs: list[KeyValue]) -> dict[str, Any]:
    return {kv.key: _any_value(kv.value) for kv in pairs}


def decompress(body: bytes, content_encoding: str | None) -> bytes:
    """Undo the exporter's compression.

    The collector's otlphttp exporter gzips by default, so a receiver that
    ignores Content-Encoding sees garbage and fails to parse -- silently, as an
    export error on the *collector* side, which is a confusing place to find it.
    """
    encoding = (content_encoding or "").strip().lower()
    if encoding in ("", "identity"):
        return body
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        return zlib.decompress(body)
    raise ValueError(f"unsupported Content-Encoding: {encoding!r}")


def decode_traces(body: bytes) -> list[Span]:
    """Parse an OTLP/HTTP protobuf trace export into flat spans.

    Resource attributes are not kept wholesale; ``service.name`` is lifted onto
    each span because that is the only one a verdict needs -- which host
    produced this part of the conversation.
    """
    request = ExportTraceServiceRequest()
    request.ParseFromString(body)

    spans: list[Span] = []
    for resource_spans in request.resource_spans:
        resource = _attributes(resource_spans.resource.attributes)
        service_name = str(resource.get("service.name", "unknown"))
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                spans.append(
                    Span(
                        trace_id=span.trace_id.hex(),
                        span_id=span.span_id.hex(),
                        parent_span_id=span.parent_span_id.hex(),
                        name=span.name,
                        service_name=service_name,
                        start_ns=span.start_time_unix_nano,
                        end_ns=span.end_time_unix_nano,
                        status_code=span.status.code,
                        attributes=_attributes(span.attributes),
                        events=tuple(
                            SpanEvent(name=event.name, attributes=_attributes(event.attributes))
                            for event in span.events
                        ),
                    )
                )
    return spans


def empty_response() -> bytes:
    """A successful, fully-accepted OTLP export response.

    OTLP requires a protobuf body even on success; an empty
    ``ExportTraceServiceResponse`` means "all spans accepted, none rejected".
    Returning 200 with no body makes the collector log a parse failure.
    """
    return ExportTraceServiceResponse().SerializeToString()
