## 1. Runtime Setup

- [x] 1.1 Add Celery and Redis client dependencies to the backend dependency manifest.
- [x] 1.2 Add async runtime configuration for broker URL, result backend mode, runtime mode, pool queue names, concurrency, retry budgets, time limits, and local fallback behavior.
- [x] 1.3 Update `.env.example`, `backend/rag_config.example.yaml`, and development docs with Redis/Celery settings and local Windows worker commands.
- [x] 1.4 Create a small queue facade so application services enqueue Bee task envelopes without importing Celery directly.
- [x] 1.5 Add serialization tests for task envelopes, idempotency keys, scope fields, and stage payload schema versions.

## 2. Celery App And Worker Pools

- [x] 2.1 Create the backend Celery app module with Redis broker configuration and task autodiscovery.
- [x] 2.2 Define queue routing for Core, PostProcess, Enrichment, Maintenance, Shared, and Wiki pools.
- [x] 2.3 Add worker entrypoints or documented commands for each pool with configurable concurrency.
- [x] 2.4 Add a health or diagnostics helper that reports broker reachability and configured queue routes.
- [x] 2.5 Add tests that verify each known task type resolves to the expected queue.

## 3. PostgreSQL Task Authority

- [x] 3.1 Extend or adapt the existing processing task repository to store broker task ids, idempotency keys, stage names, retry metadata, cancellation state, and dead-letter details needed by the Redis-backed runtime.
- [x] 3.2 Ensure duplicate enqueue attempts reuse or supersede PostgreSQL task rows according to deterministic idempotency keys.
- [x] 3.3 Add repository tests for enqueue, duplicate enqueue, retry scheduling, cancellation, dead-letter marking, and stale running task detection.
- [x] 3.4 Add reconciliation logic that re-enqueues stale non-terminal PostgreSQL tasks or marks terminal failures according to policy.

## 4. Coarse Offline Processing Migration

- [x] 4.1 Wire upload confirmation to persist task rows and enqueue coarse `document.process` tasks when the Redis-backed runtime is enabled.
- [x] 4.2 Implement the `document.process` worker task by delegating to the current document processing path while preserving processing spans and document status.
- [x] 4.3 Keep the existing PostgreSQL/local worker mode as a configurable fallback during migration.
- [x] 4.4 Ensure upload status APIs and the trace drawer show queued, running, retrying, completed, failed, cancelled, and dead-letter states from PostgreSQL.
- [x] 4.5 Add integration tests for confirmed upload, API restart recovery simulation, worker success, retryable failure, cancellation, and dead-letter exhaustion.

## 5. Staged Offline Processing

- [x] 5.1 Split parse/chunk work into typed Core pool tasks with deterministic document revision and chunk identifiers.
- [x] 5.2 Split embedding/index writes into idempotent tasks that avoid duplicate vectors or chunks after retry.
- [x] 5.3 Route `knowledge.post_process` work through the PostProcess pool and preserve existing postprocess spans.
- [x] 5.4 Route summary, generated questions, image multimodal, and graph extraction work through the Enrichment pool with provider-aware retry budgets.
- [x] 5.5 Route `wiki.ingest` and `wiki.finalize` through the Wiki pool and preserve Wiki generation idempotency, page versioning, and task visibility.
- [x] 5.6 Add stage-level tests for retry, cancellation, stale revision detection, idempotent writes, and span ordering.

## 6. Queue Operations And UI

- [x] 6.1 Add backend APIs or additive response fields for queue state, dead-letter reason, retry availability, and reconciliation status where the existing UI cannot infer them.
- [x] 6.2 Update the frontend document cards and processing trace drawer to label Redis-backed queue states without breaking existing local-worker states.
- [x] 6.3 Add retry or retry-request plumbing for dead-letter tasks if an existing endpoint cannot safely trigger it.
- [x] 6.4 Add frontend tests or focused smoke checks for queued, retrying, dead-letter, and completed trace display.

## 7. Chat EventBus Foundation

- [x] 7.1 Define chat EventBus interfaces that publish typed public events without exposing hidden prompts, secrets, or private chain-of-thought.
- [x] 7.2 Adapt current chat runtime paths to publish events to the EventBus while preserving existing SSE adapter output.
- [x] 7.3 Add unit tests that verify event ordering for conversation metadata, references, tool events, answer tokens, errors, stop, and completion.

## 8. StreamManager And SSE Replay

- [x] 8.1 Implement the StreamManager interface with append, read-after-offset, terminal-state, retention, and cleanup operations.
- [x] 8.2 Implement the memory StreamManager for single-process local development.
- [x] 8.3 Implement the Redis StreamManager keyed by session and message identifiers with monotonic offsets.
- [x] 8.4 Adapt `/chat/stream` to stream through the StreamManager by offset while preserving `conversation_id`, `sources`, `reasoning`, `token`, `final`, `error`, and `[DONE]` compatibility.
- [x] 8.5 Add reconnect or continue-stream handling for clients that provide a valid stream identity and last seen offset.
- [x] 8.6 Add stop handling that appends a stop event, signals cancellation, and terminates the active stream cleanly.
- [x] 8.7 Add tests for reconnect before completion, reconnect after completion, fatal error replay, stop, retention cleanup, and old-client SSE compatibility.

## 9. Documentation And Validation

- [x] 9.1 Update `docs/ARCHITECTURE.md` with the Redis queue worker pools and EventBus + StreamManager chat runtime.
- [x] 9.2 Update `docs/design-docs/backend-rag-pipeline.md` with the offline task pipeline, queue routing, retry/dead-letter behavior, and SSE replay flow.
- [x] 9.3 Add a runbook for starting Redis, API, Core/PostProcess/Enrichment/Maintenance/Shared/Wiki workers, and local Windows worker modes.
- [x] 9.4 Run focused backend processing, Wiki, queue, and chat SSE tests.
- [ ] 9.5 Run a manual smoke test: upload a Wiki document, observe queued-to-completed processing, verify Wiki pages/graph appear, ask a streamed chat question, and verify reconnect-compatible stream behavior where implemented.
