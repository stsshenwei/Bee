## ADDED Requirements

### Requirement: Redis-Backed Offline Queue Runtime
The system SHALL support a Redis-backed offline processing runtime that enqueues durable document, Wiki, enrichment, and maintenance work outside the FastAPI request process.

#### Scenario: Upload confirmation enqueues offline work
- **WHEN** a user confirms an upload batch while the Redis-backed runtime is enabled
- **THEN** the API SHALL persist processing task state in PostgreSQL, enqueue the corresponding broker task, and return without processing the document inline

#### Scenario: API restart does not lose pending work
- **WHEN** the API process restarts after upload tasks have been enqueued
- **THEN** pending or retryable PostgreSQL task records SHALL remain recoverable and workers SHALL continue processing after restart

### Requirement: PostgreSQL Remains Authoritative
The system SHALL treat PostgreSQL as the source of truth for upload batches, documents, processing tasks, attempts, spans, idempotency keys, cancellations, retries, and dead-letter state.

#### Scenario: Broker task is delivered twice
- **WHEN** Redis or the worker runtime delivers the same logical task more than once
- **THEN** the worker SHALL consult PostgreSQL idempotency and avoid duplicate document, chunk, vector, Wiki page, or status side effects

#### Scenario: Broker state is missing
- **WHEN** PostgreSQL contains a non-terminal task whose broker task is missing or stale
- **THEN** reconciliation SHALL re-enqueue or terminally mark the task according to retry and cancellation policy

### Requirement: Worker Pool Isolation
The system SHALL route offline task types to explicit worker pools with independent queue names and configurable concurrency.

#### Scenario: Document processing uses Core pool
- **WHEN** a document parse or chunk task is enqueued
- **THEN** the task SHALL route to the Core queue and SHALL NOT require Wiki or Enrichment worker capacity to start

#### Scenario: Wiki generation uses Wiki pool
- **WHEN** a `wiki.ingest` or `wiki.finalize` task is enqueued
- **THEN** the task SHALL route to the Wiki queue and SHALL NOT be blocked by long-running Core parsing tasks when Wiki workers are available

#### Scenario: Shared workers are configured
- **WHEN** a Shared worker is configured to listen to selected queues
- **THEN** it SHALL add optional capacity without removing the dedicated concurrency configured for the owning pools

### Requirement: Typed Stage Tasks
The system SHALL represent offline processing as typed tasks that can be executed coarsely at first and split into durable stages without changing public upload APIs.

#### Scenario: Coarse compatibility task
- **WHEN** the first Redis-backed implementation is enabled
- **THEN** the system MAY enqueue a coarse `document.process` task that delegates to the existing processing path while preserving status, trace, retry, and cancellation behavior

#### Scenario: Split stage pipeline
- **WHEN** staged processing is enabled for a document
- **THEN** the runtime SHALL route parse/chunk, embedding/index, postprocess, enrichment, graph, summary, and Wiki tasks through typed stage tasks with deterministic identifiers

### Requirement: Retry Timeout And Dead Letter Handling
The system SHALL apply per-task retry budgets, bounded backoff, execution time limits, and dead-letter recording for exhausted offline tasks.

#### Scenario: Retryable provider failure
- **WHEN** an enrichment or Wiki task fails due to a retryable provider error
- **THEN** the task SHALL record the failure in PostgreSQL, schedule a bounded retry, and keep the document or generation in a non-terminal retrying state

#### Scenario: Retry budget exhausted
- **WHEN** an offline task exhausts its retry budget
- **THEN** the system SHALL mark it dead-lettered with last error, attempt count, payload reference, trace id, and affected document or Wiki generation identifiers

### Requirement: Cancellation Is Honored Across Broker And Database
The system SHALL allow cancellation of queued, scheduled, retrying, and active offline tasks for a document, upload batch, or Wiki generation.

#### Scenario: Cancel queued work
- **WHEN** a user deletes or cancels a document with queued processing work
- **THEN** PostgreSQL state SHALL be marked cancelled and workers SHALL skip future side effects for that document

#### Scenario: Cancel active work
- **WHEN** a worker is already processing a cancelled document
- **THEN** it SHALL observe cancellation before the next stage boundary and stop committing further derived evidence

### Requirement: Queue Status Is Visible To Existing UI
The system SHALL expose queue, retry, running, completed, failed, cancelled, and dead-letter status through existing or additive processing status APIs.

#### Scenario: User opens processing trace drawer
- **WHEN** offline work is queued, running, retrying, completed, failed, cancelled, or dead-lettered
- **THEN** the processing detail API SHALL return enough task and span metadata for the frontend to display the current state and actionable error details

