import json
import logging
import inspect
import mimetypes
import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from openai import OpenAI

from app.schemas import (
    ChatAttachmentResponse,
    ChatMessageResponse,
    ChatRequest,
    ChatStreamStopRequest,
    ChatStreamStopResponse,
    ContinueStreamResponse,
    DocumentContentResponse,
    DocumentItem,
    DocumentsResponse,
    DocumentParseRequest,
    DocumentParseResponse,
    DocumentUploadResponse,
    EvalResultResponse,
    EvalResultsResponse,
    EvalRunCreateRequest,
    EvalRunResponse,
    EvalRunsResponse,
    FeedbackCreateRequest,
    FeedbackCreateResponse,
    IngestResponse,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseResponse,
    KnowledgeBasesResponse,
    KnowledgeBaseUpdateRequest,
    MemoriesResponse,
    MessagesLoadResponse,
    MemoryDeleteResponse,
    RagDeleteResponse,
    RagDocumentIngestResponse,
    RagDocumentUploadResponse,
    RagQueryRequest,
    RagQueryResponse,
    ChatSessionSummaryResponse,
    SessionDeleteResponse,
    SessionRenameRequest,
    SessionStopRequest,
    SessionStopResponse,
    SessionsRecentResponse,
    UploadBatchCreateRequest,
    UploadBatchResponse,
    UploadBatchSettingsUpdateRequest,
    WikiFolderCreateRequest,
    WikiFolderResponse,
    WikiFoldersResponse,
    WikiFolderUpdateRequest,
    WikiGenerationRequest,
    WikiGenerationResponse,
    WikiGenerationTaskResponse,
    WikiGenerationTasksResponse,
    WikiGraphResponse,
    WikiLogResponse,
    WikiLogsResponse,
    WikiOverviewResponse,
    WikiProcessingTaskResponse,
    WikiProcessingTasksResponse,
    WikiIssueCreateRequest,
    WikiIssueResponse,
    WikiIssuesResponse,
    WikiIssueUpdateRequest,
    WikiPageCreateRequest,
    WikiPageMoveRequest,
    WikiPageResponse,
    WikiPagesResponse,
    WikiPageUpdateRequest,
    WikiProposalApplyResponse,
    WikiProposalCreateRequest,
    WikiProposalResponse,
    WikiProposalsResponse,
    WikiSourceDocRequest,
    WorkspaceResponse,
)
from app.models.agentic_retrieval import AgenticRetrievalConfig
from app.models.agent_runtime import DEFAULT_AGENT_RUNTIME_TOOLS, AgentRuntimeConfig
from app.models.knowledge_base import KnowledgeBaseScope, ProviderReferences
from app.models.processing_config import DurableProcessingWorkerConfig, ProcessingRuntimeDefaults
from app.services.agent.agent_prompt_templates import AgentPromptCatalog, ContextPromptCatalog, PromptTemplateCatalog
from app.services.agent.agent_runtime import AgentRuntime
from app.services.agent.postgres_agent_runtime_spans import PostgresAgentRuntimeSpanRepository
from app.services.agent.agent_runtime_tools import build_default_tool_registry
from app.services.agent.agent_tools import GraphRetrieverTool, KeywordSearchTool, RawRAGTool
from app.services.agent.agentic_workflow import AgenticRetrievalWorkflow
from app.services.retrieval.citation_verifier import CitationVerifier
from app.services.documents.document_chunker import DocumentChunker
from app.services.memory.postgres_conversation_repository import PostgresConversationRepository
from app.services.memory.conversation_service import ConversationService
from app.services.memory.principal import principal_from_request
from app.services.documents.document_parser import PARSER_REGISTRY, RegistryDocumentParser
from app.services.documents.postgres_document_repository import PostgresDocumentRepository
from app.services.documents.document_enrichment import (
    DocumentEnrichmentService,
    OpenAIDocumentEnrichmentProvider,
    PromptBackedOpenAIDocumentEnrichmentProvider,
)
from app.services.documents.postgres_image_repository import PostgresImageRepository
from app.services.retrieval.embedding_provider import OpenAIEmbeddingProvider
from app.services.kg.entity_resolver import BaselineEntityResolver
from app.services.kg.entity_vector_store import PostgresEntityVectorStore
from app.services.evaluation.evaluation_dataset_loader import EvaluationDatasetLoader
from app.services.evaluation.evaluation_metrics import RuleBasedEvaluationScorer
from app.services.evaluation.postgres_evaluation_repository import PostgresEvaluationRepository
from app.services.evaluation.evaluation_reporter import EvaluationReporter
from app.services.evaluation.evaluation_runner import EvaluationRunner, EvaluationService
from app.services.kg.graph_store import Neo4jGraphStore, UnavailableGraphStore
from app.services.kg.graph_retriever import GraphRetriever
from app.services.kg.kg_extractor import OpenAIKGExtractor
from app.services.kg.postgres_kg_repository import PostgresKGRepository
from app.services.kg.kg_service import KGEnrichmentService
from app.services.knowledge.postgres_audit_repository import PostgresKnowledgeAuditRepository
from app.services.knowledge.postgres_knowledge_base_repository import PostgresKnowledgeBaseRepository
from app.services.knowledge.knowledge_base_service import KnowledgeBaseService, KnowledgeBaseValidationError
from app.services.wiki.postgres_wiki_repository import PostgresWikiRepository
from app.services.wiki.wiki_service import WikiPageService, WikiValidationError
from app.services.wiki.wiki_ingest_service import WikiIngestConfig, WikiIngestService
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings, StorageResetRequired
from app.services.storage.storage_reset import clear_runtime_lock, write_runtime_lock
from app.services.documents.temporary_attachment_repository import TemporaryAttachmentRepository
from app.services.documents.postgres_upload_batch_repository import PostgresUploadBatchRepository
from app.services.memory.postgres_memory_repository import PostgresMemoryRepository
from app.services.memory.memory_service import MemoryService
from app.services.infrastructure.observability import configure_observability_from_env, get_observability_sink, use_observability_trace
from app.services.processing.processing_trace import ProcessingTraceRecorder
from app.services.processing.postgres_processing_span_repository import PostgresProcessingSpanRepository
from app.services.processing.processing_span_tracker import ProcessingSpanTracker
from app.services.processing.postgres_processing_task_repository import PostgresProcessingTaskRepository
from app.services.processing.processing_worker import DocumentProcessingWorker
from app.services.async_runtime.config import AsyncRuntimeConfig
from app.services.async_runtime.diagnostics import async_runtime_diagnostics
from app.services.async_runtime.queue import CeleryProcessingQueue, DisabledProcessingQueue
from app.services.async_runtime.reconciliation import reconcile_processing_queue
from app.services.agent.runtime_skills import RuntimeSkillsManager
from app.services.infrastructure.logging_config import (
    configure_logging_from_env,
    generate_trace_id,
    get_trace_id,
    reset_trace_id,
    sanitize_headers,
    sanitize_trace_id,
    set_trace_id,
    trace_context,
)
from app.services.agent.query_router import QueryRouter
from app.services.retrieval.query_understanding import (
    OpenAIQueryIntentClient,
    OpenAIQueryRewriteClient,
    QueryUnderstandingConfig,
    QueryUnderstandingService,
)
from app.services.retrieval.rag_service import RAGService
from app.services.retrieval.rag_config import load_rag_config
from app.services.retrieval.reranker import build_reranker
from app.services.agent.retrieval_planner import RetrievalPlanner
from app.services.retrieval.postgres_vector_store import PostgresVectorStore
from app.services.storage.postgres import PostgresDatabase, PostgresSettings
from app.services.chat_streaming.event_bus import ChatEventBus, ChatStreamEvent
from app.services.chat_streaming.stream_manager import MemoryStreamManager, RedisStreamManager, StreamIdentity
from app.services.chat_pipeline import (
    ChatPipelineContext,
    ChatPipelineRequest,
    ChatPipelineRuntime,
    run_quick_rag_pipeline,
)

load_dotenv()
configure_logging_from_env()
logger = logging.getLogger(__name__)
observability_sink = configure_observability_from_env()
logger.info("observability.langfuse.status", extra={"langfuse": observability_sink.status().to_dict()})

app = FastAPI(title="RAG Backend", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_observability_middleware(request: Request, call_next):
    incoming_trace_id = request.headers.get("X-Trace-ID") or request.headers.get("X-Request-ID")
    trace_id = sanitize_trace_id(incoming_trace_id) if incoming_trace_id else generate_trace_id()
    token = set_trace_id(trace_id)
    started = time.perf_counter()
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    request_extra = {
        "method": request.method,
        "path": path,
        "client_ip": request.client.host if request.client else "",
        "content_type": request.headers.get("content-type", ""),
        "content_length": request.headers.get("content-length", ""),
    }
    logger.info("request.start", extra=request_extra)
    logger.debug("request.headers", extra={"headers": sanitize_headers(request.headers)})
    with get_observability_sink().trace(
        name=f"{request.method} {request.url.path}",
        trace_id=trace_id,
        input={"method": request.method, "path": request.url.path, "query": request.url.query},
        metadata=request_extra,
        tags=["http", request.method.lower()],
    ):
        try:
            response = await call_next(request)
        except HTTPException:
            raise
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "request.failed",
                extra={
                    **request_extra,
                    "status_code": 500,
                    "duration_ms": duration_ms,
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                },
            )
            reset_trace_id(token)
            raise

    duration_ms = int((time.perf_counter() - started) * 1000)
    response.headers["X-Trace-ID"] = get_trace_id()
    end_extra = {
        **request_extra,
        "status_code": response.status_code,
        "duration_ms": duration_ms,
        "response_content_type": response.headers.get("content-type", ""),
    }
    if response.status_code >= 500:
        logger.error("request.end", extra=end_extra)
    else:
        logger.info("request.end", extra=end_extra)
    reset_trace_id(token)
    return response


def _get_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _get_env_float(*names: str, default: float) -> float:
    value = _get_env(*names, default=str(default))
    try:
        return float(value)
    except ValueError:
        logger.warning("Invalid float env value for %s=%r, fallback to %s", ",".join(names), value, default)
        return default


def _get_env_int(*names: str, default: int) -> int:
    value = _get_env(*names, default=str(default))
    try:
        return int(value)
    except ValueError:
        logger.warning("Invalid int env value for %s=%r, fallback to %s", ",".join(names), value, default)
        return default


def _get_env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _get_env_mapping(name: str) -> dict[str, str]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return {}
    result: dict[str, str] = {}
    for item in raw.split(";"):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            result[key] = value
    return result


def _get_env_bool(*names: str, default: bool = False) -> bool:
    value = _get_env(*names, default="true" if default else "false").lower()
    return value in {"1", "true", "yes", "on"}


def _build_chat_stream_manager():
    manager_type = _get_env("STREAM_MANAGER_TYPE", default="memory").lower()
    ttl_seconds = _get_env_int("STREAM_EVENT_TTL_SECONDS", "CHAT_STREAM_TTL_SECONDS", default=24 * 60 * 60)
    if manager_type == "redis":
        redis_url = _get_env("REDIS_URL", "STREAM_MANAGER_REDIS_URL", "ASYNC_RUNTIME_BROKER_URL")
        if not redis_url:
            raise RuntimeError("STREAM_MANAGER_TYPE=redis requires REDIS_URL or STREAM_MANAGER_REDIS_URL")
        logger.info(
            "chat_stream_manager.redis_enabled",
            extra={"key_prefix": "stream:events", "ttl_seconds": ttl_seconds},
        )
        return RedisStreamManager(redis_url, key_prefix="stream:events", ttl_seconds=ttl_seconds)
    logger.warning(
        "chat_stream_manager.memory_enabled",
        extra={
            "warning": "MemoryStreamManager is for local single-process streaming; refresh replay and stop propagation are not guaranteed across restarts or replicas.",
        },
    )
    return MemoryStreamManager()


def _raise_internal_error(message: str, exc: Exception) -> None:
    logger.exception("%s: %s", message, exc)
    raise HTTPException(status_code=500, detail=f"{message}: {exc}") from exc


def _build_async_runtime_config(raw: dict | None = None) -> AsyncRuntimeConfig:
    raw = dict(raw or {})
    raw.update(
        {
            "enabled": _get_env_bool("ASYNC_RUNTIME_ENABLED", default=bool(raw.get("enabled", False))),
            "mode": _get_env("ASYNC_RUNTIME_MODE", default=str(raw.get("mode", "local"))),
            "broker_url": _get_env("REDIS_URL", "ASYNC_RUNTIME_BROKER_URL", default=str(raw.get("broker_url", ""))),
            "result_backend_url": _get_env("ASYNC_RUNTIME_RESULT_BACKEND_URL", default=str(raw.get("result_backend_url", ""))),
            "local_fallback_enabled": _get_env_bool(
                "ASYNC_RUNTIME_LOCAL_FALLBACK_ENABLED",
                default=bool(raw.get("local_fallback_enabled", True)),
            ),
            "local_worker_enabled": _get_env_bool(
                "ASYNC_RUNTIME_LOCAL_WORKER_ENABLED",
                default=bool(raw.get("local_worker_enabled", True)),
            ),
            "default_max_retries": _get_env_int(
                "ASYNC_RUNTIME_DEFAULT_MAX_RETRIES",
                default=int(raw.get("default_max_retries", 3)),
            ),
            "default_time_limit_seconds": _get_env_int(
                "ASYNC_RUNTIME_DEFAULT_TIME_LIMIT_SECONDS",
                default=int(raw.get("default_time_limit_seconds", 3600)),
            ),
            "default_soft_time_limit_seconds": _get_env_int(
                "ASYNC_RUNTIME_DEFAULT_SOFT_TIME_LIMIT_SECONDS",
                default=int(raw.get("default_soft_time_limit_seconds", 3300)),
            ),
            "core_queue": _get_env("ASYNC_RUNTIME_CORE_QUEUE", default=str(raw.get("core", {}).get("queue", "core"))),
            "core_concurrency": _get_env_int(
                "ASYNC_RUNTIME_CORE_CONCURRENCY",
                default=int(raw.get("core", {}).get("concurrency", 8)),
            ),
            "postprocess_queue": _get_env(
                "ASYNC_RUNTIME_POSTPROCESS_QUEUE",
                default=str(raw.get("postprocess", {}).get("queue", "postprocess")),
            ),
            "postprocess_concurrency": _get_env_int(
                "ASYNC_RUNTIME_POSTPROCESS_CONCURRENCY",
                default=int(raw.get("postprocess", {}).get("concurrency", 2)),
            ),
            "enrichment_queue": _get_env(
                "ASYNC_RUNTIME_ENRICHMENT_QUEUE",
                default=str(raw.get("enrichment", {}).get("queue", "enrichment")),
            ),
            "enrichment_concurrency": _get_env_int(
                "ASYNC_RUNTIME_ENRICHMENT_CONCURRENCY",
                default=int(raw.get("enrichment", {}).get("concurrency", 12)),
            ),
            "maintenance_queue": _get_env(
                "ASYNC_RUNTIME_MAINTENANCE_QUEUE",
                default=str(raw.get("maintenance", {}).get("queue", "maintenance")),
            ),
            "maintenance_concurrency": _get_env_int(
                "ASYNC_RUNTIME_MAINTENANCE_CONCURRENCY",
                default=int(raw.get("maintenance", {}).get("concurrency", 4)),
            ),
            "shared_queue": _get_env("ASYNC_RUNTIME_SHARED_QUEUE", default=str(raw.get("shared", {}).get("queue", "shared"))),
            "shared_concurrency": _get_env_int(
                "ASYNC_RUNTIME_SHARED_CONCURRENCY",
                default=int(raw.get("shared", {}).get("concurrency", 6)),
            ),
            "wiki_queue": _get_env("ASYNC_RUNTIME_WIKI_QUEUE", default=str(raw.get("wiki", {}).get("queue", "wiki"))),
            "wiki_concurrency": _get_env_int(
                "ASYNC_RUNTIME_WIKI_CONCURRENCY",
                default=int(raw.get("wiki", {}).get("concurrency", 8)),
            ),
        }
    )
    return AsyncRuntimeConfig.from_settings(raw)


