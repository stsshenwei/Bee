## 1. PostgreSQL Foundation

- [x] 1.1 Add PostgreSQL runtime dependencies and remove active Milvus dependency expectations from backend requirements.
- [x] 1.2 Add a PostgreSQL connection/pool module that reads `DATABASE_URL` and exposes transaction helpers for repositories.
- [x] 1.3 Define PostgreSQL storage schema metadata, expected version, and extension checks for `vector` and `pg_trgm`.
- [x] 1.4 Create final PostgreSQL DDL for current metadata, document, chunk, image, upload, processing, Wiki, audit, memory, KG, and evaluation tables.
- [x] 1.5 Add PostgreSQL indexes for workspace/KB scope, foreign keys, task polling, Wiki lookup, audit lookup, text search, trigram search, and vector search.
- [x] 1.6 Add startup inspection that fails closed or reports reset-required when PostgreSQL schema version, extensions, or vector dimension do not match.

## 2. Repository Migration

- [x] 2.1 Port `KnowledgeBaseRepository` and `KnowledgeBaseService` persistence to PostgreSQL while preserving service method contracts.
- [x] 2.2 Port `DocumentRepository` including document/chunk replacement, parent recall, direct-load, enrichment tasks, and scoped chunk lookup.
- [x] 2.3 Port `ImageRepository` and upload batch repository behavior to PostgreSQL.
- [x] 2.4 Port `ProcessingTaskRepository` with PostgreSQL row-level claiming, retry scheduling, cancellation, and dead-letter writes.
- [x] 2.5 Port `ProcessingSpanTracker` and agent runtime span persistence to PostgreSQL.
- [x] 2.6 Port `WikiRepository` and Wiki search/proposal/issue/log/contribution persistence to PostgreSQL.
- [x] 2.7 Port audit, feedback, conversation, memory, KG metadata, and evaluation repositories to PostgreSQL.
- [x] 2.8 Update repository tests to cover PostgreSQL-backed behavior and remove SQLite-only assumptions.

## 3. pgvector Retrieval

- [x] 3.1 Implement `PostgresVectorStore` with chunk upsert, document replacement, dense query, delete, reset, count, and scope filtering.
- [x] 3.2 Validate embedding dimension and vector column type before writing or querying vectors.
- [x] 3.3 Implement `PostgresEntityVectorStore` for KG entity vector upsert and scoped similarity search.
- [x] 3.4 Replace `MilvusVectorStore` construction in backend startup with PostgreSQL vector-store construction.
- [x] 3.5 Update vector-store tests to prove scope filtering, document replacement, deletion, and dimension mismatch failure.

## 4. PostgreSQL Keyword And Hybrid Retrieval

- [x] 4.1 Implement PostgreSQL-backed `KeywordSearch` using `tsvector` ranking plus trigram/exact fallback.
- [x] 4.2 Preserve keyword recall for model numbers, version strings, command flags, configuration keys, error codes, API identifiers, and Chinese fragments.
- [x] 4.3 Update `RAGService.keyword_retrieve_hits` to remove Milvus BM25 branching and use PostgreSQL keyword retrieval.
- [x] 4.4 Preserve hybrid dense/keyword fan-out, RRF fusion, chunk dedupe, optional reranking, parent recall, and debug metadata.
- [x] 4.5 Add regression tests for dense-only, keyword-only, hybrid overlap, selected-document direct load, multi-KB scope, and insufficient-evidence behavior.

## 5. Clean Rebuild And Data Retirement

- [x] 5.1 Refactor storage reset providers so clean-rebuild initializes PostgreSQL final schema instead of SQLite/Milvus production stores.
- [x] 5.2 Update dry-run output to list PostgreSQL initialization targets and retired SQLite/Milvus artifacts without modifying data.
- [x] 5.3 Preserve service-stopped checks, exact confirmation phrase, maintenance marker, optional backup validation, and reset manifest semantics.
- [x] 5.4 Ensure normal startup never imports old SQLite rows or Milvus vectors and never auto-drops PostgreSQL production tables.
- [x] 5.5 Add tests for dry-run, refused unsafe backup paths, confirmed rebuild, incompatible schema failure, and mixed-generation rollback refusal.

## 6. Configuration And Wiring

- [x] 6.1 Add `DATABASE_URL`, PostgreSQL pool, pgvector index, and PostgreSQL keyword settings to env handling and examples.
- [x] 6.2 Remove active use of `METADATA_DB_PATH`, `EVAL_DB_PATH`, `MILVUS_URI`, `MILVUS_TOKEN`, `MILVUS_COLLECTION`, `MILVUS_BM25_ENABLED`, and `KG_MILVUS_*` from production wiring.
- [x] 6.3 Update RAG YAML configuration to use PostgreSQL vector and keyword settings instead of Milvus settings.
- [x] 6.4 Update health/startup diagnostics to report PostgreSQL and pgvector readiness.
- [x] 6.5 Update local/development deployment notes for `pgvector/pgvector:pg16`.

## 7. Documentation

- [x] 7.1 Update `docs/ARCHITECTURE.md` topology, storage layout, retrieval flow, Wiki layer, KG layer, evaluation layer, and reset-required notes.
- [x] 7.2 Update `docs/DEVELOPMENT.md` setup, environment variables, validation commands, clean-rebuild workflow, and troubleshooting.
- [x] 7.3 Update `docs/API.md` storage/reset notes and any debug metadata naming that mentions SQLite, FTS5, Milvus, or BM25.
- [x] 7.4 Update `docs/design-docs/backend-rag-pipeline.md` and multi-KB design docs for PostgreSQL/pgvector retrieval and ownership.

## 8. Validation

- [x] 8.1 Run repository and storage schema tests against PostgreSQL-backed implementations.
- [x] 8.2 Run vector, keyword, hybrid retrieval, KG entity vector, and scoped citation verification tests.
- [x] 8.3 Run Wiki ingest, Wiki workspace API, processing task/span, upload batch, memory, audit, and evaluation tests.
- [ ] 8.4 Run an end-to-end clean-rebuild, re-ingest, `/rag/query`, `/chat/stream`, document preview, Wiki generation, and feedback smoke test on a PostgreSQL database.
- [x] 8.5 Verify backend startup and retrieval work with Milvus absent and `pymilvus` unavailable.
