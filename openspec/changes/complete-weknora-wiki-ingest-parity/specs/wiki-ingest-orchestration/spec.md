## ADDED Requirements

### Requirement: Wiki generation runs as durable background work
The system SHALL execute Wiki generation through persisted `wiki.ingest` and `wiki.finalize` tasks with leases, retries, idempotency keys, attempt history, cancellation, and dead-letter visibility.

#### Scenario: Upload request returns before Wiki generation finishes
- **WHEN** source chunks are committed for a Wiki-enabled document
- **THEN** the system durably enqueues Wiki work and allows the upload request or upload worker step to finish without waiting for LLM page generation

#### Scenario: Worker restarts during generation
- **WHEN** a worker loses its lease before acknowledging a Wiki task
- **THEN** another worker can reclaim the task without creating duplicate page revisions or log entries

#### Scenario: Retry budget is exhausted
- **WHEN** a Wiki task repeatedly fails beyond its configured retry budget
- **THEN** it enters a dead-letter state with the last error, attempt count, affected documents, and a retry action

### Requirement: Pending documents are debounced and batched per knowledge base
The orchestrator SHALL coalesce newly uploaded or reprocessed Wiki documents per knowledge base for a configurable debounce interval and process them in bounded batches.

#### Scenario: Multiple documents arrive together
- **WHEN** several documents are committed to the same Wiki knowledge base during the debounce window
- **THEN** the system claims them as one bounded batch while retaining per-document progress and failure attribution

#### Scenario: More documents remain after a batch
- **WHEN** a batch completes and pending Wiki documents still exist
- **THEN** the system schedules a follow-up batch without requiring another upload event

### Requirement: Map processing extracts grounded page updates
For each eligible document, the Map phase SHALL load persisted chunks, construct a bounded source representation, extract candidate entity and concept slugs, create a document summary, and classify supporting chunks against candidates.

#### Scenario: Normal Map execution
- **WHEN** a document contains sufficient source text
- **THEN** extract runs first and summary and chunk classification run with bounded parallelism, producing validated summary, entity, and concept updates with exact source references

#### Scenario: Candidate extraction response is invalid
- **WHEN** structured candidate extraction cannot be validated after its retry policy
- **THEN** the system invokes the configured conservative fallback extractor or records a document-level partial failure without inventing unsupported candidates

#### Scenario: Source is too small
- **WHEN** the document has insufficient meaningful text for Wiki generation
- **THEN** the task records a skipped Map result and does not call page-reduction prompts

### Requirement: Reduce processing is grouped and serialized by page slug
The Reduce phase SHALL group updates by canonical slug, execute different slugs with bounded concurrency, and serialize conflicting updates to the same knowledge-base page.

#### Scenario: Several documents mention one entity
- **WHEN** Map outputs from multiple documents target the same entity slug
- **THEN** one Reduce operation merges their additions and retractions into one versioned page update

#### Scenario: Independent slugs are ready
- **WHEN** a batch contains updates for distinct slugs
- **THEN** the worker may reduce them concurrently up to the configured per-knowledge-base limit

### Requirement: LLM inputs and outputs are bounded and validated
The system MUST enforce configurable source-length, candidate-count, page-count, batch-size, concurrency, timeout, and output-schema limits for every Wiki LLM operation.

#### Scenario: Candidate count exceeds the limit
- **WHEN** extraction returns more candidates than allowed for one document
- **THEN** the system applies deterministic ranking and truncation before classification and records the truncation in task metadata

#### Scenario: Provider rate limit occurs
- **WHEN** an LLM call returns a rate-limit response
- **THEN** the task is rescheduled with bounded backoff and does not busy-loop or lose pending document state

### Requirement: Wiki processing exposes hierarchical telemetry
The system SHALL emit correlated spans for `postprocess.wiki`, `postprocess.wiki.extract`, `postprocess.wiki.summary`, `postprocess.wiki.classify`, and `postprocess.wiki.page[<slug>]`, including duration, status, attempt, model usage, and affected identifiers.

#### Scenario: User opens processing details
- **WHEN** a Wiki task has run or is running
- **THEN** the processing API returns the hierarchical Wiki spans under the document post-processing stage with actionable failure details

### Requirement: Generation can be cancelled and superseded
The system SHALL stop obsolete Wiki work when its document is deleted, reprocessed into a newer source revision, or explicitly cancelled.

#### Scenario: Document is reprocessed during an older task
- **WHEN** an older Wiki task reaches Reduce after a newer document revision has been committed
- **THEN** the older task detects the revision mismatch and does not publish stale additions