def _build_async_processing_queue(config: AsyncRuntimeConfig):
    if not config.celery_enabled:
        return DisabledProcessingQueue()
    try:
        from app.workers.celery_app import celery_app

        return CeleryProcessingQueue(celery_app, config)
    except Exception as exc:
        if not config.local_fallback_enabled:
            raise
        logger.warning(
            "async_runtime.celery_unavailable_falling_back",
            extra={"error_type": exc.__class__.__name__, "error_message": str(exc)},
        )
        return DisabledProcessingQueue()


def _run_background_with_trace(trace_id: str, operation: str, func, *args, **kwargs) -> None:
    with trace_context(trace_id):
        with use_observability_trace(trace_id, name=f"background.{operation}"):
            started = time.perf_counter()
            logger.info("background.start", extra={"operation": operation})
            try:
                func(*args, **kwargs)
            except Exception as exc:
                logger.exception(
                    "background.failed",
                    extra={
                        "operation": operation,
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        "error_type": exc.__class__.__name__,
                        "error_message": str(exc),
                    },
                )
                raise
            logger.info(
                "background.end",
                extra={"operation": operation, "duration_ms": int((time.perf_counter() - started) * 1000)},
            )


def build_rag_service() -> RAGService:
    rag_config = load_rag_config(os.getenv("RAG_CONFIG_PATH") or None)["rag"]
    prompt_template_catalog = PromptTemplateCatalog.load_directory(
        _get_env("PROMPT_TEMPLATE_DIR", default="config/prompt_templates")
    )
    knowledge_base_config = rag_config.get("knowledge_base", {})
    knowledge_base_defaults = DefaultKnowledgeBaseSettings(
        workspace_id=_get_env(
            "DEFAULT_WORKSPACE_ID", default=str(knowledge_base_config.get("default_workspace_id", "default-workspace"))
        ),
        workspace_name=_get_env(
            "DEFAULT_WORKSPACE_NAME", default=str(knowledge_base_config.get("default_workspace_name", "默认工作空间"))
        ),
        knowledge_base_id=_get_env(
            "DEFAULT_KNOWLEDGE_BASE_ID",
            default=str(knowledge_base_config.get("default_knowledge_base_id", "default-knowledge-base")),
        ),
        knowledge_base_name=_get_env(
            "DEFAULT_KNOWLEDGE_BASE_NAME",
            default=str(knowledge_base_config.get("default_knowledge_base_name", "默认知识库")),
        ),
    )
    api_key = _get_env("OPENAI_API_KEY", "api_key")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing. Please set it in backend/.env")

    base_url = _get_env("OPENAI_BASE_URL", "base_url")
    client = OpenAI(api_key=api_key, base_url=base_url or None)

    embedding_model = _get_env("OPENAI_EMBEDDING_MODEL", default=str(rag_config["embedding"].get("model", "text-embedding-3-small")))
    embedding_client = OpenAI(api_key=api_key, base_url=base_url or None)
    embedding_provider = OpenAIEmbeddingProvider(client=embedding_client, model=embedding_model)

    vector_state_dir = _get_env("VECTOR_STORE_DIR", "CHROMA_DIR", default="./vector_db")
    reset_state_dir = Path(
        _get_env("STORAGE_RESET_STATE_DIR", default=str(Path(vector_state_dir) / "reset-state"))
    )
    maintenance_path = reset_state_dir / "maintenance.json"
    if maintenance_path.exists():
        raise StorageResetRequired(
            f"Knowledge storage is in maintenance mode; inspect {maintenance_path} and rerun clean-rebuild"
        )
    postgres_database = PostgresDatabase(PostgresSettings.from_env())
    postgres_schema = postgres_database.settings.schema
    embedding_dim = int(os.getenv("EMBEDDING_DIM", "1536"))
    pgvector_type = _get_env("PGVECTOR_TYPE", "POSTGRES_VECTOR_TYPE", default="vector")
    vector_store = PostgresVectorStore(
        database=postgres_database,
        embedding_dim=embedding_dim,
        embedding_provider=embedding_provider,
        state_dir=vector_state_dir,
        schema=postgres_schema,
        vector_type=pgvector_type,
    )
    document_repository = PostgresDocumentRepository(postgres_database, defaults=knowledge_base_defaults, schema=postgres_schema)
    knowledge_base_repository = PostgresKnowledgeBaseRepository(
        postgres_database,
        defaults=knowledge_base_defaults,
        schema=postgres_schema,
    )
    upload_batch_repository = PostgresUploadBatchRepository(
        postgres_database,
        defaults=knowledge_base_defaults,
        schema=postgres_schema,
    )
    if getattr(vector_store, "reset_required", False):
        knowledge_base_repository.update_knowledge_base(
            knowledge_base_defaults.knowledge_base_id, {"reset_required": 1}
        )
    knowledge_base_service = KnowledgeBaseService(
        knowledge_base_repository,
        default_providers=ProviderReferences(
            parser=str(rag_config.get("parser", {}).get("type", "docling")),
            embedding=str(rag_config.get("embedding", {}).get("provider", "openai")),
            reranker=str(rag_config.get("reranker", {}).get("provider", "local")),
            vector_store=str(rag_config.get("vector_store", {}).get("type", "postgres_pgvector")),
            enrichment=str(rag_config.get("llm", {}).get("provider", "openai")),
        ),
    )
    wiki_repository = PostgresWikiRepository(postgres_database, defaults=knowledge_base_defaults, schema=postgres_schema)
    wiki_page_service = WikiPageService(
        wiki_repository,
        knowledge_base_service,
        document_repository,
        generation_enabled=_get_env_bool("WIKI_GENERATION_ENABLED", default=True),
        generation_max_pages_per_document=_get_env_int("WIKI_GENERATION_MAX_PAGES_PER_DOCUMENT", default=1),
        generation_max_source_chunks=_get_env_int("WIKI_GENERATION_MAX_SOURCE_CHUNKS", default=8),
        generation_max_chars=_get_env_int("WIKI_GENERATION_MAX_CHARS", default=12000),
    )
    reranker_enabled = _get_env_bool("RERANKER_ENABLED", default=False)
    reranker_provider = _get_env("RERANKER_PROVIDER", default=str(rag_config["reranker"].get("provider", "local")))
    reranker_model = _get_env("RERANKER_MODEL", default=str(rag_config["reranker"].get("model", "BAAI/bge-reranker-v2-m3")))
    reranker_timeout_seconds = _get_env_float("RERANKER_TIMEOUT_SECONDS", default=5.0)

    data_dir = os.getenv("RAG_DATA_DIR", "./data")
    query_rewrite_enabled = _get_env_bool("QUERY_REWRITE_ENABLED", default=False)
    query_intent_detection_enabled = _get_env_bool("QUERY_INTENT_DETECTION_ENABLED", default=False)
    query_understanding = QueryUnderstandingService(
        config=QueryUnderstandingConfig(
            enabled=_get_env_bool("QUERY_UNDERSTANDING_ENABLED", default=True),
            rewrite_enabled=query_rewrite_enabled,
            intent_detection_enabled=query_intent_detection_enabled,
            max_queries=_get_env_int("QUERY_REWRITE_MAX_QUERIES", "QUERY_UNDERSTANDING_MAX_QUERIES", default=5),
            language=_get_env("QUERY_UNDERSTANDING_LANGUAGE", default="zh-CN"),
        ),
        intent_client=OpenAIQueryIntentClient(
            client,
            _get_env("OPENAI_CHAT_MODEL", default=str(rag_config["llm"].get("model", "gpt-4o-mini"))),
            prompt_catalog=prompt_template_catalog,
        )
        if query_intent_detection_enabled
        else None,
        rewrite_client=OpenAIQueryRewriteClient(
            client,
            _get_env("OPENAI_CHAT_MODEL", default=str(rag_config["llm"].get("model", "gpt-4o-mini"))),
            prompt_catalog=prompt_template_catalog,
        )
        if query_rewrite_enabled
        else None,
    )
    chat_model = _get_env("OPENAI_CHAT_MODEL", default=str(rag_config["llm"].get("model", "gpt-4o-mini")))
    kg_extraction_enabled = _get_env_bool("KG_EXTRACTION_ENABLED", default=False)
    kg_service = None
    if kg_extraction_enabled:
        kg_repository = PostgresKGRepository(
            postgres_database,
            defaults=knowledge_base_defaults,
            schema=postgres_schema,
        )
        entity_vector_provider = None
        if _get_env_bool("KG_ENTITY_VECTOR_ENABLED", default=False):
            entity_vector_provider = PostgresEntityVectorStore(
                database=postgres_database,
                embedding_dim=embedding_dim,
                embedding_provider=embedding_provider,
                schema=postgres_schema,
                vector_type=pgvector_type,
            )
        graph_store = None
        if _get_env_bool("KG_GRAPH_ENABLED", default=False):
            try:
                graph_store = Neo4jGraphStore(
                    uri=_get_env("NEO4J_URI", default="bolt://localhost:7687"),
                    auth=(
                        _get_env("NEO4J_USER", default="neo4j"),
                        _get_env("NEO4J_PASSWORD", default="password"),
                    ),
                )
            except Exception as exc:
                reason = f"Neo4j graph store is unavailable: {exc}"
                logger.warning(reason)
                graph_store = UnavailableGraphStore(reason)
        kg_service = KGEnrichmentService(
            repository=kg_repository,
            extractor=OpenAIKGExtractor(
                client=client,
                model=_get_env("KG_EXTRACTOR_MODEL", "OPENAI_CHAT_MODEL", default=chat_model),
                extractor_version=_get_env("KG_EXTRACTOR_VERSION", default="kg-v1"),
                prompt_catalog=prompt_template_catalog,
            ),
            resolver=BaselineEntityResolver(entity_vector_provider=entity_vector_provider),
            entity_vector_provider=entity_vector_provider,
            graph_store=graph_store,
        )
    graph_retriever = None
    if _get_env_bool("GRAPH_RETRIEVER_ENABLED", default=False):
        try:
            graph_provider = Neo4jGraphStore(
                uri=_get_env("NEO4J_URI", default="bolt://localhost:7687"),
                auth=(
                    _get_env("NEO4J_USER", default="neo4j"),
                    _get_env("NEO4J_PASSWORD", default="password"),
                ),
            )
            graph_retriever = GraphRetriever(
                graph_provider=graph_provider,
                evidence_repository=document_repository,
                max_neighbor_depth=_get_env_int("GRAPH_RETRIEVER_MAX_NEIGHBOR_DEPTH", default=3),
                max_path_depth=_get_env_int("GRAPH_RETRIEVER_MAX_PATH_DEPTH", default=5),
                entity_limit=_get_env_int("GRAPH_RETRIEVER_ENTITY_LIMIT", default=10),
                relation_limit=_get_env_int("GRAPH_RETRIEVER_RELATION_LIMIT", default=50),
                path_limit=_get_env_int("GRAPH_RETRIEVER_PATH_LIMIT", default=10),
            )
        except Exception as exc:
            logger.warning("GraphRetriever is unavailable: %s", exc)

    chat_agentic_workflow_enabled = _get_env_bool("CHAT_AGENTIC_WORKFLOW_ENABLED", default=False)
    document_enrichment_enabled = _get_env_bool("DOCUMENT_ENRICHMENT_ENABLED", default=False)
    document_enrichment_service = DocumentEnrichmentService(
        document_repository,
        PromptBackedOpenAIDocumentEnrichmentProvider(
            client,
            _get_env("DOCUMENT_ENRICHMENT_MODEL", "OPENAI_CHAT_MODEL", default=chat_model),
            max_summary_chars=_get_env_int("DOCUMENT_ENRICHMENT_MAX_SUMMARY_CHARS", default=1200),
            prompt_catalog=prompt_template_catalog,
        )
        if document_enrichment_enabled
        else None,
        enabled=document_enrichment_enabled,
        max_batch_tokens=_get_env_int("DOCUMENT_ENRICHMENT_MAX_BATCH_TOKENS", default=6000),
        max_retries=_get_env_int("DOCUMENT_ENRICHMENT_MAX_RETRIES", default=2),
        asynchronous=_get_env_bool("DOCUMENT_ENRICHMENT_ASYNC", default=True),
    )
    rag_service = RAGService(
        vector_store=vector_store,
        llm_client=client,
        chat_model=chat_model,
        system_prompt=os.getenv(
            "SYSTEM_PROMPT",
            "You are a RAG assistant. Answer from retrieved context first. "
            "If context is insufficient, say so clearly and provide next steps.",
        ),
        data_dir=data_dir,
        top_k=int(os.getenv("TOP_K", "4")),
        min_relevance_score=_get_env_float("MIN_RELEVANCE_SCORE", default=0.30),
        chunk_size=int(os.getenv("CHUNK_SIZE", "700")),
        chunk_overlap=int(os.getenv("CHUNK_OVERLAP", "120")),
        context_template_path=_get_env("RAG_CONTEXT_TEMPLATE_PATH", default="config/prompt_templates/context_template.yaml"),
        context_template_id=_get_env("RAG_CONTEXT_TEMPLATE_ID", default="qa_context"),
        milvus_bm25_enabled=False,
        dense_recall_top_n=_get_env_int("DENSE_RECALL_TOP_N", default=int(rag_config["retrieval"].get("dense_top_k", 50))),
        bm25_recall_top_n=_get_env_int("BM25_RECALL_TOP_N", default=int(rag_config["retrieval"].get("keyword_top_k", 50))),
        fusion_top_k=_get_env_int("FUSION_TOP_K", default=int(rag_config["retrieval"].get("fusion_top_k", 30))),
        rrf_k=_get_env_int("RRF_K", default=int(rag_config["retrieval"].get("rrf_k", 60))),
        rrf_vector_weight=_get_env_float(
            "RRF_VECTOR_WEIGHT", default=float(rag_config["retrieval"].get("rrf_vector_weight", 0.7))
        ),
        rrf_keyword_weight=_get_env_float(
            "RRF_KEYWORD_WEIGHT", default=float(rag_config["retrieval"].get("rrf_keyword_weight", 0.3))
        ),
        reranker_threshold=_get_env_float(
            "RERANKER_THRESHOLD", default=float(rag_config["retrieval"].get("reranker_threshold", 0.3))
        ),
        reranker_fallback_min_score=_get_env_float(
            "RERANKER_FALLBACK_MIN_SCORE",
            default=float(rag_config["retrieval"].get("reranker_fallback_min_score", 0.15)),
        ),
        direct_load_max_chunks=_get_env_int(
            "DIRECT_LOAD_MAX_CHUNKS", default=int(rag_config["retrieval"].get("direct_load_max_chunks", 50))
        ),
        context_short_chunk_min_chars=_get_env_int(
            "CONTEXT_SHORT_CHUNK_MIN_CHARS",
            default=int(rag_config["context"].get("short_chunk_min_chars", 240)),
        ),
        context_expanded_chunk_max_chars=_get_env_int(
            "CONTEXT_EXPANDED_CHUNK_MAX_CHARS",
            default=int(rag_config["context"].get("expanded_chunk_max_chars", 1200)),
        ),
        retrieval_debug_enabled=_get_env_bool("RETRIEVAL_DEBUG_ENABLED", default=False),
        low_recall_query_expansion_enabled=_get_env_bool(
            "LOW_RECALL_QUERY_EXPANSION_ENABLED",
            default=bool(rag_config["retrieval"].get("low_recall_query_expansion_enabled", False)),
        ),
        low_recall_min_candidates=_get_env_int(
            "LOW_RECALL_MIN_CANDIDATES",
            default=int(rag_config["retrieval"].get("low_recall_min_candidates", 3)),
        ),
        low_recall_min_score=_get_env_float(
            "LOW_RECALL_MIN_SCORE",
            default=float(rag_config["retrieval"].get("low_recall_min_score", 0.2)),
        ),
        low_recall_max_queries=_get_env_int(
            "LOW_RECALL_MAX_QUERIES",
            default=int(rag_config["retrieval"].get("low_recall_max_queries", 3)),
        ),
        reranker_degradation_enabled=_get_env_bool(
            "RERANKER_DEGRADATION_ENABLED",
            default=bool(rag_config["retrieval"].get("reranker_degradation_enabled", True)),
        ),
        reranker_degraded_threshold=_get_env_float(
            "RERANKER_DEGRADED_THRESHOLD",
            default=float(rag_config["retrieval"].get("reranker_degraded_threshold", 0.15)),
        ),
        reranker_fallback_top_n=_get_env_int(
            "RERANKER_FALLBACK_TOP_N",
            default=int(rag_config["retrieval"].get("reranker_fallback_top_n", 1)),
        ),
        mmr_enabled=_get_env_bool("MMR_ENABLED", default=bool(rag_config["retrieval"].get("mmr_enabled", False))),
        mmr_lambda=_get_env_float("MMR_LAMBDA", default=float(rag_config["retrieval"].get("mmr_lambda", 0.75))),
        mmr_top_k=_get_env_int("MMR_TOP_K", default=int(rag_config["retrieval"].get("mmr_top_k", 0))),
        duplicate_overlap_threshold=_get_env_float(
            "DUPLICATE_OVERLAP_THRESHOLD",
            default=float(rag_config["retrieval"].get("duplicate_overlap_threshold", 0.92)),
        ),
        reranker_enabled=reranker_enabled,
        reranker_provider=reranker_provider,
        reranker_top_n=_get_env_int("RERANKER_TOP_N", default=int(rag_config["retrieval"].get("rerank_top_k", 8))),
        reranker_timeout_seconds=reranker_timeout_seconds,
        reranker=build_reranker(
            reranker_enabled,
            reranker_provider,
            reranker_model,
            api_key=_get_env("RERANKER_API_KEY", "DASHSCOPE_API_KEY", default=""),
            base_url=_get_env(
                "RERANKER_BASE_URL",
                "DASHSCOPE_RERANKER_URL",
                default="https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
            ),
            timeout_seconds=reranker_timeout_seconds,
        ),
        ocr_enabled=_get_env_bool("OCR_ENABLED", default=False),
        ocr_provider=_get_env("OCR_PROVIDER", default="docling"),
        document_repository=document_repository,
        knowledge_base_service=knowledge_base_service,
        wiki_page_service=wiki_page_service,
        upload_batch_repository=upload_batch_repository,
        document_parser=RegistryDocumentParser(
            engine=_get_env("PARSER_ENGINE", default="builtin"),
            force_scanned=_get_env_bool("PDF_FORCE_SCANNED", default=False),
            render_dpi=_get_env_int("PDF_RENDER_DPI", default=200),
            jpeg_quality=_get_env_int("PDF_JPEG_QUALITY", default=90),
            max_pages=_get_env_int("PDF_MAX_PAGES", default=1000),
        ),
        document_chunker=DocumentChunker(
            parent_chunk_size_chars=_get_env_int("PARENT_CHUNK_SIZE_CHARS", default=4096),
            child_chunk_size_chars=_get_env_int("CHILD_CHUNK_SIZE_CHARS", default=384),
            child_overlap_chars=_get_env_int("CHILD_CHUNK_OVERLAP_CHARS", default=76),
            strategy=_get_env("CHUNK_STRATEGY", default="auto"),
            ocr_min_confidence=_get_env_float("OCR_MIN_CONFIDENCE", default=0.0),
        ),
        query_understanding=query_understanding,
        kg_service=kg_service,
        kg_extraction_enabled=kg_extraction_enabled,
        graph_retriever=graph_retriever,
        chat_agentic_workflow_enabled=chat_agentic_workflow_enabled,
        agent_trace_stream_enabled=_get_env_bool("AGENT_TRACE_STREAM_ENABLED", default=False),
        document_enrichment_service=document_enrichment_service,
        processing_defaults=ProcessingRuntimeDefaults(
            parser_engine=_get_env("PARSER_ENGINE", default="builtin"),
            pdf_force_scanned=_get_env_bool("PDF_FORCE_SCANNED", default=False),
            pdf_render_dpi=_get_env_int("PDF_RENDER_DPI", default=200),
            pdf_jpeg_quality=_get_env_int("PDF_JPEG_QUALITY", default=90),
            pdf_max_pages=_get_env_int("PDF_MAX_PAGES", default=1000),
            pdf_max_image_edge_px=_get_env_int("PDF_MAX_IMAGE_EDGE_PX", default=2400),
            pdf_render_concurrency=_get_env_int("PDF_RENDER_CONCURRENCY", default=2),
            chunk_strategy=_get_env("CHUNK_STRATEGY", default="auto"),
            parent_chunk_size_chars=_get_env_int("PARENT_CHUNK_SIZE_CHARS", default=4096),
            child_chunk_size_chars=_get_env_int("CHILD_CHUNK_SIZE_CHARS", default=384),
            child_chunk_overlap_chars=_get_env_int("CHILD_CHUNK_OVERLAP_CHARS", default=76),
            max_protected_span_chars=_get_env_int("MAX_PROTECTED_SPAN_CHARS", default=7500),
            embedding_token_limit=_get_env_int("EMBEDDING_TOKEN_LIMIT", default=0),
            media_storage_dir=_get_env("MEDIA_STORAGE_DIR", default=str(Path(vector_state_dir) / "media")),
            media_max_bytes=_get_env_int("MEDIA_MAX_BYTES", default=25 * 1024 * 1024),
            preview_max_file_bytes=_get_env_int("PROCESSING_PREVIEW_MAX_FILE_BYTES", default=10 * 1024 * 1024),
            preview_max_pages=_get_env_int("PROCESSING_PREVIEW_MAX_PAGES", default=20),
            preview_timeout_seconds=_get_env_float("PROCESSING_PREVIEW_TIMEOUT_SECONDS", default=5.0),
            preview_max_chunks=_get_env_int("PROCESSING_PREVIEW_MAX_CHUNKS", default=50),
            ocr_enabled=_get_env_bool("OCR_ENABLED", default=False),
            ocr_provider=_get_env("OCR_PROVIDER", default="docling") if _get_env_bool("OCR_ENABLED", default=False) else "disabled",
            ocr_min_confidence=_get_env_float("OCR_MIN_CONFIDENCE", default=0.0),
            caption_enabled=_get_env_bool("CAPTION_ENABLED", default=False),
            caption_provider=_get_env("CAPTION_PROVIDER", default="disabled"),
            graph_enabled=kg_extraction_enabled,
        ),
        processing_trace_recorder=ProcessingTraceRecorder.from_env(
            Path(_get_env("PROCESSING_TRACE_DIR", default=str(Path(data_dir) / "processing_traces"))),
            span_tracker=ProcessingSpanTracker(
                PostgresProcessingSpanRepository(
                    postgres_database,
                    defaults=knowledge_base_defaults,
                    schema=postgres_schema,
                )
            ),
        ),
        audit_repository=PostgresKnowledgeAuditRepository(
            postgres_database,
            defaults=knowledge_base_defaults,
            schema=postgres_schema,
        ),
        image_repository=PostgresImageRepository(
            postgres_database,
            defaults=knowledge_base_defaults,
            schema=postgres_schema,
        ),
    )
    agentic_config = AgenticRetrievalConfig(
        enabled=_get_env_bool("AGENTIC_RETRIEVAL_ENABLED", default=False),
        chat_stream_enabled=chat_agentic_workflow_enabled,
        trace_stream_enabled=_get_env_bool("AGENT_TRACE_STREAM_ENABLED", default=False),
        max_tool_calls=_get_env_int("AGENTIC_MAX_TOOL_CALLS", default=6),
        tool_timeout_seconds=_get_env_float("AGENTIC_TOOL_TIMEOUT_SECONDS", default=10.0),
        raw_top_k=_get_env_int("AGENTIC_RAW_TOP_K", default=8),
        keyword_top_k=_get_env_int("AGENTIC_KEYWORD_TOP_K", default=8),
        graph_top_k=_get_env_int("AGENTIC_GRAPH_TOP_K", default=8),
        graph_max_depth=_get_env_int("AGENTIC_GRAPH_MAX_DEPTH", default=3),
    )
    rag_service.agent_trace_stream_enabled = agentic_config.trace_stream_enabled
    rag_service.chat_agentic_workflow_enabled = chat_agentic_workflow_enabled
    if agentic_config.enabled or agentic_config.chat_stream_enabled:
        planner = RetrievalPlanner(agentic_config)
        rag_service.agentic_workflow = AgenticRetrievalWorkflow(
            router=QueryRouter(),
            planner=planner,
            tools={
                "RawRAGTool": RawRAGTool(rag_service),
                "KeywordSearchTool": KeywordSearchTool(rag_service),
                "GraphRetrieverTool": GraphRetrieverTool(graph_retriever),
            },
            citation_verifier=CitationVerifier(document_repository),
            rag_service=rag_service,
            config=agentic_config,
        )
        rag_service.agentic_retrieval_enabled = agentic_config.enabled
    agent_runtime_config = AgentRuntimeConfig(
        enabled=_get_env_bool("AGENT_RUNTIME_ENABLED", default=False),
        prompt_template_path=_get_env("AGENT_PROMPT_TEMPLATE_PATH", default="config/prompt_templates/agent_system_prompt.yaml"),
        prompt_template_id=_get_env("AGENT_PROMPT_TEMPLATE_ID", default="progressive_rag_agent"),
        context_template_path=_get_env("AGENT_CONTEXT_TEMPLATE_PATH", default="config/prompt_templates/context_template.yaml"),
        context_template_id=_get_env("AGENT_CONTEXT_TEMPLATE_ID", default="default_context"),
        skills_enabled=_get_env_bool("AGENT_RUNTIME_SKILLS_ENABLED", default=False),
        skills_path=_get_env("AGENT_RUNTIME_SKILLS_PATH", default="runtime_skills/preloaded"),
        enabled_tools=_get_env_csv("AGENT_RUNTIME_ENABLED_TOOLS", DEFAULT_AGENT_RUNTIME_TOOLS),
        max_iterations=_get_env_int("AGENT_RUNTIME_MAX_ITERATIONS", default=6),
        max_empty_retries=_get_env_int("AGENT_RUNTIME_MAX_EMPTY_RETRIES", default=2),
        max_repeated_responses=_get_env_int("AGENT_RUNTIME_MAX_REPEATED_RESPONSES", default=2),
        max_repeated_tool_batches=_get_env_int("AGENT_RUNTIME_MAX_REPEATED_TOOL_BATCHES", default=2),
        max_llm_calls=_get_env_int("AGENT_RUNTIME_MAX_LLM_CALLS", default=8),
        max_tool_calls=_get_env_int("AGENT_RUNTIME_MAX_TOOL_CALLS", default=24),
        max_wall_clock_seconds=_get_env_float("AGENT_RUNTIME_MAX_WALL_CLOCK_SECONDS", default=120.0),
        max_parallel_workers=_get_env_int("AGENT_RUNTIME_MAX_PARALLEL_WORKERS", default=4),
        local_concurrency_enabled=_get_env_bool("AGENT_RUNTIME_LOCAL_CONCURRENCY_ENABLED", default=True),
        parallel_tool_calls_mode=_get_env("AGENT_RUNTIME_PARALLEL_TOOL_CALLS_MODE", default="auto"),
        terminal_streaming_mode=_get_env("AGENT_RUNTIME_TERMINAL_STREAMING_MODE", default="auto"),
        legacy_remedial_retrieval_enabled=_get_env_bool(
            "AGENT_RUNTIME_LEGACY_REMEDIAL_RETRIEVAL_ENABLED",
            default=False,
        ),
        max_tool_output_chars=_get_env_int("AGENT_RUNTIME_MAX_TOOL_OUTPUT_CHARS", default=6000),
        max_remedial_retrieval_attempts=_get_env_int("AGENT_REMEDIAL_RETRIEVAL_MAX_ATTEMPTS", default=1),
        reasoning_grep_first_enabled=_get_env_bool("REASONING_LLM_GREP_FIRST_ENABLED", default=True),
        quick_grep_first_enabled=_get_env_bool("QUICK_LLM_GREP_FIRST_ENABLED", default=False),
        unified_chat_runtime_enabled=_get_env_bool("CHAT_UNIFIED_RUNTIME_ENABLED", default=False),
        quick_runtime_enabled=_get_env_bool("CHAT_UNIFIED_QUICK_RUNTIME_ENABLED", default=_get_env_bool("CHAT_UNIFIED_RUNTIME_ENABLED", default=False)),
        quick_prompt_template_id=_get_env("AGENT_RUNTIME_QUICK_PROMPT_TEMPLATE_ID", default="quick_rag_agent"),
        quick_context_template_id=_get_env("AGENT_RUNTIME_QUICK_CONTEXT_TEMPLATE_ID", default="qa_context"),
        quick_enabled_tools=_get_env_csv("AGENT_RUNTIME_QUICK_ENABLED_TOOLS", ()),
        quick_max_iterations=_get_env_int("AGENT_RUNTIME_QUICK_MAX_ITERATIONS", default=1),
        quick_max_empty_retries=_get_env_int("AGENT_RUNTIME_QUICK_MAX_EMPTY_RETRIES", default=0),
        quick_max_repeated_responses=_get_env_int("AGENT_RUNTIME_QUICK_MAX_REPEATED_RESPONSES", default=0),
        quick_preload_retrieval=_get_env_bool("AGENT_RUNTIME_QUICK_PRELOAD_RETRIEVAL", default=True),
        quick_remedial_retrieval_enabled=_get_env_bool("AGENT_RUNTIME_QUICK_REMEDIAL_RETRIEVAL_ENABLED", default=False),
        wiki_runtime_enabled=_get_env_bool("AGENT_RUNTIME_WIKI_MODE_ENABLED", default=True),
        wiki_prompt_template_id=_get_env("AGENT_RUNTIME_WIKI_PROMPT_TEMPLATE_ID", default="wiki_rag_agent"),
        wiki_context_template_id=_get_env("AGENT_RUNTIME_WIKI_CONTEXT_TEMPLATE_ID", default="default_context"),
        wiki_enabled_tools=_get_env_csv(
            "AGENT_RUNTIME_WIKI_ENABLED_TOOLS",
            (
                "thinking",
                "todo_write",
                "wiki_search",
                "wiki_read_page",
                "wiki_read_source_doc",
                "wiki_flag_issue",
                "grep_chunks",
                "list_knowledge_chunks",
                "get_document_info",
            ),
        ),
        wiki_max_iterations=_get_env_int("AGENT_RUNTIME_WIKI_MAX_ITERATIONS", default=5),
        wiki_max_empty_retries=_get_env_int("AGENT_RUNTIME_WIKI_MAX_EMPTY_RETRIES", default=1),
        wiki_max_repeated_responses=_get_env_int("AGENT_RUNTIME_WIKI_MAX_REPEATED_RESPONSES", default=1),
        wiki_preload_retrieval=_get_env_bool("AGENT_RUNTIME_WIKI_PRELOAD_RETRIEVAL", default=False),
        rag_wiki_runtime_enabled=_get_env_bool("AGENT_RUNTIME_RAG_WIKI_MODE_ENABLED", default=_get_env_bool("AGENT_RUNTIME_WIKI_MODE_ENABLED", default=True)),
        rag_wiki_prompt_template_id=_get_env("AGENT_RUNTIME_RAG_WIKI_PROMPT_TEMPLATE_ID", default="hybrid_rag_wiki_agent"),
        rag_wiki_context_template_id=_get_env("AGENT_RUNTIME_RAG_WIKI_CONTEXT_TEMPLATE_ID", default="default_context"),
        rag_wiki_enabled_tools=_get_env_csv(
            "AGENT_RUNTIME_RAG_WIKI_ENABLED_TOOLS",
            (
                "thinking",
                "todo_write",
                "wiki_search",
                "wiki_read_page",
                "wiki_read_source_doc",
                "wiki_flag_issue",
                "grep_chunks",
                "knowledge_search",
                "list_knowledge_chunks",
                "get_document_info",
                "query_knowledge_graph",
            ),
        ),
        rag_wiki_max_iterations=_get_env_int("AGENT_RUNTIME_RAG_WIKI_MAX_ITERATIONS", default=6),
        rag_wiki_max_empty_retries=_get_env_int("AGENT_RUNTIME_RAG_WIKI_MAX_EMPTY_RETRIES", default=1),
        rag_wiki_max_repeated_responses=_get_env_int("AGENT_RUNTIME_RAG_WIKI_MAX_REPEATED_RESPONSES", default=1),
        rag_wiki_preload_retrieval=_get_env_bool("AGENT_RUNTIME_RAG_WIKI_PRELOAD_RETRIEVAL", default=False),
        tool_timeout_seconds=_get_env_float("AGENT_RUNTIME_TOOL_TIMEOUT_SECONDS", default=20.0),
        web_search_enabled=_get_env_bool("AGENT_RUNTIME_WEB_SEARCH_ENABLED", default=False),
        web_search_endpoint=_get_env("AGENT_RUNTIME_WEB_SEARCH_URL", default=""),
        web_fetch_enabled=_get_env_bool("AGENT_RUNTIME_WEB_FETCH_ENABLED", default=False),
        web_fetch_allowed_domains=_get_env_csv("AGENT_RUNTIME_WEB_FETCH_ALLOWED_DOMAINS", ()),
        data_analysis_enabled=_get_env_bool("AGENT_RUNTIME_DATA_ANALYSIS_ENABLED", default=False),
        database_query_enabled=_get_env_bool("AGENT_RUNTIME_DATABASE_QUERY_ENABLED", default=False),
        database_allowed_sources=_get_env_mapping("AGENT_RUNTIME_DATABASE_SOURCES"),
        wiki_tools_enabled=_get_env_bool(
            "AGENT_RUNTIME_WIKI_TOOLS_ENABLED",
            default=(
                _get_env_bool("AGENT_RUNTIME_WIKI_MODE_ENABLED", default=True)
                or _get_env_bool("AGENT_RUNTIME_RAG_WIKI_MODE_ENABLED", default=_get_env_bool("AGENT_RUNTIME_WIKI_MODE_ENABLED", default=True))
            ),
        ),
        wiki_maintenance_tools_enabled=_get_env_bool("AGENT_RUNTIME_WIKI_MAINTENANCE_TOOLS_ENABLED", default=False),
        fallback_to_deterministic=_get_env_bool("AGENT_RUNTIME_FALLBACK_TO_DETERMINISTIC", default=True),
    )
    rag_service.agent_runtime_enabled = agent_runtime_config.enabled
    rag_service.unified_chat_runtime_enabled = agent_runtime_config.unified_chat_runtime_enabled
    rag_service.quick_runtime_enabled = agent_runtime_config.quick_runtime_enabled
    rag_service.wiki_runtime_enabled = agent_runtime_config.wiki_runtime_enabled
    rag_service.rag_wiki_runtime_enabled = agent_runtime_config.rag_wiki_runtime_enabled
    if (
        agent_runtime_config.enabled
        or agent_runtime_config.quick_runtime_enabled
        or agent_runtime_config.wiki_runtime_enabled
        or agent_runtime_config.rag_wiki_runtime_enabled
    ):
        skills_manager = RuntimeSkillsManager(
            agent_runtime_config.skills_path,
            enabled=agent_runtime_config.skills_enabled,
            max_chars=_get_env_int("AGENT_RUNTIME_SKILL_MAX_CHARS", default=12000),
        )
        registered_agent_tools = tuple(
            dict.fromkeys(
                (
                    *agent_runtime_config.enabled_tools,
                    *agent_runtime_config.quick_enabled_tools,
                    *agent_runtime_config.wiki_enabled_tools,
                    *agent_runtime_config.rag_wiki_enabled_tools,
                )
            )
        )
        rag_service.agent_runtime = AgentRuntime(
            llm_client=client,
            chat_model=chat_model,
            rag_service=rag_service,
            prompt_catalog=AgentPromptCatalog.load(agent_runtime_config.prompt_template_path),
            context_catalog=ContextPromptCatalog.load(agent_runtime_config.context_template_path),
            tool_registry=build_default_tool_registry(
                enabled_tools=registered_agent_tools,
                max_output_chars=agent_runtime_config.max_tool_output_chars,
                skills_enabled=agent_runtime_config.skills_enabled,
                web_search_enabled=agent_runtime_config.web_search_enabled,
                web_search_endpoint=agent_runtime_config.web_search_endpoint,
                web_fetch_enabled=agent_runtime_config.web_fetch_enabled,
                web_fetch_allowed_domains=agent_runtime_config.web_fetch_allowed_domains,
                web_fetch_timeout_seconds=agent_runtime_config.tool_timeout_seconds,
                data_analysis_enabled=agent_runtime_config.data_analysis_enabled,
                database_query_enabled=agent_runtime_config.database_query_enabled,
                database_allowed_sources=agent_runtime_config.database_allowed_sources,
                wiki_tools_enabled=agent_runtime_config.wiki_tools_enabled,
                wiki_maintenance_tools_enabled=agent_runtime_config.wiki_maintenance_tools_enabled,
            ),
            config=agent_runtime_config,
            skills_manager=skills_manager,
            graph_retriever=graph_retriever,
            span_repository=PostgresAgentRuntimeSpanRepository(
                postgres_database,
                defaults=knowledge_base_defaults,
                schema=postgres_schema,
            ),
        )
    worker_config_defaults = rag_config.get("processing_worker", {})
    async_runtime_config = _build_async_runtime_config(rag_config.get("async_runtime", {}))
    async_processing_queue = _build_async_processing_queue(async_runtime_config)
    rag_service.async_runtime_config = async_runtime_config
    rag_service.async_processing_queue = async_processing_queue
    processing_task_repository = PostgresProcessingTaskRepository(
        postgres_database,
        defaults=knowledge_base_defaults,
        schema=postgres_schema,
    )
    if document_enrichment_service is not None:
        document_enrichment_service.processing_repository = processing_task_repository
        document_enrichment_service.async_queue = async_processing_queue
    wiki_ingest_service = WikiIngestService(
        repository=wiki_repository,
        page_service=wiki_page_service,
        processing_repository=processing_task_repository,
        llm_client=client,
        model=chat_model,
        prompt_catalog=prompt_template_catalog,
        config=WikiIngestConfig(
            enabled=_get_env_bool("WIKI_INGEST_ENABLED", default=True),
            debounce_seconds=_get_env_int("WIKI_INGEST_DEBOUNCE_SECONDS", default=30),
            followup_seconds=_get_env_int("WIKI_FINALIZE_DEBOUNCE_SECONDS", default=5),
            batch_size=_get_env_int("WIKI_INGEST_BATCH_SIZE", default=5),
            map_concurrency=_get_env_int("WIKI_MAP_CONCURRENCY", default=4),
            reduce_concurrency=_get_env_int("WIKI_REDUCE_CONCURRENCY", default=4),
            max_source_chunks=_get_env_int("WIKI_MAX_SOURCE_CHUNKS", default=80),
            max_source_chars=_get_env_int("WIKI_MAX_SOURCE_CHARS", default=32768),
            max_candidates=_get_env_int("WIKI_MAX_CANDIDATES", default=24),
            max_pages_per_document=_get_env_int("WIKI_MAX_PAGES_PER_DOCUMENT", default=30),
            min_source_chars=_get_env_int("WIKI_MIN_SOURCE_CHARS", default=80),
            timeout_seconds=_get_env_int("WIKI_TASK_TIMEOUT_SECONDS", default=3600),
            max_attempts=_get_env_int("WIKI_TASK_MAX_ATTEMPTS", default=3),
            language=_get_env("WIKI_LANGUAGE", default="zh-CN"),
        ),
        span_tracker=rag_service.processing_trace_recorder.span_tracker,
        async_queue=async_processing_queue,
    )
    wiki_page_service.ingest_service = wiki_ingest_service
    rag_service.processing_worker = DocumentProcessingWorker(
        repository=processing_task_repository,
        rag_service=rag_service,
        config=DurableProcessingWorkerConfig.from_settings(
            {
                "enabled": _get_env_bool(
                    "PROCESSING_WORKER_ENABLED",
                    default=bool(worker_config_defaults.get("enabled", False)),
                ) or wiki_ingest_service.config.enabled,
                "poll_interval_seconds": _get_env_float(
                    "PROCESSING_WORKER_POLL_INTERVAL_SECONDS",
                    default=float(worker_config_defaults.get("poll_interval_seconds", 1.0)),
                ),
                "lease_timeout_seconds": _get_env_int(
                    "PROCESSING_WORKER_LEASE_TIMEOUT_SECONDS",
                    default=int(worker_config_defaults.get("lease_timeout_seconds", 300)),
                ),
                "max_concurrent_tasks": _get_env_int(
                    "PROCESSING_WORKER_MAX_CONCURRENT_TASKS",
                    default=int(worker_config_defaults.get("max_concurrent_tasks", 1)),
                ),
                "default_max_attempts": _get_env_int(
                    "PROCESSING_WORKER_DEFAULT_MAX_ATTEMPTS",
                    default=int(worker_config_defaults.get("default_max_attempts", 3)),
                ),
                "retry_backoff_seconds": _get_env(
                    "PROCESSING_WORKER_RETRY_BACKOFF_SECONDS",
                    default=str(worker_config_defaults.get("retry_backoff_seconds", "10,30,120")),
                ),
            }
        ),
        worker_id=_get_env("PROCESSING_WORKER_ID", default="local-processing-worker"),
        wiki_ingest_service=wiki_ingest_service,
        async_queue=async_processing_queue,
        start_local_worker=(not async_runtime_config.celery_enabled) or async_runtime_config.local_worker_enabled,
    )
    return rag_service


