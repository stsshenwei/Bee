# Development

## 自适应文档处理验证

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.test_adaptive_chunker tests.test_builtin_pdf_parser tests.test_parser_engine_registry tests.test_object_storage -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

切片自动模式顺序固定为 `heading -> heuristic -> legacy`。全库重置具有不可逆破坏性，只能在停止 API/worker 后显式执行：

```powershell
.\.venv\Scripts\python.exe -m app.scripts.rebuild_knowledge_storage --environment <环境名> --execute --confirm RESET_ALL_APPLICATION_DATA:<环境名>
```

该命令删除整个旧版应用数据库和所有索引、图及媒体数据；普通启动不会自动清库。

## Scope

This guide covers local development for the current Next.js + FastAPI RAG application.

## Prerequisites

- Python 3.11 compatible environment
- Node.js version compatible with Next.js 15
- OpenAI-compatible API credentials

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

Common optional env:

- `OPENAI_BASE_URL`
- `OPENAI_CHAT_MODEL`
- `OPENAI_EMBEDDING_MODEL`
- `LOG_LEVEL`
- `LOG_PATH`
- `LOG_FORMAT`
- `VECTOR_STORE_DIR`
- `METADATA_DB_PATH`
- `KG_METADATA_DB_PATH`
- `MEMORY_DB_PATH`
- `STORAGE_RESET_STATE_DIR`
- `STORAGE_RUNTIME_LOCK`
- `RAG_DATA_DIR`
- `DEFAULT_WORKSPACE_ID`
- `DEFAULT_WORKSPACE_NAME`
- `DEFAULT_KNOWLEDGE_BASE_ID`
- `DEFAULT_KNOWLEDGE_BASE_NAME`
- `TOP_K`
- `MIN_RELEVANCE_SCORE`
- `CHUNK_SIZE`
- `CHUNK_OVERLAP`
- `SYSTEM_PROMPT`
- `AUTO_INGEST_ON_STARTUP`
- `MILVUS_BM25_ENABLED`
- `DENSE_RECALL_TOP_N`
- `BM25_RECALL_TOP_N`
- `FUSION_TOP_K`
- `RETRIEVAL_DEBUG_ENABLED`
- `QUERY_UNDERSTANDING_ENABLED`
- `QUERY_REWRITE_ENABLED`
- `QUERY_REWRITE_MAX_QUERIES`
- `RERANKER_ENABLED`
- `RERANKER_PROVIDER`
- `RERANKER_MODEL`
- `RERANKER_API_KEY`
- `RERANKER_BASE_URL`
- `RERANKER_TOP_N`
- `RERANKER_TIMEOUT_SECONDS`
- `OCR_ENABLED`
- `OCR_PROVIDER`
- `OCR_MIN_CONFIDENCE`
- `PROCESSING_TRACE_ENABLED`
- `PROCESSING_TRACE_DIR`
- `PROCESSING_WORKER_ENABLED`
- `PROCESSING_WORKER_ID`
- `PROCESSING_WORKER_POLL_INTERVAL_SECONDS`
- `PROCESSING_WORKER_LEASE_TIMEOUT_SECONDS`
- `PROCESSING_WORKER_MAX_CONCURRENT_TASKS`
- `PROCESSING_WORKER_DEFAULT_MAX_ATTEMPTS`
- `PROCESSING_WORKER_RETRY_BACKOFF_SECONDS`
- `WIKI_INGEST_ENABLED` (default `true`)
- `WIKI_INGEST_DEBOUNCE_SECONDS` (default `30`)
- `WIKI_FINALIZE_DEBOUNCE_SECONDS` (default `5`)
- `WIKI_INGEST_BATCH_SIZE` (default `5`)
- `WIKI_MAP_CONCURRENCY` (default `4`)
- `WIKI_REDUCE_CONCURRENCY` (default `4`)
- `WIKI_MAX_SOURCE_CHUNKS` (default `80`)
- `WIKI_MAX_SOURCE_CHARS` (default `32768`)
- `WIKI_MAX_CANDIDATES` (default `24`)
- `WIKI_MAX_PAGES_PER_DOCUMENT` (default `30`)
- `WIKI_TASK_TIMEOUT_SECONDS` (default `3600`)
- `WIKI_TASK_MAX_ATTEMPTS` (default `3`)
- `WIKI_LANGUAGE` (default `zh-CN`)
- `LANGFUSE_ENABLED`
- `LANGFUSE_BASE_URL`
- `LANGFUSE_HOST`
- `LANGFUSE_PUBLIC_KEY`
- `LANGFUSE_SECRET_KEY`
- `LANGFUSE_ENVIRONMENT`
- `LANGFUSE_DEBUG`
- `KG_EXTRACTION_ENABLED`
- `KG_METADATA_DB_PATH`

