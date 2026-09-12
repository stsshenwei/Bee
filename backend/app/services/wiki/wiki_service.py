from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from app.models.knowledge_base import KnowledgeBaseScope
from app.models.wiki import WikiGraphEdge, WikiGraphNode, WikiPage, WikiPageIssue, WikiPageProposal, WikiSourceRef
from app.services.knowledge.knowledge_base_service import KnowledgeBaseService
from app.services.wiki.wiki_repository import WikiRepository, normalize_wiki_slug


class WikiValidationError(ValueError):
    pass


logger = logging.getLogger(__name__)


GENERATION_ACTIVE_STATES = {"pending", "queued", "running", "retrying", "finalizing"}
PROCESSING_ACTIVE_STATES = {"pending", "retrying", "processing"}
WIKI_PROCESSING_TASK_TYPES = {"wiki.ingest", "wiki.finalize"}


class WikiPageService:
    LINK_PATTERN = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
    GENERATION_CHUNK_TYPES = {"parent", "table", "ocr", "image_ocr", "image_caption"}

    def __init__(
        self,
        repository: WikiRepository,
        knowledge_base_service: KnowledgeBaseService,
        document_repository: Any | None = None,
        generation_enabled: bool = True,
        generation_max_pages_per_document: int = 1,
        generation_max_source_chunks: int = 8,
        generation_max_chars: int = 12000,
    ):
        self.repository = repository
        self.knowledge_base_service = knowledge_base_service
        self.document_repository = document_repository
        self.generation_enabled = bool(generation_enabled)
        self.generation_max_pages_per_document = max(1, int(generation_max_pages_per_document or 1))
        self.generation_max_source_chunks = max(1, int(generation_max_source_chunks or 8))
        self.generation_max_chars = max(1000, int(generation_max_chars or 12000))

    def create_page(self, scope: KnowledgeBaseScope, payload: dict[str, Any]) -> WikiPage:
        self._assert_wiki_capable(scope)
        data = dict(payload or {})
        data["out_links"] = self.parse_links(str(data.get("content_markdown") or ""))
        page = self.repository.create_page(
            scope,
            title=str(data.get("title") or ""),
            slug=str(data.get("slug") or ""),
            page_type=str(data.get("page_type") or "summary"),
            status=str(data.get("status") or "draft"),
            content_markdown=str(data.get("content_markdown") or ""),
            summary=str(data.get("summary") or ""),
            parent_slug=str(data.get("parent_slug") or ""),
            folder_id=str(data.get("folder_id") or ""),
            category_path=data.get("category_path") or (),
            sort_order=int(data.get("sort_order") or 0),
            source_refs=data.get("source_refs") or (),
            chunk_refs=data.get("chunk_refs") or (),
            out_links=data.get("out_links") or (),
            aliases=data.get("aliases") or (),
            metadata=data.get("metadata") or {},
        )
        self.refresh_links(scope)
        return self.repository.get_page_by_slug(scope, page.slug) or page

    def update_page(self, scope: KnowledgeBaseScope, slug: str, payload: dict[str, Any]) -> WikiPage:
        self._assert_wiki_capable(scope)
        changes = dict(payload or {})
        if "content_markdown" in changes:
            changes["out_links"] = self.parse_links(str(changes.get("content_markdown") or ""))
        page = self.repository.update_page(scope, slug, changes)
        self.refresh_links(scope)
        return self.repository.get_page_by_slug(scope, page.slug) or page

    def archive_page(self, scope: KnowledgeBaseScope, slug: str) -> WikiPage:
        self._assert_wiki_capable(scope)
        page = self.repository.archive_page(scope, slug)
        self.refresh_links(scope)
        return page

    def get_page(self, scope: KnowledgeBaseScope, slug: str) -> WikiPage:
        self._assert_wiki_capable(scope)
        page = self.repository.get_page_by_slug(scope, slug)
        if page is None:
            raise KeyError(slug)
        return page

    def list_pages(self, scope: KnowledgeBaseScope, **filters: Any) -> dict[str, Any]:
        self._assert_wiki_capable(scope)
        pages, next_cursor = self.repository.list_pages(scope, **filters)
        return {"items": [page.to_dict() for page in pages], "next_cursor": next_cursor}

    def search_pages(self, scope: KnowledgeBaseScope, query: str, *, limit: int = 10, status: str = "published") -> list[dict[str, Any]]:
        self._assert_wiki_capable(scope)
        bounded_limit = min(100, max(1, int(limit or 10)))
        queries = _wiki_search_queries(query)
        status_filters = [status]
        if status == "published":
            status_filters.append("")
        seen: set[str] = set()
        results: list[tuple[WikiPage, str]] = []
        for current_status in status_filters:
            for candidate_query in queries:
                pages, _ = self.repository.list_pages(scope, q=candidate_query, status=current_status, limit=bounded_limit)
                for page in pages:
                    if page.id in seen:
                        continue
                    seen.add(page.id)
                    results.append((page, candidate_query))
                    if len(results) >= bounded_limit:
                        return [self._page_search_item(item, matched_query) for item, matched_query in results]
        return [self._page_search_item(item, matched_query) for item, matched_query in results]

    def create_folder(self, scope: KnowledgeBaseScope, payload: dict[str, Any]):
        self._assert_wiki_capable(scope)
        return self.repository.create_folder(
            scope,
            name=str(payload.get("name") or ""),
            parent_id=str(payload.get("parent_id") or ""),
            sort_order=int(payload.get("sort_order") or 0),
        )

    def list_folders(self, scope: KnowledgeBaseScope, parent_id: str = "") -> list[dict[str, Any]]:
        self._assert_wiki_capable(scope)
        return [folder.to_dict() for folder in self.repository.list_folders(scope, parent_id=parent_id)]

    def update_folder(self, scope: KnowledgeBaseScope, folder_id: str, payload: dict[str, Any]):
        self._assert_wiki_capable(scope)
        return self.repository.update_folder(
            scope,
            folder_id,
            name=payload.get("name"),
            parent_id=payload.get("parent_id"),
            sort_order=payload.get("sort_order"),
        )

    def delete_empty_folder(self, scope: KnowledgeBaseScope, folder_id: str) -> None:
        self._assert_wiki_capable(scope)
        self.repository.delete_empty_folder(scope, folder_id)

    def move_page(self, scope: KnowledgeBaseScope, slug: str, *, folder_id: str = "", category_path: list[str] | None = None) -> WikiPage:
        current = self.get_page(scope, slug)
        metadata = dict(current.metadata)
        metadata["manual_taxonomy"] = True
        return self.update_page(
            scope,
            slug,
            {"folder_id": folder_id, "category_path": category_path or [], "metadata": metadata},
        )

    def graph(self, scope: KnowledgeBaseScope, *, center_slug: str = "", limit: int = 80) -> dict[str, Any]:
        self._assert_wiki_capable(scope)
        pages, _ = self.repository.list_pages(scope, status="", limit=min(200, max(1, int(limit or 80))))
        by_slug = {page.slug: page for page in pages}
        selected = set(by_slug)
        if center_slug:
            center = normalize_wiki_slug(center_slug)
            selected = {center}
            if center in by_slug:
                selected.update(by_slug[center].in_links)
                selected.update(by_slug[center].out_links)
        bounded = [slug for slug in selected if slug in by_slug][:limit]
        nodes = [
            WikiGraphNode(
                id=by_slug[slug].id,
                slug=slug,
                title=by_slug[slug].title,
                page_type=by_slug[slug].page_type,
                status=by_slug[slug].status,
            ).to_dict()
            for slug in bounded
        ]
        bounded_set = set(bounded)
        edges = []
        for slug in bounded:
            for target in by_slug[slug].out_links:
                if target in bounded_set:
                    edges.append(WikiGraphEdge(source=slug, target=target).to_dict())
        return {"nodes": nodes, "edges": edges, "meta": {"node_count": len(nodes), "edge_count": len(edges), "bounded": len(selected) > len(bounded)}}

    def create_issue(self, scope: KnowledgeBaseScope, payload: dict[str, Any]) -> WikiPageIssue:
        self._assert_wiki_capable(scope)
        return self.repository.create_issue(
            scope,
            slug=str(payload.get("slug") or ""),
            issue_type=str(payload.get("issue_type") or "other"),
            description=str(payload.get("description") or ""),
            suspected_doc_ids=payload.get("suspected_doc_ids") or [],
            suspected_chunk_ids=payload.get("suspected_chunk_ids") or [],
            reported_by=str(payload.get("reported_by") or "user"),
        )

    def list_issues(self, scope: KnowledgeBaseScope, *, slug: str = "", status: str = "open", issue_type: str = "", limit: int = 50) -> list[dict[str, Any]]:
        self._assert_wiki_capable(scope)
        return [issue.to_dict() for issue in self.repository.list_issues(scope, slug=slug, status=status, issue_type=issue_type, limit=limit)]

    def get_issue(self, scope: KnowledgeBaseScope, issue_id: str) -> WikiPageIssue:
        self._assert_wiki_capable(scope)
        return self.repository.get_issue(scope, issue_id)

    def update_issue_status(self, scope: KnowledgeBaseScope, issue_id: str, status: str) -> WikiPageIssue:
        self._assert_wiki_capable(scope)
        return self.repository.update_issue_status(scope, issue_id, status)

    def cleanup_issue(self, scope: KnowledgeBaseScope, issue_id: str) -> dict[str, Any]:
        self._assert_wiki_capable(scope)
        issue = self.repository.get_issue(scope, issue_id)
        page = self.get_page(scope, issue.slug)
        if issue.issue_type == "dead_link":
            self.refresh_links(scope)
        elif issue.issue_type == "missing_source":
            refs = [
                ref.to_dict()
                for ref in page.source_refs
                if self.document_repository is not None
                and self.document_repository.get_document(ref.doc_id, scope) is not None
                and (not ref.chunk_id or self.document_repository.get_chunk(ref.chunk_id, scope) is not None)
            ]
            self.update_page(scope, page.slug, {"source_refs": refs, "chunk_refs": [ref["chunk_id"] for ref in refs if ref.get("chunk_id")]})
        elif issue.issue_type == "taxonomy":
            category = {"summary": "摘要", "entity": "实体", "concept": "概念", "manual": "手工页面"}.get(page.page_type, "知识")
            self.update_page(scope, page.slug, {"category_path": [category]})
        else:
            existing = self.repository.list_proposals(scope, slug=page.slug, status="pending", limit=100)
            proposal = next((item for item in existing if item.reason == issue.description), None)
            if proposal is None:
                proposal = self.repository.create_proposal(
                    scope,
                    action="delete_page" if issue.issue_type == "ungrounded_content" else "write_page",
                    slug=page.slug,
                    title=page.title,
                    payload={"issue_id": issue.id, "issue_type": issue.issue_type},
                    reason=issue.description,
                    created_by="system",
                )
            return {"issue": issue.to_dict(), "proposal": proposal.to_dict(), "cleaned": False}
        resolved = self.repository.update_issue_status(scope, issue.id, "resolved")
        return {"issue": resolved.to_dict(), "proposal": None, "cleaned": True}

    def create_proposal(self, scope: KnowledgeBaseScope, payload: dict[str, Any]) -> WikiPageProposal:
        self._assert_wiki_capable(scope)
        return self.repository.create_proposal(
            scope,
            action=str(payload.get("action") or "write_page"),
            slug=str(payload.get("slug") or payload.get("title") or ""),
            title=str(payload.get("title") or ""),
            content_markdown=str(payload.get("content_markdown") or ""),
            payload=dict(payload.get("payload") or {}),
            source_refs=payload.get("source_refs") or [],
            chunk_refs=payload.get("chunk_refs") or [],
            reason=str(payload.get("reason") or ""),
            created_by=str(payload.get("created_by") or "agent"),
        )

    def list_proposals(self, scope: KnowledgeBaseScope, *, slug: str = "", status: str = "pending", limit: int = 50) -> list[dict[str, Any]]:
        self._assert_wiki_capable(scope)
        return [proposal.to_dict() for proposal in self.repository.list_proposals(scope, slug=slug, status=status, limit=limit)]

    def get_proposal(self, scope: KnowledgeBaseScope, proposal_id: str) -> WikiPageProposal:
        self._assert_wiki_capable(scope)
        return self.repository.get_proposal(scope, proposal_id)

    def reject_proposal(self, scope: KnowledgeBaseScope, proposal_id: str) -> WikiPageProposal:
        self._assert_wiki_capable(scope)
        return self.repository.update_proposal_status(scope, proposal_id, "rejected")

    def apply_proposal(self, scope: KnowledgeBaseScope, proposal_id: str) -> dict[str, Any]:
        self._assert_wiki_capable(scope)
        proposal = self.repository.get_proposal(scope, proposal_id)
        if proposal.status != "pending":
            raise WikiValidationError("Only pending Wiki proposals can be applied")
        payload = dict(proposal.payload or {})
        page: WikiPage | None = None
        if proposal.action == "write_page":
            existing = self.repository.get_page_by_slug(scope, proposal.slug)
            data = {
                "title": proposal.title or payload.get("title") or proposal.slug,
                "content_markdown": proposal.content_markdown or payload.get("content_markdown") or "",
                "summary": payload.get("summary") or "",
                "page_type": payload.get("page_type") or "manual",
                "status": payload.get("status") or "draft",
                "source_refs": [ref.to_dict() for ref in proposal.source_refs],
                "chunk_refs": list(proposal.chunk_refs),
            }
            page = self.update_page(scope, proposal.slug, data) if existing else self.create_page(scope, {"slug": proposal.slug, **data})
        elif proposal.action == "replace_text":
            page = self.get_page(scope, proposal.slug)
            old_text = str(payload.get("old_text") or "")
            new_text = str(payload.get("new_text") or "")
            if not old_text or old_text not in page.content_markdown:
                raise WikiValidationError("Replacement text was not found in the Wiki page")
            page = self.update_page(scope, proposal.slug, {"content_markdown": page.content_markdown.replace(old_text, new_text, 1)})
        elif proposal.action == "rename_page":
            page = self.update_page(
                scope,
                proposal.slug,
                {"slug": payload.get("new_slug") or proposal.title or proposal.slug, "title": proposal.title or payload.get("title")},
            )
        elif proposal.action == "delete_page":
            page = self.archive_page(scope, proposal.slug)
        elif proposal.action == "merge_pages":
            source = self.get_page(scope, str(payload.get("source_slug") or proposal.slug))
            target = self.get_page(scope, str(payload.get("target_slug") or ""))
            target_metadata = dict(target.metadata)
            target_metadata["merged_from"] = list(dict.fromkeys([*target_metadata.get("merged_from", []), source.slug]))
            page = self.update_page(
                scope,
                target.slug,
                {
                    "content_markdown": f"{target.content_markdown.rstrip()}\n\n{source.content_markdown.strip()}".strip(),
                    "source_refs": [*[ref.to_dict() for ref in target.source_refs], *[ref.to_dict() for ref in source.source_refs]],
                    "chunk_refs": [*target.chunk_refs, *source.chunk_refs],
                    "aliases": [*target.aliases, *source.aliases, source.slug, source.title],
                    "metadata": target_metadata,
                },
            )
            self.archive_page(scope, source.slug)
            self.refresh_links(scope)
        applied = self.repository.update_proposal_status(scope, proposal_id, "applied")
        return {"proposal": applied.to_dict(), "page": page.to_dict() if page else None}

    def read_source_doc(self, scope: KnowledgeBaseScope, *, doc_id: str = "", chunk_ids: list[str] | None = None, limit: int = 8) -> dict[str, Any]:
        if self.document_repository is None:
            raise WikiValidationError("Document repository is unavailable")
        chunks = []
        for chunk_id in chunk_ids or []:
            chunk = self.document_repository.get_chunk(chunk_id, scope)
            if chunk:
                chunks.append(chunk)
        if doc_id:
            chunks.extend(self.document_repository.list_chunks_for_documents([doc_id], scope=scope, limit=limit))
        document = self.document_repository.get_document(doc_id, scope) if doc_id else None
        return {
            "document": document or {},
            "chunks": [
                {
                    "chunk_id": item.get("id"),
                    "doc_id": item.get("doc_id"),
                    "title_path": item.get("title_path"),
                    "chunk_type": item.get("chunk_type"),
                    "content": str(item.get("content_markdown") or item.get("content") or "")[:5000],
                }
                for item in chunks[:limit]
            ],
        }

    def generate_draft_for_document(
        self,
        scope: KnowledgeBaseScope,
        doc_id: str,
        *,
        auto_publish: bool | None = None,
        max_source_chunks: int | None = None,
    ) -> dict[str, Any]:
        knowledge_base = self._assert_wiki_capable(scope)
        effective_enabled = bool(self.generation_enabled and knowledge_base.indexing_strategy.wiki_generation_enabled)
        task = self.repository.create_generation_task(
            scope,
            doc_id=doc_id,
            config={
                "enabled": effective_enabled,
                "auto_publish": bool(auto_publish if auto_publish is not None else knowledge_base.indexing_strategy.wiki_auto_publish_enabled),
                "max_pages_per_document": self.generation_max_pages_per_document,
                "max_source_chunks": max_source_chunks or self.generation_max_source_chunks,
                "max_chars": self.generation_max_chars,
            },
        )
        if not effective_enabled:
            task = self.repository.update_generation_task(scope, task.id, status="skipped", error_message="Wiki generation is disabled")
            return {"task": task.to_dict(), "page": None, "proposal": None}
        try:
            self.repository.update_generation_task(scope, task.id, status="running")
            page, proposal = self._generate_summary_page(scope, doc_id, knowledge_base, auto_publish=auto_publish, max_source_chunks=max_source_chunks)
            task = self.repository.update_generation_task(
                scope,
                task.id,
                status="completed",
                page_slug=(page.slug if page is not None else proposal.slug if proposal is not None else ""),
            )
            logger.info(
                "wiki.generation.completed",
                extra={
                    "workspace_id": scope.workspace_id,
                    "knowledge_base_id": scope.knowledge_base_id,
                    "doc_id": doc_id,
                    "page_slug": task.page_slug,
                    "task_id": task.id,
                },
            )
            return {
                "task": task.to_dict(),
                "page": page.to_dict() if page is not None else None,
                "proposal": proposal.to_dict() if proposal is not None else None,
            }
        except Exception as exc:
            message = _sanitize_generation_error(exc)
            task = self.repository.update_generation_task(scope, task.id, status="failed", error_message=message)
            logger.warning(
                "wiki.generation.failed",
                extra={
                    "workspace_id": scope.workspace_id,
                    "knowledge_base_id": scope.knowledge_base_id,
                    "doc_id": doc_id,
                    "task_id": task.id,
                    "error_type": exc.__class__.__name__,
                    "error_message": message,
                },
            )
            return {"task": task.to_dict(), "page": None, "proposal": None}

    def list_generation_tasks(self, scope: KnowledgeBaseScope, *, doc_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
        self._assert_wiki_capable(scope)
        self._reconcile_stale_finalizing_generation_tasks(scope)
        return [task.to_dict() for task in self.repository.list_generation_tasks(scope, doc_id=doc_id, limit=limit)]

    def overview(self, scope: KnowledgeBaseScope) -> dict[str, Any]:
        self._assert_wiki_capable(scope)
        self._reconcile_stale_finalizing_generation_tasks(scope)
        system_pages = []
        for slug in ("index", "log"):
            page = self.repository.get_page_by_slug(scope, slug)
            if page is not None:
                system_pages.append(page.to_dict())
        task_states: dict[str, int] = {}
        for task in self.repository.list_generation_tasks(scope, limit=100):
            task_states[task.status] = task_states.get(task.status, 0) + 1
        return {
            "page_counts": self.repository.page_type_counts(scope),
            "system_pages": system_pages,
            "open_issue_count": len(self.repository.list_issues(scope, status="open", limit=500)),
            "active_task_count": sum(count for status, count in task_states.items() if status in GENERATION_ACTIVE_STATES),
            "task_states": task_states,
        }

    def _reconcile_stale_finalizing_generation_tasks(self, scope: KnowledgeBaseScope) -> None:
        ingest_service = getattr(self, "ingest_service", None)
        processing_repository = getattr(ingest_service, "processing_repository", None)
        if processing_repository is None:
            return
        active_processing = processing_repository.list_tasks(
            scope,
            statuses=PROCESSING_ACTIVE_STATES,
            task_types=WIKI_PROCESSING_TASK_TYPES,
        )
        if active_processing:
            return
        for task in self.repository.list_generation_tasks(scope, limit=100):
            if task.status != "finalizing":
                continue
            try:
                self.repository.update_generation_task(scope, task.id, status="completed", page_slug=task.page_slug)
            except KeyError:
                continue

    def list_logs(self, scope: KnowledgeBaseScope, *, limit: int = 50, cursor: int = 0) -> dict[str, Any]:
        self._assert_wiki_capable(scope)
        items, next_cursor = self.repository.list_logs(scope, limit=limit, cursor=cursor)
        return {"items": items, "next_cursor": next_cursor}

    def list_processing_tasks(self, scope: KnowledgeBaseScope, *, statuses: set[str] | None = None) -> list[dict[str, Any]]:
        self._assert_wiki_capable(scope)
        ingest_service = getattr(self, "ingest_service", None)
        processing_repository = getattr(ingest_service, "processing_repository", None)
        if processing_repository is None:
            return []
        tasks = processing_repository.list_tasks(
            scope,
            statuses=statuses,
            task_types={"wiki.ingest", "wiki.finalize"},
        )
        return [{**task, "attempt_history": processing_repository.list_attempts(str(task["id"]))} for task in tasks]

    def get_processing_task(self, scope: KnowledgeBaseScope, task_id: str) -> dict[str, Any]:
        task, processing_repository = self._scoped_processing_task(scope, task_id)
        return {**task, "attempt_history": processing_repository.list_attempts(str(task["id"]))}

    def retry_processing_task(self, scope: KnowledgeBaseScope, task_id: str) -> dict[str, Any]:
        task, processing_repository = self._scoped_processing_task(scope, task_id)
        retried = processing_repository.retry_dead_letter(str(task["id"]))
        return {**retried, "attempt_history": processing_repository.list_attempts(str(task["id"]))}

    def cancel_processing_task(self, scope: KnowledgeBaseScope, task_id: str) -> dict[str, Any]:
        task, processing_repository = self._scoped_processing_task(scope, task_id)
        if str(task.get("status")) in {"completed", "failed", "canceled", "dead_lettered"}:
            raise WikiValidationError("Only active Wiki processing tasks can be cancelled")
        cancelled = processing_repository.cancel_task(str(task["id"]), reason="Cancelled by operator")
        ingest_service = getattr(self, "ingest_service", None)
        ingest_service.reconcile_cancelled(task, "Cancelled by operator")
        return {**cancelled, "attempt_history": processing_repository.list_attempts(str(task["id"]))}

    def _scoped_processing_task(self, scope: KnowledgeBaseScope, task_id: str):
        self._assert_wiki_capable(scope)
        ingest_service = getattr(self, "ingest_service", None)
        processing_repository = getattr(ingest_service, "processing_repository", None)
        if processing_repository is None:
            raise WikiValidationError("Wiki processing runtime is unavailable")
        task = processing_repository.get_task(task_id)
        if task.get("workspace_id") != scope.workspace_id or task.get("knowledge_base_id") != scope.knowledge_base_id:
            raise KeyError(task_id)
        if task.get("task_type") not in {"wiki.ingest", "wiki.finalize"}:
            raise KeyError(task_id)
        return task, processing_repository

    def mark_source_stale(self, scope: KnowledgeBaseScope, *, doc_id: str, chunk_id: str = "", reason: str = "source_reindexed") -> list[dict[str, Any]]:
        self._assert_wiki_capable(scope)
        pages = self.repository.pages_by_source_ref(scope, doc_id=doc_id, chunk_id=chunk_id)
        updated: list[dict[str, Any]] = []
        for page in pages:
            if page.status in {"archived", "stale"}:
                continue
            metadata = dict(page.metadata or {})
            metadata["stale_reason"] = reason
            metadata["stale_source_doc_id"] = doc_id
            stale_page = self.update_page(scope, page.slug, {"status": "stale", "metadata": metadata})
            self.create_issue(
                scope,
                {
                    "slug": page.slug,
                    "issue_type": "outdated",
                    "description": f"Source document changed or was deleted: {doc_id}",
                    "suspected_doc_ids": [doc_id],
                    "suspected_chunk_ids": [chunk_id] if chunk_id else [],
                    "reported_by": "system",
                },
            )
            updated.append(stale_page.to_dict())
        return updated

    def _generate_summary_page(
        self,
        scope: KnowledgeBaseScope,
        doc_id: str,
        knowledge_base: Any,
        *,
        auto_publish: bool | None = None,
        max_source_chunks: int | None = None,
    ) -> tuple[WikiPage | None, WikiPageProposal | None]:
        if self.document_repository is None:
            raise WikiValidationError("Document repository is unavailable")
        document = self.document_repository.get_document(doc_id, scope)
        if document is None:
            raise KeyError(doc_id)
        limit = min(self.generation_max_source_chunks, max(1, int(max_source_chunks or self.generation_max_source_chunks)))
        chunks = self.document_repository.list_chunks_for_documents(
            [doc_id],
            chunk_types=self.GENERATION_CHUNK_TYPES,
            scope=scope,
            limit=limit,
        )
        if not chunks:
            chunks = self.document_repository.list_chunks_for_documents([doc_id], scope=scope, limit=limit)
        if not chunks:
            issue = self.repository.create_issue(
                scope,
                slug=normalize_wiki_slug(Path(str(document.get("name") or doc_id)).stem),
                issue_type="missing_source",
                description="Wiki draft generation skipped because no indexed source chunks were available.",
                suspected_doc_ids=[doc_id],
                suspected_chunk_ids=[],
                reported_by="system",
            )
            raise WikiValidationError(f"No indexed source chunks available; issue={issue.id}")

        title = _title_from_document(document)
        slug = normalize_wiki_slug(title)
        source_refs = _source_refs_from_chunks(document, chunks)
        chunk_refs = [ref.chunk_id for ref in source_refs if ref.chunk_id]
        aliases = _candidate_aliases(title, chunks)
        summary = _summary_from_chunks(chunks)
        content = _draft_markdown(title, summary, chunks, self.generation_max_chars)
        status = "published" if bool(auto_publish if auto_publish is not None else knowledge_base.indexing_strategy.wiki_auto_publish_enabled) else "draft"
        metadata = {
            "generated": True,
            "generator": "deterministic_summary_v1",
            "source_doc_id": doc_id,
            "source_chunk_count": len(chunks),
            "provenance_warning": "" if chunk_refs else "No chunk refs were available for generated content.",
        }
        existing = self.repository.get_page_by_slug(scope, slug)
        payload = {
            "title": title,
            "slug": slug,
            "page_type": "summary",
            "status": status,
            "content_markdown": content,
            "summary": summary,
            "source_refs": [ref.to_dict() for ref in source_refs],
            "chunk_refs": chunk_refs,
            "aliases": aliases,
            "metadata": metadata,
        }
        if existing is None:
            return self.create_page(scope, payload), None
        if existing.status in {"draft", "stale"}:
            return self.update_page(scope, slug, payload), None
        proposal = self.create_proposal(
            scope,
            {
                "action": "write_page",
                "slug": slug,
                "title": title,
                "content_markdown": content,
                "payload": {key: value for key, value in payload.items() if key not in {"source_refs", "chunk_refs"}},
                "source_refs": [ref.to_dict() for ref in source_refs],
                "chunk_refs": chunk_refs,
                "reason": "Generated Wiki draft from a reindexed source document.",
                "created_by": "wiki_generation",
            },
        )
        self.create_issue(
            scope,
            {
                "slug": slug,
                "issue_type": "outdated",
                "description": "A source document was reindexed and generated a proposed Wiki update.",
                "suspected_doc_ids": [doc_id],
                "suspected_chunk_ids": chunk_refs,
                "reported_by": "system",
            },
        )
        return None, proposal

    def refresh_links(self, scope: KnowledgeBaseScope) -> None:
        pages, _ = self.repository.list_pages(scope, status="", limit=200)
        available = {page.slug: page for page in pages}
        aliases = {
            normalize_wiki_slug(alias): page.slug
            for page in pages
            for alias in page.aliases
            if str(alias).strip()
        }
        inbound: dict[str, list[str]] = {page.slug: [] for page in pages}
        for page in pages:
            def normalize_link(match: re.Match[str]) -> str:
                requested = normalize_wiki_slug(match.group(1))
                label = str(match.group(2) or match.group(1)).strip()
                target = requested if requested in available else aliases.get(requested, "")
                return f"[[{target}|{label}]]" if target else label

            content = self.LINK_PATTERN.sub(normalize_link, page.content_markdown)
            if page.metadata.get("generated"):
                injected = 0
                for target in sorted(pages, key=lambda item: (-len(item.title), item.slug)):
                    if target.slug == page.slug or injected >= 3 or not target.title:
                        continue
                    if f"[[{target.slug}|" in content or target.title not in content:
                        continue
                    content = content.replace(target.title, f"[[{target.slug}|{target.title}]]", 1)
                    injected += 1
            out_links = self.parse_links(content)
            if content != page.content_markdown or tuple(out_links) != page.out_links:
                self.repository.update_page(scope, page.slug, {"content_markdown": content, "out_links": out_links})
            for target in out_links:
                if target in inbound and page.slug not in inbound[target]:
                    inbound[target].append(page.slug)
        for page in pages:
            if tuple(inbound.get(page.slug, [])) != page.in_links:
                self.repository.update_page(scope, page.slug, {"in_links": inbound.get(page.slug, [])})

    def parse_links(self, content_markdown: str) -> list[str]:
        slugs: list[str] = []
        seen: set[str] = set()
        for match in self.LINK_PATTERN.finditer(content_markdown or ""):
            slug = normalize_wiki_slug(match.group(1))
            if slug and slug not in seen:
                slugs.append(slug)
                seen.add(slug)
        return slugs

    def _assert_wiki_capable(self, scope: KnowledgeBaseScope):
        knowledge_base = self.knowledge_base_service.assert_writable(scope)
        if not knowledge_base.indexing_strategy.wiki_enabled:
            raise WikiValidationError("Wiki is not enabled for this knowledge base")
        return knowledge_base

    def _page_search_item(self, page: WikiPage, query: str) -> dict[str, Any]:
        content = page.summary or page.content_markdown
        return {
            "page_id": page.id,
            "slug": page.slug,
            "title": page.title,
            "page_type": page.page_type,
            "status": page.status,
            "summary": page.summary,
            "snippet": _snippet(content, query),
            "matched_query": query,
            "source_refs": [ref.to_dict() for ref in page.source_refs[:5]],
            "chunk_refs": list(page.chunk_refs[:10]),
            "updated_at": page.updated_at,
        }


_WIKI_CJK_QUESTION_STOPWORDS = (
    "请问",
    "帮我",
    "一下",
    "这个",
    "这台",
    "该",
    "此",
    "设备",
    "支持",
    "包含",
    "包括",
    "哪些",
    "什么",
    "是否",
    "有没有",
    "有",
    "能不能",
    "可以",
    "吗",
    "呢",
    "的",
)


def _wiki_search_queries(query: str, *, limit: int = 8) -> list[str]:
    raw = re.sub(r"\s+", " ", str(query or "")).strip()
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n?？!！,，。.;；:：、/\\()[]{}【】\"'“”‘’")
        if len(cleaned) < 2 or cleaned.lower() in seen:
            return
        candidates.append(cleaned)
        seen.add(cleaned.lower())

    add(raw)
    normalized = re.sub(r"[|,，。.;；:：?？!！、/\\()\[\]{}【】\"'“”‘’]+", " ", raw)
    terms = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*|[\u4e00-\u9fff]+", normalized, flags=re.UNICODE)
    for term in terms:
        if re.search(r"[\u4e00-\u9fff]", term):
            compact = term
            for stopword in _WIKI_CJK_QUESTION_STOPWORDS:
                compact = compact.replace(stopword, "")
            add(compact)
            if len(compact) > 2:
                add(compact[-2:])
                add(compact[:2])
        else:
            add(term)
    return candidates[:limit] or [raw]