rag_service = build_rag_service()
temporary_attachment_repository = TemporaryAttachmentRepository(
    _get_env(
        "CHAT_ATTACHMENT_DIR",
        default=str(Path(_get_env("RAG_DATA_DIR", default="./data")) / "chat_attachments"),
    ),
    ttl_minutes=_get_env_int("CHAT_ATTACHMENT_TTL_MINUTES", default=60),
    max_bytes=_get_env_int("CHAT_ATTACHMENT_MAX_BYTES", default=10 * 1024 * 1024),
    max_text_chars=_get_env_int("CHAT_ATTACHMENT_MAX_TEXT_CHARS", default=120000),
)
runtime_lock_path = Path(
    _get_env("STORAGE_RUNTIME_LOCK", default=str(rag_service.vector_store.persist_dir / "runtime.lock"))
)
postgres_database = rag_service.vector_store.database
postgres_schema = rag_service.vector_store.schema
conversation_service = ConversationService(
    PostgresConversationRepository(postgres_database, schema=postgres_schema),
    recent_message_limit=_get_env_int("CONVERSATION_RECENT_MESSAGE_LIMIT", default=10),
    summary_message_threshold=_get_env_int("CONVERSATION_SUMMARY_MESSAGE_THRESHOLD", default=20),
)
memory_service = MemoryService(PostgresMemoryRepository(postgres_database, schema=postgres_schema))
chat_stream_manager = _build_chat_stream_manager()
chat_stream_cancellations: dict[str, threading.Event] = {}
chat_stream_cancellations_lock = threading.Lock()
chat_rag_pipeline_enabled = _get_env_bool("CHAT_RAG_PIPELINE_ENABLED", default=False)
eval_dataset_dir = Path(_get_env("EVAL_DATASET_DIR", default=str(Path(__file__).resolve().parents[1] / "evalsets")))
eval_report_dir = Path(_get_env("EVAL_REPORT_DIR", default=str(rag_service.vector_store.persist_dir / "eval_reports")))
evaluation_repository = PostgresEvaluationRepository(postgres_database, schema=postgres_schema)
evaluation_reporter = EvaluationReporter(eval_report_dir, evaluation_repository)
evaluation_service = EvaluationService(
    EvaluationRunner(
        rag_service=rag_service,
        repository=evaluation_repository,
        dataset_loader=EvaluationDatasetLoader([eval_dataset_dir]),
        scorer=RuleBasedEvaluationScorer(rag_service.document_repository),
        reporter=evaluation_reporter,
    ),
    evaluation_repository,
)