Wiki worker defaults are calibrated for one local SQLite writer and ordinary provider quotas. The 30-second ingest debounce absorbs upload bursts, batches are capped at 5 documents, and finalization waits 5 seconds so adjacent Reduce work can coalesce. Map and distinct-slug Reduce concurrency default to 4; same-slug commits remain serialized and SQLite lock conflicts use short bounded retries. Each LLM task has a 3600-second ceiling and 3 durable attempts. Worker backoff starts at `10,30,120` seconds, while provider `Retry-After` values take precedence up to 3600 seconds. Lower Map/Reduce concurrency to `1-2` for network filesystems or constrained SQLite deployments; raise it only after observing lock and provider-rate metrics.

`WIKI_INGEST_ENABLED=true` also enables the typed processing worker even when the legacy `PROCESSING_WORKER_ENABLED` setting is absent. To roll back typed Wiki execution without deleting pages or task history, set `WIKI_INGEST_ENABLED=false`; raw document processing and existing Wiki reads remain available.
- `KG_EXTRACTOR_MODEL`
- `KG_EXTRACTOR_VERSION`
- `KG_ENTITY_VECTOR_ENABLED`
- `KG_MILVUS_URI`
- `KG_MILVUS_TOKEN`
- `KG_ENTITY_COLLECTION`
- `KG_GRAPH_ENABLED`
- `NEO4J_URI`
- `NEO4J_USER`
- `NEO4J_PASSWORD`
- `GRAPH_RETRIEVER_ENABLED`
- `GRAPH_RETRIEVER_MAX_NEIGHBOR_DEPTH`
- `GRAPH_RETRIEVER_MAX_PATH_DEPTH`
- `GRAPH_RETRIEVER_ENTITY_LIMIT`
- `GRAPH_RETRIEVER_RELATION_LIMIT`
- `GRAPH_RETRIEVER_PATH_LIMIT`
- `AGENTIC_RETRIEVAL_ENABLED`
- `CHAT_AGENTIC_WORKFLOW_ENABLED`
- `AGENT_TRACE_STREAM_ENABLED`
- `AGENTIC_MAX_TOOL_CALLS`
- `AGENTIC_TOOL_TIMEOUT_SECONDS`
- `AGENTIC_RAW_TOP_K`
- `AGENTIC_KEYWORD_TOP_K`
- `AGENTIC_GRAPH_TOP_K`
- `AGENTIC_GRAPH_MAX_DEPTH`
- `EVAL_DATASET_DIR`
- `EVAL_REPORT_DIR`
- `EVAL_DB_PATH`
- `DOCUMENT_ENRICHMENT_ENABLED`
- `DOCUMENT_ENRICHMENT_MODEL`
- `DOCUMENT_ENRICHMENT_ASYNC`
- `DOCUMENT_ENRICHMENT_MAX_BATCH_TOKENS`
- `DOCUMENT_ENRICHMENT_MAX_SUMMARY_CHARS`
- `DOCUMENT_ENRICHMENT_MAX_RETRIES`
- `AGENT_RUNTIME_WEB_SEARCH_ENABLED`
- `AGENT_RUNTIME_WEB_SEARCH_URL`
- `AGENT_RUNTIME_WEB_FETCH_ENABLED`
- `AGENT_RUNTIME_WEB_FETCH_ALLOWED_DOMAINS`
- `AGENT_RUNTIME_DATA_ANALYSIS_ENABLED`
- `AGENT_RUNTIME_DATABASE_QUERY_ENABLED`
- `AGENT_RUNTIME_DATABASE_SOURCES`
- `REASONING_LLM_GREP_FIRST_ENABLED`
- `QUICK_LLM_GREP_FIRST_ENABLED`

The backend reads these variables while building `RAGService` and during startup ingest checks. Evidence: `backend/app/main.py:36-99`.

## Run Backend

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

Useful manual checks:

