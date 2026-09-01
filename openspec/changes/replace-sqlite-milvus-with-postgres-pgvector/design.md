## Context

The backend currently uses SQLite as the authoritative metadata database for workspace, knowledge-base, document, chunk, processing, Wiki, audit, memory, KG, image, and evaluation records. It uses SQLite FTS5 as a keyword fallback and Milvus for dense chunk vectors, optional BM25/sparse retrieval, and KG entity vectors.

That split has become expensive for a product-style RAG system. Every schema change must validate SQLite and Milvus separately, scope filters must be correct in both stores, clean-rebuild must coordinate several independent persistence targets, and backup/restore cannot be described as one transactional unit. The project already treats incompatible storage as `reset_required` and supports protected clean rebuilds, so this change uses that boundary deliberately instead of attempting an in-place production migration.

PostgreSQL with pgvector becomes the active persistence platform. Business tables, raw evidence chunks, derived retrieval columns, vector embeddings, Wiki state, processing queues, memories, audit rows, KG metadata, and evaluation rows will live in PostgreSQL. Milvus and SQLite production data will be retired.

## Goals / Non-Goals

**Goals:**

- Use PostgreSQL 16 with pgvector as the only active production database for knowledge data and retrieval indexes.
- Preserve existing API behavior, SSE framing, KB scoping, citation verification, Wiki workflows, processing task semantics, memory semantics, and evaluation semantics unless a storage-specific behavior must change.
- Replace Milvus chunk vectors and KG entity vectors with PostgreSQL pgvector-backed tables and indexes.
- Replace SQLite FTS5 and Milvus BM25 usage with PostgreSQL keyword retrieval built from text search, trigram, and exact-term matching.
- Keep raw document chunks as authoritative evidence and keep retrieval vectors/text indexes as rebuildable projections.
- Make clean-rebuild the only supported upgrade path from SQLite/Milvus production state to PostgreSQL production state.
- Remove active `pymilvus` dependency and Milvus runtime configuration after PostgreSQL retrieval parity is complete.

**Non-Goals:**

- No in-place migration of existing SQLite rows, FTS5 rows, Milvus collections, Chroma artifacts, or Milvus entity-vector collections.
- No dual-write compatibility period where SQLite/Milvus and PostgreSQL are both production writers.
- No new user-facing APIs solely for data migration.
- No change to OpenAI model usage, parsing engines, chunking strategy, reranker behavior, Wiki generation semantics, or frontend route contracts beyond storage-related status/config copy.
- No replacement of optional Neo4j graph storage in this change.
- No guarantee that PostgreSQL full-text search alone provides production-grade Chinese segmentation; this change provides bounded trigram/exact fallback and leaves advanced Chinese parser extensions as future work.

## Decisions

### 1. PostgreSQL is the single production persistence boundary

All SQLite-backed repository responsibilities move to PostgreSQL. The application should construct repositories from `DATABASE_URL` and a shared connection pool instead of from `METADATA_DB_PATH` or `EVAL_DB_PATH`.

Target repositories include:

- Knowledge-base domain: workspace, knowledge_base, provider/effective config, aggregate counts.
- Document evidence: document, document_chunk, document images, enrichment tasks, upload batches.
- Processing runtime: document_processing_task, document_processing_dead_letter, knowledge_processing_spans, agent runtime spans.
- Wiki: wiki_page, wiki_folder, wiki_page_issue, wiki_page_proposal, wiki_page_source_ref, wiki_document_contribution, wiki_ingest_pending, wiki_log_entry.
- Retrieval support: chunk embeddings, search vectors, trigram/exact retrieval fields.
- Audit/memory/evaluation/KG: query_log, answer_feedback, conversations, memories, eval runs/results, entity mentions, graph community summaries.

Alternative considered: keep some low-risk tables in SQLite and only move chunks/vectors to PostgreSQL. Rejected because it keeps the same multi-store reset, backup, and scoping failure modes this change is meant to remove.

### 2. Final schema only; no compatibility migrations from SQLite/Milvus

The PostgreSQL schema should initialize as a final version on an empty database. Application startup validates that the configured PostgreSQL database has the expected storage version and required extensions. If old SQLite files or Milvus collections are present, they are ignored by normal production code and reported only as retired artifacts or clean-rebuild inputs for deletion/backup planning.

Alternative considered: build importers from SQLite tables and Milvus collections. Rejected because current production data includes generated Wiki pages, task histories, spans, feedback, vectors, and old schema states whose correctness is hard to prove. Trusted source files plus re-ingest are safer for the first PostgreSQL cutover.

### 3. pgvector stores chunk and entity embeddings

`PostgresVectorStore` replaces `MilvusVectorStore` while preserving the service-facing methods that `RAGService` uses:

```text
upsert_chunks(chunks)
replace_document_chunks(doc_id, chunks, scope)
query_dense(query, top_k, scope)
query(query, top_k, scope)
delete_document(doc_id, scope)
delete_knowledge_base(scope)
reset_collection()
count()
```

Chunk embeddings are stored in PostgreSQL rows keyed by `(workspace_id, knowledge_base_id, doc_id, chunk_id)` and filtered before ranking. The dense vector table may either be separate from `document_chunk` or an extension of it; the implementation should choose based on simpler transactional replacement and clearer index maintenance. In both shapes, the authoritative content remains in `document_chunk`.

`PostgresEntityVectorStore` replaces `MilvusEntityVectorStore` for KG entity similarity. It uses the same scope keys as entity metadata and must not return entities outside the requested workspace/KB scope.

Alternative considered: keep Milvus only for KG entity vectors while moving document vectors. Rejected because it leaves Milvus operationally required and weakens the single-store goal.

### 4. Embedding dimension is explicit and validated at startup

The PostgreSQL vector column type must match the configured embedding model. The implementation must choose one of these supported configurations:

- `vector(1536)` for 1536-dimensional embeddings.
- `halfvec(3072)` or another explicitly supported pgvector type for 3072-dimensional embeddings.

Startup must fail closed or report reset-required when the configured embedding dimension is incompatible with the existing PostgreSQL schema. Silent truncation, padding, or writing vectors into the wrong dimension is not allowed.

Alternative considered: use unbounded JSON/array storage for embeddings. Rejected because it loses pgvector index support and makes vector search slower and less predictable.

### 5. PostgreSQL keyword retrieval replaces SQLite FTS5 and Milvus BM25

The retrieval stack should keep a `KeywordSearch` boundary, but its implementation becomes PostgreSQL-backed. Keyword recall should combine:

- PostgreSQL `tsvector` search for tokenized terms and ranking.
- `pg_trgm` similarity or indexed substring matching for Chinese fragments, model names, identifiers, file names, commands, and short technical terms.
- Exact `ILIKE`/normalized token matching where it improves recall for versions, error codes, and configuration keys.

The hybrid retriever continues to fan out dense and keyword searches, fuse by weighted RRF, dedupe by chunk id, optionally rerank, and recall parents from authoritative chunk rows.

Alternative considered: rely only on pgvector dense search. Rejected because enterprise product documents often require exact model names, parameter names, error codes, prices, and command snippets that dense retrieval can miss.

Alternative considered: rely only on PostgreSQL full-text search. Rejected because Chinese and mixed technical text need trigram/exact fallback unless a stronger language parser is explicitly added later.

### 6. Existing clean-rebuild becomes PostgreSQL initialization and retirement workflow

The rebuild CLI remains protected by dry-run, service-stopped preconditions, confirmation phrase, optional backup directory, maintenance marker, and reset manifest. Its target list changes:

```text
old:
  SQLite metadata/eval DBs
  Milvus rag_chunk_vectors and kg_entity_vectors
  optional Neo4j
  generated state/report/source artifacts

new:
  PostgreSQL schema/tables/indexes/extensions
  retired SQLite/Milvus artifacts listed for deletion/backup when configured
  optional Neo4j reset still handled separately
  generated state/report/source artifacts
```

Normal startup must not drop or alter production PostgreSQL data to recover from a mismatch. The CLI is the only place where destructive reset is allowed.

Alternative considered: auto-create missing PostgreSQL columns on startup. Rejected because the project has intentionally moved toward final-schema-only storage and fail-closed behavior.

### 7. Configuration uses PostgreSQL names and keeps deprecated names visibly inactive

Production config adds:

```text
DATABASE_URL
POSTGRES_SCHEMA
POSTGRES_POOL_MIN_SIZE
POSTGRES_POOL_MAX_SIZE
PGVECTOR_INDEX_TYPE
PGVECTOR_HNSW_M
PGVECTOR_HNSW_EF_CONSTRUCTION
PGVECTOR_SEARCH_EF
POSTGRES_KEYWORD_LANGUAGE
POSTGRES_TRIGRAM_ENABLED
```

The old `METADATA_DB_PATH`, `EVAL_DB_PATH`, `MILVUS_URI`, `MILVUS_TOKEN`, `MILVUS_COLLECTION`, `MILVUS_BM25_ENABLED`, and `KG_MILVUS_*` settings should be removed from active production wiring. If they remain in examples for transition notes, they must be marked deprecated and ignored by production startup.

Alternative considered: map Milvus config names to PostgreSQL behavior for compatibility. Rejected because misleading names make operations more dangerous during a breaking storage cutover.

### 8. Tests should prefer fake PostgreSQL adapters plus bounded integration coverage

Most repository and retrieval tests should run against controlled PostgreSQL-compatible fakes or a disposable PostgreSQL test database when available. Storage SQL and pgvector behavior need integration coverage because SQLite in-memory tests cannot validate PostgreSQL locking, JSONB, text search, trigram, vector dimension, or HNSW/IVFFlat behavior.

Alternative considered: keep existing SQLite tests as the main suite and unit-test conversion helpers. Rejected because the highest-risk behavior is in PostgreSQL query semantics and transactional claims.

## Risks / Trade-offs

- [PostgreSQL service becomes mandatory] -> Provide clear Docker/local setup with `pgvector/pgvector:pg16`, health checks, and fail-fast startup messages.
- [No production data migration surprises operators] -> Mark the change as breaking, make dry-run list retired SQLite/Milvus artifacts, and document that trusted source files must be re-ingested.
- [Chinese keyword quality regresses versus tuned search] -> Combine text search with trigram/exact matching and preserve dense recall; add regression tests for Chinese product names, model identifiers, command snippets, and version strings.
- [Embedding dimension mismatch blocks startup] -> Store dimension and vector type in schema metadata and validate before ingest or query.
- [pgvector performance varies by corpus size] -> Add configurable HNSW/IVFFlat settings, measure top-k retrieval on representative evalsets, and preserve reranker/fusion controls.
- [Large JSONB/chunk rows may bloat indexes] -> Keep authoritative chunk content in document tables and index only bounded generated search text/vector projections.
- [Task workers contend on PostgreSQL rows] -> Use row-level locking with `FOR UPDATE SKIP LOCKED`, short transactions, and no LLM calls inside transactions.
- [Clean rebuild can destroy the wrong database] -> Require explicit `DATABASE_URL`, dry-run, confirmation phrase, service-stopped marker, and reset manifest before destructive actions.
- [Repository rewrite is broad] -> Implement by boundaries, keep public service method shapes stable, and migrate tests module-by-module.

## Migration Plan

1. Add PostgreSQL dependencies, connection pooling, extension checks, and schema metadata.
2. Define final PostgreSQL DDL and indexes for all current SQLite tables plus vector/search projection fields.
3. Implement PostgreSQL repository replacements while preserving existing repository method contracts.
4. Implement `PostgresVectorStore`, `PostgresEntityVectorStore`, and PostgreSQL-backed `KeywordSearch`.
5. Wire `build_rag_service()` and related startup paths from `DATABASE_URL`.
6. Update clean-rebuild to initialize PostgreSQL final schema and list old SQLite/Milvus production artifacts as retired data.
7. Remove active Milvus wiring and dependency usage once retrieval tests pass.
8. Update docs and env examples.
9. Run backend unit tests, PostgreSQL integration tests, ingest smoke tests, retrieval smoke tests, Wiki generation smoke tests, and evaluation replay.
10. Deploy by stopping API/workers, backing up old data externally if desired, running clean-rebuild, starting PostgreSQL-backed API/workers, and re-ingesting trusted source files.

Rollback is backup-based. Operators may restore the pre-change application and its complete SQLite/Milvus backup set, or restore a PostgreSQL backup taken after cutover. The system must not support pointing old application code at the new PostgreSQL schema or new application code at old SQLite/Milvus production data.

## Open Questions

- Which embedding model/dimension should be the default PostgreSQL schema target: 1536-dimensional `vector` or 3072-dimensional `halfvec`?
- Should the first implementation use HNSW only, or allow IVFFlat as a documented option for smaller local deployments?
- Should evaluation rows share the main PostgreSQL database/schema, or use a separate schema/database controlled by `EVALUATION_DATABASE_URL`?
- Do we need an optional one-time export of Wiki pages/feedback before retirement, or is re-ingest from source files sufficient for the first cutover?
- Should advanced Chinese search extensions be considered in a follow-up after text/trigram regression results are measured?