@app.on_event("startup")
def startup_runtime_lock() -> None:
    write_runtime_lock(runtime_lock_path)


@app.on_event("shutdown")
def shutdown_runtime_lock() -> None:
    clear_runtime_lock(runtime_lock_path)


@app.on_event("startup")
def startup_processing_worker() -> None:
    worker = getattr(rag_service, "processing_worker", None)
    if worker is not None:
        worker.start()


@app.on_event("startup")
def startup_async_runtime_reconciliation() -> None:
    worker = getattr(rag_service, "processing_worker", None)
    queue = getattr(rag_service, "async_processing_queue", None)
    if worker is None or queue is None or not getattr(queue, "enabled", False):
        return
    try:
        reconcile_processing_queue(worker.repository, queue, stale_broker_after_seconds=60)
    except Exception as exc:
        logger.warning(
            "async_runtime.startup_reconcile_failed",
            extra={"error_type": exc.__class__.__name__, "error_message": str(exc)},
        )


@app.on_event("shutdown")
def shutdown_processing_worker() -> None:
    worker = getattr(rag_service, "processing_worker", None)
    if worker is not None:
        worker.stop()


@app.on_event("startup")
def startup_ingest() -> None:
    # Re-index on startup when source files changed or db is empty.
    auto_ingest = _get_env("AUTO_INGEST_ON_STARTUP", default="true").lower() not in {"0", "false", "no", "off"}
    if not auto_ingest:
        logger.info("Skip startup ingest because AUTO_INGEST_ON_STARTUP is disabled.")
        return
    try:
        if rag_service.needs_reingest():
            rag_service.ingest()
    except Exception as exc:
        logger.exception("Startup ingest failed. Service will continue without re-ingest: %s", exc)


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "storage": {
            "database": "postgres",
            "schema": getattr(rag_service.vector_store, "schema", ""),
            "vector_store": rag_service.vector_store.__class__.__name__,
            "pgvector": {
                "type": getattr(rag_service.vector_store, "vector_type", ""),
                "dimension": getattr(rag_service.vector_store, "embedding_dim", None),
            },
            "reset_required": bool(getattr(rag_service.vector_store, "reset_required", False)),
        },
        "async_runtime": async_runtime_diagnostics(
            getattr(rag_service, "async_runtime_config", AsyncRuntimeConfig()),
            getattr(rag_service, "async_processing_queue", DisabledProcessingQueue()).health(),
        ),
        "observability": {"langfuse": get_observability_sink().status().to_dict()},
    }


@app.get("/observability/status")
def observability_status() -> dict:
    return {"langfuse": get_observability_sink().status().to_dict()}


def _knowledge_base_service() -> KnowledgeBaseService:
    service = getattr(rag_service, "knowledge_base_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Knowledge base service is unavailable")
    return service


def _resolve_request_scope(knowledge_base_ids: list[str] | None = None, document_ids: list[str] | None = None):
    resolver = getattr(rag_service, "resolve_scope", None)
    if callable(resolver):
        return resolver(knowledge_base_ids, document_ids)
    if knowledge_base_ids:
        raise HTTPException(status_code=400, detail="Knowledge base selection is unavailable")
    return KnowledgeBaseScope(
        "default-workspace",
        ("default-knowledge-base",),
        tuple(document_ids or ()),
        compatibility_default=True,
    )


def _wiki_service() -> WikiPageService:
    service = getattr(rag_service, "wiki_page_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Wiki service is unavailable")
    return service


def _payload_dict(payload, *, exclude_unset: bool = False) -> dict:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(exclude_unset=exclude_unset)
    return payload.dict(exclude_unset=exclude_unset)


