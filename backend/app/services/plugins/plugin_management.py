from __future__ import annotations

import re
import time
from typing import Any, Protocol
from urllib.parse import urlparse

from app.models.agent_runtime import AgentRuntimeConfig
from app.services.agent.agent_runtime_tools import HTTPJSONSearchProvider
from app.services.infrastructure.logging_config import sanitize_payload
from app.services.plugins.plugin_models import (
    PLUGIN_CHAT_MODES,
    PluginConfigField,
    PluginDescriptor,
    PluginRecord,
    PluginRuntimeEnvironment,
    PluginSetting,
    SECRET_FIELD_TYPES,
)


class PluginValidationError(ValueError):
    pass


class PluginNotFoundError(KeyError):
    pass


class PluginSettingsRepository(Protocol):
    def list_settings(self, workspace_id: str) -> list[PluginSetting]:
        ...

    def get_setting(self, workspace_id: str, plugin_id: str) -> PluginSetting | None:
        ...

    def upsert_setting(
        self,
        workspace_id: str,
        plugin_id: str,
        *,
        enabled: bool,
        mode_bindings: tuple[str, ...],
        config: dict[str, Any],
        status_metadata: dict[str, Any] | None = None,
    ) -> PluginSetting:
        ...


class InMemoryPluginSettingsRepository:
    def __init__(self):
        self._settings: dict[tuple[str, str], PluginSetting] = {}

    def list_settings(self, workspace_id: str) -> list[PluginSetting]:
        return [setting for key, setting in sorted(self._settings.items()) if key[0] == workspace_id]

    def get_setting(self, workspace_id: str, plugin_id: str) -> PluginSetting | None:
        return self._settings.get((workspace_id, plugin_id))

    def upsert_setting(
        self,
        workspace_id: str,
        plugin_id: str,
        *,
        enabled: bool,
        mode_bindings: tuple[str, ...],
        config: dict[str, Any],
        status_metadata: dict[str, Any] | None = None,
    ) -> PluginSetting:
        now = "memory"
        current = self._settings.get((workspace_id, plugin_id))
        setting = PluginSetting(
            workspace_id=workspace_id,
            plugin_id=plugin_id,
            enabled=enabled,
            mode_bindings=tuple(mode_bindings),
            config=dict(config),
            status_metadata=dict(status_metadata or {}),
            created_at=current.created_at if current else now,
            updated_at=now,
        )
        self._settings[(workspace_id, plugin_id)] = setting
        return setting


