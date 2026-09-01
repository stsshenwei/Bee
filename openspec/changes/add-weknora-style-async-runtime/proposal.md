## Why

Bee already has durable PostgreSQL task records, Wiki tasks, processing traces, and SSE chat streaming, but execution is still centered on process-local workers and request-owned streams. To reach the Weknora-style reliability model, offline document/Wiki work needs Redis-backed queue isolation and online chat needs an event stream layer that can replay, continue, and survive reconnects.

## What Changes

- Add a Redis-backed asynchronous runtime for offline document, Wiki, enrichment, and maintenance jobs, using Python-native worker infrastructure instead of the current in-process-only execution loop.
- Introduce Weknora-style worker pools with explicit queue routing: Core, PostProcess, Enrichment, Maintenance, Shared, and Wiki.
- Keep PostgreSQL as the source of truth for upload batches, documents, processing tasks, spans, retries, dead letters, Wiki generations, and idempotency.
- Split document processing into durable stage tasks where appropriate: `document.process`, `chunk.extract`, `embedding.index`, `knowledge.post_process`, `summary.generation`, `image.multimodal`, `graph.extraction`, `wiki.ingest`, `wiki.finalize`, and maintenance tasks.
- Add queue observability, retry/backoff, timeout, cancellation, dead-letter, and reconciliation behavior that maps cleanly to the existing processing trace drawer and document status UI.
- Add an EventBus + StreamManager architecture for `/chat/stream` as a later phase of the same runtime direction, preserving the current SSE payload contract while enabling offset-based replay and Redis-backed stream storage.
- Preserve existing upload, Wiki, chat, and retrieval APIs unless an endpoint receives additive fields for queue or stream continuation metadata.

## Capabilities

### New Capabilities

- `async-processing-runtime`: Redis-backed durable worker pools and staged offline processing for document upload, Wiki generation, enrichment, maintenance, retries, cancellation, and dead-letter handling.
- `chat-event-stream-runtime`: EventBus + StreamManager + SSE replay runtime for online chat events, including memory and Redis stream stores while preserving current `/chat/stream` compatibility.

### Modified Capabilities

None. The repo has no current mainline OpenSpec capabilities under `openspec/specs/`; this change introduces new capability specs.

## Impact

- Backend dependencies: Redis queue/client and a Python worker runtime, preferably Celery for queue routing, retry, delayed execution, inspection, and operational familiarity.
- Backend services: app startup configuration, processing worker, task repository integration, upload confirmation, Wiki ingest/finalize, enrichment, maintenance jobs, chat stream adapter, and SSE continuation handling.
- Frontend: processing status polling, trace drawer labels, queue/dead-letter visibility, and optional reconnect/continue metadata for chat streams.
- Operations: new worker commands, queue names, concurrency settings, Redis configuration, local Windows worker guidance, reconciliation jobs, and deployment docs.
- Tests/docs: unit and integration coverage for queue enqueue/claim/retry/cancel/dead-letter, Wiki task routing, SSE replay, stream cancellation, and updated architecture/development documentation.
