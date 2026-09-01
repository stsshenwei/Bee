from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from app.models.document_models import Chunk
from app.models.knowledge_base import KnowledgeBaseScope
from app.services.agent.agent_prompt_templates import PromptTemplateCatalog, PromptTemplateError
from app.services.async_runtime.task_routes import SUMMARY_GENERATION_TASK
from app.services.infrastructure.logging_config import get_trace_id, trace_context

if TYPE_CHECKING:
    from app.services.async_runtime.queue import AsyncProcessingQueue
    from app.services.processing.processing_task_repository import ProcessingTaskRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocumentEnrichmentResult:
    summary: str
    keywords: list[str]
    suggested_questions: list[str]


class DocumentEnrichmentProvider(Protocol):
    model_ref: str

    def generate(
        self,
        document_name: str,
        content: str,
        *,
        partial: bool = False,
    ) -> DocumentEnrichmentResult: ...


class OpenAIDocumentEnrichmentProvider:
    def __init__(
        self,
        client: Any,
        model: str,
        max_summary_chars: int = 1200,
        *,
        prompt_catalog: PromptTemplateCatalog | None = None,
        template_id: str = "generate_summary",
    ):
        self.client = client
        self.model_ref = model
        self.max_summary_chars = max(200, max_summary_chars)
        self.prompt_catalog = prompt_catalog
        self.template_id = template_id

    def generate(
        self,
        document_name: str,
        content: str,
        *,
        partial: bool = False,
    ) -> DocumentEnrichmentResult:
        task = "生成分段概要" if partial else "生成文档概要、关键词和建议问题"
        response = self.client.chat.completions.create(
            model=self.model_ref,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是企业知识库文档后处理器，只能根据输入文本输出 JSON。"
                        "格式为 summary:string, keywords:string[], suggested_questions:string[]。"
                        "不得补充输入未提供的事实。"
                    ),
                },
                {"role": "user", "content": f"任务：{task}\n文档：{document_name}\n\n{content}"},
            ],
        )
        raw = str(response.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
            raise ValueError("Enrichment provider returned invalid JSON schema")
        return DocumentEnrichmentResult(
            summary=data["summary"].strip()[: self.max_summary_chars],
            keywords=_bounded_unique(data.get("keywords"), 12, 80),
            suggested_questions=_bounded_unique(data.get("suggested_questions"), 8, 200),
        )


class PromptBackedOpenAIDocumentEnrichmentProvider(OpenAIDocumentEnrichmentProvider):
    def generate(
        self,
        document_name: str,
        content: str,
        *,
        partial: bool = False,
    ) -> DocumentEnrichmentResult:
        task = "generate partial summary" if partial else "generate document summary, keywords, and suggested questions"
        if self.prompt_catalog is None:
            return super().generate(document_name, content, partial=partial)
        try:
            system_content = self.prompt_catalog.render(
                self.template_id,
                {"document_name": document_name, "content": content, "task": task},
                mode="postprocess",
            )
        except PromptTemplateError as exc:
            logger.warning("Summary prompt render failed, using built-in prompt: %s", exc)
            return super().generate(document_name, content, partial=partial)
        response = self.client.chat.completions.create(
            model=self.model_ref,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": document_name},
            ],
        )
        raw = str(response.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
            raise ValueError("Enrichment provider returned invalid JSON schema")
        return DocumentEnrichmentResult(
            summary=data["summary"].strip()[: self.max_summary_chars],
            keywords=_bounded_unique(data.get("keywords"), 12, 80),
            suggested_questions=_bounded_unique(data.get("suggested_questions"), 8, 200),
        )


class DocumentEnrichmentService:
    def __init__(
        self,
        repository: Any,
        provider: DocumentEnrichmentProvider | None,
        *,
        enabled: bool = False,
        max_batch_tokens: int = 6000,
        max_retries: int = 2,
        asynchronous: bool = True,
        processing_repository: "ProcessingTaskRepository | None" = None,
        async_queue: "AsyncProcessingQueue | None" = None,
    ):
        self.repository = repository
        self.provider = provider
        self.enabled = enabled
        self.max_batch_tokens = max(500, max_batch_tokens)
        self.max_retries = max(0, max_retries)
        self.asynchronous = asynchronous
        self.processing_repository = processing_repository
        self.async_queue = async_queue
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="document-enrichment") if asynchronous else None
        self._futures: dict[str, Future] = {}

    def enqueue(self, doc_id: str, chunks: list[Chunk], scope: KnowledgeBaseScope) -> Future | None:
        if not self.enabled or self.provider is None:
            self.repository.update_enrichment(doc_id, scope, status="none", error="")
            return None
        parent_ids = [chunk.id for chunk in chunks if chunk.chunk_type == "parent"]
        task = self.repository.create_enrichment_task(
            doc_id,
            scope,
            provider_ref=self.provider.model_ref,
            source_chunk_ids=parent_ids,
        )
        task_id = str(task["id"])
        if self.processing_repository is not None and self.async_queue is not None and self.async_queue.enabled:
            processing_task = self.processing_repository.create_task(
                SUMMARY_GENERATION_TASK,
                scope,
                document_id=doc_id,
                payload={
                    "schema_version": 1,
                    "doc_id": doc_id,
                    "enrichment_task_id": task_id,
                    "source_chunk_ids": parent_ids,
                },
                max_attempts=max(1, self.max_retries + 1),
                trace_id=get_trace_id(),
                idempotency_key=f"summary-generation:{scope.knowledge_base_id}:{doc_id}:{task_id}",
                source_revision=",".join(parent_ids),
            )
            dispatch = self.async_queue.enqueue(processing_task)
            record_dispatch = getattr(self.processing_repository, "record_broker_dispatch", None)
            if callable(record_dispatch):
                record_dispatch(processing_task["id"], broker_task_id=dispatch.broker_task_id, queue_name=dispatch.queue)
            return None
        if self._executor is None:
            self._run(task_id, doc_id, chunks, scope)
            return None
        trace_id = get_trace_id()
        future = self._executor.submit(self._run_with_trace, trace_id, task_id, doc_id, list(chunks), scope)
        self._futures[doc_id] = future
        return future

    def process_task(self, task: dict[str, Any]) -> None:
        payload = dict(task.get("payload") or {})
        doc_id = str(task.get("document_id") or payload.get("doc_id") or "")
        enrichment_task_id = str(payload.get("enrichment_task_id") or "")
        if not doc_id:
            raise ValueError("summary.generation task is missing document_id")
        if not enrichment_task_id:
            raise ValueError("summary.generation task is missing enrichment_task_id")
        scope = KnowledgeBaseScope(
            workspace_id=str(task["workspace_id"]),
            selected_knowledge_base_ids=(str(task["knowledge_base_id"]),),
        )
        chunks = [self._chunk_from_row(row) for row in self.repository.list_chunks(doc_id=doc_id, scope=scope)]
        self._run(enrichment_task_id, doc_id, chunks, scope, raise_on_failure=True)

    def _run_with_trace(self, trace_id: str, task_id: str, doc_id: str, chunks: list[Chunk], scope: KnowledgeBaseScope) -> None:
        with trace_context(trace_id):
            self._run(task_id, doc_id, chunks, scope)

    def retry(self, doc_id: str, scope: KnowledgeBaseScope) -> Future | None:
        document = self.repository.get_document(doc_id, scope)
        if document is None:
            raise KeyError(doc_id)
        if int(document.get("summary_version", 0) or 0) > self.max_retries:
            raise ValueError("Document enrichment retry limit reached")
        chunks = [self._chunk_from_row(row) for row in self.repository.list_chunks(doc_id=doc_id, scope=scope)]
        return self.enqueue(doc_id, chunks, scope)

    def wait(self, doc_id: str, timeout: float = 10.0) -> None:
        future = self._futures.get(doc_id)
        if future is not None:
            future.result(timeout=timeout)

    def _run(
        self,
        task_id: str,
        doc_id: str,
        chunks: list[Chunk],
        scope: KnowledgeBaseScope,
        *,
        raise_on_failure: bool = False,
    ) -> None:
        started = time.monotonic()
        logger.info(
            "document.enrichment.start",
            extra={"workspace_id": scope.workspace_id, "knowledge_base_id": scope.knowledge_base_id, "doc_id": doc_id, "task_id": task_id},
        )
        try:
            document = self.repository.get_document(doc_id, scope)
            if document is None:
                raise KeyError(doc_id)
            self.repository.update_enrichment_task(task_id, scope, status="processing")
            self.repository.update_enrichment(doc_id, scope, status="processing", error="", current_task_id=task_id)
            parents = [chunk for chunk in chunks if chunk.chunk_type == "parent"]
            if not parents:
                raise ValueError("Document has no parent chunks for enrichment")
            batches = self._batch_parent_chunks(parents)
            partials = []
            for batch in batches:
                text = "\n\n".join(chunk.content_markdown or chunk.content for chunk in batch)
                partials.append(
                    self.provider.generate(
                        str(document.get("name", doc_id)), text, partial=len(batches) > 1
                    )
                )
            if len(partials) == 1:
                result = partials[0]
            else:
                combined = "\n\n".join(f"分段 {index}: {item.summary}" for index, item in enumerate(partials, 1))
                result = self.provider.generate(str(document.get("name", doc_id)), combined, partial=False)
            source_chunk_ids = [chunk.id for chunk in parents]
            self.repository.update_enrichment(
                doc_id,
                scope,
                status="completed",
                summary=result.summary,
                keywords=_bounded_unique(result.keywords, 12, 80),
                suggested_questions=_bounded_unique(result.suggested_questions, 8, 200),
                error="",
                model_ref=self.provider.model_ref,
                generated_at=datetime.now().isoformat(timespec="seconds"),
                source_chunk_ids=source_chunk_ids,
                current_task_id=task_id,
            )
            self.repository.update_enrichment_task(task_id, scope, status="completed")
            logger.info(
                "document.enrichment.end",
                extra={
                    "workspace_id": scope.workspace_id,
                    "knowledge_base_id": scope.knowledge_base_id,
                    "doc_id": doc_id,
                    "task_id": task_id,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
        except Exception as exc:
            logger.exception(
                "document.enrichment.failed",
                extra={
                    "workspace_id": scope.workspace_id,
                    "knowledge_base_id": scope.knowledge_base_id,
                    "doc_id": doc_id,
                    "task_id": task_id,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                },
            )
            sanitized = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", str(exc))
            sanitized = re.sub(
                r"(?i)(api[_-]?key|authorization)\s*[:=]\s*\S+",
                r"\1=[redacted]",
                sanitized,
            )[:1000]
            try:
                self.repository.update_enrichment(doc_id, scope, status="failed", error=sanitized)
                self.repository.update_enrichment_task(
                    task_id, scope, status="failed", error_message=sanitized
                )
            except Exception:
                logger.exception("Failed to persist document enrichment failure for %s", doc_id)
            if raise_on_failure:
                raise

    def _batch_parent_chunks(self, chunks: list[Chunk]) -> list[list[Chunk]]:
        batches: list[list[Chunk]] = []
        current: list[Chunk] = []
        current_tokens = 0
        for chunk in chunks:
            chunk_tokens = max(1, int(chunk.token_count or len(chunk.content) // 4 or 1))
            if current and current_tokens + chunk_tokens > self.max_batch_tokens:
                batches.append(current)
                current = []
                current_tokens = 0
            current.append(chunk)
            current_tokens += chunk_tokens
        if current:
            batches.append(current)
        return batches

    def _chunk_from_row(self, row: dict[str, Any]) -> Chunk:
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


def _bounded_unique(value: Any, limit: int, max_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item).strip()[:max_chars]
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result
