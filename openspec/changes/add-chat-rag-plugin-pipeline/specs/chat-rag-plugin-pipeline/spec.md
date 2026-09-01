## ADDED Requirements

### Requirement: Typed Chat Pipeline Context
The system SHALL represent online Chat/RAG execution with a typed pipeline context that separates immutable request configuration, mutable stage state, and runtime handles.

#### Scenario: Pipeline context is initialized for quick chat
- **WHEN** `/chat/stream` starts a quick-answer request through the Chat/RAG pipeline
- **THEN** the pipeline context SHALL include the question, knowledge-base scope, chat mode, memory settings, temporary attachment context, EventBus handle, stream identity, and stop signal

#### Scenario: Stage state is explicit
- **WHEN** a stage writes intermediate output such as rewritten query, retrieved hits, sources, prompt text, answer tokens, or debug metadata
- **THEN** the output SHALL be stored in named state fields rather than hidden globals or request-local side channels

### Requirement: Plugin Stage Registration And Execution
The system SHALL execute Chat/RAG work through registered stage plugins selected by an ordered stage list.

#### Scenario: Quick RAG pipeline runs ordered stages
- **WHEN** quick-answer mode requires RAG retrieval
- **THEN** the executor SHALL run query understanding, retrieval, parent/context recall, source emission, reasoning emission, prompt assembly, streamed completion, persistence, and terminal completion in deterministic order

#### Scenario: Pure chat skips retrieval stages
- **WHEN** a chat request is configured to run without knowledge retrieval
- **THEN** the executor SHALL omit retrieval and source-dependent stages while preserving streamed answer and terminal completion behavior

#### Scenario: Missing stage plugin is detected
- **WHEN** a pipeline stage id has no registered plugin
- **THEN** the executor SHALL fail the run with a typed pipeline error and emit a compatible error event before terminal completion

### Requirement: SSE Compatibility
The system SHALL preserve the existing `/chat/stream` client-readable SSE contract while routing quick-answer execution through the pipeline.

#### Scenario: Existing quick-answer event order is preserved
- **WHEN** a quick RAG answer has retrieved sources
- **THEN** the SSE stream SHALL emit `conversation_id`, `sources`, `reasoning` or trace events, answer `token` payloads, and terminal `[DONE]` in the same client-compatible order as the current raw path

#### Scenario: Old clients ignore additive pipeline metadata
- **WHEN** the pipeline emits stage progress or debug metadata
- **THEN** additive metadata SHALL NOT remove or rename existing `sources`, `reasoning`, `agent_trace`, `token`, `final`, `error`, or `[DONE]` payloads

### Requirement: EventBus And StreamManager Integration
The system SHALL publish public pipeline events through the existing ChatEventBus and store them through the existing StreamManager path.

#### Scenario: Stage events are stored with stream offsets
- **WHEN** a pipeline stage emits a public event
- **THEN** the event SHALL be appended to the active stream identity with a monotonic offset and remain replayable through the existing stream continuation behavior

#### Scenario: Private runtime data is not stored
- **WHEN** a stage handles hidden prompts, secrets, provider credentials, or private chain-of-thought
- **THEN** the EventBus payload SHALL exclude those fields before StreamManager storage

### Requirement: Stage-Level Progress And Observability
The system SHALL expose stage-level progress and tracing for pipeline execution without requiring frontend changes for old clients.

#### Scenario: Retrieval progress is visible
- **WHEN** the pipeline enters query understanding, retrieval, rerank/fusion, merge/context recall, or completion stages
- **THEN** the system SHALL record or emit stage progress metadata that can be associated with the current conversation/message stream

#### Scenario: Stage failures are traceable
- **WHEN** a pipeline stage fails
- **THEN** the error event and debug metadata SHALL identify the stage id, error type, and safe error message

### Requirement: Fallback Handling
The system SHALL handle empty retrieval and recoverable stage failures at pipeline boundaries with compatible fallback behavior.

#### Scenario: Retrieval returns no usable evidence
- **WHEN** the RAG retrieval stages produce no usable hits
- **THEN** the pipeline SHALL emit compatible empty sources/debug metadata and produce the configured fallback answer behavior instead of leaving the stream open

#### Scenario: Rerank degradation preserves answerability
- **WHEN** reranking is enabled but fails or filters all otherwise usable candidates
- **THEN** the retrieval stage SHALL preserve the existing degraded or fallback candidate behavior and record the decision in debug metadata

### Requirement: Cancellation At Stage Boundaries
The system SHALL honor user stop requests before each stage and during streamed completion.

#### Scenario: Stop before completion
- **WHEN** the active stream stop signal is set before the completion stage finishes
- **THEN** the executor SHALL stop further stage execution, emit a compatible stop event, and terminate with `[DONE]`

#### Scenario: Stop during token streaming
- **WHEN** the stop signal is set while the completion stage is yielding answer tokens
- **THEN** the completion stage SHALL stop yielding new tokens and the executor SHALL avoid persisting an ambiguous running message

### Requirement: Retrieval Subset Reuse
The system SHALL allow non-chat callers to run a retrieval-focused subset of pipeline stages where behavior matches existing retrieval semantics.

#### Scenario: Retrieval-only pipeline returns selected hits
- **WHEN** a caller executes the retrieval-only stage list for a scoped query
- **THEN** the pipeline SHALL return the same selected hit shape, citation metadata, and retrieval debug data expected by current search/evaluation callers

#### Scenario: Retrieval-only pipeline does not call the chat model
- **WHEN** the retrieval-only stage list is executed
- **THEN** the pipeline SHALL NOT invoke chat completion or persist assistant messages

### Requirement: Feature-Flagged Migration
The system SHALL support a configuration-controlled migration from the raw quick-chat path to the plugin pipeline.

#### Scenario: Pipeline flag disabled
- **WHEN** the Chat/RAG pipeline feature flag is disabled
- **THEN** quick-answer requests SHALL continue using the existing raw quick-chat path

#### Scenario: Pipeline flag enabled
- **WHEN** the Chat/RAG pipeline feature flag is enabled
- **THEN** quick-answer requests SHALL use the plugin pipeline while preserving existing public response behavior
