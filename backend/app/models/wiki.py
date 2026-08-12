from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.models.knowledge_base import utc_now_iso


WikiPageType = Literal["summary", "entity", "concept", "synthesis", "manual", "index", "log"]
WikiPageStatus = Literal["draft", "published", "stale", "archived"]
WikiIssueType = Literal[
    "incorrect",
    "outdated",
    "missing_source",
    "conflict",
    "dead_link",
    "duplicate_page",
    "ungrounded_content",
    "taxonomy",
    "other",
]
WikiIssueStatus = Literal["open", "resolved", "wontfix"]
WikiProposalAction = Literal["write_page", "replace_text", "rename_page", "delete_page", "merge_pages"]
WikiProposalStatus = Literal["pending", "applied", "rejected"]
WikiGenerationTaskStatus = Literal[
    "pending",
    "queued",
    "running",
    "retrying",
    "finalizing",
    "completed",
    "failed",
    "cancelled",
    "dead_lettered",
    "skipped",
]


@dataclass(frozen=True)
class WikiSourceRef:
    doc_id: str
    chunk_id: str = ""
    title: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "WikiSourceRef":
        value = value or {}
        return cls(
            doc_id=str(value.get("doc_id") or "").strip(),
            chunk_id=str(value.get("chunk_id") or "").strip(),
            title=str(value.get("title") or "").strip(),
        )


@dataclass(frozen=True)
class WikiPage:
    id: str
    workspace_id: str
    knowledge_base_id: str
    slug: str
    title: str
    page_type: WikiPageType = "summary"
    status: WikiPageStatus = "draft"
    content_markdown: str = ""
    summary: str = ""
    parent_slug: str = ""
    folder_id: str = ""
    category_path: tuple[str, ...] = ()
    wiki_path: str = ""
    depth: int = 0
    sort_order: int = 0
    source_refs: tuple[WikiSourceRef, ...] = ()
    chunk_refs: tuple[str, ...] = ()
    in_links: tuple[str, ...] = ()
    out_links: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "knowledge_base_id": self.knowledge_base_id,
            "slug": self.slug,
            "title": self.title,
            "page_type": self.page_type,
            "status": self.status,
            "content_markdown": self.content_markdown,
            "summary": self.summary,
            "parent_slug": self.parent_slug,
            "folder_id": self.folder_id,
            "category_path": list(self.category_path),
            "wiki_path": self.wiki_path,
            "depth": self.depth,
            "sort_order": self.sort_order,
            "source_refs": [item.to_dict() for item in self.source_refs],
            "chunk_refs": list(self.chunk_refs),
            "in_links": list(self.in_links),
            "out_links": list(self.out_links),
            "aliases": list(self.aliases),
            "metadata": dict(self.metadata),
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class WikiFolder:
    id: str
    workspace_id: str
    knowledge_base_id: str
    parent_id: str = ""
    name: str = ""
    path: str = ""
    depth: int = 0
    sort_order: int = 0
    page_count: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WikiPageIssue:
    id: str
    workspace_id: str
    knowledge_base_id: str
    slug: str
    issue_type: WikiIssueType = "other"
    description: str = ""
    suspected_doc_ids: tuple[str, ...] = ()
    suspected_chunk_ids: tuple[str, ...] = ()
    status: WikiIssueStatus = "open"
    reported_by: str = "user"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["suspected_doc_ids"] = list(self.suspected_doc_ids)
        data["suspected_chunk_ids"] = list(self.suspected_chunk_ids)
        return data


@dataclass(frozen=True)
class WikiPageProposal:
    id: str
    workspace_id: str
    knowledge_base_id: str
    action: WikiProposalAction
    slug: str
    status: WikiProposalStatus = "pending"
    title: str = ""
    content_markdown: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    source_refs: tuple[WikiSourceRef, ...] = ()
    chunk_refs: tuple[str, ...] = ()
    reason: str = ""
    created_by: str = "agent"
    created_at: str = ""
    updated_at: str = ""
    applied_at: str = ""
    rejected_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "knowledge_base_id": self.knowledge_base_id,
            "action": self.action,
            "slug": self.slug,
            "status": self.status,
            "title": self.title,
            "content_markdown": self.content_markdown,
            "payload": dict(self.payload),
            "source_refs": [item.to_dict() for item in self.source_refs],
            "chunk_refs": list(self.chunk_refs),
            "reason": self.reason,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "applied_at": self.applied_at,
            "rejected_at": self.rejected_at,
        }


@dataclass(frozen=True)
class WikiGenerationTask:
    id: str
    workspace_id: str
    knowledge_base_id: str
    doc_id: str
    status: WikiGenerationTaskStatus = "pending"
    page_slug: str = ""
    error_message: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    created_at: str = ""
    updated_at: str = ""
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WikiGraphNode:
    id: str
    slug: str
    title: str
    page_type: str = ""
    status: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class WikiGraphEdge:
    source: str
    target: str
    relation: str = "wiki_link"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def wiki_now_iso() -> str:
    return utc_now_iso()