```powershell
curl http://localhost:8000/health
curl -X POST http://localhost:8000/ingest
curl -X POST http://localhost:8000/documents/parse -H "Content-Type: application/json" -d "{\"source\":\"example.md\"}"
```

Request logging can be enabled during local debugging:

```powershell
$env:LOG_LEVEL="debug"
$env:LOG_PATH="log/app.log"
$env:LOG_FORMAT="%d %level %traceId %msg"
uvicorn app.main:app --reload --port 8000
```

Every HTTP response includes `X-Trace-ID`. When a request fails, copy that value and search `backend/log/app.log` for the same ID to inspect `request.start`, component flow logs, `request.end`, and traceback entries. Logs redact secrets and summarize binary uploads, file downloads, and SSE streams.

Optional local Langfuse setup:

```powershell
$env:LANGFUSE_ENABLED="true"
$env:LANGFUSE_BASE_URL="http://localhost:3001"
$env:LANGFUSE_PUBLIC_KEY="pk-lf-..."
$env:LANGFUSE_SECRET_KEY="sk-lf-..."
$env:LANGFUSE_DEBUG="true"
```

`LANGFUSE_BASE_URL` is preferred for local development; `LANGFUSE_HOST` is still accepted when `LANGFUSE_BASE_URL` is empty. Langfuse is fail-open: missing credentials, missing package, or an unreachable server will not stop the backend. Use `GET /observability/status` or `/health` to inspect enabled/configured/package/failed state.

## Frontend Setup

```powershell
cd frontend
npm install
Copy-Item .env.local.example .env.local
```

Required env:

- `NEXT_PUBLIC_API_BASE`

Default example value: `http://localhost:8000`. Evidence: `frontend/.env.local.example:1`.

## Run Frontend

```powershell
cd frontend
npm run dev
```

Other available scripts:

```powershell
npm run build
npm run start
npm run lint
```

Evidence: `frontend/package.json:5-10`.

## Recommended Workflow

1. Start the backend first so the frontend can resolve API requests immediately.
2. Verify `/health`.
3. 在知识库详情页上传文档，或为目录 ingest 显式选择目标 KB。
4. Use `POST /documents/parse` to confirm parsing and parent-child chunk counts for a new file.
5. If you changed corpus files or chunking logic, run `POST /ingest`.
6. Start the frontend and open `http://localhost:3000`.
7. Test one chat request, one source preview, and one dataset refresh.

## What To Touch For Common Tasks

| Task | Primary files |
|---|---|
| Add or change API routes | `backend/app/main.py`, `backend/app/schemas.py` |
| Change retrieval or feedback logic | `backend/app/services/retrieval/rag_service.py` |
| Change embedding or vector persistence | `backend/app/services/retrieval/vector_store.py` |
| Add support for a new file type | `backend/app/services/documents/document_loader.py` |
| Change upload or parse behavior | `backend/app/main.py`, `backend/app/schemas.py`, `backend/app/services/retrieval/rag_service.py`, `backend/app/services/documents/document_loader.py` |
| Change chat UI behavior | `frontend/app/page.tsx` |
| Change styling | `frontend/app/globals.css` |

## Validation Checklist

