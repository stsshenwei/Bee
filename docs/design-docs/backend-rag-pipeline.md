# Backend RAG Pipeline

## WeKnora 自适应文档处理

新文档处理入口通过 `ParserEngineRegistry` 解析请求引擎与实际生效引擎。默认 `builtin`：PDF 使用 `pypdfium2` 逐页计算文本量与图片覆盖率，原生页保留文本层，扫描页按受限 DPI/质量渲染为 JPEG，最终按页序输出 Markdown 与图片引用；Docling 是延迟加载的可选引擎。

切片遵循不可变的 WeKnora 降级链：

```text
DocumentProfile
  -> heading（满足标题数量、密度和主层级）
  -> heuristic（满足分页、章节或结构标记）
  -> legacy/recursive（始终最终兜底）
```

每个非最终层都必须经过空结果、无效单块、碎片化、全体过小和超过两倍目标尺寸检查。只有检查失败才进入下一层；不得跳级、重排或以其他策略替换 recursive 兜底。代码块、公式、图片/链接和表格区间在边界扫描中受到保护。父块与每个父块内的子块分别运行同一自适应策略；父块保存在 SQLite，子块、表格块和图片派生块进入 FTS5/Milvus。

`POST /documents/parse` 是只读处理预览：复用生产解析与切片决策，但不写 repository、FTS5、Milvus、KG 或知识增强状态。响应包含解析诊断、PDF 页面统计、文档画像、候选/拒绝策略、最终层和切片样本。

OCR 与图片描述属于文本解析后的派生证据。结果使用 `image_ocr`、`image_caption` 类型并保留图片、页码、父块、Provider 和置信度；失败不得回滚已经成功的原生文本索引。

本架构不兼容旧数据。启用前必须停止全部写入进程，并通过维护命令显式清空整个旧 SQLite、全部 FTS 表、所有 Milvus collection、Neo4j 数据和旧媒体状态。普通启动只进入 `reset_required`，绝不隐式删除数据。

## Staged Upload Batch And Task Lifecycle

Knowledge management uploads now have a staged API under `/knowledge-bases/{knowledge_base_id}/upload-batches`:

```text
POST   /knowledge-bases/{kb}/upload-batches
POST   /knowledge-bases/{kb}/upload-batches/{batch}/files
PATCH  /knowledge-bases/{kb}/upload-batches/{batch}/settings
POST   /knowledge-bases/{kb}/upload-batches/{batch}/confirm
GET    /knowledge-bases/{kb}/upload-batches/{batch}
POST   /knowledge-bases/{kb}/upload-batches/{batch}/files/{file}/retry
POST   /knowledge-bases/{kb}/upload-batches/{batch}/cancel
```

`knowledge_upload_batch` persists batch status, requested settings JSON, timestamps, and sanitized errors. `knowledge_upload_file` persists each file task with original name, relative path, managed storage path, size, status, document id, chunk count, timestamps, and sanitized errors.

Batch states:

```text
draft -> uploading -> ready_to_process -> processing
  -> completed | partial_failed | failed | canceled
```

File states:

```text
pending -> uploaded -> parsing -> indexed -> enrichment_pending
  -> completed | failed | canceled
```

Provider-safety boundary:

- selecting files in the frontend and uploading them into a draft batch only writes managed source files and task rows
- draft/uploading/ready batches do not parse, chunk, embed, index, enrich, or call external providers
- processing starts only after explicit confirm
- confirmation and retry persist visible file-task status before and after parse/index/enrichment phases

The processing path reuses the existing parser, chunker, `DocumentRepository`, vector store, FTS rows, optional KG, optional enrichment, preview, and deletion boundaries. Every operation carries a `KnowledgeBaseScope`; cross-KB batch fetches return not found and do not reveal file names or errors from another KB. Clean rebuild reinitializes upload batch/file task tables consistently with managed upload source deletion.

The legacy `/documents/upload` route remains a compatibility shortcut, but the knowledge management UI uses staged upload endpoints.

## Processing Trace And Local Evidence

Every production parse/index run writes a Weknora-style span trace into SQLite table `knowledge_processing_spans`. The span trace is the source of truth for the frontend trace drawer and stores a root span plus canonical stage spans:

- `docreader`: document loading and parser output
- `chunking`: adaptive chunk strategy selection and chunk statistics
- `embedding`: SQLite chunk replacement plus vector index writes
- `multimodal`: OCR/caption/image operation persistence and processing
- `postprocess`: KG enrichment and document enrichment task enqueueing

Each row stores `knowledge_id`, `attempt`, `span_id`, `parent_span_id`, `name`, `kind`, `status`, JSON input/output/metadata, error fields, timestamps, and `duration_ms`. New runs create a new attempt, and stage failures cascade cancellation to downstream dependent stages.

For operator debugging, every production parse/index run also creates local artifacts by default under `RAG_DATA_DIR/processing_traces` or `PROCESSING_TRACE_DIR` when set. A trace directory contains:

- `trace.json`: root trace metadata plus `load`, `chunk_strategy`, `index`, `postprocess`, and `multimodal` spans
- `parsed.md`: the parsed document markdown after parser/OCR loading
- `chunks.jsonl`: every parent, child, table, OCR, and image-derived chunk with ids, strategy metadata, page fields, and full content
- `report.md`: human-readable processing report with status, stage timing, parser/chunk/index summaries, config, and error traceback
- `chunks_preview.md`: human-readable preview of the first chunks with ids, type, strategy, page/title metadata, and content snippets
- `error.txt`: traceback for failed runs

`trace.json` stores requested/effective processing settings, parser diagnostics, chunk strategy summary, tier chains, chunk length statistics, index counts, and the final status. Failed upload file tasks include a `processing trace: <path>` error entry so the UI-visible task can be matched to the local evidence directory.

Langfuse is optional. When `LANGFUSE_ENABLED=true`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_BASE_URL` or `LANGFUSE_HOST` are configured and the `langfuse` Python package is installed, the same stage spans and selected request/retrieval/agent/model observations are emitted to Langfuse. Local trace writing remains the source of truth and does not depend on Langfuse availability.

## Durable Processing Runtime

Upload confirmation now has two execution modes:

- `PROCESSING_WORKER_ENABLED=true`: `/upload-batches/{batch}/confirm` writes `document_processing_task` rows before returning. `DocumentProcessingWorker` claims runnable rows, refreshes leases, retries failures according to `PROCESSING_WORKER_RETRY_BACKOFF_SECONDS`, and moves exhausted work to `document_processing_dead_letter`.
- `PROCESSING_WORKER_ENABLED=false`: the route still calls `start_upload_batch_processing()` for state transition and schedules the existing FastAPI `BackgroundTasks` compatibility path.

Task rows carry task type, workspace/knowledge-base scope, document/upload ids, payload JSON, status, attempt counters, next-run time, lease owner/deadline, trace id, timestamps, and sanitized last-error fields. Document deletion cancels queued/active task rows for that document, closes open database spans on the latest attempt, removes SQLite chunks/FTS rows, deletes derived media objects, and asks the vector store to remove document vectors.

The frontend document list consumes `summary_available`, `processing_task_status`, retry attempt counters, dead-letter state, last error, and `processing_latest_attempt`. The Trace drawer loads the database span tree first and treats local files (`parsed.md`, `chunks.jsonl`, `report.md`, `chunks_preview.md`, `trace.json`) as supplemental evidence links.

## Prompt Catalog And Runtime Tools

Prompt-bearing model calls are backed by YAML files in `backend/config/prompt_templates/`. The startup catalog validates required template ids, placeholder declarations, UTF-8 readability, mode compatibility, and malformed YAML. Quick-answer context uses the configured RAG context template; reasoning mode renders the agent system template with selected knowledge bases, tool definitions, skills metadata, conversation context, language, and user question.

The optional reasoning runtime registers tools through `ToolRegistry`. Core read-only tools remain available when enabled. Extended non-wiki tools are feature-gated:

- web search and web fetch require explicit enablement, provider endpoint or allowlist, timeout, and output limits
- data analysis and database query are read-only and scoped to configured data sources
- executable skill behavior remains disabled and returns a stable unavailable observation

Tool spans record bounded arguments, status, duration, error class, and output summaries. Hidden reasoning, raw prompts, secrets, cookies, provider payloads, and unbounded content are not exposed to UI traces.

### LLM Wiki Runtime

LLM Wiki adds a curated page layer after raw chunk persistence. The implementation stores pages, folders, issues, proposals, source refs, aliases, generation tasks, contribution manifests, pending operations, logical logs, and `[[slug]]` link caches in SQLite. It does not replace raw evidence retrieval.

`parse_and_index_document()` first persists current document chunks. Dense/keyword strategy controls embedding and vector writes; graph and Wiki decisions are independent. A Wiki-only knowledge base therefore records a skipped embedding span with `dense_and_keyword_disabled`, creates no vector rows, and still enqueues durable Wiki work.

`wiki.ingest` reconstructs bounded parent/table/OCR/image-derived evidence with stable chunk markers, extracts entity/concept candidates, classifies exact citations and generates the document summary, plans one coherent taxonomy for the new contribution set, replaces the document contribution manifest, and writes each affected canonical page with the planned category applied on first publication. `wiki.finalize` performs strict duplicate review before refreshing the Index introduction, then maintains the logical Log, refreshes links, emits lint issues, and applies taxonomy only as a fallback for historical unclassified pages. The seven primary LLM stages are candidate extraction, chunk citation classification, document summary generation, taxonomy planning, canonical page writing, duplicate review, and Index introduction update; all are defined in `backend/config/prompt_templates/wiki_generation.yaml` and called through the same structured JSON helper with temperature `0.3` and at most three attempts. Invalid primary extraction gets one conservative combined-extraction fallback. Wiki generation reads persisted source chunks directly and does not invoke Embedding or Rerank models.

Valid automatic output is published with source and chunk refs. A source revision change cancels stale work; retry exhaustion moves the typed task and generation state to dead letter without rolling back parsed chunks. Existing manual folder placement and any previously assigned category path are preserved during automatic taxonomy planning, and duplicate review creates recoverable merge proposals instead of applying destructive merges directly.

The calibrated single-node defaults are: 30-second ingest debounce, 5-document claims, 4 Map workers, 4 distinct-slug Reduce workers, 5-second coalesced finalization, 3600-second task timeout, 3 attempts, and worker backoff of 10/30/120 seconds. Provider `Retry-After` is honored up to one hour. Manifest and page commit journals make replay idempotent; optimistic page versions and same-slug serialization protect conflicting commits. These settings are enabled with `WIKI_INGEST_ENABLED=true` and can be rolled back by disabling that flag without removing generated pages, evidence, or task history.

Source deletion and reindexing retract prior active contributions. Pages without grounded generated content are archived or marked stale when protected manual content remains. These operations are scoped to one KB and do not roll back successful parsing.

Agent Wiki read tools are gated by `AGENT_RUNTIME_WIKI_TOOLS_ENABLED` and are enabled for the dedicated Wiki Q&A mode by default through `AGENT_RUNTIME_WIKI_MODE_ENABLED=true`. They can search pages, read full Markdown, drill into raw source chunks, and flag issues. Maintenance tools are gated separately by `AGENT_RUNTIME_WIKI_MAINTENANCE_TOOLS_ENABLED`; write, replace, rename, and delete operations create pending proposals by default. For exact answers, `wiki_rag_agent` prompts prefer `wiki_read_source_doc`, `list_knowledge_chunks`, or existing raw retrieval tools over Wiki summaries.

## Retrieval Quality Controls

The base retrieval path remains dense + keyword + RRF + optional rerank + parent recall. Additional Weknora-style controls are conservative and configurable:

- low-recall query expansion runs only when the initial candidate count or score is below threshold
- reranker threshold degradation keeps bounded top candidates when strict filtering would remove all useful evidence
- MMR diversity selection can reduce redundant near-duplicate evidence after fusion/rerank
- duplicate removal uses stable chunk ids, parent ids, content signatures, and overlap thresholds
- debug metadata records query understanding, expansion, dense/keyword hits, fusion, rerank, degradation, MMR, duplicate removal, parent recall, and context expansion

FAQ-specific retrieval remains excluded from this change.

## Goal

Provide a single backend service that can:

- upload and parse user-provided knowledge documents
- index local knowledge files
- retrieve relevant child chunks with vector, keyword, and hybrid recall
- recall parent chunks for answer context
- stream an answer with source references
- learn from user-submitted corrections

## Owning Files

- `backend/app/main.py`
- `backend/app/services/retrieval/rag_service.py`
- `backend/app/services/retrieval/vector_store.py`
- `backend/app/services/documents/document_loader.py`

## Design Shape

`main.py` handles framework concerns and delegates application behavior into `RAGService`. Evidence: `backend/app/main.py:53-178`.

`RAGService` coordinates six concerns:

1. corpus change detection and ingest state tracking
2. document upload and parse previews
3. parent-child chunk generation and vector upsert
4. keyword index state and hybrid retrieval
5. parent recall, score filtering, and source extraction
6. feedback file creation plus immediate parent-child re-upsert
7. optional KG enrichment after Raw Evidence indexing succeeds
8. optional agentic retrieval orchestration for `/rag/query` and `/chat/stream`
9. optional enterprise evaluation replay through the query service

Evidence: `backend/app/services/retrieval/rag_service.py:44-300`.

## Ingest Strategy

- Source discovery is recursive and extension-based.
- Temporary Office lock files are skipped.
- Ingest wipes the existing `rag_chunks` collection before rebuilding.
- The vector collection stores child chunk text.
- Metadata stores `source`, `parent_id`, `child_id`, `parent_index`, and `child_index`.
- Parent chunk text is stored in `parent_store.json` under the active vector store directory.
- Keyword child text is stored in `keyword_index.json` under the active vector store directory.

Evidence: `backend/app/services/documents/document_loader.py:16-21`, `backend/app/services/retrieval/rag_service.py:84-120`, `backend/app/services/retrieval/vector_store.py:47-64`.

### Tradeoff

This full rebuild strategy keeps consistency simple, but it is expensive for large corpora and does not preserve partial history.

## Upload And Parse Strategy

- `POST /documents/upload` stores uploaded files under `data/uploads/`.
- The upload endpoint also accepts optional `relative_path` and `batch_id` form fields. When present, the backend stores files under `data/uploads/<batch_id>/<relative_path>` after sanitizing every path segment and verifying the final target remains inside the upload directory.
- Folder uploads are still parsed one file at a time. The frontend submits each file independently, and the backend reuses the same `parse_and_index_document()` path for storage, SQLite metadata, chunking, and Milvus indexing.
- Unsafe upload paths are rejected before writing: empty paths, absolute paths, Windows drive-qualified paths, `..` path traversal, and unsupported extensions.
- Upload filenames are reduced to safe basenames and checked against `SUPPORTED_EXTS`.
- `POST /documents/parse` resolves the source through the same path safety boundary as preview/download.
- Parse returns extension, character count, parent chunk count, child chunk count, and a text preview.

## Optional KG Enrichment Hook

Knowledge graph extraction is a post-index enrichment step, not part of the default query path. It is controlled by `KG_EXTRACTION_ENABLED=false` by default.

When enabled for document ingest:

1. `RAGService` saves the `document` row and `document_chunk` rows.
2. child/table/OCR chunks are written to Milvus and SQLite FTS5.
3. parent chunks are sent to `KGEnrichmentService`.
4. the configured `KGExtractorProvider` returns entities and relations.
5. `EntityResolverProvider` canonicalizes entities through normalized exact names, aliases, and optional entity-vector similarity.
6. entity mentions are written to SQLite `entity_mention`.
7. canonical entities are optionally written to `kg_entity_vectors` and the graph store.
8. relations are optionally written to the graph store with source evidence fields.

KG failures are fail-open for Raw RAG. Extractor, resolver, entity-vector, or graph-store errors update `kg_extraction_task` to `failed` or `partial_failed`, but the document ingest remains successful once Raw Evidence storage has completed. This keeps upload, parse, `/rag/query`, and `/chat/stream` behavior stable when KG providers are disabled, unavailable, or misconfigured.

## Graph Retrieval Strategy

GraphRetriever is a read-only graph evidence tool for later Agent workflows. It is not part of default Raw RAG retrieval.

When configured, GraphRetriever can:

- search graph entities by name, alias, and optional entity-vector similarity
- retrieve bounded neighbors for an entity id
- find bounded paths between two entities
- build structured graph context from paths and entities

GraphRetriever treats graph data as derived evidence. Every returned relation must carry `source_chunk_id`, and every source chunk id is resolved through SQLite `document_chunk` before the relation or path is returned as usable evidence. Missing or unsupported relations are filtered and recorded in debug metadata.

Default answer behavior remains:

```text
question
  -> query understanding and terminology expansion
  -> Raw RAG dense/keyword retrieval
  -> RRF fusion and optional reranking
  -> SQLite parent/table/OCR context lookup
  -> prompt context -> answer
