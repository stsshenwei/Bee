## Why

The current quick chat/RAG path still coordinates retrieval, source emission, reasoning trace, prompt assembly, and streaming generation through request-level functions and a large `RAGService`. Bee already has Weknora-style offline task routing and chat stream storage foundations; the next step is to make online Chat/RAG execution equally composable, observable, and reusable.

## What Changes

- Introduce a Weknora-inspired Chat/RAG plugin pipeline for online quick-answer execution.
- Define typed pipeline stages for history, memory, query understanding, hybrid retrieval, rerank/fusion/merge, prompt assembly, streamed completion, and memory persistence.
- Add a pipeline context object that carries immutable request configuration, mutable stage state, and runtime handles such as EventBus, StreamManager identity, stop signal, trace metadata, and scope.
- Move the raw quick-chat path from ad hoc `main.py` orchestration into a pipeline executor while preserving existing `/chat/stream` SSE payloads.
- Reuse the same retrieval stage subset for pure search, evaluation, and future agent/tool integrations where practical.
- Emit stage-level progress, traces, debug metadata, fallback decisions, and errors through the existing EventBus/StreamManager layer.
- Preserve existing offline document/Wiki Celery pipeline behavior; this change does not replace the completed async-processing runtime.

## Capabilities

### New Capabilities
- `chat-rag-plugin-pipeline`: Online Chat/RAG requests are executed through a typed, dynamically assembled plugin pipeline with reusable stages, progress events, fallback handling, and SSE compatibility.

### Modified Capabilities
None.

## Impact

- Backend services: `backend/app/main.py`, `backend/app/services/retrieval/rag_service.py`, new chat/RAG pipeline modules, retrieval debug metadata, and chat stream event emission.
- Existing runtime foundations: integrates with `ChatEventBus`, `StreamManager`, `KnowledgeBaseScope`, query understanding, hybrid retrieval, reranking, memory, and conversation services.
- Frontend/API: public `/chat/stream` payloads remain backwards-compatible; stage/progress metadata may be additive.
- Tests/docs: add pipeline stage, executor, SSE compatibility, stop/fallback, retrieval subset reuse, and design documentation coverage.