- Backend boots without env errors.
- Startup ingest does not throw.
- `POST /documents/upload` accepts a supported file and rejects unsupported extensions.
- `POST /documents/upload` accepts optional `relative_path` and `batch_id` form fields for folder uploads, rejects unsafe paths, and stores safe nested files under `backend/data/uploads/<batch_id>/`.
- `POST /documents/parse` returns preview text plus parent and child chunk counts.
- `POST /chat/stream` returns SSE chunks.
- Dataset tab loads `GET /documents`.
- PDF sources open with `/documents/file`.
- Non-PDF sources open with `/documents/content`.
- Feedback submission writes a new markdown file and refreshes the list.
- Knowledge upload workspace handles single-file, multi-file, and folder uploads with per-file status and partial failure reporting.
- Query understanding keeps the raw query by default. When `QUERY_REWRITE_ENABLED=true`, the LLM may add bounded aliases, abbreviations, translations, field-name variants, and relation/action variants without a static terminology file.
- With `RETRIEVAL_DEBUG_ENABLED=true`, `POST /rag/query` includes `debug_info.query_understanding` so you can inspect the normalized query, retrieval queries, extracted constraints, and understanding source.
- With `MILVUS_BM25_ENABLED=false`, keyword retrieval uses SQLite FTS5 and should still match exact terms such as model names, API names, config keys, and error codes.
- `POST /rag/query` returns `answer`, `citations`, `used_chunks`, `used_entities`, `graph_paths`, `confidence`, and `debug_info`; during the Raw Evidence phase `used_entities` and `graph_paths` are empty lists.
- With `KG_EXTRACTION_ENABLED=false`, ingest/query behavior remains Raw RAG only.
- With `KG_EXTRACTION_ENABLED=true`, parent chunks can create KG extraction tasks and entity mentions without changing `/rag/query` default retrieval behavior.
- With `GRAPH_RETRIEVER_ENABLED=false`, graph retrieval is not constructed and `/rag/query` remains Raw RAG only.
- With `AGENTIC_RETRIEVAL_ENABLED=false`, `/rag/query` remains the existing Raw RAG query path and enterprise fields default to empty values.
- With `AGENTIC_RETRIEVAL_ENABLED=true`, `/rag/query` runs the finite-state Agentic Retrieval workflow and returns `agent_trace`, `tool_calls`, and `evidence_summary`.
- With `CHAT_AGENTIC_WORKFLOW_ENABLED=true`, `/chat/stream` runs the finite-state Agentic Retrieval workflow and streams `agent_trace`, `tool_call`, `tool_observation`, `evidence_summary`, `citation_verification`, `sources`, `reasoning`, and `token` payloads.
- With `AGENT_TRACE_STREAM_ENABLED=true`, `/chat/stream` may emit optional `agent_trace` SSE payloads before answer tokens while preserving existing SSE payloads.
- With `LOG_PATH=log/app.log`, `/health` returns `X-Trace-ID` and writes matching `request.start`/`request.end` entries to the log file.
- With local Langfuse configured, `/observability/status` reports `enabled=true`, `configured=true`, selected `host`, package availability, and failure state.
- With `PROCESSING_WORKER_ENABLED=true`, upload confirmation creates durable task rows, the worker can finish or retry them after restart, and failed exhausted tasks appear as dead-lettered in the document card.
- Opening a document Trace drawer should show the latest SQLite span tree. Local `PROCESSING_TRACE_DIR` files are supplemental evidence only.
- Document cards should show title, generated summary when available, date, type, processing state, Trace action, and delete action.
- Deleting a processing document should cancel queued/active task rows, close open spans, remove SQLite chunks/FTS rows, and ask the vector store to remove document vectors.
- With `RETRIEVAL_DEBUG_ENABLED=true`, retrieval debug should show query understanding, low-recall expansion decisions, dense/keyword hits, fusion, rerank degradation, MMR, duplicate removal, parent recall, and context expansion when those controls run.
- `POST /eval/runs` can run a configured evaluation dataset without adding it to document ingest, feedback, memory, vector stores, or graph data.

## Cautions

- 普通 ingest 只替换目标 KB/文档的派生索引，不得全局重置其他知识库。
- 旧 metadata 或 Milvus schema 不能通过 ingest 升级；系统会报告 `reset_required`，必须停服务后运行受保护的 clean-rebuild。
- Do not hand-edit persisted Chroma artifacts; they are legacy data after the Milvus migration.
- Avoid committing real secrets from `backend/.env` or `frontend/.env.local`.

## Milvus/Docling Development Notes

The backend now uses SQLite + Milvus for the active RAG store:

- SQLite metadata path: `METADATA_DB_PATH`, default `./vector_db/rag_metadata.sqlite3`
- SQLite FTS5 keyword index: `document_chunk_fts`, derived from child/table/OCR rows in `document_chunk`
- Milvus URI: `MILVUS_URI`, default `http://127.0.0.1:19530`
- Milvus token: `MILVUS_TOKEN`, default `root:Milvus`
- Milvus collection: `MILVUS_COLLECTION`, default `rag_chunk_vectors`
- Milvus BM25/sparse retrieval requires a Milvus 2.5-compatible server and `pymilvus==2.5.4` or newer with `FunctionType.BM25` and `DataType.SPARSE_FLOAT_VECTOR` support.
- Enabling `MILVUS_BM25_ENABLED=true` adds `bm25_text` and `bm25_sparse` to the final collection schema; incompatible existing collections require the protected clean-rebuild command.
- When `MILVUS_BM25_ENABLED=false`, `RAGService` uses the SQLite FTS5 keyword provider instead of scanning all chunks in Python.
- Dense/BM25 recall defaults to 50/50 and fuses to 30 candidates before optional reranking.
- Query understanding defaults to enabled and keeps the raw query unless optional LLM rewrite is enabled with `QUERY_REWRITE_ENABLED=true` after validating latency and cost.
- Reranker is disabled by default. `RERANKER_ENABLED=true` tries a local/bge cross-encoder and falls back to NoOp if the local dependency is not installed.
- DashScope rerank can be enabled without local model dependencies:

```powershell
RERANKER_ENABLED=true
RERANKER_PROVIDER=dashscope
RERANKER_MODEL=qwen3-vl-rerank
RERANKER_API_KEY=<your-dashscope-api-key>
RERANKER_BASE_URL=https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank
RERANKER_TOP_N=8
```

With `RETRIEVAL_DEBUG_ENABLED=true`, `/rag/query` includes `debug_info.reranked_results`. If this list is empty while reranker is enabled, check backend logs for a provider fallback or DashScope request failure.
- OCR is disabled by default. `OCR_ENABLED=true` uses parser-provided Docling OCR/image-text hooks when available and does not fail the main parse if OCR fails.
- Embedding dimension: `EMBEDDING_DIM`, default `1536`
- Embedding model: `OPENAI_EMBEDDING_MODEL`, default `text-embedding-3-small`
- Chunk sizing uses explicit character units: `PARENT_CHUNK_SIZE_CHARS`, `CHILD_CHUNK_SIZE_CHARS`, `CHILD_CHUNK_OVERLAP_CHARS`

## Knowledge Graph Development Notes

The KG foundation is optional and default-disabled:

- `KG_EXTRACTION_ENABLED=false` keeps Raw RAG ingest/query unchanged.
- `KG_METADATA_DB_PATH` defaults to the active metadata DB and stores `kg_extraction_task`, `entity_mention`, and `graph_community_summary`.
- `KG_EXTRACTOR_MODEL` defaults to `OPENAI_CHAT_MODEL`.
- `KG_EXTRACTOR_VERSION` defaults to `kg-v1` and is copied to every graph relation.
- `KG_ENTITY_VECTOR_ENABLED=true` writes canonical entities to Milvus `kg_entity_vectors`.
- `KG_MILVUS_URI` / `KG_MILVUS_TOKEN` default to the regular Milvus settings.
- `KG_ENTITY_COLLECTION` defaults to `kg_entity_vectors`.
- `KG_GRAPH_ENABLED=true` enables graph writes.
- `NEO4J_URI`, `NEO4J_USER`, and `NEO4J_PASSWORD` configure `Neo4jGraphStore`.

Neo4j is an optional dependency. If graph storage is disabled, the backend does not import or require the Neo4j driver. If graph storage is enabled but the driver or server is unavailable, backend startup remains safe and KG extraction tasks fail or partial-fail without failing Raw RAG ingest.

Current KG validation commands:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.test_kg_models_repository tests.test_kg_extractor_resolver tests.test_kg_vector_graph_store tests.test_rag_service_kg_enrichment tests.test_runtime_config -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## GraphRetriever Development Notes

GraphRetriever is a read-only graph evidence tool for later Agent workflows:

- `GRAPH_RETRIEVER_ENABLED=false` keeps graph retrieval disabled by default.
- `GRAPH_RETRIEVER_MAX_NEIGHBOR_DEPTH` caps neighbor traversal depth, default `3`.
- `GRAPH_RETRIEVER_MAX_PATH_DEPTH` caps path traversal depth, default `5`.
- `GRAPH_RETRIEVER_ENTITY_LIMIT` caps entity candidates, default `10`.
- `GRAPH_RETRIEVER_RELATION_LIMIT` caps returned graph relations, default `50`.
- `GRAPH_RETRIEVER_PATH_LIMIT` caps returned paths, default `10`.
- `NEO4J_URI`, `NEO4J_USER`, and `NEO4J_PASSWORD` configure the Neo4j read provider when graph retrieval is enabled.

GraphRetriever does not generate answers and is not called by `/rag/query` or `/chat/stream` by default. It returns structured entities, relations, paths, source chunk ids, confidence, evidence chunks, and debug metadata. Relations without resolvable `source_chunk_id` evidence are filtered out.

