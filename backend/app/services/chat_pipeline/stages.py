from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Iterable

from app.services.chat_pipeline.types import ChatPipelineContext, ChatPipelineEvent, StageId


@dataclass(frozen=True)
class EmitConversationStage:
    stage_id: str = StageId.EMIT_CONVERSATION

    def run(self, context: ChatPipelineContext) -> ChatPipelineEvent:
        request = context.request
        return ChatPipelineEvent(
            "conversation_id",
            {"conversation_id": request.conversation_id, "stream_message_id": request.stream_message_id},
        )


@dataclass(frozen=True)
class LoadHistoryStage:
    stage_id: str = StageId.LOAD_HISTORY

    def run(self, context: ChatPipelineContext) -> None:
        service = context.runtime.conversation_service
        if service is not None:
            context.state.conversation_context = service.build_context(context.request.conversation_id)


@dataclass(frozen=True)
class MemoryRetrievalStage:
    stage_id: str = StageId.MEMORY_RETRIEVAL

    def run(self, context: ChatPipelineContext) -> None:
        service = context.runtime.memory_service
        request = context.request
        memory_block = ""
        if service is not None and request.memory_enabled:
            context.state.memories = service.recall_memories(request.question)
            memory_block = service.format_prompt_context(context.state.memories)
        context.state.memory_context = _join_request_context(memory_block, request.temporary_context)


@dataclass(frozen=True)
class QueryUnderstandStage:
    stage_id: str = StageId.QUERY_UNDERSTAND

    def run(self, context: ChatPipelineContext) -> None:
        context.state.query_understanding = {
            "original_query": context.request.question,
            "normalized_query": context.request.question,
            "retrieval_queries": [context.request.question],
            "source": "deferred_to_retrieval",
        }


@dataclass(frozen=True)
class RetrieveStage:
    stage_id: str = StageId.RETRIEVE

    def run(self, context: ChatPipelineContext) -> None:
        rag_service = context.runtime.rag_service
        context.state.child_hits = rag_service.hybrid_retrieve_hits(context.request.question, scope=context.request.scope)
        context.state.retrieval_debug = dict(getattr(rag_service, "_last_retrieval_debug", {}) or {})
        understanding = context.state.retrieval_debug.get("query_understanding")
        if isinstance(understanding, dict):
            context.state.query_understanding = dict(understanding)


@dataclass(frozen=True)
class RecallParentContextStage:
    stage_id: str = StageId.RECALL_PARENT_CONTEXT

    def run(self, context: ChatPipelineContext) -> None:
        context.state.hits = context.runtime.rag_service.recall_parent_hits(
            context.state.child_hits,
            scope=context.request.scope,
        )
        debug = getattr(context.runtime.rag_service, "_last_retrieval_debug", None)
        if isinstance(debug, dict):
            context.state.retrieval_debug = dict(debug)


@dataclass(frozen=True)
class FilterTopKStage:
    stage_id: str = StageId.FILTER_TOP_K

    def run(self, context: ChatPipelineContext) -> None:
        top_k = getattr(context.runtime.rag_service, "top_k", None)
        if top_k:
            context.state.hits = context.state.hits[: int(top_k)]


@dataclass(frozen=True)
class EmitSourcesStage:
    stage_id: str = StageId.EMIT_SOURCES

    def run(self, context: ChatPipelineContext) -> ChatPipelineEvent:
        sources = context.runtime.rag_service.extract_sources(context.state.hits)
        context.state.sources = _merge_sources(sources, context.request.temporary_sources)
        return ChatPipelineEvent("sources", {"sources": context.state.sources})


@dataclass(frozen=True)
class EmitReasoningStage:
    stage_id: str = StageId.EMIT_REASONING

    def run(self, context: ChatPipelineContext) -> ChatPipelineEvent:
        reasoning = context.runtime.rag_service.build_reasoning_summary(context.request.question, context.state.hits)
        return ChatPipelineEvent("reasoning", {"reasoning": reasoning})


