from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable
from uuid import uuid4


DEFAULT_AGENT_RUNTIME_TOOLS = (
    "thinking",
    "todo_write",
    "knowledge_search",
    "grep_chunks",
    "list_knowledge_chunks",
    "get_document_info",
    "query_knowledge_graph",
    "wiki_search",
    "wiki_read_page",
    "wiki_read_source_doc",
    "wiki_flag_issue",
    "wiki_read_issue",
    "wiki_update_issue",
    "wiki_write_page",
    "wiki_replace_text",
    "wiki_rename_page",
    "wiki_delete_page",
    "read_skill",
)

PRIVATE_TRACE_KEYS = {
    "chain_of_thought",
    "scratchpad",
    "private_reasoning",
    "raw_prompt",
    "memory_context",
    "api_key",
    "apikey",
    "authorization",
    "secret",
    "token",
    "raw_tool_payload",
}

AGENT_DOMAIN_EVENT_TYPES = {
    "agent_query",
    "agent_thought",
    "agent_tool_call",
    "agent_tool_result",
    "agent_reflection",
    "agent_remedial_search",
    "agent_references",
    "agent_final_answer",
    "agent_complete",
    "agent_error",
}

AGENT_EVENT_STATUSES = {"running", "completed", "partial", "failed", "skipped"}


@dataclass
class AgentRuntimeConfig:
    enabled: bool = False
    prompt_template_path: str = "config/prompt_templates/agent_system_prompt.yaml"
    prompt_template_id: str = "progressive_rag_agent"
    context_template_path: str = "config/prompt_templates/context_template.yaml"
    context_template_id: str = "default_context"
    skills_enabled: bool = False
    skills_path: str = "runtime_skills/preloaded"
    enabled_tools: tuple[str, ...] = DEFAULT_AGENT_RUNTIME_TOOLS
    max_iterations: int = 6
    max_empty_retries: int = 2
    max_repeated_responses: int = 2
    max_repeated_tool_batches: int = 2
    max_llm_calls: int = 8
    max_tool_calls: int = 24
    max_wall_clock_seconds: float = 120.0
    max_parallel_workers: int = 4
    local_concurrency_enabled: bool = True
    parallel_tool_calls_mode: str = "auto"
    terminal_streaming_mode: str = "auto"
    legacy_remedial_retrieval_enabled: bool = False
    max_tool_output_chars: int = 6000
    max_remedial_retrieval_attempts: int = 1
    reasoning_grep_first_enabled: bool = True
    quick_grep_first_enabled: bool = False
    unified_chat_runtime_enabled: bool = False
    quick_runtime_enabled: bool = False
    quick_prompt_template_id: str = "quick_rag_agent"
    quick_context_template_id: str = "qa_context"
    quick_enabled_tools: tuple[str, ...] = ()
    quick_max_iterations: int = 1
    quick_max_empty_retries: int = 0
    quick_max_repeated_responses: int = 0
    quick_preload_retrieval: bool = True
    quick_remedial_retrieval_enabled: bool = False
    wiki_runtime_enabled: bool = True
    wiki_prompt_template_id: str = "wiki_rag_agent"
    wiki_context_template_id: str = "default_context"
    wiki_enabled_tools: tuple[str, ...] = (
        "thinking",
        "todo_write",
        "wiki_search",
        "wiki_read_page",
        "wiki_read_source_doc",
        "wiki_flag_issue",
        "grep_chunks",
        "list_knowledge_chunks",
        "get_document_info",
    )
    wiki_max_iterations: int = 5
    wiki_max_empty_retries: int = 1
    wiki_max_repeated_responses: int = 1
    wiki_preload_retrieval: bool = False
    tool_timeout_seconds: float = 20.0
    web_search_enabled: bool = False
    web_search_endpoint: str = ""
    web_fetch_enabled: bool = False
    web_fetch_allowed_domains: tuple[str, ...] = ()
    data_analysis_enabled: bool = False
    database_query_enabled: bool = False
    database_allowed_sources: dict[str, str] = field(default_factory=dict)
    wiki_tools_enabled: bool = False
    wiki_maintenance_tools_enabled: bool = False
    fallback_to_deterministic: bool = True


@dataclass(frozen=True)
class ChatRuntimePolicy:
    mode: str
    prompt_template_id: str
    context_template_id: str
    enabled_tools: tuple[str, ...] = ()
    max_iterations: int = 1
    max_empty_retries: int = 0
    max_repeated_responses: int = 0
    max_repeated_tool_batches: int = 0
    max_llm_calls: int = 1
    max_tool_calls: int = 0
    max_wall_clock_seconds: float = 120.0
    max_parallel_workers: int = 1
    local_concurrency_enabled: bool = False
    parallel_tool_calls_mode: str = "off"
    terminal_streaming_mode: str = "off"
    max_remedial_retrieval_attempts: int = 0
    tool_choice: str = "none"
    preload_retrieval: bool = False
    remedial_retrieval_enabled: bool = False
    require_deep_read: bool = True
    grep_first_enabled: bool = False
    emit_initial_thought: bool = True

    @property
    def quick(self) -> bool:
        return self.mode == "quick"


def resolve_chat_runtime_policy(mode: str, config: AgentRuntimeConfig) -> ChatRuntimePolicy:
    normalized = (mode or "reasoning").strip().lower()
    if normalized == "quick":
        return ChatRuntimePolicy(
            mode="quick",
            prompt_template_id=config.quick_prompt_template_id,
            context_template_id=config.quick_context_template_id,
            enabled_tools=tuple(config.quick_enabled_tools or ()),
            max_iterations=max(1, int(config.quick_max_iterations or 1)),
            max_empty_retries=max(0, int(config.quick_max_empty_retries or 0)),
            max_repeated_responses=max(0, int(config.quick_max_repeated_responses or 0)),
            max_repeated_tool_batches=0,
            max_llm_calls=max(1, int(config.max_llm_calls or 1)),
            max_tool_calls=max(0, int(config.max_tool_calls or 0)),
            max_wall_clock_seconds=max(1.0, float(config.max_wall_clock_seconds or 120.0)),
            max_parallel_workers=1,
            local_concurrency_enabled=False,
            parallel_tool_calls_mode="off",
            terminal_streaming_mode=_capability_mode(config.terminal_streaming_mode),
            max_remedial_retrieval_attempts=max(0, int(config.max_remedial_retrieval_attempts if config.quick_remedial_retrieval_enabled else 0)),
            tool_choice="auto" if config.quick_enabled_tools else "none",
            preload_retrieval=bool(config.quick_preload_retrieval),
            remedial_retrieval_enabled=bool(config.quick_remedial_retrieval_enabled),
            require_deep_read=False,
            grep_first_enabled=bool(config.quick_grep_first_enabled),
            emit_initial_thought=False,
        )
    if normalized == "wiki":
        return ChatRuntimePolicy(
            mode="wiki",
            prompt_template_id=config.wiki_prompt_template_id,
            context_template_id=config.wiki_context_template_id,
            enabled_tools=tuple(config.wiki_enabled_tools or ()),
            max_iterations=max(1, int(config.wiki_max_iterations or 5)),
            max_empty_retries=max(0, int(config.wiki_max_empty_retries or 1)),
            max_repeated_responses=max(0, int(config.wiki_max_repeated_responses or 1)),
            max_repeated_tool_batches=max(1, int(config.max_repeated_tool_batches or 1)),
            max_llm_calls=max(1, int(config.max_llm_calls or 6)),
            max_tool_calls=max(1, int(config.max_tool_calls or 16)),
            max_wall_clock_seconds=max(1.0, float(config.max_wall_clock_seconds or 120.0)),
            max_parallel_workers=max(1, int(config.max_parallel_workers or 1)),
            local_concurrency_enabled=bool(config.local_concurrency_enabled),
            parallel_tool_calls_mode=_capability_mode(config.parallel_tool_calls_mode),
            terminal_streaming_mode=_capability_mode(config.terminal_streaming_mode),
            max_remedial_retrieval_attempts=0,
            tool_choice="auto" if config.wiki_enabled_tools else "none",
            preload_retrieval=bool(config.wiki_preload_retrieval),
            remedial_retrieval_enabled=False,
            require_deep_read=True,
            grep_first_enabled=False,
            emit_initial_thought=True,
        )
    return ChatRuntimePolicy(
        mode="reasoning",
        prompt_template_id=config.prompt_template_id,
        context_template_id=config.context_template_id,
        enabled_tools=tuple(config.enabled_tools or ()),
        max_iterations=max(1, int(config.max_iterations or 1)),
        max_empty_retries=max(0, int(config.max_empty_retries or 0)),
        max_repeated_responses=max(0, int(config.max_repeated_responses or 0)),
        max_repeated_tool_batches=max(1, int(config.max_repeated_tool_batches or 1)),
        max_llm_calls=max(1, int(config.max_llm_calls or (config.max_iterations + 2))),
        max_tool_calls=max(1, int(config.max_tool_calls or 1)),
        max_wall_clock_seconds=max(1.0, float(config.max_wall_clock_seconds or 120.0)),
        max_parallel_workers=max(1, int(config.max_parallel_workers or 1)),
        local_concurrency_enabled=bool(config.local_concurrency_enabled),
        parallel_tool_calls_mode=_capability_mode(config.parallel_tool_calls_mode),
        terminal_streaming_mode=_capability_mode(config.terminal_streaming_mode),
        max_remedial_retrieval_attempts=max(
            0,
            int(config.max_remedial_retrieval_attempts or 0)
            if config.legacy_remedial_retrieval_enabled
            else 0,
        ),
        tool_choice="auto" if config.enabled_tools else "none",
        preload_retrieval=False,
        remedial_retrieval_enabled=bool(
            config.legacy_remedial_retrieval_enabled and config.max_remedial_retrieval_attempts
        ),
        require_deep_read=True,
        grep_first_enabled=bool(config.reasoning_grep_first_enabled),
        emit_initial_thought=True,
    )


