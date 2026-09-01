## Context

Bee already has a Weknora-inspired processing surface: uploads are confirmed before processing, PostgreSQL stores document/task/span state, Wiki generation has `wiki.ingest` and `wiki.finalize` tasks, and `/chat/stream` exposes SSE events. The remaining runtime gap is execution topology. Offline work is still consumed by process-local worker loops, and online chat streams are still owned by the request path rather than by a replayable stream manager.

The target is Weknora's runtime discipline adapted to this Python/FastAPI/PostgreSQL/Redis project:

- Redis is available locally and should become the broker for worker scheduling.
- PostgreSQL remains the authoritative state store for documents, batches, spans, Wiki pages, task attempts, and dead letters.
- Existing frontend/API contracts should remain compatible while queue and stream internals change behind them.
- Existing Agent domain events and SSE compatibility work should be reused; this change adds the StreamManager layer that stores and replays events.

## Goals / Non-Goals

**Goals:**

- Replace in-process-only offline execution with Redis-backed worker pools that can run outside the API process.
- Model Weknora-style pool isolation for Core, PostProcess, Enrichment, Maintenance, Shared, and Wiki work.
- Preserve PostgreSQL task/span visibility so the current processing drawer and document cards keep showing useful status.
- Route document, Wiki, enrichment, graph, summary, and maintenance work through durable task types with retry, timeout, cancellation, and dead-letter behavior.
- Add a later-phase EventBus + StreamManager runtime for chat SSE replay, reconnect, stop, and distributed streaming.
- Keep current upload confirmation and `/chat/stream` response contracts backwards-compatible.

**Non-Goals:**

- Do not port Go Asynq directly into Python.
- Do not replace PostgreSQL metadata with Redis state.
- Do not introduce Kafka, RabbitMQ, or NATS for this change.
- Do not rewrite retrieval quality, prompt extraction, or Wiki page generation logic except where task routing requires it.
- Do not change public SSE payload names in the first StreamManager implementation; new behavior must be additive.
- Do not require the chat EventBus + StreamManager phase before the offline queue phase can ship.

## Decisions

### Decision: Use Celery with Redis as the Python equivalent of Weknora Asynq

Celery will provide Redis-backed queue routing, scheduled retries, worker processes, concurrency controls, task time limits, revoke support, and queue inspection. The project should introduce a small internal queue facade so application code enqueues typed Bee tasks rather than depending on Celery APIs everywhere.

Alternative considered: Dramatiq with Redis. Dramatiq is lighter and pleasant in Python, but Celery is closer to Asynq's operational model for named queues, retries, delayed work, worker inspection, and mixed deployment topologies.

Alternative considered: keep the PostgreSQL worker loop only. It already provides useful task state, but it does not provide hard pool isolation, multi-process scheduling, or familiar Redis-backed worker operations.

### Decision: Keep PostgreSQL as the source of truth

Redis/Celery owns scheduling and delivery. PostgreSQL owns business state and observability. Every queued task gets or updates a PostgreSQL task row containing task type, scope, payload, idempotency key, attempts, status, stage, Celery task id, timestamps, trace id, and last error. Workers must check PostgreSQL before doing side effects and must commit stage/span updates there.

This prevents Redis broker loss, duplicate delivery, or worker crashes from becoming business-state corruption. A reconciliation job can scan PostgreSQL for stale queued/running/retrying rows and re-enqueue work.

Alternative considered: store task truth only in Celery result backend. That would simplify the first code path but would fragment the existing document/Wiki trace model and make the frontend harder to keep accurate.

### Decision: Introduce six explicit worker pools

The runtime will route work by task type:

- Core: `document.process`, `chunk.extract`, parser/chunker-heavy work.
- PostProcess: `knowledge.post_process`, graph extraction orchestration, source cleanup.
- Enrichment: `summary.generation`, `generated_questions`, `image.multimodal`, expensive provider calls.
- Wiki: `wiki.ingest`, `wiki.finalize`, page reduce/finalization work.
- Maintenance: delete cleanup, datasource sync, stale-task reconciliation, index repair.
- Shared: optional overflow capacity that listens to selected queues without removing the dedicated pool minimums.

Default concurrency should be configurable and can mirror Weknora's starting point: Core 8, PostProcess 2, Enrichment 12, Maintenance 4, Shared 6, Wiki 8. Local Windows development should document Celery `solo` or `threads` pool usage because prefork is a Linux-oriented production default.

Alternative considered: one generic worker queue. This is simpler but lets long document parsing starve Wiki finalization, maintenance, or lightweight enrichment tasks.

### Decision: Migrate in phases from coarse tasks to staged tasks