@app.get("/workspaces/default", response_model=WorkspaceResponse)
def get_default_workspace() -> WorkspaceResponse:
    service = _knowledge_base_service()
    workspace = service.repository.get_workspace(service.default_workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Default workspace not found")
    return WorkspaceResponse(**workspace.to_dict())


@app.get("/knowledge-bases", response_model=KnowledgeBasesResponse)
def list_knowledge_bases(
    workspace_id: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
) -> KnowledgeBasesResponse:
    service = _knowledge_base_service()
    return KnowledgeBasesResponse(
        items=[KnowledgeBaseResponse(**item.to_dict()) for item in service.list(workspace_id, include_archived)]
    )


@app.post("/knowledge-bases", response_model=KnowledgeBaseResponse, status_code=201)
def create_knowledge_base(payload: KnowledgeBaseCreateRequest) -> KnowledgeBaseResponse:
    service = _knowledge_base_service()
    try:
        result = service.create(
            name=payload.name,
            description=payload.description,
            knowledge_base_type=payload.type,
            is_default=payload.is_default,
            workspace_id=payload.workspace_id,
            indexing_strategy=payload.indexing_strategy,
            provider_config=payload.provider_config,
        )
        return KnowledgeBaseResponse(**result.to_dict())
    except KnowledgeBaseValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/knowledge-bases/{knowledge_base_id}", response_model=KnowledgeBaseResponse)
def get_knowledge_base(knowledge_base_id: str) -> KnowledgeBaseResponse:
    try:
        return KnowledgeBaseResponse(**_knowledge_base_service().get(knowledge_base_id).to_dict())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Knowledge base not found") from exc


@app.patch("/knowledge-bases/{knowledge_base_id}", response_model=KnowledgeBaseResponse)
def update_knowledge_base(
    knowledge_base_id: str,
    payload: KnowledgeBaseUpdateRequest,
) -> KnowledgeBaseResponse:
    try:
        result = _knowledge_base_service().update(
            knowledge_base_id,
            name=payload.name,
            description=payload.description,
            is_default=payload.is_default,
            indexing_strategy=payload.indexing_strategy,
            provider_config=payload.provider_config,
        )
        return KnowledgeBaseResponse(**result.to_dict())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Knowledge base not found") from exc
    except KnowledgeBaseValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/knowledge-bases/{knowledge_base_id}", response_model=KnowledgeBaseResponse)
def archive_knowledge_base(knowledge_base_id: str) -> KnowledgeBaseResponse:
    try:
        return KnowledgeBaseResponse(**_knowledge_base_service().archive(knowledge_base_id).to_dict())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Knowledge base not found") from exc
    except KnowledgeBaseValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/restore", response_model=KnowledgeBaseResponse)
def restore_knowledge_base(knowledge_base_id: str) -> KnowledgeBaseResponse:
    try:
        return KnowledgeBaseResponse(**_knowledge_base_service().restore(knowledge_base_id).to_dict())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Knowledge base not found") from exc


@app.get("/knowledge-bases/{knowledge_base_id}/wiki/pages", response_model=WikiPagesResponse)
def list_wiki_pages(
    knowledge_base_id: str,
    q: str = Query(default=""),
    status: str = Query(default=""),
    page_type: str = Query(default=""),
    folder_id: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str = Query(default="", max_length=300),
) -> WikiPagesResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        result = _wiki_service().list_pages(
            scope,
            q=q,
            status=status,
            page_type=page_type,
            folder_id=folder_id,
            limit=limit,
            cursor=cursor,
        )
        return WikiPagesResponse(**result)
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/wiki/pages", response_model=WikiPageResponse, status_code=201)
def create_wiki_page(knowledge_base_id: str, payload: WikiPageCreateRequest) -> WikiPageResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        page = _wiki_service().create_page(scope, _payload_dict(payload))
        return WikiPageResponse(**page.to_dict())
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/knowledge-bases/{knowledge_base_id}/wiki/folders", response_model=WikiFoldersResponse)
def list_wiki_folders(
    knowledge_base_id: str,
    parent_id: str = Query(default=""),
) -> WikiFoldersResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiFoldersResponse(items=[WikiFolderResponse(**item) for item in _wiki_service().list_folders(scope, parent_id)])
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/wiki/folders", response_model=WikiFolderResponse, status_code=201)
def create_wiki_folder(knowledge_base_id: str, payload: WikiFolderCreateRequest) -> WikiFolderResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiFolderResponse(**_wiki_service().create_folder(scope, _payload_dict(payload)).to_dict())
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/knowledge-bases/{knowledge_base_id}/wiki/folders/{folder_id}", response_model=WikiFolderResponse)
def update_wiki_folder(knowledge_base_id: str, folder_id: str, payload: WikiFolderUpdateRequest) -> WikiFolderResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiFolderResponse(**_wiki_service().update_folder(scope, folder_id, _payload_dict(payload, exclude_unset=True)).to_dict())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wiki folder not found") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/knowledge-bases/{knowledge_base_id}/wiki/folders/{folder_id}", status_code=204)
def delete_wiki_folder(knowledge_base_id: str, folder_id: str) -> None:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        _wiki_service().delete_empty_folder(scope, folder_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wiki folder not found") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/knowledge-bases/{knowledge_base_id}/wiki/graph", response_model=WikiGraphResponse)
def get_wiki_graph(
    knowledge_base_id: str,
    center_slug: str = Query(default=""),
    limit: int = Query(default=80, ge=1, le=200),
) -> WikiGraphResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiGraphResponse(**_wiki_service().graph(scope, center_slug=center_slug, limit=limit))
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/wiki/source-doc")
def read_wiki_source_doc(knowledge_base_id: str, payload: WikiSourceDocRequest) -> dict:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return _wiki_service().read_source_doc(scope, **_payload_dict(payload))
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/knowledge-bases/{knowledge_base_id}/wiki/generation-tasks", response_model=WikiGenerationTasksResponse)
def list_wiki_generation_tasks(
    knowledge_base_id: str,
    doc_id: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
) -> WikiGenerationTasksResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        items = _wiki_service().list_generation_tasks(scope, doc_id=doc_id, limit=limit)
        return WikiGenerationTasksResponse(items=[WikiGenerationTaskResponse(**item) for item in items])
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/knowledge-bases/{knowledge_base_id}/wiki/overview", response_model=WikiOverviewResponse)
def get_wiki_overview(knowledge_base_id: str) -> WikiOverviewResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiOverviewResponse(**_wiki_service().overview(scope))
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/knowledge-bases/{knowledge_base_id}/wiki/logs", response_model=WikiLogsResponse)
def list_wiki_logs(
    knowledge_base_id: str,
    limit: int = Query(default=30, ge=1, le=100),
    cursor: int = Query(default=0, ge=0),
) -> WikiLogsResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        result = _wiki_service().list_logs(scope, limit=limit, cursor=cursor)
        return WikiLogsResponse(
            items=[WikiLogResponse(**item) for item in result["items"]],
            next_cursor=result["next_cursor"],
        )
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/knowledge-bases/{knowledge_base_id}/wiki/processing-tasks", response_model=WikiProcessingTasksResponse)
def list_wiki_processing_tasks(knowledge_base_id: str, status: str = Query(default="")) -> WikiProcessingTasksResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        statuses = {item.strip() for item in status.split(",") if item.strip()} or None
        items = _wiki_service().list_processing_tasks(scope, statuses=statuses)
        return WikiProcessingTasksResponse(items=[WikiProcessingTaskResponse(**item) for item in items])
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/wiki/processing-tasks/{task_id}/retry", response_model=WikiProcessingTaskResponse)
def retry_wiki_processing_task(knowledge_base_id: str, task_id: str) -> WikiProcessingTaskResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiProcessingTaskResponse(**_wiki_service().retry_processing_task(scope, task_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wiki processing task not found or not retryable") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/knowledge-bases/{knowledge_base_id}/wiki/processing-tasks/{task_id}", response_model=WikiProcessingTaskResponse)
def get_wiki_processing_task(knowledge_base_id: str, task_id: str) -> WikiProcessingTaskResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiProcessingTaskResponse(**_wiki_service().get_processing_task(scope, task_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wiki processing task not found") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/wiki/processing-tasks/{task_id}/cancel", response_model=WikiProcessingTaskResponse)
def cancel_wiki_processing_task(knowledge_base_id: str, task_id: str) -> WikiProcessingTaskResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiProcessingTaskResponse(**_wiki_service().cancel_processing_task(scope, task_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wiki processing task not found") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/wiki/generate", response_model=WikiGenerationResponse)
def generate_wiki_page(knowledge_base_id: str, payload: WikiGenerationRequest) -> WikiGenerationResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        wiki_service = _wiki_service()
        ingest_service = getattr(wiki_service, "ingest_service", None)
        result = (
            ingest_service.enqueue_document(scope, payload.doc_id, debounce_seconds=0)
            if ingest_service is not None
            else wiki_service.generate_draft_for_document(
                scope,
                payload.doc_id,
                auto_publish=payload.auto_publish,
                max_source_chunks=payload.max_source_chunks,
            )
        )
        return WikiGenerationResponse(**result)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/knowledge-bases/{knowledge_base_id}/wiki/issues", response_model=WikiIssuesResponse)
def list_wiki_issues(
    knowledge_base_id: str,
    slug: str = Query(default=""),
    status: str = Query(default="open"),
    issue_type: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=100),
) -> WikiIssuesResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiIssuesResponse(items=[WikiIssueResponse(**item) for item in _wiki_service().list_issues(scope, slug=slug, status=status, issue_type=issue_type, limit=limit)])
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/wiki/issues", response_model=WikiIssueResponse, status_code=201)
def create_wiki_issue(knowledge_base_id: str, payload: WikiIssueCreateRequest) -> WikiIssueResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiIssueResponse(**_wiki_service().create_issue(scope, _payload_dict(payload)).to_dict())
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/knowledge-bases/{knowledge_base_id}/wiki/issues/{issue_id}", response_model=WikiIssueResponse)
def get_wiki_issue(knowledge_base_id: str, issue_id: str) -> WikiIssueResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiIssueResponse(**_wiki_service().get_issue(scope, issue_id).to_dict())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wiki issue not found") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.patch("/knowledge-bases/{knowledge_base_id}/wiki/issues/{issue_id}", response_model=WikiIssueResponse)
def update_wiki_issue(knowledge_base_id: str, issue_id: str, payload: WikiIssueUpdateRequest) -> WikiIssueResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiIssueResponse(**_wiki_service().update_issue_status(scope, issue_id, payload.status).to_dict())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wiki issue not found") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/wiki/issues/{issue_id}/cleanup")
def cleanup_wiki_issue(knowledge_base_id: str, issue_id: str) -> dict:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return _wiki_service().cleanup_issue(scope, issue_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wiki issue or page not found") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/knowledge-bases/{knowledge_base_id}/wiki/proposals", response_model=WikiProposalsResponse)
def list_wiki_proposals(
    knowledge_base_id: str,
    slug: str = Query(default=""),
    status: str = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=100),
) -> WikiProposalsResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiProposalsResponse(items=[WikiProposalResponse(**item) for item in _wiki_service().list_proposals(scope, slug=slug, status=status, limit=limit)])
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/wiki/proposals", response_model=WikiProposalResponse, status_code=201)
def create_wiki_proposal(knowledge_base_id: str, payload: WikiProposalCreateRequest) -> WikiProposalResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiProposalResponse(**_wiki_service().create_proposal(scope, _payload_dict(payload)).to_dict())
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/knowledge-bases/{knowledge_base_id}/wiki/proposals/{proposal_id}", response_model=WikiProposalResponse)
def get_wiki_proposal(knowledge_base_id: str, proposal_id: str) -> WikiProposalResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiProposalResponse(**_wiki_service().get_proposal(scope, proposal_id).to_dict())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wiki proposal not found") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/wiki/proposals/{proposal_id}/apply", response_model=WikiProposalApplyResponse)
def apply_wiki_proposal(knowledge_base_id: str, proposal_id: str) -> WikiProposalApplyResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiProposalApplyResponse(**_wiki_service().apply_proposal(scope, proposal_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wiki proposal not found") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/wiki/proposals/{proposal_id}/reject", response_model=WikiProposalResponse)
def reject_wiki_proposal(knowledge_base_id: str, proposal_id: str) -> WikiProposalResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiProposalResponse(**_wiki_service().reject_proposal(scope, proposal_id).to_dict())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wiki proposal not found") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/knowledge-bases/{knowledge_base_id}/wiki/pages/{slug:path}", response_model=WikiPageResponse)
def get_wiki_page(knowledge_base_id: str, slug: str) -> WikiPageResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiPageResponse(**_wiki_service().get_page(scope, slug).to_dict())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wiki page not found") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.patch("/knowledge-bases/{knowledge_base_id}/wiki/pages/{slug:path}", response_model=WikiPageResponse)
def update_wiki_page(knowledge_base_id: str, slug: str, payload: WikiPageUpdateRequest) -> WikiPageResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiPageResponse(**_wiki_service().update_page(scope, slug, _payload_dict(payload, exclude_unset=True)).to_dict())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wiki page not found") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/wiki/pages/{slug:path}/move", response_model=WikiPageResponse)
def move_wiki_page(knowledge_base_id: str, slug: str, payload: WikiPageMoveRequest) -> WikiPageResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiPageResponse(**_wiki_service().move_page(scope, slug, folder_id=payload.folder_id, category_path=payload.category_path).to_dict())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wiki page not found") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/knowledge-bases/{knowledge_base_id}/wiki/pages/{slug:path}", response_model=WikiPageResponse)
def archive_wiki_page(knowledge_base_id: str, slug: str) -> WikiPageResponse:
    try:
        scope = _resolve_request_scope([knowledge_base_id])
        return WikiPageResponse(**_wiki_service().archive_page(scope, slug).to_dict())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wiki page not found") from exc
    except WikiValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/eval/runs", response_model=EvalRunResponse)
