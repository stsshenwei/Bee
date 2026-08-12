from __future__ import annotations

import json
import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from time import perf_counter, sleep
from typing import Any, Iterator
from uuid import uuid4

from app.models.knowledge_base import KnowledgeBaseScope
from app.services.agent.agent_prompt_templates import PromptTemplateCatalog, PromptTemplateError
from app.services.processing.processing_span_tracker import ProcessingSpan, ProcessingSpanTracker, SPAN_STAGE
from app.services.processing.processing_task_repository import ProcessingTaskRepository
from app.services.processing.processing_worker import WIKI_FINALIZE_TASK, WIKI_INGEST_TASK
from app.services.wiki.wiki_repository import WikiRepository, normalize_wiki_slug
from app.services.wiki.wiki_service import WikiPageService, WikiValidationError


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WikiIngestConfig:
    enabled: bool = True
    debounce_seconds: int = 30
    followup_seconds: int = 5
    batch_size: int = 5
    map_concurrency: int = 4
    reduce_concurrency: int = 4
    max_source_chunks: int = 80
    max_source_chars: int = 32768
    max_candidates: int = 24
    max_pages_per_document: int = 30
    min_source_chars: int = 80
    timeout_seconds: int = 3600
    max_attempts: int = 3
    llm_temperature: float = 0.3
    llm_max_attempts: int = 3
    language: str = "zh-CN"


@dataclass(frozen=True)
class WikiIngestPayload:
    document_id: str
    document_revision: str
    generation_run_id: str
    generation_task_id: str
    root_trace_id: str = ""
    schema_version: int = 1

    @classmethod
    def from_task(cls, task: dict[str, Any]) -> "WikiIngestPayload":
        payload = dict(task.get("payload") or {})
        schema_version = int(payload.get("schema_version") or task.get("payload_schema_version") or 0)
        if schema_version != 1:
            raise WikiValidationError(f"Unsupported wiki.ingest payload schema_version: {schema_version}")
        document_id = _required_payload_text(payload.get("document_id") or task.get("document_id"), "document_id")
        document_revision = _required_payload_text(
            payload.get("document_revision") or task.get("source_revision"), "document_revision"
        )
        generation_run_id = _required_payload_text(
            payload.get("generation_run_id") or task.get("upload_batch_id") or task.get("id"),
            "generation_run_id",
        )
        return cls(
            document_id=document_id,
            document_revision=document_revision,
            generation_run_id=generation_run_id,
            generation_task_id=str(payload.get("generation_task_id") or "").strip(),
            root_trace_id=str(payload.get("root_trace_id") or task.get("parent_trace_id") or "").strip(),
            schema_version=schema_version,
        )


@dataclass(frozen=True)
class WikiFinalizePayload:
    generation_run_id: str
    generation_task_ids: tuple[str, ...]
    document_ids: tuple[str, ...]
    affected_slugs: tuple[str, ...]
    schema_version: int = 1

    @classmethod
    def from_task(cls, task: dict[str, Any]) -> "WikiFinalizePayload":
        payload = dict(task.get("payload") or {})
        schema_version = int(payload.get("schema_version") or task.get("payload_schema_version") or 0)
        if schema_version != 1:
            raise WikiValidationError(f"Unsupported wiki.finalize payload schema_version: {schema_version}")
        generation_run_id = _required_payload_text(
            payload.get("generation_run_id") or task.get("upload_batch_id") or task.get("id"),
            "generation_run_id",
        )
        return cls(
            generation_run_id=generation_run_id,
            generation_task_ids=tuple(str(value).strip() for value in payload.get("generation_task_ids") or [] if str(value).strip()),
            document_ids=tuple(str(value).strip() for value in payload.get("document_ids") or [] if str(value).strip()),
            affected_slugs=tuple(normalize_wiki_slug(str(value)) for value in payload.get("affected_slugs") or [] if str(value).strip()),
            schema_version=schema_version,
        )


@dataclass(frozen=True)
class WikiCandidateOutput:
    title: str
    slug: str
    page_type: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    details: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "WikiCandidateOutput":
        if not isinstance(value, dict):
            raise WikiValidationError("Wiki candidate must be an object")
        title = re.sub(r"\s+", " ", str(value.get("title") or "").strip())[:160]
        page_type = str(value.get("page_type") or "concept").strip().lower()
        slug = normalize_wiki_slug(str(value.get("slug") or title))
        if not title or page_type not in {"entity", "concept"}:
            raise WikiValidationError("Wiki candidate requires a title and entity/concept page_type")
        aliases = tuple(
            dict.fromkeys(str(alias).strip()[:160] for alias in value.get("aliases") or [] if str(alias).strip())
        )[:8]
        return cls(
            title=title,
            slug=slug,
            page_type=page_type,
            aliases=aliases,
            description=str(value.get("description") or value.get("reason") or "").strip()[:500],
            details=str(value.get("details") or "").strip()[:1200],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "slug": self.slug,
            "page_type": self.page_type,
            "aliases": list(self.aliases),
            "description": self.description,
            "details": self.details,
        }


@dataclass(frozen=True)
class WikiSummaryOutput:
    summary: str
    content_markdown: str

    @classmethod
    def from_value(cls, value: Any) -> "WikiSummaryOutput":
        value = value if isinstance(value, dict) else {}
        return cls(
            summary=str(value.get("summary") or "").strip()[:1200],
            content_markdown=str(value.get("content_markdown") or "").strip(),
        )


@dataclass(frozen=True)
class WikiClassificationOutput:
    slug: str
    chunk_ids: tuple[str, ...]
    summary: str = ""

    @classmethod
    def from_value(
        cls,
        value: Any,
        *,
        valid_slugs: set[str],
        valid_chunk_ids: set[str],
    ) -> "WikiClassificationOutput":
        if not isinstance(value, dict):
            raise WikiValidationError("Wiki classification must be an object")
        slug = normalize_wiki_slug(str(value.get("slug") or ""))
        chunk_ids = tuple(
            dict.fromkeys(str(chunk_id) for chunk_id in value.get("chunk_ids") or [] if str(chunk_id) in valid_chunk_ids)
        )
        if slug not in valid_slugs or not chunk_ids:
            raise WikiValidationError("Wiki classification references an unknown slug or chunk")
        return cls(slug=slug, chunk_ids=chunk_ids, summary=str(value.get("summary") or "").strip()[:1200])

    def to_dict(self) -> dict[str, Any]:
        return {"slug": self.slug, "chunk_ids": list(self.chunk_ids), "summary": self.summary}


