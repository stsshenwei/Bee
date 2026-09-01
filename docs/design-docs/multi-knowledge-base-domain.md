# Multi-Knowledge-Base Domain

## Domain Boundaries

- `workspace`: stable top-level ownership container.
- `knowledge_base`: lifecycle, type, indexing strategy, provider references, aggregate state, and archive status.
- `KnowledgeBaseScope`: immutable request scope; it is passed explicitly and is not stored in global service state.
- `document` and `document_chunk`: authoritative PostgreSQL evidence rows, each owned by one workspace and one KB.
- `document_chunk_embedding`: pgvector projection of indexable scoped chunks.
- Wiki, KG, processing, audit, memory, and evaluation rows use the same PostgreSQL schema and ownership boundaries.

## Compatibility And Isolation

Legacy requests without KB selection resolve to the configured default KB and never mean all KBs. Explicit KBs must exist, be active, and belong to one workspace. Archived KBs reject upload and retrieval.

Dense pgvector retrieval, PostgreSQL keyword retrieval, parent/child hydration, GraphRetriever, Agent tools, evaluation, and citation verification all use the same scope. Cross-KB child/parent, graph relation, document id, or citation references are rejected even if the raw id exists.

## Lifecycle

The application supports create, list, detail, update, archive, and restore. Archive is logical deletion: uploads and retrieval are blocked, but PostgreSQL rows, source files, vectors, Wiki state, KG rows, and optional graph data are retained. Physical purge is a separate future change.

## Provider Configuration

Knowledge bases store requested and effective provider configuration. Requested values record user intent; effective values record the currently wired parser, embedding, reranker, vector store, and enrichment providers. Unsupported requested values remain visible in `inactive_overrides`; they are not silently treated as active.

The active vector provider is `postgres_pgvector`; keyword search is PostgreSQL text/trigram/exact search. The first phase does not create per-KB databases or vector collections.

## Rebuild And Compatibility

This domain does not migrate old knowledge data. Empty/final PostgreSQL storage can start normally. Legacy SQLite metadata, old Milvus collections, missing workspace/KB ownership columns, incompatible schema generation, or pgvector dimension/type mismatch return `reset_required` and fail closed.

Clean-rebuild is dry-run by default. Execution requires stopped services and the exact confirmation phrase. The coordinator writes maintenance/manifest state, resets and initializes PostgreSQL, retires legacy SQLite/Milvus artifacts, optionally handles Neo4j and managed files, and removes maintenance only after full success.

Backups must represent one coherent application/storage generation. Restoring partial old SQLite/Milvus data into the final PostgreSQL schema is unsupported.

## Current Limits

- The default workspace remains the compatibility workspace for old clients.
- Archive is not physical purge.
- User/member/RBAC ownership is out of scope for this phase.
- External source sync and provider instance registry are out of scope.
- Old data import is out of scope; rebuild requires trusted source documents to be re-ingested.
