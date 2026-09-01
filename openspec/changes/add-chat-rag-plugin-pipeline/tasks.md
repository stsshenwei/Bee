## 1. Pipeline Foundation

- [x] 1.1 Create `backend/app/services/chat_pipeline/` with stage id constants, context dataclasses, result types, and public exports.
- [x] 1.2 Define the plugin protocol, typed plugin errors, registry, and executor contract.
- [x] 1.3 Implement the executor loop with deterministic stage ordering, stop checks before each stage, typed error wrapping, and cleanup hooks.
- [x] 1.4 Add unit tests for context initialization, plugin registration, missing stage errors, and ordered execution.

## 2. Quick Chat Stages

- [x] 2.1 Implement a bootstrap stage that emits the compatible `conversation_id` event and initializes answer/source state.
- [x] 2.2 Implement history and memory retrieval stages using existing conversation and memory services.
- [x] 2.3 Implement query understanding using the existing service and current debug metadata shape.
- [x] 2.4 Implement retrieval using `RAGService.hybrid_retrieve_hits` with existing scope and top-k behavior preserved.
- [x] 2.5 Implement parent/context recall using `RAGService.recall_parent_hits`.
- [x] 2.6 Implement source, reasoning, and agent trace emission stages while preserving current SSE payload keys and ordering.
- [x] 2.7 Implement prompt assembly and completion streaming using `RAGService.stream_answer` and token accumulation.
- [x] 2.8 Implement assistant persistence and memory storage stages matching current quick-chat side effects.

## 3. Events, Fallback, And Cancellation

- [x] 3.1 Route public stage outputs through `ChatEventBus` so `StreamManager` stores monotonic offsets.
- [x] 3.2 Add stage progress metadata for query understanding, retrieval, context recall, and completion without renaming existing SSE payloads.
- [x] 3.3 Implement empty-retrieval fallback with compatible sources/debug metadata and a terminal answer path.
- [x] 3.4 Preserve rerank degradation and fallback debug decisions when delegating to existing retrieval logic.
- [x] 3.5 Stop execution cleanly when the stop signal is set before a stage or during token streaming.
- [x] 3.6 Add tests for source-before-token ordering, fallback, stage failure to `error` plus `done`, and stop during streaming.

## 4. API Wiring And Migration Flag

- [x] 4.1 Add `CHAT_RAG_PIPELINE_ENABLED` config/env wiring with conservative fallback to the current raw quick-chat path.
- [x] 4.2 Use the pipeline path for quick-mode chat streaming when the flag is enabled.
- [x] 4.3 Keep the existing raw quick-chat path callable while the flag is disabled.
- [x] 4.4 Ensure temporary attachment sources/context flow through the pipeline and are marked consumed exactly once.
- [x] 4.5 Add API route tests for old-client SSE compatibility with the feature flag enabled and disabled.
- [x] 4.6 Add replay/continue tests proving stored pipeline events remain compatible with `stream_offset`.

## 5. Retrieval Subset Reuse

- [x] 5.1 Add a retrieval-only stage list/helper for query understanding, retrieval, parent/context recall, and filtering without chat completion.
- [x] 5.2 Add tests comparing retrieval-only output shape to current search/evaluation expectations.
- [x] 5.3 Migrate one low-risk internal retrieval caller to the retrieval-only helper behind the same feature flag or focused compatibility tests.
- [x] 5.4 Document which callers still use direct `RAGService` retrieval and why.

## 6. Documentation And Validation

- [x] 6.1 Update `docs/ARCHITECTURE.md` with the online Chat/RAG plugin pipeline and its relationship to EventBus/StreamManager.
- [x] 6.2 Update `docs/design-docs/backend-rag-pipeline.md` with stage order, fallback, cancellation, and retrieval subset behavior.
- [x] 6.3 Document how to enable/disable the pipeline flag and read stage debug output.
- [ ] 6.4 Run focused backend tests for chat streaming, retrieval, memory, and pipeline modules.
- [ ] 6.5 Run a manual smoke test covering quick chat with sources, no-result fallback, stop during streaming, reconnect replay, and feature-flag rollback.
