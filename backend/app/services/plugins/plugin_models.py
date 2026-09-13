from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


PLUGIN_CHAT_MODES = {"quick", "reasoning", "wiki", "rag_wiki"}
SECRET_FIELD_TYPES = {"secret", "password", "token"}


@dataclass(frozen=True)
class PluginConfigField:
    name: str
    label: str
    field_type: str = "text"
    required: bool = False
    description: str = ""
    placeholder: str = ""
    options: tuple[str, ...] = ()
    secret: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "type": self.field_type,
            "required": self.required,
            "description": self.description,
            "placeholder": self.placeholder,
            "options": list(self.options),
            "secret": self.secret,
        }


@dataclass(frozen=True)
class PluginDescriptor:
    id: str
    name: str
    description: str
    category: str
    mapped_tools: tuple[str, ...]
    supported_modes: tuple[str, ...]
    safety_labels: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    config_fields: tuple[PluginConfigField, ...] = ()
    requires_server_config: tuple[str, ...] = ()
    default_enabled: bool = False
    always_unavailable_reason: str = ""


@dataclass
class PluginSetting:
    workspace_id: str
    plugin_id: str
    enabled: bool
    mode_bindings: tuple[str, ...] = ()
    config: dict[str, Any] = field(default_factory=dict)
    status_metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class PluginRuntimeEnvironment:
    web_search_enabled: bool = False
    web_search_endpoint: str = ""
    web_fetch_enabled: bool = False
    web_fetch_allowed_domains: tuple[str, ...] = ()
    data_analysis_enabled: bool = False
    database_query_enabled: bool = False
    database_allowed_sources: dict[str, str] = field(default_factory=dict)
    skills_enabled: bool = False
    wiki_tools_enabled: bool = False
    wiki_maintenance_tools_enabled: bool = False


@dataclass(frozen=True)
class PluginRecord:
    id: str
    name: str
    description: str
    category: str
    enabled: bool
    availability: str
    configuration_status: str
    supported_modes: tuple[str, ...]
    enabled_modes: tuple[str, ...]
    safety_labels: tuple[str, ...]
    permissions: tuple[str, ...]
    mapped_tools: tuple[str, ...]
    config_schema: tuple[PluginConfigField, ...]
    config: dict[str, Any]
    warnings: tuple[str, ...] = ()
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "enabled": self.enabled,
            "availability": self.availability,
            "configuration_status": self.configuration_status,
            "supported_modes": list(self.supported_modes),
            "enabled_modes": list(self.enabled_modes),
            "safety_labels": list(self.safety_labels),
            "permissions": list(self.permissions),
            "mapped_tools": list(self.mapped_tools),
            "config_schema": [field.to_dict() for field in self.config_schema],
            "config": dict(self.config),
            "warnings": list(self.warnings),
            "updated_at": self.updated_at,
        }