```

GraphRetriever becomes useful when a later Agent workflow chooses it as a tool for dependency, impact, troubleshooting, or path-style questions.

## Agentic Retrieval Strategy

The Agentic Retrieval Layer composes the existing evidence tools without replacing them. It is controlled by `AGENTIC_RETRIEVAL_ENABLED=false` by default.

Query routing is deterministic:

- `fact`: Raw RAG plus GraphRetriever entity search.
- `source`: Raw RAG only.
- `howto`: Raw RAG plus SQLite FTS5 keyword search.
- `troubleshooting`: Error/Config/Service graph context plus Raw RAG plus keyword search.
- `comparison`: Raw RAG plus graph entity context.
- `impact`: graph neighbor/path evidence plus Raw evidence validation.
- `dependency`: GraphRetriever path search is required.
- `summary`: Raw RAG, with graph community summaries reserved for a later change.
- `decision`: Raw RAG plus graph context, with explicit uncertainty.

The finite-state workflow is:

```text
AnalyzeQuestion -> PlanRetrieval -> CheckPermissionScope -> RunRetrieval
-> FuseEvidence -> RerankEvidence -> NeedMoreEvidence -> BuildContext
-> GenerateAnswer -> VerifyCitations -> ReturnAnswer
```

Tools only return structured evidence; they do not generate final answers. Evidence fusion deduplicates raw chunks, keyword chunks, graph entities, graph paths, citations, and source chunk ids while preserving the source tool metadata. Dependency and impact questions require graph path or relation evidence; if graph evidence is unavailable, Raw RAG may still be shown as context, but the final answer must state that the dependency or impact cannot be determined.

`CitationVerifier` is the final factual gate. It resolves citations, used chunks, and graph relation `source_chunk_id` values through `DocumentRepository.get_chunk()`. Invalid citations or graph sources downgrade the answer to explicit insufficient evidence and are surfaced in `debug_info.citation_verification`.

Visible workbuddy-style trace is an audit summary, not hidden chain-of-thought. Trace entries include stage, status, summary, tool metadata, source chunk ids, evidence sufficiency, and citation verification status. Private scratchpads and model deliberation must not be exposed.

Agentic chat streaming is controlled separately by `CHAT_AGENTIC_WORKFLOW_ENABLED=false`. When enabled, `/chat/stream` uses `AgenticRetrievalWorkflow.stream_query_events()` instead of running Raw RAG directly in the route handler. The SSE order remains compatible:

## Autonomous ReAct Agent Runtime

The optional ReAct runtime is controlled by `AGENT_RUNTIME_ENABLED=false`. When enabled, `/chat/stream` reasoning mode prefers `AgentRuntime` before the deterministic `AgenticRetrievalWorkflow`; quick mode still uses the direct retrieval-answer path.

The runtime adds:

- YAML prompt templates from `config/prompt_templates/agent_system_prompt.yaml`
- a model-facing `ToolRegistry` with stable function definitions, argument validation, output truncation, and recoverable error observations
- read-only document tools: `thinking`, `todo_write`, `knowledge_search`, `grep_chunks`, `list_knowledge_chunks`, `get_document_info`, `query_knowledge_graph`, and optional `read_skill`
- model-directed action rounds with mandatory deep-read enforcement after search tools return candidates
- runtime trace events compatible with the existing agent timeline stream
- optional read-only runtime skills under `runtime_skills/preloaded`

The LLM request is the Think phase. A response may answer directly or return one ordered batch containing one or more tool calls. The controller validates the whole batch before execution. Contiguous `parallel_safe` calls may overlap in a bounded thread pool; `serial` and `exclusive` calls form barriers. Every worker receives an immutable context and pre-batch state snapshot. Tools return explicit state deltas, and the controller commits results, model tool messages, sources, and events in the original call order even when physical completion is out of order.

Wiki tools, web search/fetch, data-analysis SQL tools, and executable skill scripts remain feature-gated. The runtime can be rolled back by disabling `AGENT_RUNTIME_ENABLED`, which returns reasoning mode to the existing deterministic workflow.

The runtime now also emits first-class Agent domain events for the Weknora-style reasoning lifecycle:

```text
agent_query
  -> agent_thought
  -> agent_tool_call / agent_tool_result
  -> agent_reflection
  -> optional agent_remedial_search
  -> agent_references
  -> agent_final_answer
  -> agent_complete
