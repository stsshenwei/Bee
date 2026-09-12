## ADDED Requirements

### Requirement: Relational chat history is authoritative
The system SHALL persist chat sessions and messages in the relational database and SHALL treat the database as the only source of truth for historical conversations.

#### Scenario: History survives process restart
- **WHEN** the API process restarts after completed chat turns were written
- **THEN** session and message history SHALL still be available from the relational database without reading Redis

#### Scenario: Redis does not cache history
- **WHEN** chat history is loaded, paginated, or used as RAG/Agent context
- **THEN** the backend SHALL read session and message rows from the relational database and SHALL NOT use Redis as a history cache layer

#### Scenario: Service boundary documents source of truth
- **WHEN** a developer reads the session/message service layer
- **THEN** comments or module documentation SHALL state that the relational database is authoritative and Redis is limited to transient stream replay and temporary web-search state

### Requirement: Session ownership and configuration
The system SHALL store chat session ownership and input-state configuration with tenant/user isolation fields.

#### Scenario: Principal resolves session owner
- **WHEN** an authenticated request creates or reads sessions
- **THEN** the backend SHALL parse a Principal from the token or request context and derive the session owner through `SessionOwnerIDFromContext`

#### Scenario: Owner-scoped session query
- **WHEN** a user lists or reads sessions
- **THEN** the backend SHALL scope the query by tenant and by `(user_id = current_owner OR user_id IS NULL OR user_id = '')`

#### Scenario: Agent input state is restored
- **WHEN** a session is loaded
- **THEN** the response SHALL include `agent_config` so the frontend can restore session-level input state such as chat mode, selected knowledge bases, and web-search settings

### Requirement: Paired request messages
The system SHALL pair each user message and assistant message with a shared `request_id` and track assistant completion state.

#### Scenario: User and assistant placeholders are created before streaming
- **WHEN** a user sends a chat request
- **THEN** the backend SHALL create a completed user message and an incomplete assistant placeholder in the same session before answer tokens are streamed

#### Scenario: Assistant message completes once
- **WHEN** the stream ends normally
- **THEN** the backend SHALL update the assistant placeholder with the full answer, sources, trace metadata, and `is_completed=true` in one durable completion step

#### Scenario: Request ID pairs one turn
- **WHEN** the frontend receives stream metadata for a new turn
- **THEN** it SHALL be able to match the local user row and assistant row by `request_id` and `assistant_message_id`

### Requirement: Bounded recent context
The system SHALL expose a repository method that returns a bounded recent message window for RAG/Agent multi-turn context.

#### Scenario: Recent context omits old transcript tail
- **WHEN** a session contains more messages than the configured context window
- **THEN** `GetRecentMessagesBySession` SHALL return only the newest bounded window needed for prompt construction

#### Scenario: Recent context is prompt ordered
- **WHEN** recent messages are selected by descending database order for efficiency
- **THEN** the service SHALL sort them back into chronological order before adding them to RAG/Agent context

### Requirement: Cursor-paginated message history
The system SHALL provide cursor pagination for loading older messages from a session.

#### Scenario: Load latest page
- **WHEN** the frontend first opens a session
- **THEN** it SHALL request the latest page with a default limit of 20 and render messages in chronological order

#### Scenario: Load older page before timestamp
- **WHEN** the user scrolls to the top of a non-empty transcript
- **THEN** the frontend SHALL call `GET /api/v1/messages/{session_id}/load?before_time=<oldest_created_at>&limit=20` and insert returned older messages at the beginning

#### Scenario: Pagination terminates
- **WHEN** the backend returns fewer messages than the requested limit or `hasMoreHistory=false`
- **THEN** the frontend SHALL stop requesting older pages for that session

#### Scenario: Duplicate boundary rows are ignored
- **WHEN** two paginated loads include the same message at a page boundary
- **THEN** the frontend SHALL deduplicate by stable message id before updating the transcript

### Requirement: Stable message order
The system SHALL provide deterministic ordering for messages that share a timestamp.

#### Scenario: Same timestamp user precedes assistant
- **WHEN** a user message and its paired assistant message have the same `created_at`
- **THEN** chronological rendering SHALL order the user message before the assistant message

#### Scenario: Database query can use descending index
- **WHEN** the backend queries recent or paginated messages using `created_at DESC`
- **THEN** it SHALL sort the returned slice into ascending display order with role/request tie-breakers before responding
