## Why

The current production storage model splits authoritative business records across SQLite while dense and keyword retrieval projections live in Milvus, creating two persistence systems to initialize, validate, reset, back up, and scope correctly. Moving to PostgreSQL with pgvector consolidates business metadata, raw evidence chunks, full-text retrieval, vector retrieval, Wiki state, task state, audit rows, and optional KG entity vectors behind one transactional database boundary.

This change intentionally treats existing SQLite and Milvus production data as retired data instead of migrating it. The supported upgrade path is a protected clean rebuild from trusted source files and configuration, which matches the project's existing reset-required posture for incompatible storage schemas.

## What Changes

- **BREAKING**: The backend no longer opens existing SQLite metadata/evaluation databases or Milvus collections as production data after this change.
- **BREAKING**: Operators must stop writers and run the protected clean-rebuild flow to initialize PostgreSQL before normal ingest, retrieval, Wiki, task, memory, audit, KG, or evaluation paths are available.
- Replace SQLite repository implementations with PostgreSQL-backed repositories for knowledge bases, documents/chunks, upload batches, processing tasks/spans, Wiki state, audit/feedback, memory/conversations, KG metadata, images, and evaluation runs.
- Replace `MilvusVectorStore` and Milvus entity-vector storage with PostgreSQL/pgvector-backed vector storage.
- Replace SQLite FTS5 and Milvus BM25 keyword retrieval with PostgreSQL text search plus trigram/exact-term retrieval, while preserving the existing `KeywordSearch` and hybrid retrieval contracts where possible.
- Use `DATABASE_URL` as the production persistence entry point and add PostgreSQL extension/schema initialization for `vector` and `pg_trgm`.
- Update clean-rebuild, storage inspection, health/startup checks, configuration, tests, and documentation to describe PostgreSQL as the single active production store.
- Remove active `pymilvus` usage and Milvus environment requirements after equivalent PostgreSQL retrieval behavior is available.

## Capabilities

### New Capabilities

- `postgres-production-storage`: PostgreSQL becomes the single authoritative production store for knowledge, document, Wiki, processing, memory, audit, KG, and evaluation records.
- `postgres-pgvector-retrieval`: Dense vector, keyword, and hybrid retrieval use PostgreSQL pgvector plus PostgreSQL text/trigram indexes instead of Milvus and SQLite FTS5.
- `retire-sqlite-milvus-data`: Existing SQLite databases and Milvus collections are treated as retired production data and can only be handled through a protected clean-rebuild or explicit external backup/export workflow.

### Modified Capabilities

- None. No main OpenSpec capabilities are currently present under `openspec/specs/`; this change introduces the storage and retrieval contracts as new capabilities.

## Impact

- Backend storage modules: `backend/app/services/storage/storage_schema.py`, `backend/app/services/storage/storage_reset.py`, and `backend/app/scripts/rebuild_knowledge_storage.py`.
- Backend repositories: document, image, upload batch, processing task/span, knowledge base, audit, Wiki, KG, memory, conversation, and evaluation repositories.
- Retrieval modules: `backend/app/services/retrieval/vector_store.py`, `keyword_search.py`, `rag_service.py`, `rag_config.py`, and retrieval tests.
- KG vector module: `backend/app/services/kg/entity_vector_store.py`.
- Startup wiring and configuration in `backend/app/main.py`, `.env` examples, and `backend/requirements.txt`.
- Documentation in `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `docs/API.md`, and relevant design docs.
- Deployment: local and container environments must provide PostgreSQL 16 with pgvector support, such as `pgvector/pgvector:pg16`.
