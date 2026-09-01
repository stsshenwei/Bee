from __future__ import annotations

from collections.abc import Iterable, Iterator

from app.services.chat_pipeline.executor import ChatPipelineExecutor
from app.services.chat_pipeline.plugin import ChatPipelineRegistry
from app.services.chat_pipeline.stages import (
    ChatCompletionStreamStage,
    DoneStage,
    EmitAgentTraceStage,
    EmitConversationStage,
    EmitReasoningStage,
    EmitSourcesStage,
    FilterTopKStage,
    IntoPromptStage,
    LoadHistoryStage,
    MemoryRetrievalStage,
    MemoryStorageStage,
    PersistAssistantMessageStage,
    QueryUnderstandStage,
    RecallParentContextStage,
    RetrieveStage,
)
from app.services.chat_pipeline.types import (
    QUICK_RAG_STAGE_IDS,
    RETRIEVAL_ONLY_STAGE_IDS,
    ChatPipelineContext,
    ChatPipelineEvent,
)


def default_chat_pipeline_registry() -> ChatPipelineRegistry:
    registry = ChatPipelineRegistry()
    for plugin in (
        EmitConversationStage(),
        LoadHistoryStage(),
        MemoryRetrievalStage(),
        QueryUnderstandStage(),
        RetrieveStage(),
        RecallParentContextStage(),
        FilterTopKStage(),
        EmitSourcesStage(),
        EmitReasoningStage(),
        EmitAgentTraceStage(),
        IntoPromptStage(),
        ChatCompletionStreamStage(),
        PersistAssistantMessageStage(),
        MemoryStorageStage(),
        DoneStage(),
    ):
        registry.register(plugin)
    return registry


def run_quick_rag_pipeline(
    context: ChatPipelineContext,
    *,
    stage_ids: Iterable[str] = QUICK_RAG_STAGE_IDS,
    registry: ChatPipelineRegistry | None = None,
) -> Iterator[ChatPipelineEvent]:
    executor = ChatPipelineExecutor(registry or default_chat_pipeline_registry())
    yield from executor.run(context, stage_ids)


def run_retrieval_only_pipeline(
    context: ChatPipelineContext,
    *,
    stage_ids: Iterable[str] = RETRIEVAL_ONLY_STAGE_IDS,
    registry: ChatPipelineRegistry | None = None,
) -> ChatPipelineContext:
    executor = ChatPipelineExecutor(registry or default_chat_pipeline_registry())
    for _event in executor.run(context, stage_ids):
        pass
    return context