@dataclass(frozen=True)
class EmitAgentTraceStage:
    stage_id: str = StageId.EMIT_AGENT_TRACE

    def run(self, context: ChatPipelineContext) -> Iterable[ChatPipelineEvent]:
        build_agent_trace = getattr(context.runtime.rag_service, "build_chat_agent_trace", None)
        if not callable(build_agent_trace):
            return []
        trace_kwargs: dict[str, Any] = {}
        trace_parameters = inspect.signature(build_agent_trace).parameters
        if "scope" in trace_parameters:
            trace_kwargs["scope"] = context.request.scope
        if "sources" in trace_parameters:
            trace_kwargs["sources"] = context.state.sources
        return (
            ChatPipelineEvent("agent_trace", {"agent_trace": trace_step})
            for trace_step in build_agent_trace(context.request.question, context.state.hits, **trace_kwargs)
        )


@dataclass(frozen=True)
class IntoPromptStage:
    stage_id: str = StageId.INTO_PROMPT

    def run(self, context: ChatPipelineContext) -> None:
        context.state.prompt_ready = True


@dataclass(frozen=True)
class ChatCompletionStreamStage:
    stage_id: str = StageId.CHAT_COMPLETION_STREAM

    def run(self, context: ChatPipelineContext) -> Iterable[ChatPipelineEvent]:
        def events() -> Iterable[ChatPipelineEvent]:
            for token in context.runtime.rag_service.stream_answer(
                context.request.question,
                hits=context.state.hits,
                conversation_context=context.state.conversation_context,
                memory_context=context.state.memory_context,
                scope=context.request.scope,
            ):
                if context.runtime.is_stopped():
                    context.state.stopped = True
                    return
                token = str(token)
                context.state.answer_parts.append(token)
                yield ChatPipelineEvent("token", {"token": token})

        return events()


@dataclass(frozen=True)
class PersistAssistantMessageStage:
    stage_id: str = StageId.PERSIST_ASSISTANT_MESSAGE

    def run(self, context: ChatPipelineContext) -> None:
        if context.state.stopped:
            return
        service = context.runtime.conversation_service
        if service is None:
            return
        service.repository.append_message(
            context.request.conversation_id,
            "assistant",
            context.state.answer,
            {
                "sources": context.state.sources,
                "knowledge_base_scope": context.request.scope.to_dict(),
                "chat_mode": context.request.chat_mode,
                "temporary_attachment_ids": context.request.temporary_attachment_ids,
            },
        )
        service.maybe_summarize(context.request.conversation_id)
        context.state.persisted_assistant_message = True


@dataclass(frozen=True)
class MemoryStorageStage:
    stage_id: str = StageId.MEMORY_STORAGE

    def run(self, context: ChatPipelineContext) -> ChatPipelineEvent | None:
        if context.state.stopped:
            return None
        service = context.runtime.memory_service
        if service is None:
            return None
        updates = service.process_exchange(
            user_message=context.request.question,
            assistant_message=context.state.answer,
            conversation_id=context.request.conversation_id,
            user_message_id=context.request.user_message_id,
            memory_enabled=context.request.memory_enabled,
        )
        context.state.memory_updates = list(updates or [])
        if context.state.memory_updates:
            return ChatPipelineEvent("memory_updated", {"memory_updated": context.state.memory_updates})
        return None


@dataclass(frozen=True)
class DoneStage:
    stage_id: str = StageId.DONE

    def run(self, context: ChatPipelineContext) -> ChatPipelineEvent:
        return ChatPipelineEvent("done", {}, terminal=True)


def _join_request_context(*blocks: str) -> str:
    return "\n\n".join(block.strip() for block in blocks if block and block.strip())


def _merge_sources(sources: list[dict[str, Any]], temporary_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not temporary_sources:
        return list(sources)
    seen = {str(item.get("temporary_attachment_id") or item.get("source") or "") for item in sources}
    merged = list(sources)
    for item in temporary_sources:
        key = str(item.get("temporary_attachment_id") or item.get("source") or "")
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged
