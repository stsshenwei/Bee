# Backend RAG Pipeline

PostgreSQL 16 with pgvector is the single active production store for knowledge data and retrieval indexes. SQLite, FTS5, Chroma, and Milvus files or collections are legacy artifacts and are never imported during normal startup.

## Parse And Chunk

`POST /documents/parse` is a read-only preview path. It reuses the production parser and chunker but does not write PostgreSQL rows, pgvector embeddings, KG state, Wiki state, audit rows, or enrichment state.

Production ingest parses documents through the configured parser registry, builds parent chunks plus indexable child/table/OCR/image-derived chunks, and preserves page/title/table/image metadata. Parent and child chunks are both persisted in PostgreSQL. Indexable chunk types are embedded and written to `document_chunk_embedding`.

## Persistence

The production write path stores:

- `document` and `document_chunk` as authoritative raw evidence
- keyword-search data derived from authoritative chunk rows
- `document_chunk_embedding` pgvector rows for child/table/OCR/image OCR/image caption chunks
- processing spans and durable task/dead-letter rows
- upload batch/file rows and image resources/operations
- optional KG extraction tasks and entity mentions
- optional Wiki page/generation/contribution/log rows
- audit, feedback, memory, conversation, and evaluation rows

Every durable row that represents knowledge evidence carries workspace/KB ownership. Document-scoped operations validate the owning document before writing chunks, images, tasks, vectors, Wiki work, or KG work.

## Retrieval

Hybrid retrieval uses PostgreSQL for both dense and keyword recall:

1. `KnowledgeBaseScope` is resolved from `knowledge_base_id`, `knowledge_base_ids`, and `document_ids`.
2. Optional query understanding rewrites or expands the query under configured bounds.
3. Dense recall calls `PostgresVectorStore.query_dense`, which applies workspace/KB/document filters before pgvector distance ranking.
4. Keyword recall calls `PostgresKeywordSearch`, which delegates to PostgreSQL full-text, trigram, and exact fallback search over chunk rows with the same filters.
5. Weighted RRF fuses dense and keyword candidates. Candidate metadata preserves vector score, keyword score, hybrid score, and optional reranker score.
6. Parent/table/OCR/image context is hydrated from PostgreSQL, citations are verified against scoped chunks, and context is assembled for generation.

Explicit `doc_ids` are retrieval constraints, not post-filters. Dense, keyword, hydration, parent recall, graph evidence, citation verification, and final context all reject chunks outside the selected documents.

## Processing Runtime

Upload confirmation can either enqueue durable PostgreSQL task rows for workers or run the compatibility background path. `DocumentProcessingWorker` uses PostgreSQL row claiming and leases to avoid duplicate work. Spans are stored in PostgreSQL and are the source of truth for the frontend trace drawer; local files under `PROCESSING_TRACE_DIR` remain supplemental debugging evidence.

Staged upload batches always enable Dense, Keyword, Wiki, and Graph indexing channels. Upload confirmation also upgrades the target knowledge-base indexing strategy to those enabled channels before task registration so Wiki generation and graph enrichment are not blocked by older KB defaults.

When `ASYNC_RUNTIME_ENABLED=true` and `ASYNC_RUNTIME_MODE=celery`, upload confirmation persists the PostgreSQL task row and dispatches a matching Celery task to Redis. Celery workers call back into the same processing worker service but claim the exact PostgreSQL task id before doing side effects. This keeps Redis as delivery infrastructure and PostgreSQL as the authoritative runtime state.

Initial Redis-backed routing is coarse and compatible: `upload_file.process` runs in the Core queue and delegates to the existing parse/chunk/index/postprocess path. The runtime also defines stable task routes for staged work:

- Core: `document.process`, `upload_file.process`, `chunk.extract`, `embedding.index`
- PostProcess: `knowledge.post_process`
- Enrichment: `summary.generation`, `generated_questions`, `image.multimodal`, `graph.extraction`
- Wiki: `wiki.ingest`, `wiki.finalize`
- Maintenance: `datasource.sync`, `maintenance.reconcile`, `delete.cleanup`
- Shared: fallback route for unknown or overflow work

Deleting a document cancels queued/active tasks for that document, closes open spans on the latest attempt, removes PostgreSQL chunks/keyword/vector rows, and deletes derived media objects.

## Wiki Runtime

LLM Wiki adds a curated page layer after raw chunk persistence. Pages, folders, issues, proposals, source refs, aliases, generation tasks, contribution manifests, pending operations, link caches, and logical logs live in PostgreSQL.

Wiki generation reads persisted source chunks directly and does not invoke embedding or rerank models. A Wiki-only KB can skip dense/keyword embedding while still enqueueing Wiki work.

Wiki page search normalizes natural-language questions into bounded recall candidates before querying storage, so Chinese questions such as "该设备支持哪些安全认证？" can fall back from the full sentence to compact terms like "安全认证" or "认证" instead of relying on one literal SQL match.

In Celery mode, Wiki ingest/finalize task rows still live in PostgreSQL and are dispatched to the Wiki queue for hard isolation from document parsing and embedding work.

## Chat Event Streaming

The public `/chat/stream` SSE payloads remain backwards-compatible. Internally, chat events can be appended to a StreamManager by conversation/session id and message id. The current implementation provides an in-memory StreamManager foundation and adds stream metadata to outgoing JSON payloads under `_stream`, which old clients ignore. Stored events use monotonic offsets so future clients can reconnect with a last-seen offset and replay missed events before terminal `[DONE]`.

`chat_mode: "rag_wiki"` routes to the Hybrid RAG + Wiki runtime policy and the `hybrid_rag_wiki_agent` system prompt. Runtime context includes a `capabilities` attribute for each bound knowledge base so the agent can internally route between Wiki pages, raw chunk retrieval, and graph evidence without probing unavailable surfaces.

Chat history is relational and authoritative. The conversation repository stores sessions with tenant/user ownership plus `agent_config`, and messages with paired `request_id` values and `is_completed`. Redis is only auxiliary for current stream event replay and temporary web-search knowledge state; history loading and prompt context never read Redis.

For a new `/chat/stream` request, the backend resolves the request Principal, creates or scopes the session, saves the completed user message, and saves an empty incomplete assistant placeholder before any answer event is emitted. The first public stream metadata includes `session_id`, `conversation_id`, `request_id`, `user_message_id`, `assistant_message_id`, and legacy `stream_message_id`. Normal completion updates that assistant placeholder once with the final answer, sources, chat mode metadata, and `is_completed=true`.

Prompt context and UI history now use separate repository paths. `list_recent_messages` selects the newest bounded window for prompt construction and then returns chronological order. `list_messages_before_time` implements cursor pagination for the UI with a default page size of 20, descending SQL selection, ascending service sort, user-before-assistant tie-breakers, and `hasMoreHistory`.

When `CHAT_RAG_PIPELINE_ENABLED=true`, quick-answer chat uses the online Chat/RAG plugin pipeline in `backend/app/services/chat_pipeline/` instead of the raw helper in `main.py`. The first quick-RAG stage list is:

1. `emit_conversation`
2. `load_history`
3. `memory_retrieval`
4. `query_understand`
5. `retrieve`
6. `recall_parent_context`
7. `emit_sources`
8. `emit_reasoning`
9. `emit_agent_trace`
10. `into_prompt`
11. `chat_completion_stream`
12. `persist_assistant_message`
13. `memory_storage`
14. `done`

Stages share a typed context with immutable request data, explicit mutable state, and runtime handles for `RAGService`, conversation service, memory service, EventBus, stream identity, and stop signal. Stage progress is recorded on the context, and public stage events are converted to the existing stored SSE shape by `main.py`.

Fallback and cancellation are handled at pipeline boundaries. Empty retrieval still emits compatible `sources` and reasoning metadata before answer generation delegates to the existing `RAGService.stream_answer` behavior. Rerank degradation decisions remain owned by `RAGService.hybrid_retrieve_hits` and are copied into pipeline retrieval debug state. If the stop signal is set before a stage or during token streaming, the executor emits a compatible `stop` event and terminal `[DONE]`; the runtime completes the existing assistant placeholder with the exact accumulated partial answer and stopped metadata.

Refresh recovery uses StreamManager as a transient event log. Every public event is appended before SSE delivery, and replay polls storage every 100ms from the requested offset until complete, stop, or terminal error. Redis mode stores events under `stream:events:{sessionId}:{messageId}` with a configurable TTL that defaults to 24 hours; memory mode is development-only for replay across refresh/restart/multi-replica scenarios.

Stop is distributed through StreamManager rather than direct HTTP-handler cancellation. The stop endpoint validates the scoped assistant row and appends a `stop` event. The active SSE loop and an independent 300ms stop watcher both observe that event and set the runtime stop signal. Stopped partial answers are completed in the message table but are not submitted to feedback or corrective knowledge indexing.

The retrieval-only subset is:

1. `query_understand`
2. `retrieve`
3. `recall_parent_context`
4. `filter_top_k`

This subset returns selected hits, source-ready metadata, and retrieval debug state without calling the chat model or writing assistant/memory side effects. Current production chat, search, evaluation, agent, and Wiki callers that are not feature-flagged still call `RAGService` or their specialized runtimes directly; those direct calls remain intentional until parity tests cover each migration.

## Knowledge Graph

KG enrichment is optional. When enabled, extraction tasks and entity mentions are written to PostgreSQL after chunks are persisted. Entity vector search uses PostgreSQL pgvector. Neo4j graph writes are optional; GraphRetriever treats graph data as derived evidence and only returns relations/paths whose `source_chunk_id` resolves inside the requested PostgreSQL scope.

## Evaluation

Evalsets live outside `backend/data/` and are never indexed as knowledge documents. Evaluation runs and results live in PostgreSQL, while generated reports remain filesystem artifacts. Evaluation does not write feedback files, update memory, rebuild vector stores, or modify graph data.

## Reset Required

The backend only starts against an empty/final PostgreSQL schema with the expected generation, pgvector type/dimension, and required indexes. Incompatible PostgreSQL schema, legacy SQLite metadata, old FTS5 state, or Milvus collections require the protected clean-rebuild CLI. Startup and retrieval fail closed instead of mixing generations.

Clean-rebuild writes maintenance/manifest state, drops and initializes the configured PostgreSQL schema, retires legacy SQLite/Milvus artifacts, optionally handles Neo4j/managed files, and clears maintenance only after success.
