## ADDED Requirements

### Requirement: Current generation comes from SSE
The system SHALL render the currently generating assistant message from SSE events while using the database only for persisted historical messages.

#### Scenario: In-flight assistant is locally accumulated
- **WHEN** the backend streams `answer`, `thinking`, `tool_call`, token, source, or trace events for an incomplete assistant message
- **THEN** the frontend SHALL match them by `request_id` and `assistant_message_id` and append content locally in real time

#### Scenario: History reload does not duplicate active stream
- **WHEN** the frontend has an active stream and refreshes history data from the database
- **THEN** it SHALL avoid replacing the in-flight assistant content with stale incomplete database content

### Requirement: Stream events are buffered before delivery
The system SHALL append each public stream event to the StreamManager before it is delivered to an SSE client.

#### Scenario: Event written before SSE send
- **WHEN** the runtime produces a stream event
- **THEN** the backend SHALL atomically append the event to stream storage before the SSE polling loop forwards it to the browser

#### Scenario: SSE loop polls stream storage
- **WHEN** a chat request is actively streaming
- **THEN** the SSE response loop SHALL poll StreamManager every 100ms and emit newly appended events in offset order until a terminal event is observed

#### Scenario: Browser disconnect does not erase events
- **WHEN** the browser closes the SSE connection while generation continues
- **THEN** already appended events SHALL remain readable from StreamManager until retention expires

### Requirement: Redis stream replay
The system SHALL support Redis-backed stream replay by session/message identity for refresh and reconnect.

#### Scenario: Redis key shape is stable
- **WHEN** Redis StreamManager is enabled
- **THEN** stream events SHALL be stored in a List at `stream:events:{sessionId}:{messageId}` with monotonic offsets and a retention TTL

#### Scenario: Default stream retention
- **WHEN** no environment override is provided for stream retention
- **THEN** Redis stream replay TTL SHALL default to 24 hours for production configuration and MAY default to 1 hour in factory/test configuration

#### Scenario: Replay starts from offset
- **WHEN** a client calls continue-stream with an offset
- **THEN** the backend SHALL replay events after that offset and continue polling new events until complete, stop, or error

### Requirement: Refresh recovery for incomplete assistant messages
The system SHALL resume display of an in-progress assistant answer after browser refresh when stream events are still retained.

#### Scenario: Continue incomplete last assistant message
- **WHEN** the frontend loads a session and the last assistant message has `is_completed=false`
- **THEN** it SHALL call the continue-stream endpoint with the session id and assistant message id and rebuild the visible partial answer from replayed events

#### Scenario: Completed during refresh
- **WHEN** generation completed while the page was closed
- **THEN** the frontend SHALL render the completed database message after history load and SHALL NOT continue polling once `is_completed=true`

#### Scenario: Replay buffer missing
- **WHEN** the latest assistant message is incomplete but the stream buffer has expired or was stored in process memory that is no longer available
- **THEN** continue-stream SHALL return a not-found response and the frontend SHALL keep a recoverable incomplete state until the next history reload observes a completed database row or an error state

### Requirement: Distributed deployment warning
The system SHALL make Redis StreamManager requirements explicit for refresh recovery in production.

#### Scenario: Memory manager fallback
- **WHEN** `STREAM_MANAGER_TYPE` is unset or configured to memory
- **THEN** the backend SHALL support active single-process streaming but SHALL NOT guarantee refresh replay after process loss or cross-instance reconnect

#### Scenario: Redis required for multi-replica
- **WHEN** the service is deployed with multiple API replicas
- **THEN** operators SHALL configure `STREAM_MANAGER_TYPE=redis` and Redis connection settings so continue-stream and stop propagation can be observed across replicas

### Requirement: Temporary web-search state remains auxiliary
The system SHALL keep temporary web-search knowledge state in Redis without making it part of durable chat history.

#### Scenario: Temporary KB state is separate from history
- **WHEN** web-search temporary knowledge state is stored for a session
- **THEN** it SHALL use `tempkb:{sessionId}` or equivalent auxiliary state and SHALL NOT be required to load durable session/message history
