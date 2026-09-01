from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

from app.models.knowledge_base import (
    EffectiveKnowledgeBaseConfig,
    IndexingStrategy,
    KnowledgeBase,
    KnowledgeBaseScope,
    ProviderReferences,
    utc_now_iso,
)
from app.services.knowledge.knowledge_base_contracts import KnowledgeBaseRepositoryProtocol
from app.services.storage.postgres import PostgresIntegrityError


class KnowledgeBaseValidationError(ValueError):
    pass


SUPPORTED_KNOWLEDGE_BASE_TYPES = {"document", "faq", "wiki"}


class KnowledgeBaseService:
    def __init__(
        self,
        repository: KnowledgeBaseRepositoryProtocol,
        default_providers: ProviderReferences | None = None,
        supported_provider_refs: dict[str, set[str]] | None = None,
    ):
        self.repository = repository
        self.default_providers = default_providers or ProviderReferences()
        self.supported_provider_refs = supported_provider_refs or {}
        default_knowledge_base = self.repository.get_knowledge_base(self.repository.defaults.knowledge_base_id)
        if default_knowledge_base is not None:
            resolved_defaults = self._resolve_provider_config(ProviderReferences())
            if default_knowledge_base.provider_config.effective != resolved_defaults.effective:
                self.repository.update_knowledge_base(
                    default_knowledge_base.id,
                    {"provider_config_json": json.dumps(resolved_defaults.to_dict())},
                )

    @property
    def default_workspace_id(self) -> str:
        return self.repository.defaults.workspace_id

    @property
    def default_knowledge_base_id(self) -> str:
        default = self.repository.get_default_knowledge_base(self.default_workspace_id)
        return default.id if default is not None else self.repository.defaults.knowledge_base_id

    def create(
        self,
        name: str,
        description: str = "",
        knowledge_base_type: str = "document",
        is_default: bool = False,
        workspace_id: str | None = None,
        indexing_strategy: dict[str, Any] | None = None,
        provider_config: dict[str, Any] | None = None,
    ) -> KnowledgeBase:
        workspace_id = workspace_id or self.default_workspace_id
        workspace = self.repository.get_workspace(workspace_id)
        if workspace is None or workspace.status != "active":
            raise KnowledgeBaseValidationError("Workspace does not exist or is archived")
        clean_name = name.strip()
        if not clean_name:
            raise KnowledgeBaseValidationError("Knowledge base name cannot be empty")
        clean_type = knowledge_base_type.strip().lower() or "document"
        if clean_type not in SUPPORTED_KNOWLEDGE_BASE_TYPES:
            raise KnowledgeBaseValidationError("Unsupported knowledge base type")
        requested = ProviderReferences.from_dict(provider_config)
        effective_config = self._resolve_provider_config(requested)
        now = utc_now_iso()
        knowledge_base = KnowledgeBase(
            id=uuid4().hex,
            workspace_id=workspace_id,
            name=clean_name,
            description=description.strip(),
            type=clean_type,
            is_default=is_default,
            indexing_strategy=(
                IndexingStrategy.from_dict(indexing_strategy)
                if indexing_strategy
                else IndexingStrategy.default_for_type(clean_type)
            ),
            provider_config=effective_config,
            created_at=now,
            updated_at=now,
        )
        try:
            return self.repository.create_knowledge_base(knowledge_base, set_as_default=is_default)
        except (sqlite3.IntegrityError, PostgresIntegrityError) as exc:
            raise KnowledgeBaseValidationError("A knowledge base with this name already exists") from exc

    def list(self, workspace_id: str | None = None, include_archived: bool = False) -> list[KnowledgeBase]:
        return self.repository.list_knowledge_bases(workspace_id or self.default_workspace_id, include_archived)

    def get(self, knowledge_base_id: str, allow_archived: bool = True) -> KnowledgeBase:
        knowledge_base = self.repository.get_knowledge_base(knowledge_base_id)
        if knowledge_base is None or (not allow_archived and knowledge_base.status != "active"):
            raise KeyError(knowledge_base_id)
        return knowledge_base

    def update(
        self,
        knowledge_base_id: str,
        name: str | None = None,
        description: str | None = None,
        is_default: bool | None = None,
        indexing_strategy: dict[str, Any] | None = None,
        provider_config: dict[str, Any] | None = None,
    ) -> KnowledgeBase:
        current = self.get(knowledge_base_id, allow_archived=False)
        changes: dict[str, Any] = {}
        if name is not None:
            clean_name = name.strip()
            if not clean_name:
                raise KnowledgeBaseValidationError("Knowledge base name cannot be empty")
            changes["name"] = clean_name
        if description is not None:
            changes["description"] = description.strip()
        if indexing_strategy is not None:
            changes["indexing_strategy_json"] = json.dumps(IndexingStrategy.from_dict(indexing_strategy).to_dict())
        if provider_config is not None:
            config = self._resolve_provider_config(ProviderReferences.from_dict(provider_config))
            changes["provider_config_json"] = json.dumps(config.to_dict())
        try:
            updated = self.repository.update_knowledge_base(current.id, changes)
            if is_default:
                updated = self.repository.set_default_knowledge_base(current.id)
            return updated
        except (sqlite3.IntegrityError, PostgresIntegrityError) as exc:
            raise KnowledgeBaseValidationError("A knowledge base with this name already exists") from exc
        except ValueError as exc:
            raise KnowledgeBaseValidationError(str(exc)) from exc

    def archive(self, knowledge_base_id: str) -> KnowledgeBase:
        current = self.get(knowledge_base_id, allow_archived=False)
        if current.is_default:
            raise KnowledgeBaseValidationError("Default knowledge base cannot be archived")
        return self.repository.set_knowledge_base_status(knowledge_base_id, "archived")

    def restore(self, knowledge_base_id: str) -> KnowledgeBase:
        self.get(knowledge_base_id, allow_archived=True)
        return self.repository.set_knowledge_base_status(knowledge_base_id, "active")

    def resolve_scope(
        self,
        knowledge_base_ids: list[str] | tuple[str, ...] | None = None,
        document_ids: list[str] | tuple[str, ...] | None = None,
    ) -> KnowledgeBaseScope:
        requested = tuple(dict.fromkeys(knowledge_base_ids or ()))
        compatibility_default = not requested
        selected = requested or (self.default_knowledge_base_id,)
        workspaces = set()
        for knowledge_base_id in selected:
            knowledge_base = self.get(knowledge_base_id, allow_archived=False)
            workspaces.add(knowledge_base.workspace_id)
        if len(workspaces) != 1:
            raise KnowledgeBaseValidationError("Selected knowledge bases must belong to one workspace")
        return KnowledgeBaseScope(
            workspace_id=workspaces.pop(),
            selected_knowledge_base_ids=selected,
            document_ids=tuple(document_ids or ()),
            compatibility_default=compatibility_default,
        )

    def assert_writable(self, scope: KnowledgeBaseScope) -> KnowledgeBase:
        knowledge_base = self.get(scope.knowledge_base_id, allow_archived=False)
        if knowledge_base.workspace_id != scope.workspace_id:
            raise KnowledgeBaseValidationError("Knowledge base scope does not match workspace")
        return knowledge_base

    def effective_config(self, knowledge_base_id: str) -> dict[str, Any]:
        return self.get(knowledge_base_id).provider_config.to_dict()

    def resolve_indexing_strategy(self, scope: KnowledgeBaseScope) -> IndexingStrategy:
        return self.assert_writable(scope).indexing_strategy

    def _resolve_provider_config(self, requested: ProviderReferences) -> EffectiveKnowledgeBaseConfig:
        effective_values = self.default_providers.to_dict()
        inactive: list[str] = []
        requested_values = requested.to_dict()
        for field_name, requested_value in requested_values.items():
            if requested_value == "default":
                continue
            supported = self.supported_provider_refs.get(field_name, set())
            if requested_value in supported:
                effective_values[field_name] = requested_value
            else:
                inactive.append(field_name)
        return EffectiveKnowledgeBaseConfig(
            requested=requested,
            effective=ProviderReferences.from_dict(effective_values),
            inactive_overrides=tuple(inactive),
        )
