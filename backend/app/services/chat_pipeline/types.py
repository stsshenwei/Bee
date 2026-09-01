from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from app.services.chat_streaming.event_bus import ChatEventBus
from app.services.chat_streaming.stream_manager import StreamIdentity


class StageId:
    EMIT_CONVERSATION = "emit_conversation"
    LOAD_HISTORY = "load_history"
    MEMORY_RETRIEVAL = "memory_retrieval"
    QUERY_UNDERSTAND = "query_understand"
    RETRIEVE = "retrieve"
    RECALL_PARENT_CONTEXT = "recall_parent_context"
    FILTER_TOP_K = "filter_top_k"
    EMIT_SOURCES = "emit_sources"
    EMIT_REASONING = "emit_reasoning"
    EMIT_AGENT_TRACE = "emit_agent_trace"
    INTO_PROMPT = "into_prompt"
    CHAT_COMPLETION_STREAM = "chat_completion_stream"
    PERSIST_ASSISTANT_MESSAGE = "persist_assistant_message"
    MEMORY_STORAGE = "memory_storage"
    DONE = "done"


QUICK_RAG_STAGE_IDS: tuple[str, ...] = (
    StageId.EMIT_CONVERSATION,
    StageId.LOAD_HISTORY,
    StageId.MEMORY_RETRIEVAL,
    StageId.QUERY_UNDERSTAND,
    StageId.RETRIEVE,
    StageId.RECALL_PARENT_CONTEXT,
    StageId.EMIT_SOURCES,
    StageId.EMIT_REASONING,
    StageId.EMIT_AGENT_TRACE,
    StageId.INTO_PROMPT,
    StageId.CHAT_COMPLETION_STREAM,
    StageId.PERSIST_ASSISTANT_MESSAGE,
    StageId.MEMORY_STORAGE,
    StageId.DONE,
)


RETRIEVAL_ONLY_STAGE_IDS: tuple[str, ...] = (
    StageId.QUERY_UNDERSTAND,
    StageId.RETRIEVE,
    StageId.RECALL_PARENT_CONTEXT,
    StageId.FILTER_TOP_K,
)


@dataclass(frozen=True)
class ChatPipelineRequest:
    question: str
    conversation_id: str
    stream_message_id: str
    scope: Any
    chat_mode: str = "quick"
    memory_enabled: bool = True
    user_message_id: str = ""
    temporary_attachment_ids: list[str] = field(default_factory=list)
    temporary_context: str = ""
    temporary_sources: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ChatPipelineState:
    conversation_context: dict[str, Any] = field(default_factory=dict)
    memories: list[dict[str, Any]] = field(default_factory=list)
    memory_context: str = ""
    query_understanding: dict[str, Any] = field(default_factory=dict)
    child_hits: list[dict[str, Any]] = field(default_factory=list)
    hits: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    prompt_ready: bool = False
    answer_parts: list[str] = field(default_factory=list)
    memory_updates: list[dict[str, Any]] = field(default_factory=list)
    retrieval_debug: dict[str, Any] = field(default_factory=dict)
    stage_progress: list[dict[str, Any]] = field(default_factory=list)
    failed_stage_id: str | None = None
    error: str | None = None
    stopped: bool = False
    persisted_assistant_message: bool = False

    @property
    def answer(self) -> str:
        return "".join(self.answer_parts)


@dataclass
class ChatPipelineRuntime:
    rag_service: Any
    conversation_service: Any | None = None
    memory_service: Any | None = None
    event_bus: ChatEventBus | None = None
    stream_identity: StreamIdentity | None = None
    stop_signal: Any | None = None
    trace_id: str = ""
    emit: Callable[["ChatPipelineEvent"], None] | None = None

    def is_stopped(self) -> bool:
        return bool(self.stop_signal is not None and self.stop_signal.is_set())


@dataclass
class ChatPipelineContext:
    request: ChatPipelineRequest
    runtime: ChatPipelineRuntime
    state: ChatPipelineState = field(default_factory=ChatPipelineState)


@dataclass(frozen=True)
class ChatPipelineEvent:
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    terminal: bool = False


StageOutput = Iterable[ChatPipelineEvent] | ChatPipelineEvent | None


@dataclass
class ChatPipelineResult:
    context: ChatPipelineContext
    events: list[ChatPipelineEvent] = field(default_factory=list)