```

The domain events are additive. `/chat/stream` maps them back to compatible SSE payloads for existing clients: `agent_references` also emits `sources`, `agent_tool_call`/`agent_tool_result` preserve `tool_call`/`tool_observation`, `agent_final_answer` is followed by the existing `token` stream, and `agent_complete` is followed by `[DONE]` at the route layer. Sourced reasoning responses emit references before answer tokens.

Reasoning mode uses a Weknora-aligned Assess-Reconnaissance-Plan-Execute prompt when `REASONING_LLM_GREP_FIRST_ENABLED=true`:

```text
reasoning question
  -> intent assessment
  -> Phase 1: grep_chunks + knowledge_search reconnaissance
  -> mandatory full-content deep read
  -> Path A: synthesize when evidence is sufficient
  -> Path B: execute evidence tasks sequentially
  -> reflect on each task and repair focused gaps
  -> final synthesis with no further tool calls
```

The prompt follows Weknora's retrieval discipline but preserves project truth: Bee identity, provider-safe simple alternation instead of PostgreSQL regex guarantees, the actual chunk/document tool schemas, natural source titles instead of unsupported citation tags, and feature-gated Web use. The LLM generates synonyms, aliases, translations, abbreviations, legacy names, model fragments, and equivalent parameter expressions directly inside each tool call. For one search objective, it selects the 2-3 highest-value alternatives and packs them into one simple `term1|term2|term3` query instead of emitting separate `grep_chunks` calls for each synonym. The backend normalizes this simple alternation into provider-safe bounded variants. These generated terms are retrieval hints, not answer evidence. Quick mode remains bounded by default because `QUICK_LLM_GREP_FIRST_ENABLED=false`.

For any multi-constraint filtering, comparison, or recommendation request, reasoning mode asks the LLM to extract all hard constraints, normalize relevant aliases and units as hypotheses, and verify every condition against deep-read evidence for the same candidate or subject. Verified candidates are listed first; a recommendation is added only when the evidence supports meaningful differences.

`thinking` is an optional public audit tool, not hidden chain-of-thought and not a prerequisite for retrieval. It can record `phase`, `summary`, `validity`, `gap`, `correction_query`, `completion_status`, and `source_chunk_ids`. By default, a gap is returned to the model as an observation and the model chooses the next query, read, tool, or final answer. The old controller-selected remedial path is available only through `AGENT_RUNTIME_LEGACY_REMEDIAL_RETRIEVAL_ENABLED=true`.

Budgets are independent: action rounds, total LLM calls, proposed/executed tool calls, elapsed wall time, and repeated unchanged action signatures are tracked separately. If a limit is reached after qualifying deep reads, one reserved tools-disabled synthesis request produces a grounded answer. Without qualifying evidence, or if synthesis fails, the runtime returns a localized deterministic insufficient-evidence fallback.

Provider adaptation uses `AGENT_RUNTIME_PARALLEL_TOOL_CALLS_MODE` and `AGENT_RUNTIME_TERMINAL_STREAMING_MODE` with `auto`, `on`, or `off`. The runtime retries once without `parallel_tool_calls` on a recognized unsupported-parameter response and caches the result per model. Streamed tool-call deltas are fully assembled before entering history; provisional content and partial arguments are never exposed. References are emitted before terminal answer tokens.

Runtime spans and event metadata include batch/call identity, declared call index, execution class, action-round duration, model latency/first byte, queue time, tool duration, physical completion order, batch wall time, terminal first token, worker limit, budgets, stop reason, and provider compatibility fallback. Local concurrency can be disabled without changing batch semantics or deterministic result order.

## Quick Answer Trace And Grounded Synthesis

Quick chat remains a bounded Raw RAG path rather than a ReAct/runtime-agent path. After scoped retrieval and parent recall, `/chat/stream` emits a public quick-answer trace before answer tokens:

```text
sources
  -> reasoning
  -> agent_trace: UnderstandQuestion
  -> agent_trace: RetrieveKnowledgeBase
  -> agent_trace: ReadEvidence
  -> agent_trace: SynthesizeAnswer
  -> agent_trace: Complete
  -> token...
  -> [DONE]