Phase 1 should enqueue one durable `upload_file.process` or `document.process` Celery task that calls the current processing path and updates existing PostgreSQL rows. This proves the broker, worker commands, idempotency, and UI status without changing every stage at once.

Phase 2 should split the pipeline into stage tasks where it helps reliability and isolation: parse/chunk, embedding/index, postprocess, enrichment, Wiki ingest/finalize. Stage boundaries must be idempotent and use deterministic document/chunk/vector/page identifiers.

Alternative considered: split every stage before introducing Celery. That creates too many moving parts before the new runtime is proven.

### Decision: Add queue reconciliation and cancellation as first-class runtime behavior

Workers should handle retries through Celery retry scheduling, but final status and dead letters are written to PostgreSQL. Cancellation marks PostgreSQL rows and uses Celery revoke as a best effort. Workers must check cancellation before stage boundaries and before committing derived artifacts.

Reconciliation periodically detects stale rows, missing broker tasks, expired running tasks, and dead-letter retry requests, then re-enqueues or marks terminal states according to policy.

### Decision: Add EventBus + StreamManager after offline queue foundations

Chat streaming will keep the current `/chat/stream` contract while introducing two internal layers:

- EventBus: per chat run domain-event dispatcher for answer tokens, references, tool calls/results, public thoughts, errors, stop, and complete.
- StreamManager: append/read abstraction with memory and Redis implementations. Redis-backed streams store events by `session_id` and `message_id` with monotonic offsets for reconnect and replay.

The existing Agent event models and SSE compatibility adapter remain useful. The new work is persistence and replay, not renaming every event. The API can add optional continuation metadata while old clients keep reading `sources`, `reasoning`, `token`, `final`, `error`, and `[DONE]`.

Alternative considered: leave SSE request-owned. That works for single-process happy paths but cannot support robust reconnect, stream continuation, or distributed chat workers.

## Risks / Trade-offs

- [Risk] Celery introduces operational complexity and separate worker processes. -> Mitigation: add clear local commands, env examples, health checks, and a local fallback mode during migration.
- [Risk] Redis may lose broker state if persistence is not configured. -> Mitigation: make PostgreSQL authoritative and add reconciliation that can re-enqueue non-terminal task rows.
- [Risk] Duplicate delivery can duplicate chunks, vectors, Wiki pages, or summaries. -> Mitigation: enforce idempotency keys and deterministic write identifiers at every stage boundary.
- [Risk] Pool isolation can underutilize workers when one pool is idle. -> Mitigation: add a Shared worker pool that can listen to selected queues while preserving dedicated pool capacity.
- [Risk] Windows local Celery behavior differs from Linux production. -> Mitigation: document `--pool=solo` or `--pool=threads` for local development and test production-like commands separately.
- [Risk] SSE replay can leak stale or duplicate events to clients. -> Mitigation: store monotonic offsets, make events idempotent for frontend reducers, and keep `[DONE]` terminal semantics explicit.
- [Risk] Migrating all stages at once could break uploads/Wiki again. -> Mitigation: ship a coarse Celery-backed task first, then split stages behind feature flags and tests.

## Migration Plan

1. Add Redis/Celery configuration, queue facade, worker app, task envelope model, and worker command documentation.
2. Wire upload confirmation to create PostgreSQL task rows and enqueue coarse Celery processing tasks while keeping the current local worker fallback available.
3. Add worker pool commands and routing for Core, PostProcess, Enrichment, Maintenance, Shared, and Wiki.
4. Implement retry, timeout, cancellation, dead-letter, and stale-task reconciliation against PostgreSQL.
5. Split offline processing into durable stage tasks, starting with Wiki ingest/finalize and enrichment tasks where pool isolation matters most.
6. Update frontend status and trace displays only where new queue states need labels or retry actions.
7. Add EventBus and StreamManager interfaces, first with in-memory storage, then Redis storage.
8. Adapt `/chat/stream` to append domain events to the StreamManager and stream by offset while preserving existing SSE payloads.
9. Add reconnect/continue/stop tests and document the distributed chat streaming behavior.

Rollback is configuration-based: disable the Celery runtime and use the existing PostgreSQL/local worker path while retaining PostgreSQL task records. For chat, disable Redis StreamManager and keep request-owned SSE until replay is stable.

## Open Questions

- Should the first Python queue dependency be Celery immediately, or should the internal queue facade allow Dramatiq as a later adapter?
- Which queue inspection endpoints should be exposed in the admin UI versus kept as operator-only logs?
- Should Shared workers listen to all queues by default or only enrichment/wiki queues where starvation is most likely?
- Should chat stream continuation use an explicit `stream_id`, or reuse `session_id` plus `message_id` as Weknora does?
- What retry budgets should be configured per provider-heavy stage versus deterministic local stages?
