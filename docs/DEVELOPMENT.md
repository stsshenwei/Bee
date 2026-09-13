# Development

This guide covers local development for the Next.js + FastAPI RAG application. The active backend store is PostgreSQL 16 with pgvector. SQLite, Chroma, and Milvus artifacts in the workspace are legacy data and are not used by normal startup.

## Prerequisites

- Python 3.11 compatible environment
- Node.js compatible with Next.js 15
- Docker or another local PostgreSQL 16 + pgvector installation
- Redis 6+ for the optional Celery-backed async runtime
- OpenAI-compatible API credentials

## Local PostgreSQL/pgvector

Start a disposable local database:

```powershell
docker run --name rag-postgres `
  -e POSTGRES_USER=rag `
  -e POSTGRES_PASSWORD=rag `
  -e POSTGRES_DB=rag `
  -p 5432:5432 `
  -d pgvector/pgvector:pg16
```

Use these backend env values:

```env
DATABASE_URL=postgresql://rag:rag@localhost:5432/rag
POSTGRES_SCHEMA=public
POSTGRES_POOL_MIN_SIZE=1
POSTGRES_POOL_MAX_SIZE=10
PGVECTOR_TYPE=vector
PGVECTOR_INDEX_TYPE=hnsw
PGVECTOR_HNSW_M=16
PGVECTOR_HNSW_EF_CONSTRUCTION=200
PGVECTOR_SEARCH_EF=64
POSTGRES_KEYWORD_LANGUAGE=simple
POSTGRES_TRIGRAM_ENABLED=true
```

## Backend Setup

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Required env:

- `OPENAI_API_KEY`
- `DATABASE_URL`

Common optional env:

- OpenAI: `OPENAI_BASE_URL`, `OPENAI_CHAT_MODEL`, `OPENAI_EMBEDDING_MODEL`, `EMBEDDING_DIM`
- PostgreSQL: `POSTGRES_SCHEMA`, `POSTGRES_POOL_MIN_SIZE`, `POSTGRES_POOL_MAX_SIZE`
- pgvector: `PGVECTOR_TYPE`, `PGVECTOR_INDEX_TYPE`, `PGVECTOR_HNSW_M`, `PGVECTOR_HNSW_EF_CONSTRUCTION`, `PGVECTOR_SEARCH_EF`
- keyword: `POSTGRES_KEYWORD_LANGUAGE`, `POSTGRES_TRIGRAM_ENABLED`
- retrieval: `TOP_K`, `DENSE_RECALL_TOP_N`, `BM25_RECALL_TOP_N` (legacy env name for keyword recall), `FUSION_TOP_K`, `RRF_K`, `RRF_VECTOR_WEIGHT`, `RRF_KEYWORD_WEIGHT`, `RETRIEVAL_DEBUG_ENABLED`
- parsing/processing: `RAG_DATA_DIR`, `VECTOR_STORE_DIR`, `PARSER_ENGINE`, `PROCESSING_WORKER_ENABLED`, `WIKI_INGEST_ENABLED`, `PROCESSING_TRACE_DIR`
- async runtime: `ASYNC_RUNTIME_ENABLED`, `ASYNC_RUNTIME_MODE`, `REDIS_URL`, `ASYNC_RUNTIME_*_QUEUE`, `ASYNC_RUNTIME_*_CONCURRENCY`
- chat stream replay: `STREAM_MANAGER_TYPE` (`memory` or `redis`), `STREAM_EVENT_TTL_SECONDS`, `STREAM_MANAGER_REDIS_URL` or `REDIS_URL`
- optional features: `RERANKER_ENABLED`, `OCR_ENABLED`, `KG_EXTRACTION_ENABLED`, `KG_ENTITY_VECTOR_ENABLED`, `KG_GRAPH_ENABLED`, `GRAPH_RETRIEVER_ENABLED`, `AGENTIC_RETRIEVAL_ENABLED`, `AGENT_RUNTIME_ENABLED`, `AGENT_RUNTIME_RAG_WIKI_MODE_ENABLED`
- reports/state: `EVAL_DATASET_DIR`, `EVAL_REPORT_DIR`, `STORAGE_RESET_STATE_DIR`, `STORAGE_RUNTIME_LOCK`
- MCP server: `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT`, `MCP_SERVER_AUTH_TOKEN`, `MCP_TOOL_TIMEOUT_SECONDS`, `MCP_MAX_OUTPUT_CHARS`, `MCP_MAX_EVENTS`, `MCP_MAX_UPLOAD_BYTES`

Plugin management uses PostgreSQL table `plugin_setting` for workspace-scoped UI settings. Environment values remain defaults and guardrails for the Plugins workspace, including `AGENT_RUNTIME_WEB_SEARCH_ENABLED`, `AGENT_RUNTIME_WEB_SEARCH_URL`, `AGENT_RUNTIME_WEB_FETCH_ENABLED`, `AGENT_RUNTIME_WEB_FETCH_ALLOWED_DOMAINS`, `AGENT_RUNTIME_DATA_ANALYSIS_ENABLED`, `AGENT_RUNTIME_DATABASE_QUERY_ENABLED`, `AGENT_RUNTIME_DATABASE_ALLOWED_SOURCES`, and `AGENT_RUNTIME_SKILLS_ENABLED`.

Do not set old production storage variables such as `METADATA_DB_PATH`, `MEMORY_DB_PATH`, `EVAL_DB_PATH`, `MILVUS_URI`, `MILVUS_TOKEN`, `MILVUS_COLLECTION`, `MILVUS_BM25_ENABLED`, or `KG_MILVUS_*`; they are retired from production wiring.

## Run Backend

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

## Optional MCP Server

The backend can also run a curated MCP server for external AI clients. See [MCP.md](MCP.md) for the tool inventory and client examples.

Local stdio:

```powershell
cd backend
python -m app.mcp --transport stdio
```

Authenticated Streamable HTTP:

```powershell
cd backend
$env:MCP_SERVER_AUTH_TOKEN="replace-with-a-strong-secret"
python -m app.mcp --transport streamable-http --host 127.0.0.1 --port 8765
```

Useful checks:

```powershell
curl http://localhost:8000/health
curl -X POST http://localhost:8000/ingest
curl -X POST http://localhost:8000/documents/parse -H "Content-Type: application/json" -d "{\"source\":\"example.md\"}"
curl http://localhost:8000/plugins
```

`/health` should report `storage.database=postgres`, the configured schema, the vector store class, pgvector type/dimension, `reset_required`, and the current `async_runtime` mode.

## Optional Redis/Celery Async Runtime

The default local mode still uses PostgreSQL task rows and the in-process worker. To route upload/Wiki processing through Redis/Celery, start Redis and set:

```env
ASYNC_RUNTIME_ENABLED=true
ASYNC_RUNTIME_MODE=celery
REDIS_URL=redis://localhost:6379/0
PROCESSING_WORKER_ENABLED=true
ASYNC_RUNTIME_LOCAL_WORKER_ENABLED=false
```

Start the API in one terminal, then start worker pools from `backend/` in separate terminals. On Windows local development, prefer `--pool=solo` or `--pool=threads`; Linux production can use Celery's normal prefork pool.

```powershell
.\.venv\Scripts\celery.exe -A app.workers.celery_app:celery_app worker -Q core --pool=solo --concurrency=1 --loglevel=info
.\.venv\Scripts\celery.exe -A app.workers.celery_app:celery_app worker -Q wiki --pool=solo --concurrency=1 --loglevel=info
.\.venv\Scripts\celery.exe -A app.workers.celery_app:celery_app worker -Q postprocess,enrichment,maintenance,shared --pool=solo --concurrency=1 --loglevel=info
```

The configured Weknora-style pools are Core, PostProcess, Enrichment, Maintenance, Shared, and Wiki. Redis is the broker; PostgreSQL remains the source of truth for task status, attempts, traces, cancellation, and dead letters.

For chat refresh recovery and distributed stop propagation in multi-replica deployments, also set `STREAM_MANAGER_TYPE=redis`. When unset, the memory StreamManager works for local single-process streaming but cannot replay events after restart or guarantee cross-replica stop observation.

## Frontend Setup

```powershell
cd frontend
npm install
Copy-Item .env.local.example .env.local
```

Required frontend env:

- `NEXT_PUBLIC_API_BASE`

Run:

```powershell
cd frontend
npm run dev
```

Other scripts:

```powershell
npm run build
npm run start
npm run lint
```

## Recommended Workflow

1. Start PostgreSQL/pgvector.
2. Start the backend and verify `/health`.
3. Start the frontend at `http://localhost:3000`.
4. Upload or ingest one document into a target KB.
5. Ask one chat question and verify streamed answer, sources, document preview, and feedback submission.