def _snippet(content: str, query: str, limit: int = 360) -> str:
    text = re.sub(r"\s+", " ", content or "").strip()
    if not text:
        return ""
    needle = (query or "").strip().lower()
    if needle and needle in text.lower():
        index = text.lower().find(needle)
        start = max(0, index - 120)
        return text[start : start + limit]
    return text[:limit]


def _title_from_document(document: dict[str, Any]) -> str:
    name = str(document.get("name") or document.get("storage_path") or document.get("id") or "Wiki Page")
    stem = Path(name).stem or name
    return stem[:120]


def _source_refs_from_chunks(document: dict[str, Any], chunks: list[dict[str, Any]]) -> list[WikiSourceRef]:
    title = _title_from_document(document)
    refs: list[WikiSourceRef] = []
    seen: set[tuple[str, str]] = set()
    for chunk in chunks:
        key = (str(chunk.get("doc_id") or ""), str(chunk.get("id") or ""))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        refs.append(WikiSourceRef(doc_id=key[0], chunk_id=key[1], title=title))
    if not refs and document.get("id"):
        refs.append(WikiSourceRef(doc_id=str(document.get("id")), title=title))
    return refs


def _summary_from_chunks(chunks: list[dict[str, Any]], limit: int = 360) -> str:
    for chunk in chunks:
        text = _clean_chunk_text(chunk)
        if text:
            return text[:limit]
    return "Generated Wiki draft awaiting source review."


