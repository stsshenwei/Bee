## Context

Bee already has several Weknora-style foundations:

- offline processing is split into typed Celery/PostgreSQL tasks and isolated queues;
- chat streaming has a `ChatEventBus` and `StreamManager` with memory/Redis storage;
- retrieval quality work already includes query understanding, dense and keyword recall, RRF fusion, rerank degradation, duplicate removal, parent recall, and debug metadata.

The gap is online quick-answer orchestration. The raw quick-chat path in `backend/app/main.py` still runs a fixed sequence directly:

```text
hybrid_retrieve_hits
-> recall_parent_hits
-> extract_sources
-> build_reasoning_summary / agent_trace
-> stream_answer
-> append conversation + memory side effects
```

That shape works, but it makes every new feature add another branch around `main.py` or another helper inside the already-large `RAGService`. Weknora's useful lesson is not the Go implementation itself; it is the split between:

- a typed request/state/runtime context;
- an ordered list of stage identifiers;
- stage plugins with narrow responsibilities;
- one executor that handles progress, tracing, fallback, cancellation, and errors.

## Goals / Non-Goals

**Goals:**

- Introduce a Python-native Chat/RAG pipeline executor and plugin interface.
- Move quick-answer orchestration out of request-level `_stream_raw_chat_events` into pipeline stages.
- Keep `/chat/stream` payload compatibility for existing clients: `conversation_id`, `sources`, `reasoning`, `agent_trace`, `token`, `final`, `error`, and `[DONE]`.
- Reuse existing services instead of rewriting retrieval, reranking, prompt templates, memory, or StreamManager.
- Make stage-level progress and debug output available through EventBus/StreamManager without leaking hidden prompts, secrets, or private chain-of-thought.
- Allow dynamic assembly of pure chat, quick RAG, memory-enabled quick RAG, and retrieval-only stage subsets.
- Reduce future growth pressure on `backend/app/main.py` and `RAGService`.

**Non-Goals:**

- Do not replace the existing Celery/PostgreSQL offline processing runtime.
- Do not port Weknora's Go `EventManager` directly.
- Do not change public `/chat/stream` semantics or require frontend migration for old clients.
- Do not rewrite the agent runtime, wiki runtime, or reasoning mode in the first implementation.
- Do not change retrieval scoring behavior except where extracting it into a stage requires preserving equivalent outputs.
- Do not add a new external dependency.

## Decisions

### Decision: Add a small `chat_pipeline` package

Create `backend/app/services/chat_pipeline/` with:

- `types.py`: stage ids, request/config dataclasses, mutable state, runtime handles, and pipeline result;
- `plugin.py`: `ChatPipelinePlugin` protocol and `ChatPipelineError`;
- `executor.py`: stage loop, tracing/progress, cancellation checks, and fallback handling;
- one module per stage group, for example `history.py`, `memory.py`, `query_understand.py`, `retrieval.py`, `prompt.py`, `completion.py`.

Rationale: this keeps the new architecture separate from `RAGService` while still allowing stages to call existing `RAGService` methods during migration.

Alternative considered: put the executor inside `RAGService`. That would reduce file count but keep the same growth pressure and blur orchestration with retrieval implementation.

### Decision: Model context like Weknora's `ChatManage`, but Pythonic

Use a single `ChatPipelineContext` object with three conceptual areas:

```text
request   immutable inputs: question, scope, mode, top_k, attachments, memory flags
state     mutable stage output: query, intent, hits, sources, prompt, answer parts, debug
runtime   EventBus, stream identity, stop signal, trace id, service references
```

Rationale: stage plugins need shared state, but the contract should be explicit and testable. Separating request/state/runtime prevents stage code from smuggling configuration through globals.

Alternative considered: pass loose dictionaries between stages. That is quick to write but makes stage dependencies opaque and fragile.

### Decision: Keep stage plugins synchronous generators only where streaming is needed

Most stages return normally after mutating context. The completion stage may yield SSE-compatible event payloads or typed events as it streams tokens.

Rationale: current quick chat uses synchronous FastAPI `StreamingResponse` generators. A fully async pipeline would be nice later, but it is not required for compatibility and would increase migration risk.

Alternative considered: make every stage async. That would better fit future distributed chat workers, but it forces broader changes to current retrieval and OpenAI client usage.

### Decision: Initial quick RAG stage list

The first quick-answer pipeline should assemble roughly:

```text
emit_conversation
load_history
memory_retrieval
query_understand
retrieve
recall_parent_context
emit_sources
emit_reasoning
emit_agent_trace
into_prompt
chat_completion_stream
memory_storage
persist_assistant_message
done
```

