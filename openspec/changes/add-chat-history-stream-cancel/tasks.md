## 1. Storage And Repository

- [x] 1.1 Extend PostgreSQL schema generation for session ownership/config fields and message `request_id` / `is_completed` fields while keeping existing conversation tables compatible.
- [x] 1.2 Add additive migration/backfill logic for existing conversation rows, defaulting legacy history to shared owner scope and completed message state.
- [x] 1.3 Implement Principal extraction helpers and `SessionOwnerIDFromContext` for web user, API tenant, and anonymous/local development requests.
- [x] 1.4 Update conversation/session repository methods to enforce tenant and `(user_id = owner OR user_id IS NULL OR user_id = '')` scope on list/read/update operations.
- [x] 1.5 Add repository methods for creating paired user/assistant turn rows, completing an assistant placeholder idempotently, and reading a single scoped assistant message.
- [x] 1.6 Add `GetRecentMessagesBySession` and `GetMessagesBySessionBeforeTime` equivalents with descending DB query, ascending service sort, role/request tie-breakers, and stable `hasMoreHistory`.
- [x] 1.7 Add repository tests for ownership filtering, anonymous/shared visibility, request pairing, completion idempotency, pagination boundaries, duplicate timestamps, and recent-context limits.

## 2. Backend API Contracts

- [x] 2.1 Add request/response schemas for session summaries, message rows, paginated message loads, continue-stream identity, and stop-generation payloads.
- [x] 2.2 Add `GET /api/v1/messages/{session_id}/load?before_time=&limit=` for latest and older-page message history.
- [x] 2.3 Add `GET /api/v1/sessions/{session_id}/continue-stream?message_id=&offset=` to replay retained stream events and poll new events.
- [x] 2.4 Add `POST /api/v1/sessions/{session_id}/stop` with `{ "message_id": "..." }`, ownership checks, completed-message idempotency, and privacy-preserving 404/403 behavior.
- [x] 2.5 Preserve legacy `/chat/stream` response compatibility while including durable `session_id` / `conversation_id`, `request_id`, and `assistant_message_id` metadata early in the stream.
- [x] 2.6 Add API tests for pagination responses, incomplete-message recovery, continue-stream not found, stop success, stop already completed, and unauthorized stop.

## 3. Stream Runtime

- [x] 3.1 Update StreamManager configuration to select memory or Redis via `STREAM_MANAGER_TYPE`, with Redis list keys shaped as `stream:events:{sessionId}:{messageId}`.
- [x] 3.2 Add configurable stream TTL with production default 24h and factory/test default 1h where applicable.
- [x] 3.3 Ensure every public chat event is appended to StreamManager before SSE delivery and carries a monotonic offset.
- [x] 3.4 Refactor active chat generation so the producer can continue after browser disconnect and complete the assistant row through a fresh durable DB operation.
- [x] 3.5 Make SSE delivery poll StreamManager every 100ms, replay from offset, and terminate on complete, stop, or terminal error.
- [x] 3.6 Add startup logging/docs for memory StreamManager refresh limitations and Redis requirements for multi-replica deployments.
- [x] 3.7 Add StreamManager unit tests for Redis key naming, offset replay, TTL application, terminal detection, and memory fallback behavior.

## 4. Cancellation Pipeline

- [x] 4.1 Wire stop events from StreamManager into ChatEventBus/runtime cancellation for active quick, reasoning, and wiki chat modes.
- [x] 4.2 Add a stop watcher background task that polls StreamManager every 300ms and exits on stop, complete, terminal error, or 2h timeout.
- [x] 4.3 Update model/tool streaming stages to check cancellation before stage execution and during token/tool iteration.
- [x] 4.4 Accumulate assistant content in runtime state during streaming and persist that exact partial content when stop is consumed.
- [x] 4.5 Mark stopped assistant placeholders `is_completed=true` without indexing stopped partial answers into feedback or knowledge content.
- [x] 4.6 Add race/idempotency tests for stop-before-token, stop-after-partial, stop-after-complete, SSE-disconnect-then-stop, and normal-complete-vs-stop races.

## 5. Frontend History And Recovery

- [x] 5.1 Extend chat message types and API helpers with stable message ids, `request_id`, `assistant_message_id`, `created_at`, `is_completed`, and stopped state.
- [x] 5.2 Load the latest 20 messages for the active session on mount/session selection and render them in chronological order.
- [x] 5.3 Implement scroll-to-top loading of older history using the oldest `created_at` cursor, `hasMoreHistory`, and stable-id dedupe before prepend.
- [x] 5.4 Update send flow to create local user/assistant rows, bind them to streamed durable IDs, and append current assistant content only from SSE.
- [x] 5.5 Detect an incomplete last assistant message after history load and call continue-stream to rebuild partial content from replayed events.
- [x] 5.6 Add a stop-generation control while replying that aborts the local SSE reader, clears loading immediately, marks the row stopped, retains message identity, and posts to the stop endpoint.
- [x] 5.7 Cover frontend parsing/state behavior with tests for initial load, pagination prepend, duplicate boundary rows, stream replay, stop UI state, and later DB history reconciliation.

## 6. Documentation And Validation

- [x] 6.1 Update `docs/ARCHITECTURE.md` with durable chat history, StreamManager Redis/memory behavior, and stop/replay responsibilities.
- [x] 6.2 Update `docs/design-docs/backend-rag-pipeline.md` with placeholder persistence, recent history context, replay, and cancellation flow.
- [x] 6.3 Update `docs/design-docs/frontend-chat-ui.md` with paginated history, current-stream local accumulation, refresh recovery, and stop controls.
- [x] 6.4 Add or update `docs/API.md` entries for message load, continue-stream, stop generation, and stream metadata fields.
- [x] 6.5 Run backend validation from `docs/DEVELOPMENT.md`, including targeted repository/API tests for chat history and streaming.
- [x] 6.6 Run frontend validation from `docs/DEVELOPMENT.md`, including chat UI state tests and lint/type checks.
- [x] 6.7 Perform an end-to-end smoke test: send a question, observe stream events, refresh during generation, continue replay, stop generation, reload history, and verify partial answer persistence.
