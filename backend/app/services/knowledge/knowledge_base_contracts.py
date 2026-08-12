from __future__ import annotations

from typing import Any, Protocol

from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseScope, Workspace


class KnowledgeBaseRepositoryProtocol(Protocol):
    def ensure_defaults(self) -> tuple[Workspace, KnowledgeBase]: ...

    def create_knowledge_base(self, knowledge_base: KnowledgeBase, set_as_default: bool = False) -> KnowledgeBase: ...

    def list_knowledge_bases(self, workspace_id: str, include_archived: bool = False) -> list[KnowledgeBase]: ...

    def get_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBase | None: ...

    def get_default_knowledge_base(self, workspace_id: str) -> KnowledgeBase | None: ...

    def update_knowledge_base(self, knowledge_base_id: str, changes: dict[str, Any]) -> KnowledgeBase: ...

    def set_knowledge_base_status(self, knowledge_base_id: str, status: str) -> KnowledgeBase: ...

    def set_default_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBase: ...


class ScopedEvidenceRepositoryProtocol(Protocol):
    def get_chunk(self, chunk_id: str, scope: KnowledgeBaseScope | None = None) -> dict[str, Any] | None: ...

    def search_keyword_chunks(
        self,
        query: str,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> list[dict[str, Any]]: ...


class ScopedVectorProviderProtocol(Protocol):
    def query_dense(self, question: str, top_k: int, scope: KnowledgeBaseScope) -> list[dict[str, Any]]: ...

    def query_bm25(self, question: str, top_k: int, scope: KnowledgeBaseScope) -> list[dict[str, Any]]: ...


class KnowledgeBaseServiceProtocol(Protocol):
    def resolve_scope(
        self,
        knowledge_base_ids: list[str] | tuple[str, ...] | None = None,
        document_ids: list[str] | tuple[str, ...] | None = None,
    ) -> KnowledgeBaseScope: ...

    def effective_config(self, knowledge_base_id: str) -> dict[str, Any]: ...