The retrieval-only subset should support:

```text
query_understand
retrieve
recall_parent_context
filter_top_k
```

Rationale: this mirrors Weknora's stage separation while matching Bee's current public outputs. Some stages, like `emit_sources`, are explicitly output stages because existing clients expect references before answer tokens.

Alternative considered: only pipeline retrieval and leave SSE emission in `main.py`. That would help reuse search, but the real branch complexity in quick chat would remain.

### Decision: Preserve current EventBus/StreamManager as the event persistence boundary

Pipeline stages should publish typed public events through the existing `ChatEventBus`; the existing adapter can continue appending to `StreamManager` and formatting SSE payloads.

Rationale: the stream storage work is already present and tested. The pipeline should feed it, not replace it.

Alternative considered: let pipeline stages write directly to `StreamManager`. That couples business stages to storage and makes unit tests heavier.

### Decision: Keep `RAGService` as retrieval/generation service during migration

Initial stage implementations should call existing `RAGService` methods such as `hybrid_retrieve_hits`, `recall_parent_hits`, `extract_sources`, `build_reasoning_summary`, `build_chat_agent_trace`, and `stream_answer`.

Rationale: this preserves current behavior while moving orchestration first. After the executor is stable, retrieval internals can be split into smaller services incrementally.

Alternative considered: refactor `RAGService` and introduce pipeline in one pass. That is riskier because behavior, architecture, and call sites would all move at once.

### Decision: Fallback and cancellation happen at stage boundaries

The executor should check the stop signal before each stage and during streaming token iteration. Retrieval-empty fallback should be centralized: if retrieval returns no usable evidence, stages emit compatible empty `sources`, reasoning/fallback metadata, and either fixed fallback answer or configured model fallback where current behavior supports it.

Rationale: Weknora's stage boundary handling is one of its biggest operational advantages. It prevents half-finished side effects and gives clearer traces.

Alternative considered: keep fallback inside individual stages only. That allows custom behavior but makes global ordering and compatibility harder to reason about.

## Risks / Trade-offs

- [Risk] Stage extraction changes output order or duplicates SSE events. -> Mitigation: add golden-order tests for quick chat and replay-compatible stored events.
- [Risk] `RAGService` remains large after this change. -> Mitigation: treat this as orchestration extraction first; follow-up work can split retrieval services after behavior is stable.
- [Risk] Pipeline context becomes a new god object. -> Mitigation: keep request/state/runtime dataclasses narrow, document stage inputs/outputs, and test each plugin independently.
- [Risk] Streaming stages make error handling subtle. -> Mitigation: executor owns terminal `done`/`error` behavior and wraps streaming iteration.
- [Risk] Additive progress events could surprise old clients. -> Mitigation: preserve existing payload keys and hide new stage metadata under ignored additive fields or typed internal events.
- [Risk] Agent, reasoning, and wiki chat modes diverge from quick mode. -> Mitigation: migrate quick mode first, then decide whether agent/wiki should run on the same pipeline abstraction or stay in their runtime.

## Migration Plan

1. Add the `chat_pipeline` package with types, plugin protocol, executor, and no-op/basic test stages.
2. Implement quick-answer stages using existing `RAGService`, conversation, memory, EventBus, and StreamManager integrations.
3. Add a feature flag, for example `CHAT_RAG_PIPELINE_ENABLED`, defaulting conservatively for local validation.
4. Wire quick chat mode in `/chat/stream` to the pipeline when enabled; retain the current `_stream_raw_chat_events` path as fallback during rollout.
5. Add tests for stage ordering, source-before-token order, empty retrieval fallback, stop behavior, error-to-done behavior, and old-client SSE compatibility.
6. Add retrieval-only executor helper and migrate pure search/evaluation call sites only after quick chat parity is proven.
7. Update architecture and backend RAG design docs with the online pipeline model.
8. Remove the old raw quick-chat branch once compatibility tests and manual smoke validation pass.

Rollback is configuration-based: disable `CHAT_RAG_PIPELINE_ENABLED` and use the existing raw quick-chat path while keeping the new package in place for further fixes.

## Open Questions

- Should the first pipeline default to enabled in development only, or enabled everywhere after tests pass?
- Should fallback answer generation remain inside `RAGService.stream_answer`, or become its own plugin stage from the start?
- Should retrieval-only pipeline migration be part of the first implementation or a follow-up after quick chat parity?
- Should stage progress be rendered in the current frontend trace UI immediately, or stored first and surfaced in a later UI polish pass?