class ToolExecutionClass(str, Enum):
    PARALLEL_SAFE = "parallel_safe"
    SERIAL = "serial"
    EXCLUSIVE = "exclusive"


@dataclass(frozen=True)
class RuntimeToolError:
    code: str
    message: str
    fatal: bool = False
    retryable: bool = False


@dataclass
class RuntimeStateDelta:
    candidate_ids: list[str] = field(default_factory=list)
    deep_read_ids: list[str] = field(default_factory=list)
    source_titles: list[str] = field(default_factory=list)
    flags: dict[str, bool] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    append_values: dict[str, list[Any]] = field(default_factory=dict)
    replace_values: dict[str, Any] = field(default_factory=dict)
    debug: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeToolResult:
    success: bool
    output: str = ""
    observation: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    source_chunk_ids: list[str] = field(default_factory=list)
    source_titles: list[str] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    deep_read: bool = False
    state_delta: RuntimeStateDelta = field(default_factory=RuntimeStateDelta)
    structured_error: RuntimeToolError | None = None
    debug: dict[str, Any] = field(default_factory=dict)

    def to_observation_text(self) -> str:
        if self.success:
            return self.output or self.observation or "Tool completed."
        return self.error or self.observation or "Tool failed."


@dataclass(frozen=True)
class ActionToolCall:
    index: int
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    execution_class: ToolExecutionClass
    validation_error: RuntimeToolError | None = None


@dataclass(frozen=True)
class ActionBatch:
    batch_id: str
    round_number: int
    calls: tuple[ActionToolCall, ...]


@dataclass
class ActionToolExecution:
    call: ActionToolCall
    result: RuntimeToolResult
    queued_at: float
    started_at: float
    finished_at: float

    @property
    def queue_ms(self) -> int:
        return max(0, int((self.started_at - self.queued_at) * 1000))

    @property
    def duration_ms(self) -> int:
        return max(0, int((self.finished_at - self.started_at) * 1000))


def apply_runtime_state_delta(state: dict[str, Any], delta: RuntimeStateDelta) -> None:
    if delta.candidate_ids:
        state["previous_candidate_ids"] = _ordered_union(
            state.get("previous_candidate_ids"),
            state.get("search_candidate_ids"),
        )
        state["search_candidate_ids"] = _ordered_union(
            state.get("search_candidate_ids"),
            delta.candidate_ids,
        )
    if delta.deep_read_ids:
        state["previous_deep_read_ids"] = _ordered_union(
            state.get("previous_deep_read_ids"),
            state.get("deep_read_ids"),
        )
        state["deep_read_ids"] = _ordered_union(
            state.get("deep_read_ids"),
            delta.deep_read_ids,
        )
    if delta.source_titles:
        sources = list(state.get("sources") or [])
        seen = {str(item.get("source") or "") for item in sources if isinstance(item, dict)}
        for title in delta.source_titles:
            if title and title not in seen:
                sources.append({"source": title, "score": 0.0})
                seen.add(title)
        state["sources"] = sources
    for key, value in delta.flags.items():
        state[key] = bool(state.get(key)) or bool(value)
    for key, value in delta.counters.items():
        state[key] = int(state.get(key) or 0) + int(value)
    for key, values in delta.append_values.items():
        current = list(state.get(key) or [])
        current.extend(values)
        state[key] = current
    for key, value in delta.replace_values.items():
        state[key] = value
    if delta.debug:
        state.setdefault("tool_debug", []).append(dict(delta.debug))