## Validation Commands

Focused backend checks used during the PostgreSQL/pgvector migration:

```powershell
cd backend
python -m pytest tests/test_runtime_config.py tests/test_postgres_vector_store.py tests/test_keyword_search.py tests/test_hybrid_retrieval.py tests/test_postgres_document_repository.py tests/test_storage_schema_reset.py
python -m pytest tests/test_postgres_domain_repositories.py tests/test_postgres_wiki_repository.py tests/test_wiki_repository_service.py
python -m pytest tests/test_memory_repositories.py tests/test_knowledge_audit_repository.py tests/test_kg_models_repository.py tests/test_enterprise_evaluation_suite.py
python -m pytest tests/test_mcp_tools.py
python -m pytest tests/test_plugin_management.py tests/test_rag_api_routes.py tests/test_runtime_config.py
python -m pytest tests/test_marketplace_service.py tests/test_marketplace_routes.py
```

Frontend:

```powershell
cd frontend
node --test app/lib/api.test.mjs app/lib/marketplace-api.test.mjs app/lib/responsive-css.test.mjs
npm run build
```

## Clean Rebuild

Use clean-rebuild whenever startup raises `StorageResetRequired`, including incompatible PostgreSQL schema generation, pgvector dimension/type mismatch, missing required indexes, or legacy storage generations.