```

The quick trace is an audit summary derived from the current retrieval data. It records normalized query details, retrieval query count, candidate/hit counts, cited document count, matched chunk ids, selected knowledge-base scope, and insufficient-evidence status. It does not expose hidden chain-of-thought, scratchpads, raw prompts, memory context, secrets, or provider payloads. If `AGENT_TRACE_STREAM_ENABLED=false`, quick mode still falls back to the older shape of sources, reasoning, and tokens.

Quick answer generation uses one domain-agnostic evidence contract for every question. The model selects the appropriate Markdown structure from the request semantics, extracts hard constraints for filtering/comparison/recommendation tasks, and verifies each candidate or subject against its own source evidence. The backend does not classify questions with domain keyword lists or inject domain-specific answer templates.

```text
conversation_id
  -> agent_trace / tool_call / tool_observation / evidence_summary
  -> sources
  -> reasoning
  -> citation_verification
  -> token...
  -> memory_updated
  -> [DONE]
```

The stream still persists the assistant message, summarizes conversation history, and processes long-term memory after answer tokens complete. Memory and conversation context are prompt context only; they are not citable evidence.

## Unified Chat ReAct Runtime

`CHAT_UNIFIED_RUNTIME_ENABLED=true` enables the shared runtime shell for quick chat while keeping the legacy Raw RAG path available as rollback. The runtime shape is:

```text
execute
  -> execute_loop
    -> run_react_iteration
      -> Think
      -> Analyze
      -> Act
      -> Observe
