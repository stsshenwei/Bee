from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from app.models.document_models import Chunk
from app.models.knowledge_base import KnowledgeBaseScope
from app.models.processing_config import DurableProcessingWorkerConfig
from app.services.async_runtime.task_routes import (
    CHUNK_EXTRACT_TASK,
    EMBEDDING_INDEX_TASK,
    GENERATED_QUESTIONS_TASK,
    GRAPH_EXTRACTION_TASK,
    IMAGE_MULTIMODAL_TASK,
    KNOWLEDGE_POSTPROCESS_TASK,
    SUMMARY_GENERATION_TASK,
    UPLOAD_FILE_TASK,
    WIKI_FINALIZE_TASK,
    WIKI_INGEST_TASK,
)
from app.services.infrastructure.logging_config import get_trace_id, trace_context
from app.services.infrastructure.observability import use_observability_trace
from app.services.processing.processing_task_repository import ProcessingTaskRepository

if TYPE_CHECKING:
    from app.services.retrieval.rag_service import RAGService
    from app.services.async_runtime.queue import AsyncProcessingQueue


logger = logging.getLogger(__name__)


class DocumentProcessingWorker:
    def __init__(
        self,
        *,
        repository: ProcessingTaskRepository,
        rag_service: "RAGService",
        config: DurableProcessingWorkerConfig | None = None,
        worker_id: str | None = None,
        wiki_ingest_service: Any | None = None,
        async_queue: "AsyncProcessingQueue | None" = None,
        start_local_worker: bool = True,
    ):
        self.repository = repository
        self.rag_service = rag_service
        self.config = config or DurableProcessingWorkerConfig()
        self.worker_id = worker_id or f"worker-{uuid4().hex[:12]}"
        self.wiki_ingest_service = wiki_ingest_service
        self.async_queue = async_queue
        self.start_local_worker = bool(start_local_worker)
        self._handlers: dict[str, Callable[[dict[str, Any]], None]] = {
            UPLOAD_FILE_TASK: self._process_upload_file_task,
        }
        if wiki_ingest_service is not None:
            self._handlers[WIKI_INGEST_TASK] = wiki_ingest_service.process_ingest_task
            self._handlers[WIKI_FINALIZE_TASK] = wiki_ingest_service.process_finalize_task
        enrichment_service = getattr(rag_service, "document_enrichment_service", None)
        if enrichment_service is not None and callable(getattr(enrichment_service, "process_task", None)):
            self._handlers[SUMMARY_GENERATION_TASK] = enrichment_service.process_task
            self._handlers[GENERATED_QUESTIONS_TASK] = enrichment_service.process_task
        self._handlers[CHUNK_EXTRACT_TASK] = self._process_document_stage_task
        self._handlers[EMBEDDING_INDEX_TASK] = self._process_document_stage_task
        self._handlers[IMAGE_MULTIMODAL_TASK] = self._process_document_stage_task
        self._handlers[KNOWLEDGE_POSTPROCESS_TASK] = self._process_postprocess_stage_task
        self._handlers[GRAPH_EXTRACTION_TASK] = self._process_document_stage_task
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def enqueue_upload_batch(self, batch_id: str, scope: KnowledgeBaseScope) -> list[dict[str, Any]]:
        batch = self.rag_service.upload_batch_repository.get_batch(batch_id, scope)
        tasks: list[dict[str, Any]] = []
        for file_task in batch.get("files", []):
            if str(file_task.get("status", "")) in {"completed", "canceled"}:
                continue
            document_id = str(file_task.get("document_id") or "")
            task = self.repository.create_task(
                UPLOAD_FILE_TASK,
                scope,
                payload={
                    "batch_id": batch_id,
                    "file_id": file_task.get("id"),
                    "storage_path": file_task.get("storage_path"),
                    "original_name": file_task.get("original_name"),
                },
                document_id=document_id,
                upload_batch_id=batch_id,
                upload_file_id=str(file_task.get("id") or ""),
                max_attempts=self.config.default_max_attempts,
                trace_id=get_trace_id(),
            )
            if self.async_queue is not None and self.async_queue.enabled:
                dispatch = self.async_queue.enqueue(task)
                record_dispatch = getattr(self.repository, "record_broker_dispatch", None)
                if callable(record_dispatch):
                    task = record_dispatch(task["id"], broker_task_id=dispatch.broker_task_id, queue_name=dispatch.queue)
                task = {**task, "broker_task_id": dispatch.broker_task_id, "queue_name": dispatch.queue}
            tasks.append(task)
        logger.info(
            "processing_worker.enqueue_upload_batch",
            extra={
                "workspace_id": scope.workspace_id,
                "knowledge_base_id": scope.knowledge_base_id,
                "batch_id": batch_id,
                "tasks": len(tasks),
            },
        )
        return tasks

    def start(self) -> None:
        if not self.enabled:
            return
        if not self.start_local_worker:
            logger.info("processing_worker.local_worker_disabled", extra={"worker_id": self.worker_id})
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self.run_forever, name=self.worker_id, daemon=True)
        self._thread.start()
        logger.info("processing_worker.start", extra={"worker_id": self.worker_id})

    def register_handler(self, task_type: str, handler: Callable[[dict[str, Any]], None]) -> None:
        clean_type = str(task_type or "").strip()
        if not clean_type:
            raise ValueError("task_type cannot be empty")
        self._handlers[clean_type] = handler

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        logger.info("processing_worker.stop", extra={"worker_id": self.worker_id})

    def run_forever(self) -> None:
        while not self._stop_event.is_set():
            processed = self.run_once()
            if not processed:
                self._stop_event.wait(self.config.poll_interval_seconds)

    def run_once(self) -> bool:
        task = self.repository.claim_next(
            self.worker_id,
            lease_seconds=self.config.lease_timeout_seconds,
            task_types=set(self._handlers),
        )
        if task is None:
            return False
        self._process_claimed_task(task)
        return True

    def _process_claimed_task(self, task: dict[str, Any]) -> None:
        task_trace_id = str(task.get("trace_id") or "")
        lease_worker_id = str(task.get("lease_owner") or self.worker_id)
        task_id = str(task["id"])
        with trace_context(task_trace_id or None):
            with use_observability_trace(task_trace_id or None, name=f"processing_worker.{task.get('task_type')}"):
                logger.info(
                    "processing_worker.task.start",
                    extra={
                        "worker_id": lease_worker_id,
                        "task_id": task_id,
                        "task_type": task.get("task_type"),
                        "attempt": task.get("attempt"),
                    },
                )
                heartbeat_stop, heartbeat_thread = self._start_task_heartbeat(task_id, lease_worker_id)
                try:
                    handler = self._handlers.get(str(task.get("task_type") or ""))
                    if handler is None:
                        raise ValueError(f"Unsupported processing task type: {task['task_type']}")
                    handler(task)
                    self.repository.complete(task_id, worker_id=lease_worker_id)
                    logger.info("processing_worker.task.completed", extra={"worker_id": lease_worker_id, "task_id": task_id})
                except ProcessingTaskCanceled as exc:
                    self.repository.cancel_task(task_id, reason=str(exc))
                    logger.info("processing_worker.task.canceled", extra={"worker_id": lease_worker_id, "task_id": task_id})
                except Exception as exc:
                    if exc.__class__.__name__ in {"WikiSupersededError", "StaleDocumentRevision"}:
                        self.repository.cancel_task(task_id, reason=str(exc))
                        if self.wiki_ingest_service is not None:
                            self.wiki_ingest_service.reconcile_cancelled(task, str(exc))
                        logger.info("processing_worker.task.superseded", extra={"worker_id": lease_worker_id, "task_id": task_id})
                    else:
                        self._handle_task_failure(task, exc, worker_id=lease_worker_id)
                finally:
                    heartbeat_stop.set()
                    heartbeat_thread.join(timeout=1.0)

    def _start_task_heartbeat(self, task_id: str, worker_id: str) -> tuple[threading.Event, threading.Thread]:
        stop_event = threading.Event()
        interval = max(5.0, min(30.0, float(self.config.lease_timeout_seconds) / 3.0))

        def heartbeat_loop() -> None:
            while not stop_event.wait(interval):
                try:
                    self.repository.heartbeat(
                        task_id,
                        worker_id,
                        lease_seconds=self.config.lease_timeout_seconds,
                    )
                except Exception as exc:
                    logger.warning(
                        "processing_worker.task.heartbeat_failed",
                        extra={
                            "worker_id": worker_id,
                            "task_id": task_id,
                            "error_type": exc.__class__.__name__,
                            "error_message": str(exc),
                        },
                    )
                    return

        thread = threading.Thread(target=heartbeat_loop, name=f"{worker_id}-heartbeat", daemon=True)
        thread.start()
        return stop_event, thread

    def _process_upload_file_task(self, task: dict[str, Any]) -> None:
        scope = KnowledgeBaseScope(
            workspace_id=str(task["workspace_id"]),
            selected_knowledge_base_ids=(str(task["knowledge_base_id"]),),
        )
        payload = task.get("payload") or {}
        batch_id = str(task.get("upload_batch_id") or payload.get("batch_id") or "")
        file_id = str(task.get("upload_file_id") or payload.get("file_id") or "")
        file_task = self.rag_service.upload_batch_repository.get_file(file_id, scope)
        self._ensure_upload_file_active(file_id, batch_id, scope)
        if self.async_queue is not None and self.async_queue.enabled:
            staged = self.rag_service.enqueue_upload_file_chunk_stage(file_task, scope)
            if staged is None:
                raise RuntimeError("Failed to enqueue chunk extraction task")
            return

        self.rag_service._process_upload_file(
            file_task,
            scope,
            cancel_check=lambda: self._ensure_upload_file_active(file_id, batch_id, scope),
        )
        updated_file = self.rag_service.upload_batch_repository.get_file(file_id, scope)
        if str(updated_file.get("status")) == "failed":
            raise RuntimeError(str(updated_file.get("error_message") or "Upload file processing failed"))
        self._ensure_upload_file_active(file_id, batch_id, scope)
        self.rag_service._finish_upload_batch_from_files(batch_id, scope)

    def _process_document_stage_task(self, task: dict[str, Any]) -> None:
        payload = dict(task.get("payload") or {})
        file_id = str(task.get("upload_file_id") or payload.get("file_id") or "")
        task_type = str(task.get("task_type") or "")
        if file_id and task_type == UPLOAD_FILE_TASK:
            self._process_upload_file_task(task)
            return
        if task_type == CHUNK_EXTRACT_TASK:
            scope = KnowledgeBaseScope(
                workspace_id=str(task["workspace_id"]),
                selected_knowledge_base_ids=(str(task["knowledge_base_id"]),),
            )
            file_id = str(task.get("upload_file_id") or payload.get("file_id") or "")
            batch_id = str(task.get("upload_batch_id") or payload.get("batch_id") or "")
            self.rag_service.process_chunk_extract_task(
                task,
                scope,
                cancel_check=(lambda: self._ensure_upload_file_active(file_id, batch_id, scope)) if file_id and batch_id else None,
            )
            return
        if task_type == EMBEDDING_INDEX_TASK:
            scope = KnowledgeBaseScope(
                workspace_id=str(task["workspace_id"]),
                selected_knowledge_base_ids=(str(task["knowledge_base_id"]),),
            )
            file_id = str(task.get("upload_file_id") or payload.get("file_id") or "")
            batch_id = str(task.get("upload_batch_id") or payload.get("batch_id") or "")
            self.rag_service.process_embedding_index_task(
                task,
                scope,
                cancel_check=(lambda: self._ensure_upload_file_active(file_id, batch_id, scope)) if file_id and batch_id else None,
            )
            return
        if task_type == SUMMARY_GENERATION_TASK:
            enrichment_service = getattr(self.rag_service, "document_enrichment_service", None)
            if enrichment_service is None or not callable(getattr(enrichment_service, "process_task", None)):
                raise ValueError("Document enrichment service is not configured")
            enrichment_service.process_task(task)
            return
        if task_type == GRAPH_EXTRACTION_TASK:
            self._process_graph_stage_task(task)
            return
        if task_type == IMAGE_MULTIMODAL_TASK:
            self._process_multimodal_stage_task(task)
            return
        raise ValueError(f"Unsupported staged document task without upload file payload: {task_type}")

    def _process_graph_stage_task(self, task: dict[str, Any]) -> None:
        scope = KnowledgeBaseScope(
            workspace_id=str(task["workspace_id"]),
            selected_knowledge_base_ids=(str(task["knowledge_base_id"]),),
        )
        doc_id = str(task.get("document_id") or (task.get("payload") or {}).get("doc_id") or "")
        if not doc_id:
            raise ValueError("graph.extraction task is missing document_id")
        chunks = [_chunk_from_row(row) for row in self.rag_service.document_repository.list_chunks(doc_id=doc_id, scope=scope)]
        self.rag_service._run_kg_enrichment(doc_id, chunks, scope)

    def _process_multimodal_stage_task(self, task: dict[str, Any]) -> None:
        scope = KnowledgeBaseScope(
            workspace_id=str(task["workspace_id"]),
            selected_knowledge_base_ids=(str(task["knowledge_base_id"]),),
        )
        doc_id = str(task.get("document_id") or (task.get("payload") or {}).get("doc_id") or "")
        if not doc_id:
            raise ValueError("image.multimodal task is missing document_id")
        self.rag_service.process_multimodal_operations(doc_id, scope)

    def _process_postprocess_stage_task(self, task: dict[str, Any]) -> None:
        payload = dict(task.get("payload") or {})
        scope = KnowledgeBaseScope(
            workspace_id=str(task["workspace_id"]),
            selected_knowledge_base_ids=(str(task["knowledge_base_id"]),),
        )
        doc_id = str(task.get("document_id") or payload.get("doc_id") or "")
        if not doc_id:
            raise ValueError("knowledge.post_process task is missing document_id")
        chunks = [_chunk_from_row(row) for row in self.rag_service.document_repository.list_chunks(doc_id=doc_id, scope=scope)]
        graph_enabled = bool(payload.get("graph_enabled", False))
        wiki_enabled = bool(payload.get("wiki_enabled", False))
        batch_id = str(task.get("upload_batch_id") or payload.get("batch_id") or "")
        file_id = str(task.get("upload_file_id") or payload.get("file_id") or "")
        if graph_enabled:
            graph_task = self.rag_service._enqueue_async_document_stage(
                GRAPH_EXTRACTION_TASK,
                doc_id,
                scope,
                payload={"batch_id": batch_id, "file_id": file_id, "chunks": len(chunks)},
                stage="postprocess",
                source_revision=str(task.get("source_revision") or ""),
            )
            if graph_task is None:
                self.rag_service._run_kg_enrichment(doc_id, chunks, scope)
        if wiki_enabled:
            self.rag_service._run_wiki_generation(doc_id, scope)
        enrichment_service = getattr(self.rag_service, "document_enrichment_service", None)
        if enrichment_service is not None:
            enrichment_service.enqueue(doc_id, chunks, scope)

    def _ensure_upload_file_active(self, file_id: str, batch_id: str, scope: KnowledgeBaseScope) -> None:
        file_task = self.rag_service.upload_batch_repository.get_file(file_id, scope)
        if str(file_task.get("status")) == "canceled":
            raise ProcessingTaskCanceled(f"Upload file {file_id} was canceled")
        batch = self.rag_service.upload_batch_repository.get_batch(batch_id, scope)
        if str(batch.get("status")) == "canceled":
            raise ProcessingTaskCanceled(f"Upload batch {batch_id} was canceled")

    def _handle_task_failure(self, task: dict[str, Any], exc: Exception, *, worker_id: str | None = None) -> None:
        task_id = str(task["id"])
        active_worker_id = worker_id or self.worker_id
        attempt = int(task.get("attempt", 0) or 0)
        max_attempts = int(task.get("max_attempts", self.config.default_max_attempts) or self.config.default_max_attempts)
        error_code = exc.__class__.__name__
        error_message = str(exc)
        if attempt < max_attempts:
            delay = _retry_delay_seconds(exc, self.config.retry_delay_for_attempt(attempt))
            self.repository.retry(
                task_id,
                error_code=error_code,
                error_message=error_message,
                delay_seconds=delay,
                worker_id=active_worker_id,
            )
            logger.warning(
                "processing_worker.task.retry",
                extra={
                    "worker_id": active_worker_id,
                    "task_id": task_id,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "delay_seconds": delay,
                    "error_type": error_code,
                    "error_message": error_message,
                },
            )
            return
        self.repository.dead_letter(task_id, error_code=error_code, error_message=error_message, worker_id=active_worker_id)
        if task.get("task_type") == UPLOAD_FILE_TASK:
            self._reconcile_failed_upload_task(task, error_message)
        elif self.wiki_ingest_service is not None:
            self.wiki_ingest_service.reconcile_terminal_failure(task, error_message)
        logger.exception(
            "processing_worker.task.dead_lettered",
            extra={
                "worker_id": active_worker_id,
                "task_id": task_id,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "error_type": error_code,
                "error_message": error_message,
            },
        )

    def _reconcile_failed_upload_task(self, task: dict[str, Any], error_message: str) -> None:
        try:
            scope = KnowledgeBaseScope(
                workspace_id=str(task["workspace_id"]),
                selected_knowledge_base_ids=(str(task["knowledge_base_id"]),),
            )
            file_id = str(task.get("upload_file_id") or "")
            batch_id = str(task.get("upload_batch_id") or "")
            if file_id:
                self.rag_service.upload_batch_repository.update_file(
                    file_id,
                    scope,
                    status="failed",
                    error_message=error_message,
                    retry_eligible=True,
                )
            if batch_id:
                self.rag_service._finish_upload_batch_from_files(batch_id, scope)
        except Exception:
            logger.exception("processing_worker.task.reconcile_failed_upload_failed", extra={"task_id": task.get("id")})


class ProcessingTaskCanceled(RuntimeError):
    pass


def _retry_delay_seconds(exc: Exception, configured_delay: int) -> int:
    candidates: list[Any] = [
        getattr(exc, "retry_after_seconds", None),
        getattr(exc, "retry_after", None),
    ]
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            candidates.append(headers.get("retry-after"))
        except Exception:
            pass
    retry_after = 0
    for value in candidates:
        try:
            retry_after = max(retry_after, int(float(value)))
        except (TypeError, ValueError):
            continue
    return min(3600, max(0, int(configured_delay), retry_after))


def _chunk_from_row(row: dict[str, Any]) -> Chunk:
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


def drain_worker(worker: DocumentProcessingWorker, *, limit: int = 100) -> int:
    processed = 0
    while processed < limit and worker.run_once():
        processed += 1
        time.sleep(0)
    return processed
