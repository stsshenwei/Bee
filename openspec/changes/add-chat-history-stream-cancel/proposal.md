## Why

The chat experience currently streams answers but does not provide a WeKnora-style durable history, refresh recovery, or distributed stop mechanism as one coherent contract. Users need conversations to survive reloads, large transcripts to load efficiently, and in-flight generation to be cancellable without depending on the browser SSE connection staying open.

## What Changes

- Add durable chat session/message persistence in the relational database as the only source of truth for history.
- Add owner-aware session/message access so web users, API tenants, and future IM principals see only their scoped history plus explicitly shared anonymous rows.
- Add cursor-based message pagination for loading older history and a bounded recent-message query for RAG/Agent prompt context.
- Persist each chat turn as a completed user message paired by `request_id` with an incomplete assistant placeholder, then update the assistant row once streaming completes or stops.
- Extend stream storage so current-generation events are appended before SSE delivery and can be replayed by session/message identity after refresh or reconnect.
- Add a stop-generation endpoint and UI behavior that appends a distributed stop event, cancels active generation, and durably saves the partial assistant content as completed.
- Document that Redis is only auxiliary for transient stream replay and temporary web-search knowledge state; the relational DB remains authoritative for history.

## Capabilities

### New Capabilities
- `durable-chat-history`: Relational session/message history, owner scoping, paired request IDs, completion state, recent context loading, and cursor pagination.
- `recoverable-chat-streams`: SSE stream event buffering, replay/continue semantics, refresh recovery for incomplete assistant messages, and Redis-vs-memory deployment behavior.
- `chat-generation-cancel`: Stop endpoint, frontend abort behavior, distributed stop event handling, runtime cancellation, and partial-answer persistence.

### Modified Capabilities

None.

## Impact

- Backend storage schema and repositories: extend `conversation` / `conversation_message` or introduce compatibility views/aliases that map to WeKnora-style `sessions` / `messages` fields (`tenant_id`, `user_id`, `agent_config`, `request_id`, `is_completed`).
- Backend API: add message pagination, stream continuation, and stop endpoints under the existing versioned API shape while preserving `/chat/stream` compatibility.
- Backend runtime: update chat pipeline, EventBus, StreamManager, and assistant persistence to support placeholder rows, replayable events, independent stop watching, and cancellable provider calls.
- Frontend chat UI: load history on mount, page older messages on scroll, render current assistant content from SSE only, reconnect incomplete streams, and expose a stop button during generation.
- Redis: required only for distributed stream replay and stop propagation; memory stream manager remains a local fallback with explicit refresh-replay limitations.
- Documentation and tests: update chat/backend design docs and cover repository ordering, owner scope, replay, stop idempotency, and frontend dedupe/restore behavior.