class WikiIngestService:
    SOURCE_TYPES = {"parent", "table", "ocr", "image_ocr", "image_caption"}

    def __init__(
        self,
        *,
        repository: WikiRepository,
        page_service: WikiPageService,
        processing_repository: ProcessingTaskRepository,
        llm_client: Any | None,
        model: str,
        prompt_catalog: PromptTemplateCatalog,
        config: WikiIngestConfig | None = None,
        span_tracker: ProcessingSpanTracker | None = None,
    ):
        self.repository = repository
        self.page_service = page_service
        self.document_repository = page_service.document_repository
        self.processing_repository = processing_repository
        self.llm_client = llm_client
        self.model = model
        self.prompt_catalog = prompt_catalog
        self.config = config or WikiIngestConfig()
        self.span_tracker = span_tracker or ProcessingSpanTracker.disabled()
        self._page_lock_guard = Lock()
        self._page_locks: dict[tuple[str, str], Lock] = {}
        self._llm_metrics: dict[str, dict[str, Any]] = {}
        self._source_truncated = False

    def enqueue_document(
        self,
        scope: KnowledgeBaseScope,
        document_id: str,
        *,
        trace_id: str = "",
        debounce_seconds: int | None = None,
    ) -> dict[str, Any]:
        knowledge_base = self.page_service._assert_wiki_capable(scope)
        if not self.config.enabled or not knowledge_base.indexing_strategy.wiki_generation_enabled:
            generation = self.repository.create_generation_task(
                scope,
                doc_id=document_id,
                config={"enabled": False, "reason": "wiki_generation_disabled"},
            )
            generation = self.repository.update_generation_task(
                scope, generation.id, status="skipped", error_message="Wiki generation is disabled"
            )
            return {"task": generation.to_dict(), "processing_task": None, "page": None, "proposal": None}
        document = self._required_document(scope, document_id)
        revision = self._document_revision(document)
        idempotency_key = f"wiki-ingest:{scope.knowledge_base_id}:{document_id}:{revision}"
        existing_task = self.processing_repository.find_by_idempotency(scope, WIKI_INGEST_TASK, idempotency_key)
        if existing_task is not None:
            existing_generation_id = str((existing_task.get("payload") or {}).get("generation_task_id") or "")
            existing_generation = (
                self.repository.get_generation_task(scope, existing_generation_id)
                if existing_generation_id
                else None
            )
            return {
                "task": existing_generation.to_dict() if existing_generation is not None else None,
                "processing_task": existing_task,
                "page": None,
                "proposal": None,
            }
        generation_run_id = f"wiki-run-{uuid4().hex}"
        generation = self.repository.create_generation_task(
            scope,
            doc_id=document_id,
            config={
                "enabled": True,
                "generation_run_id": generation_run_id,
                "document_revision": revision,
                "model": self.model,
                "pipeline": "map_reduce_v1",
            },
        )
        generation = self.repository.update_generation_task(scope, generation.id, status="queued")
        delay = self.config.debounce_seconds if debounce_seconds is None else max(0, int(debounce_seconds))
        available_at = _iso_after(delay)
        payload = {
            "schema_version": 1,
            "document_id": document_id,
            "document_revision": revision,
            "generation_run_id": generation_run_id,
            "generation_task_id": generation.id,
            "root_trace_id": trace_id,
        }
        processing_task = self.processing_repository.create_task(
            WIKI_INGEST_TASK,
            scope,
            payload=payload,
            document_id=document_id,
            upload_batch_id=generation_run_id,
            max_attempts=self.config.max_attempts,
            run_after=available_at,
            trace_id=trace_id,
            payload_schema_version=1,
            idempotency_key=idempotency_key,
            source_revision=revision,
            parent_trace_id=trace_id,
        )
        self.repository.enqueue_pending(
            scope,
            document_id=document_id,
            document_revision=revision,
            task_id=str(processing_task.get("id") or ""),
            available_at=available_at,
        )
        return {
            "task": generation.to_dict(),
            "processing_task": processing_task,
            "page": None,
            "proposal": None,
        }

    def process_ingest_task(self, task: dict[str, Any]) -> None:
        scope = _task_scope(task)
        payload = WikiIngestPayload.from_task(task)
        document_id = payload.document_id
        revision = payload.document_revision
        generation_run_id = payload.generation_run_id
        generation_task_id = payload.generation_task_id
        document = self._required_document(scope, document_id)
        if self._document_revision(document) != revision:
            raise WikiSupersededError("A newer document revision superseded this Wiki task")
        self.repository.update_pending_status(
            scope,
            document_id=document_id,
            document_revision=revision,
            status="running",
            task_id=str(task.get("id") or ""),
        )
        if generation_task_id:
            self.repository.update_generation_task(scope, generation_task_id, status="running")

        postprocess = self._postprocess_parent(document_id)
        with self._span(
            postprocess,
            "postprocess.wiki",
            input={
                "document_id": document_id,
                "revision": revision,
                "generation_run_id": generation_run_id,
                "task_id": str(task.get("id") or ""),
                "attempt": int(task.get("attempt") or 0),
                "model": self.model,
            },
        ) as wiki_span:
            contributions = self._map_document(scope, document, wiki_span)
            if self._document_revision(self._required_document(scope, document_id)) != revision:
                raise WikiSupersededError("Document changed before Wiki contributions were committed")
            affected = self.repository.replace_document_contributions(
                scope,
                document_id=document_id,
                document_revision=revision,
                generation_run_id=generation_run_id,
                contributions=contributions,
            )
            affected_slugs = sorted(affected)[: self.config.max_pages_per_document + 1]
            taxonomy_plan = self._plan_contribution_taxonomy(scope, contributions)
            with ThreadPoolExecutor(max_workers=max(1, min(len(affected_slugs) or 1, self.config.reduce_concurrency))) as executor:
                reduced = [
                    slug
                    for slug in executor.map(
                        lambda value: self._reduce_affected_slug(
                            scope,
                            value,
                            generation_run_id,
                            wiki_span,
                            category_path=taxonomy_plan.get(value),
                        ),
                        affected_slugs,
                    )
                    if slug
                ]

        self.repository.append_log(
            scope,
            idempotency_key=f"ingest:{generation_run_id}",
            event_type="document_ingested",
            document_id=document_id,
            page_slugs=reduced,
            message=f"Wiki ingest completed for {document.get('name') or document_id}",
            metadata={"document_revision": revision, "generation_run_id": generation_run_id},
        )
        finalize_payload = {
            "schema_version": 1,
            "generation_run_id": generation_run_id,
            "generation_run_ids": [generation_run_id],
            "generation_task_ids": [generation_task_id] if generation_task_id else [],
            "document_ids": [document_id],
            "affected_slugs": reduced,
        }
        finalize_at = _iso_after(self.config.followup_seconds)
        merged_finalize = self.processing_repository.merge_runnable_task_payload(
            scope,
            WIKI_FINALIZE_TASK,
            finalize_payload,
            run_after=finalize_at,
        )
        if merged_finalize is None:
            self.processing_repository.create_task(
                WIKI_FINALIZE_TASK,
                scope,
                payload=finalize_payload,
                document_id="",
                upload_batch_id=generation_run_id,
                max_attempts=self.config.max_attempts,
                run_after=finalize_at,
                trace_id=str(task.get("trace_id") or ""),
                payload_schema_version=1,
                idempotency_key=f"wiki-finalize:{scope.knowledge_base_id}:{generation_run_id}",
                parent_trace_id=str(task.get("trace_id") or ""),
            )
        if generation_task_id:
            self.repository.update_generation_task(
                scope,
                generation_task_id,
                status="finalizing",
                page_slug=next((slug for slug in reduced if self._page_type_for_slug(scope, slug) == "summary"), ""),
            )

    def process_finalize_task(self, task: dict[str, Any]) -> None:
        scope = _task_scope(task)
        payload = WikiFinalizePayload.from_task(task)
        generation_run_id = payload.generation_run_id
        document_ids = list(payload.document_ids)
        parent = self._postprocess_parent(document_ids[0]) if document_ids else None
        with self._span(parent, "postprocess.wiki.finalize", input={"generation_run_id": generation_run_id}):
            self._plan_taxonomy(scope)
            pages, _ = self.repository.list_pages(scope, status="published", limit=200)
            self._detect_duplicate_pages(scope, pages)
            index_page = self._rebuild_index(scope, generation_run_id)
            log_page = self._rebuild_log_page(scope, generation_run_id)
            self.page_service.refresh_links(scope)
            self._lint_pages(scope, review_duplicates=False)
        for generation_task_id in payload.generation_task_ids:
            if generation_task_id:
                self.repository.update_generation_task(
                    scope,
                    str(generation_task_id),
                    status="completed",
                    page_slug=index_page.slug,
                )
        for document_id in document_ids:
            document = self.page_service.document_repository.get_document(document_id, scope)
            if document:
                self.repository.update_pending_status(
                    scope,
                    document_id=document_id,
                    document_revision=self._document_revision(document),
                    status="completed",
                    task_id=str(task.get("id") or ""),
                )
        self.repository.append_log(
            scope,
            idempotency_key=f"finalize:{generation_run_id}",
            event_type="wiki_finalized",
            page_slugs=[index_page.slug, log_page.slug],
            message="Wiki index, log, and links were finalized.",
        )

    def reconcile_terminal_failure(self, task: dict[str, Any], error_message: str) -> None:
        if task.get("task_type") not in {WIKI_INGEST_TASK, WIKI_FINALIZE_TASK}:
            return
        scope = _task_scope(task)
        payload = dict(task.get("payload") or {})
        generation_ids = list(payload.get("generation_task_ids") or [])
        if payload.get("generation_task_id"):
            generation_ids.append(payload["generation_task_id"])
        for generation_id in generation_ids:
            try:
                self.repository.update_generation_task(
                    scope, str(generation_id), status="dead_lettered", error_message=error_message
                )
            except KeyError:
                pass
        document_id = str(payload.get("document_id") or task.get("document_id") or "")
        revision = str(payload.get("document_revision") or task.get("source_revision") or "")
        if document_id and revision:
            self.repository.update_pending_status(
                scope,
                document_id=document_id,
                document_revision=revision,
                status="dead_lettered",
                task_id=str(task.get("id") or ""),
                last_error=error_message,
            )

    def reconcile_cancelled(self, task: dict[str, Any], reason: str) -> None:
        if task.get("task_type") not in {WIKI_INGEST_TASK, WIKI_FINALIZE_TASK}:
            return
        scope = _task_scope(task)
        payload = dict(task.get("payload") or {})
        generation_ids = list(payload.get("generation_task_ids") or [])
        if payload.get("generation_task_id"):
            generation_ids.append(payload["generation_task_id"])
        for generation_id in generation_ids:
            try:
                self.repository.update_generation_task(
                    scope, str(generation_id), status="cancelled", error_message=reason
                )
            except KeyError:
                pass
        document_id = str(payload.get("document_id") or task.get("document_id") or "")
        revision = str(payload.get("document_revision") or task.get("source_revision") or "")
        if document_id and revision:
            self.repository.update_pending_status(
                scope,
                document_id=document_id,
                document_revision=revision,
                status="cancelled",
                task_id=str(task.get("id") or ""),
                last_error=reason,
            )

    def _map_document(
        self,
        scope: KnowledgeBaseScope,
        document: dict[str, Any],
        parent_span: ProcessingSpan | None,
    ) -> list[dict[str, Any]]:
        document_id = str(document.get("id") or "")
        chunks = self.document_repository.list_chunks_for_documents(
            [document_id],
            chunk_types=self.SOURCE_TYPES,
            scope=scope,
            limit=self.config.max_source_chunks,
        )
        if not chunks:
            chunks = self.document_repository.list_chunks_for_documents(
                [document_id], scope=scope, limit=self.config.max_source_chunks
            )
        source_text, usable_chunks = self._source_text(chunks)
        if len(re.sub(r"\s+", "", source_text)) < self.config.min_source_chars:
            return []
        document_name = str(document.get("name") or document_id)
        with self._span(parent_span, "postprocess.wiki.extract", input={"chunks": len(usable_chunks)}) as extract_span:
            candidates = self._extract_candidates(document_name, source_text, usable_chunks)
            self._set_span_output(extract_span, {"candidate_count": len(candidates), "document_id": document_id, "source_truncated": self._source_truncated, **self._llm_metrics.get("wiki_extract_candidates", {})})

        with ThreadPoolExecutor(max_workers=max(1, min(2, self.config.map_concurrency))) as executor:
            summary_future = executor.submit(
                self._summarize_document,
                document_name,
                source_text,
                usable_chunks,
                candidates,
            )
            classify_future = executor.submit(self._classify_chunks, document_name, source_text, usable_chunks, candidates)
            with self._span(parent_span, "postprocess.wiki.summary", input={"chunks": len(usable_chunks)}) as summary_span:
                summary = summary_future.result(timeout=self.config.timeout_seconds)
                self._set_span_output(summary_span, {"summary_chars": len(summary.get("summary", "")), "document_id": document_id, **self._llm_metrics.get("wiki_document_summary", {})})
            with self._span(parent_span, "postprocess.wiki.classify", input={"candidates": len(candidates)}) as classify_span:
                matches = classify_future.result(timeout=self.config.timeout_seconds)
                self._set_span_output(classify_span, {"match_count": len(matches), "document_id": document_id, **self._llm_metrics.get("wiki_classify_chunks", {})})

        refs = [
            {"doc_id": document_id, "chunk_id": str(chunk.get("id") or ""), "title": Path(document_name).stem}
            for chunk in usable_chunks
            if chunk.get("id")
        ]
        title = Path(document_name).stem or document_name
        contributions = [
            {
                "page_slug": normalize_wiki_slug(title),
                "title": title,
                "page_type": "summary",
                "summary": summary["summary"],
                "content_markdown": summary["content_markdown"],
                "aliases": [document_name] if document_name != title else [],
                "source_refs": refs,
                "source_chunk_ids": [str(chunk.get("id") or "") for chunk in usable_chunks if chunk.get("id")],
            }
        ]
        match_by_slug = {str(item.get("slug") or ""): item for item in matches}
        chunk_by_id = {str(chunk.get("id") or ""): chunk for chunk in usable_chunks}
        for candidate in candidates[: self.config.max_pages_per_document - 1]:
            slug = normalize_wiki_slug(str(candidate.get("slug") or candidate.get("title") or ""))
            match = match_by_slug.get(slug)
            if not match:
                continue
            chunk_ids = [value for value in match.get("chunk_ids") or [] if value in chunk_by_id]
            if not chunk_ids:
                continue
            candidate_refs = [ref for ref in refs if ref["chunk_id"] in chunk_ids]
            contribution_summary = str(match.get("summary") or "").strip()
            if not contribution_summary:
                contribution_summary = str(candidate.get("details") or candidate.get("description") or "").strip()
            if not contribution_summary:
                contribution_summary = _first_text([chunk_by_id[value] for value in chunk_ids], 500)
            contributions.append(
                {
                    "page_slug": slug,
                    "title": str(candidate.get("title") or slug),
                    "page_type": str(candidate.get("page_type") or "concept"),
                    "summary": contribution_summary,
                    "content_markdown": contribution_summary,
                    "aliases": list(candidate.get("aliases") or []),
                    "source_refs": candidate_refs,
                    "source_chunk_ids": chunk_ids,
                }
            )
        return contributions

    def _reduce_affected_slug(
        self,
        scope: KnowledgeBaseScope,
        slug: str,
        generation_run_id: str,
        parent_span: ProcessingSpan | None,
        *,
        category_path: list[str] | None = None,
    ) -> str:
        page_type = self._page_type_for_slug(scope, slug)
        with self._span(
            parent_span,
            f"postprocess.wiki.page[{page_type}:{slug}]",
            input={"slug": slug, "page_type": page_type, "generation_run_id": generation_run_id},
        ) as page_span:
            with self._page_lock(scope, slug):
                for attempt in range(5):
                    try:
                        if category_path:
                            published = self._reduce_slug(
                                scope,
                                slug,
                                generation_run_id,
                                category_path=category_path,
                            )
                        else:
                            published = self._reduce_slug(scope, slug, generation_run_id)
                        reduced = slug if published else ""
                        self._set_span_output(page_span, {"slug": slug, "page_type": page_type, "generation_run_id": generation_run_id, "commit_attempt": attempt + 1, "published": bool(reduced)})
                        return reduced
                    except sqlite3.OperationalError as exc:
                        if "locked" not in str(exc).lower() or attempt == 4:
                            raise
                        sleep(0.01 * (2**attempt))
        return ""

    def _page_lock(self, scope: KnowledgeBaseScope, slug: str) -> Lock:
        key = (scope.knowledge_base_id, normalize_wiki_slug(slug))
        with self._page_lock_guard:
            return self._page_locks.setdefault(key, Lock())

    def _source_text(self, chunks: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        lines: list[str] = []
        selected: list[dict[str, Any]] = []
        remaining = self.config.max_source_chars
        self._source_truncated = False
        for chunk in chunks:
            text = str(chunk.get("content_markdown") or chunk.get("content") or "").strip()
            chunk_id = str(chunk.get("id") or "")
            if not text or not chunk_id or remaining <= 0:
                continue
            block = f"[chunk:{chunk_id}]\n{text}\n"
            block = block[:remaining]
            lines.append(block)
            selected.append(chunk)
            remaining -= len(block)
        self._source_truncated = len(selected) < len(chunks) or remaining <= 0
        return "\n".join(lines), selected

    def _extract_candidates(
        self, document_name: str, source_text: str, chunks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        variables = {
            "document_name": document_name,
            "source_chunks": source_text,
            "max_candidates": self.config.max_candidates,
            "language": self.config.language,
        }
        payload = self._complete_json("wiki_extract_candidates", variables)
        candidates = self._validate_candidates(payload.get("candidates") if payload else None)
        if not candidates:
            payload = self._complete_json("wiki_combined_extract_fallback", variables)
            candidates = self._validate_candidates(payload.get("candidates") if payload else None)
        if not candidates:
            candidates = self._fallback_candidates(chunks)
        return candidates[: self.config.max_candidates]

    def _summarize_document(
        self,
        document_name: str,
        source_text: str,
        chunks: list[dict[str, Any]],
        candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        payload = self._complete_json(
            "wiki_document_summary",
            {
                "document_name": document_name,
                "source_chunks": source_text,
                "candidates": json.dumps(candidates or [], ensure_ascii=False),
                "language": self.config.language,
            },
        )
        structured = WikiSummaryOutput.from_value(payload)
        summary = structured.summary
        content = structured.content_markdown
        title = Path(document_name).stem or document_name
        if not summary:
            summary = _first_text(chunks, 420)
        if not content:
            notes = "\n\n".join(
                f"### {chunk.get('title_path') or f'来源 {index}'}\n{str(chunk.get('content_markdown') or chunk.get('content') or '')[:1200]}"
                for index, chunk in enumerate(chunks[:8], start=1)
            )
            content = f"# {title}\n\n## 摘要\n\n{summary}\n\n## 主要内容\n\n{notes}"
        return {"summary": summary[:1200], "content_markdown": content[: self.config.max_source_chars]}

    def _classify_chunks(
        self,
        document_name: str,
        source_text: str,
        chunks: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not candidates:
            return []
        payload = self._complete_json(
            "wiki_classify_chunks",
            {
                "document_name": document_name,
                "source_chunks": source_text,
                "candidates": json.dumps(candidates, ensure_ascii=False),
                "language": self.config.language,
            },
        )
        valid_ids = {str(chunk.get("id") or "") for chunk in chunks}
        candidate_slugs = {normalize_wiki_slug(str(item.get("slug") or item.get("title") or "")) for item in candidates}
        matches: list[dict[str, Any]] = []
        for item in list((payload or {}).get("matches") or []):
            try:
                matches.append(
                    WikiClassificationOutput.from_value(
                        item,
                        valid_slugs=candidate_slugs,
                        valid_chunk_ids=valid_ids,
                    ).to_dict()
                )
            except WikiValidationError:
                continue
        if matches:
            return matches
        return self._fallback_classification(chunks, candidates)

    def _validate_candidates(self, value: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in list(value or []):
            try:
                candidate = WikiCandidateOutput.from_value(item)
            except WikiValidationError:
                continue
            if candidate.slug in seen:
                continue
            seen.add(candidate.slug)
            result.append(candidate.to_dict())
        return result

    def _fallback_candidates(self, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for chunk in chunks:
            title = str(chunk.get("title_path") or "").split("/")[-1].strip()
            if len(title) < 2 or title.lower() in {"overview", "summary", "摘要", "概述"}:
                continue
            slug = normalize_wiki_slug(title)
            if slug in seen:
                continue
            seen.add(slug)
            candidates.append({"title": title, "slug": slug, "page_type": "concept", "aliases": []})
        return candidates[: self.config.max_candidates]

    def _fallback_classification(
        self, chunks: list[dict[str, Any]], candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for candidate in candidates:
            title = str(candidate.get("title") or "").lower()
            slug = normalize_wiki_slug(str(candidate.get("slug") or title))
            selected = []
            for chunk in chunks:
                haystack = f"{chunk.get('title_path') or ''}\n{chunk.get('content_markdown') or chunk.get('content') or ''}".lower()
                if title and title in haystack:
                    selected.append(str(chunk.get("id") or ""))
            if selected:
                matches.append({"slug": slug, "chunk_ids": selected[:12], "summary": ""})
        return matches

    def _reduce_slug(
        self,
        scope: KnowledgeBaseScope,
        slug: str,
        generation_run_id: str,
        *,
        category_path: list[str] | None = None,
    ) -> bool:
        commit_key = f"reduce:{generation_run_id}:{normalize_wiki_slug(slug)}"
        if self.repository.get_committed_page_update(scope, commit_key) is not None:
            return True
        rows = self.repository.active_contributions_for_slug(scope, slug)
        existing = self.repository.get_page_by_slug(scope, slug, include_archived=True)
        grounded = []
        for row in rows:
            contribution = dict(row.get("contribution") or {})
            refs = []
            for ref in contribution.get("source_refs") or []:
                doc_id = str(ref.get("doc_id") or "")
                chunk_id = str(ref.get("chunk_id") or "")
                if not doc_id or self.document_repository.get_document(doc_id, scope) is None:
                    continue
                if chunk_id and self.document_repository.get_chunk(chunk_id, scope) is None:
                    continue
                refs.append(ref)
            if refs:
                contribution["source_refs"] = refs
                contribution["source_chunk_ids"] = [str(ref.get("chunk_id") or "") for ref in refs if ref.get("chunk_id")]
                grounded.append(contribution)
        if not grounded:
            if existing and existing.metadata.get("generated"):
                if existing.metadata.get("protected_manual_content"):
                    self.repository.update_page(scope, slug, {"status": "stale"})
                else:
                    self.repository.archive_page(scope, slug)
            return False

        title = str(grounded[0].get("title") or slug)
        page_type = str(grounded[0].get("page_type") or "concept")
        source_refs = _dedupe_dicts(
            [ref for item in grounded for ref in item.get("source_refs") or []],
            keys=("doc_id", "chunk_id"),
        )
        chunk_refs = list(dict.fromkeys(str(value) for item in grounded for value in item.get("source_chunk_ids") or [] if str(value)))
        aliases = list(dict.fromkeys(str(value) for item in grounded for value in item.get("aliases") or [] if str(value)))
        summary = " ".join(str(item.get("summary") or "").strip() for item in grounded if str(item.get("summary") or "").strip())[:1200]
        deterministic = self._deterministic_page(title, page_type, grounded)
        payload = self._complete_json(
            "wiki_reduce_page",
            {
                "page_title": title,
                "page_type": page_type,
                "current_page": existing.content_markdown if existing else "",
                "contributions": json.dumps(grounded, ensure_ascii=False),
                "language": self.config.language,
            },
        )
        content = str((payload or {}).get("content_markdown") or deterministic).strip()
        reduced_summary = str((payload or {}).get("summary") or summary or _first_markdown_paragraph(content)).strip()
        metadata = dict(existing.metadata if existing else {})
        metadata.update(
            {
                "generated": True,
                "generator": "wiki_map_reduce_v1",
                "generation_run_id": generation_run_id,
                "contribution_count": len(grounded),
            }
        )
        planned_category = self._clean_taxonomy_path(category_path)
        should_apply_category = (
            bool(planned_category)
            and not (existing and existing.folder_id)
            and not (existing and existing.category_path)
            and not (existing and existing.metadata.get("manual_taxonomy"))
        )
        if should_apply_category:
            metadata["taxonomy_generated"] = True
        changes = {
            "title": title,
            "page_type": page_type,
            "status": "published",
            "content_markdown": content[: self.config.max_source_chars],
            "summary": reduced_summary[:1200],
            "source_refs": source_refs,
            "chunk_refs": chunk_refs,
            "aliases": aliases,
            "metadata": metadata,
        }
        if should_apply_category:
            changes["category_path"] = planned_category
        if existing and existing.status != "archived":
            self.repository.update_page(
                scope,
                slug,
                changes,
                expected_version=existing.version,
                idempotency_key=commit_key,
                generation_run_id=generation_run_id,
            )
        else:
            self.repository.create_page(
                scope,
                slug=slug,
                idempotency_key=commit_key,
                generation_run_id=generation_run_id,
                **changes,
            )
        return True

    def _deterministic_page(self, title: str, page_type: str, contributions: list[dict[str, Any]]) -> str:
        label = {"entity": "实体", "concept": "概念", "summary": "摘要"}.get(page_type, "知识")
        sections = [f"# {title}", "", f"> {label}页面", ""]
        for contribution in contributions:
            text = str(contribution.get("content_markdown") or contribution.get("summary") or "").strip()
            if text.startswith("# "):
                text = "\n".join(text.splitlines()[1:]).strip()
            if text:
                sections.extend([text, ""])
        return "\n".join(sections).strip()

    def _rebuild_index(self, scope: KnowledgeBaseScope, generation_run_id: str):
        pages, _ = self.repository.list_pages(scope, status="published", limit=100)
        content_pages = [page for page in pages if page.page_type not in {"index", "log"}]
        groups = {
            "summary": [page for page in content_pages if page.page_type == "summary"],
            "entity": [page for page in content_pages if page.page_type == "entity"],
            "concept": [page for page in content_pages if page.page_type == "concept"],
        }
        knowledge_base = self.page_service.knowledge_base_service.get(scope.knowledge_base_id)
        counts = {key: len(value) for key, value in groups.items()}
        existing_index = self.repository.get_page_by_slug(scope, "index", include_archived=True)
        intro_payload = self._complete_json(
            "wiki_index_intro",
            {
                "knowledge_base_name": knowledge_base.name,
                "page_counts": json.dumps(counts, ensure_ascii=False),
                "page_summaries": json.dumps(
                    [
                        {
                            "slug": page.slug,
                            "title": page.title,
                            "page_type": page.page_type,
                            "summary": page.summary[:500],
                            "category_path": list(page.category_path),
                        }
                        for page in content_pages
                    ],
                    ensure_ascii=False,
                ),
                "current_introduction": existing_index.summary if existing_index else "",
                "language": self.config.language,
            },
        )
        intro = str((intro_payload or {}).get("introduction") or "本页为维基索引，将随页面增加自动更新。")
        lines = ["# 索引", "", "> 分类目录", "", "## 维基索引", "", intro, ""]
        for page_type, heading in (("summary", "摘要"), ("entity", "实体"), ("concept", "概念")):
            items = groups[page_type]
            lines.extend([f"## {heading} ({len(items)})", ""])
            for page in sorted(items, key=lambda value: value.title.lower()):
                lines.append(f"- [[{page.slug}|{page.title}]] — {page.summary[:180]}")
            if not items:
                lines.append("暂无页面。")
            lines.append("")
        return self._upsert_system_page(
            scope,
            slug="index",
            title="索引",
            page_type="index",
            content="\n".join(lines).strip(),
            summary=intro,
            generation_run_id=generation_run_id,
        )

    def _rebuild_log_page(self, scope: KnowledgeBaseScope, generation_run_id: str):
        logs, _ = self.repository.list_logs(scope, limit=100)
        lines = ["# 日志", "", "Wiki 生成与维护记录。", ""]
        for item in logs:
            pages = ", ".join(str(value) for value in item.get("page_slugs") or [])
            suffix = f"；页面：{pages}" if pages else ""
            lines.append(f"- **{item.get('created_at') or ''}** {item.get('message') or item.get('event_type')}{suffix}")
        return self._upsert_system_page(
            scope,
            slug="log",
            title="日志",
            page_type="log",
            content="\n".join(lines).strip(),
            summary="Wiki 生成与维护记录。",
            generation_run_id=generation_run_id,
        )

    def _upsert_system_page(
        self,
        scope: KnowledgeBaseScope,
        *,
        slug: str,
        title: str,
        page_type: str,
        content: str,
        summary: str,
        generation_run_id: str,
    ):
        existing = self.repository.get_page_by_slug(scope, slug, include_archived=True)
        changes = {
            "title": title,
            "page_type": page_type,
            "status": "published",
            "content_markdown": content,
            "summary": summary,
            "metadata": {"generated": True, "system_view": True, "generation_run_id": generation_run_id},
        }
        commit_key = f"system:{generation_run_id}:{normalize_wiki_slug(slug)}"
        if existing and existing.status != "archived":
            return self.repository.update_page(
                scope,
                slug,
                changes,
                expected_version=existing.version,
                idempotency_key=commit_key,
                generation_run_id=generation_run_id,
            )
        return self.repository.create_page(
            scope,
            slug=slug,
            idempotency_key=commit_key,
            generation_run_id=generation_run_id,
            **changes,
        )

    def _plan_taxonomy(self, scope: KnowledgeBaseScope) -> None:
        pages, _ = self.repository.list_pages(scope, status="published", limit=200)
        pending = [
            page
            for page in pages
            if page.page_type not in {"index", "log"}
            and not page.folder_id
            and not page.category_path
            and not page.metadata.get("manual_taxonomy")
        ]
        if not pending:
            return
        items = [self._taxonomy_item_from_page(page) for page in pending]
        assignments = self._generate_taxonomy_assignments(scope, items, pages)

        for page in pending:
            category_path = assignments[page.slug]
            metadata = dict(page.metadata)
            metadata["taxonomy_generated"] = True
            self.repository.update_page(
                scope,
                page.slug,
                {"category_path": category_path, "metadata": metadata},
            )

    def _plan_contribution_taxonomy(
        self,
        scope: KnowledgeBaseScope,
        contributions: list[dict[str, Any]],
    ) -> dict[str, list[str]]:
        pages, _ = self.repository.list_pages(scope, status="published", limit=200)
        existing = {page.slug: page for page in pages}
        items_by_slug: dict[str, dict[str, Any]] = {}
        for contribution in contributions:
            slug = normalize_wiki_slug(str(contribution.get("page_slug") or ""))
            page = existing.get(slug)
            if page and (page.folder_id or page.category_path or page.metadata.get("manual_taxonomy")):
                continue
            if slug not in items_by_slug:
                items_by_slug[slug] = {
                    "slug": slug,
                    "title": str(contribution.get("title") or slug)[:160],
                    "page_type": str(contribution.get("page_type") or "concept"),
                    "aliases": list(contribution.get("aliases") or [])[:8],
                    "summary": str(contribution.get("summary") or "")[:500],
                }
        if not items_by_slug:
            return {}
        return self._generate_taxonomy_assignments(scope, list(items_by_slug.values()), pages)

    def _generate_taxonomy_assignments(
        self,
        scope: KnowledgeBaseScope,
        items: list[dict[str, Any]],
        pages: list[Any],
    ) -> dict[str, list[str]]:
        categories = {"summary": "摘要", "entity": "实体", "concept": "概念", "manual": "手工页面"}
        payload = self._complete_json(
            "wiki_taxonomy_plan",
            {
                "pages": json.dumps(items, ensure_ascii=False),
                "existing_taxonomy": json.dumps(self._existing_taxonomy(scope, pages), ensure_ascii=False),
                "language": self.config.language,
            },
        )
        raw_assignments = list((payload or {}).get("assignments") or (payload or {}).get("placements") or [])
        assignments: dict[str, list[str]] = {}
        items_by_slug = {normalize_wiki_slug(str(item.get("slug") or "")): item for item in items}
        for item in raw_assignments:
            if not isinstance(item, dict):
                continue
            slug = normalize_wiki_slug(str(item.get("slug") or ""))
            if slug not in items_by_slug or slug in assignments:
                continue
            path = self._clean_taxonomy_path(item.get("path") or item.get("category_path"))
            if path:
                assignments[slug] = path
        for slug, item in items_by_slug.items():
            assignments.setdefault(slug, [categories.get(str(item.get("page_type") or ""), "知识")])
        return assignments

    @staticmethod
    def _taxonomy_item_from_page(page: Any) -> dict[str, Any]:
        return {
            "slug": page.slug,
            "title": page.title,
            "page_type": page.page_type,
            "aliases": list(page.aliases),
            "summary": page.summary[:500],
        }

    def _existing_taxonomy(self, scope: KnowledgeBaseScope, pages: list[Any]) -> list[list[str]]:
        paths = {tuple(page.category_path) for page in pages if page.category_path}
        pending_parent_ids = [""]
        seen_folder_ids: set[str] = set()
        while pending_parent_ids and len(seen_folder_ids) < 200:
            parent_id = pending_parent_ids.pop(0)
            for folder in self.repository.list_folders(scope, parent_id=parent_id):
                if folder.id in seen_folder_ids:
                    continue
                seen_folder_ids.add(folder.id)
                path = self._clean_taxonomy_path(folder.path.split("/"))
                if path:
                    paths.add(tuple(path))
                pending_parent_ids.append(folder.id)
        return [list(path) for path in sorted(paths)]

    @staticmethod
    def _clean_taxonomy_path(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        path: list[str] = []
        for raw_label in value[:2]:
            label = re.sub(r"\s+", " ", str(raw_label or "").strip())[:120]
            if not label or "/" in label or "\\" in label:
                continue
            path.append(label)
        return path

    def _lint_pages(self, scope: KnowledgeBaseScope, *, review_duplicates: bool = True) -> None:
        pages, _ = self.repository.list_pages(scope, status="published", limit=200)
        available = {page.slug for page in pages}
        for page in pages:
            checks: list[tuple[str, str, list[str], list[str]]] = []
            for target in page.out_links:
                if target not in available:
                    checks.append(("dead_link", f"Wiki page links to unavailable target: {target}", [], []))
            stale_doc_ids = list(dict.fromkeys(ref.doc_id for ref in page.source_refs if self.document_repository.get_document(ref.doc_id, scope) is None))
            stale_chunk_ids = list(dict.fromkeys(ref.chunk_id for ref in page.source_refs if ref.chunk_id and self.document_repository.get_chunk(ref.chunk_id, scope) is None))
            if stale_doc_ids or stale_chunk_ids:
                checks.append(("missing_source", "Page references source evidence that is no longer available.", stale_doc_ids, stale_chunk_ids))
            if page.metadata.get("generated") and page.page_type not in {"index", "log"} and not page.source_refs:
                checks.append(("ungrounded_content", "Generated page has no current source evidence.", [], []))
            if page.page_type not in {"index", "log"} and not page.folder_id and not page.category_path:
                checks.append(("taxonomy", "Page has no taxonomy placement.", [], []))
            existing = self.repository.list_issues(scope, slug=page.slug, status="open", limit=100)
            for issue_type, description, doc_ids, chunk_ids in checks:
                if not any(issue.issue_type == issue_type and issue.description == description for issue in existing):
                    self.repository.create_issue(
                        scope,
                        slug=page.slug,
                        issue_type=issue_type,
                        description=description,
                        suspected_doc_ids=doc_ids,
                        suspected_chunk_ids=chunk_ids,
                        reported_by="system",
                    )
        if review_duplicates:
            self._detect_duplicate_pages(scope, pages)

    def _detect_duplicate_pages(self, scope: KnowledgeBaseScope, pages: list[Any]) -> None:
        review_pages = [item for item in pages if item.page_type in {"entity", "concept"}]
        by_slug = {page.slug: page for page in review_pages}
        review = self._complete_json(
            "wiki_duplicate_review",
            {
                "pages": json.dumps(
                    [
                        {
                            "slug": page.slug,
                            "title": page.title,
                            "page_type": page.page_type,
                            "aliases": list(page.aliases),
                            "summary": page.summary[:500],
                            "source_count": len(page.source_refs),
                            "created_at": page.created_at,
                        }
                        for page in review_pages
                    ],
                    ensure_ascii=False,
                ),
                "language": self.config.language,
            },
        ) if len(review_pages) > 1 else None
        reasons = dict((review or {}).get("reasons") or {})
        proposed_pairs: set[tuple[str, str]] = set()
        for source_slug, target_slug in dict((review or {}).get("merges") or {}).items():
            source = by_slug.get(normalize_wiki_slug(str(source_slug)))
            target = by_slug.get(normalize_wiki_slug(str(target_slug)))
            if source is None or target is None or source.slug == target.slug or source.page_type != target.page_type:
                continue
            pair = tuple(sorted((source.slug, target.slug)))
            if pair in proposed_pairs:
                continue
            proposed_pairs.add(pair)
            self._propose_duplicate_pair(
                scope,
                source,
                target,
                str(reasons.get(source_slug) or "High-confidence LLM identity review."),
            )

        owners: dict[str, Any] = {}
        for page in sorted(review_pages, key=lambda item: item.slug):
            keys = {normalize_wiki_slug(page.title), *(normalize_wiki_slug(alias) for alias in page.aliases)}
            for key in keys:
                other = owners.get(key)
                if other is None or other.slug == page.slug:
                    owners[key] = page
                    continue
                pair = tuple(sorted((other.slug, page.slug)))
                if pair in proposed_pairs:
                    continue
                proposed_pairs.add(pair)
                self._propose_duplicate_pair(scope, page, other, "Titles or aliases normalize to the same value.")

    def _propose_duplicate_pair(self, scope: KnowledgeBaseScope, source: Any, target: Any, reason: str) -> None:
        pair = tuple(sorted((source.slug, target.slug)))
        description = f"Potential duplicate Wiki pages: {pair[0]} and {pair[1]}"
        existing_issues = self.repository.list_issues(scope, slug=source.slug, status="open", limit=100)
        if not any(issue.issue_type == "duplicate_page" and issue.description == description for issue in existing_issues):
            self.repository.create_issue(
                scope,
                slug=source.slug,
                issue_type="duplicate_page",
                description=description,
                reported_by="system",
            )
        existing_proposals = self.repository.list_proposals(scope, slug=source.slug, status="pending", limit=100)
        if any(
            proposal.action == "merge_pages" and proposal.payload.get("target_slug") == target.slug
            for proposal in existing_proposals
        ):
            return
        self.repository.create_proposal(
            scope,
            action="merge_pages",
            slug=source.slug,
            title=f"Merge {source.title} into {target.title}",
            payload={"source_slug": source.slug, "target_slug": target.slug},
            source_refs=[*[ref.to_dict() for ref in target.source_refs], *[ref.to_dict() for ref in source.source_refs]],
            chunk_refs=[*target.chunk_refs, *source.chunk_refs],
            reason=f"{description}. {reason}"[:1200],
            created_by="system",
        )

    def _page_type_for_slug(self, scope: KnowledgeBaseScope, slug: str) -> str:
        rows = self.repository.active_contributions_for_slug(scope, slug)
        if rows:
            return str(rows[0].get("page_type") or "concept")
        page = self.repository.get_page_by_slug(scope, slug, include_archived=True)
        return page.page_type if page else "concept"

    def _complete_json(self, template_id: str, variables: dict[str, Any]) -> dict[str, Any] | None:
        if self.llm_client is None:
            return None
        try:
            prompt = self.prompt_catalog.render(template_id, variables, mode="postprocess")
        except PromptTemplateError:
            return None
        last_error: Exception | None = None
        attempt_limit = min(3, max(1, int(self.config.llm_max_attempts)))
        for attempt in range(1, attempt_limit + 1):
            try:
                completion = self.llm_client.chat.completions.create(
                    model=self.model,
                    temperature=self.config.llm_temperature,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Follow the supplied Wiki task exactly. Return only valid JSON, preserve exact slugs and "
                                "chunk IDs, and never add facts not grounded in the supplied inputs."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                )
                text = str(completion.choices[0].message.content or "{}")
                value = json.loads(_strip_json_fence(text))
                if not isinstance(value, dict):
                    raise ValueError("Wiki LLM response must be a JSON object")
                usage = getattr(completion, "usage", None)
                self._llm_metrics[template_id] = {
                    "model": self.model,
                    "llm_attempts": attempt,
                    "temperature": self.config.llm_temperature,
                    "usage": usage.model_dump() if hasattr(usage, "model_dump") else {},
                }
                return value
            except Exception as exc:
                last_error = exc
        self._llm_metrics[template_id] = {
            "model": self.model,
            "llm_attempts": attempt_limit,
            "temperature": self.config.llm_temperature,
            "error": str(last_error or "invalid response")[:500],
        }
        if last_error is not None:
            logger.warning("wiki.llm.invalid_response", extra={"template_id": template_id, "error": str(last_error)})
        return None

    def _required_document(self, scope: KnowledgeBaseScope, document_id: str) -> dict[str, Any]:
        if self.document_repository is None:
            raise WikiValidationError("Document repository is unavailable")
        document = self.document_repository.get_document(document_id, scope)
        if document is None:
            raise KeyError(document_id)
        return document

    def _document_revision(self, document: dict[str, Any]) -> str:
        metadata = dict(document.get("metadata_json") or {})
        source_marker = metadata.get("source_revision") or metadata.get("processing_trace_id") or document.get("updated_at")
        return ":".join(
            [
                str(source_marker or ""),
                str(metadata.get("processing_version") or ""),
                str(metadata.get("chunks") or ""),
            ]
        )

    def _postprocess_parent(self, document_id: str) -> ProcessingSpan | None:
        attempt = self.span_tracker.latest_attempt(document_id)
        if not attempt:
            return None
        row = self.span_tracker.lookup_stage(document_id, attempt, "postprocess")
        if not row:
            return None
        return ProcessingSpan(
            knowledge_id=document_id,
            attempt=attempt,
            span_id=str(row["span_id"]),
            name="postprocess",
            kind=SPAN_STAGE,
            parent_span_id=row.get("parent_span_id"),
            started_clock=perf_counter(),
        )

    @contextmanager
    def _span(
        self,
        parent: ProcessingSpan | None,
        name: str,
        *,
        input: dict[str, Any] | None = None,
    ) -> Iterator[ProcessingSpan | None]:
        span = self.span_tracker.begin_subspan(parent, name, input=input or {})
        try:
            yield span
        except Exception as exc:
            self.span_tracker.fail_span(span, exc)
            raise
        else:
            self.span_tracker.end_span(span)

    def _set_span_output(self, span: ProcessingSpan | None, output: dict[str, Any]) -> None:
        self.span_tracker.update_output(span, output)


class WikiSupersededError(RuntimeError):
    pass


def _task_scope(task: dict[str, Any]) -> KnowledgeBaseScope:
    return KnowledgeBaseScope(
        workspace_id=str(task.get("workspace_id") or ""),
        selected_knowledge_base_ids=(str(task.get("knowledge_base_id") or ""),),
    )


def _required_payload_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise WikiValidationError(f"Wiki task payload requires {name}")
    return text


def _iso_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0, int(seconds)))).replace(
        microsecond=0
    ).isoformat()


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _first_text(chunks: list[dict[str, Any]], limit: int) -> str:
    for chunk in chunks:
        text = re.sub(r"\s+", " ", str(chunk.get("content_markdown") or chunk.get("content") or "").strip())
        if text:
            return text[:limit]
    return ""


def _first_markdown_paragraph(value: str) -> str:
    for block in re.split(r"\n\s*\n", value or ""):
        text = re.sub(r"^[#>\-\s]+", "", block).strip()
        if text:
            return re.sub(r"\s+", " ", text)[:500]
    return ""


def _dedupe_dicts(values: list[dict[str, Any]], *, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for value in values:
        key = tuple(str(value.get(name) or "") for name in keys)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        result.append(dict(value))
    return result
