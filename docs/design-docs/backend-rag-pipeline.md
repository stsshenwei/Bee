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

In Celery mode, Wiki ingest/finalize task rows still live in PostgreSQL and are dispatched to the Wiki queue for hard isolation from document parsing and embedding work.

## Chat Event Streaming

The public `/chat/stream` SSE payloads remain backwards-compatible. Internally, chat events can be appended to a StreamManager by conversation/session id and message id. The current implementation provides an in-memory StreamManager foundation and adds stream metadata to outgoing JSON payloads under `_stream`, which old clients ignore. Stored events use monotonic offsets so future clients can reconnect with a last-seen offset and replay missed events before terminal `[DONE]`.

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

Fallback and cancellation are handled at pipeline boundaries. Empty retrieval still emits compatible `sources` and reasoning metadata before answer generation delegates to the existing `RAGService.stream_answer` behavior. Rerank degradation decisions remain owned by `RAGService.hybrid_retrieve_hits` and are copied into pipeline retrieval debug state. If the stop signal is set before a stage or during token streaming, the executor emits a compatible `stop` event and terminal `[DONE]`; stopped streams do not persist an ambiguous assistant message.

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