Current GraphRetriever validation commands:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.test_graph_retriever tests.test_kg_vector_graph_store tests.test_runtime_config tests.test_hybrid_retrieval tests.test_rag_api_routes -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Agentic Retrieval Development Notes

Agentic retrieval is optional and finite-state:

- `AGENTIC_RETRIEVAL_ENABLED=false` keeps `/rag/query` on Raw RAG.
- `CHAT_AGENTIC_WORKFLOW_ENABLED=false` keeps `/chat/stream` on the existing Raw RAG streaming path.
- `AGENT_TRACE_STREAM_ENABLED=false` keeps `/chat/stream` free of agent trace events.
- `AGENTIC_MAX_TOOL_CALLS` caps planned tool calls, default `6`.
- `AGENTIC_TOOL_TIMEOUT_SECONDS` is reserved for provider timeout enforcement, default `10.0`.
- `AGENTIC_RAW_TOP_K`, `AGENTIC_KEYWORD_TOP_K`, and `AGENTIC_GRAPH_TOP_K` set per-tool result limits.
- `AGENTIC_GRAPH_MAX_DEPTH` caps graph path/neighbor depth.

Current Agentic Retrieval validation commands:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.test_agentic_router_planner tests.test_agentic_tools_workflow tests.test_rag_api_routes tests.test_runtime_config -v
.\.venv\Scripts\python.exe -m unittest tests.test_agentic_chat_stream -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Autonomous Agent Runtime Notes

Intelligent reasoning mode defaults to model-directed ReAct batches when `AGENT_RUNTIME_ENABLED=true`.

- `REASONING_LLM_GREP_FIRST_ENABLED=true` requires `grep_chunks` in the first model-selected KB retrieval batch; independent semantic or graph calls may be in that same batch.
- `QUICK_LLM_GREP_FIRST_ENABLED=false` keeps quick mode bounded by default.
- `grep_chunks` accepts either the legacy `query` string or structured `queries`, `required_terms`, `match_mode`, and `top_k`.
- Search terms and aliases are generated by the LLM in each tool call. They are retrieval hints only; final answers still require retrieved and deep-read KB evidence.
- `thinking` is optional public audit output. The model can call retrieval directly and chooses every follow-up query or read from prior observations.
- `AGENT_RUNTIME_LEGACY_REMEDIAL_RETRIEVAL_ENABLED=false` keeps controller-selected corrective retrieval off. Enable it only for compatibility rollback.

Scheduler and budget configuration:

- `AGENT_RUNTIME_MAX_ITERATIONS`: maximum action rounds.
- `AGENT_RUNTIME_MAX_LLM_CALLS`: model-call budget, excluding the one reserved terminal synthesis opportunity.
- `AGENT_RUNTIME_MAX_TOOL_CALLS`: total tool-call execution budget; over-budget calls receive structured failures.
- `AGENT_RUNTIME_MAX_WALL_CLOCK_SECONDS`: request wall-time budget checked before each model round.
- `AGENT_RUNTIME_MAX_REPEATED_TOOL_BATCHES`: unchanged action-signature limit when evidence has not grown.
- `AGENT_RUNTIME_MAX_PARALLEL_WORKERS`: maximum local workers for a parallel-safe segment.
- `AGENT_RUNTIME_LOCAL_CONCURRENCY_ENABLED`: disable physical overlap while retaining ordered batch semantics.
- `AGENT_RUNTIME_PARALLEL_TOOL_CALLS_MODE`: provider option mode, one of `auto`, `on`, or `off`.
- `AGENT_RUNTIME_TERMINAL_STREAMING_MODE`: safe terminal streaming mode, one of `auto`, `on`, or `off`.

Focused validation:

```powershell
cd backend
python -m pytest tests/test_agent_runtime_loop.py tests/test_agent_runtime_prompts_tools.py tests/test_agent_runtime_batching.py -q

cd ..\frontend
node --test app/lib/agent-stream.test.mjs
```

## Enterprise Evaluation Suite Development Notes

Enterprise evaluation is optional and isolated from the retrievable corpus:

- `EVAL_DATASET_DIR` defaults to `backend/evalsets`.
- `EVAL_REPORT_DIR` defaults to `<VECTOR_STORE_DIR>/eval_reports`.
- `EVAL_DB_PATH` defaults to `<VECTOR_STORE_DIR>/rag_eval.sqlite3`.
- Evalsets are JSON or YAML files with `schema_version`, dataset metadata, and cases.
- `POST /eval/runs` starts a synchronous run from a dataset path.
- `GET /eval/runs`, `GET /eval/runs/{run_id}`, and `GET /eval/runs/{run_id}/results` inspect stored runs.
- Rule-based metrics run without LLM judge calls; optional judge providers can be added behind the evaluation provider interface.

