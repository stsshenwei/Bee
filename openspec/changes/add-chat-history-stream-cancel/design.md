## Context

The repository is a FastAPI + Next.js RAG application with PostgreSQL 16/pgvector as the active production store. It already has `conversation` and `conversation_message` tables, a `PostgresConversationRepository`, a chat pipeline, a public `/chat/stream` SSE contract, `ChatEventBus`, and memory/Redis StreamManager foundations. The current shape is close to WeKnora's direction but does not yet provide the exact durable session/message contract, cursor history loading, refresh replay, or distributed stop semantics described in this change.

WeKnora's reference model uses relational `sessions` and `messages` tables with `tenant_id`, `agent_config`, `request_id`, and `is_completed`, and uses Redis only for `stream:events:{sessionId}:{messageId}` replay buffers plus `tempkb:{sessionId}` web-search state. This project should mirror the behavior while using Python repositories and PostgreSQL schema generation rather than introducing GORM.

## Goals / Non-Goals

**Goals:**
- Make relational chat history authoritative and explicitly keep Redis out of historical session/message reads.
- Add tenant/user scoped session access based on a Principal resolved from request context.
- Persist a completed user message and incomplete assistant placeholder before streaming starts.
- Support bounded recent history for prompt context and cursor pagination for large transcripts.
- Buffer current stream events before SSE delivery and support refresh/reconnect replay.
- Stop generation through a distributed stop event, cancel runtime work, and save partial assistant content as completed.
- Keep existing `/chat/stream` clients compatible while adding versioned session/message APIs.

**Non-Goals:**
- Replacing the current FastAPI backend with Go/GORM.
- Persisting every transient SSE event into PostgreSQL.
- Using Redis as a query cache for historical messages.
- Changing document retrieval, pgvector indexing, Wiki storage, or feedback semantics beyond the stopped-answer exclusion.
- Building IM-channel integrations; the owner/principal model should leave room for them.

## Decisions

### Decision: Evolve existing conversation tables into WeKnora-style session/message records

Use PostgreSQL DDL and repository migrations to add the required fields to existing `conversation` and `conversation_message` tables, or create compatibility aliases at the service/API layer named session/message. Target fields:

- session: `tenant_id`, `user_id`, `agent_config`, optional `deleted_at`, and existing `title`, `summary`, timestamps
- message: `request_id`, `role`, `content`, `metadata_json`/typed source fields, `is_completed`, timestamps, optional `deleted_at`

Rationale: the current code already wires conversation repositories into memory, chat pipeline, and schemas. Extending them limits blast radius and avoids dual history stores.

Alternatives considered:
- Create separate `sessions` and `messages` tables: closer to WeKnora names but duplicates existing conversation state and requires migration/compatibility work immediately.
- Keep the current minimal schema: insufficient for ownership, paired requests, incomplete assistant recovery, and stop persistence.

### Decision: Principal scope is a service-level guardrail

Introduce a lightweight Principal resolver from request headers/token context with principal kinds such as `web_user`, `api_tenant`, and future `im_user`. `SessionOwnerIDFromContext` returns the effective owner for session rows. All list/read/update/delete methods apply tenant scope plus `(user_id = current_owner OR user_id IS NULL OR user_id = '')`.

Rationale: consistent repository scoping is safer than repeating ad hoc route checks. It also matches the WeKnora behavior requested by the user.

Alternatives considered:
- Trust frontend-provided user IDs: rejected because it weakens isolation.
- Apply scope only in API handlers: easy to miss when services call repositories directly.

### Decision: Persist placeholders before starting generation

When `/chat/stream` receives a new user message, the backend creates or loads the session, stores `agent_config`, creates a `request_id`, inserts the completed user message, and inserts an incomplete assistant placeholder. The stream response emits session/message metadata early so the frontend can bind local rows to durable IDs. Normal completion performs one update of the assistant row with full content, sources, trace metadata, and `is_completed=true`.

Rationale: the placeholder is the durable anchor needed by refresh recovery and stop requests.

Alternatives considered:
- Persist assistant only at the end: cannot identify the active assistant row after refresh and cannot stop by message id.
- Persist every token to DB: durable but too write-heavy and duplicates StreamManager's short-lived purpose.

### Decision: Separate history queries from prompt context queries

Add repository methods equivalent to:

- `GetRecentMessagesBySession(session_id, limit, principal)` for bounded RAG/Agent context.
- `GetMessagesBySessionBeforeTime(session_id, before_time, limit, principal)` for UI pagination.

Both can query descending for index efficiency, then sort ascending for display/prompt order. Tie-breakers put paired user messages before assistant messages when timestamps match.

Rationale: UI history and model context have different bounds and should not accidentally load an entire transcript into prompts.

Alternatives considered:
- One `list_messages` method for everything: simple but unsafe for long histories and prompt size.

### Decision: StreamManager is the transient stream event log