def create_eval_run(payload: EvalRunCreateRequest) -> EvalRunResponse:
    try:
        return EvalRunResponse(**evaluation_service.start_run(payload.dataset_path, case_ids=payload.case_ids, baseline_run_id=payload.baseline_run_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _raise_internal_error("Failed to start evaluation run", exc)


@app.get("/eval/runs", response_model=EvalRunsResponse)
def list_eval_runs() -> EvalRunsResponse:
    return EvalRunsResponse(items=[EvalRunResponse(**item) for item in evaluation_service.list_runs()])


@app.get("/eval/runs/{run_id}", response_model=EvalRunResponse)
def get_eval_run(run_id: str) -> EvalRunResponse:
    try:
        return EvalRunResponse(**evaluation_service.get_run(run_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Evaluation run not found")


@app.get("/eval/runs/{run_id}/results", response_model=EvalResultsResponse)
def list_eval_results(run_id: str) -> EvalResultsResponse:
    try:
        evaluation_service.get_run(run_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Evaluation run not found")
    return EvalResultsResponse(items=[EvalResultResponse(**item) for item in evaluation_service.list_results(run_id)])


@app.post("/ingest", response_model=IngestResponse)
def ingest(knowledge_base_id: str | None = Query(default=None)) -> IngestResponse:
    scope = rag_service.resolve_scope([knowledge_base_id] if knowledge_base_id else None)
    files, chunks = rag_service.ingest(scope=scope)
    return IngestResponse(files=files, chunks=chunks)


def _temporary_attachment_sources(attachments: list[dict]) -> list[dict]:
    return [temporary_attachment_repository.source_for_resolved(item) for item in attachments]


def _merge_sources(sources: list[dict], temporary_sources: list[dict]) -> list[dict]:
    if not temporary_sources:
        return sources
    seen = {
        str(item.get("temporary_attachment_id") or item.get("source") or "")
        for item in sources
    }
    merged = list(sources)
    for item in temporary_sources:
        key = str(item.get("temporary_attachment_id") or item.get("source") or "")
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged


def _temporary_attachment_context(attachments: list[dict]) -> str:
    if not attachments:
        return ""
    blocks = ["[鏈疆涓存椂闄勪欢]"]
    for index, item in enumerate(attachments, start=1):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        blocks.append(f"## 闄勪欢 {index}: {item.get('filename')}\n{text}")
    return "\n\n".join(blocks)


def _join_request_context(*blocks: str) -> str:
    return "\n\n".join(block.strip() for block in blocks if block and block.strip())


def _resolve_chat_mode(payload: ChatRequest) -> str:
    if payload.chat_mode:
        return payload.chat_mode
    use_agentic_chat = (
        bool(getattr(rag_service, "chat_agentic_workflow_enabled", False))
        and getattr(rag_service, "agentic_workflow", None) is not None
    )
    return "reasoning" if use_agentic_chat else "quick"


def _stream_agentic_chat_events(
    question: str,
    conversation_context: str,
    memory_context: str,
    answer_parts: list[str],
    stream_state: dict,
    scope,
    temporary_sources: list[dict] | None = None,
):
    temporary_sources = temporary_sources or []
    for event in rag_service.agentic_workflow.stream_query_events(
        question,
        conversation_context=conversation_context,
        memory_context=memory_context,
        scope=scope,
    ):
        event_type = getattr(event, "event_type", "")
        payload_data = getattr(event, "payload", {})
        if event_type == "sources":
            sources = list(payload_data.get("items", payload_data if isinstance(payload_data, list) else []))
            sources = _merge_sources(sources, temporary_sources)
            stream_state["sources"] = sources
            yield f"data: {json.dumps({'sources': sources}, ensure_ascii=False)}\n\n"
        elif event_type == "reasoning":
            yield f"data: {json.dumps({'reasoning': payload_data}, ensure_ascii=False)}\n\n"
        elif event_type == "token":
            token = str(payload_data.get("token", ""))
            answer_parts.append(token)
            yield f"data: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"
        elif event_type == "final":
            stream_state["sources"] = _merge_sources(
                list(payload_data.get("citations", stream_state.get("sources", []))),
                temporary_sources,
            )
        else:
            yield f"data: {json.dumps({event_type: payload_data}, ensure_ascii=False)}\n\n"


def _stream_agent_runtime_chat_events(
    question: str,
    conversation_context: str,
    memory_context: str,
    answer_parts: list[str],
    stream_state: dict,
    scope,
    temporary_sources: list[dict] | None = None,
    mode: str = "reasoning",
):
    temporary_sources = temporary_sources or []
    stream_kwargs = {
        "conversation_context": conversation_context,
        "memory_context": memory_context,
        "scope": scope,
    }
    try:
        runtime_parameters = inspect.signature(rag_service.agent_runtime.stream_query_events).parameters
    except (TypeError, ValueError):
        runtime_parameters = {}
    if "mode" in runtime_parameters:
        stream_kwargs["mode"] = mode
    for event in rag_service.agent_runtime.stream_query_events(question, **stream_kwargs):
        event_type = getattr(event, "event_type", "")
        payload_data = getattr(event, "payload", {})
        for sse_payload in _agent_runtime_sse_payloads(event_type, payload_data, stream_state, answer_parts, temporary_sources):
            yield f"data: {json.dumps(sse_payload, ensure_ascii=False)}\n\n"


def _agent_runtime_sse_payloads(
    event_type: str,
    payload_data: dict,
    stream_state: dict,
    answer_parts: list[str],
    temporary_sources: list[dict],
) -> list[dict]:
    if event_type == "agent_references":
        sources = list(payload_data.get("items", []))
        sources = _merge_sources(sources, temporary_sources)
        stream_state["sources"] = sources
        stream_state["_sources_sent_from_agent_references"] = True
        agent_payload = dict(payload_data)
        agent_payload["items"] = sources
        return [{"agent_references": agent_payload}, {"sources": sources}]
    if event_type == "sources":
        sources = list(payload_data.get("items", payload_data if isinstance(payload_data, list) else []))
        sources = _merge_sources(sources, temporary_sources)
        stream_state["sources"] = sources
        if stream_state.get("_sources_sent_from_agent_references"):
            return []
        return [{"sources": sources}]
    if event_type == "token":
        token = str(payload_data.get("token", ""))
        answer_parts.append(token)
        return [{"token": token}]
    if event_type == "final":
        stream_state["sources"] = _merge_sources(
            list(payload_data.get("citations", stream_state.get("sources", []))),
            temporary_sources,
        )
        final_metadata = {key: value for key, value in payload_data.items() if key != "answer"}
        return [{"final": final_metadata}] if final_metadata else []
    return [{event_type: payload_data}]


_CHAT_PROCESS_EVENT_KEYS = {
    "agent_trace",
    "tool_call",
    "tool_observation",
    "agent_query",
    "agent_thought",
    "agent_tool_call",
    "agent_tool_result",
    "agent_reflection",
    "agent_remedial_search",
    "agent_references",
    "agent_final_answer",
    "agent_complete",
    "agent_error",
    "evidence_summary",
    "citation_verification",
}


def _remember_chat_process_payload(stream_state: dict, payload: dict) -> None:
    if "reasoning" in payload and isinstance(payload.get("reasoning"), dict):
        stream_state["reasoning"] = payload["reasoning"]
    if "evidence_summary" in payload and isinstance(payload.get("evidence_summary"), dict):
        stream_state["evidence_summary"] = payload["evidence_summary"]
    if "citation_verification" in payload and isinstance(payload.get("citation_verification"), dict):
        stream_state["citation_verification"] = payload["citation_verification"]
    for key in _CHAT_PROCESS_EVENT_KEYS:
        value = payload.get(key)
        if not isinstance(value, dict):
            continue
        events = stream_state.setdefault("agent_events", [])
        if len(events) >= 200:
            stream_state["agent_events_truncated"] = True
            return
        events.append(
            {
                "kind": key,
                "payload": value,
                "sequence": len(events) + 1,
                "timestamp": int(time.time() * 1000),
            }
        )


def _chat_completion_metadata(
    stream_state: dict,
    *,
    scope,
    chat_mode: str,
    attachment_ids: list[str],
) -> dict:
    metadata = {
        "sources": stream_state.get("sources", []),
        "knowledge_base_scope": scope.to_dict(),
        "chat_mode": chat_mode,
        "temporary_attachment_ids": attachment_ids,
    }
    if isinstance(stream_state.get("reasoning"), dict):
        metadata["reasoning"] = stream_state["reasoning"]
    if isinstance(stream_state.get("evidence_summary"), dict):
        metadata["evidence_summary"] = stream_state["evidence_summary"]
    if isinstance(stream_state.get("citation_verification"), dict):
        metadata["citation_verification"] = stream_state["citation_verification"]
    if isinstance(stream_state.get("agent_events"), list):
        metadata["agent_events"] = stream_state["agent_events"]
    if stream_state.get("agent_events_truncated"):
        metadata["agent_events_truncated"] = True
    return metadata


def _stream_raw_chat_events(
    question: str,
    conversation_context: str,
    memory_context: str,
    answer_parts: list[str],
    stream_state: dict,
    scope,
    temporary_sources: list[dict] | None = None,
):
    temporary_sources = temporary_sources or []
    hits = rag_service.recall_parent_hits(rag_service.hybrid_retrieve_hits(question, scope=scope), scope=scope)
    sources = _merge_sources(rag_service.extract_sources(hits), temporary_sources)
    stream_state["sources"] = sources

    yield f"data: {json.dumps({'sources': sources}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'reasoning': rag_service.build_reasoning_summary(question, hits)}, ensure_ascii=False)}\n\n"

    build_agent_trace = getattr(rag_service, "build_chat_agent_trace", None)
    if callable(build_agent_trace):
        trace_kwargs = {}
        trace_parameters = inspect.signature(build_agent_trace).parameters
        if "scope" in trace_parameters:
            trace_kwargs["scope"] = scope
        if "sources" in trace_parameters:
            trace_kwargs["sources"] = sources
        for trace_step in build_agent_trace(question, hits, **trace_kwargs):
            yield f"data: {json.dumps({'agent_trace': trace_step}, ensure_ascii=False)}\n\n"

    for token in rag_service.stream_answer(
        question,
        hits=hits,
        conversation_context=conversation_context,
        memory_context=memory_context,
        scope=scope,
    ):
        answer_parts.append(token)
        yield f"data: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"


def _infer_sse_event_type(payload: dict) -> str:
    for key in (
        "conversation_id",
        "sources",
        "reasoning",
        "token",
        "final",
        "error",
        "stop",
        "memory_updated",
        "agent_trace",
        "tool_call",
        "tool_observation",
        "agent_query",
        "agent_thought",
        "agent_tool_call",
        "agent_tool_result",
        "agent_reflection",
        "agent_remedial_search",
        "agent_references",
        "agent_final_answer",
        "agent_complete",
        "agent_error",
    ):
        if key in payload:
            return key
    return "sse_payload"


class ChatStreamStopped(Exception):
    pass


def _register_chat_stream(identity: StreamIdentity) -> threading.Event:
    with chat_stream_cancellations_lock:
        signal = threading.Event()
        chat_stream_cancellations[identity.key] = signal
        return signal


def _active_chat_stream_signal(identity: StreamIdentity) -> threading.Event | None:
    with chat_stream_cancellations_lock:
        return chat_stream_cancellations.get(identity.key)


def _unregister_chat_stream(identity: StreamIdentity, signal: threading.Event) -> None:
    with chat_stream_cancellations_lock:
        if chat_stream_cancellations.get(identity.key) is signal:
            chat_stream_cancellations.pop(identity.key, None)


def _request_chat_stream_stop(identity: StreamIdentity, *, reason: str = "client_requested") -> str:
    with chat_stream_cancellations_lock:
        signal = chat_stream_cancellations.get(identity.key)
    if _stream_has_event_type(identity, "stop"):
        if signal is not None:
            signal.set()
        return "stopping"
    if signal is not None:
        signal.set()
        chat_stream_manager.append(
            identity,
            ChatStreamEvent(
                event_type="stop",
                payload={"stop": {"session_id": identity.session_id, "message_id": identity.message_id, "reason": reason or "client_requested"}},
            ),
        )
        return "stopping"
    if chat_stream_manager.is_terminal(identity):
        return "completed"
    chat_stream_manager.append(
        identity,
        ChatStreamEvent(
            event_type="stop",
            payload={"stop": {"session_id": identity.session_id, "message_id": identity.message_id, "reason": reason or "client_requested"}},
        ),
    )
    chat_stream_manager.append(identity, ChatStreamEvent(event_type="done", payload={}, terminal=True))
    return "stopped"


def _append_chat_stream_event(identity: StreamIdentity, event: ChatStreamEvent) -> ChatStreamEvent:
    return chat_stream_manager.append(identity, event)


def _publish_chat_stream_event(
    identity: StreamIdentity,
    event_bus: ChatEventBus | None,
    event_type: str,
    payload: dict,
    *,
    terminal: bool = False,
) -> ChatStreamEvent:
    if event_bus is not None:
        event = event_bus.publish(event_type, payload, terminal=terminal)
    else:
        event = ChatStreamEvent(event_type=event_type, payload=payload, terminal=terminal)
    return _append_chat_stream_event(identity, event)


def _emit_stored_sse(
    identity: StreamIdentity,
    payload: dict,
    *,
    event_type: str | None = None,
    terminal: bool = False,
    event_bus: ChatEventBus | None = None,
) -> str:
    stored = _publish_chat_stream_event(
        identity,
        event_bus,
        event_type or _infer_sse_event_type(payload),
        payload,
        terminal=terminal,
    )
    outbound = dict(payload)
    outbound["_stream"] = {
        "session_id": identity.session_id,
        "message_id": identity.message_id,
        "offset": stored.offset,
        "terminal": stored.terminal,
    }
    return f"data: {json.dumps(outbound, ensure_ascii=False)}\n\n"


def _stored_event_to_sse(identity: StreamIdentity, event: ChatStreamEvent) -> str:
    if event.event_type == "done":
        return "data: [DONE]\n\n"
    outbound = dict(event.payload)
    outbound["_stream"] = {
        "session_id": identity.session_id,
        "message_id": identity.message_id,
        "offset": event.offset,
        "terminal": event.terminal,
    }
    return f"data: {json.dumps(outbound, ensure_ascii=False)}\n\n"


def _replay_chat_stream(
    identity: StreamIdentity,
    offset: int,
    *,
    stop_signal: threading.Event | None = None,
    poll_until_terminal: bool = False,
) -> object:
    cursor = int(offset)
    while True:
        events = chat_stream_manager.read_after(identity, cursor)
        if events:
            for event in events:
                cursor = max(cursor, int(event.offset))
                yield _stored_event_to_sse(identity, event)
                if event.terminal:
                    return
                if event.event_type == "stop" and stop_signal is not None:
                    stop_signal.set()
            continue
        if chat_stream_manager.is_terminal(identity):
            return
        if stop_signal is None and not poll_until_terminal:
            return
        if stop_signal is not None and stop_signal.is_set():
            return
        time.sleep(0.1)


def _stream_has_events(identity: StreamIdentity) -> bool:
    return bool(chat_stream_manager.read_after(identity, 0, limit=1))


def _stream_has_event_type(identity: StreamIdentity, event_type: str) -> bool:
    offset = 0
    while True:
        events = chat_stream_manager.read_after(identity, offset, limit=100)
        if not events:
            return False
        for event in events:
            offset = max(offset, int(event.offset))
            if event.event_type == event_type:
                return True


def _message_response(message: dict) -> ChatMessageResponse:
    session_id = str(message.get("conversation_id") or message.get("session_id") or "")
    return ChatMessageResponse(
        id=str(message.get("id") or ""),
        session_id=session_id,
        conversation_id=session_id,
        request_id=str(message.get("request_id") or ""),
        role=str(message.get("role") or "assistant"),
        content=str(message.get("content") or ""),
        metadata_json=dict(message.get("metadata_json") or {}),
        is_completed=bool(message.get("is_completed", True)),
        created_at=str(message.get("created_at") or ""),
        updated_at=str(message.get("updated_at") or message.get("created_at") or ""),
    )


def _session_summary_response(conversation: dict) -> ChatSessionSummaryResponse:
    session_id = str(conversation.get("id") or conversation.get("session_id") or "")
    return ChatSessionSummaryResponse(
        id=session_id,
        session_id=session_id,
        conversation_id=session_id,
        title=str(conversation.get("display_title") or conversation.get("title") or "新对话"),
        last_message_preview=str(conversation.get("last_message_preview") or ""),
        is_running=bool(conversation.get("is_running")),
        agent_config=dict(conversation.get("agent_config") or {}),
        created_at=str(conversation.get("created_at") or ""),
        updated_at=str(conversation.get("updated_at") or conversation.get("created_at") or ""),
    )


def _start_stop_watcher(identity: StreamIdentity, signal: threading.Event) -> threading.Thread:
    def watch() -> None:
        deadline = time.monotonic() + 2 * 60 * 60
        offset = 0
        while time.monotonic() < deadline:
            events = chat_stream_manager.read_after(identity, offset)
            for event in events:
                offset = max(offset, int(event.offset))
                if event.event_type == "stop":
                    signal.set()
                    return
                if event.event_type == "done" or event.terminal or event.event_type == "error":
                    return
            if signal.is_set() or chat_stream_manager.is_terminal(identity):
                return
            time.sleep(0.3)

    thread = threading.Thread(target=watch, name=f"chat-stop-watch:{identity.key}", daemon=True)
    thread.start()
    return thread


def _get_or_create_chat_conversation(conversation_id: str | None, *, principal, agent_config: dict) -> dict:
    try:
        return conversation_service.get_or_create_conversation(
            conversation_id,
            principal=principal,
            agent_config=agent_config,
        )
    except TypeError:
        return conversation_service.get_or_create_conversation(conversation_id)


def _build_chat_context(conversation_id: str, *, principal) -> dict:
    try:
        return conversation_service.build_context(conversation_id, principal=principal)
    except TypeError:
        return conversation_service.build_context(conversation_id)


def _maybe_summarize_chat_conversation(conversation_id: str, *, principal) -> str:
    try:
        return conversation_service.maybe_summarize(conversation_id, principal=principal)
    except TypeError:
        return conversation_service.maybe_summarize(conversation_id)


def _create_chat_turn(conversation_id: str, question: str, metadata: dict) -> tuple[dict, dict, str]:
    create_turn = getattr(conversation_service.repository, "create_turn", None)
    if callable(create_turn):
        return create_turn(conversation_id, question, metadata)
    request_id = f"req_{int(time.time() * 1000)}"
    user_message = conversation_service.repository.append_message(conversation_id, "user", question, metadata)
    assistant_message = {
        "id": f"msg-assistant-{int(time.time() * 1000)}",
        "conversation_id": conversation_id,
        "role": "assistant",
        "content": "",
        "metadata_json": metadata,
        "request_id": request_id,
        "is_completed": False,
    }
    user_message.setdefault("request_id", request_id)
    return user_message, assistant_message, request_id


def _complete_chat_assistant(
    conversation_id: str,
    message_id: str,
    answer: str,
    metadata: dict,
    *,
    stopped: bool = False,
) -> None:
    complete_assistant = getattr(conversation_service.repository, "complete_assistant_message", None)
    if callable(complete_assistant):
        complete_assistant(conversation_id, message_id, answer, metadata, stopped=stopped)
        return
    if stopped:
        metadata = {**metadata, "stopped": True}
    conversation_service.repository.append_message(conversation_id, "assistant", answer, metadata)


def _store_raw_sse_events(
    identity: StreamIdentity,
    raw_events,
    *,
    stream_state: dict | None = None,
    event_bus: ChatEventBus | None = None,
    stop_signal: threading.Event | None = None,
) -> object:
    for event in raw_events:
        if stop_signal is not None and stop_signal.is_set():
            raise ChatStreamStopped()
        line = next((item for item in str(event).splitlines() if item.startswith("data:")), "")
        payload = line.replace("data:", "", 1).strip() if line else ""
        if payload == "[DONE]":
            yield _emit_stored_sse(identity, {}, event_type="done", terminal=True, event_bus=event_bus)
            continue
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            yield event
            continue
        if isinstance(decoded, dict):
            if stream_state is not None:
                _remember_chat_process_payload(stream_state, decoded)
            yield _emit_stored_sse(identity, decoded, event_bus=event_bus)
        else:
            yield event
        if stop_signal is not None and stop_signal.is_set():
            raise ChatStreamStopped()


def _store_pipeline_events(
    identity: StreamIdentity,
    pipeline_events,
    *,
    event_bus: ChatEventBus | None = None,
) -> object:
    for event in pipeline_events:
        event_type = getattr(event, "event_type", "")
        payload = dict(getattr(event, "payload", {}) or {})
        terminal = bool(getattr(event, "terminal", False))
        if event_type == "done":
            _publish_chat_stream_event(identity, event_bus, "done", {}, terminal=True)
            yield "data: [DONE]\n\n"
            return
        yield _emit_stored_sse(
            identity,
            payload,
            event_type=event_type or _infer_sse_event_type(payload),
            terminal=terminal,
            event_bus=event_bus,
        )
        if terminal:
            return


@app.post("/chat/attachments", response_model=ChatAttachmentResponse)
async def upload_chat_attachment(file: UploadFile = File(...)) -> ChatAttachmentResponse:
    try:
        content = await file.read()
        result = temporary_attachment_repository.create(
            filename=file.filename or "attachment",
            content=content,
            content_type=file.content_type or "",
        )
        return ChatAttachmentResponse(**result.to_response())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _raise_internal_error("Failed to upload chat attachment", exc)


@app.get("/api/v1/messages/{session_id}/load", response_model=MessagesLoadResponse)
def load_session_messages(
    session_id: str,
    request: Request,
    before_time: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> MessagesLoadResponse:
    principal = principal_from_request(request)
    page = conversation_service.repository.list_messages_before_time(
        session_id,
        before_time,
        limit=limit,
        principal=principal,
    )
    return MessagesLoadResponse(
        session_id=session_id,
        conversation_id=session_id,
        items=[_message_response(item) for item in page["items"]],
        hasMoreHistory=bool(page.get("hasMoreHistory")),
    )


@app.get("/api/v1/sessions/recent", response_model=SessionsRecentResponse)
def list_recent_sessions(request: Request, limit: int = Query(default=20, ge=1, le=100)) -> SessionsRecentResponse:
    principal = principal_from_request(request)
    list_conversations = getattr(conversation_service.repository, "list_conversations", None)
    if not callable(list_conversations):
        return SessionsRecentResponse(items=[])
    conversations = list_conversations(limit=limit, principal=principal)
    return SessionsRecentResponse(items=[_session_summary_response(item) for item in conversations])


@app.patch("/api/v1/sessions/{session_id}", response_model=ChatSessionSummaryResponse)
def rename_session(session_id: str, payload: SessionRenameRequest, request: Request) -> ChatSessionSummaryResponse:
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    principal = principal_from_request(request)
    rename_conversation = getattr(conversation_service.repository, "rename_conversation", None)
    if not callable(rename_conversation):
        raise HTTPException(status_code=501, detail="Session rename is not supported")
    conversation = rename_conversation(session_id, title, principal=principal)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return _session_summary_response(conversation)


@app.delete("/api/v1/sessions/{session_id}", response_model=SessionDeleteResponse)
def delete_session(session_id: str, request: Request) -> SessionDeleteResponse:
    principal = principal_from_request(request)
    delete_conversation = getattr(conversation_service.repository, "delete_conversation", None)
    if not callable(delete_conversation):
        raise HTTPException(status_code=501, detail="Session delete is not supported")
    deleted = delete_conversation(session_id, principal=principal)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionDeleteResponse(session_id=session_id, deleted=True)


@app.get("/api/v1/sessions/{session_id}/continue-stream")
def continue_session_stream(
    session_id: str,
    request: Request,
    message_id: str = Query(..., min_length=1),
    offset: int = Query(default=0, ge=0),
) -> StreamingResponse:
    principal = principal_from_request(request)
    message = conversation_service.repository.get_message(session_id, message_id, principal=principal)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    identity = StreamIdentity(session_id, message_id)
    if not _stream_has_events(identity):
        raise HTTPException(status_code=404, detail="No stream events found")
    return StreamingResponse(
        _replay_chat_stream(
            identity,
            offset,
            stop_signal=_active_chat_stream_signal(identity),
            poll_until_terminal=True,
        ),
        media_type="text/event-stream",
    )


@app.get("/api/v1/sessions/continue-stream")
def continue_session_stream_by_message(
    request: Request,
    message_id: str = Query(..., min_length=1),
    session_id: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
) -> StreamingResponse:
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    return continue_session_stream(session_id, request, message_id, offset)


@app.post("/api/v1/sessions/{session_id}/stop", response_model=SessionStopResponse)
def stop_session_generation(session_id: str, payload: SessionStopRequest, request: Request) -> SessionStopResponse:
    message_id = payload.message_id.strip()
    if not message_id:
        raise HTTPException(status_code=400, detail="message_id is required")
    principal = principal_from_request(request)
    message = conversation_service.repository.get_message(session_id, message_id, principal=principal)
    if message is None or message.get("role") != "assistant":
        raise HTTPException(status_code=404, detail="Message not found")
    if bool(message.get("is_completed")):
        status = "completed"
    else:
        status = _request_chat_stream_stop(StreamIdentity(session_id, message_id), reason="user_requested")
    return SessionStopResponse(
        session_id=session_id,
        message_id=message_id,
        status=status,
        stopped=status in {"stopping", "stopped", "completed"},
    )


@app.post("/chat/stream")
def chat_stream(payload: ChatRequest, request: Request) -> StreamingResponse:
    question = payload.message.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Message is required")
    requested_ids = payload.knowledge_base_ids or ([payload.knowledge_base_id] if payload.knowledge_base_id else None)
    scope = _resolve_request_scope(requested_ids)
    chat_mode = _resolve_chat_mode(payload)
    attachment_ids = list(payload.attachment_ids or [])
    try:
        temporary_attachments = temporary_attachment_repository.resolve_many(attachment_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    temporary_context = _temporary_attachment_context(temporary_attachments)
    temporary_sources = _temporary_attachment_sources(temporary_attachments)
    principal = principal_from_request(request)
    requested_conversation_id = payload.session_id or payload.conversation_id
    agent_config = {
        "chat_mode": chat_mode,
        "knowledge_base_ids": list(scope.selected_knowledge_base_ids),
        "temporary_attachment_ids": attachment_ids,
        "memory_enabled": bool(payload.memory_enabled),
        "temporary": bool(payload.temporary),
    }
    conversation = _get_or_create_chat_conversation(
        requested_conversation_id,
        principal=principal,
        agent_config=agent_config,
    )
    conversation_id = str(conversation["id"])
    if payload.stream_message_id and payload.stream_offset is not None:
        stream_identity = StreamIdentity(conversation_id, payload.stream_message_id)

        def replay_event_gen():
            try:
                yield from _replay_chat_stream(
                    stream_identity,
                    int(payload.stream_offset),
                    stop_signal=_active_chat_stream_signal(stream_identity),
                    poll_until_terminal=False,
                )
            finally:
                temporary_attachment_repository.mark_consumed(attachment_ids)

        return StreamingResponse(replay_event_gen(), media_type="text/event-stream")

    memory_enabled = bool(payload.memory_enabled) and not bool(payload.temporary)
    turn_metadata = {
        "knowledge_base_scope": scope.to_dict(),
        "chat_mode": chat_mode,
        "temporary_attachment_ids": attachment_ids,
    }
    user_message, assistant_message, request_id = _create_chat_turn(conversation_id, question, turn_metadata)
    stream_message_id = payload.stream_message_id or str(assistant_message["id"])
    stream_identity = StreamIdentity(conversation_id, stream_message_id)

    def produce_events() -> None:
        event_bus = ChatEventBus()
        stop_signal = _register_chat_stream(stream_identity)
        _start_stop_watcher(stream_identity, stop_signal)
        answer_parts: list[str] = []
        stream_state: dict = {"sources": []}
        try:
            runtime_available = bool(getattr(rag_service, "agent_runtime_enabled", False)) and getattr(rag_service, "agent_runtime", None) is not None
            quick_runtime_available = (
                bool(getattr(rag_service, "unified_chat_runtime_enabled", False))
                and bool(getattr(rag_service, "quick_runtime_enabled", False))
                and getattr(rag_service, "agent_runtime", None) is not None
            )
            wiki_runtime_available = (
                bool(getattr(rag_service, "wiki_runtime_enabled", False))
                and getattr(rag_service, "agent_runtime", None) is not None
            )
            rag_wiki_runtime_available = (
                bool(getattr(rag_service, "rag_wiki_runtime_enabled", False))
                and getattr(rag_service, "agent_runtime", None) is not None
            )
            agentic_available = getattr(rag_service, "agentic_workflow", None) is not None
            if chat_mode == "wiki" and not wiki_runtime_available:
                raise ValueError("Wiki 问答模式暂不可用，请确认 Wiki runtime 已启用")
            if chat_mode == "rag_wiki" and not rag_wiki_runtime_available:
                raise ValueError("RAG + Wiki 模式暂不可用，请确认混合检索 runtime 已启用")
            if chat_mode == "reasoning" and not (runtime_available or agentic_available):
                raise ValueError("智能推理暂不可用，请切换为快速问答后重试")
            if chat_mode == "quick" and chat_rag_pipeline_enabled and not quick_runtime_available:
                context = ChatPipelineContext(
                    request=ChatPipelineRequest(
                        question=question,
                        conversation_id=conversation_id,
                        stream_message_id=stream_message_id,
                        scope=scope,
                        chat_mode=chat_mode,
                        memory_enabled=memory_enabled,
                        user_message_id=str(user_message["id"]),
                        assistant_message_id=str(assistant_message["id"]),
                        request_id=request_id,
                        principal=principal,
                        temporary_attachment_ids=attachment_ids,
                        temporary_context=temporary_context,
                        temporary_sources=temporary_sources,
                    ),
                    runtime=ChatPipelineRuntime(
                        rag_service=rag_service,
                        conversation_service=conversation_service,
                        memory_service=memory_service,
                        event_bus=event_bus,
                        stream_identity=stream_identity,
                        stop_signal=stop_signal,
                        trace_id=get_trace_id(),
                    ),
                )
                for _event in _store_pipeline_events(
                    stream_identity,
                    run_quick_rag_pipeline(context),
                    event_bus=event_bus,
                ):
                    pass
                stream_state["sources"] = context.state.sources
                stream_state["reasoning"] = context.state.reasoning
                stream_state["agent_events"] = context.state.agent_events
                stream_state["agent_events_truncated"] = context.state.agent_events_truncated
                answer_parts[:] = list(context.state.answer_parts)
                if context.state.stopped:
                    _complete_chat_assistant(
                        conversation_id,
                        str(assistant_message["id"]),
                        "".join(answer_parts),
                        _chat_completion_metadata(stream_state, scope=scope, chat_mode=chat_mode, attachment_ids=attachment_ids),
                        stopped=True,
                    )
                return

            _emit_stored_sse(
                stream_identity,
                {
                    "conversation_id": conversation_id,
                    "session_id": conversation_id,
                    "stream_message_id": stream_message_id,
                    "assistant_message_id": str(assistant_message["id"]),
                    "request_id": request_id,
                    "user_message_id": str(user_message["id"]),
                },
                event_type="conversation_id",
                event_bus=event_bus,
            )

            conversation_context = _build_chat_context(conversation_id, principal=principal)
            memories = memory_service.recall_memories(question) if memory_enabled else []
            memory_context = _join_request_context(
                memory_service.format_prompt_context(memories) if memory_enabled else "",
                temporary_context,
            )
            if chat_mode == "rag_wiki":
                for _event in _store_raw_sse_events(
                    stream_identity,
                    _stream_agent_runtime_chat_events(
                        question,
                        conversation_context,
                        memory_context,
                        answer_parts,
                        stream_state,
                        scope,
                        temporary_sources,
                        mode="rag_wiki",
                    ),
                    stream_state=stream_state,
                    event_bus=event_bus,
                    stop_signal=stop_signal,
                ):
                    pass
            elif chat_mode == "wiki":
                for _event in _store_raw_sse_events(
                    stream_identity,
                    _stream_agent_runtime_chat_events(
                        question,
                        conversation_context,
                        memory_context,
                        answer_parts,
                        stream_state,
                        scope,
                        temporary_sources,
                        mode="wiki",
                    ),
                    stream_state=stream_state,
                    event_bus=event_bus,
                    stop_signal=stop_signal,
                ):
                    pass
            elif chat_mode == "reasoning" and runtime_available:
                for _event in _store_raw_sse_events(
                    stream_identity,
                    _stream_agent_runtime_chat_events(
                        question,
                        conversation_context,
                        memory_context,
                        answer_parts,
                        stream_state,
                        scope,
                        temporary_sources,
                        mode="reasoning",
                    ),
                    stream_state=stream_state,
                    event_bus=event_bus,
                    stop_signal=stop_signal,
                ):
                    pass
            elif chat_mode == "reasoning":
                for _event in _store_raw_sse_events(
                    stream_identity,
                    _stream_agentic_chat_events(
                        question,
                        conversation_context,
                        memory_context,
                        answer_parts,
                        stream_state,
                        scope,
                        temporary_sources,
                    ),
                    stream_state=stream_state,
                    event_bus=event_bus,
                    stop_signal=stop_signal,
                ):
                    pass
            elif quick_runtime_available:
                for _event in _store_raw_sse_events(
                    stream_identity,
                    _stream_agent_runtime_chat_events(
                        question,
                        conversation_context,
                        memory_context,
                        answer_parts,
                        stream_state,
                        scope,
                        temporary_sources,
                        mode="quick",
                    ),
                    stream_state=stream_state,
                    event_bus=event_bus,
                    stop_signal=stop_signal,
                ):
                    pass
            else:
                for _event in _store_raw_sse_events(
                    stream_identity,
                    _stream_raw_chat_events(
                        question,
                        conversation_context,
                        memory_context,
                        answer_parts,
                        stream_state,
                        scope,
                        temporary_sources,
                    ),
                    stream_state=stream_state,
                    event_bus=event_bus,
                    stop_signal=stop_signal,
                ):
                    pass

            answer = "".join(answer_parts)
            completion_metadata = _chat_completion_metadata(
                stream_state,
                scope=scope,
                chat_mode=chat_mode,
                attachment_ids=attachment_ids,
            )
            _complete_chat_assistant(conversation_id, str(assistant_message["id"]), answer, completion_metadata)
            _maybe_summarize_chat_conversation(conversation_id, principal=principal)
            memory_updates = memory_service.process_exchange(
                user_message=question,
                assistant_message=answer,
                conversation_id=conversation_id,
                user_message_id=str(user_message["id"]),
                memory_enabled=memory_enabled,
            )
            if memory_updates:
                _emit_stored_sse(
                    stream_identity,
                    {"memory_updated": memory_updates},
                    event_type="memory_updated",
                    event_bus=event_bus,
                )
            _publish_chat_stream_event(stream_identity, event_bus, "done", {}, terminal=True)
        except ChatStreamStopped:
            partial_answer = "".join(answer_parts)
            _complete_chat_assistant(
                conversation_id,
                str(assistant_message["id"]),
                partial_answer,
                _chat_completion_metadata(stream_state, scope=scope, chat_mode=chat_mode, attachment_ids=attachment_ids),
                stopped=True,
            )
            _emit_stored_sse(
                stream_identity,
                {"stop": {"session_id": conversation_id, "message_id": stream_message_id, "reason": "client_requested"}},
                event_type="stop",
                event_bus=event_bus,
            )
            _publish_chat_stream_event(stream_identity, event_bus, "done", {}, terminal=True)
        except Exception as e:
            logger.exception("Chat stream failed: %s", e)
            _emit_stored_sse(stream_identity, {"error": str(e)}, event_type="error", event_bus=event_bus)
            _publish_chat_stream_event(stream_identity, event_bus, "done", {}, terminal=True)
        finally:
            _unregister_chat_stream(stream_identity, stop_signal)
            temporary_attachment_repository.mark_consumed(attachment_ids)

    def event_gen():
        producer = threading.Thread(target=produce_events, name=f"chat-producer:{stream_identity.key}", daemon=True)
        producer.start()
        yield from _replay_chat_stream(
            stream_identity,
            0,
            stop_signal=_active_chat_stream_signal(stream_identity),
            poll_until_terminal=True,
        )

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/chat/stream/stop", response_model=ChatStreamStopResponse)
def stop_chat_stream(payload: ChatStreamStopRequest) -> ChatStreamStopResponse:
    if not payload.conversation_id.strip() or not payload.stream_message_id.strip():
        raise HTTPException(status_code=400, detail="conversation_id and stream_message_id are required")
    identity = StreamIdentity(payload.conversation_id.strip(), payload.stream_message_id.strip())
    status = _request_chat_stream_stop(identity, reason=payload.reason)
    return ChatStreamStopResponse(
        conversation_id=identity.session_id,
        stream_message_id=identity.message_id,
        status=status,
        stopped=status in {"stopping", "stopped"},
    )


@app.post("/documents/upload", response_model=DocumentUploadResponse)
async def upload_document(
    file: UploadFile = File(...),
    relative_path: str | None = Form(default=None),
    batch_id: str | None = Form(default=None),
    knowledge_base_id: str | None = Form(default=None),
) -> DocumentUploadResponse:
    try:
        content = await file.read()
        result = rag_service.save_uploaded_document(
            filename=file.filename or "",
            content=content,
            relative_path=relative_path,
            batch_id=batch_id,
            scope=rag_service.resolve_scope([knowledge_base_id] if knowledge_base_id else None),
        )
        return DocumentUploadResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _raise_internal_error("Failed to upload document", exc)


@app.post("/knowledge-bases/{knowledge_base_id}/upload-batches", response_model=UploadBatchResponse, status_code=201)
def create_upload_batch(
    knowledge_base_id: str,
    payload: UploadBatchCreateRequest | None = None,
) -> UploadBatchResponse:
    try:
        scope = rag_service.resolve_scope([knowledge_base_id])
        logger.info("upload_batch.create.start", extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id})
        result = rag_service.create_upload_batch(scope, (payload or UploadBatchCreateRequest()).settings)
        logger.info(
            "upload_batch.create.end",
            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "batch_id": result.get("id")},
        )
        return UploadBatchResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/knowledge-bases/{knowledge_base_id}/upload-batches/{batch_id}", response_model=UploadBatchResponse)
def get_upload_batch(knowledge_base_id: str, batch_id: str) -> UploadBatchResponse:
    try:
        scope = rag_service.resolve_scope([knowledge_base_id])
        return UploadBatchResponse(**rag_service.get_upload_batch(batch_id, scope))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upload batch not found") from exc


@app.patch("/knowledge-bases/{knowledge_base_id}/upload-batches/{batch_id}/settings", response_model=UploadBatchResponse)
def update_upload_batch_settings(
    knowledge_base_id: str,
    batch_id: str,
    payload: UploadBatchSettingsUpdateRequest,
) -> UploadBatchResponse:
    try:
        scope = rag_service.resolve_scope([knowledge_base_id])
        return UploadBatchResponse(**rag_service.update_upload_batch_settings(batch_id, scope, payload.settings))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upload batch not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/upload-batches/{batch_id}/files", response_model=UploadBatchResponse)
async def add_upload_batch_file(
    knowledge_base_id: str,
    batch_id: str,
    file: UploadFile = File(...),
    relative_path: str | None = Form(default=None),
) -> UploadBatchResponse:
    try:
        scope = rag_service.resolve_scope([knowledge_base_id])
        content = await file.read()
        logger.info(
            "upload_batch.file.add.start",
            extra={
                "workspace_id": scope.workspace_id,
                "knowledge_base_id": scope.knowledge_base_id,
                "batch_id": batch_id,
                "file_name": file.filename or "",
                "size": len(content),
            },
        )
        rag_service.add_upload_batch_file(
            batch_id,
            filename=file.filename or "",
            content=content,
            relative_path=relative_path,
            scope=scope,
        )
        result = rag_service.get_upload_batch(batch_id, scope)
        logger.info(
            "upload_batch.file.add.end",
            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "batch_id": batch_id},
        )
        return UploadBatchResponse(**result)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upload batch not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/upload-batches/{batch_id}/confirm", response_model=UploadBatchResponse)
def confirm_upload_batch(
    knowledge_base_id: str,
    batch_id: str,
    background_tasks: BackgroundTasks,
) -> UploadBatchResponse:
    try:
        scope = rag_service.resolve_scope([knowledge_base_id])
        logger.info(
            "upload_batch.confirm.start",
            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "batch_id": batch_id},
        )
        batch = rag_service.start_upload_batch_processing(batch_id, scope)
        uses_durable_processing = getattr(rag_service, "uses_durable_upload_processing", lambda: False)
        if not uses_durable_processing():
            background_tasks.add_task(
                _run_background_with_trace,
                get_trace_id(),
                "upload_batch.process",
                rag_service.process_upload_batch,
                batch_id,
                scope,
            )
        logger.info(
            "upload_batch.confirm.end",
            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "batch_id": batch_id},
        )
        return UploadBatchResponse(**batch)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upload batch not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/upload-batches/{batch_id}/files/{file_id}/retry", response_model=UploadBatchResponse)
def retry_upload_batch_file(knowledge_base_id: str, batch_id: str, file_id: str) -> UploadBatchResponse:
    try:
        scope = rag_service.resolve_scope([knowledge_base_id])
        logger.info(
            "upload_batch.file.retry",
            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "batch_id": batch_id, "file_id": file_id},
        )
        return UploadBatchResponse(**rag_service.retry_upload_batch_file(batch_id, file_id, scope))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upload file task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/knowledge-bases/{knowledge_base_id}/upload-batches/{batch_id}/cancel", response_model=UploadBatchResponse)
def cancel_upload_batch(knowledge_base_id: str, batch_id: str) -> UploadBatchResponse:
    try:
        scope = rag_service.resolve_scope([knowledge_base_id])
        logger.info(
            "upload_batch.cancel",
            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "batch_id": batch_id},
        )
        return UploadBatchResponse(**rag_service.cancel_upload_batch(batch_id, scope))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Upload batch not found") from exc