class PluginManagementService:
    def __init__(
        self,
        repository: PluginSettingsRepository | None = None,
        *,
        workspace_id: str = "default-workspace",
        runtime_environment: PluginRuntimeEnvironment | None = None,
    ):
        self.repository = repository or InMemoryPluginSettingsRepository()
        self.workspace_id = workspace_id
        self.runtime_environment = runtime_environment or PluginRuntimeEnvironment()
        self._descriptors = {descriptor.id: descriptor for descriptor in _default_descriptors()}

    def list_plugins(self, workspace_id: str | None = None) -> dict[str, Any]:
        workspace_id = workspace_id or self.workspace_id
        settings = {item.plugin_id: item for item in self.repository.list_settings(workspace_id)}
        items = [self._record_for(descriptor, settings.get(descriptor.id)) for descriptor in self._descriptors.values()]
        return {
            "items": [item.to_dict() for item in items],
            "aggregate": {
                "total": len(items),
                "enabled": sum(1 for item in items if item.enabled),
                "available": sum(1 for item in items if item.availability == "available"),
                "needs_configuration": sum(1 for item in items if item.configuration_status == "needs_configuration"),
            },
        }

    def get_plugin(self, plugin_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        descriptor = self._descriptor(plugin_id)
        setting = self.repository.get_setting(workspace_id or self.workspace_id, descriptor.id)
        return self._record_for(descriptor, setting).to_dict()

    def update_plugin(self, plugin_id: str, payload: dict[str, Any], workspace_id: str | None = None) -> dict[str, Any]:
        workspace_id = workspace_id or self.workspace_id
        descriptor = self._descriptor(plugin_id)
        current = self.repository.get_setting(workspace_id, descriptor.id)
        enabled = bool(payload.get("enabled", current.enabled if current else descriptor.default_enabled))
        mode_bindings = self._validate_modes(descriptor, payload.get("enabled_modes", current.mode_bindings if current else descriptor.supported_modes))
        existing_config = current.config if current else {}
        config = self._validate_config(descriptor, {**existing_config, **dict(payload.get("config") or {})})
        status_metadata = {"updated_by": "plugin_management"}
        setting = self.repository.upsert_setting(
            workspace_id,
            descriptor.id,
            enabled=enabled,
            mode_bindings=mode_bindings,
            config=config,
            status_metadata=status_metadata,
        )
        return self._record_for(descriptor, setting).to_dict()

    def test_plugin(self, plugin_id: str, payload: dict[str, Any] | None = None, workspace_id: str | None = None) -> dict[str, Any]:
        descriptor = self._descriptor(plugin_id)
        record = self._record_for(descriptor, self.repository.get_setting(workspace_id or self.workspace_id, descriptor.id))
        started = time.perf_counter()
        status = "success"
        summary = "插件配置可用。"
        details: dict[str, Any] = {"plugin_id": descriptor.id}
        if record.availability != "available":
            status = "unavailable"
            summary = "；".join(record.warnings) or "插件不可用。"
        elif record.configuration_status == "needs_configuration":
            status = "unavailable"
            summary = "插件需要完成必填配置后才能使用。"
        elif descriptor.id == "web_search":
            query = str((payload or {}).get("query") or "health check").strip() or "health check"
            endpoint = str(record.config.get("endpoint") or self.runtime_environment.web_search_endpoint or "").strip()
            if endpoint and bool((payload or {}).get("execute", False)):
                try:
                    results = HTTPJSONSearchProvider(endpoint, timeout_seconds=5.0).search(query, top_k=1)
                    details = {"query": query, "result_count": len(results)}
                    summary = f"搜索服务返回 {len(results)} 条结果。"
                except Exception as exc:
                    status = "failed"
                    summary = "搜索服务测试失败。"
                    details = {"error_type": exc.__class__.__name__}
            else:
                summary = "端点格式和配置有效。"
        return {
            "plugin_id": descriptor.id,
            "status": status,
            "success": status == "success",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "summary": summary,
            "details": sanitize_payload(details, limit=1000) if isinstance(details, dict) else {},
        }

    def activity(self, limit: int = 20) -> dict[str, Any]:
        return {"items": [], "limit": max(1, min(int(limit or 20), 100)), "source": "unavailable"}

    def apply_to_agent_runtime_config(self, config: AgentRuntimeConfig) -> AgentRuntimeConfig:
        settings = {item.plugin_id: item for item in self.repository.list_settings(self.workspace_id)}
        next_config = config
        for descriptor in self._descriptors.values():
            setting = settings.get(descriptor.id)
            if setting is None:
                continue
            record = self._record_for(descriptor, setting)
            if descriptor.id == "web_search" and record.enabled and record.availability == "available":
                endpoint = str(record.config.get("endpoint") or next_config.web_search_endpoint or "").strip()
                next_config.web_search_enabled = bool(endpoint)
                next_config.web_search_endpoint = endpoint
            if descriptor.id == "web_fetch" and record.enabled and record.availability == "available":
                domains = tuple(_csv_values(record.config.get("allowed_domains"))) or next_config.web_fetch_allowed_domains
                next_config.web_fetch_enabled = bool(domains)
                next_config.web_fetch_allowed_domains = domains
            if descriptor.id == "data_analysis":
                next_config.data_analysis_enabled = bool(record.enabled and record.availability == "available")
            if descriptor.id == "database_query" and record.enabled and record.availability == "available":
                next_config.database_query_enabled = True
                sources = record.config.get("sources")
                if isinstance(sources, dict):
                    next_config.database_allowed_sources = {str(k): str(v) for k, v in sources.items() if str(k).strip() and str(v).strip()}
            if descriptor.id == "skills":
                next_config.skills_enabled = bool(record.enabled and record.availability == "available")
            if descriptor.id == "wiki":
                next_config.wiki_tools_enabled = bool(record.enabled and record.availability == "available")
            next_config = _apply_tool_bindings(next_config, descriptor, record.enabled and record.availability == "available", setting.mode_bindings)
        return next_config

    def _descriptor(self, plugin_id: str) -> PluginDescriptor:
        clean = str(plugin_id or "").strip()
        descriptor = self._descriptors.get(clean)
        if descriptor is None:
            raise PluginNotFoundError(clean)
        return descriptor

    def _record_for(self, descriptor: PluginDescriptor, setting: PluginSetting | None) -> PluginRecord:
        available, warnings = self._availability(descriptor)
        config = dict(setting.config) if setting else {}
        effective_config = self._config_with_env_defaults(descriptor, config)
        configuration_status = self._configuration_status(descriptor, effective_config, available)
        enabled = bool(setting.enabled if setting else descriptor.default_enabled)
        if available != "available":
            enabled = False
        enabled_modes = tuple(setting.mode_bindings if setting else descriptor.supported_modes)
        return PluginRecord(
            id=descriptor.id,
            name=descriptor.name,
            description=descriptor.description,
            category=descriptor.category,
            enabled=enabled,
            availability=available,
            configuration_status=configuration_status,
            supported_modes=descriptor.supported_modes,
            enabled_modes=enabled_modes,
            safety_labels=descriptor.safety_labels,
            permissions=descriptor.permissions,
            mapped_tools=descriptor.mapped_tools,
            config_schema=descriptor.config_fields,
            config=_mask_config(descriptor, effective_config),
            warnings=tuple(warnings),
            updated_at=setting.updated_at if setting else "",
        )

    def _availability(self, descriptor: PluginDescriptor) -> tuple[str, list[str]]:
        if descriptor.always_unavailable_reason:
            return "unavailable", [descriptor.always_unavailable_reason]
        env = self.runtime_environment
        if descriptor.id == "knowledge":
            return "available", []
        if descriptor.id == "wiki":
            return ("available", []) if env.wiki_tools_enabled else ("unavailable", ["服务器配置已禁用 Wiki 运行时工具。"])
        if descriptor.id == "data_analysis":
            return ("available", []) if env.data_analysis_enabled else ("unavailable", ["服务器配置已禁用数据分析。"])
        if descriptor.id == "database_query":
            if not env.database_query_enabled:
                return "unavailable", ["服务器配置已禁用数据库查询。"]
            if not env.database_allowed_sources:
                return "unavailable", ["尚未配置只读数据库源。"]
        if descriptor.id == "web_fetch":
            if not env.web_fetch_enabled:
                return "unavailable", ["服务器配置已禁用网页抓取。"]
            if not env.web_fetch_allowed_domains:
                return "unavailable", ["服务器配置中尚未设置网页抓取域名白名单。"]
        if descriptor.id == "skills":
            return ("available", []) if env.skills_enabled else ("unavailable", ["服务器配置已禁用运行时技能。"])
        return "available", []

    def _configuration_status(self, descriptor: PluginDescriptor, config: dict[str, Any], available: str) -> str:
        if available != "available":
            return "unavailable"
        try:
            self._validate_config(descriptor, config)
        except PluginValidationError:
            return "needs_configuration"
        return "configured"

    def _config_with_env_defaults(self, descriptor: PluginDescriptor, config: dict[str, Any]) -> dict[str, Any]:
        env = self.runtime_environment
        merged = dict(config)
        if descriptor.id == "web_search":
            merged.setdefault("endpoint", env.web_search_endpoint)
        if descriptor.id == "web_fetch":
            merged.setdefault("allowed_domains", ", ".join(env.web_fetch_allowed_domains))
        if descriptor.id == "database_query":
            merged.setdefault("sources", dict(env.database_allowed_sources))
        return merged

    def _validate_modes(self, descriptor: PluginDescriptor, value: Any) -> tuple[str, ...]:
        modes = tuple(dict.fromkeys(_csv_values(value)))
        if not modes:
            return descriptor.supported_modes
        invalid = [mode for mode in modes if mode not in PLUGIN_CHAT_MODES or mode not in descriptor.supported_modes]
        if invalid:
            raise PluginValidationError(f"不支持的插件模式绑定：{', '.join(invalid)}")
        return modes

    def _validate_config(self, descriptor: PluginDescriptor, config: dict[str, Any]) -> dict[str, Any]:
        clean = dict(config or {})
        for field in descriptor.config_fields:
            value = clean.get(field.name)
            if field.required and not str(value or "").strip():
                raise PluginValidationError(f"{field.label} 为必填项")
            if field.name == "endpoint" and str(value or "").strip():
                parsed = urlparse(str(value).strip())
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise PluginValidationError("搜索端点必须是 http(s) URL")
                clean[field.name] = str(value).strip()
            if field.name == "allowed_domains":
                domains = _validate_domains(value)
                env_domains = set(self.runtime_environment.web_fetch_allowed_domains)
                if env_domains and not set(domains).issubset(env_domains):
                    raise PluginValidationError("允许域名必须在服务器白名单内")
                clean[field.name] = ", ".join(domains)
            if field.name == "sources" and value is not None:
                if not isinstance(value, dict):
                    raise PluginValidationError("数据源必须是对象")
                env_allowed_sources = self.runtime_environment.database_allowed_sources
                env_sources = set(env_allowed_sources)
                requested = {str(key): str(path) for key, path in value.items() if str(key).strip() and str(path).strip()}
                if env_sources and not set(requested).issubset(env_sources):
                    raise PluginValidationError("数据库源必须在服务器白名单内")
                if env_allowed_sources:
                    requested = {key: env_allowed_sources[key] for key in requested}
                clean[field.name] = requested
        return clean


def _default_descriptors() -> tuple[PluginDescriptor, ...]:
    return (
        PluginDescriptor(
            id="knowledge",
            name="知识库检索",
            description="检索并读取当前范围内的知识库证据。",
            category="knowledge",
            mapped_tools=("knowledge_search", "grep_chunks", "list_knowledge_chunks", "get_document_info", "query_knowledge_graph"),
            supported_modes=("reasoning", "rag_wiki"),
            safety_labels=("只读", "范围受限"),
            permissions=("读取已选择知识库的分块内容和文档元数据。",),
            default_enabled=True,
        ),
        PluginDescriptor(
            id="wiki",
            name="Wiki 工具",
            description="检索并读取已生成的 Wiki 知识页面。",
            category="knowledge",
            mapped_tools=("wiki_search", "wiki_read_page", "wiki_read_source_doc", "wiki_flag_issue"),
            supported_modes=("wiki", "rag_wiki", "reasoning"),
            safety_labels=("以只读为主", "范围受限"),
            permissions=("读取 Wiki 页面和来源引用。", "在运行时策略允许时标记 Wiki 问题。"),
            default_enabled=True,
        ),
        PluginDescriptor(
            id="web_search",
            name="网页搜索",
            description="通过已配置的 HTTP JSON 服务进行网页搜索。",
            category="external",
            mapped_tools=("web_search",),
            supported_modes=("reasoning", "rag_wiki"),
            safety_labels=("外部网络", "受限"),
            permissions=("将搜索查询发送给已配置的搜索服务。",),
            config_fields=(PluginConfigField("endpoint", "搜索端点", "url", True, "支持 q= 查询参数的 HTTP JSON 端点。"),),
        ),
        PluginDescriptor(
            id="web_fetch",
            name="网页抓取",
            description="从白名单网页中抓取受限文本内容。",
            category="external",
            mapped_tools=("web_fetch",),
            supported_modes=("reasoning", "rag_wiki"),
            safety_labels=("外部网络", "白名单"),
            permissions=("从已配置域名抓取网页文本。",),
            config_fields=(PluginConfigField("allowed_domains", "允许域名", "csv", True, "用逗号分隔的域名白名单。"),),
        ),
        PluginDescriptor(
            id="data_analysis",
            name="数据分析",
            description="在一次对话中分析受限的内联 JSON 记录。",
            category="analysis",
            mapped_tools=("data_analysis",),
            supported_modes=("reasoning",),
            safety_labels=("只读", "受限"),
            permissions=("分析用户提供的结构化记录。",),
        ),
        PluginDescriptor(
            id="database_query",
            name="数据库查询",
            description="对服务器白名单内的 SQLite 数据源执行只读查询。",
            category="data",
            mapped_tools=("database_query",),
            supported_modes=("reasoning",),
            safety_labels=("只读", "白名单"),
            permissions=("按行数限制读取已配置的 SQLite 数据源。",),
            config_fields=(PluginConfigField("sources", "允许数据源", "object", False, "数据源名称到服务器本地数据库路径的映射。"),),
        ),
        PluginDescriptor(
            id="skills",
            name="运行时技能",
            description="读取已配置的运行时技能说明。",
            category="skills",
            mapped_tools=("read_skill",),
            supported_modes=("reasoning",),
            safety_labels=("只读",),
            permissions=("从已配置的技能路径读取预加载技能文件。",),
        ),
        PluginDescriptor(
            id="execute_skill",
            name="技能执行",
            description="在安全沙箱可用时执行运行时技能。",
            category="skills",
            mapped_tools=("execute_skill",),
            supported_modes=("reasoning",),
            safety_labels=("已禁用", "专用"),
            permissions=("当前构建未授予执行权限。",),
            always_unavailable_reason="尚未配置安全沙箱，因此技能执行不可用。",
        ),
    )


def _apply_tool_bindings(config: AgentRuntimeConfig, descriptor: PluginDescriptor, enabled: bool, modes: tuple[str, ...]) -> AgentRuntimeConfig:
    tools = descriptor.mapped_tools
    mode_attrs = {
        "quick": "quick_enabled_tools",
        "reasoning": "enabled_tools",
        "wiki": "wiki_enabled_tools",
        "rag_wiki": "rag_wiki_enabled_tools",
    }
    for mode, attr in mode_attrs.items():
        current = tuple(getattr(config, attr))
        if mode not in descriptor.supported_modes:
            continue
        stripped = tuple(item for item in current if item not in tools)
        if enabled and mode in modes:
            stripped = tuple(dict.fromkeys((*stripped, *tools)))
        setattr(config, attr, stripped)
    return config


def _mask_config(descriptor: PluginDescriptor, config: dict[str, Any]) -> dict[str, Any]:
    secret_fields = {field.name for field in descriptor.config_fields if field.secret or field.field_type in SECRET_FIELD_TYPES}
    masked: dict[str, Any] = {}
    for key, value in config.items():
        masked[key] = "********" if key in secret_fields and str(value or "") else value
    return masked


def _csv_values(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _validate_domains(value: Any) -> tuple[str, ...]:
    domains = tuple(dict.fromkeys(domain.lower() for domain in _csv_values(value)))
    pattern = re.compile(r"^[a-z0-9.-]+$")
    invalid = [domain for domain in domains if not pattern.match(domain) or ".." in domain or domain.startswith(".")]
    if invalid:
        raise PluginValidationError(f"无效域名：{invalid[0]}")
    return domains