Every public stream event is appended to StreamManager before the SSE loop emits it. Redis StreamManager should use the WeKnora-compatible list key `stream:events:{sessionId}:{messageId}`, monotonic offsets, and TTL. Memory StreamManager remains a single-process development fallback and should clearly report that refresh replay is not guaranteed after restart or cross-replica routing.

Rationale: this keeps current-generation recovery independent from the browser connection while preserving PostgreSQL as the history source of truth.

Alternatives considered:
- Push directly from generator to SSE: lowest latency but fails refresh/replay and distributed stop.
- PostgreSQL stream events: stronger durability than needed and higher write load.

### Decision: Generation is detached from request cancellation, but stop-aware

Generation should run with a lifecycle that survives browser disconnect long enough to finish and update the assistant row. In Python, this means avoiding dependence on the `StreamingResponse` client task as the only owner of the model call, and using explicit cancellation objects/events for user stop. Completion updates use a fresh durable DB operation so client disconnect cancellation cannot prevent `is_completed=true`.

Rationale: mirrors WeKnora's `context.WithoutCancel` behavior in the current Python stack.

Alternatives considered:
- Let browser disconnect cancel generation: saves provider spend, but breaks refresh recovery and can strand incomplete placeholders.

### Decision: Stop is a distributed event plus local cancellation signal

`POST /api/v1/sessions/{session_id}/stop` verifies ownership and message state, then appends a `stop` event to StreamManager. Active SSE polling converts stop to an EventBus/runtime signal. A separate stop watcher polls every 300ms and catches API/programmatic clients that close SSE before calling stop. The watcher exits on stop, complete, terminal error, or a 2 hour timeout.

Rationale: writing stop into Redis lets another replica or detached generation task observe cancellation without sharing in-process state.

Alternatives considered:
- Directly set only an in-memory event: works locally but not in distributed deployments.
- Mark DB row completed immediately in StopSession: races with the generator and can lose the actual partial content accumulated in memory.

### Decision: Frontend treats active answer as stream state

On mount, the chat UI loads the latest history page from DB. If the last assistant row is incomplete, it calls continue-stream with session/message identity, rebuilds the partial answer from replayed events, and resumes polling. During normal streaming it appends incoming chunks locally, and only trusts DB content after completion or history reload. Loading older pages prepends messages and deduplicates with a stable id set.

Rationale: DB history and current stream have different freshness models; mixing them causes duplicate or stale assistant rows.

Alternatives considered:
- Poll DB during streaming: simpler recovery but slow, write-heavy, and incompatible with token-level UX.

## Risks / Trade-offs

- [Risk] Existing code and docs use both `conversation_id` and session terminology. -> Mitigation: keep API compatibility fields and document `session_id` as the durable conversation identity.
- [Risk] Placeholder rows can remain incomplete if the process dies before DB completion. -> Mitigation: provide incomplete-state UI recovery, replay fallback behavior, and future reconciliation hooks that can mark orphaned rows failed or stopped.
- [Risk] Memory StreamManager cannot support refresh replay across restart or replicas. -> Mitigation: log startup warnings and document `STREAM_MANAGER_TYPE=redis` as required for production multi-replica.
- [Risk] Detached generation can continue spending provider tokens after a browser disconnect. -> Mitigation: expose explicit stop, make runtime stop-aware, and keep wall-clock/provider timeouts.
- [Risk] Owner scoping could hide anonymous/shared sessions unexpectedly. -> Mitigation: cover `(user_id = owner OR user_id IS NULL OR user_id = '')` in repository tests and API tests.
- [Risk] Stop and normal completion can race. -> Mitigation: make assistant completion idempotent, update only incomplete rows, and preserve the first terminal outcome metadata.

## Migration Plan

1. Add nullable ownership/config/completion columns to PostgreSQL schema generation and compatibility migration scripts.
2. Backfill existing conversations with empty `user_id`, empty/default `tenant_id` where needed by local mode, and `is_completed=true` for existing assistant messages.
3. Add repository methods and tests for scoped access, recent context, cursor pagination, placeholder creation, and assistant completion.
4. Update `/chat/stream` to create placeholders and emit durable IDs while preserving legacy response fields.
5. Add StreamManager Redis configuration, continue-stream endpoint, stop endpoint, stop watcher, and cancellation-aware pipeline wiring behind safe defaults.
6. Update the frontend to load/paginate history, reconnect incomplete streams, stop generation, and dedupe message pages.
7. Update docs and run backend/frontend validation.

Rollback: keep new columns nullable and additive. If stream replay causes production issues, disable Redis StreamManager or the new versioned endpoints while leaving durable completed history readable. Existing legacy `/chat/stream` clients should continue to receive compatible SSE payloads throughout the rollout.

## Open Questions

- Should the public API expose both `/api/v1/sessions/{session_id}/continue-stream` and a message-id-only convenience route, or only the session/message route?
- Which token/header claims should be canonical for the first Principal implementation in local development, web auth, and API-key modes?
- Should orphaned incomplete assistant rows age into a visible `failed` state, or remain `is_completed=false` until a reconciliation task can determine the final outcome?