@app.post("/rag/documents/upload", response_model=RagDocumentUploadResponse)
async def rag_upload_document(
    file: UploadFile = File(...),
    knowledge_base_id: str | None = Form(default=None),
) -> RagDocumentUploadResponse:
    try:
        content = await file.read()
        scope = rag_service.resolve_scope([knowledge_base_id] if knowledge_base_id else None)
        logger.info(
            "document.upload.start",
            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "file_name": file.filename or "", "size": len(content)},
        )
        result = rag_service.save_uploaded_document(
            filename=file.filename or "",
            content=content,
            scope=scope,
        )
        logger.info(
            "document.upload.end",
            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "doc_id": result.get("doc_id")},
        )
        return RagDocumentUploadResponse(doc_id=result["doc_id"], status="uploaded")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _raise_internal_error("Failed to upload document", exc)


@app.post("/rag/documents/{doc_id}/ingest", response_model=RagDocumentIngestResponse)
def rag_ingest_document(
    doc_id: str,
    knowledge_base_id: str | None = Query(default=None),
) -> RagDocumentIngestResponse:
    try:
        scope = rag_service.resolve_scope([knowledge_base_id] if knowledge_base_id else None)
        logger.info("document.ingest.start", extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "doc_id": doc_id})
        result = rag_service.ingest_document_by_id(doc_id, scope=scope)
        logger.info(
            "document.ingest.end",
            extra={
                "workspace_id": scope.workspace_id,
                "knowledge_base_id": scope.knowledge_base_id,
                "doc_id": doc_id,
                "chunks": result.get("chunk_count", 0),
                "vectors": result.get("vector_count", 0),
            },
        )
        return RagDocumentIngestResponse(**result)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document not found")
    except Exception as exc:
        _raise_internal_error("Failed to ingest document", exc)


