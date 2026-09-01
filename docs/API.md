# API

All knowledge APIs are scoped by workspace and knowledge base. Omitting `knowledge_base_id` or `knowledge_base_ids` uses the configured default KB for backward compatibility; it never means "all knowledge bases".

## Health

- `GET /health`: returns `ok`, PostgreSQL storage diagnostics, pgvector type/dimension, `reset_required`, and observability status.
- `GET /observability/status`: returns Langfuse/observability configuration and failure state.

## Knowledge Bases

- `GET /workspaces/default`
- `GET /knowledge-bases?workspace_id=...&include_archived=false`
- `POST /knowledge-bases`
- `GET /knowledge-bases/{knowledge_base_id}`
- `PATCH /knowledge-bases/{knowledge_base_id}`
- `DELETE /knowledge-bases/{knowledge_base_id}`
- `POST /knowledge-bases/{knowledge_base_id}/restore`

Archived KBs cannot accept uploads or retrieval, but rows and source files are retained.

## Documents And Uploads

These endpoints accept or resolve one active KB scope:

- `POST /documents/upload`
- `POST /documents/parse`
- `GET /documents`
- `GET /documents/content`
- `GET /documents/file`
- `POST /rag/documents/upload`
- `POST /rag/documents/{doc_id}/ingest`
- `DELETE /rag/documents/{doc_id}`
- `POST /documents/{doc_id}/enrichment/retry`

Staged uploads live under `/knowledge-bases/{knowledge_base_id}/upload-batches`. Draft/uploading batches only persist managed source files and task rows. Parsing, chunking, embeddings, keyword indexing, KG, Wiki, and enrichment begin only after confirm.

Document, parent, child, table, OCR, image-derived, keyword, and vector rows are stored in PostgreSQL and must match the requested workspace/KB/document scope. Cross-scope document or chunk ids are rejected.

## Query And Chat

`POST /rag/query` and `POST /chat/stream` support:

```json
{
  "question": "Question text",
  "knowledge_base_id": "kb-a",
  "knowledge_base_ids": ["kb-a", "kb-b"],
  "document_ids": ["doc-1"]
}
```

The backend validates every selected KB is active and belongs to one workspace. Dense pgvector retrieval, PostgreSQL keyword retrieval, hydration, parent recall, graph evidence, and citation verification all apply the same scope before returning evidence.

`/rag/query` returns `answer`, `citations`, `used_chunks`, `used_entities`, `graph_paths`, `confidence`, and `debug_info`. Agentic mode may also return `agent_trace`, `tool_calls`, and `evidence_summary`. `/chat/stream` preserves the existing SSE contract while optionally emitting agent trace/tool events before answer tokens.

## Feedback And Audit

Feedback must target one active KB. Multi-KB answers require the client to provide a single correction target. Query logs and answer feedback are PostgreSQL audit records; generated feedback markdown may also be written into `backend/data/feedback/` and ingested as normal knowledge content.

## Wiki

Wiki routes are scoped under one active KB with `indexing_strategy.wiki_enabled=true`:

- `GET|POST /knowledge-bases/{kb}/wiki/pages`
- `GET|PATCH|DELETE /knowledge-bases/{kb}/wiki/pages/{slug}`
- `GET|POST /knowledge-bases/{kb}/wiki/folders`
- `PATCH|DELETE /knowledge-bases/{kb}/wiki/folders/{folder_id}`
- `GET /knowledge-bases/{kb}/wiki/graph`
- `POST /knowledge-bases/{kb}/wiki/source-doc`
- `GET /knowledge-bases/{kb}/wiki/generation-tasks`
- `POST /knowledge-bases/{kb}/wiki/generate`
- `GET /knowledge-bases/{kb}/wiki/overview`
- `GET /knowledge-bases/{kb}/wiki/logs`
- `GET /knowledge-bases/{kb}/wiki/processing-tasks`
- `POST /knowledge-bases/{kb}/wiki/processing-tasks/{task_id}/retry`
- `POST /knowledge-bases/{kb}/wiki/processing-tasks/{task_id}/cancel`
- `GET|POST /knowledge-bases/{kb}/wiki/issues`
- `GET|PATCH /knowledge-bases/{kb}/wiki/issues/{issue_id}`
- `GET|POST /knowledge-bases/{kb}/wiki/proposals`
- `GET /knowledge-bases/{kb}/wiki/proposals/{proposal_id}`
- `POST /knowledge-bases/{kb}/wiki/proposals/{proposal_id}/apply`
- `POST /knowledge-bases/{kb}/wiki/proposals/{proposal_id}/reject`

## Reset Required

Destructive clean-rebuild has no HTTP API. Old SQLite schema, legacy Milvus collections, maintenance markers, incompatible PostgreSQL schema, or pgvector dimension/type mismatches fail closed. Operators must stop services and run `python -m app.scripts.rebuild_knowledge_storage`; normal HTTP requests cannot bypass, confirm, or trigger global reset.