```

Reasoning mode continues to use the existing Agent runtime policy when `AGENT_RUNTIME_ENABLED=true`. Quick mode uses a separate `quick` policy when `CHAT_UNIFIED_QUICK_RUNTIME_ENABLED=true` or when it inherits the unified runtime flag. The quick policy preloads bounded Raw RAG evidence, renders the `quick_rag_agent` prompt plus `qa_context`, exposes no tools by default, disables remedial retrieval, and caps the loop to one iteration unless explicitly configured.

The runtime emits first-class domain events through a request-scoped event bus before `/chat/stream` maps them to SSE payloads. Domain events remain additive and compatible with legacy clients:

```text
agent_query
  -> optional agent_thought
  -> optional agent_tool_call / agent_tool_result
  -> agent_reflection
  -> agent_references
  -> agent_final_answer
  -> agent_complete
```

Quick mode normally emits no tool events. If a model or provider returns a tool call outside the active policy allowlist, the runtime rejects it with a sanitized failed `agent_tool_result` and does not execute the tool. Runtime exceptions emit `agent_error` followed by a failed `agent_complete` when the stream is still writable; the route still emits legacy `error` and `[DONE]`.

## Retrieval Strategy

- Dense recall uses Milvus vector search through `query_dense`.
- Keyword recall uses Milvus BM25/sparse search when `MILVUS_BM25_ENABLED=true`.
- When Milvus BM25 is disabled, keyword recall uses SQLite FTS5 through `SQLiteFTSKeywordSearch`; FTS5 rows are derived from child/table/OCR rows in `document_chunk`.
- Query understanding runs before quick retrieval when `QUERY_UNDERSTANDING_ENABLED=true`. It keeps the raw query by default and can use optional LLM rewrite variants when `QUERY_REWRITE_ENABLED=true`; it does not load a static terminology dictionary.
- Reasoning-mode query expansion happens in the model's first `grep_chunks` arguments. The runtime validates, bounds, executes, and deduplicates those variants without owning domain synonyms.
- Hybrid retrieval fans out to dense top 50 and BM25 top 50 by default, fuses by weighted RRF with `RRF_K=60`, `RRF_VECTOR_WEIGHT=0.7`, `RRF_KEYWORD_WEIGHT=0.3`, deduplicates by chunk id, and keeps top 30 fused candidates.
- Explicit `doc_ids` are retrieval constraints, not just post-filters: Milvus dense/BM25 expressions, SQLite FTS5, hydration, parent recall, citation verification, and final context assembly all reject chunks outside the selected documents.
- If `doc_ids` are provided and the selected child/table/OCR chunk count is at or below `DIRECT_LOAD_MAX_CHUNKS=50`, the service direct-loads those chunks from SQLite and skips dense/keyword recall for that request. Larger selections fall back to normal scoped retrieval.
- Candidate metadata preserves vector score, BM25 score, hybrid/RRF score, and optional reranker score.
- Candidate metadata also preserves matched retrieval queries for debug tracing.
- Reranking is optional and local-first. Reranked candidates below `RERANKER_THRESHOLD=0.3` are filtered; if none pass, the top reranked candidate is kept when it reaches `RERANKER_FALLBACK_MIN_SCORE=0.15`. Disabled, unavailable, or failing rerankers fall back to hybrid order.
- Remaining child/table/OCR hits are grouped by `parent_id`; each parent keeps the best child score and matched child ids.
- The LLM receives parent/table/OCR context, not only the smaller child text. Short final context windows expand to nearby same-document siblings until `CONTEXT_SHORT_CHUNK_MIN_CHARS=240` or the bounded `CONTEXT_EXPANDED_CHUNK_MAX_CHARS=1200` limit is reached.
- Sources include trace metadata such as doc id, parent id, chunk id, title path, page range, and matched child ids.
- `/rag/query` returns future-compatible raw evidence fields: `used_chunks`, empty `used_entities`, empty `graph_paths`, numeric retrieval `confidence`, citations, and optional debug details.
- With agentic retrieval enabled, `/rag/query` also returns `agent_trace`, `tool_calls`, and `evidence_summary`.
- If `/rag/query` has no usable evidence, it returns an explicit insufficient-evidence answer rather than an unsupported factual answer.
- Chat streaming also emits a user-facing reasoning summary after sources and before answer tokens. This is an audit summary, not hidden chain-of-thought: it includes normalized query, retrieval query variants, applied terminology, evidence snippets, and source scores.
- With `CHAT_AGENTIC_WORKFLOW_ENABLED=true`, chat streaming emits a reasoning summary derived from agent evidence and streams the answer from the agent-built context.

## Enterprise Evaluation Strategy

The evaluation suite replays curated questions through the existing query service instead of becoming another retrieval path. It calls `RAGService.answer_query()` so the active runtime configuration decides whether a case measures Raw RAG or Agentic Retrieval behavior.

Evalsets live outside `backend/data/` and are never indexed as knowledge documents. A run captures answer text, citations, used chunks, used entities, graph paths, confidence, agent trace, tool calls, evidence summary, debug metadata, latency, and metric scores. `CitationVerifier` and SQLite `document_chunk` remain the source of truth for citation and graph source traceability.

Rule-based metrics are deterministic and CI-friendly:

- citation resolvability
- required source coverage
- expected answer terms
- graph path `source_chunk_id` traceability
- expected and forbidden tool usage
- insufficient-evidence behavior
- latency capture

JSON and Markdown reports are generated from stored run results. Evaluation does not write feedback files, update memory, rebuild vector stores, or modify graph data.

Evidence: `backend/app/services/retrieval/rag_service.py`, `backend/app/services/retrieval/vector_store.py`, `backend/app/services/retrieval/reranker.py`.

### Tradeoff

RRF avoids fragile score calibration across dense and BM25 search, but the resulting score is a rank-fusion score rather than an absolute confidence value.

## Prompting Strategy

- The system prompt comes from env.
- Retrieved parent chunks are stitched into a numbered context block.
- The user prompt explicitly tells the model to admit when context is insufficient.
- Streaming uses OpenAI chat completions directly.
- For conservative how-to/procedure questions, the user prompt adds answer-style guidance requiring source-grounded Markdown sections, ordered steps, fenced command/config code blocks when commands appear in context, and explicit `无法确定` language when evidence is missing.
- How-to guidance must not authorize the model to invent unsupported install flags, commands, versions, URLs, or prerequisites.

Evidence: `backend/app/main.py:68-82`, `backend/app/services/retrieval/rag_service.py:146-154`, `backend/app/services/retrieval/rag_service.py:280-299`.

## Feedback Strategy

- The frontend can submit a corrected answer tied to a past user question.
- The backend writes a markdown file into `data/feedback`.
- That markdown is split into parent-child chunks and upserted immediately.
- The correction then becomes part of later retrieval results.

Evidence: `backend/app/services/retrieval/rag_service.py:230-274`.

## File Parsing Strategy

Supported formats currently include text, markdown, HTML, CSV, JSON, DOC, DOCX, Excel, and PDF. Evidence: `backend/app/services/documents/document_loader.py`.

Notable parser behavior:

- CSV is flattened into pipe-delimited lines.
- JSON is normalized via pretty-printed `json.dumps`.
- DOC uses heuristic text extraction from decoded binary text.
- DOCX extracts paragraphs and tables with `python-docx`.
- HTML extracts visible text while skipping script/style/noscript content.
- Excel flattens sheets, headers, and row values into text lines.
- PDF prefers `pymupdf4llm` markdown extraction and disables layout mode when available.
- PDF, Markdown, and HTML chunks preserve markdown header boundaries before recursive splitting where possible.

Evidence: `backend/app/services/documents/document_loader.py:24-45`, `backend/app/services/documents/document_loader.py:48-119`, `backend/app/services/documents/document_loader.py:139-174`.

## Constraints For Future Changes

- Preserve path traversal protection in `_resolve_source_path`.
- Keep route handlers thin unless there is a strong reason otherwise.
- If incremental ingest is added later, document how old chunks are retired.
- If provider abstraction is added, keep `VectorStore` and streaming callsites behind clear interfaces.

## Milvus/Docling Structured Pipeline

The active pipeline now uses these boundaries:

- `DocumentParser.parse(file_path) -> ParsedDocument` is the parser contract.
- `ParsedDocument` contains `doc_id`, `file_name`, `file_type`, and structured elements.
- Each element carries type, text, Markdown, HTML, page range, heading level, title path, and metadata.
- PDF and DOCX parsing should use Docling first; fallback parsing must still produce the same `ParsedDocument` shape.
- `DocumentChunker` creates parent chunks from section/title boundaries and child chunks from smaller paragraph-sized content.
- Table chunks are not split arbitrarily. SQLite stores caption, nearby explanation, Markdown, HTML, page range, title path, and generated summary. Milvus indexes table chunks using `title_path + caption + summary + content_markdown`.
- `EmbeddingProvider` owns embedding calls and supports batch embedding.
- SQLite stores `document` and `document_chunk`; `document_chunk_fts` is a derived FTS5 keyword index for child/table/OCR chunks.
- Milvus stores only chunk vectors and filter metadata.
- `retrieval_models.py` defines provider protocols for vector index access, keyword search, evidence lookup, reranking, context building, and LLM answer generation. The current raw keyword providers are `MilvusKeywordSearch` and `SQLiteFTSKeywordSearch`.

Enhanced retrieval flow:

```text
question
  -> query understanding and terminology expansion
  -> Milvus dense top 50 per retrieval query
  -> Milvus BM25 top 50 per retrieval query when enabled
  -> SQLite FTS5 top 50 per retrieval query when Milvus BM25 is disabled
  -> RRF fusion + chunk-id dedupe
  -> hydrate Milvus metadata-only hits from SQLite document_chunk
  -> preserve candidates for evidence evaluation
  -> optional local-first reranker
  -> SQLite parent/table/OCR context lookup
  -> prompt context -> streaming answer