def _draft_markdown(title: str, summary: str, chunks: list[dict[str, Any]], max_chars: int) -> str:
    lines = [
        f"# {title}",
        "",
        "## Summary",
        summary,
        "",
        "## Source-Bound Notes",
    ]
    budget = max(1000, int(max_chars or 12000))
    for index, chunk in enumerate(chunks, start=1):
        text = _clean_chunk_text(chunk)
        if not text:
            continue
        heading = str(chunk.get("title_path") or chunk.get("chunk_type") or f"Source {index}")[:160]
        chunk_id = str(chunk.get("id") or "")
        lines.extend(["", f"### {heading}", "", text[:1200], "", f"_Source chunk: `{chunk_id}`_"])
        if len("\n".join(lines)) >= budget:
            break
    lines.extend(["", "## Review Notes", "This draft was generated from indexed source chunks and should be reviewed before relying on it as curated Wiki knowledge."])
    return "\n".join(lines)[:budget]


def _clean_chunk_text(chunk: dict[str, Any]) -> str:
    text = str(chunk.get("content_markdown") or chunk.get("content") or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _candidate_aliases(title: str, chunks: list[dict[str, Any]], limit: int = 8) -> list[str]:
    candidates = [title, Path(title).stem]
    pattern = re.compile(r"[\w][\w .:/\-]{2,80}", flags=re.UNICODE)
    for chunk in chunks[:4]:
        for value in pattern.findall(_clean_chunk_text(chunk)[:1500]):
            clean = re.sub(r"\s+", " ", value).strip(" .:/-")
            if clean and clean not in candidates and len(clean) <= 80:
                candidates.append(clean)
            if len(candidates) >= limit:
                return candidates
    return candidates[:limit]


def _sanitize_generation_error(exc: Exception) -> str:
    message = re.sub(r"\s+", " ", str(exc) or exc.__class__.__name__).strip()
    message = re.sub(r"[A-Za-z]:\\[^\s]+", "<path>", message)
    return message[:800]