Current evaluation validation commands:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.test_enterprise_evaluation_suite -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Install backend dependencies after pulling this change:

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Milvus must be reachable before starting the backend. Chroma is no longer used by the app, and `backend/chroma_db/` should be treated as legacy data.

Current local validation:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m unittest tests.test_document_repository tests.test_keyword_search tests.test_hybrid_retrieval tests.test_rag_api_routes -v

cd frontend
npm run build
```

## Multi-Knowledge-Base Development Notes

- 本地旧请求缺省使用 `default-workspace/default-knowledge-base`，缺省不代表全部知识库。
- `DocumentRepository.replace_chunks` 要求权威 document 已存在，且 chunk 归属必须与 document 一致。
- 不得在 KB 局部 ingest 中调用全局 `reset_collection()`；使用 Provider 的 KB 范围删除/重建。
- Milvus 缺少 workspace/KB schema 时 `reset_required=true`；Dense/BM25 查询和普通写入失败关闭，不允许用 FTS5 掩盖存储版本不一致。
- 文档 enrichment 默认关闭；开启后需要可用的 OpenAI-compatible JSON 输出模型。

知识库保存 requested/effective Provider 配置。requested 记录用户期望，effective 记录当前工厂实际启用值；不受支持的覆盖项进入 `inactive_overrides`。归档是逻辑删除：归档 KB 不能上传或检索，但数据仍保留，物理 purge 不属于当前阶段。

### Destructive clean-rebuild

Use this flow whenever startup raises `StorageResetRequired`, including metadata schema changes for `document_processing_task`, `document_processing_dead_letter`, `knowledge_processing_spans`, prompt/runtime state, Milvus collection shape, FTS5 layout, or KG/evaluation tables. Do not fix those errors with manual `ALTER TABLE`, hand-deleting one table, or partial Milvus cleanup; the supported upgrade path is a full clean-rebuild after stopping API and workers.

该命令只用于从旧单库结构升级到最终多库结构，不提供历史数据迁移。必须先停止 API 和所有 worker；运行中的服务会写入 `STORAGE_RUNTIME_LOCK`，命令检测到活动 PID 时拒绝执行。

```powershell
cd backend

# dry-run：检查 SQLite、Milvus、Neo4j、报告、状态和受管理源文件计划
python -m app.scripts.rebuild_knowledge_storage --delete-managed-sources --include-neo4j

# execute：确认短语必须完全一致
python -m app.scripts.rebuild_knowledge_storage `
  --execute `
  --environment dev `
  --confirm RESET_ALL_APPLICATION_DATA:dev `
  --backup-dir D:\backup\bee-before-reset `
  --delete-managed-sources `
  --include-neo4j
```

- 不加 `--execute` 永远只输出计划。
- `--delete-managed-sources` 只允许删除 `RAG_DATA_DIR/uploads`、`feedback`；`.env`、词表、应用配置和备份目录不在白名单。
- 评测报告和生成的 ingest state 会清理；SQLite/WAL/SHM 会按最终 DDL 重建。
- `--backup-dir` 只覆盖 SQLite 和受管理文件。需要保留向量/图数据时，执行前分别使用 Milvus 原生备份和 `neo4j-admin`。
- `--skip-milvus` 只用于离线 schema 开发，不能作为生产升级完成标准。
- 成功后 manifest 位于 `STORAGE_RESET_STATE_DIR/reset-manifest.json`；任一 Provider 失败时保留 `maintenance.json`，应用拒绝启动。排除故障后重新执行完整 clean-rebuild，不要手工拼接新旧存储。
- 恢复只支持回到 clean-rebuild 前的完整一致备份和对应旧版本应用；当前命令不负责把旧数据导入新 schema。

聚焦验证：

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.test_knowledge_base_repository tests.test_knowledge_base_scoped_retrieval tests.test_document_enrichment tests.test_milvus_vector_store -v
.\.venv\Scripts\python.exe -m unittest tests.test_storage_schema_reset tests.test_knowledge_audit_repository -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

cd ..\frontend
npm run build
```