@dataclass
class AgentRuntimeEvent:
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"event_type": self.event_type, "payload": scrub_private_fields(self.payload)}

    def to_sse_payload(self) -> dict[str, Any]:
        return {self.event_type: scrub_private_fields(self.payload)}


def agent_event(
    event_type: str,
    *,
    run_id: str,
    sequence: int,
    status: str = "completed",
    payload: dict[str, Any] | None = None,
) -> AgentRuntimeEvent:
    if event_type not in AGENT_DOMAIN_EVENT_TYPES:
        raise ValueError(f"unsupported agent domain event: {event_type}")
    normalized_status = status if status in AGENT_EVENT_STATUSES else "completed"
    body = {
        "event_id": uuid4().hex,
        "event_type": event_type,
        "run_id": run_id,
        "sequence": max(1, int(sequence)),
        "status": normalized_status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **(payload or {}),
    }
    return AgentRuntimeEvent(event_type, scrub_private_fields(body))


class AgentEventSequencer:
    def __init__(self) -> None:
        self._value = 0

    def next(self) -> int:
        self._value += 1
        return self._value


class AgentEventBus:
    def __init__(self) -> None:
        self._subscribers: list[Callable[[AgentRuntimeEvent], None]] = []
        self._events: list[AgentRuntimeEvent] = []
        self.closed = False

    def on(self, handler: Callable[[AgentRuntimeEvent], None]) -> Callable[[AgentRuntimeEvent], None]:
        if self.closed:
            raise RuntimeError("agent event bus is closed")
        self._subscribers.append(handler)
        return handler

    def emit(self, event: AgentRuntimeEvent) -> AgentRuntimeEvent:
        if self.closed:
            return event
        clean_event = AgentRuntimeEvent(event.event_type, scrub_private_fields(event.payload))
        self._events.append(clean_event)
        for subscriber in list(self._subscribers):
            subscriber(clean_event)
        return clean_event

    def events(self) -> list[AgentRuntimeEvent]:
        return list(self._events)

    def close(self) -> None:
        self.closed = True
        self._subscribers.clear()


def trace_to_agent_thought(
    trace_payload: dict[str, Any],
    *,
    run_id: str,
    sequence: int,
    event_type: str = "agent_thought",
) -> AgentRuntimeEvent:
    metadata = scrub_private_fields(trace_payload.get("metadata") or {})
    return agent_event(
        event_type,
        run_id=run_id,
        sequence=sequence,
        status=str(trace_payload.get("status") or "completed"),
        payload={
            "phase": trace_payload.get("stage") or "",
            "summary": trace_payload.get("summary") or "",
            "source_chunk_ids": trace_payload.get("source_chunk_ids") or metadata.get("source_chunk_ids") or [],
            "metadata": metadata,
        },
    )


def tool_call_to_agent_event(payload: dict[str, Any], *, run_id: str, sequence: int) -> AgentRuntimeEvent:
    return agent_event(
        "agent_tool_call",
        run_id=run_id,
        sequence=sequence,
        status=str(payload.get("status") or "running"),
        payload={
            "call_id": payload.get("call_id") or "",
            "tool": payload.get("tool") or "",
            "action": payload.get("action") or "execute",
            "input_summary": payload.get("input_summary") or "",
            "metadata": payload.get("metadata") or {},
        },
    )


def tool_result_to_agent_event(payload: dict[str, Any], *, run_id: str, sequence: int) -> AgentRuntimeEvent:
    return agent_event(
        "agent_tool_result",
        run_id=run_id,
        sequence=sequence,
        status=str(payload.get("status") or "completed"),
        payload={
            "call_id": payload.get("call_id") or "",
            "tool": payload.get("tool") or "",
            "action": payload.get("action") or "execute",
            "output_summary": payload.get("output_summary") or payload.get("observation") or "",
            "source_chunk_ids": payload.get("source_chunk_ids") or [],
            "metadata": payload.get("metadata") or {},
        },
    )


def scrub_private_fields(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in PRIVATE_TRACE_KEYS or any(secret in normalized for secret in ("secret", "token", "api_key", "authorization")):
                continue
            clean[str(key)] = scrub_private_fields(item)
        return clean
    if isinstance(value, list):
        return [scrub_private_fields(item) for item in value]
    return value


def _ordered_union(*values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        items = value if isinstance(value, (list, tuple, set)) else [value]
        for item in items:
            text = str(item or "")
            if text and text not in seen:
                result.append(text)
                seen.add(text)
    return result


def _capability_mode(value: str) -> str:
    normalized = str(value or "auto").strip().lower()
    return normalized if normalized in {"auto", "on", "off"} else "auto"