```

Query understanding is fail-open. If it is disabled or an optional rewrite call fails, retrieval continues with the raw question. With retrieval debug enabled, `debug_info.query_understanding` shows the normalized query, bounded retrieval queries, extracted constraints, and understanding source.

The backend does not remove candidates with domain-specific regex filters. For multi-constraint tasks, the LLM performs an evidence ledger after retrieval and deep reading: each condition must be supported by evidence belonging to the same candidate or subject. Generated aliases, equivalences, and unit conversions remain hypotheses until the evidence supports them.

## Conversation And Memory Strategy

Chat memory is handled outside the document ingest pipeline:

- `ConversationRepository` persists conversation records and chat messages in SQLite.
- `ConversationService` returns a bounded recent-message window and updates rolling summaries for long conversations.
- `MemoryRepository` persists durable memories separately from uploaded documents, feedback documents, and Milvus document vectors.
- `MemoryService` recalls active memories, formats them as labeled prompt context, extracts conservative memory candidates after assistant responses, merges duplicate normalized keys, filters sensitive content, and deletes memories.

Prompt assembly order:

```text
system prompt
  -> long-term memory context
  -> conversation summary and recent turns
  -> retrieved RAG document context
  -> current question
```

Memory controls:

- `memory_enabled=false` disables long-term memory recall and extraction for a request.
- `temporary=true` also disables long-term memory recall and extraction.
- `GET /memories` lists active saved memories.
- `DELETE /memories/{memory_id}` marks a memory deleted and excludes it from future recall.

Long-term memories are not source documents, do not appear in `/documents`, and are not affected by document ingest or reindex.

## Knowledge-Base Scope And Enrichment

所有证据路径现在接受不可变 `KnowledgeBaseScope`。Dense/BM25 在 Milvus 召回前过滤，FTS5 关联权威 `document_chunk` 后过滤，parent/table hydration、GraphRetriever 和 CitationVerifier 再做范围校验。Provider 不支持显式 scope 时非默认请求失败关闭；只有旧客户端默认 KB 保留兼容调用。

KB 保存 requested/effective Provider 配置。运行时不能激活的覆盖值保留在 requested 中，并列入 `inactive_overrides`，不得静默宣称已生效。

基础索引成功后可调用 `DocumentEnrichmentService`。长文按 parent chunk token 上限分批摘要再汇总，保存来源 chunk ID、模型、版本和生成时间。LLM 超时、限流或无效 JSON 只将 `summary_status` 标为 failed，不回滚 Raw RAG。

metadata repository 只初始化空数据库或已是最终版本的数据库，不再执行历史字段回填。旧 SQLite/Milvus 结构进入 `reset_required` 后，Raw、FTS、Graph 和 Agent 证据路径都不得把混合版本数据当成可用降级。clean-rebuild 是唯一受支持的升级入口，并通过 runtime lock、确认短语、maintenance marker 和 reset manifest 管理失败边界。

每次 `/rag/query` 在 `query_log` 中记录解析后的 scope、实际工具、used chunks、响应 metadata 和结果状态；反馈写入 `answer_feedback` 并继承一个明确活动 KB。日志用于审计，不参与证据召回。
