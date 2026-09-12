import hashlib
import inspect
import json
import copy
from contextvars import ContextVar
from dataclasses import asdict
import logging
import re
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

from openai import OpenAI

from app.models.document_models import Chunk
from app.models.knowledge_base import KnowledgeBaseScope
from app.models.processing_config import (
    PROCESSING_VERSION,
    ProcessingRuntimeDefaults,
    resolve_processing_config,
)
from app.services.documents.document_chunker import DocumentChunker
from app.services.documents.document_loader import SUPPORTED_EXTS, iter_source_files, load_text
from app.services.documents.document_parser import DocumentParser, PARSER_REGISTRY, stable_doc_id
from app.services.documents.document_repository import DocumentRepository
from app.services.knowledge.audit_repository import KnowledgeAuditRepository
from app.services.agent.agent_prompt_templates import ContextPromptCatalog, scope_to_prompt_kbs
from app.services.documents.image_repository import ImageRepository
from app.services.retrieval.keyword_search import PostgresKeywordSearch
from app.services.documents.multimodal_processing import (
    CaptionProvider,
    DisabledCaptionProvider,
    DisabledOCRProvider,
    OCRProvider,
    image_result_chunk,
)
from app.services.documents.object_storage import LocalObjectStorage, ObjectStorageProvider
from app.services.infrastructure.observability import get_observability_sink
from app.services.processing.processing_trace import ProcessingTraceRecorder
from app.services.processing.processing_task_repository import (
    TASK_CANCELED,
    TASK_COMPLETED,
    TASK_DEAD_LETTERED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_PROCESSING,
    TASK_RETRYING,
)
from app.services.infrastructure.logging_config import get_trace_id as _current_log_trace_id
from app.services.retrieval.query_understanding import QueryUnderstandingResult, QueryUnderstandingService
from app.services.retrieval.retrieval_models import KeywordSearch, RetrievedChunk
from app.services.documents.upload_batch_repository import UploadBatchRepository, initial_phase_report
from app.services.async_runtime.task_routes import (
    CHUNK_EXTRACT_TASK,
    DOCUMENT_PROCESS_TASK,
    EMBEDDING_INDEX_TASK,
    GENERATED_QUESTIONS_TASK,
    GRAPH_EXTRACTION_TASK,
    IMAGE_MULTIMODAL_TASK,
    KNOWLEDGE_POSTPROCESS_TASK,
    SUMMARY_GENERATION_TASK,
    UPLOAD_FILE_TASK,
    WIKI_FINALIZE_TASK,
    WIKI_INGEST_TASK,
    route_for_task_type,
)

if TYPE_CHECKING:
    from app.services.retrieval.vector_store import MilvusVectorStore
    from app.services.kg.kg_service import KGEnrichmentService
    from app.services.kg.graph_retriever import GraphRetriever
    from app.services.agent.agentic_workflow import AgenticRetrievalWorkflow
    from app.services.agent.agent_runtime import AgentRuntime
    from app.services.processing.processing_worker import DocumentProcessingWorker

logger = logging.getLogger(__name__)

UPLOAD_REQUIRED_INDEXING_FLAGS = {
    "dense_enabled": True,
    "keyword_enabled": True,
    "wiki_enabled": True,
    "graph_enabled": True,
}


TRACE_STAGE_DEFINITIONS = [
    ("docreader", "文档解析", "load"),
    ("chunking", "分块", "chunk_strategy"),
    ("embedding", "向量化", "index"),
    ("multimodal", "多模态识别", "multimodal"),
    ("postprocess", "后处理", "postprocess"),
]

TRACE_STATUS_MAP = {
    "completed": "done",
    "parsed": "done",
    "done": "done",
    "processing": "running",
    "parsing": "running",
    "running": "running",
    "failed": "failed",
    "skipped": "skipped",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "pending": "pending",
}

ACTIVE_PROCESSING_TASK_STATUSES = {TASK_PENDING, TASK_PROCESSING, TASK_RETRYING}
FAILED_PROCESSING_TASK_STATUSES = {TASK_FAILED, TASK_DEAD_LETTERED}
TERMINAL_PROCESSING_TASK_STATUSES = {TASK_COMPLETED, TASK_FAILED, TASK_CANCELED, TASK_DEAD_LETTERED}
PROCESSING_TASK_STAGE_NAMES = {
    DOCUMENT_PROCESS_TASK: "docreader",
    UPLOAD_FILE_TASK: "docreader",
    CHUNK_EXTRACT_TASK: "chunking",
    EMBEDDING_INDEX_TASK: "embedding",
    IMAGE_MULTIMODAL_TASK: "multimodal",
    KNOWLEDGE_POSTPROCESS_TASK: "postprocess",
    SUMMARY_GENERATION_TASK: "postprocess",
    GENERATED_QUESTIONS_TASK: "postprocess",
    GRAPH_EXTRACTION_TASK: "postprocess",
    WIKI_INGEST_TASK: "postprocess",
    WIKI_FINALIZE_TASK: "postprocess",
}


class ProcessingPreviewError(ValueError):
    """Raised when read-only processing preview fails a safety limit."""


class StaleDocumentRevision(RuntimeError):
    """Raised when a staged task belongs to an older document revision."""


