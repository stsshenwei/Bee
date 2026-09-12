## ADDED Requirements

### Requirement: Stop generation API
The system SHALL expose an idempotent API for stopping an active assistant generation in a session.

#### Scenario: Stop active assistant message
- **WHEN** the client posts `POST /api/v1/sessions/{session_id}/stop` with `{ "message_id": "<assistant_message_id>" }`
- **THEN** the backend SHALL verify tenant and user ownership, append a stop event to StreamManager, and return success

#### Scenario: Stop already completed message
- **WHEN** the client stops an assistant message that is already `is_completed=true`
- **THEN** the backend SHALL return success without appending duplicate stop side effects

#### Scenario: Stop unauthorized message
- **WHEN** the message does not belong to the requested session, tenant, or user scope
- **THEN** the backend SHALL return a not-found or forbidden response without revealing another user's history

### Requirement: Stop event propagation
The system SHALL represent cancellation as a distributed stream event instead of directly killing only the local HTTP handler.

#### Scenario: Stop event written to shared stream
- **WHEN** StopSession accepts a stop request
- **THEN** it SHALL append a `stop` event with `session_id`, `message_id`, and reason `user_requested` to the same StreamManager identity used by the active answer stream

#### Scenario: SSE loop observes stop
- **WHEN** the active SSE polling loop reads a stop event
- **THEN** it SHALL emit a stop response to the frontend, signal the runtime EventBus, and terminate the SSE response predictably

#### Scenario: Independent watcher observes stop
- **WHEN** the client closes SSE before POSTing stop
- **THEN** a background stop watcher SHALL poll StreamManager every 300ms and convert the stop event into the same runtime cancellation signal

#### Scenario: Stop watcher exits
- **WHEN** the watcher observes stop, complete, terminal error, or reaches the 2 hour safety timeout
- **THEN** it SHALL exit without leaking a background task

### Requirement: Runtime cancellation
The system SHALL cancel active model/tool generation when a stop event is observed.

#### Scenario: Provider call is interrupted
- **WHEN** the runtime cancellation signal is set during LLM streaming or tool execution
- **THEN** provider calls and pipeline stages that accept cancellation SHALL stop as soon as safely possible

#### Scenario: Stop does not depend on browser connection
- **WHEN** generation continues in a detached backend task after the browser disconnects
- **THEN** the stop watcher SHALL still be able to cancel that task through the shared stop event

### Requirement: Partial answer persistence
The system SHALL durably save the assistant content accumulated before cancellation.

#### Scenario: Stop saves partial content
- **WHEN** cancellation is consumed after some answer chunks have been generated
- **THEN** the backend SHALL update the assistant placeholder with the accumulated partial content and set `is_completed=true`

#### Scenario: Empty stopped answer is completed
- **WHEN** cancellation is consumed before any answer content is generated
- **THEN** the backend SHALL mark the assistant placeholder completed with an empty or stopped-status content according to the response contract and SHALL NOT leave it incomplete

#### Scenario: Stopped answer is not indexed as feedback knowledge
- **WHEN** the assistant message is completed due to stop
- **THEN** the backend SHALL NOT index the partial answer into the knowledge base as feedback or corrective corpus content

### Requirement: Frontend stop behavior
The system SHALL provide immediate stop controls during generation while preserving enough identity to complete backend cancellation.

#### Scenario: Stop button replaces send while replying
- **WHEN** the frontend is waiting for an assistant response
- **THEN** it SHALL show a stop-generation control and disable duplicate sends for the active composer

#### Scenario: Stop updates local UI immediately
- **WHEN** the user clicks stop
- **THEN** the frontend SHALL abort the active SSE reader, clear loading state, mark the assistant message as stopped locally, and retain `currentAssistantMessageId`

#### Scenario: Stop API follows local abort
- **WHEN** the local stream reader is aborted by the stop control
- **THEN** the frontend SHALL POST the active `message_id` to the session stop endpoint so backend generation can be cancelled across processes

#### Scenario: Later history matches stopped UI
- **WHEN** history is reloaded after stop persistence completes
- **THEN** the stopped assistant row SHALL appear as a completed message containing the partial content that was visible before cancellation
