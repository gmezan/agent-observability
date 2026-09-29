"""A self-hosted LLM-as-a-judge service that ingests OTLP and emits eval traces."""

from .buffer import TraceBuffer
from .emit import emit_evaluation
from .judges import Judge, LLMJudge, MockJudge, Verdict, build_judge
from .otlp import Span, decode_traces
from .rubric import EVALUATORS, RUBRIC_VERSION, build_prompt
from .settings import JudgeSettings
from .summary import ToolCall, TraceSummary, summarize

__all__ = [
    "EVALUATORS",
    "RUBRIC_VERSION",
    "Judge",
    "JudgeSettings",
    "LLMJudge",
    "MockJudge",
    "Span",
    "ToolCall",
    "TraceBuffer",
    "TraceSummary",
    "Verdict",
    "build_judge",
    "build_prompt",
    "decode_traces",
    "emit_evaluation",
    "summarize",
]