@app.post("/rag/query", response_model=RagQueryResponse)
def rag_query(payload: RagQueryRequest) -> RagQueryResponse:
    try:
        requested_ids = payload.knowledge_base_ids or ([payload.knowledge_base_id] if payload.knowledge_base_id else None)
        scope = rag_service.resolve_scope(requested_ids, payload.doc_ids)
        logger.info(
            "rag.query.start",
            extra={
                "workspace_id": scope.workspace_id,
                "knowledge_base_ids": list(scope.selected_knowledge_base_ids),
                "document_ids": list(scope.document_ids),
                "top_k": payload.top_k,
            },
        )
        result = rag_service.answer_query(
            payload.question,
            top_k=payload.top_k,
            filters={**payload.filters, "doc_ids": payload.doc_ids or []},
            scope=scope,
        )
        logger.info(
            "rag.query.end",
            extra={
                "workspace_id": scope.workspace_id,
                "knowledge_base_ids": list(scope.selected_knowledge_base_ids),
                "used_chunks": len(result.get("used_chunks", [])),
                "citations": len(result.get("citations", [])),
                "confidence": result.get("confidence"),
            },
        )
        return RagQueryResponse(**result)
    except Exception as exc:
        _raise_internal_error("Failed to query RAG", exc)


@app.delete("/rag/documents/{doc_id}", response_model=RagDeleteResponse)
def rag_delete_document(
    doc_id: str,
    knowledge_base_id: str | None = Query(default=None),
) -> RagDeleteResponse:
    try:
        scope = rag_service.resolve_scope([knowledge_base_id] if knowledge_base_id else None)
        logger.info("document.delete.start", extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "doc_id": doc_id})
        rag_service.delete_document(doc_id, scope=scope)
        logger.info("document.delete.end", extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "doc_id": doc_id})
        return RagDeleteResponse(doc_id=doc_id, status="deleted")
    except Exception as exc:
        _raise_internal_error("Failed to delete document", exc)


@app.post("/documents/parse", response_model=DocumentParseResponse)
def parse_document(payload: DocumentParseRequest) -> DocumentParseResponse:
    try:
        scope = rag_service.resolve_scope([payload.knowledge_base_id] if payload.knowledge_base_id else None)
        result = rag_service.parse_document(payload.source, scope=scope)
        return DocumentParseResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document not found")
    except Exception as exc:
        _raise_internal_error("Failed to parse document", exc)


@app.get("/parser-engines")
def list_parser_engines() -> dict:
    return {"items": PARSER_REGISTRY.list_engines(), "default": "builtin"}


@app.get("/documents/content", response_model=DocumentContentResponse)
def get_document_content(
    source: str = Query(..., min_length=1),
    knowledge_base_id: str | None = Query(default=None),
) -> DocumentContentResponse:
    try:
        scope = rag_service.resolve_scope([knowledge_base_id] if knowledge_base_id else None)
        content = rag_service.get_document_content(source, scope=scope)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid source path")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document not found")
    except Exception as exc:
        _raise_internal_error("Failed to read document", exc)

    return DocumentContentResponse(source=source, content=content)


@app.get("/documents/file")
def get_document_file(
    source: str = Query(..., min_length=1),
    knowledge_base_id: str | None = Query(default=None),
) -> FileResponse:
    try:
        scope = rag_service.resolve_scope([knowledge_base_id] if knowledge_base_id else None)
        file_path = rag_service.get_document_path(source, scope=scope)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid source path")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Document not found")
    except Exception as exc:
        _raise_internal_error("Failed to read document", exc)

    media_type, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(path=file_path, media_type=media_type or "application/octet-stream")


@app.get("/documents", response_model=DocumentsResponse)
def list_documents(
    knowledge_base_id: str | None = Query(default=None),
    q: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    file_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    source: str | None = Query(default=None),
    created_from: str | None = Query(default=None),
    created_to: str | None = Query(default=None),
) -> DocumentsResponse:
    scope = rag_service.resolve_scope([knowledge_base_id] if knowledge_base_id else None)
    return DocumentsResponse(
        items=rag_service.list_documents(
            scope=scope,
            filters={
                "q": q,
                "tag": tag,
                "file_type": file_type,
                "status": status,
                "source": source,
                "created_from": created_from,
                "created_to": created_to,
            },
        )
    )


@app.get("/documents/{doc_id}/processing-trace")
def get_document_processing_trace(
    doc_id: str,
    knowledge_base_id: str | None = Query(default=None),
) -> dict:
    try:
        scope = rag_service.resolve_scope([knowledge_base_id] if knowledge_base_id else None)
        return rag_service.get_document_processing_trace(doc_id, scope)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Processing trace not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/documents/{doc_id}/enrichment/retry", response_model=DocumentItem)
def retry_document_enrichment(
    doc_id: str,
    knowledge_base_id: str | None = Query(default=None),
) -> DocumentItem:
    scope = _resolve_request_scope([knowledge_base_id] if knowledge_base_id else None)
    service = getattr(rag_service, "document_enrichment_service", None)
    if service is None or not service.enabled:
        raise HTTPException(status_code=409, detail="Document enrichment is disabled")
    try:
        service.retry(doc_id, scope)
        document = rag_service.document_repository.get_document(doc_id, scope)
        if document is None:
            raise KeyError(doc_id)
        metadata = document.get("metadata_json", {})
        runtime_status = {}
        get_runtime_status = getattr(rag_service, "_document_runtime_status", None)
        if callable(get_runtime_status):
            runtime_status = get_runtime_status(document, scope)
        return DocumentItem(
            **document,
            source=document.get("storage_path", ""),
            size=int(metadata.get("size", 0) or 0),
            **runtime_status,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/documents/{doc_id}/processing/retry", response_model=DocumentItem)
def retry_document_processing(
    doc_id: str,
    knowledge_base_id: str | None = Query(default=None),
) -> DocumentItem:
    try:
        scope = _resolve_request_scope([knowledge_base_id] if knowledge_base_id else None)
        return DocumentItem(**rag_service.retry_document_processing_task(doc_id, scope))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/feedback/answer", response_model=FeedbackCreateResponse)
def create_feedback_answer(payload: FeedbackCreateRequest) -> FeedbackCreateResponse:
    try:
        if payload.knowledge_base_ids and len(set(payload.knowledge_base_ids)) != 1 and not payload.knowledge_base_id:
            raise ValueError("Multi-knowledge-base feedback requires one explicit target knowledge_base_id")
        requested_ids = [payload.knowledge_base_id] if payload.knowledge_base_id else payload.knowledge_base_ids
        scope = rag_service.resolve_scope(requested_ids)
        result = rag_service.create_feedback_document(question=payload.question, answer=payload.answer, scope=scope)
        return FeedbackCreateResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _raise_internal_error("Failed to create feedback document", exc)


@app.get("/memories", response_model=MemoriesResponse)
def list_memories() -> MemoriesResponse:
    return MemoriesResponse(items=memory_service.list_active_memories())


@app.delete("/memories/{memory_id}", response_model=MemoryDeleteResponse)
def delete_memory(memory_id: str) -> MemoryDeleteResponse:
    if not memory_service.delete_memory(memory_id):
        raise HTTPException(status_code=404, detail="Memory not found")
    return MemoryDeleteResponse(id=memory_id, status="deleted")
