## ADDED Requirements

### Requirement: Chat EventBus
The system SHALL route chat runtime progress through a per-run EventBus that emits typed public domain events before they are adapted to SSE payloads.

#### Scenario: Chat emits public lifecycle events
- **WHEN** `/chat/stream` handles a chat request
- **THEN** the runtime SHALL emit typed events for conversation metadata, references, public thoughts or trace steps, tool calls and results, answer tokens, errors, stop, and completion where applicable

#### Scenario: Existing event compatibility is preserved
- **WHEN** the EventBus emits a typed chat event
- **THEN** the SSE adapter SHALL preserve existing client-readable payloads such as `conversation_id`, `sources`, `reasoning`, `token`, `final`, `error`, and `[DONE]`

### Requirement: StreamManager Storage
The system SHALL provide a StreamManager abstraction with memory and Redis implementations for appending and reading chat events by stream offset.

#### Scenario: Memory stream manager in local mode
- **WHEN** the app is configured for single-process local streaming
- **THEN** the memory StreamManager SHALL store events long enough for the active request to read them by monotonic offset

#### Scenario: Redis stream manager in distributed mode
- **WHEN** the app is configured for distributed streaming
- **THEN** the Redis StreamManager SHALL persist events by session and message identifiers so another process can read them by monotonic offset

### Requirement: SSE Replay And Continuation
The system SHALL support replaying stored chat stream events after a client reconnects or asks to continue a stream.

#### Scenario: Client reconnects with offset
- **WHEN** a client reconnects with a valid stream identity and last seen offset
- **THEN** the backend SHALL replay events after that offset and continue streaming new events until completion or error

#### Scenario: Client reconnects after completion
- **WHEN** a client reconnects to a stream that already completed
- **THEN** the backend SHALL replay remaining events and terminate with the same completion semantics as the original stream

### Requirement: Stop And Cancellation Events
The system SHALL represent user stop requests as stream events and cancellation signals that the active chat runtime can observe.

#### Scenario: User stops active chat
- **WHEN** a user stops an active chat response
- **THEN** the system SHALL append a stop event, signal the running chat task, and finish the SSE stream without leaving the message in an ambiguous running state

### Requirement: Stream Event Ordering
The system SHALL keep stream offsets monotonic and preserve references-before-answer and terminal completion ordering.

#### Scenario: Sourced answer
- **WHEN** a sourced answer is streamed
- **THEN** references SHALL be appended and emitted before the first answer token for clients that consume the full event stream

#### Scenario: Fatal error
- **WHEN** chat generation fails fatally
- **THEN** an error event SHALL be appended before the terminal completion marker and existing clients SHALL still receive a compatible `error` payload followed by `[DONE]`

### Requirement: Stream Retention And Privacy
The system SHALL apply configurable retention and sanitization rules to stored chat events.

#### Scenario: Stream retention expires
- **WHEN** a stored chat stream exceeds its configured retention period
- **THEN** the StreamManager SHALL allow cleanup without deleting durable conversation records that are stored elsewhere

#### Scenario: Internal-only data is produced
- **WHEN** the chat runtime produces hidden prompts, secrets, raw provider credentials, or private chain-of-thought
- **THEN** the EventBus and StreamManager SHALL NOT store or expose that data in public SSE events