class RAGService:
    def __init__(
        self,
        vector_store: "MilvusVectorStore",
        llm_client: OpenAI,
        chat_model: str,
        system_prompt: str,
        data_dir: str,
        top_k: int,
        min_relevance_score: float,
        chunk_size: int,
        chunk_overlap: int,
        milvus_bm25_enabled: bool = False,
        dense_recall_top_n: int = 40,
        bm25_recall_top_n: int = 40,
        fusion_top_k: int = 30,
        rrf_k: int = 60,
        rrf_vector_weight: float = 0.7,
        rrf_keyword_weight: float = 0.3,
        reranker_threshold: float = 0.3,
        reranker_fallback_min_score: float = 0.15,
        direct_load_max_chunks: int = 50,
        context_short_chunk_min_chars: int = 240,
        context_expanded_chunk_max_chars: int = 1200,
        retrieval_debug_enabled: bool = False,
        low_recall_query_expansion_enabled: bool = False,
        low_recall_min_candidates: int = 3,
        low_recall_min_score: float = 0.2,
        low_recall_max_queries: int = 3,
        reranker_degradation_enabled: bool = True,
        reranker_degraded_threshold: float = 0.15,
        reranker_fallback_top_n: int = 1,
        mmr_enabled: bool = False,
        mmr_lambda: float = 0.75,
        mmr_top_k: int = 0,
        duplicate_overlap_threshold: float = 0.92,
        reranker_enabled: bool = False,
        reranker_provider: str = "local",
        reranker_top_n: int = 8,
        reranker_timeout_seconds: float = 5.0,
        reranker: Any | None = None,
        ocr_enabled: bool = False,
        ocr_provider: str = "docling",
        document_repository: DocumentRepository | None = None,
        knowledge_base_service: Any | None = None,
        wiki_page_service: Any | None = None,
        upload_batch_repository: UploadBatchRepository | None = None,
        document_parser: DocumentParser | None = None,
        document_chunker: DocumentChunker | None = None,
        query_understanding: QueryUnderstandingService | None = None,
        keyword_search: KeywordSearch | None = None,
        kg_service: "KGEnrichmentService | None" = None,
        kg_extraction_enabled: bool = False,
        graph_retriever: "GraphRetriever | None" = None,
        agentic_workflow: "AgenticRetrievalWorkflow | None" = None,
        agentic_retrieval_enabled: bool = False,
        chat_agentic_workflow_enabled: bool = False,
        agent_trace_stream_enabled: bool = False,
        document_enrichment_service: Any | None = None,
        audit_repository: KnowledgeAuditRepository | None = None,
        image_repository: ImageRepository | None = None,
        object_storage: ObjectStorageProvider | None = None,
        ocr_provider_service: OCRProvider | None = None,
        caption_provider_service: CaptionProvider | None = None,
        multimodal_max_workers: int = 2,
        processing_defaults: ProcessingRuntimeDefaults | None = None,
        processing_trace_recorder: ProcessingTraceRecorder | None = None,
        context_template_path: str = "config/prompt_templates/context_template.yaml",
        context_template_id: str = "qa_context",
    ):
        self.vector_store = vector_store
        self.llm_client = llm_client
        self.chat_model = chat_model
        self.system_prompt = system_prompt
        self.context_prompt_catalog = ContextPromptCatalog.load(context_template_path)
        self.context_template_id = context_template_id
        self.data_dir = Path(data_dir)
        self.top_k = top_k
        self.min_relevance_score = min_relevance_score
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.milvus_bm25_enabled = milvus_bm25_enabled
        self.dense_recall_top_n = dense_recall_top_n
        self.bm25_recall_top_n = bm25_recall_top_n
        self.fusion_top_k = fusion_top_k
        self.rrf_k = rrf_k
        self.rrf_vector_weight = rrf_vector_weight
        self.rrf_keyword_weight = rrf_keyword_weight
        self.reranker_threshold = reranker_threshold
        self.reranker_fallback_min_score = reranker_fallback_min_score
        self.direct_load_max_chunks = direct_load_max_chunks
        self.context_short_chunk_min_chars = context_short_chunk_min_chars
        self.context_expanded_chunk_max_chars = context_expanded_chunk_max_chars
        self.retrieval_debug_enabled = retrieval_debug_enabled
        self.low_recall_query_expansion_enabled = low_recall_query_expansion_enabled
        self.low_recall_min_candidates = low_recall_min_candidates
        self.low_recall_min_score = low_recall_min_score
        self.low_recall_max_queries = low_recall_max_queries
        self.reranker_degradation_enabled = reranker_degradation_enabled
        self.reranker_degraded_threshold = reranker_degraded_threshold
        self.reranker_fallback_top_n = reranker_fallback_top_n
        self.mmr_enabled = mmr_enabled
        self.mmr_lambda = mmr_lambda
        self.mmr_top_k = mmr_top_k
        self.duplicate_overlap_threshold = duplicate_overlap_threshold
        self._retrieval_debug_context: ContextVar[dict[str, Any] | None] = ContextVar(
            f"rag_retrieval_debug_{id(self)}",
            default=None,
        )
        self._last_retrieval_debug_global: dict[str, Any] = {}
        self.reranker_enabled = reranker_enabled
        self.reranker_provider = reranker_provider
        self.reranker_top_n = reranker_top_n
        self.reranker_timeout_seconds = reranker_timeout_seconds
        self.reranker = reranker
        self.ocr_enabled = ocr_enabled
        self.ocr_provider = ocr_provider
        self.parent_chunk_size = max(chunk_size * 3, chunk_size + 1)
        self.parent_chunk_overlap = max(chunk_overlap * 2, 0)
        self.ingest_state_file = self.vector_store.persist_dir / "ingest_state.json"
        self.feedback_dir = self.data_dir / "feedback"
        self.upload_dir = self.data_dir / "uploads"
        self.document_repository = document_repository or DocumentRepository(self.vector_store.persist_dir / "rag_metadata.sqlite3")
        self.knowledge_base_service = knowledge_base_service
        self.wiki_page_service = wiki_page_service
        self.upload_batch_repository = upload_batch_repository or UploadBatchRepository(
            self.document_repository.db_path,
            self.document_repository.defaults,
        )
        self.default_scope = (
            knowledge_base_service.resolve_scope() if knowledge_base_service is not None else self.document_repository.default_scope()
        )
        self.document_parser = document_parser
        self.document_chunker = document_chunker or DocumentChunker(
            parent_max_tokens=self.parent_chunk_size,
            child_max_tokens=chunk_size,
            child_overlap_tokens=chunk_overlap,
        )
        self.query_understanding = query_understanding or QueryUnderstandingService()
        self.keyword_search = keyword_search or PostgresKeywordSearch(self.document_repository)
        self.kg_service = kg_service
        self.kg_extraction_enabled = kg_extraction_enabled
        self.graph_retriever = graph_retriever
        self.agentic_workflow = agentic_workflow
        self.agent_runtime: "AgentRuntime | None" = None
        self.agent_runtime_enabled = False
        self.agentic_retrieval_enabled = agentic_retrieval_enabled
        self.chat_agentic_workflow_enabled = chat_agentic_workflow_enabled
        self.agent_trace_stream_enabled = agent_trace_stream_enabled
        self.document_enrichment_service = document_enrichment_service
        self.audit_repository = audit_repository or KnowledgeAuditRepository(
            self.document_repository.db_path,
            self.document_repository.defaults,
        )
        self.image_repository = image_repository or ImageRepository(
            self.document_repository.db_path,
            self.document_repository.defaults,
        )
        self.ocr_provider_service = ocr_provider_service or DisabledOCRProvider()
        self.caption_provider_service = caption_provider_service or DisabledCaptionProvider()
        self.multimodal_max_workers = max(1, int(multimodal_max_workers))
        self._multimodal_db_lock = Lock()
        self.processing_defaults = processing_defaults or ProcessingRuntimeDefaults(
            parser_engine=getattr(document_parser, "engine", "builtin") if document_parser is not None else "builtin",
            pdf_force_scanned=bool(getattr(document_parser, "parser_options", {}).get("force_scanned", False))
            if document_parser is not None
            else False,
            pdf_render_dpi=int(getattr(document_parser, "parser_options", {}).get("render_dpi", 200))
            if document_parser is not None
            else 200,
            pdf_jpeg_quality=int(getattr(document_parser, "parser_options", {}).get("jpeg_quality", 90))
            if document_parser is not None
            else 90,
            pdf_max_pages=int(getattr(document_parser, "parser_options", {}).get("max_pages", 1000))
            if document_parser is not None
            else 1000,
            chunk_strategy=getattr(self.document_chunker, "strategy", "auto"),
            parent_chunk_size_chars=int(getattr(self.document_chunker, "parent_max_tokens", 4096)),
            child_chunk_size_chars=int(getattr(self.document_chunker, "child_max_tokens", 384)),
            child_chunk_overlap_chars=int(getattr(self.document_chunker, "child_overlap_tokens", 76)),
            media_storage_dir=str(self.vector_store.persist_dir / "media"),
            ocr_enabled=self.ocr_enabled,
            ocr_provider=self.ocr_provider if self.ocr_enabled else "disabled",
            ocr_min_confidence=float(getattr(self.document_chunker, "ocr_min_confidence", 0.0)),
            graph_enabled=self.kg_extraction_enabled,
        )
        self.object_storage = object_storage or LocalObjectStorage(
            self.processing_defaults.media_storage_dir,
            max_object_bytes=self.processing_defaults.media_max_bytes,
        )
        self.processing_trace_recorder = processing_trace_recorder or ProcessingTraceRecorder.from_env(
            self.data_dir / "processing_traces"
        )
        self.processing_worker: "DocumentProcessingWorker | None" = None

        # Legacy compatibility for existing unit tests and old in-memory callers.
        self.parent_store: dict[str, dict[str, Any]] = {}
        self.keyword_items: list[dict[str, Any]] = []

    @property
    def _last_retrieval_debug(self) -> dict[str, Any]:
        context = getattr(self, "_retrieval_debug_context", None)
        if context is not None:
            current = context.get()
            if current is not None:
                return current
        return getattr(self, "_last_retrieval_debug_global", {})

    @_last_retrieval_debug.setter
    def _last_retrieval_debug(self, value: dict[str, Any]) -> None:
        normalized = dict(value or {})
        context = getattr(self, "_retrieval_debug_context", None)
        if context is not None and context.get() is not None:
            context.set(normalized)
            return
        self._last_retrieval_debug_global = normalized

    def run_with_retrieval_debug(self, operation) -> tuple[Any, dict[str, Any]]:
        context = self._retrieval_debug_context
        token = context.set({})
        try:
            result = operation()
            debug = copy.deepcopy(context.get() or {})
        finally:
            context.reset(token)
        self._last_retrieval_debug_global = copy.deepcopy(debug)
        return result, debug

    def _compute_data_signature(self) -> str:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        manifest = []
        for f in iter_source_files(self.data_dir):
            stat = f.stat()
            manifest.append({"path": str(f.relative_to(self.data_dir)), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        manifest.sort(key=lambda x: x["path"])
        raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def resolve_scope(
        self,
        knowledge_base_ids: list[str] | tuple[str, ...] | None = None,
        document_ids: list[str] | tuple[str, ...] | None = None,
    ) -> KnowledgeBaseScope:
        if self.knowledge_base_service is not None:
            return self.knowledge_base_service.resolve_scope(knowledge_base_ids, document_ids)
        if knowledge_base_ids and tuple(knowledge_base_ids) != self.default_scope.selected_knowledge_base_ids:
            raise ValueError("Knowledge base selection is unavailable")
        return replace(
            self.default_scope,
            document_ids=tuple(document_ids or ()),
            compatibility_default=not bool(knowledge_base_ids),
        )

    def _ingest_state_path(self, scope: KnowledgeBaseScope) -> Path:
        if scope.knowledge_base_id == self.default_scope.knowledge_base_id:
            return self.ingest_state_file
        return self.ingest_state_file.with_name(f"ingest_state_{scope.knowledge_base_id}.json")

    def _write_ingest_state(
        self,
        files: int | None = None,
        chunks: int | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> None:
        scope = scope or self.default_scope
        state_path = self._ingest_state_path(scope)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "data_signature": self._compute_data_signature(),
            "files": files if files is not None else len(list(iter_source_files(self.data_dir))),
            "chunks": chunks if chunks is not None else self.document_repository.count_chunks({"child", "table"}, scope),
            "workspace_id": scope.workspace_id,
            "knowledge_base_id": scope.knowledge_base_id,
        }
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def needs_reingest(self, scope: KnowledgeBaseScope | None = None) -> bool:
        scope = scope or self.default_scope
        state_path = self._ingest_state_path(scope)
        if self.document_repository.count_chunks({"child", "table"}, scope) == 0:
            return True
        if not state_path.exists():
            return True
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return True
        return state.get("data_signature") != self._compute_data_signature()

    def ingest(self, scope: KnowledgeBaseScope | None = None) -> tuple[int, int]:
        scope = scope or self.default_scope
        if self.knowledge_base_service is not None:
            self.knowledge_base_service.assert_writable(scope)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if scope.knowledge_base_id == self.default_scope.knowledge_base_id:
            custom_ids = {
                item.id
                for item in (self.knowledge_base_service.list() if self.knowledge_base_service is not None else [])
                if item.id != self.default_scope.knowledge_base_id
            }
            files = [
                path
                for path in iter_source_files(self.data_dir)
                if not (
                    len(path.relative_to(self.data_dir).parts) >= 2
                    and path.relative_to(self.data_dir).parts[0] == "uploads"
                    and path.relative_to(self.data_dir).parts[1] in custom_ids
                )
            ]
        else:
            files = []
            for document in self.document_repository.list_documents(scope):
                try:
                    file_path = self._resolve_source_path(str(document.get("storage_path", "")))
                except ValueError:
                    continue
                if file_path.exists() and file_path.is_file():
                    files.append(file_path)
        requires_schema_rebuild = bool(getattr(self.vector_store, "reset_required", False))
        delete_knowledge_base = getattr(self.vector_store, "delete_knowledge_base", None)
        if requires_schema_rebuild:
            raise RuntimeError("Vector storage requires the clean-rebuild CLI before ingest")
        elif callable(delete_knowledge_base):
            delete_knowledge_base(scope)
        elif scope.knowledge_base_id == self.default_scope.knowledge_base_id:
            self.vector_store.reset_collection()
        else:
            raise RuntimeError("Vector provider does not support knowledge-base scoped rebuild")
        if self.knowledge_base_service is not None:
            self.knowledge_base_service.repository.update_knowledge_base(
                scope.knowledge_base_id, {"reset_required": 0}
            )
        self.document_repository.reset(scope)

        indexed_files = 0
        indexed_chunks = 0
        for file_path in files:
            try:
                result = self.parse_and_index_document(file_path, scope=scope)
            except Exception as exc:
                logger.warning("Skipping unreadable source file %s: %s", file_path, exc)
                continue
            indexed_files += 1
            indexed_chunks += int(result.get("indexed_chunks", 0))

        self._write_ingest_state(files=indexed_files, chunks=indexed_chunks, scope=scope)
        return indexed_files, indexed_chunks

    def parse_and_index_document(
        self,
        file_path: Path,
        scope: KnowledgeBaseScope | None = None,
        processing_settings: dict[str, Any] | None = None,
        cancel_check: Any | None = None,
    ) -> dict[str, Any]:
        scope = scope or self.default_scope
        indexing_strategy = None
        if self.knowledge_base_service is not None:
            indexing_strategy = self.knowledge_base_service.resolve_indexing_strategy(scope)
        dense_enabled = bool(getattr(indexing_strategy, "dense_enabled", True))
        keyword_enabled = bool(getattr(indexing_strategy, "keyword_enabled", True))
        graph_enabled = bool(getattr(indexing_strategy, "graph_enabled", self.kg_extraction_enabled))
        wiki_enabled = bool(getattr(indexing_strategy, "wiki_enabled", False))
        needs_embedding = dense_enabled or keyword_enabled
        resolved_processing = self._resolve_processing_settings(processing_settings)
        processing_metadata = {
            "processing_version": resolved_processing.effective.processing_version,
            "processing": resolved_processing.to_dict(),
            "indexing_strategy": {
                "dense_enabled": dense_enabled,
                "keyword_enabled": keyword_enabled,
                "graph_enabled": graph_enabled,
                "wiki_enabled": wiki_enabled,
            },
        }
        resolved_file = file_path.resolve()
        data_root = self.data_dir.resolve()
        source = resolved_file.relative_to(data_root).as_posix() if resolved_file.is_relative_to(data_root) else file_path.name
        doc_id = stable_doc_id(file_path)
        file_size = file_path.stat().st_size
        trace = self.processing_trace_recorder.start(
            name="document_processing",
            doc_id=doc_id,
            file_name=file_path.name,
            source=source,
            scope=scope.to_dict(),
            metadata={
                "file_size": file_size,
                "extension": file_path.suffix.lower(),
                "requested_processing": resolved_processing.requested.to_dict(),
                "effective_processing": resolved_processing.effective.to_dict(),
            },
        )

        try:
            logger.info(
                "document.processing.start",
                extra={
                    "workspace_id": scope.workspace_id,
                    "knowledge_base_id": scope.knowledge_base_id,
                    "doc_id": doc_id,
                    "source": source,
                    "file_size": file_size,
                },
            )
            with trace.span(
                "load",
                input={
                    "source": source,
                    "file_name": file_path.name,
                    "file_type": file_path.suffix.lower().lstrip("."),
                    "size": file_size,
                    "parser_engine": resolved_processing.effective.parser_engine,
                },
            ) as load_span:
                if callable(cancel_check):
                    cancel_check()
                logger.info("document.processing.stage.start", extra={"stage": "parse", "doc_id": doc_id, "source": source})
                self.document_repository.upsert_document(
                    id=doc_id,
                    name=file_path.name,
                    file_type=file_path.suffix.lower().lstrip("."),
                    storage_path=source,
                    parse_status="parsing",
                    metadata_json={
                        "size": file_size,
                        "processing_trace_id": trace.trace_id,
                        "processing_trace_dir": str(trace.trace_dir),
                        **scope.to_dict(),
                        **processing_metadata,
                    },
                    workspace_id=scope.workspace_id,
                    knowledge_base_id=scope.knowledge_base_id,
                )

                parser = self.document_parser
                parser_engine = resolved_processing.effective.parser_engine
                with trace.db_subspan(
                    load_span,
                    "parser_call",
                    input={
                        "parser_engine": parser_engine,
                        "file_type": file_path.suffix.lower().lstrip("."),
                        "custom_parser": parser is not None,
                    },
                ):
                    if parser is not None:
                        parse_signature = inspect.signature(parser.parse)
                        accepts_requested_engine = "requested_engine" in parse_signature.parameters or any(
                            parameter.kind == inspect.Parameter.VAR_KEYWORD
                            for parameter in parse_signature.parameters.values()
                        )
                        if accepts_requested_engine:
                            parsed = parser.parse(file_path, requested_engine=parser_engine)
                        else:
                            parsed = parser.parse(file_path)
                    else:
                        parsed = PARSER_REGISTRY.parse(
                            file_path,
                            engine=parser_engine,
                            force_scanned=resolved_processing.effective.pdf_force_scanned,
                            render_dpi=resolved_processing.effective.pdf_render_dpi,
                            jpeg_quality=resolved_processing.effective.pdf_jpeg_quality,
                            max_pages=resolved_processing.effective.pdf_max_pages,
                            max_image_edge_px=resolved_processing.effective.pdf_max_image_edge_px,
                            render_concurrency=resolved_processing.effective.pdf_render_concurrency,
                        )
                if self.ocr_enabled:
                    parsed = self._with_ocr_elements(parser, file_path, parsed)
                trace.reassign_doc_id(parsed.doc_id)
                if parsed.doc_id != doc_id:
                    self.document_repository.upsert_document(
                        id=parsed.doc_id,
                        name=parsed.file_name,
                        file_type=parsed.file_type,
                        storage_path=source,
                        parse_status="parsing",
                        metadata_json={
                            "size": file_size,
                            "processing_trace_id": trace.trace_id,
                            "processing_trace_dir": str(trace.trace_dir),
                            **scope.to_dict(),
                            **processing_metadata,
                        },
                        workspace_id=scope.workspace_id,
                        knowledge_base_id=scope.knowledge_base_id,
                    )
                    self.document_repository.delete_document(doc_id, scope)
                parsed_markdown = self._parsed_markdown(parsed)
                parsed_file = trace.write_text("parsed.md", parsed_markdown)
                trace.record_output(
                    load_span,
                    {
                        "doc_id": parsed.doc_id,
                        "file_type": parsed.file_type,
                        "elements": len(parsed.elements),
                        "images": len(parsed.images),
                        "characters": len(parsed_markdown),
                        "parsed_markdown_file": parsed_file,
                        "parser_diagnostics": asdict(parsed.diagnostics),
                        "document_metadata": parsed.metadata,
                    },
                )
                logger.info(
                    "document.processing.stage.end",
                    extra={"stage": "parse", "doc_id": parsed.doc_id, "elements": len(parsed.elements), "images": len(parsed.images)},
                )

            with trace.span(
                "chunk_strategy",
                input={
                    "requested_strategy": resolved_processing.requested.chunk_strategy,
                    "effective_strategy": resolved_processing.effective.chunk_strategy,
                    "parent_chunk_size_chars": resolved_processing.effective.parent_chunk_size_chars,
                    "child_chunk_size_chars": resolved_processing.effective.child_chunk_size_chars,
                    "child_chunk_overlap_chars": resolved_processing.effective.child_chunk_overlap_chars,
                },
            ) as chunk_span:
                if callable(cancel_check):
                    cancel_check()
                logger.info("document.processing.stage.start", extra={"stage": "chunk", "doc_id": parsed.doc_id})
                with trace.db_subspan(
                    chunk_span,
                    "chunk_strategy_attempt",
                    input={
                        "requested_strategy": resolved_processing.requested.chunk_strategy,
                        "effective_strategy": resolved_processing.effective.chunk_strategy,
                    },
                ):
                    chunks = self._with_source_metadata(
                        self.document_chunker.chunk(parsed),
                        source=source,
                        file_name=file_path.name,
                        scope=scope,
                        processing_version=resolved_processing.effective.processing_version,
                        size_unit=resolved_processing.effective.size_unit,
                        requested_parser_engine=resolved_processing.requested.parser_engine,
                        effective_parser_engine=resolved_processing.effective.parser_engine,
                    )
                chunks_file = trace.write_jsonl("chunks.jsonl", self._chunk_trace_rows(chunks))
                chunk_summary = self._chunk_trace_summary(chunks)
                trace.record_output(
                    chunk_span,
                    {
                        **chunk_summary,
                        "chunks_file": chunks_file,
                        "processing_version": resolved_processing.effective.processing_version,
                    },
                )
                logger.info(
                    "document.processing.stage.end",
                    extra={"stage": "chunk", "doc_id": parsed.doc_id, **chunk_summary},
                )

            with trace.span(
                "index",
                input={
                    "doc_id": parsed.doc_id,
                    "dense_enabled": dense_enabled,
                    "keyword_enabled": keyword_enabled,
                    **self._chunk_trace_summary(chunks),
                },
            ) as index_span:
                if callable(cancel_check):
                    cancel_check()
                logger.info("document.processing.stage.start", extra={"stage": "index", "doc_id": parsed.doc_id})
                self._mark_wiki_source_stale(parsed.doc_id, scope, reason="source_reindexed")
                self.document_repository.replace_chunks(parsed.doc_id, chunks, scope)
                self._reset_image_resources_for_reparse(parsed.doc_id, parsed.images, scope)
                if needs_embedding:
                    with trace.db_subspan(
                        index_span,
                        "embedding_batch",
                        input={
                            "doc_id": parsed.doc_id,
                            "chunks": len(chunks),
                            "vector_store": self.vector_store.__class__.__name__,
                        },
                    ):
                        replace_document_chunks = getattr(self.vector_store, "replace_document_chunks", None)
                        if callable(replace_document_chunks):
                            try:
                                replace_document_chunks(parsed.doc_id, chunks, scope=scope)
                            except TypeError:
                                replace_document_chunks(parsed.doc_id, chunks)
                        else:
                            self.vector_store.upsert_chunks(chunks)
                else:
                    self._delete_vector_document(parsed.doc_id, scope)
                self.document_repository.upsert_document(
                    id=parsed.doc_id,
                    name=parsed.file_name,
                    file_type=parsed.file_type,
                    storage_path=source,
                    parse_status="parsed",
                    metadata_json={
                        "size": file_size,
                        "elements": len(parsed.elements),
                        "chunks": len(chunks),
                        "parser": asdict(parsed.diagnostics),
                        "processing_trace_id": trace.trace_id,
                        "processing_trace_dir": str(trace.trace_dir),
                        **scope.to_dict(),
                        **processing_metadata,
                    },
                    workspace_id=scope.workspace_id,
                    knowledge_base_id=scope.knowledge_base_id,
                )
                trace.record_output(
                    index_span,
                    {
                        "sqlite_chunks": len(chunks),
                        "vector_chunks": len(
                            [
                                chunk
                                for chunk in chunks
                                if chunk.chunk_type in {"child", "table", "ocr", "image_ocr", "image_caption"}
                            ]
                        ) if needs_embedding else 0,
                        "embedding_status": "completed" if needs_embedding else "skipped",
                        "skip_reason": "dense_and_keyword_disabled" if not needs_embedding else "",
                    },
                )
                logger.info(
                    "document.processing.stage.end",
                    extra={"stage": "index", "doc_id": parsed.doc_id, "chunks": len(chunks)},
                )

            with trace.span(
                "multimodal",
                input={
                    "image_count": len(parsed.images),
                    "ocr_enabled": resolved_processing.effective.ocr_enabled,
                    "caption_enabled": resolved_processing.effective.caption_enabled,
                },
            ) as multimodal_span:
                if callable(cancel_check):
                    cancel_check()
                logger.info("document.processing.stage.start", extra={"stage": "multimodal", "doc_id": parsed.doc_id, "image_count": len(parsed.images)})
                with trace.db_subspan(
                    multimodal_span,
                    "multimodal_provider_calls",
                    input={
                        "image_count": len(parsed.images),
                        "ocr_enabled": resolved_processing.effective.ocr_enabled,
                        "caption_enabled": resolved_processing.effective.caption_enabled,
                    },
                ):
                    image_operation_summary = self._persist_image_operations(
                        parsed.doc_id,
                        parsed.images,
                        scope,
                        processing_settings,
                        resolved_processing=resolved_processing,
                    )
                    multimodal_task = None
                    if int(image_operation_summary.get("operations", 0) or 0) > 0:
                        multimodal_task = self._enqueue_async_document_stage(
                            IMAGE_MULTIMODAL_TASK,
                            parsed.doc_id,
                            scope,
                            payload={"image_count": len(parsed.images)},
                            stage="multimodal",
                        )
                    if multimodal_task is not None:
                        multimodal_summary = {
                            "total": image_operation_summary["operations"],
                            "completed": 0,
                            "failed": 0,
                            "canceled": 0,
                            "skipped": 0,
                            "queued": image_operation_summary["operations"],
                            "errors": [],
                            "task": multimodal_task,
                        }
                    else:
                        multimodal_summary = self.process_multimodal_operations(parsed.doc_id, scope)
                trace.record_output(
                    multimodal_span,
                    {
                        "image_resources": self._count_trace_value(image_operation_summary["resources"]),
                        "image_operations": self._count_trace_value(image_operation_summary["operations"]),
                        "image_errors": image_operation_summary["errors"],
                        "multimodal": multimodal_summary,
                    },
                )
                logger.info(
                    "document.processing.stage.end",
                    extra={
                        "stage": "multimodal",
                        "doc_id": parsed.doc_id,
                        "total": multimodal_summary.get("total", 0),
                        "failed": multimodal_summary.get("failed", 0),
                    },
                )

            with trace.span(
                "postprocess",
                input={"kg_enabled": graph_enabled, "wiki_enabled": wiki_enabled},
            ) as postprocess_span:
                if callable(cancel_check):
                    cancel_check()
                logger.info("document.processing.stage.start", extra={"stage": "postprocess", "doc_id": parsed.doc_id})
                postprocess_task = self._enqueue_async_document_stage(
                    KNOWLEDGE_POSTPROCESS_TASK,
                    parsed.doc_id,
                    scope,
                    payload={
                        "chunks": len(chunks),
                        "graph_enabled": graph_enabled,
                        "wiki_enabled": wiki_enabled,
                    },
                    stage="postprocess",
                )
                if postprocess_task is not None:
                    wiki_generation_result = {
                        "task": {
                            "status": "queued",
                            "task_id": postprocess_task["task_id"],
                            "queue": postprocess_task["queue"],
                        },
                        "page": None,
                        "proposal": None,
                    }
                    enrichment_queued = False
                else:
                    with trace.db_subspan(
                        postprocess_span,
                        "graph_extraction",
                        input={"enabled": graph_enabled, "chunks": len(chunks)},
                    ) as graph_span:
                        if graph_enabled:
                            graph_task = self._enqueue_async_document_stage(
                                GRAPH_EXTRACTION_TASK,
                                parsed.doc_id,
                                scope,
                                payload={"chunks": len(chunks)},
                                stage="postprocess",
                            )
                            if graph_task is None:
                                self._run_kg_enrichment(parsed.doc_id, chunks, scope)
                            elif graph_span is not None:
                                self.processing_trace_recorder.span_tracker.update_output(
                                    graph_span,
                                    {"queued": True, "task": graph_task},
                                )
                        else:
                            trace.mark_db_subspan_skipped(
                                graph_span,
                                reason="graph_disabled_by_strategy",
                                output={"enabled": False},
                            )
                    with trace.db_subspan(
                        postprocess_span,
                        "wiki_enqueue",
                        input={"enabled": wiki_enabled, "document_id": parsed.doc_id},
                    ) as wiki_enqueue_span:
                        if wiki_enabled:
                            wiki_generation_result = self._run_wiki_generation(parsed.doc_id, scope)
                        else:
                            wiki_generation_result = {
                                "task": {"status": "skipped", "reason": "wiki_disabled_by_strategy"},
                                "page": None,
                                "proposal": None,
                            }
                            trace.mark_db_subspan_skipped(
                                wiki_enqueue_span,
                                reason="wiki_disabled_by_strategy",
                                output={"enabled": False},
                            )
                    enrichment_queued = False
                    if self.document_enrichment_service is not None:
                        provider_known = hasattr(self.document_enrichment_service, "provider")
                        enrichment_queued = bool(
                            getattr(self.document_enrichment_service, "enabled", False)
                            and (not provider_known or getattr(self.document_enrichment_service, "provider", None) is not None)
                        )
                        with trace.db_subspan(
                            postprocess_span,
                            "summary_generation",
                            kind="generation",
                            input={"enabled": enrichment_queued, "chunks": len(chunks)},
                        ):
                            self.document_enrichment_service.enqueue(parsed.doc_id, chunks, scope)
                trace.record_output(
                    postprocess_span,
                    {
                        "kg_attempted": graph_enabled,
                        "wiki_generation": wiki_generation_result,
                        "enrichment_queued": enrichment_queued,
                        "postprocess_task": postprocess_task,
                    },
                )
                if not needs_embedding:
                    trace.mark_skipped(
                        index_span,
                        reason="dense_and_keyword_disabled",
                        output={"sqlite_chunks": len(chunks), "vector_chunks": 0},
                    )
                logger.info(
                    "document.processing.stage.end",
                    extra={
                        "stage": "postprocess",
                        "doc_id": parsed.doc_id,
                        "kg_enabled": graph_enabled,
                        "wiki_generation_status": (wiki_generation_result.get("task") or {}).get("status", "skipped")
                        if isinstance(wiki_generation_result, dict)
                        else "skipped",
                        "enrichment_queued": enrichment_queued,
                    },
                )

            result = {
                "doc_id": parsed.doc_id,
                "source": source,
                "parent_chunks": len([chunk for chunk in chunks if chunk.chunk_type == "parent"]),
                "child_chunks": len([chunk for chunk in chunks if chunk.chunk_type == "child"]),
                "table_chunks": len([chunk for chunk in chunks if chunk.chunk_type == "table"]),
                "indexed_chunks": len([chunk for chunk in chunks if chunk.chunk_type in {"child", "table", "ocr", "image_ocr", "image_caption"}]) if needs_embedding else 0,
                "persisted_chunks": len(chunks),
                "image_resources": image_operation_summary["resources"],
                "image_operations": image_operation_summary["operations"],
                "image_operation_errors": image_operation_summary["errors"],
                "multimodal": multimodal_summary,
                "wiki_generation": wiki_generation_result,
                "parser_warnings": list(getattr(parsed.diagnostics, "warnings", ()) or ()),
                "requested_processing": resolved_processing.requested.to_dict(),
                "effective_processing": resolved_processing.effective.to_dict(),
                "processing_version": resolved_processing.effective.processing_version,
                "processing_trace_id": trace.trace_id,
                "processing_trace_dir": str(trace.trace_dir),
                "preview": "\n\n".join(element.markdown for element in parsed.elements[:20])[:2000],
            }
            trace.finish()
            logger.info(
                "document.processing.end",
                extra={
                    "workspace_id": scope.workspace_id,
                    "knowledge_base_id": scope.knowledge_base_id,
                    "doc_id": parsed.doc_id,
                    "indexed_chunks": result["indexed_chunks"],
                    "status": "completed",
                },
            )
            return result
        except Exception as exc:
            logger.exception(
                "document.processing.failed",
                extra={
                    "workspace_id": scope.workspace_id,
                    "knowledge_base_id": scope.knowledge_base_id,
                    "doc_id": doc_id,
                    "source": source,
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                },
            )
            self.document_repository.upsert_document(
                id=doc_id,
                name=file_path.name,
                file_type=file_path.suffix.lower().lstrip("."),
                storage_path=source,
                parse_status="failed",
                metadata_json={
                    "error_message": str(exc),
                    "processing_trace_id": trace.trace_id,
                    "processing_trace_dir": str(trace.trace_dir),
                    **scope.to_dict(),
                    **processing_metadata,
                },
                workspace_id=scope.workspace_id,
                knowledge_base_id=scope.knowledge_base_id,
            )
            trace.finish(error=exc)
            setattr(exc, "processing_trace_id", trace.trace_id)
            setattr(exc, "processing_trace_dir", str(trace.trace_dir))
            raise

    def _parsed_markdown(self, parsed: Any) -> str:
        markdown = str(getattr(parsed, "markdown", "") or "").strip()
        if markdown:
            return markdown
        return "\n\n".join(str(getattr(element, "markdown", "") or "") for element in parsed.elements).strip()

    def _count_trace_value(self, value: Any) -> int:
        if isinstance(value, int):
            return value
        if isinstance(value, (list, tuple, set, dict)):
            return len(value)
        return 0

    def _source_revision_for_file(self, file_path: Path, processing_settings: dict[str, Any] | None = None) -> str:
        resolved = self._resolve_processing_settings(processing_settings)
        stat = file_path.stat()
        source = file_path.resolve().as_posix()
        try:
            source = file_path.resolve().relative_to(self.data_dir.resolve()).as_posix()
        except ValueError:
            pass
        raw = json.dumps(
            {
                "source": source,
                "size": int(stat.st_size),
                "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
                "processing": resolved.effective.to_dict(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _indexing_state(self, scope: KnowledgeBaseScope, processing_settings: dict[str, Any] | None = None) -> dict[str, bool]:
        indexing_strategy = None
        if self.knowledge_base_service is not None:
            indexing_strategy = self.knowledge_base_service.resolve_indexing_strategy(scope)
        dense_enabled = bool(getattr(indexing_strategy, "dense_enabled", True))
        keyword_enabled = bool(getattr(indexing_strategy, "keyword_enabled", True))
        state = {
            "dense_enabled": dense_enabled,
            "keyword_enabled": keyword_enabled,
            "graph_enabled": bool(getattr(indexing_strategy, "graph_enabled", self.kg_extraction_enabled)),
            "wiki_enabled": bool(getattr(indexing_strategy, "wiki_enabled", False)),
        }
        if processing_settings is not None:
            state.update(UPLOAD_REQUIRED_INDEXING_FLAGS)
        state["needs_embedding"] = bool(state["dense_enabled"] or state["keyword_enabled"])
        return state

    def _chunk_from_repository_row(self, row: dict[str, Any]) -> Chunk:
        return Chunk(
            id=str(row["id"]),
            doc_id=str(row["doc_id"]),
            parent_id=row.get("parent_id"),
            chunk_type=str(row["chunk_type"]),
            title_path=str(row.get("title_path", "")),
            content=str(row.get("content", "")),
            content_markdown=str(row.get("content_markdown", "")),
            page_start=row.get("page_start"),
            page_end=row.get("page_end"),
            token_count=int(row.get("token_count", 0) or 0),
            metadata=dict(row.get("metadata_json", {})),
        )

    def _assert_document_revision_current(
        self,
        doc_id: str,
        scope: KnowledgeBaseScope,
        source_revision: str,
    ) -> None:
        if not source_revision:
            return
        document = self.document_repository.get_document(doc_id, scope)
        metadata = dict((document or {}).get("metadata_json") or {})
        current = str(metadata.get("source_revision") or "")
        if current and current != source_revision:
            raise StaleDocumentRevision(f"Document {doc_id} revision changed before staged task execution")

    def _chunk_trace_summary(self, chunks: list[Chunk]) -> dict[str, Any]:
        lengths = [len(chunk.content or "") for chunk in chunks]
        child_target = int(getattr(self.document_chunker, "child_max_tokens", self.chunk_size) or self.chunk_size)
        by_type: dict[str, int] = {}
        by_strategy: dict[str, int] = {}
        tier_chains: list[list[str]] = []
        for chunk in chunks:
            by_type[chunk.chunk_type] = by_type.get(chunk.chunk_type, 0) + 1
            strategy = str(chunk.metadata.get("strategy") or chunk.strategy or "").strip() or "unknown"
            by_strategy[strategy] = by_strategy.get(strategy, 0) + 1
            tier_chain = chunk.metadata.get("tier_chain")
            if isinstance(tier_chain, (list, tuple)):
                normalized_chain = [str(item) for item in tier_chain]
                if normalized_chain and normalized_chain not in tier_chains:
                    tier_chains.append(normalized_chain)
        average = sum(lengths) / len(lengths) if lengths else 0.0
        return {
            "chunk_count": len(chunks),
            "by_type": by_type,
            "by_strategy": by_strategy,
            "tier_chains": tier_chains,
            "lengths": {
                "minimum": min(lengths, default=0),
                "maximum": max(lengths, default=0),
                "average": average,
                "tiny_count": sum(1 for length in lengths if length < 50),
                "oversize_count": sum(1 for length in lengths if length > child_target * 2),
            },
        }

    def _chunk_trace_rows(self, chunks: list[Chunk]) -> list[dict[str, Any]]:
        return [
            {
                "id": chunk.id,
                "doc_id": chunk.doc_id,
                "parent_id": chunk.parent_id,
                "chunk_type": chunk.chunk_type,
                "title_path": chunk.title_path,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "characters": len(chunk.content or ""),
                "approx_tokens": chunk.token_count,
                "strategy": chunk.metadata.get("strategy", chunk.strategy),
                "metadata": chunk.metadata,
                "content": chunk.content,
                "content_markdown": chunk.content_markdown,
            }
            for chunk in chunks
        ]

    def _run_kg_enrichment(self, doc_id: str, chunks: list[Chunk], scope: KnowledgeBaseScope) -> None:
        if self.kg_service is None:
            return
        try:
            try:
                self.kg_service.enrich_document(doc_id, chunks, scope=scope)
            except TypeError:
                self.kg_service.enrich_document(doc_id, chunks)
        except Exception as exc:
            logger.warning("KG enrichment failed for document %s without failing Raw RAG ingest: %s", doc_id, exc)

    def _enqueue_async_document_stage(
        self,
        task_type: str,
        doc_id: str,
        scope: KnowledgeBaseScope,
        *,
        payload: dict[str, Any] | None = None,
        stage: str,
        source_revision: str = "",
    ) -> dict[str, Any] | None:
        queue = getattr(self, "async_processing_queue", None)
        worker = getattr(self, "processing_worker", None)
        repository = getattr(worker, "repository", None)
        if queue is None or repository is None or not getattr(queue, "enabled", False):
            return None
        revision = source_revision or self._document_processing_revision(doc_id, scope)
        task_payload = {"schema_version": 1, "doc_id": doc_id, "source_revision": revision, **(payload or {})}
        task = repository.create_task(
            task_type,
            scope,
            document_id=doc_id,
            upload_batch_id=str(task_payload.get("batch_id") or ""),
            upload_file_id=str(task_payload.get("file_id") or ""),
            payload=task_payload,
            max_attempts=worker.config.max_attempts_for_stage(stage),
            trace_id=_current_log_trace_id() or "",
            idempotency_key=f"{task_type}:{scope.knowledge_base_id}:{doc_id}:{revision}",
            source_revision=revision,
        )
        dispatch_queue = route_for_task_type(task_type)
        broker_task_id = str(task.get("broker_task_id") or "")
        if str(task.get("status") or "") in {"pending", "retrying"}:
            dispatch = queue.enqueue(task)
            dispatch_queue = dispatch.queue
            broker_task_id = dispatch.broker_task_id
            record_dispatch = getattr(repository, "record_broker_dispatch", None)
            if callable(record_dispatch):
                task = record_dispatch(task["id"], broker_task_id=dispatch.broker_task_id, queue_name=dispatch.queue)
        return {
            "task_id": task["id"],
            "task_type": task_type,
            "status": task.get("status"),
            "queue": dispatch_queue,
            "broker_task_id": broker_task_id,
            "source_revision": revision,
        }

    def _document_processing_revision(self, doc_id: str, scope: KnowledgeBaseScope) -> str:
        document = self.document_repository.get_document(doc_id, scope)
        if document is None:
            return ""
        metadata = dict(document.get("metadata_json") or {})
        marker = metadata.get("source_revision") or metadata.get("processing_trace_id") or document.get("updated_at")
        return ":".join(
            [
                str(marker or ""),
                str(metadata.get("processing_version") or ""),
                str(metadata.get("chunks") or ""),
            ]
        )

    def _with_ocr_elements(self, parser: DocumentParser, file_path: Path, parsed):
        extractor = getattr(parser, "extract_ocr_elements", None)
        if not callable(extractor):
            return parsed
        try:
            ocr_elements = extractor(file_path)
        except Exception as exc:
            logger.warning("OCR extraction failed for %s: %s", file_path, exc)
            return parsed
        if not ocr_elements:
            return parsed
        return replace(parsed, elements=[*parsed.elements, *ocr_elements])

    def _persist_image_operations(
        self,
        doc_id: str,
        images: list[Any],
        scope: KnowledgeBaseScope,
        processing_settings: dict[str, Any] | None,
        *,
        resolved_processing: Any | None = None,
    ) -> dict[str, Any]:
        if not images:
            return {"resources": 0, "operations": 0, "errors": []}
        effective = (
            resolved_processing.effective.to_dict()
            if resolved_processing is not None
            else self._effective_upload_settings(processing_settings)["effective"]
        )
        operation_types: list[str] = []
        if effective.get("ocr_enabled"):
            operation_types.append("ocr")
        if effective.get("caption_enabled"):
            operation_types.append("caption")
        if not operation_types:
            return {"resources": 0, "operations": 0, "errors": []}

        resources = 0
        operations = 0
        errors: list[str] = []
        storage_provider = str(effective.get("media_storage_provider") or "local")
        for image in images:
            try:
                try:
                    self.image_repository.get_image(image.image_id, scope)
                except FileNotFoundError:
                    self.image_repository.add_image(doc_id, image, scope, storage_provider=storage_provider)
                    resources += 1
                for operation_type in operation_types:
                    self.image_repository.create_operation(image.image_id, doc_id, operation_type, scope)
                    operations += 1
            except Exception as exc:
                message = f"{getattr(image, 'image_id', '<unknown>')}: {exc}"
                errors.append(message)
                logger.warning("Image operation registration failed for document %s: %s", doc_id, message)
        return {"resources": resources, "operations": operations, "errors": errors}

    def _reset_image_resources_for_reparse(
        self,
        doc_id: str,
        current_images: list[Any],
        scope: KnowledgeBaseScope,
    ) -> None:
        keep_storage_keys = {str(image.storage_key) for image in current_images}
        existing = self.image_repository.list_images(doc_id, scope)
        for image in existing:
            storage_key = self.image_repository.delete_image(str(image["id"]), scope)
            if storage_key not in keep_storage_keys:
                self.object_storage.delete(storage_key)

    def process_multimodal_operations(
        self,
        doc_id: str,
        scope: KnowledgeBaseScope,
        *,
        operation_ids: list[str] | tuple[str, ...] | None = None,
        max_workers: int | None = None,
    ) -> dict[str, Any]:
        selected = set(operation_ids or ())
        operations = [
            operation
            for operation in self.image_repository.list_operations(doc_id, scope)
            if operation["status"] == "pending" and (not selected or operation["id"] in selected)
        ]
        if not operations:
            return {"total": 0, "completed": 0, "failed": 0, "canceled": 0, "skipped": 0, "errors": []}

        summary = {"total": len(operations), "completed": 0, "failed": 0, "canceled": 0, "skipped": 0, "errors": []}
        workers = max(1, min(int(max_workers or self.multimodal_max_workers), len(operations)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="multimodal") as executor:
            futures = [executor.submit(self._process_single_multimodal_operation, operation, scope) for operation in operations]
            for future in as_completed(futures):
                result = future.result()
                status = str(result.get("status", "failed"))
                if status in summary:
                    summary[status] += 1
                else:
                    summary["failed"] += 1
                if result.get("error"):
                    summary["errors"].append(str(result["error"]))
        return summary

    def retry_image_operation(self, operation_id: str, scope: KnowledgeBaseScope) -> dict[str, Any]:
        operation = self.image_repository.retry_operation(operation_id, scope)
        summary = self.process_multimodal_operations(
            str(operation["doc_id"]),
            scope,
            operation_ids=[operation_id],
        )
        return {"operation": self.image_repository.get_operation(operation_id, scope), "summary": summary}

    def cancel_multimodal_operations(self, doc_id: str, scope: KnowledgeBaseScope) -> dict[str, Any]:
        canceled = self.image_repository.cancel_document_operations(doc_id, scope)
        return {"doc_id": doc_id, "canceled": canceled}

    def _process_single_multimodal_operation(
        self,
        operation: dict[str, Any],
        scope: KnowledgeBaseScope,
    ) -> dict[str, Any]:
        operation_id = str(operation["id"])
        try:
            with self._multimodal_db_lock:
                current = self.image_repository.get_operation(operation_id, scope)
                if current["status"] == "canceled":
                    return {"status": "canceled"}
                if current["status"] != "pending":
                    return {"status": "skipped"}
                self.image_repository.update_operation(
                    operation_id,
                    scope,
                    status="processing",
                    increment_attempt=int(current.get("attempt") or 0) == 0,
                )
                image = self.image_repository.get_image(str(operation["image_id"]), scope)
            image_bytes = self.object_storage.read(str(image["storage_key"]))
            parsed_image = self._parsed_image_from_row(image)
            operation_type = str(operation["operation_type"])
            if operation_type == "ocr":
                if not self.ocr_provider_service.available:
                    raise RuntimeError("OCR provider is unavailable")
                result = self.ocr_provider_service.extract_text(image_bytes, str(image["mime_type"]))
                result_type = "image_ocr"
            elif operation_type == "caption":
                if not self.caption_provider_service.available:
                    raise RuntimeError("Caption provider is unavailable")
                result = self.caption_provider_service.describe(image_bytes, str(image["mime_type"]))
                result_type = "image_caption"
            else:
                raise ValueError("Unsupported image operation")

            with self._multimodal_db_lock:
                chunk = image_result_chunk(
                    doc_id=str(operation["doc_id"]),
                    image=parsed_image,
                    result=result,
                    result_type=result_type,
                    parent_id=self._nearest_parent_id_for_image(str(operation["doc_id"]), parsed_image, scope),
                    title_path=str((image.get("metadata") or {}).get("title_path") or ""),
                    scope_metadata={
                        "workspace_id": scope.workspace_id,
                        "knowledge_base_id": scope.knowledge_base_id,
                        "operation_id": operation_id,
                    },
                )
                self.document_repository.upsert_chunks(str(operation["doc_id"]), [chunk], scope)
                upsert_chunks = getattr(self.vector_store, "upsert_chunks", None)
                if callable(upsert_chunks):
                    upsert_chunks([chunk])
                self.image_repository.update_operation(
                    operation_id,
                    scope,
                    status="completed",
                    provider_ref=result.provider,
                    result_chunk_id=chunk.id,
                    error_message="",
                )
            return {"status": "completed", "chunk_id": chunk.id}
        except Exception as exc:
            logger.warning("Multimodal operation failed: %s", exc)
            try:
                with self._multimodal_db_lock:
                    self.image_repository.update_operation(
                        operation_id,
                        scope,
                        status="failed",
                        error_message=str(exc),
                    )
            except Exception:
                logger.exception("Failed to persist multimodal operation failure")
            return {"status": "failed", "error": str(exc)}

    def _parsed_image_from_row(self, image: dict[str, Any]):
        from app.models.document_models import ParsedImage

        return ParsedImage(
            image_id=str(image["id"]),
            storage_key=str(image["storage_key"]),
            source_type=str(image["source_type"]),
            page_number=image.get("page_number"),
            mime_type=str(image.get("mime_type") or "image/jpeg"),
            width=image.get("width"),
            height=image.get("height"),
            metadata=dict(image.get("metadata") or {}),
        )

    def _nearest_parent_id_for_image(
        self,
        doc_id: str,
        image,
        scope: KnowledgeBaseScope,
    ) -> str | None:
        parents = self.document_repository.list_chunks(doc_id=doc_id, chunk_types={"parent"}, scope=scope)
        if not parents:
            return None
        page_number = image.page_number
        if page_number is not None:
            for parent in parents:
                start = parent.get("page_start")
                end = parent.get("page_end")
                if start is not None and end is not None and int(start) <= int(page_number) <= int(end):
                    return str(parent["id"])
        return str(parents[0]["id"])

    def _with_source_metadata(
        self,
        chunks: list[Chunk],
        source: str,
        file_name: str,
        scope: KnowledgeBaseScope | None = None,
        processing_version: str = PROCESSING_VERSION,
        size_unit: str = "chars",
        requested_parser_engine: str = "builtin",
        effective_parser_engine: str = "builtin",
    ) -> list[Chunk]:
        scope = scope or self.default_scope
        return [
            replace(
                chunk,
                metadata={
                    **chunk.metadata,
                    "source": source,
                    "file_name": file_name,
                    "workspace_id": scope.workspace_id,
                    "knowledge_base_id": scope.knowledge_base_id,
                    "processing_version": processing_version,
                    "size_unit": size_unit,
                    "requested_parser_engine": requested_parser_engine,
                    "effective_parser_engine": effective_parser_engine,
                },
            )
            for chunk in chunks
        ]

    def _tokenize_query(self, text: str) -> list[str]:
        tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{2,}", text.lower())
        expanded: list[str] = []
        for token in tokens:
            expanded.append(token)
            if re.fullmatch(r"[\u4e00-\u9fff]{3,}", token):
                expanded.extend(token[i : i + 2] for i in range(len(token) - 1))
        return list(dict.fromkeys(expanded))

    def keyword_retrieve_hits(
        self,
        question: str,
        top_k: int | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> list[dict[str, Any]]:
        scope = scope or self.default_scope
        limit = top_k or self.bm25_recall_top_n or self.top_k
        try:
            fts_hits = self.keyword_search.search(question, limit, filters={"scope": scope, "doc_ids": scope.document_ids})
        except Exception as exc:
            logger.warning("PostgreSQL keyword retrieval failed: %s", exc)
            fts_hits = []
        if fts_hits:
            return [self._retrieved_chunk_to_hit(hit) for hit in fts_hits]

        tokens = self._tokenize_query(question)
        if not tokens:
            return []

        scored: list[dict[str, Any]] = []
        repository_chunks = self.document_repository.list_chunks(chunk_types={"child", "table"}, scope=scope)
        if repository_chunks:
            for chunk in repository_chunks:
                text = str(chunk.get("content", ""))
                embedding_text = "\n".join([str(chunk.get("title_path", "")), text, json.dumps(chunk.get("metadata_json", {}), ensure_ascii=False)])
                matched = [token for token in tokens if token in embedding_text.lower()]
                if not matched:
                    continue
                score = len(matched) / len(tokens)
                scored.append(
                    {
                        "content": text,
                        "metadata": {
                            "source": chunk.get("metadata_json", {}).get("source", chunk.get("storage_path", "unknown")),
                            "parent_id": chunk.get("parent_id", ""),
                            "child_id": chunk.get("id", ""),
                            "chunk_id": chunk.get("id", ""),
                            "chunk_type": chunk.get("chunk_type", ""),
                            "title_path": chunk.get("title_path", ""),
                            "workspace_id": chunk.get("workspace_id", ""),
                            "knowledge_base_id": chunk.get("knowledge_base_id", ""),
                        },
                        "distance": max(0.0, 1.0 - score),
                        "keyword_score": score,
                    }
                )
        else:
            scored.extend(self._legacy_keyword_hits(tokens))

        scored.sort(key=lambda hit: hit["keyword_score"], reverse=True)
        return scored[:limit]

    def _retrieved_chunk_to_hit(self, chunk: RetrievedChunk) -> dict[str, Any]:
        metadata = {
            **chunk.metadata,
            "source": chunk.metadata.get("source", chunk.doc_id),
            "child_id": chunk.chunk_id,
            "chunk_id": chunk.chunk_id,
            "doc_id": chunk.doc_id,
            "parent_id": chunk.parent_id,
            "chunk_type": chunk.chunk_type,
            "title_path": chunk.title_path,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
        }
        return {
            "content": chunk.content,
            "metadata": metadata,
            "distance": max(0.0, 1.0 - float(chunk.score)),
            "keyword_score": float(chunk.score),
            "bm25_score": float(chunk.bm25_score if chunk.bm25_score is not None else chunk.score),
        }

    def _legacy_keyword_hits(self, tokens: list[str]) -> list[dict[str, Any]]:
        scored = []
        for item in self.keyword_items:
            text = str(item.get("child_text", ""))
            matched = [token for token in tokens if token in text.lower()]
            if matched:
                score = len(matched) / len(tokens)
                scored.append(
                    {
                        "content": text,
                        "metadata": {
                            "source": item.get("source", "unknown"),
                            "parent_id": item.get("parent_id", ""),
                            "child_id": item.get("child_id", ""),
                        },
                        "distance": max(0.0, 1.0 - score),
                        "keyword_score": score,
                    }
                )
        return scored

    def retrieve_hits(
        self,
        question: str,
        top_k: int | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> list[dict[str, Any]]:
        scope = scope or self.default_scope
        limit = top_k or self.top_k
        query_dense = getattr(self.vector_store, "query_dense", None)
        query_method = query_dense if callable(query_dense) else self.vector_store.query
        try:
            hits = query_method(question, limit, scope=scope)
        except TypeError:
            if not scope.compatibility_default:
                raise RuntimeError("Dense provider does not support knowledge-base scope")
            hits = query_method(question, limit)
        filtered_hits: list[dict[str, Any]] = []
        for hit in hits:
            distance = float(hit.get("distance", 1.0))
            score = max(0.0, 1.0 - distance)
            if score >= self.min_relevance_score:
                hit["vector_score"] = score
                filtered_hits.append(hit)
        return filtered_hits

    def hybrid_retrieve_hits(
        self,
        question: str,
        scope: KnowledgeBaseScope | None = None,
    ) -> list[dict[str, Any]]:
        scope = scope or self.default_scope
        retrieval_span = get_observability_sink().start_span(
            name="retrieval.hybrid",
            input={"query": question, "scope": scope.to_dict()},
            metadata={"trace_id": _current_log_trace_id(), "knowledge_base_scope": scope.to_dict()},
        )
        try:
            result = self._hybrid_retrieve_hits_impl(question, scope)
            retrieval_span.finish(
                output={
                    "hit_count": len(result),
                    "debug": self._last_retrieval_debug,
                    "top_hits": self._debug_hits(result[:10]),
                }
            )
            return result
        except Exception as exc:
            retrieval_span.finish(output={"debug": self._last_retrieval_debug}, error=exc)
            raise

    def _hybrid_retrieve_hits_impl(
        self,
        question: str,
        scope: KnowledgeBaseScope,
    ) -> list[dict[str, Any]]:
        understanding = self._understand_query(question)
        retrieval_queries = understanding.retrieval_queries or [question]
        direct_hits = self._direct_load_selected_document_hits(scope)
        if direct_hits is not None:
            self._last_retrieval_debug = {
                "query_understanding": understanding.to_dict(),
                "knowledge_base_scope": scope.to_dict(),
                "selected_document_ids": list(scope.document_ids),
                "direct_load": {
                    **self._direct_load_debug(scope, direct_hits, used=True),
                    "decision": "used",
                },
                "dense_results": [],
                "bm25_results": [],
                "fused_results": self._debug_hits(direct_hits),
                "reranked_results": [],
                "query_expansion": {"enabled": self.low_recall_query_expansion_enabled, "used": False},
                "duplicate_removal": {"input_count": len(direct_hits), "output_count": len(direct_hits), "removed_count": 0},
                "mmr": {"enabled": self.mmr_enabled, "used": False},
                "retrieval_stages": {
                    "dense": {"skipped": True, "reason": "direct_load"},
                    "keyword": {"skipped": True, "reason": "direct_load"},
                    "fusion": {"input_count": len(direct_hits), "output_count": len(direct_hits)},
                },
            }
            selected = self._apply_reranker(question, direct_hits)
            return self._finalize_retrieval_selection(selected)

        dense_enabled, keyword_enabled = self._retrieval_channels_enabled(scope)
        vector_hits: list[dict[str, Any]] = []
        keyword_hits: list[dict[str, Any]] = []
        keyword_fallback_attempted = False
        for retrieval_query in retrieval_queries:
            if dense_enabled:
                vector_hits.extend(
                    self._tag_retrieval_hits(
                        self.retrieve_hits(retrieval_query, top_k=self.dense_recall_top_n, scope=scope), retrieval_query
                    )
                )
            if keyword_enabled:
                keyword_hits.extend(
                    self._tag_retrieval_hits(
                        self.keyword_retrieve_hits(retrieval_query, top_k=self.bm25_recall_top_n, scope=scope), retrieval_query
                    )
                )
        if not vector_hits and not keyword_hits and not keyword_enabled:
            keyword_fallback_attempted = True
            for retrieval_query in retrieval_queries:
                keyword_hits.extend(
                    self._tag_retrieval_hits(
                        self.keyword_retrieve_hits(retrieval_query, top_k=self.bm25_recall_top_n, scope=scope),
                        retrieval_query,
                    )
                )
        fused_hits = self._hydrate_retrieval_hit_content(
            self._fuse_retrieval_hits(vector_hits, keyword_hits), scope=scope
        )
        query_expansion_debug = {
            "enabled": self.low_recall_query_expansion_enabled,
            "used": False,
            "reason": "disabled",
            "initial_query_count": len(retrieval_queries),
            "expanded_queries": [],
            "initial_candidate_count": len(fused_hits),
            "final_candidate_count": len(fused_hits),
            "min_candidates": self.low_recall_min_candidates,
            "min_score": self.low_recall_min_score,
        }
        if self._should_expand_low_recall(fused_hits):
            expanded_queries = self._low_recall_expanded_queries(question, understanding, retrieval_queries)
            query_expansion_debug["reason"] = "low_recall" if expanded_queries else "no_expansion_queries"
            if expanded_queries:
                query_expansion_debug["used"] = True
                query_expansion_debug["expanded_queries"] = expanded_queries
                for retrieval_query in expanded_queries:
                    if dense_enabled:
                        vector_hits.extend(
                            self._tag_retrieval_hits(
                                self.retrieve_hits(retrieval_query, top_k=self.dense_recall_top_n, scope=scope),
                                retrieval_query,
                            )
                        )
                    if keyword_enabled:
                        keyword_hits.extend(
                            self._tag_retrieval_hits(
                                self.keyword_retrieve_hits(retrieval_query, top_k=self.bm25_recall_top_n, scope=scope),
                                retrieval_query,
                            )
                        )
                if not vector_hits and not keyword_hits and not keyword_enabled:
                    keyword_fallback_attempted = True
                    for retrieval_query in expanded_queries:
                        keyword_hits.extend(
                            self._tag_retrieval_hits(
                                self.keyword_retrieve_hits(retrieval_query, top_k=self.bm25_recall_top_n, scope=scope),
                                retrieval_query,
                            )
                        )
                fused_hits = self._hydrate_retrieval_hit_content(
                    self._fuse_retrieval_hits(vector_hits, keyword_hits), scope=scope
                )
                query_expansion_debug["final_candidate_count"] = len(fused_hits)
        elif self.low_recall_query_expansion_enabled:
            query_expansion_debug["reason"] = "recall_sufficient"

        deduped_hits = self._remove_duplicate_retrieval_hits(fused_hits)
        direct_debug = self._direct_load_debug(scope, [], used=False)
        self._last_retrieval_debug = {
            "query_understanding": understanding.to_dict(),
            "knowledge_base_scope": scope.to_dict(),
            "selected_document_ids": list(scope.document_ids),
            "direct_load": direct_debug,
            "dense_results": self._debug_hits(vector_hits),
            "bm25_results": self._debug_hits(keyword_hits),
            "fused_results": self._debug_hits(fused_hits),
            "reranked_results": [],
            "query_expansion": query_expansion_debug,
            "duplicate_removal": self._duplicate_removal_debug(fused_hits, deduped_hits),
            "mmr": {"enabled": self.mmr_enabled, "used": False},
            "retrieval_stages": {
                "dense": (
                    {"query_count": len(retrieval_queries), "candidate_count": len(vector_hits)}
                    if dense_enabled
                    else {"skipped": True, "reason": "disabled_by_indexing_strategy", "candidate_count": 0}
                ),
                "keyword": (
                    {"query_count": len(retrieval_queries), "candidate_count": len(keyword_hits)}
                    if keyword_enabled
                    else {
                        "skipped": not keyword_fallback_attempted,
                        "reason": "repository_keyword_fallback" if keyword_fallback_attempted else "disabled_by_indexing_strategy",
                        "candidate_count": len(keyword_hits),
                    }
                ),
                "query_expansion": query_expansion_debug,
                "fusion": {"input_count": len(vector_hits) + len(keyword_hits), "output_count": len(fused_hits)},
                "duplicate_removal": self._duplicate_removal_debug(fused_hits, deduped_hits),
            },
        }

        if self.reranker_enabled and self.reranker is not None:
            return self._finalize_retrieval_selection(self._apply_reranker(question, deduped_hits))
        return self._finalize_retrieval_selection(deduped_hits)

    def _retrieval_channels_enabled(self, scope: KnowledgeBaseScope) -> tuple[bool, bool]:
        if self.knowledge_base_service is None:
            return True, True
        try:
            indexing_strategy = self.knowledge_base_service.resolve_indexing_strategy(scope)
        except Exception:
            logger.warning("retrieval.indexing_strategy.resolve_failed", exc_info=True)
            return True, True
        dense_enabled = bool(getattr(indexing_strategy, "dense_enabled", True))
        keyword_enabled = bool(getattr(indexing_strategy, "keyword_enabled", True))
        return dense_enabled, keyword_enabled

    def _direct_load_selected_document_hits(self, scope: KnowledgeBaseScope) -> list[dict[str, Any]] | None:
        doc_ids = tuple(scope.document_ids)
        if not doc_ids or self.direct_load_max_chunks <= 0:
            return None
        chunk_types = {"child", "table", "image_ocr", "image_caption"}
        counts = self.document_repository.count_chunks_for_documents(doc_ids, chunk_types=chunk_types, scope=scope)
        total = sum(counts.values())
        if total == 0 or total > self.direct_load_max_chunks:
            return None
        chunks = self.document_repository.list_chunks_for_documents(
            doc_ids,
            chunk_types=chunk_types,
            scope=scope,
            limit=self.direct_load_max_chunks,
        )
        hits: list[dict[str, Any]] = []
        for rank, chunk in enumerate(chunks, start=1):
            hits.append(self._chunk_row_to_direct_hit(chunk, rank))
        return hits

    def _direct_load_debug(self, scope: KnowledgeBaseScope, hits: list[dict[str, Any]], *, used: bool) -> dict[str, Any]:
        doc_ids = tuple(scope.document_ids)
        counts = (
            self.document_repository.count_chunks_for_documents(
                doc_ids,
                chunk_types={"child", "table", "image_ocr", "image_caption"},
                scope=scope,
            )
            if doc_ids
            else {}
        )
        total = sum(counts.values())
        skipped = [doc_id for doc_id, count in counts.items() if count == 0]
        decision = "used" if used else "not_requested"
        if doc_ids and self.direct_load_max_chunks <= 0:
            decision = "disabled"
        elif doc_ids and total > self.direct_load_max_chunks:
            decision = "over_limit"
        elif doc_ids and total == 0:
            decision = "no_chunks"
        elif doc_ids and not used:
            decision = "not_used"
        return {
            "used": used,
            "decision": decision,
            "selected_document_ids": list(doc_ids),
            "loaded_chunk_count": len(hits),
            "selected_chunk_count": total,
            "skipped_document_ids": skipped,
            "max_chunks": self.direct_load_max_chunks,
        }

    def _should_expand_low_recall(self, hits: list[dict[str, Any]]) -> bool:
        if not self.low_recall_query_expansion_enabled:
            return False
        if len(hits) < max(1, int(self.low_recall_min_candidates)):
            return True
        top_score = max((self._retrieval_score(hit) for hit in hits), default=0.0)
        return top_score < float(self.low_recall_min_score)

    def _low_recall_expanded_queries(
        self,
        question: str,
        understanding: QueryUnderstandingResult,
        existing_queries: list[str],
    ) -> list[str]:
        existing = {query.strip() for query in existing_queries if query.strip()}
        candidates: list[str] = []
        if understanding.normalized_query and understanding.normalized_query not in existing:
            candidates.append(understanding.normalized_query)
        for term in understanding.expanded_terms:
            if term and term not in question:
                candidates.append(f"{question} {term}")
        candidates.extend(
            [
                f"{question} specification",
                f"{question} configuration",
                f"{question} manual",
            ]
        )
        result: list[str] = []
        for candidate in candidates:
            cleaned = candidate.strip()
            if not cleaned or cleaned in existing or cleaned in result:
                continue
            result.append(cleaned)
            if len(result) >= max(0, int(self.low_recall_max_queries)):
                break
        return result

    def _duplicate_removal_debug(self, before: list[dict[str, Any]], after: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "input_count": len(before),
            "output_count": len(after),
            "removed_count": max(0, len(before) - len(after)),
            "overlap_threshold": self.duplicate_overlap_threshold,
        }

    def _remove_duplicate_retrieval_hits(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for hit in sorted(hits, key=self._retrieval_score, reverse=True):
            metadata = hit.get("metadata", {})
            identity = self._retrieval_identity(metadata, prefer_parent=False)
            if identity and identity in seen_keys:
                continue
            content = str(hit.get("content", ""))
            if any(self._retrieval_hits_near_duplicate(existing, hit) for existing in selected):
                continue
            selected.append(hit)
            if identity:
                seen_keys.add(identity)
            content_signature = self._content_signature(content)
            if content_signature:
                seen_keys.add(f"content:{content_signature}")
        return selected

    def _retrieval_identity(self, metadata: dict[str, Any], *, prefer_parent: bool = False) -> str:
        workspace_id = str(metadata.get("workspace_id", "")).strip()
        knowledge_base_id = str(metadata.get("knowledge_base_id", "")).strip()
        doc_id = str(metadata.get("doc_id", "")).strip()
        chunk_id = str(metadata.get("chunk_id") or metadata.get("child_id") or "").strip()
        parent_id = str(metadata.get("parent_id") or "").strip()
        selected_id = parent_id if prefer_parent and parent_id else chunk_id or parent_id
        if not selected_id:
            return ""
        return f"{workspace_id}:{knowledge_base_id}:{doc_id}:{selected_id}"

    def _retrieval_hits_near_duplicate(self, left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_metadata = left.get("metadata", {})
        right_metadata = right.get("metadata", {})
        left_identity = self._retrieval_identity(left_metadata, prefer_parent=False)
        right_identity = self._retrieval_identity(right_metadata, prefer_parent=False)
        if left_identity and left_identity == right_identity:
            return True
        left_parent_identity = self._retrieval_identity(left_metadata, prefer_parent=True)
        right_parent_identity = self._retrieval_identity(right_metadata, prefer_parent=True)
        if left_parent_identity and left_parent_identity == right_parent_identity:
            return True
        left_content = str(left.get("content", ""))
        right_content = str(right.get("content", ""))
        left_signature = self._content_signature(left_content)
        right_signature = self._content_signature(right_content)
        if left_signature and left_signature == right_signature:
            return True
        return self._content_similarity(left_content, right_content) >= float(self.duplicate_overlap_threshold)

    def _content_similarity(self, left: str, right: str) -> float:
        left_norm = re.sub(r"\s+", " ", left).strip().lower()
        right_norm = re.sub(r"\s+", " ", right).strip().lower()
        if not left_norm or not right_norm:
            return 0.0
        if left_norm in right_norm or right_norm in left_norm:
            return min(len(left_norm), len(right_norm)) / max(len(left_norm), len(right_norm))
        return SequenceMatcher(None, left_norm[:1000], right_norm[:1000]).ratio()

    def _finalize_retrieval_selection(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped = self._remove_duplicate_retrieval_hits(hits)
        if "duplicate_removal" not in self._last_retrieval_debug:
            duplicate_debug = self._duplicate_removal_debug(hits, deduped)
            self._last_retrieval_debug["duplicate_removal"] = duplicate_debug
            self._last_retrieval_debug.setdefault("retrieval_stages", {})["duplicate_removal"] = duplicate_debug
        if not self.mmr_enabled:
            self._last_retrieval_debug["mmr"] = {
                "enabled": False,
                "used": False,
                "input_count": len(deduped),
                "output_count": min(len(deduped), self.fusion_top_k),
            }
            self._last_retrieval_debug.setdefault("retrieval_stages", {})["mmr"] = self._last_retrieval_debug["mmr"]
            return deduped[: self.fusion_top_k]
        selected = self._mmr_select(deduped, self.mmr_top_k or self.fusion_top_k)
        debug = {
            "enabled": True,
            "used": True,
            "input_count": len(deduped),
            "output_count": len(selected),
            "lambda": self.mmr_lambda,
            "top_k": self.mmr_top_k or self.fusion_top_k,
        }
        self._last_retrieval_debug["mmr"] = debug
        self._last_retrieval_debug.setdefault("retrieval_stages", {})["mmr"] = debug
        return selected

    def _mmr_select(self, hits: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        if top_k <= 0 or len(hits) <= top_k:
            return hits[: max(0, top_k)]
        remaining = hits[:]
        selected: list[dict[str, Any]] = []
        lambda_value = min(1.0, max(0.0, float(self.mmr_lambda)))
        while remaining and len(selected) < top_k:
            best_index = 0
            best_score = float("-inf")
            for index, candidate in enumerate(remaining):
                relevance = self._retrieval_score(candidate)
                diversity_penalty = max(
                    (self._content_similarity(str(candidate.get("content", "")), str(item.get("content", ""))) for item in selected),
                    default=0.0,
                )
                mmr_score = lambda_value * relevance - (1.0 - lambda_value) * diversity_penalty
                if mmr_score > best_score:
                    best_index = index
                    best_score = mmr_score
            selected.append(remaining.pop(best_index))
        return selected

    def _chunk_row_to_direct_hit(self, chunk: dict[str, Any], rank: int) -> dict[str, Any]:
        metadata_json = chunk.get("metadata_json", {}) or {}
        metadata = {
            **metadata_json,
            "source": metadata_json.get("source", chunk.get("storage_path", chunk.get("doc_id", "unknown"))),
            "child_id": chunk.get("id", ""),
            "chunk_id": chunk.get("id", ""),
            "doc_id": chunk.get("doc_id", ""),
            "parent_id": chunk.get("parent_id", ""),
            "chunk_type": chunk.get("chunk_type", ""),
            "title_path": chunk.get("title_path", ""),
            "page_start": chunk.get("page_start"),
            "page_end": chunk.get("page_end"),
            "workspace_id": chunk.get("workspace_id", ""),
            "knowledge_base_id": chunk.get("knowledge_base_id", ""),
            "direct_loaded": True,
            "direct_load_rank": rank,
        }
        score = max(0.0, 1.0 - ((rank - 1) * 0.001))
        return {
            "content": str(chunk.get("content_markdown") or chunk.get("content") or ""),
            "metadata": metadata,
            "distance": max(0.0, 1.0 - score),
            "hybrid_score": score,
            "direct_load_score": score,
        }

    def _apply_reranker(self, question: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not (self.reranker_enabled and self.reranker is not None):
            return candidates[: self.fusion_top_k]
        debug = {
            "enabled": True,
            "threshold": self.reranker_threshold,
            "fallback_min_score": self.reranker_fallback_min_score,
            "degradation_enabled": self.reranker_degradation_enabled,
            "degraded_threshold": self.reranker_degraded_threshold,
            "threshold_degraded": False,
            "fallback_top_n": self.reranker_fallback_top_n,
            "input_count": len(candidates),
            "filtered_count": 0,
            "fallback_used": False,
            "failed": False,
        }
        try:
            reranked = self.reranker.rerank(question, candidates, self.reranker_top_n)
            passed = [hit for hit in reranked if float(hit.get("reranker_score", 0.0)) >= self.reranker_threshold]
            if passed:
                selected = passed
            elif self.reranker_degradation_enabled:
                degraded_threshold = max(float(self.reranker_fallback_min_score), float(self.reranker_degraded_threshold))
                degraded = [hit for hit in reranked if float(hit.get("reranker_score", 0.0)) >= degraded_threshold]
                if degraded:
                    selected = degraded[: self.fusion_top_k]
                    debug["threshold_degraded"] = True
                    debug["fallback_used"] = True
                    debug["fallback_reason"] = "degraded_threshold"
                elif reranked and self.reranker_fallback_top_n > 0:
                    selected = reranked[: max(1, int(self.reranker_fallback_top_n))]
                    debug["fallback_used"] = True
                    debug["fallback_reason"] = "bounded_top_candidates"
                else:
                    selected = []
                    debug["fallback_reason"] = "no_candidate_after_degradation"
            elif reranked and float(reranked[0].get("reranker_score", 0.0)) >= self.reranker_fallback_min_score:
                selected = reranked[: max(1, int(self.reranker_fallback_top_n))]
                debug["fallback_used"] = True
                debug["fallback_reason"] = "top_candidate_above_fallback_min_score"
            else:
                selected = []
                debug["fallback_reason"] = "no_candidate_above_threshold"
            debug["output_count"] = len(selected)
            debug["filtered_count"] = max(0, len(reranked) - len(selected))
            self._last_retrieval_debug["reranked_results"] = self._debug_hits(reranked)
            self._last_retrieval_debug["rerank"] = debug
            self._last_retrieval_debug.setdefault("retrieval_stages", {})["rerank"] = debug
            return selected
        except Exception as exc:
            logger.warning("Reranker failed, falling back to hybrid order: %s", exc)
            debug["failed"] = True
            debug["error"] = str(exc)
            debug["output_count"] = min(len(candidates), self.fusion_top_k)
            self._last_retrieval_debug["rerank"] = debug
            self._last_retrieval_debug.setdefault("retrieval_stages", {})["rerank"] = debug
            return candidates[: self.fusion_top_k]

    def _hydrate_retrieval_hit_content(
        self,
        hits: list[dict[str, Any]],
        scope: KnowledgeBaseScope | None = None,
    ) -> list[dict[str, Any]]:
        """Restore raw chunk text kept in SQLite for metadata-only vector hits."""
        scope = scope or self.default_scope
        verified_hits: list[dict[str, Any]] = []
        for hit in hits:
            metadata = hit.get("metadata", {})
            if not self._metadata_in_scope(metadata, scope):
                continue
            chunk_id = str(metadata.get("chunk_id") or metadata.get("child_id") or "").strip()
            authoritative_chunk = self.document_repository.get_chunk(chunk_id, scope) if chunk_id else None
            if not scope.compatibility_default and chunk_id and authoritative_chunk is None:
                continue
            if str(hit.get("content", "")).strip():
                verified_hits.append(hit)
                continue
            if not chunk_id:
                continue
            chunk = authoritative_chunk
            if not chunk:
                if scope.compatibility_default:
                    verified_hits.append(hit)
                continue
            content = str(chunk.get("content_markdown") or chunk.get("content") or "").strip()
            if not content:
                continue
            hit["content"] = content
            metadata.setdefault("doc_id", chunk.get("doc_id", ""))
            metadata.setdefault("parent_id", chunk.get("parent_id", ""))
            metadata.setdefault("chunk_type", chunk.get("chunk_type", ""))
            metadata.setdefault("title_path", chunk.get("title_path", ""))
            metadata.setdefault("page_start", chunk.get("page_start"))
            metadata.setdefault("page_end", chunk.get("page_end"))
            metadata.setdefault("workspace_id", chunk.get("workspace_id", ""))
            metadata.setdefault("knowledge_base_id", chunk.get("knowledge_base_id", ""))
            verified_hits.append(hit)
        return verified_hits

    def _understand_query(self, question: str) -> QueryUnderstandingResult:
        try:
            return self.query_understanding.understand(question)
        except Exception as exc:
            logger.warning("Query understanding failed, falling back to raw query: %s", exc)
            return QueryUnderstandingResult(
                original_query=question,
                normalized_query=question,
                retrieval_queries=[question],
                source="fallback",
            )

    def _tag_retrieval_hits(self, hits: list[dict[str, Any]], retrieval_query: str) -> list[dict[str, Any]]:
        tagged = []
        for hit in hits:
            metadata = {**hit.get("metadata", {})}
            trace = list(metadata.get("matched_queries", []))
            if retrieval_query not in trace:
                trace.append(retrieval_query)
            metadata["matched_queries"] = trace
            tagged.append({**hit, "metadata": metadata, "retrieval_query": retrieval_query})
        return tagged

    def _debug_hits(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        debug = []
        for hit in hits:
            metadata = hit.get("metadata", {})
            debug.append(
                {
                    "chunk_id": metadata.get("chunk_id", metadata.get("child_id", "")),
                    "doc_id": metadata.get("doc_id", ""),
                    "parent_id": metadata.get("parent_id", ""),
                    "score": self._retrieval_score(hit),
                    "vector_score": hit.get("vector_score"),
                    "bm25_score": hit.get("bm25_score", hit.get("keyword_score")),
                    "reranker_score": hit.get("reranker_score"),
                    "dense_rank": hit.get("dense_rank"),
                    "keyword_rank": hit.get("keyword_rank"),
                    "vector_contribution": hit.get("vector_contribution"),
                    "keyword_contribution": hit.get("keyword_contribution"),
                    "hybrid_score": hit.get("hybrid_score"),
                    "direct_loaded": bool(metadata.get("direct_loaded")),
                    "expanded_neighbor_ids": metadata.get("expanded_neighbor_ids", []),
                    "matched_queries": metadata.get("matched_queries", []),
                }
            )
        return debug

    def _fuse_retrieval_hits(self, vector_hits: list[dict[str, Any]], keyword_hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}

        for rank, hit in enumerate(vector_hits, start=1):
            metadata = hit.get("metadata", {})
            chunk_identity = str(metadata.get("chunk_id") or metadata.get("child_id") or hit.get("content", ""))
            key = f"{metadata.get('knowledge_base_id', '')}:{chunk_identity}"
            contribution = self.rrf_vector_weight / (self.rrf_k + rank)
            current = merged.setdefault(
                key,
                {
                    **hit,
                    "vector_score": float(hit.get("vector_score", 0.0)),
                    "bm25_score": 0.0,
                    "keyword_score": 0.0,
                    "hybrid_score": 0.0,
                    "dense_rank": rank,
                    "keyword_rank": None,
                    "vector_contribution": 0.0,
                    "keyword_contribution": 0.0,
                },
            )
            current["vector_score"] = max(float(current.get("vector_score", 0.0)), float(hit.get("vector_score", 0.0)))
            current["dense_rank"] = min(rank, int(current.get("dense_rank") or rank))
            current["vector_contribution"] = float(current.get("vector_contribution", 0.0)) + contribution
            current["hybrid_score"] = float(current.get("hybrid_score", 0.0)) + contribution
            self._merge_matched_queries(current, hit)

        for rank, hit in enumerate(keyword_hits, start=1):
            metadata = hit.get("metadata", {})
            chunk_identity = str(metadata.get("chunk_id") or metadata.get("child_id") or hit.get("content", ""))
            key = f"{metadata.get('knowledge_base_id', '')}:{chunk_identity}"
            contribution = self.rrf_keyword_weight / (self.rrf_k + rank)
            current = merged.setdefault(
                key,
                {
                    **hit,
                    "vector_score": 0.0,
                    "bm25_score": 0.0,
                    "keyword_score": float(hit.get("keyword_score", 0.0)),
                    "hybrid_score": 0.0,
                    "dense_rank": None,
                    "keyword_rank": rank,
                    "vector_contribution": 0.0,
                    "keyword_contribution": 0.0,
                },
            )
            bm25_score = float(hit.get("bm25_score", hit.get("keyword_score", 0.0)))
            current["bm25_score"] = max(float(current.get("bm25_score", 0.0)), bm25_score)
            current["keyword_score"] = max(float(current.get("keyword_score", 0.0)), float(hit.get("keyword_score", 0.0)))
            current["keyword_rank"] = min(rank, int(current.get("keyword_rank") or rank))
            current["keyword_contribution"] = float(current.get("keyword_contribution", 0.0)) + contribution
            current["hybrid_score"] = float(current.get("hybrid_score", 0.0)) + contribution
            self._merge_matched_queries(current, hit)

        hits = list(merged.values())
        hits.sort(key=lambda item: item["hybrid_score"], reverse=True)
        return hits[: self.fusion_top_k]

    def _merge_matched_queries(self, current: dict[str, Any], hit: dict[str, Any]) -> None:
        current_metadata = current.setdefault("metadata", {})
        current_queries = list(current_metadata.get("matched_queries", []))
        for query in hit.get("metadata", {}).get("matched_queries", []):
            if query not in current_queries:
                current_queries.append(query)
        current_metadata["matched_queries"] = current_queries

    def recall_parent_hits(
        self,
        child_hits: list[dict[str, Any]],
        scope: KnowledgeBaseScope | None = None,
    ) -> list[dict[str, Any]]:
        scope = scope or self.default_scope
        best_by_parent: dict[str, dict[str, Any]] = {}
        for hit in child_hits:
            metadata = hit.get("metadata", {})
            if not self._metadata_in_scope(metadata, scope):
                continue
            chunk_id = str(metadata.get("chunk_id") or metadata.get("child_id") or "")
            parent_id = str(metadata.get("parent_id", ""))
            chunk_type = str(metadata.get("chunk_type", ""))
            score = self._retrieval_score(hit)
            hit_scope = self._scope_for_retrieved_metadata(scope, metadata)

            if chunk_type == "table" and chunk_id:
                table_chunk = self.document_repository.get_chunk(chunk_id, hit_scope)
                if table_chunk:
                    self._merge_parent_hit(best_by_parent, table_chunk, score, chunk_id, table_chunk["metadata_json"].get("llm_context") or table_chunk["content_markdown"])
                    continue

            parent_chunk = self.document_repository.get_chunk(parent_id, hit_scope) if parent_id else None
            if parent_chunk:
                self._merge_parent_hit(best_by_parent, parent_chunk, score, chunk_id, parent_chunk["content_markdown"])
                continue

            self._legacy_parent_hit(best_by_parent, hit, score, chunk_id, parent_id)

        parent_hits = list(best_by_parent.values())
        for hit in parent_hits:
            child_scores = hit["metadata"].pop("_matched_child_scores", [])
            child_scores.sort(key=lambda item: item["score"], reverse=True)
            hit["metadata"]["matched_child_ids"] = list(dict.fromkeys(item["child_id"] for item in child_scores if item["child_id"]))
        parent_hits = self._assemble_final_context_hits(parent_hits, scope)
        parent_hits.sort(key=lambda item: item["hybrid_score"], reverse=True)
        return parent_hits[: self.top_k]

    def _metadata_in_scope(self, metadata: dict[str, Any], scope: KnowledgeBaseScope) -> bool:
        hit_workspace_id = str(metadata.get("workspace_id", "")).strip()
        hit_knowledge_base_id = str(metadata.get("knowledge_base_id", "")).strip()
        if (hit_workspace_id or hit_knowledge_base_id) and not scope.contains(hit_workspace_id, hit_knowledge_base_id):
            return False
        hit_doc_id = str(metadata.get("doc_id", "")).strip()
        if scope.document_ids and hit_doc_id and hit_doc_id not in scope.document_ids:
            return False
        return True

    def _assemble_final_context_hits(
        self,
        hits: list[dict[str, Any]],
        scope: KnowledgeBaseScope,
    ) -> list[dict[str, Any]]:
        expanded = [self._expand_short_context_hit(hit, scope) for hit in hits]
        result: list[dict[str, Any]] = []
        for hit in expanded:
            current = self._find_mergeable_context_hit(result, hit)
            if current is None:
                result.append(hit)
                continue
            self._merge_context_hit(current, hit)
        self._last_retrieval_debug.setdefault("retrieval_stages", {})["context"] = {
            "input_count": len(hits),
            "output_count": len(result),
            "expanded_count": len([hit for hit in result if hit.get("metadata", {}).get("expanded_neighbor_ids")]),
            "short_chunk_min_chars": self.context_short_chunk_min_chars,
            "expanded_chunk_max_chars": self.context_expanded_chunk_max_chars,
        }
        return result

    def _find_mergeable_context_hit(self, selected: list[dict[str, Any]], hit: dict[str, Any]) -> dict[str, Any] | None:
        metadata = hit.get("metadata", {})
        hit_scope = (
            str(metadata.get("workspace_id", "")),
            str(metadata.get("knowledge_base_id", "")),
            str(metadata.get("doc_id", "")),
        )
        hit_key = str(metadata.get("parent_id") or metadata.get("chunk_id") or "")
        hit_content = str(hit.get("content", ""))
        hit_signature = self._content_signature(hit_content)
        for current in selected:
            current_metadata = current.get("metadata", {})
            current_scope = (
                str(current_metadata.get("workspace_id", "")),
                str(current_metadata.get("knowledge_base_id", "")),
                str(current_metadata.get("doc_id", "")),
            )
            if current_scope != hit_scope:
                continue
            current_key = str(current_metadata.get("parent_id") or current_metadata.get("chunk_id") or "")
            current_content = str(current.get("content", ""))
            if current_key == hit_key:
                return current
            if self._content_signature(current_content) == hit_signature:
                return current
            if self._contexts_overlap(current_content, hit_content):
                return current
        return None

    def _merge_context_hit(self, current: dict[str, Any], hit: dict[str, Any]) -> None:
        current_content = str(current.get("content", ""))
        hit_content = str(hit.get("content", ""))
        if len(hit_content) > len(current_content) and self._contexts_overlap(current_content, hit_content):
            current["content"] = hit_content
        if self._retrieval_score(hit) > self._retrieval_score(current):
            current["hybrid_score"] = hit.get("hybrid_score", current.get("hybrid_score", 0.0))
            current["distance"] = hit.get("distance", current.get("distance", 1.0))
        current_metadata = current.setdefault("metadata", {})
        hit_metadata = hit.get("metadata", {})
        for field in ("matched_child_ids", "expanded_neighbor_ids", "context_window_chunk_ids"):
            current_values = list(current_metadata.get(field, []))
            for value in hit_metadata.get(field, []):
                if value and value not in current_values:
                    current_values.append(value)
            current_metadata[field] = current_values
        for page_field, reducer in (("page_start", min), ("page_end", max)):
            values = [value for value in (current_metadata.get(page_field), hit_metadata.get(page_field)) if value is not None]
            if values:
                current_metadata[page_field] = reducer(values)

    def _expand_short_context_hit(self, hit: dict[str, Any], scope: KnowledgeBaseScope) -> dict[str, Any]:
        content = str(hit.get("content", "")).strip()
        if len(content) >= self.context_short_chunk_min_chars or self.context_short_chunk_min_chars <= 0:
            return hit
        metadata = hit.get("metadata", {})
        doc_id = str(metadata.get("doc_id", "")).strip()
        current_id = str(metadata.get("parent_id") or metadata.get("chunk_id") or "").strip()
        if not doc_id or not current_id or self.context_expanded_chunk_max_chars <= len(content):
            return hit
        chunk_type = str(metadata.get("chunk_type") or "parent")
        siblings = self.document_repository.list_chunks(doc_id=doc_id, chunk_types={chunk_type}, scope=scope)
        if not siblings and chunk_type != "parent":
            siblings = self.document_repository.list_chunks(doc_id=doc_id, chunk_types={"parent"}, scope=scope)
        ids = [str(chunk.get("id", "")) for chunk in siblings]
        if current_id not in ids:
            return hit
        index = ids.index(current_id)
        selected_indexes = [index]
        left = index - 1
        right = index + 1
        while len(self._join_context_chunks([siblings[i] for i in selected_indexes])) < self.context_short_chunk_min_chars:
            added = False
            if left >= 0:
                selected_indexes.insert(0, left)
                left -= 1
                added = True
            if len(self._join_context_chunks([siblings[i] for i in selected_indexes])) >= self.context_short_chunk_min_chars:
                break
            if right < len(siblings):
                selected_indexes.append(right)
                right += 1
                added = True
            if not added:
                break
            if len(self._join_context_chunks([siblings[i] for i in selected_indexes])) >= self.context_expanded_chunk_max_chars:
                break
        selected_chunks = [siblings[i] for i in selected_indexes]
        expanded_content = self._join_context_chunks(selected_chunks)
        if len(expanded_content) > self.context_expanded_chunk_max_chars:
            expanded_content = expanded_content[: self.context_expanded_chunk_max_chars].rstrip()
        neighbor_ids = [str(chunk.get("id", "")) for chunk in selected_chunks if str(chunk.get("id", "")) != current_id]
        if not neighbor_ids:
            return hit
        updated = {**hit, "content": expanded_content}
        updated_metadata = {**metadata}
        updated_metadata["expanded_neighbor_ids"] = list(dict.fromkeys([*updated_metadata.get("expanded_neighbor_ids", []), *neighbor_ids]))
        updated_metadata["context_window_chunk_ids"] = [str(chunk.get("id", "")) for chunk in selected_chunks if chunk.get("id")]
        if selected_chunks:
            page_values = [chunk.get("page_start") for chunk in selected_chunks if chunk.get("page_start")]
            page_end_values = [chunk.get("page_end") or chunk.get("page_start") for chunk in selected_chunks if chunk.get("page_start")]
            if page_values:
                updated_metadata["page_start"] = min(page_values)
            if page_end_values:
                updated_metadata["page_end"] = max(page_end_values)
        updated["metadata"] = updated_metadata
        return updated

    def _join_context_chunks(self, chunks: list[dict[str, Any]]) -> str:
        return "\n\n".join(str(chunk.get("content_markdown") or chunk.get("content") or "").strip() for chunk in chunks if str(chunk.get("content_markdown") or chunk.get("content") or "").strip())

    def _content_signature(self, content: str) -> str:
        normalized = re.sub(r"\s+", " ", content).strip().lower()
        return normalized[:500]

    def _contexts_overlap(self, left: str, right: str) -> bool:
        left_norm = re.sub(r"\s+", " ", left).strip().lower()
        right_norm = re.sub(r"\s+", " ", right).strip().lower()
        if not left_norm or not right_norm:
            return False
        if left_norm in right_norm or right_norm in left_norm:
            return True
        shorter, longer = (left_norm, right_norm) if len(left_norm) <= len(right_norm) else (right_norm, left_norm)
        min_overlap = min(len(shorter), 120)
        if min_overlap < 24:
            return False
        return shorter[-min_overlap:] in longer or shorter[:min_overlap] in longer

    def _retrieval_score(self, hit: dict[str, Any]) -> float:
        return float(hit.get("reranker_score", hit.get("hybrid_score", hit.get("vector_score", hit.get("keyword_score", 0.0)))))

    def _scope_for_retrieved_metadata(self, scope: KnowledgeBaseScope, metadata: dict[str, Any]) -> KnowledgeBaseScope:
        workspace_id = str(metadata.get("workspace_id", "")).strip()
        knowledge_base_id = str(metadata.get("knowledge_base_id", "")).strip()
        if workspace_id and knowledge_base_id and scope.contains(workspace_id, knowledge_base_id):
            return replace(
                scope,
                workspace_id=workspace_id,
                selected_knowledge_base_ids=(knowledge_base_id,),
            )
        return scope

    def _scoped_parent_key(self, parent_id: str, workspace_id: Any = "", knowledge_base_id: Any = "") -> str:
        return f"{str(workspace_id or '').strip()}:{str(knowledge_base_id or '').strip()}:{parent_id}"

    def _merge_parent_hit(self, best_by_parent: dict[str, dict[str, Any]], chunk: dict[str, Any], score: float, child_id: str, context: str) -> None:
        metadata_json = chunk.get("metadata_json", {})
        parent_id = str(chunk.get("id", ""))
        parent_key = self._scoped_parent_key(parent_id, chunk.get("workspace_id", ""), chunk.get("knowledge_base_id", ""))
        current = best_by_parent.get(parent_key)
        if current is None:
            current = {
                "content": context,
                "metadata": {
                    "source": metadata_json.get("source", chunk.get("doc_id", "unknown")),
                    "doc_id": chunk.get("doc_id", ""),
                    "chunk_id": parent_id,
                    "parent_id": parent_id,
                    "chunk_type": chunk.get("chunk_type", "parent"),
                    "title_path": chunk.get("title_path", ""),
                    "page_start": chunk.get("page_start"),
                    "page_end": chunk.get("page_end"),
                    "file_name": metadata_json.get("file_name", ""),
                    "storage_path": metadata_json.get("storage_path", metadata_json.get("source", "")),
                    "workspace_id": chunk.get("workspace_id", ""),
                    "knowledge_base_id": chunk.get("knowledge_base_id", ""),
                    "matched_child_ids": [],
                    "expanded_neighbor_ids": [],
                    "_matched_child_scores": [],
                },
                "hybrid_score": score,
                "distance": max(0.0, 1.0 - score),
            }
            best_by_parent[parent_key] = current
        elif score > float(current.get("hybrid_score", 0.0)):
            current["hybrid_score"] = score
            current["distance"] = max(0.0, 1.0 - score)
        current["metadata"]["_matched_child_scores"].append({"child_id": child_id, "score": score})

    def _legacy_parent_hit(self, best_by_parent: dict[str, dict[str, Any]], hit: dict[str, Any], score: float, child_id: str, parent_id: str) -> None:
        if not parent_id or parent_id not in self.parent_store:
            return
        parent_data = self.parent_store[parent_id]
        hit_metadata = hit.get("metadata", {})
        parent_key = self._scoped_parent_key(
            parent_id,
            hit_metadata.get("workspace_id", ""),
            hit_metadata.get("knowledge_base_id", ""),
        )
        current = best_by_parent.get(parent_key)
        if current is None:
            current = {
                "content": parent_data.get("text", ""),
                "metadata": {
                    "source": parent_data.get("source", hit.get("metadata", {}).get("source", "unknown")),
                    "parent_id": parent_id,
                    "parent_index": parent_data.get("parent_index", 0),
                    "section_title": parent_data.get("section_title"),
                    "workspace_id": hit_metadata.get("workspace_id", ""),
                    "knowledge_base_id": hit_metadata.get("knowledge_base_id", ""),
                    "matched_child_ids": [],
                    "_matched_child_scores": [],
                },
                "hybrid_score": score,
                "distance": max(0.0, 1.0 - score),
            }
            best_by_parent[parent_key] = current
        elif score > float(current.get("hybrid_score", 0.0)):
            current["hybrid_score"] = score
            current["distance"] = max(0.0, 1.0 - score)
        current["metadata"]["_matched_child_scores"].append({"child_id": child_id, "score": score})

    def extract_sources(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        best_by_source: dict[str, dict[str, Any]] = {}
        for hit in hits:
            metadata = hit.get("metadata", {})
            source = str(metadata.get("source", "unknown"))
            title_path = str(metadata.get("title_path", ""))
            page_start = metadata.get("page_start")
            label = source
            if title_path:
                label = f"{label} · {title_path}"
            if page_start:
                label = f"{label} · p.{page_start}"
            distance = float(hit.get("distance", 1.0))
            source_key = f"{metadata.get('knowledge_base_id', '')}:{label}"
            prev = best_by_source.get(source_key)
            if prev is None or distance < float(prev["distance"]):
                best_by_source[source_key] = {
                    "source": label,
                    "score": max(0.0, 1.0 - distance),
                    "distance": distance,
                    "doc_id": metadata.get("doc_id", ""),
                    "chunk_id": metadata.get("chunk_id", metadata.get("child_id", "")) or next(iter(metadata.get("matched_child_ids", [])), ""),
                    "parent_id": metadata.get("parent_id", ""),
                    "title_path": title_path,
                    "page_start": page_start,
                    "page_end": metadata.get("page_end"),
                    "matched_child_ids": metadata.get("matched_child_ids", []),
                    "workspace_id": metadata.get("workspace_id", ""),
                    "knowledge_base_id": metadata.get("knowledge_base_id", ""),
                }
        sources = []
        for item in sorted(best_by_source.values(), key=lambda x: x["distance"]):
            item.pop("distance", None)
            sources.append(item)
        return sources

    def build_reasoning_summary(self, question: str, hits: list[dict[str, Any]]) -> dict[str, Any]:
        understanding = self._last_retrieval_debug.get("query_understanding", {}) if isinstance(self._last_retrieval_debug, dict) else {}
        applied_terms = understanding.get("applied_terms", []) if isinstance(understanding, dict) else []
        term_mappings = []
        for item in applied_terms:
            if isinstance(item, dict) and item.get("term") and item.get("canonical"):
                term_mappings.append(f"{item['term']} -> {item['canonical']}")

        evidence = []
        for hit in hits[:5]:
            metadata = hit.get("metadata", {})
            source = str(metadata.get("source", "unknown"))
            title_path = str(metadata.get("title_path", ""))
            score = self._retrieval_score(hit)
            content = str(hit.get("content", "")).strip()
            evidence.append(
                {
                    "source": source,
                    "title_path": title_path,
                    "score": score,
                    "matched_child_ids": metadata.get("matched_child_ids", []),
                    "preview": content[:220],
                }
            )

        return {
            "question": question,
            "normalized_query": understanding.get("normalized_query", question) if isinstance(understanding, dict) else question,
            "retrieval_queries": understanding.get("retrieval_queries", [question]) if isinstance(understanding, dict) else [question],
            "expanded_terms": understanding.get("expanded_terms", []) if isinstance(understanding, dict) else [],
            "term_mappings": term_mappings,
            "evidence": evidence,
            "summary": "基于问题理解、检索变体、知识库命中和来源片段生成回答。",
        }

    def build_chat_agent_trace(
        self,
        question: str,
        hits: list[dict[str, Any]],
        scope: KnowledgeBaseScope | None = None,
        sources: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if not self.agent_trace_stream_enabled:
            return []
        return self._build_quick_answer_trace(question, hits, scope=scope, sources=sources)

    def _build_quick_answer_trace(
        self,
        question: str,
        hits: list[dict[str, Any]],
        scope: KnowledgeBaseScope | None = None,
        sources: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        debug = self._last_retrieval_debug if isinstance(self._last_retrieval_debug, dict) else {}
        understanding = debug.get("query_understanding", {}) if isinstance(debug.get("query_understanding"), dict) else {}
        retrieval_queries = understanding.get("retrieval_queries")
        if not isinstance(retrieval_queries, list) or not retrieval_queries:
            retrieval_queries = [question]
        normalized_query = str(understanding.get("normalized_query") or question)
        generated_query_count = max(0, len(retrieval_queries) - 1)

        retrieval_stages = debug.get("retrieval_stages", {}) if isinstance(debug.get("retrieval_stages"), dict) else {}
        query_expansion = debug.get("query_expansion", {}) if isinstance(debug.get("query_expansion"), dict) else {}
        fused_results = debug.get("fused_results", [])
        reranked_results = debug.get("reranked_results", [])
        dense_stage = retrieval_stages.get("dense", {}) if isinstance(retrieval_stages.get("dense"), dict) else {}
        keyword_stage = retrieval_stages.get("keyword", {}) if isinstance(retrieval_stages.get("keyword"), dict) else {}
        fusion_stage = retrieval_stages.get("fusion", {}) if isinstance(retrieval_stages.get("fusion"), dict) else {}
        candidate_count = (
            self._safe_int(query_expansion.get("final_candidate_count"))
            or self._safe_int(fusion_stage.get("output_count"))
            or (len(fused_results) if isinstance(fused_results, list) else 0)
            or len(hits)
        )

        source_chunk_ids = self._quick_trace_source_chunk_ids(hits)
        source_documents = self._quick_trace_source_documents(hits, sources=sources)
        cited_document_count = len(source_documents)
        hit_count = len(hits)
        insufficient = hit_count == 0 or cited_document_count == 0
        scope = scope or self.default_scope
        base_metadata = {
            "quick_rag": True,
            "chat_mode": "quick",
            "normalized_query": normalized_query,
            "retrieval_query_count": len(retrieval_queries),
            "candidate_count": candidate_count,
            "hit_count": hit_count,
            "cited_document_count": cited_document_count,
            "source_count": cited_document_count,
            "source_documents": source_documents[:8],
            "source_chunk_ids": source_chunk_ids[:20],
            "knowledge_base_scope": scope.to_dict(),
            "insufficient_evidence": insufficient,
            "dense_candidate_count": self._safe_int(dense_stage.get("candidate_count")),
            "keyword_candidate_count": self._safe_int(keyword_stage.get("candidate_count")),
            "fusion_output_count": self._safe_int(fusion_stage.get("output_count")),
            "reranked_count": len(reranked_results) if isinstance(reranked_results, list) else 0,
            "query_expansion_used": bool(query_expansion.get("used")),
            "agentic_retrieval_enabled": self.agentic_retrieval_enabled,
        }

        read_status = "partial" if insufficient else "completed"
        complete_status = "partial" if insufficient else "completed"
        return [
            {
                "stage": "UnderstandQuestion",
                "status": "completed",
                "summary": f"已完成问题理解，使用 {len(retrieval_queries)} 个检索问题，其中 {generated_query_count} 个为扩展变体。",
                "source_chunk_ids": [],
                "metadata": {**base_metadata, "stage": "understand_question", "generated_query_count": generated_query_count},
            },
            {
                "stage": "RetrieveKnowledgeBase",
                "status": "completed",
                "summary": f"检索知识库完成：得到 {candidate_count} 个候选，选用 {hit_count} 条证据。",
                "source_chunk_ids": source_chunk_ids,
                "metadata": {**base_metadata, "stage": "retrieve_knowledge_base"},
            },
            {
                "stage": "ReadEvidence",
                "status": read_status,
                "summary": (
                    f"引用了 {cited_document_count} 篇文档，整理 {len(source_chunk_ids)} 个来源片段。"
                    if not insufficient
                    else "未找到足够的可引用文档，回答将明确说明无法确定。"
                ),
                "source_chunk_ids": source_chunk_ids,
                "metadata": {**base_metadata, "stage": "read_evidence"},
            },
            {
                "stage": "SynthesizeAnswer",
                "status": "completed",
                "summary": "根据已检索证据组织结构化回答；证据不足的细节会明确标注无法确定。",
                "source_chunk_ids": source_chunk_ids,
                "metadata": {**base_metadata, "stage": "synthesize_answer"},
            },
            {
                "stage": "Complete",
                "status": complete_status,
                "summary": (
                    f"完成快速问答，回答引用 {cited_document_count} 篇文档。"
                    if not insufficient
                    else "完成快速问答，但当前知识库证据不足。"
                ),
                "source_chunk_ids": source_chunk_ids,
                "metadata": {**base_metadata, "stage": "complete"},
            },
        ]

    def _quick_trace_source_chunk_ids(self, hits: list[dict[str, Any]]) -> list[str]:
        chunk_ids: list[str] = []
        for hit in hits:
            metadata = hit.get("metadata", {}) if isinstance(hit, dict) else {}
            for child_id in metadata.get("matched_child_ids", []) if isinstance(metadata.get("matched_child_ids"), list) else []:
                if child_id:
                    chunk_ids.append(str(child_id))
            for key in ("chunk_id", "child_id"):
                value = metadata.get(key)
                if value:
                    chunk_ids.append(str(value))
        return list(dict.fromkeys(chunk_ids))

    def _quick_trace_source_documents(self, hits: list[dict[str, Any]], sources: list[dict[str, Any]] | None = None) -> list[str]:
        source_documents: list[str] = []
        for item in sources or []:
            if isinstance(item, dict) and item.get("source"):
                source_documents.append(str(item["source"]))
        for hit in hits:
            metadata = hit.get("metadata", {}) if isinstance(hit, dict) else {}
            source = metadata.get("source")
            if source:
                source_documents.append(str(source))
        return list(dict.fromkeys(source_documents))

    def _safe_int(self, value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return None

    def _build_context(self, hits: list[dict[str, Any]]) -> str:
        blocks = []
        for i, hit in enumerate(hits, start=1):
            metadata = hit["metadata"]
            source = metadata.get("source", "unknown")
            title_path = metadata.get("title_path", "")
            pages = ""
            if metadata.get("page_start"):
                pages = f" pages={metadata.get('page_start')}-{metadata.get('page_end') or metadata.get('page_start')}"
            blocks.append(f"[{i}] source={source} title_path={title_path}{pages}\n{hit['content']}")
        return "\n\n".join(blocks)

    def _resolve_source_path(self, source: str) -> Path:
        data_root = self.data_dir.resolve()
        source_path = (data_root / source).resolve()
        try:
            source_path.relative_to(data_root)
        except ValueError as exc:
            raise ValueError("Invalid source path") from exc
        return source_path

    def get_document_content(self, source: str, scope: KnowledgeBaseScope | None = None) -> str:
        scope = scope or self.default_scope
        self._assert_source_in_scope(source, scope)
        source_path = self._resolve_source_path(source)
        if not source_path.exists() or not source_path.is_file():
            raise FileNotFoundError(source)
        return load_text(source_path)

    def get_document_path(self, source: str, scope: KnowledgeBaseScope | None = None) -> Path:
        scope = scope or self.default_scope
        self._assert_source_in_scope(source, scope)
        source_path = self._resolve_source_path(source)
        if not source_path.exists() or not source_path.is_file():
            raise FileNotFoundError(source)
        return source_path

    def _assert_source_in_scope(self, source: str, scope: KnowledgeBaseScope) -> None:
        if self.document_repository.get_document_by_path(source, scope) is not None:
            return
        if scope.compatibility_default and scope.knowledge_base_id == self.default_scope.knowledge_base_id:
            parts = Path(source).parts
            custom_ids = {
                item.id
                for item in (self.knowledge_base_service.list() if self.knowledge_base_service is not None else [])
                if item.id != self.default_scope.knowledge_base_id
            }
            if not (len(parts) >= 2 and parts[0] == "uploads" and parts[1] in custom_ids):
                return
        raise FileNotFoundError(source)

    def _validate_upload_suffix(self, filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_EXTS:
            raise ValueError(f"Unsupported document type: {suffix}")
        return suffix

    def _sanitize_path_segment(self, text: str, fallback: str = "upload") -> str:
        cleaned = self._sanitize_filename(text)
        return cleaned or fallback

    def _sanitize_upload_batch_id(self, batch_id: str | None) -> str | None:
        if batch_id is None or not batch_id.strip():
            return None
        raw = batch_id.strip()
        if raw in {".", ".."} or "/" in raw or "\\" in raw or re.match(r"^[A-Za-z]:", raw):
            raise ValueError("Invalid upload batch id")
        return self._sanitize_path_segment(raw, fallback="batch")

    def _sanitize_upload_relative_path(self, relative_path: str) -> Path:
        raw = relative_path.strip()
        if not raw:
            raise ValueError("Relative path is required")
        if raw.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", raw):
            raise ValueError("Invalid upload path")

        parts = [part for part in re.split(r"[\\/]+", raw) if part]
        if not parts:
            raise ValueError("Relative path is required")
        if any(part in {".", ".."} for part in parts):
            raise ValueError("Invalid upload path")

        file_name = parts[-1]
        suffix = self._validate_upload_suffix(file_name)
        sanitized_dirs = [self._sanitize_path_segment(part, fallback="folder") for part in parts[:-1]]
        sanitized_stem = self._sanitize_path_segment(Path(file_name).stem, fallback="upload")
        return Path(*sanitized_dirs, f"{sanitized_stem}{suffix}")

    def _sanitize_upload_filename(self, filename: str) -> str:
        raw_name = Path(filename).name.strip()
        if not raw_name:
            raise ValueError("Filename is required")
        suffix = self._validate_upload_suffix(raw_name)
        stem = self._sanitize_filename(Path(raw_name).stem)
        return f"{stem}{suffix}"

    def _unique_upload_target(self, target: Path) -> Path:
        if not target.exists():
            return target
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        candidate = target.with_name(f"{target.stem}_{timestamp}{target.suffix}")
        counter = 1
        while candidate.exists():
            candidate = target.with_name(f"{target.stem}_{timestamp}_{counter}{target.suffix}")
            counter += 1
        return candidate

    def _assert_upload_processing_allowed(self, scope: KnowledgeBaseScope) -> None:
        if self.knowledge_base_service is None:
            return
        knowledge_base = self.knowledge_base_service.assert_writable(scope)
        if getattr(getattr(knowledge_base, "aggregate", None), "reset_required", False):
            raise ValueError("Knowledge storage reset is required before uploading documents")

    def _resolve_processing_settings(self, settings: dict[str, Any] | None = None):
        available = {
            item["name"]
            for item in PARSER_REGISTRY.list_engines()
            if item.get("available") or item["name"] == "builtin"
        }
        return resolve_processing_config(
            settings,
            self.processing_defaults,
            available_parser_engines=available,
            caption_available=bool(getattr(self.caption_provider_service, "available", False)),
        )

    def _effective_upload_settings(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        resolved = self._resolve_processing_settings(self._normalize_upload_settings(settings))
        effective = resolved.effective.to_dict()
        requested = resolved.requested.to_dict()
        return {
            **effective,
            "requested": requested,
            "effective": effective,
            "processing_version": PROCESSING_VERSION,
            "multimodal_enabled": bool(effective["ocr_enabled"] or effective["caption_enabled"]),
            "audio_enabled": False,
        }

    def _with_effective_upload_settings(self, batch: dict[str, Any]) -> dict[str, Any]:
        return {**batch, "effective_settings": self._effective_upload_settings(batch.get("settings"))}

    def create_upload_batch(self, scope: KnowledgeBaseScope, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        self._assert_upload_processing_allowed(scope)
        upload_settings = self._normalize_upload_settings(settings)
        result = self._with_effective_upload_settings(self.upload_batch_repository.create_batch(scope, upload_settings))
        logger.info(
            "rag_service.upload_batch.created",
            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "batch_id": result.get("id")},
        )
        return result

    def get_upload_batch(self, batch_id: str, scope: KnowledgeBaseScope) -> dict[str, Any]:
        return self._with_effective_upload_settings(self.upload_batch_repository.get_batch(batch_id, scope))

    def update_upload_batch_settings(
        self, batch_id: str, scope: KnowledgeBaseScope, settings: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._assert_upload_processing_allowed(scope)
        batch = self.upload_batch_repository.get_batch(batch_id, scope)
        if batch["status"] not in {"draft", "uploading", "ready_to_process"}:
            raise ValueError("Upload batch settings can only be changed before processing")
        return self._with_effective_upload_settings(
            self.upload_batch_repository.update_batch(batch_id, scope, settings=self._normalize_upload_settings(settings))
        )

    def _normalize_upload_settings(self, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        return {**dict(settings or {}), **UPLOAD_REQUIRED_INDEXING_FLAGS}

    def cancel_upload_batch(self, batch_id: str, scope: KnowledgeBaseScope) -> dict[str, Any]:
        if self.processing_worker is not None:
            self.processing_worker.repository.cancel_for_upload_batch(scope, batch_id, reason="upload batch canceled")
        return self._with_effective_upload_settings(self.upload_batch_repository.cancel_batch(batch_id, scope))

    def _save_upload_source_file(
        self,
        *,
        filename: str,
        content: bytes,
        scope: KnowledgeBaseScope,
        relative_path: str | None = None,
        batch_id: str | None = None,
    ) -> tuple[Path, str]:
        if not content:
            raise ValueError("Uploaded file is empty")
        batch = self._sanitize_upload_batch_id(batch_id)
        if relative_path is None:
            relative_target = Path(self._sanitize_upload_filename(filename))
        else:
            relative_target = self._sanitize_upload_relative_path(relative_path)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        scoped_upload_root = (
            self.upload_dir
            if scope.knowledge_base_id == self.default_scope.knowledge_base_id
            else self.upload_dir / scope.knowledge_base_id
        )
        target_root = scoped_upload_root / batch if batch else scoped_upload_root
        target = (target_root / relative_target).resolve()
        upload_root = self.upload_dir.resolve()
        data_root = self.data_dir.resolve()
        try:
            target.relative_to(upload_root)
        except ValueError as exc:
            raise ValueError("Invalid upload path") from exc
        target = self._unique_upload_target(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target, target.relative_to(data_root).as_posix()

    def add_upload_batch_file(
        self,
        batch_id: str,
        *,
        filename: str,
        content: bytes,
        scope: KnowledgeBaseScope,
        relative_path: str | None = None,
    ) -> dict[str, Any]:
        self._assert_upload_processing_allowed(scope)
        batch = self.upload_batch_repository.get_batch(batch_id, scope)
        if batch["status"] not in {"draft", "uploading", "ready_to_process"}:
            raise ValueError("Upload batch does not accept more files")
        target, source = self._save_upload_source_file(
            filename=filename,
            content=content,
            relative_path=relative_path or filename,
            batch_id=batch_id,
            scope=scope,
        )
        file_task = self.upload_batch_repository.add_file(
            batch_id,
            scope,
            original_name=filename,
            relative_path=relative_path or filename,
            storage_path=source,
            size=len(content),
        )
        logger.info(
            "rag_service.upload_batch.file_added",
            extra={
                "workspace_id": scope.workspace_id,
                "knowledge_base_id": scope.knowledge_base_id,
                "batch_id": batch_id,
                "file_id": file_task.get("id"),
                "source": source,
                "size": len(content),
            },
        )
        self.upload_batch_repository.update_batch(batch_id, scope, status="ready_to_process")
        return file_task

    def confirm_upload_batch(self, batch_id: str, scope: KnowledgeBaseScope) -> dict[str, Any]:
        self._assert_upload_processing_allowed(scope)
        batch = self.upload_batch_repository.get_batch(batch_id, scope)
        if batch["status"] == "canceled":
            raise ValueError("Upload batch is canceled")
        if not batch["files"]:
            raise ValueError("Upload batch has no files")
        self._ensure_upload_required_indexing_strategy(scope)
        self._register_upload_batch_documents(batch_id, scope)
        self.upload_batch_repository.update_batch(batch_id, scope, status="processing")
        self._process_upload_batch(batch_id, scope)
        return self.get_upload_batch(batch_id, scope)

    def start_upload_batch_processing(self, batch_id: str, scope: KnowledgeBaseScope) -> dict[str, Any]:
        self._assert_upload_processing_allowed(scope)
        batch = self.upload_batch_repository.get_batch(batch_id, scope)
        if batch["status"] == "canceled":
            raise ValueError("Upload batch is canceled")
        if batch["status"] == "processing":
            self._register_upload_batch_documents(batch_id, scope)
            return self.get_upload_batch(batch_id, scope)
        if not batch["files"]:
            raise ValueError("Upload batch has no files")
        if batch["status"] in {"completed", "partial_failed", "failed"}:
            raise ValueError("Upload batch has already been processed")
        self._ensure_upload_required_indexing_strategy(scope)
        self._register_upload_batch_documents(batch_id, scope)
        self.upload_batch_repository.update_batch(batch_id, scope, status="processing")
        if self.uses_durable_upload_processing():
            self.processing_worker.enqueue_upload_batch(batch_id, scope)
        return self.get_upload_batch(batch_id, scope)

    def _ensure_upload_required_indexing_strategy(self, scope: KnowledgeBaseScope) -> None:
        if self.knowledge_base_service is None:
            return
        knowledge_base = self.knowledge_base_service.assert_writable(scope)
        current = knowledge_base.indexing_strategy.to_dict()
        required = {**current, **UPLOAD_REQUIRED_INDEXING_FLAGS}
        if required != current:
            self.knowledge_base_service.update(scope.knowledge_base_id, indexing_strategy=required)

    def uses_durable_upload_processing(self) -> bool:
        worker = self.processing_worker
        return bool(worker is not None and worker.enabled)

    def process_upload_batch(self, batch_id: str, scope: KnowledgeBaseScope) -> None:
        logger.info(
            "rag_service.upload_batch.process.start",
            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "batch_id": batch_id},
        )
        try:
            self._process_upload_batch(batch_id, scope)
            logger.info(
                "rag_service.upload_batch.process.end",
                extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "batch_id": batch_id},
            )
        except Exception:
            logger.exception("Upload batch background processing failed: %s", batch_id)
            try:
                self._finish_upload_batch_from_files(batch_id, scope)
            except Exception:
                logger.exception("Upload batch status reconciliation failed: %s", batch_id)
            raise

    def retry_upload_batch_file(self, batch_id: str, file_id: str, scope: KnowledgeBaseScope) -> dict[str, Any]:
        self._assert_upload_processing_allowed(scope)
        file_task = self.upload_batch_repository.get_file(file_id, scope)
        if file_task["batch_id"] != batch_id:
            raise KeyError(file_id)
        if file_task["status"] != "failed":
            raise ValueError("Only failed upload file tasks can be retried")
        self.upload_batch_repository.update_batch(batch_id, scope, status="processing")
        self._process_upload_file(file_task, scope)
        self._finish_upload_batch_from_files(batch_id, scope)
        return self.get_upload_batch(batch_id, scope)

    def _process_upload_batch(self, batch_id: str, scope: KnowledgeBaseScope) -> None:
        self._register_upload_batch_documents(batch_id, scope)
        for file_task in self.upload_batch_repository.list_files(batch_id, scope):
            if file_task["status"] in {"completed", "canceled"}:
                continue
            self._process_upload_file(file_task, scope)
        self._finish_upload_batch_from_files(batch_id, scope)

    def _register_upload_batch_documents(self, batch_id: str, scope: KnowledgeBaseScope) -> None:
        for file_task in self.upload_batch_repository.list_files(batch_id, scope):
            if file_task["status"] in {"completed", "canceled"}:
                continue
            document_id = str(file_task.get("document_id") or "")
            storage_path = str(file_task.get("storage_path") or "")
            if not storage_path:
                continue
            try:
                source_path = self._resolve_source_path(storage_path)
                if not source_path.exists() or not source_path.is_file():
                    continue
                batch = self.upload_batch_repository.get_batch(batch_id, scope)
                source_revision = self._source_revision_for_file(source_path, batch.get("settings") or {})
                document_id = document_id or stable_doc_id(source_path)
                metadata = {
                    "size": int(file_task.get("size") or source_path.stat().st_size),
                    "upload_batch_id": batch_id,
                    "upload_file_id": str(file_task.get("id") or ""),
                    "upload_relative_path": str(file_task.get("relative_path") or ""),
                    "source_revision": source_revision,
                    **scope.to_dict(),
                }
                self.document_repository.upsert_document(
                    id=document_id,
                    name=source_path.name,
                    file_type=source_path.suffix.lower().lstrip("."),
                    storage_path=storage_path,
                    parse_status="pending",
                    metadata_json=metadata,
                    workspace_id=scope.workspace_id,
                    knowledge_base_id=scope.knowledge_base_id,
                )
                if not file_task.get("document_id"):
                    self.upload_batch_repository.update_file(
                        str(file_task["id"]),
                        scope,
                        document_id=document_id,
                    )
            except Exception:
                logger.exception(
                    "rag_service.upload_batch.document_registration_failed",
                    extra={
                        "workspace_id": scope.workspace_id,
                        "knowledge_base_id": scope.knowledge_base_id,
                        "batch_id": batch_id,
                        "file_id": file_task.get("id"),
                        "storage_path": storage_path,
                    },
                )
                raise

    def _upload_phase_report(
        self,
        statuses: dict[str, str] | None = None,
        *,
        warnings_by_phase: dict[str, list[str]] | None = None,
        errors_by_phase: dict[str, list[str]] | None = None,
        retry_phases: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        statuses = statuses or {}
        warnings_by_phase = warnings_by_phase or {}
        errors_by_phase = errors_by_phase or {}
        retry_phases = retry_phases or set()
        phases = initial_phase_report()
        for phase in phases:
            name = str(phase["name"])
            phase["status"] = statuses.get(name, phase["status"])
            phase["warnings"] = warnings_by_phase.get(name, [])
            phase["errors"] = errors_by_phase.get(name, [])
            phase["retry_eligible"] = name in retry_phases
        return phases

    def _multimodal_phase_status(self, result: dict[str, Any]) -> str:
        total = int(result.get("total", 0) or 0)
        failed = int(result.get("failed", 0) or 0)
        canceled = int(result.get("canceled", 0) or 0)
        queued = int(result.get("queued", 0) or 0)
        if total <= 0:
            return "skipped"
        if queued:
            return "queued"
        if failed or canceled:
            return "partial_failed"
        return "completed"

    def enqueue_upload_file_chunk_stage(self, file_task: dict[str, Any], scope: KnowledgeBaseScope) -> dict[str, Any] | None:
        file_id = str(file_task["id"])
        batch_id = str(file_task.get("batch_id") or "")
        source_path = self._resolve_source_path(str(file_task["storage_path"]))
        batch = self.upload_batch_repository.get_batch(batch_id, scope)
        processing_settings = self._normalize_upload_settings(batch.get("settings") or {})
        doc_id = str(file_task.get("document_id") or "") or stable_doc_id(source_path)
        source_revision = self._source_revision_for_file(source_path, processing_settings)
        worker = getattr(self, "processing_worker", None)
        repository = getattr(worker, "repository", None)
        if repository is not None:
            for existing in repository.list_tasks(scope, upload_batch_id=batch_id, task_types={CHUNK_EXTRACT_TASK}):
                if (
                    str(existing.get("upload_file_id") or "") == file_id
                    and str(existing.get("source_revision") or "") == source_revision
                ):
                    payload = dict(existing.get("payload") or {})
                    runtime = dict(payload.get("_async_runtime") or {})
                    return {
                        "task_id": existing["id"],
                        "task_type": CHUNK_EXTRACT_TASK,
                        "status": existing.get("status"),
                        "queue": runtime.get("queue_name") or route_for_task_type(CHUNK_EXTRACT_TASK),
                        "broker_task_id": runtime.get("broker_task_id") or "",
                        "source_revision": source_revision,
                    }
        document = self.document_repository.get_document(doc_id, scope)
        metadata = dict((document or {}).get("metadata_json") or {})
        self.document_repository.upsert_document(
            id=doc_id,
            name=source_path.name,
            file_type=source_path.suffix.lower().lstrip("."),
            storage_path=str(file_task["storage_path"]),
            parse_status="pending",
            metadata_json={
                **metadata,
                "size": int(file_task.get("size") or source_path.stat().st_size),
                "upload_batch_id": batch_id,
                "upload_file_id": file_id,
                "source_revision": source_revision,
                **scope.to_dict(),
            },
            workspace_id=scope.workspace_id,
            knowledge_base_id=scope.knowledge_base_id,
        )
        self.upload_batch_repository.update_file(
            file_id,
            scope,
            status="parsing",
            document_id=doc_id,
            error_message="",
            phases=self._upload_phase_report({"parse": "queued", "chunk": "queued"}),
            warnings=[],
            errors=[],
            retry_eligible=False,
        )
        return self._enqueue_async_document_stage(
            CHUNK_EXTRACT_TASK,
            doc_id,
            scope,
            payload={
                "batch_id": batch_id,
                "file_id": file_id,
                "storage_path": file_task.get("storage_path"),
                "original_name": file_task.get("original_name"),
                "processing_settings": processing_settings,
            },
            stage="parse",
            source_revision=source_revision,
        )

    def process_chunk_extract_task(
        self,
        task: dict[str, Any],
        scope: KnowledgeBaseScope,
        *,
        cancel_check: Any | None = None,
    ) -> dict[str, Any]:
        payload = dict(task.get("payload") or {})
        source_revision = str(task.get("source_revision") or payload.get("source_revision") or "")
        file_id = str(task.get("upload_file_id") or payload.get("file_id") or "")
        batch_id = str(task.get("upload_batch_id") or payload.get("batch_id") or "")
        storage_path = str(payload.get("storage_path") or "")
        processing_settings = dict(payload.get("processing_settings") or {})
        if not storage_path:
            raise ValueError("chunk.extract task is missing storage_path")
        source_path = self._resolve_source_path(storage_path)
        resolved_processing = self._resolve_processing_settings(processing_settings)
        indexing = self._indexing_state(scope, processing_settings)
        doc_id = str(task.get("document_id") or payload.get("doc_id") or stable_doc_id(source_path))
        self._assert_document_revision_current(doc_id, scope, source_revision)
        file_size = source_path.stat().st_size
        trace = self.processing_trace_recorder.start(
            name="document_processing",
            doc_id=doc_id,
            file_name=source_path.name,
            source=storage_path,
            scope=scope.to_dict(),
            metadata={
                "file_size": file_size,
                "extension": source_path.suffix.lower(),
                "source_revision": source_revision,
                "staged_task_type": CHUNK_EXTRACT_TASK,
                "requested_processing": resolved_processing.requested.to_dict(),
                "effective_processing": resolved_processing.effective.to_dict(),
            },
        )
        parser_warnings: list[str] = []
        image_operation_summary = {"resources": 0, "operations": 0, "errors": []}
        try:
            with trace.span(
                "load",
                input={
                    "source": storage_path,
                    "file_name": source_path.name,
                    "file_type": source_path.suffix.lower().lstrip("."),
                    "size": file_size,
                    "parser_engine": resolved_processing.effective.parser_engine,
                },
            ) as load_span:
                if callable(cancel_check):
                    cancel_check()
                self.document_repository.upsert_document(
                    id=doc_id,
                    name=source_path.name,
                    file_type=source_path.suffix.lower().lstrip("."),
                    storage_path=storage_path,
                    parse_status="parsing",
                    metadata_json={
                        "size": file_size,
                        "processing_trace_id": trace.trace_id,
                        "processing_trace_dir": str(trace.trace_dir),
                        "source_revision": source_revision,
                        **scope.to_dict(),
                    },
                    workspace_id=scope.workspace_id,
                    knowledge_base_id=scope.knowledge_base_id,
                )
                parser = self.document_parser
                parser_engine = resolved_processing.effective.parser_engine
                with trace.db_subspan(
                    load_span,
                    "parser_call",
                    input={
                        "parser_engine": parser_engine,
                        "file_type": source_path.suffix.lower().lstrip("."),
                        "custom_parser": parser is not None,
                    },
                ):
                    if parser is not None:
                        parse_signature = inspect.signature(parser.parse)
                        accepts_requested_engine = "requested_engine" in parse_signature.parameters or any(
                            parameter.kind == inspect.Parameter.VAR_KEYWORD
                            for parameter in parse_signature.parameters.values()
                        )
                        parsed = parser.parse(source_path, requested_engine=parser_engine) if accepts_requested_engine else parser.parse(source_path)
                    else:
                        parsed = PARSER_REGISTRY.parse(
                            source_path,
                            engine=parser_engine,
                            force_scanned=resolved_processing.effective.pdf_force_scanned,
                            render_dpi=resolved_processing.effective.pdf_render_dpi,
                            jpeg_quality=resolved_processing.effective.pdf_jpeg_quality,
                            max_pages=resolved_processing.effective.pdf_max_pages,
                            max_image_edge_px=resolved_processing.effective.pdf_max_image_edge_px,
                            render_concurrency=resolved_processing.effective.pdf_render_concurrency,
                        )
                if self.ocr_enabled:
                    parsed = self._with_ocr_elements(parser, source_path, parsed)
                trace.reassign_doc_id(parsed.doc_id)
                if parsed.doc_id != doc_id:
                    self.document_repository.upsert_document(
                        id=parsed.doc_id,
                        name=parsed.file_name,
                        file_type=parsed.file_type,
                        storage_path=storage_path,
                        parse_status="parsing",
                        metadata_json={
                            "size": file_size,
                            "processing_trace_id": trace.trace_id,
                            "processing_trace_dir": str(trace.trace_dir),
                            "source_revision": source_revision,
                            **scope.to_dict(),
                        },
                        workspace_id=scope.workspace_id,
                        knowledge_base_id=scope.knowledge_base_id,
                    )
                    self.document_repository.delete_document(doc_id, scope)
                parsed_markdown = self._parsed_markdown(parsed)
                parsed_file = trace.write_text("parsed.md", parsed_markdown)
                parser_warnings = [str(item) for item in getattr(parsed.diagnostics, "warnings", ()) or ()]
                trace.record_output(
                    load_span,
                    {
                        "doc_id": parsed.doc_id,
                        "file_type": parsed.file_type,
                        "elements": len(parsed.elements),
                        "images": len(parsed.images),
                        "characters": len(parsed_markdown),
                        "parsed_markdown_file": parsed_file,
                        "parser_diagnostics": asdict(parsed.diagnostics),
                        "document_metadata": parsed.metadata,
                    },
                )

            with trace.span(
                "chunk_strategy",
                input={
                    "requested_strategy": resolved_processing.requested.chunk_strategy,
                    "effective_strategy": resolved_processing.effective.chunk_strategy,
                    "parent_chunk_size_chars": resolved_processing.effective.parent_chunk_size_chars,
                    "child_chunk_size_chars": resolved_processing.effective.child_chunk_size_chars,
                    "child_chunk_overlap_chars": resolved_processing.effective.child_chunk_overlap_chars,
                },
            ) as chunk_span:
                if callable(cancel_check):
                    cancel_check()
                with trace.db_subspan(
                    chunk_span,
                    "chunk_strategy_attempt",
                    input={
                        "requested_strategy": resolved_processing.requested.chunk_strategy,
                        "effective_strategy": resolved_processing.effective.chunk_strategy,
                    },
                ):
                    chunks = self._with_source_metadata(
                        self.document_chunker.chunk(parsed),
                        source=storage_path,
                        file_name=source_path.name,
                        scope=scope,
                        processing_version=resolved_processing.effective.processing_version,
                        size_unit=resolved_processing.effective.size_unit,
                        requested_parser_engine=resolved_processing.requested.parser_engine,
                        effective_parser_engine=resolved_processing.effective.parser_engine,
                    )
                self.document_repository.replace_chunks(parsed.doc_id, chunks, scope)
                self._reset_image_resources_for_reparse(parsed.doc_id, parsed.images, scope)
                image_operation_summary = self._persist_image_operations(
                    parsed.doc_id,
                    parsed.images,
                    scope,
                    processing_settings,
                    resolved_processing=resolved_processing,
                )
                chunks_file = trace.write_jsonl("chunks.jsonl", self._chunk_trace_rows(chunks))
                chunk_summary = self._chunk_trace_summary(chunks)
                trace.record_output(
                    chunk_span,
                    {
                        **chunk_summary,
                        "chunks_file": chunks_file,
                        "processing_version": resolved_processing.effective.processing_version,
                        "image_operations": image_operation_summary,
                    },
                )

            self.document_repository.upsert_document(
                id=parsed.doc_id,
                name=parsed.file_name,
                file_type=parsed.file_type,
                storage_path=storage_path,
                parse_status="chunked",
                metadata_json={
                    "size": file_size,
                    "elements": len(parsed.elements),
                    "chunks": len(chunks),
                    "parser": asdict(parsed.diagnostics),
                    "processing_trace_id": trace.trace_id,
                    "processing_trace_dir": str(trace.trace_dir),
                    "source_revision": source_revision,
                    "processing_version": resolved_processing.effective.processing_version,
                    "processing": resolved_processing.to_dict(),
                    "indexing_strategy": {key: value for key, value in indexing.items() if key != "needs_embedding"},
                    **scope.to_dict(),
                },
                workspace_id=scope.workspace_id,
                knowledge_base_id=scope.knowledge_base_id,
            )
            if file_id:
                self.upload_batch_repository.update_file(
                    file_id,
                    scope,
                    status="parsing",
                    document_id=parsed.doc_id,
                    chunks=len(chunks),
                    phases=self._upload_phase_report(
                        {"parse": "completed", "chunk": "completed", "index": "queued"},
                        warnings_by_phase={"parse": parser_warnings},
                    ),
                    warnings=parser_warnings,
                    errors=[str(item) for item in image_operation_summary.get("errors", [])],
                    retry_eligible=False,
                )
            next_task = self._enqueue_async_document_stage(
                EMBEDDING_INDEX_TASK,
                parsed.doc_id,
                scope,
                payload={
                    "batch_id": batch_id,
                    "file_id": file_id,
                    "storage_path": storage_path,
                    "processing_settings": processing_settings,
                    "image_operation_summary": image_operation_summary,
                    "parser_warnings": parser_warnings,
                    **indexing,
                },
                stage="index",
                source_revision=source_revision,
            )
            if next_task is None:
                self.process_embedding_index_task(
                    {
                        "document_id": parsed.doc_id,
                        "upload_batch_id": batch_id,
                        "upload_file_id": file_id,
                        "source_revision": source_revision,
                        "payload": {
                            "batch_id": batch_id,
                            "file_id": file_id,
                            "storage_path": storage_path,
                            "processing_settings": processing_settings,
                            "image_operation_summary": image_operation_summary,
                            "parser_warnings": parser_warnings,
                            **indexing,
                        },
                    },
                    scope,
                    cancel_check=cancel_check,
                )
            trace.finish()
            return {"doc_id": parsed.doc_id, "chunks": len(chunks), "next_task": next_task, "source_revision": source_revision}
        except Exception as exc:
            if file_id:
                self.upload_batch_repository.update_file(
                    file_id,
                    scope,
                    status="failed",
                    error_message=str(exc),
                    phases=self._upload_phase_report(
                        {"parse": "failed", "chunk": "skipped", "index": "skipped", "multimodal": "skipped", "postprocess": "skipped"},
                        errors_by_phase={"parse": [str(exc)]},
                        retry_phases={"parse"},
                    ),
                    warnings=parser_warnings,
                    errors=[str(exc)],
                    retry_eligible=True,
                )
            trace.finish(error=exc)
            raise

    def process_embedding_index_task(
        self,
        task: dict[str, Any],
        scope: KnowledgeBaseScope,
        *,
        cancel_check: Any | None = None,
    ) -> dict[str, Any]:
        payload = dict(task.get("payload") or {})
        doc_id = str(task.get("document_id") or payload.get("doc_id") or "")
        if not doc_id:
            raise ValueError("embedding.index task is missing document_id")
        source_revision = str(task.get("source_revision") or payload.get("source_revision") or "")
        self._assert_document_revision_current(doc_id, scope, source_revision)
        if callable(cancel_check):
            cancel_check()
        document = self.document_repository.get_document(doc_id, scope)
        if document is None:
            raise ValueError(f"Cannot index missing document {doc_id!r}")
        metadata = dict(document.get("metadata_json") or {})
        chunks = [self._chunk_from_repository_row(row) for row in self.document_repository.list_chunks(doc_id=doc_id, scope=scope)]
        needs_embedding = bool(payload.get("needs_embedding", True))
        dense_enabled = bool(payload.get("dense_enabled", True))
        keyword_enabled = bool(payload.get("keyword_enabled", True))
        graph_enabled = bool(payload.get("graph_enabled", False))
        wiki_enabled = bool(payload.get("wiki_enabled", False))
        image_operation_summary = dict(payload.get("image_operation_summary") or {})
        parser_warnings = [str(item) for item in payload.get("parser_warnings", [])]
        file_id = str(task.get("upload_file_id") or payload.get("file_id") or "")
        batch_id = str(task.get("upload_batch_id") or payload.get("batch_id") or "")
        storage_path = str(payload.get("storage_path") or document.get("storage_path") or "")

        self._mark_wiki_source_stale(doc_id, scope, reason="source_reindexed")
        if needs_embedding:
            replace_document_chunks = getattr(self.vector_store, "replace_document_chunks", None)
            if callable(replace_document_chunks):
                try:
                    replace_document_chunks(doc_id, chunks, scope=scope)
                except TypeError:
                    replace_document_chunks(doc_id, chunks)
            else:
                self.vector_store.upsert_chunks(chunks)
        else:
            self._delete_vector_document(doc_id, scope)
        self.document_repository.upsert_document(
            id=doc_id,
            name=str(document.get("name") or Path(storage_path).name or doc_id),
            file_type=str(document.get("file_type") or Path(storage_path).suffix.lower().lstrip(".")),
            storage_path=storage_path,
            parse_status="parsed",
            metadata_json={
                **metadata,
                "chunks": len(chunks),
                "source_revision": source_revision or metadata.get("source_revision", ""),
                "indexing_strategy": {
                    "dense_enabled": dense_enabled,
                    "keyword_enabled": keyword_enabled,
                    "graph_enabled": graph_enabled,
                    "wiki_enabled": wiki_enabled,
                },
                **scope.to_dict(),
            },
            workspace_id=scope.workspace_id,
            knowledge_base_id=scope.knowledge_base_id,
        )

        multimodal_summary: dict[str, Any]
        operations = int(image_operation_summary.get("operations", 0) or 0)
        multimodal_task = None
        if operations > 0:
            multimodal_task = self._enqueue_async_document_stage(
                IMAGE_MULTIMODAL_TASK,
                doc_id,
                scope,
                payload={
                    "batch_id": batch_id,
                    "file_id": file_id,
                    "image_count": image_operation_summary.get("resources", 0),
                },
                stage="multimodal",
                source_revision=source_revision,
            )
        if multimodal_task is not None:
            multimodal_summary = {
                "total": operations,
                "completed": 0,
                "failed": 0,
                "canceled": 0,
                "skipped": 0,
                "queued": operations,
                "errors": [],
                "task": multimodal_task,
            }
        else:
            multimodal_summary = self.process_multimodal_operations(doc_id, scope)

        postprocess_task = self._enqueue_async_document_stage(
            KNOWLEDGE_POSTPROCESS_TASK,
            doc_id,
            scope,
            payload={
                "batch_id": batch_id,
                "file_id": file_id,
                "chunks": len(chunks),
                "graph_enabled": graph_enabled,
                "wiki_enabled": wiki_enabled,
            },
            stage="postprocess",
            source_revision=source_revision,
        )
        if postprocess_task is None:
            if graph_enabled:
                graph_task = self._enqueue_async_document_stage(
                    GRAPH_EXTRACTION_TASK,
                    doc_id,
                    scope,
                    payload={"batch_id": batch_id, "file_id": file_id, "chunks": len(chunks)},
                    stage="postprocess",
                    source_revision=source_revision,
                )
                if graph_task is None:
                    self._run_kg_enrichment(doc_id, chunks, scope)
            if wiki_enabled:
                self._run_wiki_generation(doc_id, scope)
            if self.document_enrichment_service is not None:
                self.document_enrichment_service.enqueue(doc_id, chunks, scope)

        multimodal_errors = [
            *[str(item) for item in image_operation_summary.get("errors", [])],
            *[str(item) for item in multimodal_summary.get("errors", [])],
        ]
        multimodal_status = self._multimodal_phase_status(multimodal_summary)
        phases = self._upload_phase_report(
            {
                "parse": "completed",
                "chunk": "completed",
                "index": "completed",
                "multimodal": multimodal_status,
                "postprocess": "queued" if postprocess_task is not None else "completed",
            },
            warnings_by_phase={"parse": parser_warnings},
            errors_by_phase={"multimodal": multimodal_errors},
            retry_phases={"multimodal"} if multimodal_status == "partial_failed" else set(),
        )
        if file_id:
            self.upload_batch_repository.update_file(
                file_id,
                scope,
                status="completed",
                document_id=doc_id,
                chunks=len(chunks),
                phases=phases,
                warnings=parser_warnings,
                errors=multimodal_errors,
                retry_eligible=False,
            )
        if batch_id:
            self._finish_upload_batch_from_files(batch_id, scope)
        return {
            "doc_id": doc_id,
            "indexed_chunks": len(
                [chunk for chunk in chunks if chunk.chunk_type in {"child", "table", "ocr", "image_ocr", "image_caption"}]
            )
            if needs_embedding
            else 0,
            "persisted_chunks": len(chunks),
            "multimodal": multimodal_summary,
            "postprocess_task": postprocess_task,
        }

    def _process_upload_file(self, file_task: dict[str, Any], scope: KnowledgeBaseScope, cancel_check: Any | None = None) -> None:
        file_id = str(file_task["id"])
        logger.info(
            "rag_service.upload_file.process.start",
            extra={
                "workspace_id": scope.workspace_id,
                "knowledge_base_id": scope.knowledge_base_id,
                "batch_id": file_task.get("batch_id"),
                "file_id": file_id,
                "storage_path": file_task.get("storage_path"),
            },
        )
        try:
            self.upload_batch_repository.update_file(
                file_id,
                scope,
                status="parsing",
                error_message="",
                phases=self._upload_phase_report({"parse": "processing"}),
                warnings=[],
                errors=[],
                retry_eligible=False,
            )
            if callable(cancel_check):
                cancel_check()
            source_path = self._resolve_source_path(str(file_task["storage_path"]))
            batch = self.upload_batch_repository.get_batch(str(file_task["batch_id"]), scope)
            result = self.parse_and_index_document(
                source_path,
                scope=scope,
                processing_settings=batch.get("settings") or {},
                cancel_check=cancel_check,
            )
            if callable(cancel_check):
                cancel_check()
            chunks = int(result.get("parent_chunks", 0)) + int(result.get("child_chunks", 0)) + int(result.get("table_chunks", 0))
            self.upload_batch_repository.update_file(
                file_id,
                scope,
                status="indexed",
                document_id=str(result["doc_id"]),
                chunks=chunks,
            )
            multimodal_errors = [
                *[str(item) for item in result.get("image_operation_errors", [])],
                *[str(item) for item in result.get("multimodal", {}).get("errors", [])],
            ]
            multimodal_status = self._multimodal_phase_status(result.get("multimodal", {}))
            parser_warnings = [str(item) for item in result.get("parser_warnings", [])]
            phases = self._upload_phase_report(
                {
                    "parse": "completed",
                    "chunk": "completed",
                    "index": "completed",
                    "multimodal": multimodal_status,
                    "postprocess": (
                        "queued"
                        if self.document_enrichment_service is not None
                        and getattr(self.document_enrichment_service, "enabled", False)
                        else "skipped"
                    ),
                },
                warnings_by_phase={"parse": parser_warnings},
                errors_by_phase={"multimodal": multimodal_errors},
                retry_phases={"multimodal"} if multimodal_status == "partial_failed" else set(),
            )
            if self.document_enrichment_service is not None and getattr(self.document_enrichment_service, "enabled", False):
                self.upload_batch_repository.update_file(
                    file_id,
                    scope,
                    status="enrichment_pending",
                    phases=phases,
                    warnings=parser_warnings,
                    errors=multimodal_errors,
                    retry_eligible=False,
                )
            self.upload_batch_repository.update_file(
                file_id,
                scope,
                status="completed",
                phases=phases,
                warnings=parser_warnings,
                errors=multimodal_errors,
                retry_eligible=False,
            )
            logger.info(
                "rag_service.upload_file.process.end",
                extra={
                    "workspace_id": scope.workspace_id,
                    "knowledge_base_id": scope.knowledge_base_id,
                    "batch_id": file_task.get("batch_id"),
                    "file_id": file_id,
                    "document_id": result.get("doc_id"),
                    "chunks": chunks,
                },
            )
        except Exception as exc:
            logger.exception("Upload file task failed: %s", file_id)
            error = str(exc)
            trace_error = ""
            trace_dir = getattr(exc, "processing_trace_dir", "")
            if trace_dir:
                trace_error = f"processing trace: {trace_dir}"
            file_errors = [error, trace_error] if trace_error else [error]
            self.upload_batch_repository.update_file(
                file_id,
                scope,
                status="failed",
                error_message=error,
                phases=self._upload_phase_report(
                    {
                        "parse": "failed",
                        "chunk": "skipped",
                        "index": "skipped",
                        "multimodal": "skipped",
                        "postprocess": "skipped",
                    },
                    errors_by_phase={"parse": file_errors},
                    retry_phases={"parse"},
                ),
                warnings=[],
                errors=file_errors,
                retry_eligible=True,
            )

    def _finish_upload_batch_from_files(self, batch_id: str, scope: KnowledgeBaseScope) -> None:
        files = self.upload_batch_repository.list_files(batch_id, scope)
        failed = [item for item in files if item["status"] == "failed"]
        completed = [item for item in files if item["status"] == "completed"]
        canceled = [item for item in files if item["status"] == "canceled"]
        if failed and completed:
            status = "partial_failed"
        elif failed and not completed:
            status = "failed"
        elif canceled and not failed and not completed:
            status = "canceled"
        else:
            status = "completed"
        self.upload_batch_repository.update_batch(batch_id, scope, status=status)

    def save_uploaded_document(
        self,
        filename: str,
        content: bytes,
        relative_path: str | None = None,
        batch_id: str | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> dict[str, Any]:
        scope = scope or self.default_scope
        if self.knowledge_base_service is not None:
            self.knowledge_base_service.assert_writable(scope)
        logger.info(
            "rag_service.document.upload.save.start",
            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "file_name": filename, "size": len(content)},
        )
        target, source = self._save_upload_source_file(
            filename=filename,
            content=content,
            relative_path=relative_path,
            batch_id=batch_id,
            scope=scope,
        )
        result = self.parse_and_index_document(target, scope=scope)
        chunks = int(result.get("parent_chunks", 0)) + int(result.get("child_chunks", 0)) + int(result.get("table_chunks", 0))
        response = {
            "doc_id": result["doc_id"],
            "source": source,
            "filename": target.name,
            "size": len(content),
            "parse_status": "parsed",
            "chunks": chunks,
            "error": None,
        }
        logger.info(
            "rag_service.document.upload.save.end",
            extra={
                "workspace_id": scope.workspace_id,
                "knowledge_base_id": scope.knowledge_base_id,
                "doc_id": response["doc_id"],
                "source": source,
                "chunks": chunks,
            },
        )
        return response

    def parse_document(self, source: str, scope: KnowledgeBaseScope | None = None) -> dict[str, Any]:
        scope = scope or self.default_scope
        self._assert_source_in_scope(source, scope)
        source_path = self._resolve_source_path(source)
        if not source_path.exists() or not source_path.is_file():
            raise FileNotFoundError(source)
        file_size = source_path.stat().st_size
        max_file_bytes = int(self.processing_defaults.preview_max_file_bytes)
        if file_size > max_file_bytes:
            raise ProcessingPreviewError(
                f"Preview file size {file_size} exceeds limit {max_file_bytes}"
            )
        started = time.monotonic()
        try:
            parsed = self.document_parser.parse(source_path)
            self._enforce_preview_elapsed(started)
            self._enforce_preview_page_limit(parsed)
            chunks = self.document_chunker.chunk(parsed)
            self._enforce_preview_elapsed(started)
        except ProcessingPreviewError:
            raise
        except Exception as exc:
            raise ProcessingPreviewError(f"Preview failed: {self._sanitize_preview_error(exc)}") from exc
        parent_chunks = [chunk for chunk in chunks if chunk.chunk_type == "parent"]
        child_chunks = [chunk for chunk in chunks if chunk.chunk_type == "child"]
        table_chunks = [chunk for chunk in chunks if chunk.chunk_type == "table"]
        from app.services.documents.adaptive_chunker import AdaptiveChunkConfig, split_with_diagnostics
        try:
            _, chunk_diagnostics = split_with_diagnostics(
                parsed.markdown or "\n\n".join(element.markdown for element in parsed.elements),
                AdaptiveChunkConfig(
                    chunk_size_chars=self.document_chunker.child_max_tokens,
                    chunk_overlap_chars=self.document_chunker.child_overlap_tokens,
                    strategy=self.document_chunker.strategy,
                ),
            )
            self._enforce_preview_elapsed(started)
        except ProcessingPreviewError:
            raise
        except Exception as exc:
            raise ProcessingPreviewError(f"Preview failed: {self._sanitize_preview_error(exc)}") from exc
        chunk_lengths = [len(chunk.content) for chunk in chunks]
        average = sum(chunk_lengths) / len(chunk_lengths) if chunk_lengths else 0.0
        variance = sum((length - average) ** 2 for length in chunk_lengths) / len(chunk_lengths) if chunk_lengths else 0.0
        max_chunks = int(self.processing_defaults.preview_max_chunks)
        return {
            "doc_id": parsed.doc_id,
            "source": source,
            "extension": source_path.suffix.lower(),
            "characters": len(parsed.markdown),
            "parent_chunks": len(parent_chunks),
            "child_chunks": len(child_chunks),
            "table_chunks": len(table_chunks),
            "preview": (parsed.markdown or "\n\n".join(element.markdown for element in parsed.elements))[:2000],
            "parser_diagnostics": asdict(parsed.diagnostics),
            "document_metadata": parsed.metadata,
            "chunk_diagnostics": asdict(chunk_diagnostics),
            "chunk_statistics": {
                "count": len(chunk_lengths), "minimum": min(chunk_lengths, default=0),
                "maximum": max(chunk_lengths, default=0), "average": average,
                "stddev": variance ** 0.5,
                "tiny_count": sum(1 for length in chunk_lengths if length < 50),
                "oversize_count": sum(1 for length in chunk_lengths if length > self.document_chunker.child_max_tokens * 2),
            },
            "chunk_previews": [
                {
                    "id": chunk.id, "type": chunk.chunk_type, "characters": len(chunk.content),
                    "approx_tokens": chunk.token_count, "parent_id": chunk.parent_id,
                    "title_path": chunk.title_path, "page_start": chunk.page_start,
                    "page_end": chunk.page_end, "strategy": chunk.metadata.get("strategy", ""),
                    "preview": chunk.content[:300],
                }
                for chunk in chunks[:max_chunks]
            ],
        }

    def _enforce_preview_elapsed(self, started: float) -> None:
        timeout_seconds = float(self.processing_defaults.preview_timeout_seconds)
        if time.monotonic() - started > timeout_seconds:
            raise ProcessingPreviewError(
                f"Preview exceeded runtime limit {timeout_seconds:.2f}s"
            )

    def _enforce_preview_page_limit(self, parsed) -> None:
        max_pages = int(self.processing_defaults.preview_max_pages)
        metadata_pages = parsed.metadata.get("page_count") if isinstance(parsed.metadata, dict) else None
        pages = int(metadata_pages or 0)
        if pages <= 0:
            page_numbers = []
            for element in parsed.elements:
                if element.page_start is not None:
                    page_numbers.append(int(element.page_start))
                if element.page_end is not None:
                    page_numbers.append(int(element.page_end))
            pages = max(page_numbers, default=0)
        if pages > max_pages:
            raise ProcessingPreviewError(
                f"Preview page count {pages} exceeds limit {max_pages}"
            )

    def _sanitize_preview_error(self, exc: Exception) -> str:
        message = " ".join(str(exc).split()) or exc.__class__.__name__
        data_root = str(self.data_dir.resolve())
        message = message.replace(data_root, "<data-dir>")
        root_name = re.escape(self.data_dir.resolve().name)
        message = re.sub(rf"[A-Za-z]:\\[^\s]*{root_name}[^\s]*", "<data-dir>", message)
        message = re.sub(rf"/[^\s]*{root_name}[^\s]*", "<data-dir>", message)
        return message[:300]

    def _run_wiki_generation(self, doc_id: str, scope: KnowledgeBaseScope) -> dict[str, Any]:
        service = getattr(self, "wiki_page_service", None)
        worker = getattr(self, "processing_worker", None)
        ingest_service = getattr(worker, "wiki_ingest_service", None)
        enqueue = getattr(ingest_service, "enqueue_document", None)
        if callable(enqueue):
            try:
                return enqueue(scope, doc_id, trace_id=_current_log_trace_id() or "")
            except Exception as exc:
                message = self._sanitize_preview_error(exc)
                logger.warning(
                    "wiki.generation.enqueue_failed",
                    extra={
                        "workspace_id": scope.workspace_id,
                        "knowledge_base_id": scope.knowledge_base_id,
                        "doc_id": doc_id,
                        "error_type": exc.__class__.__name__,
                        "error_message": message,
                    },
                )
                return {"task": {"status": "failed", "error_message": message}}
        generate = getattr(service, "generate_draft_for_document", None)
        if not callable(generate):
            return {"task": {"status": "skipped", "error_message": "Wiki service is unavailable"}}
        try:
            return generate(scope, doc_id)
        except Exception as exc:
            message = self._sanitize_preview_error(exc)
            logger.info(
                "wiki.generation.skipped",
                extra={
                    "workspace_id": scope.workspace_id,
                    "knowledge_base_id": scope.knowledge_base_id,
                    "doc_id": doc_id,
                    "error_type": exc.__class__.__name__,
                    "error_message": message,
                },
            )
            return {"task": {"status": "skipped", "error_message": message}}

    def _mark_wiki_source_stale(self, doc_id: str, scope: KnowledgeBaseScope, *, reason: str) -> list[dict[str, Any]]:
        service = getattr(self, "wiki_page_service", None)
        mark = getattr(service, "mark_source_stale", None)
        if not callable(mark):
            return []
        try:
            return mark(scope, doc_id=doc_id, reason=reason)
        except Exception as exc:
            message = self._sanitize_preview_error(exc)
            logger.info(
                "wiki.source_stale.skipped",
                extra={
                    "workspace_id": scope.workspace_id,
                    "knowledge_base_id": scope.knowledge_base_id,
                    "doc_id": doc_id,
                    "reason": reason,
                    "error_type": exc.__class__.__name__,
                    "error_message": message,
                },
            )
            return []

    def ingest_document_by_id(
        self,
        doc_id: str,
        scope: KnowledgeBaseScope | None = None,
    ) -> dict[str, Any]:
        scope = scope or self.default_scope
        doc = self.document_repository.get_document(doc_id, scope)
        if not doc:
            raise FileNotFoundError(doc_id)
        result = self.parse_document(str(doc.get("storage_path") or doc.get("source") or ""), scope=scope)
        return {
            "doc_id": result["doc_id"],
            "parse_status": "parsed",
            "chunk_count": result["parent_chunks"] + result["child_chunks"] + result["table_chunks"],
            "vector_count": result["child_chunks"] + result["table_chunks"],
        }

    def answer_query(
        self,
        question: str,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> dict[str, Any]:
        scope = scope or self.default_scope
        query_log_id = self.audit_repository.start_query(question, scope)
        started = time.monotonic()
        logger.info(
            "rag_service.query.start",
            extra={
                "workspace_id": scope.workspace_id,
                "knowledge_base_ids": list(scope.selected_knowledge_base_ids),
                "document_ids": list(scope.document_ids),
                "top_k": top_k,
                "agentic": bool(self.agentic_retrieval_enabled and self.agentic_workflow is not None),
            },
        )
        try:
            if self.agentic_retrieval_enabled and self.agentic_workflow is not None:
                result = self.agentic_workflow.run_query(question, top_k=top_k, filters=filters, scope=scope)
            else:
                result = self._answer_query_raw(question, top_k=top_k, filters=filters, scope=scope)
            self.audit_repository.finish_query(
                query_log_id,
                status="completed",
                tool_calls=list(result.get("tool_calls") or []),
                citation_chunk_ids=list(result.get("used_chunks") or []),
                response_metadata={
                    "confidence": float(result.get("confidence") or 0.0),
                    "citation_count": len(result.get("citations") or []),
                    "knowledge_base_scope": scope.to_dict(),
                },
            )
            logger.info(
                "rag_service.query.end",
                extra={
                    "workspace_id": scope.workspace_id,
                    "knowledge_base_ids": list(scope.selected_knowledge_base_ids),
                    "used_chunks": len(result.get("used_chunks") or []),
                    "citations": len(result.get("citations") or []),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
            return result
        except Exception as exc:
            self.audit_repository.finish_query(query_log_id, status="failed", error_message=str(exc))
            logger.exception(
                "rag_service.query.failed",
                extra={
                    "workspace_id": scope.workspace_id,
                    "knowledge_base_ids": list(scope.selected_knowledge_base_ids),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                },
            )
            raise

    def _answer_query_raw(
        self,
        question: str,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> dict[str, Any]:
        scope = scope or self.default_scope
        previous_top_k = self.top_k
        if top_k:
            self.top_k = top_k
        try:
            hits = self.recall_parent_hits(self.hybrid_retrieve_hits(question, scope=scope), scope=scope)
            if not hits:
                return {
                    "answer": "I cannot determine the answer from the available evidence.",
                    "citations": [],
                    "used_chunks": [],
                    "used_entities": [],
                    "graph_paths": [],
                    "confidence": 0.0,
                    "agent_trace": [],
                    "tool_calls": [],
                    "evidence_summary": {},
                    "debug_info": self._last_retrieval_debug if self.retrieval_debug_enabled else None,
                }
            answer = "".join(self.stream_answer(question, hits=hits))
            used_chunks = self._valid_used_chunks(hits, scope=scope)
            debug_info = None
            if self.retrieval_debug_enabled:
                context = self._build_context(hits)
                debug_info = {
                    **self._last_retrieval_debug,
                    "selected_parent_chunks": [hit.get("metadata", {}) for hit in hits],
                    "final_context_token_count": max(1, len(context) // 4) if context else 0,
                    "knowledge_base_scope": scope.to_dict(),
                }
            return {
                "answer": answer,
                "citations": self.extract_sources(hits),
                "used_chunks": used_chunks,
                "used_entities": [],
                "graph_paths": [],
                "confidence": self._calculate_retrieval_confidence(hits),
                "agent_trace": [],
                "tool_calls": [],
                "evidence_summary": {},
                "debug_info": debug_info,
            }
        finally:
            self.top_k = previous_top_k

    def _valid_used_chunks(
        self,
        hits: list[dict[str, Any]],
        scope: KnowledgeBaseScope | None = None,
    ) -> list[str]:
        chunk_ids = [
            str(child_id)
            for hit in hits
            for child_id in hit.get("metadata", {}).get("matched_child_ids", [])
            if child_id
        ]
        valid = []
        for chunk_id in dict.fromkeys(chunk_ids):
            if self.document_repository.get_chunk(chunk_id, scope):
                valid.append(chunk_id)
        return valid

    def _calculate_retrieval_confidence(self, hits: list[dict[str, Any]]) -> float:
        if not hits:
            return 0.0
        scores = [
            max(0.0, min(1.0, self._retrieval_score(hit)))
            for hit in hits
        ]
        return round(max(scores), 4) if scores else 0.0

    def delete_document(self, doc_id: str, scope: KnowledgeBaseScope | None = None) -> None:
        scope = scope or self.default_scope
        if self.knowledge_base_service is not None:
            self.knowledge_base_service.assert_writable(scope)
        if self.processing_worker is not None:
            self.processing_worker.repository.cancel_for_document(scope, doc_id, reason="document deleted")
        try:
            self.processing_trace_recorder.span_tracker.cancel_all_open_spans(doc_id, reason="document deleted")
        except Exception as exc:
            logger.warning("Failed to cancel open processing spans for deleted document %s: %s", doc_id, exc)
        logger.info(
            "rag_service.document.delete.start",
            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "doc_id": doc_id},
        )
        doc = self.document_repository.get_document(doc_id, scope)
        if doc:
            storage_path = str(doc.get("storage_path") or doc.get("source") or "")
            if storage_path:
                try:
                    file_path = self._resolve_source_path(storage_path)
                    if file_path.exists() and file_path.is_file():
                        logger.info(
                            "rag_service.document.delete.file",
                            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "doc_id": doc_id, "storage_path": storage_path},
                        )
                        file_path.unlink()
                except FileNotFoundError:
                    pass
            for storage_key in self.image_repository.delete_document_images(doc_id, scope):
                logger.info(
                    "rag_service.document.delete.object",
                    extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "doc_id": doc_id, "storage_key": storage_key},
                )
                self.object_storage.delete(storage_key)
        self._mark_wiki_source_stale(doc_id, scope, reason="source_deleted")
        logger.info(
            "rag_service.document.delete.sqlite",
            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "doc_id": doc_id},
        )
        self.document_repository.delete_document(doc_id, scope)
        delete_document = getattr(self.vector_store, "delete_document", None)
        if callable(delete_document):
            try:
                logger.info(
                    "rag_service.document.delete.vector",
                    extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "doc_id": doc_id},
                )
                delete_document(doc_id, scope=scope)
            except TypeError:
                delete_document(doc_id)
        logger.info(
            "rag_service.document.delete.end",
            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "doc_id": doc_id},
        )

    def _delete_vector_document(self, doc_id: str, scope: KnowledgeBaseScope) -> None:
        delete_document = getattr(self.vector_store, "delete_document", None)
        if not callable(delete_document):
            return
        try:
            delete_document(doc_id, scope=scope)
        except TypeError:
            delete_document(doc_id)

    def list_documents(
        self,
        scope: KnowledgeBaseScope | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        scope = scope or self.default_scope
        docs = self.document_repository.list_documents(scope)
        if docs:
            normalized = []
            for doc in docs:
                metadata = doc.get("metadata_json", {})
                normalized.append(
                    {
                        **doc,
                        "source": doc.get("storage_path", ""),
                        "size": int(metadata.get("size", 0) or 0),
                        **self._document_runtime_status(doc, scope),
                    }
                )
            return self._filter_document_items(normalized, filters)
        if not scope.compatibility_default or scope.knowledge_base_id != self.default_scope.knowledge_base_id:
            return []
        fallback = []
        for file in iter_source_files(self.data_dir):
            stat = file.stat()
            source = str(file.relative_to(self.data_dir))
            fallback.append(
                {
                    "id": stable_doc_id(file),
                    "name": file.name,
                    "file_type": file.suffix.lower().lstrip("."),
                    "storage_path": source,
                    "source": source,
                    "parse_status": "pending",
                    "size": int(stat.st_size),
                    "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(timespec="seconds"),
                    "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    "chunks": 0,
                    "metadata_json": {},
                    "summary_available": False,
                    "processing_task_id": "",
                    "processing_task_type": "",
                    "processing_task_status": "",
                    "processing_task_queue": "",
                    "processing_broker_task_id": "",
                    "processing_task_attempt": 0,
                    "processing_task_max_attempts": 0,
                    "processing_dead_lettered": False,
                    "processing_last_error": "",
                    "processing_dead_letter_reason": "",
                    "processing_retry_available": False,
                    "processing_latest_attempt": 0,
                }
            )
        return self._filter_document_items(sorted(fallback, key=lambda x: x["updated_at"], reverse=True), filters)

    def _document_runtime_status(self, doc: dict[str, Any], scope: KnowledgeBaseScope) -> dict[str, Any]:
        doc_id = str(doc.get("id") or "")
        tasks, dead_letters = self._processing_tasks_for_document(doc_id, scope)
        latest_task = self._select_display_processing_task(tasks)
        latest_attempt = 0
        try:
            latest_attempt = self.processing_trace_recorder.span_tracker.latest_attempt(doc_id)
        except Exception:
            latest_attempt = 0
        summary = str(doc.get("summary") or "").strip()
        summary_status = str(doc.get("summary_status") or "none")
        last_error = str(latest_task.get("last_error_message") or "")
        if dead_letters and not last_error:
            last_error = str(dead_letters[-1].get("error_message") or "")
        payload = latest_task.get("payload") if isinstance(latest_task.get("payload"), dict) else {}
        async_runtime = payload.get("_async_runtime") if isinstance(payload.get("_async_runtime"), dict) else {}
        latest_status = str(latest_task.get("status") or "")
        latest_is_dead_letter = latest_status == TASK_DEAD_LETTERED
        dead_letter_reason = str((dead_letters[-1].get("error_message") if dead_letters else "") or "")
        return {
            "summary_available": bool(summary and summary_status == "completed"),
            "processing_task_id": str(latest_task.get("id") or ""),
            "processing_task_type": str(latest_task.get("task_type") or ""),
            "processing_task_status": str(latest_task.get("status") or ""),
            "processing_task_queue": str(async_runtime.get("queue_name") or ""),
            "processing_broker_task_id": str(async_runtime.get("broker_task_id") or ""),
            "processing_task_attempt": int(latest_task.get("attempt") or 0),
            "processing_task_max_attempts": int(latest_task.get("max_attempts") or 0),
            "processing_dead_lettered": bool(latest_is_dead_letter),
            "processing_last_error": last_error,
            "processing_dead_letter_reason": dead_letter_reason,
            "processing_retry_available": bool(latest_is_dead_letter or latest_status == TASK_PENDING),
            "processing_latest_attempt": latest_attempt,
        }

    def _processing_tasks_for_document(
        self, doc_id: str, scope: KnowledgeBaseScope
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if self.processing_worker is None:
            return [], []
        try:
            tasks = self.processing_worker.repository.list_tasks(scope, document_id=doc_id)
            dead_letters = [
                item
                for item in self.processing_worker.repository.list_dead_letters(scope)
                if str(item.get("document_id") or "") == doc_id
            ]
            return tasks, dead_letters
        except Exception as exc:
            logger.warning("Failed to read processing task status for document %s: %s", doc_id, exc)
            return [], []

    def _select_display_processing_task(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        if not tasks:
            return {}
        active = [
            task
            for task in reversed(tasks)
            if str(task.get("status") or "").lower() in ACTIVE_PROCESSING_TASK_STATUSES
        ]
        if active:
            return active[0]
        failed = [
            task
            for task in reversed(tasks)
            if str(task.get("status") or "").lower() in FAILED_PROCESSING_TASK_STATUSES
        ]
        if failed:
            return failed[0]
        visible_terminal = [
            task
            for task in reversed(tasks)
            if str(task.get("status") or "").lower() != TASK_CANCELED
        ]
        return visible_terminal[0] if visible_terminal else tasks[-1]

    def _overlay_processing_tasks_on_trace(
        self, root: dict[str, Any], tasks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not tasks:
            return root
        merged_root = copy.deepcopy(root)
        stages = merged_root.get("children")
        if not isinstance(stages, list):
            return merged_root
        tasks_by_stage: dict[str, list[dict[str, Any]]] = {}
        for task in tasks:
            stage_name = PROCESSING_TASK_STAGE_NAMES.get(str(task.get("task_type") or ""))
            if stage_name:
                tasks_by_stage.setdefault(stage_name, []).append(task)
        if not tasks_by_stage:
            return merged_root

        for stage in stages:
            if not isinstance(stage, dict):
                continue
            stage_name = str(stage.get("name") or "")
            stage_tasks = tasks_by_stage.get(stage_name)
            if not stage_tasks:
                continue
            stage["status"] = self._aggregate_task_trace_status(stage_tasks, str(stage.get("status") or "pending"))
            if stage["status"] in {"pending", "running"}:
                stage["ended_at"] = ""
            output = stage.get("output") if isinstance(stage.get("output"), dict) else {}
            stage["output"] = {
                **output,
                "processing_tasks": [
                    {
                        "id": str(task.get("id") or ""),
                        "type": str(task.get("task_type") or ""),
                        "status": str(task.get("status") or ""),
                        "attempt": int(task.get("attempt") or 0),
                        "max_attempts": int(task.get("max_attempts") or 0),
                    }
                    for task in stage_tasks
                ],
            }

        active_task_statuses = {
            str(task.get("status") or "").lower()
            for task in tasks
            if str(task.get("status") or "").lower() in ACTIVE_PROCESSING_TASK_STATUSES
        }
        stage_statuses = {str(stage.get("status") or "").lower() for stage in stages if isinstance(stage, dict)}
        if "failed" in stage_statuses:
            merged_root["status"] = "failed"
        elif TASK_PROCESSING in active_task_statuses or TASK_RETRYING in active_task_statuses or "running" in stage_statuses:
            merged_root["status"] = "running"
        elif TASK_PENDING in active_task_statuses:
            merged_root["status"] = "pending"
        elif tasks and all(str(task.get("status") or "").lower() in TERMINAL_PROCESSING_TASK_STATUSES for task in tasks):
            merged_root["status"] = "done"
        return merged_root

    def _aggregate_task_trace_status(self, tasks: list[dict[str, Any]], fallback_status: str) -> str:
        statuses = [self._task_trace_status(str(task.get("status") or "")) for task in tasks]
        if "failed" in statuses:
            return "failed"
        if "running" in statuses:
            return "running"
        if "pending" in statuses:
            return "pending"
        if statuses and all(status in {"done", "cancelled"} for status in statuses):
            return "done" if "done" in statuses else "cancelled"
        return self._map_trace_status(fallback_status)

    def _task_trace_status(self, status: str) -> str:
        normalized = status.lower()
        if normalized in {TASK_PROCESSING, TASK_RETRYING}:
            return "running"
        if normalized == TASK_PENDING:
            return "pending"
        if normalized == TASK_COMPLETED:
            return "done"
        if normalized in FAILED_PROCESSING_TASK_STATUSES:
            return "failed"
        if normalized == TASK_CANCELED:
            return "cancelled"
        return self._map_trace_status(normalized)

    def _current_trace_stage(self, stages: list[dict[str, Any]]) -> str:
        current_stage = next(
            (str(stage.get("name") or "") for stage in stages if isinstance(stage, dict) and stage.get("status") == "running"),
            "",
        )
        if not current_stage:
            current_stage = next(
                (
                    str(stage.get("name") or "")
                    for stage in stages
                    if isinstance(stage, dict) and stage.get("status") in {"pending", "failed"}
                ),
                "",
            )
        return current_stage

    def retry_document_processing_task(self, doc_id: str, scope: KnowledgeBaseScope) -> dict[str, Any]:
        if self.processing_worker is None:
            raise ValueError("Document processing worker is disabled")
        document = self.document_repository.get_document(doc_id, scope)
        if document is None:
            raise KeyError(doc_id)
        repository = self.processing_worker.repository
        tasks = repository.list_tasks(scope, document_id=doc_id)
        task = next((item for item in reversed(tasks) if item.get("status") == "dead_lettered"), None)
        if task is not None:
            retried = repository.retry_dead_letter(str(task["id"]), delay_seconds=0)
        else:
            retried = next((item for item in reversed(tasks) if item.get("status") in {TASK_PENDING, TASK_RETRYING}), None)
            if retried is None:
                raise ValueError("No dead-letter, pending, or retrying processing task is available for retry")
        queue = getattr(self, "async_processing_queue", None)
        if queue is not None and getattr(queue, "enabled", False):
            dispatch = queue.enqueue(retried)
            record_dispatch = getattr(repository, "record_broker_dispatch", None)
            if callable(record_dispatch):
                repository.record_broker_dispatch(retried["id"], broker_task_id=dispatch.broker_task_id, queue_name=dispatch.queue)
        document = self.document_repository.get_document(doc_id, scope)
        if document is None:
            raise KeyError(doc_id)
        metadata = document.get("metadata_json") or {}
        return {
            **document,
            "source": document.get("storage_path", ""),
            "size": int(metadata.get("size", 0) or 0),
            **self._document_runtime_status(document, scope),
        }

    def get_document_processing_trace(self, doc_id: str, scope: KnowledgeBaseScope | None = None) -> dict[str, Any]:
        scope = scope or self.default_scope
        doc = self.document_repository.get_document(doc_id, scope)
        if doc is None:
            raise KeyError(doc_id)

        processing_tasks, _ = self._processing_tasks_for_document(doc_id, scope)
        span_tree = self.processing_trace_recorder.span_tracker.latest_tree(doc_id)
        if span_tree is not None:
            root = self._overlay_processing_tasks_on_trace(span_tree["root"], processing_tasks)
            stages = root.get("children") or []
            current_stage = self._current_trace_stage(stages)
            metadata = doc.get("metadata_json") or {}
            runtime_status = self._document_runtime_status(doc, scope)
            return {
                "document_id": doc_id,
                "knowledge_base_id": doc.get("knowledge_base_id", scope.knowledge_base_id),
                "parse_status": doc.get("parse_status", ""),
                "summary_status": doc.get("summary_status", "none"),
                "current_attempt": int(span_tree["attempt"]),
                "current_stage": current_stage,
                "trace": root,
                "last_error": root.get("error"),
                "trace_dir": str(metadata.get("processing_trace_dir") or root.get("metadata", {}).get("trace_dir") or ""),
                "processing_task": runtime_status,
            }

        metadata = doc.get("metadata_json") or {}
        trace_payload: dict[str, Any] | None = None
        trace_dir_value = str(metadata.get("processing_trace_dir") or "")
        trace_dir = Path(trace_dir_value) if trace_dir_value and trace_dir_value != "." else None
        if trace_dir is not None:
            trace_root = self.processing_trace_recorder.root_dir.resolve()
            resolved_trace_dir = trace_dir.resolve()
            try:
                resolved_trace_dir.relative_to(trace_root)
            except ValueError as exc:
                raise ValueError("Processing trace path is outside configured trace directory") from exc
            trace_file = resolved_trace_dir / "trace.json"
            if trace_file.exists():
                try:
                    trace_payload = json.loads(trace_file.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    trace_payload = {
                        "trace_id": str(metadata.get("processing_trace_id") or ""),
                        "name": "document_processing",
                        "status": "running",
                        "doc_id": doc_id,
                        "file_name": doc.get("name", ""),
                        "source": doc.get("storage_path", ""),
                        "spans": [],
                        "error": None,
                        "trace_dir": str(resolved_trace_dir),
                    }

        root = self._overlay_processing_tasks_on_trace(self._weknora_trace_root(doc, trace_payload), processing_tasks)
        stages = root["children"]
        current_stage = self._current_trace_stage(stages)

        runtime_status = self._document_runtime_status(doc, scope)
        return {
            "document_id": doc_id,
            "knowledge_base_id": doc.get("knowledge_base_id", scope.knowledge_base_id),
            "parse_status": doc.get("parse_status", ""),
            "summary_status": doc.get("summary_status", "none"),
            "current_attempt": 1,
            "current_stage": current_stage,
            "trace": root,
            "last_error": root.get("error"),
            "trace_dir": str(trace_payload.get("trace_dir") or trace_dir or "") if trace_payload else str(trace_dir or ""),
            "processing_task": runtime_status,
        }

    def _weknora_trace_root(self, doc: dict[str, Any], payload: dict[str, Any] | None) -> dict[str, Any]:
        spans = list((payload or {}).get("spans") or [])
        span_by_name = {str(span.get("name") or ""): span for span in spans}
        root_status = self._map_trace_status(str((payload or {}).get("status") or doc.get("parse_status") or "pending"))
        summary_status = str(doc.get("summary_status") or "none")
        if summary_status in {"pending", "processing"}:
            root_status = "running"
        stages: list[dict[str, Any]] = []
        failure_seen = False
        for canonical_name, label, source_name in TRACE_STAGE_DEFINITIONS:
            source_span = span_by_name.get(source_name)
            if source_span:
                stage = self._weknora_stage_from_span(canonical_name, label, source_span)
            else:
                stage = self._synthetic_weknora_stage(canonical_name, label, doc, payload, failure_seen)
            if canonical_name == "postprocess" and summary_status in {"pending", "processing"} and stage["status"] in {"done", "pending", "skipped"}:
                stage = {**stage, "status": "running", "ended_at": "", "output": {**stage.get("output", {}), "summary_status": summary_status}}
            stages.append(stage)
            failure_seen = failure_seen or stage["status"] == "failed"

        if any(stage["status"] == "failed" for stage in stages):
            root_status = "failed"
        elif any(stage["status"] == "running" for stage in stages):
            root_status = "running"
        elif stages and all(stage["status"] in {"done", "skipped"} for stage in stages):
            root_status = "done"

        return {
            "span_id": str((payload or {}).get("trace_id") or ""),
            "name": "knowledge_processing",
            "label": "知识处理",
            "kind": "root",
            "status": root_status,
            "started_at": str((payload or {}).get("started_at") or doc.get("created_at") or ""),
            "ended_at": str((payload or {}).get("ended_at") or ""),
            "duration_ms": int((payload or {}).get("duration_ms") or 0),
            "input": {
                "source": doc.get("storage_path", ""),
                "file_name": doc.get("name", ""),
            },
            "output": {},
            "error": (payload or {}).get("error") or self._document_trace_error(doc),
            "children": stages,
        }

    def _weknora_stage_from_span(self, canonical_name: str, label: str, span: dict[str, Any]) -> dict[str, Any]:
        error = span.get("error")
        return {
            "span_id": str(span.get("span_id") or canonical_name),
            "name": canonical_name,
            "label": label,
            "kind": "stage",
            "status": self._map_trace_status(str(span.get("status") or "pending")),
            "started_at": str(span.get("started_at") or ""),
            "ended_at": str(span.get("ended_at") or ""),
            "duration_ms": int(span.get("duration_ms") or 0),
            "input": span.get("input") or {},
            "output": span.get("output") or {},
            "error": error,
            "children": [],
        }

    def _synthetic_weknora_stage(
        self,
        canonical_name: str,
        label: str,
        doc: dict[str, Any],
        payload: dict[str, Any] | None,
        failure_seen: bool,
    ) -> dict[str, Any]:
        parse_status = str(doc.get("parse_status") or "pending")
        trace_status = str((payload or {}).get("status") or "")
        if failure_seen or parse_status == "failed" or trace_status == "failed":
            status = "skipped"
        elif parse_status == "parsed":
            status = "skipped" if canonical_name in {"multimodal", "postprocess"} else "done"
        elif parse_status == "parsing" and canonical_name == "docreader":
            status = "running"
        else:
            status = "pending"
        return {
            "span_id": canonical_name,
            "name": canonical_name,
            "label": label,
            "kind": "stage",
            "status": status,
            "started_at": "",
            "ended_at": "",
            "duration_ms": 0,
            "input": {},
            "output": {},
            "error": None,
            "children": [],
        }

    def _map_trace_status(self, status: str) -> str:
        return TRACE_STATUS_MAP.get(status.lower(), status.lower() or "pending")

    def _document_trace_error(self, doc: dict[str, Any]) -> dict[str, Any] | None:
        metadata = doc.get("metadata_json") or {}
        message = str(metadata.get("error_message") or doc.get("summary_error") or "")
        if not message:
            return None
        return {"type": "DocumentProcessingError", "message": message, "traceback": ""}

    def _filter_document_items(
        self,
        documents: list[dict[str, Any]],
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        filters = filters or {}
        query = str(filters.get("q") or "").strip().lower()
        tag = str(filters.get("tag") or "").strip().lower()
        file_type = str(filters.get("file_type") or "").strip().lower()
        status = str(filters.get("status") or "").strip().lower()
        source = str(filters.get("source") or "").strip().lower()
        created_from = str(filters.get("created_from") or "").strip()
        created_to = str(filters.get("created_to") or "").strip()
        filtered = []
        for item in documents:
            metadata = item.get("metadata_json") or {}
            keywords = item.get("keywords_json") or metadata.get("keywords_json") or []
            haystack = " ".join(
                str(value or "")
                for value in (
                    item.get("name"),
                    item.get("storage_path"),
                    item.get("source"),
                    item.get("summary"),
                    " ".join(keywords) if isinstance(keywords, list) else keywords,
                )
            ).lower()
            updated_at = str(item.get("updated_at") or item.get("created_at") or "")
            if query and query not in haystack:
                continue
            if tag and tag not in haystack:
                continue
            if file_type and str(item.get("file_type") or "").lower() != file_type:
                continue
            if status and status not in {str(item.get("parse_status") or "").lower(), str(item.get("summary_status") or "").lower()}:
                continue
            if source and source not in str(item.get("source") or item.get("storage_path") or "").lower():
                continue
            if created_from and updated_at < created_from:
                continue
            if created_to and updated_at > created_to:
                continue
            filtered.append(item)
        return filtered

    def _sanitize_filename(self, text: str) -> str:
        normalized = re.sub(r"\s+", "-", text.strip())
        normalized = re.sub(r"[^\w\u4e00-\u9fff\-]+", "", normalized, flags=re.UNICODE)
        normalized = normalized.strip("-_")
        return (normalized or "qa-feedback")[:60]

    def _generate_qa_title(self, question: str, answer: str) -> str:
        fallback = question.strip().replace("\n", " ")[:24] or "知识纠错记录"
        try:
            from app.services.agent.agent_prompt_templates import PromptTemplateCatalog

            user_prompt = PromptTemplateCatalog.load_directory("config/prompt_templates").render(
                "session_title",
                {"question": question, "answer": answer},
                mode="quick",
            )
            completion = self.llm_client.chat.completions.create(
                model=self.chat_model,
                temperature=0.1,
                messages=[
                    {"role": "system", "content": "Generate a concise conversation title. Return title text only."},
                    {"role": "user", "content": user_prompt},
                ],
            )
            title = (completion.choices[0].message.content or "").strip().strip("“”\"'。")
            if title:
                return title[:24]
        except Exception as exc:
            logger.warning("Generate QA title from prompt catalog failed: %s", exc)
        try:
            completion = self.llm_client.chat.completions.create(
                model=self.chat_model,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": "你是标题生成助手。请基于问题与答案生成一个简洁中文标题，不超过24个字，不要使用引号和句号。"},
                    {"role": "user", "content": f"问题：{question}\n答案：{answer}"},
                ],
            )
            title = (completion.choices[0].message.content or "").strip().strip("“”\"'。")
            return title[:24] if title else fallback
        except Exception as exc:
            logger.warning("Generate QA title failed: %s", exc)
            return fallback

    def create_feedback_document(
        self,
        question: str,
        answer: str,
        scope: KnowledgeBaseScope | None = None,
    ) -> dict[str, Any]:
        scope = scope or self.default_scope
        if self.knowledge_base_service is not None:
            self.knowledge_base_service.assert_writable(scope)
        q = question.strip()
        a = answer.strip()
        if not q or not a:
            raise ValueError("Question and answer are required")
        title = self._generate_qa_title(q, a)
        self.feedback_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        feedback_root = (
            self.feedback_dir
            if scope.knowledge_base_id == self.default_scope.knowledge_base_id
            else self.feedback_dir / scope.knowledge_base_id
        )
        feedback_root.mkdir(parents=True, exist_ok=True)
        file_path = feedback_root / f"{timestamp}_{self._sanitize_filename(title)}.md"
        md = (
            f"# {title}\n\n## 问题\n{q}\n\n## 标准答案\n{a}\n\n## 元数据\n"
            f"- created_at: {datetime.now().isoformat(timespec='seconds')}\n"
            f"- source: user-feedback\n- workspace_id: {scope.workspace_id}\n"
            f"- knowledge_base_id: {scope.knowledge_base_id}\n"
        )
        file_path.write_text(md, encoding="utf-8")
        result = self.parse_and_index_document(file_path, scope=scope)
        self.audit_repository.create_feedback(
            scope,
            correction=a,
            rating="correction",
            metadata={"question": q, "source": result.get("source", "")},
        )
        return {"title": title, "source": result["source"], "chunks": result["indexed_chunks"]}

    def stream_answer(
        self,
        question: str,
        hits: list[dict[str, Any]] | None = None,
        conversation_context: dict[str, Any] | None = None,
        memory_context: str | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> Generator[str, None, None]:
        scope = scope or self.default_scope
        current_hits = hits if hits is not None else self.recall_parent_hits(
            self.hybrid_retrieve_hits(question, scope=scope),
            scope=scope,
        )
        context = self._build_context(current_hits)
        conversation_block = self._build_conversation_context(conversation_context or {})
        memory_block = (memory_context or "").strip()
        answer_guidance = self._build_answer_style_guidance(question, context)
        user_prompt = (
            "请基于下面的上下文回答用户问题。\n"
            "如果上下文信息不足，请明确指出。\n\n"
            f"上下文:\n{context if context else '(无检索结果)'}\n\n"
            f"问题: {question}"
        )
        user_prompt = (
            "请基于下面的上下文回答用户问题。\n"
            "如果上下文信息不足，请明确指出“无法确定”，不要编造没有来源的事实、命令、版本、URL 或参数。\n"
            "涉及多个候选对象时，每个对象的属性和结论必须由该对象自己的来源块共同支持；禁止跨对象或跨文档拼接属性。\n"
            "直接结束于有信息量的结论，不要添加“如果需要更多信息”“如需详细建议”“请提供更多信息”等客套式结尾。\n"
            f"{answer_guidance}"
            f"\n上下文:\n{context if context else '(无检索结果)'}\n\n"
            f"问题: {question}"
        )
        prompt_prefix = "".join(
            block + "\n\n"
            for block in [memory_block, conversation_block]
            if block
        )
        user_prompt = f"{prompt_prefix}{user_prompt}"
        user_prompt = self.context_prompt_catalog.render(
            self.context_template_id,
            query=question,
            language="zh-CN",
            contexts=context if context else "(no retrieval results)",
            conversation_context=self._build_conversation_context(conversation_context or {}),
            memory_context=(memory_context or "").strip(),
            knowledge_base_scope=scope.to_dict(),
            knowledge_bases=scope_to_prompt_kbs(scope, self.knowledge_base_service),
            answer_guidance=answer_guidance,
        )
        generation = get_observability_sink().start_generation(
            name="chat.completion.stream",
            model=self.chat_model,
            input={
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "hit_count": len(current_hits),
            },
            metadata={"scope": scope.to_dict(), "streaming": True},
            model_parameters={"temperature": 0.2},
        )
        output_parts: list[str] = []
        try:
            stream = self.llm_client.chat.completions.create(
                model=self.chat_model,
                stream=True,
                messages=[{"role": "system", "content": self.system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.2,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    output_parts.append(delta)
                    yield delta
        except Exception as exc:
            generation.finish(error=exc)
            raise
        else:
            generation.finish(output={"content": "".join(output_parts), "content_len": sum(len(item) for item in output_parts)})

    def _build_conversation_context(self, conversation_context: dict[str, Any]) -> str:
        if not conversation_context:
            return ""
        lines = ["[会话上下文]"]
        summary = str(conversation_context.get("summary", "")).strip()
        if summary:
            lines.append(f"摘要: {summary}")
        recent_messages = conversation_context.get("recent_messages", [])
        if recent_messages:
            lines.append("最近对话:")
            for message in recent_messages:
                role = message.get("role", "unknown")
                content = str(message.get("content", "")).strip()
                if content:
                    lines.append(f"{role}: {content}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def _build_answer_style_guidance(self, question: str, context: str) -> str:
        del question, context
        return (
            "\n回答要求:\n"
            "- 先理解用户要完成的任务，再自行选择最合适的 Markdown 结构；不要依赖关键词列表判断问题类型。\n"
            "- 先给直接结论。简单问题保持简短；步骤使用有序列表；多对象、多条件或对比任务优先使用表格。\n"
            "- 对筛选、比较或推荐任务，先从用户问题中完整抽取硬性条件，再逐个候选、逐个条件核验；只有全部硬性条件都有同一对象证据支持的候选才能进入结果。\n"
            "- 可以利用语言和领域知识理解同义词、别名、缩写、字段变体、符号、单位和阈值，但这些只是解释与检索假设，不能代替上下文证据。\n"
            "- 每个结论、属性和数值都必须能回溯到对应对象自己的上下文；禁止跨对象或跨来源拼接属性。\n"
            "- 当存在多个可行候选且证据足以区分其适用条件时，再给推荐建议并说明证据支持的理由；证据不足时明确写“根据提供的文档无法确定”。\n"
            "- 不要添加与问题无关的小节，不要生成空表格，也不要编造事实、命令、版本、URL、参数或推荐理由。\n"
        )
