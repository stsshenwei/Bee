from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal


WorkspaceStatus = Literal["active", "archived"]
KnowledgeBaseStatus = Literal["active", "archived"]
KnowledgeBaseType = Literal["document", "faq", "wiki"]


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    description: str = ""
    status: WorkspaceStatus = "active"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IndexingStrategy:
    dense_enabled: bool = True
    keyword_enabled: bool = True
    graph_enabled: bool = False
    wiki_enabled: bool = False
    wiki_generation_enabled: bool = True
    wiki_auto_publish_enabled: bool = False

    @property
    def needs_embedding(self) -> bool:
        return self.dense_enabled or self.keyword_enabled

    @property
    def needs_chunks(self) -> bool:
        return self.needs_embedding or self.graph_enabled or self.wiki_enabled

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> IndexingStrategy:
        value = value or {}
        return cls(
            dense_enabled=bool(value.get("dense_enabled", True)),
            keyword_enabled=bool(value.get("keyword_enabled", True)),
            graph_enabled=bool(value.get("graph_enabled", False)),
            wiki_enabled=bool(value.get("wiki_enabled", False)),
            wiki_generation_enabled=bool(value.get("wiki_generation_enabled", True)),
            wiki_auto_publish_enabled=bool(value.get("wiki_auto_publish_enabled", False)),
        )

    @classmethod
    def default_for_type(cls, knowledge_base_type: str) -> IndexingStrategy:
        if str(knowledge_base_type or "").strip().lower() == "wiki":
            return cls(
                dense_enabled=False,
                keyword_enabled=False,
                graph_enabled=False,
                wiki_enabled=True,
                wiki_generation_enabled=True,
                wiki_auto_publish_enabled=False,
            )
        return cls()


@dataclass(frozen=True)
class ProviderReferences:
    parser: str = "default"
    embedding: str = "default"
    reranker: str = "default"
    vector_store: str = "default"
    enrichment: str = "default"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> ProviderReferences:
        value = value or {}
        return cls(**{field_name: str(value.get(field_name, "default")) for field_name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class EffectiveKnowledgeBaseConfig:
    requested: ProviderReferences = field(default_factory=ProviderReferences)
    effective: ProviderReferences = field(default_factory=ProviderReferences)
    inactive_overrides: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested.to_dict(),
            "effective": self.effective.to_dict(),
            "inactive_overrides": list(self.inactive_overrides),
        }


@dataclass(frozen=True)
class KnowledgeBaseAggregate:
    document_count: int = 0
    indexed_chunk_count: int = 0
    processing_count: int = 0
    failed_count: int = 0
    wiki_page_count: int = 0
    wiki_issue_count: int = 0
    reset_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class KnowledgeBase:
    id: str
    workspace_id: str
    name: str
    description: str = ""
    type: KnowledgeBaseType = "document"
    is_default: bool = False
    status: KnowledgeBaseStatus = "active"
    indexing_strategy: IndexingStrategy = field(default_factory=IndexingStrategy)
    provider_config: EffectiveKnowledgeBaseConfig = field(default_factory=EffectiveKnowledgeBaseConfig)
    aggregate: KnowledgeBaseAggregate = field(default_factory=KnowledgeBaseAggregate)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "is_default": self.is_default,
            "status": self.status,
            "indexing_strategy": self.indexing_strategy.to_dict(),
            "provider_config": self.provider_config.to_dict(),
            "aggregate": self.aggregate.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class KnowledgeBaseScope:
    workspace_id: str
    selected_knowledge_base_ids: tuple[str, ...]
    document_ids: tuple[str, ...] = ()
    compatibility_default: bool = False

    def __post_init__(self) -> None:
        workspace_id = self.workspace_id.strip()
        kb_ids = tuple(dict.fromkeys(item.strip() for item in self.selected_knowledge_base_ids if item.strip()))
        doc_ids = tuple(dict.fromkeys(item.strip() for item in self.document_ids if item.strip()))
        if not workspace_id:
            raise ValueError("KnowledgeBaseScope requires workspace_id")
        if not kb_ids:
            raise ValueError("KnowledgeBaseScope requires at least one knowledge_base_id")
        object.__setattr__(self, "workspace_id", workspace_id)
        object.__setattr__(self, "selected_knowledge_base_ids", kb_ids)
        object.__setattr__(self, "document_ids", doc_ids)

    @property
    def knowledge_base_id(self) -> str:
        if len(self.selected_knowledge_base_ids) != 1:
            raise ValueError("Operation requires exactly one knowledge base")
        return self.selected_knowledge_base_ids[0]

    def contains(self, workspace_id: str, knowledge_base_id: str) -> bool:
        return workspace_id == self.workspace_id and knowledge_base_id in self.selected_knowledge_base_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "knowledge_base_ids": list(self.selected_knowledge_base_ids),
            "document_ids": list(self.document_ids),
            "compatibility_default": self.compatibility_default,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> KnowledgeBaseScope:
        return cls(
            workspace_id=str(value.get("workspace_id", "")),
            selected_knowledge_base_ids=tuple(value.get("knowledge_base_ids") or ()),
            document_ids=tuple(value.get("document_ids") or ()),
            compatibility_default=bool(value.get("compatibility_default", False)),
        )


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