Stop API and workers first. Dry-run by default:

```powershell
cd backend
python -m app.scripts.rebuild_knowledge_storage --environment dev
```

Execute only with the exact confirmation phrase:

```powershell
python -m app.scripts.rebuild_knowledge_storage `
  --execute `
  --environment dev `
  --confirm RESET_ALL_APPLICATION_DATA:dev `
  --backup-dir D:\backup\bee-before-reset
```

The coordinator writes maintenance/manifest state, drops and initializes the configured PostgreSQL schema, retires legacy SQLite/Milvus artifacts, optionally handles Neo4j and managed source files, and clears maintenance only after success. Normal HTTP APIs cannot trigger or confirm global reset.

## Troubleshooting

- Missing `DATABASE_URL`: backend startup fails fast because PostgreSQL is mandatory.
- `psycopg` import error: rerun `pip install -r requirements.txt`; the project requires `psycopg[binary,pool]`.
- `/health` shows `reset_required=true`: stop services and run clean-rebuild; do not patch tables manually.
- pgvector dimension mismatch: verify `EMBEDDING_DIM`, embedding model, and `PGVECTOR_TYPE`; then clean-rebuild if storage was initialized with another dimension.
- Keyword misses exact model names or error codes: enable `RETRIEVAL_DEBUG_ENABLED=true` and inspect dense/keyword recall, query understanding, fusion, rerank, and parent recall debug metadata.
- Worker appears idle: check PostgreSQL processing task rows, `PROCESSING_WORKER_ENABLED`, `WIKI_INGEST_ENABLED`, and backend logs with `X-Trace-ID`.
- Celery worker appears idle: verify `ASYNC_RUNTIME_ENABLED=true`, `ASYNC_RUNTIME_MODE=celery`, `REDIS_URL`, worker `-Q` queue names, and `/health.async_runtime.routes`.

## Common Ownership Boundaries

- Keep FastAPI route handlers thin; put business logic in services.
- PostgreSQL repositories own persistence. Do not add new SQLite/Milvus production paths.
- `PostgresVectorStore` is the vector boundary for document chunks; `PostgresEntityVectorStore` is the vector boundary for KG entities.
- `KnowledgeBaseScope` must be enforced before ranking, hydration, graph evidence, citation verification, and final context assembly.
- Generated feedback markdown joins the retrievable corpus after upsert, but audit rows remain PostgreSQL records.
