from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from app.models.knowledge_base import KnowledgeBaseScope


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[3]


class PromptTemplateError(ValueError):
    pass


@dataclass(frozen=True)
class AgentPromptTemplate:
    id: str
    name: str
    description: str
    mode: str
    content: str
    default: bool = False


@dataclass(frozen=True)
class ContextPromptTemplate:
    id: str
    name: str
    description: str
    content: str
    default: bool = False
    has_knowledge_base: bool = True


@dataclass(frozen=True)
class GenericPromptTemplate:
    id: str
    name: str
    description: str
    content: str
    placeholders: tuple[str, ...] = ()
    modes: tuple[str, ...] = ()
    default_language: str = "zh-CN"
    required: bool = False


class AgentPromptCatalog:
    def __init__(self, templates: list[AgentPromptTemplate]):
        if not templates:
            raise PromptTemplateError("Prompt template catalog is empty")
        self._templates = {template.id: template for template in templates}
        if len(self._templates) != len(templates):
            raise PromptTemplateError("Prompt template ids must be unique")
        defaults = [template for template in templates if template.default]
        self.default_id = defaults[0].id if defaults else templates[0].id

    @classmethod
    def load(cls, path: str | Path, *, allow_builtin_default: bool = True) -> "AgentPromptCatalog":
        template_path = Path(path)
        if not template_path.is_absolute():
            template_path = _backend_root() / template_path
        if not template_path.exists():
            if allow_builtin_default:
                fallback = _backend_root() / "config" / "prompt_templates" / "agent_system_prompt.yaml"
                if fallback.exists() and fallback != template_path:
                    return cls.load(fallback, allow_builtin_default=False)
            raise PromptTemplateError(f"Prompt template file not found: {template_path}")
        try:
            parsed = yaml.safe_load(template_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise PromptTemplateError(f"Failed to load prompt template file: {exc}") from exc
        raw_templates = parsed.get("templates")
        if not isinstance(raw_templates, list):
            raise PromptTemplateError("Prompt template file must contain a templates list")
        templates: list[AgentPromptTemplate] = []
        for item in raw_templates:
            if not isinstance(item, dict):
                raise PromptTemplateError("Each prompt template must be a mapping")
            template_id = str(item.get("id") or "").strip()
            mode = str(item.get("mode") or "").strip()
            content = str(item.get("content") or "").strip()
            if not template_id or not mode or not content:
                raise PromptTemplateError("Each prompt template requires id, mode, and content")
            templates.append(
                AgentPromptTemplate(
                    id=template_id,
                    name=str(item.get("name") or template_id),
                    description=str(item.get("description") or ""),
                    mode=mode,
                    content=content,
                    default=bool(item.get("default")),
                )
            )
        return cls(templates)

    def get(self, template_id: str | None = None) -> AgentPromptTemplate:
        selected = template_id or self.default_id
        template = self._templates.get(selected)
        if template is None:
            raise PromptTemplateError(f"Unknown agent prompt template id: {selected}")
        return template

    def render(
        self,
        template_id: str | None = None,
        *,
        language: str = "zh-CN",
        web_search_enabled: bool = False,
        knowledge_bases: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        skills: list[dict[str, Any]] | None = None,
    ) -> str:
        template = self.get(template_id)
        rendered = template.content
        replacements = {
            "{{language}}": language,
            "{{web_search_status}}": "enabled" if web_search_enabled else "disabled",
            "{{bound_knowledge_bases}}": format_bound_knowledge_bases(knowledge_bases or []),
            "{{available_tools}}": format_available_tools(tools or []),
            "{{available_skills}}": format_available_skills(skills or []),
        }
        for placeholder, value in replacements.items():
            rendered = rendered.replace(placeholder, value)
        return rendered


class ContextPromptCatalog:
    def __init__(self, templates: list[ContextPromptTemplate]):
        if not templates:
            raise PromptTemplateError("Context template catalog is empty")
        self._templates = {template.id: template for template in templates}
        if len(self._templates) != len(templates):
            raise PromptTemplateError("Context template ids must be unique")
        defaults = [template for template in templates if template.default]
        self.default_id = defaults[0].id if defaults else templates[0].id

    @classmethod
    def load(cls, path: str | Path, *, allow_builtin_default: bool = True) -> "ContextPromptCatalog":
        template_path = Path(path)
        if not template_path.is_absolute():
            template_path = _backend_root() / template_path
        if not template_path.exists():
            if allow_builtin_default:
                fallback = _backend_root() / "config" / "prompt_templates" / "context_template.yaml"
                if fallback.exists() and fallback != template_path:
                    return cls.load(fallback, allow_builtin_default=False)
            raise PromptTemplateError(f"Context template file not found: {template_path}")
        try:
            parsed = yaml.safe_load(template_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise PromptTemplateError(f"Failed to load context template file: {exc}") from exc
        raw_templates = parsed.get("templates")
        if not isinstance(raw_templates, list):
            raise PromptTemplateError("Context template file must contain a templates list")
        templates: list[ContextPromptTemplate] = []
        for item in raw_templates:
            if not isinstance(item, dict):
                raise PromptTemplateError("Each context template must be a mapping")
            template_id = str(item.get("id") or "").strip()
            content = str(item.get("content") or "").strip()
            if not template_id or not content:
                raise PromptTemplateError("Each context template requires id and content")
            templates.append(
                ContextPromptTemplate(
                    id=template_id,
                    name=str(item.get("name") or template_id),
                    description=str(item.get("description") or ""),
                    content=content,
                    default=bool(item.get("default")),
                    has_knowledge_base=bool(item.get("has_knowledge_base", True)),
                )
            )
        return cls(templates)

    def get(self, template_id: str | None = None) -> ContextPromptTemplate:
        selected = template_id or self.default_id
        template = self._templates.get(selected)
        if template is None:
            raise PromptTemplateError(f"Unknown context template id: {selected}")
        return template

    def render(
        self,
        template_id: str | None = None,
        *,
        query: str,
        language: str = "zh-CN",
        contexts: list[dict[str, Any]] | str | None = None,
        conversation_context: str | dict[str, Any] | None = None,
        memory_context: str | None = None,
        temporary_attachments: list[dict[str, Any]] | None = None,
        knowledge_base_scope: dict[str, Any] | None = None,
        knowledge_bases: list[dict[str, Any]] | None = None,
        answer_guidance: str = "",
        now: datetime | None = None,
    ) -> str:
        template = self.get(template_id)
        current = now or datetime.now()
        replacements = {
            "{{query}}": query,
            "{{language}}": language,
            "{{contexts}}": format_contexts(contexts),
            "{{conversation_context}}": format_optional_block("conversation_context", conversation_context),
            "{{memory_context}}": format_optional_block("memory_context", memory_context),
            "{{temporary_attachments}}": format_optional_block("temporary_attachments", temporary_attachments),
            "{{knowledge_base_scope}}": format_optional_block("knowledge_base_scope", knowledge_base_scope),
            "{{bound_knowledge_bases}}": format_bound_knowledge_bases(knowledge_bases or []),
            "{{answer_guidance}}": format_optional_block("answer_guidance", answer_guidance),
            "{{current_time}}": current.strftime("%Y-%m-%d %H:%M:%S"),
            "{{current_week}}": current.strftime("%A"),
        }
        rendered = template.content
        for placeholder, value in replacements.items():
            rendered = rendered.replace(placeholder, value)
        return rendered.strip()


class PromptTemplateCatalog:
    def __init__(self, templates: list[GenericPromptTemplate], *, required_ids: set[str] | None = None):
        if not templates:
            raise PromptTemplateError("Prompt template catalog is empty")
        self._templates = {template.id: template for template in templates}
        if len(self._templates) != len(templates):
            raise PromptTemplateError("Prompt template ids must be unique")
        missing = sorted((required_ids or set()) - set(self._templates))
        if missing:
            raise PromptTemplateError(f"Missing required prompt template ids: {missing}")
        for template in templates:
            _validate_declared_placeholders(template.id, template.content, template.placeholders)

    @classmethod
    def load_directory(
        cls,
        path: str | Path = "config/prompt_templates",
        *,
        required_ids: set[str] | None = None,
    ) -> "PromptTemplateCatalog":
        template_dir = Path(path)
        if not template_dir.is_absolute():
            template_dir = _backend_root() / template_dir
        if not template_dir.exists():
            raise PromptTemplateError(f"Prompt template directory not found: {template_dir}")
        templates: list[GenericPromptTemplate] = []
        for template_path in sorted([*template_dir.glob("*.yaml"), *template_dir.glob("*.yml")]):
            parsed = _load_yaml(template_path)
            raw_templates = parsed.get("templates")
            if not isinstance(raw_templates, list):
                raise PromptTemplateError(f"Prompt template file must contain a templates list: {template_path}")
            for item in raw_templates:
                templates.append(_generic_template_from_mapping(item, template_path))
        return cls(templates, required_ids=required_ids)

    def get(self, template_id: str) -> GenericPromptTemplate:
        template = self._templates.get(template_id)
        if template is None:
            raise PromptTemplateError(f"Unknown prompt template id: {template_id}")
        return template

    def render(self, template_id: str, variables: dict[str, Any] | None = None, *, mode: str | None = None) -> str:
        template = self.get(template_id)
        if mode and template.modes and mode not in template.modes:
            raise PromptTemplateError(f"Prompt template {template_id} does not support mode {mode}")
        values = dict(variables or {})
        values.setdefault("language", template.default_language)
        missing = [name for name in template.placeholders if f"{{{{{name}}}}}" in template.content and name not in values]
        if missing:
            raise PromptTemplateError(f"Prompt template {template_id} missing variables: {missing}")
        rendered = template.content
        for key, value in values.items():
            if _is_secret_key(key):
                raise PromptTemplateError(f"Prompt template {template_id} refuses secret variable: {key}")
            rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
        return rendered.strip()

    def ids(self) -> list[str]:
        return sorted(self._templates)


def format_bound_knowledge_bases(items: list[dict[str, Any]]) -> str:
    if not items:
        return "<bound_knowledge_bases />"
    lines = ["<bound_knowledge_bases>"]
    for item in items:
        capabilities = [str(value) for value in (item.get("capabilities") or ["chunks"]) if str(value).strip()]
        lines.append(
            f'<knowledge_base id="{_xml(str(item.get("id", "")))}" '
            f'name="{_xml(str(item.get("name", "")))}" '
            f'type="{_xml(str(item.get("type", "document")))}" '
            f'capabilities="{_xml(",".join(capabilities or ["chunks"]))}" '
            f'doc_count="{int(item.get("doc_count", item.get("documents", 0)) or 0)}">'
        )
        description = str(item.get("description") or "").strip()
        if description:
            lines.append(f"<description>{_xml(description[:500])}</description>")
        lines.append("</knowledge_base>")
    lines.append("</bound_knowledge_bases>")
    return "\n".join(lines)


def format_contexts(contexts: list[dict[str, Any]] | str | None) -> str:
    if contexts is None or contexts == "":
        return "<contexts />"
    if isinstance(contexts, str):
        return f"<contexts>\n{contexts.strip()}\n</contexts>" if contexts.strip() else "<contexts />"
    if not contexts:
        return "<contexts />"
    lines = ["<contexts>"]
    for index, item in enumerate(contexts, start=1):
        source = _xml(str(item.get("source") or item.get("name") or f"source-{index}"))
        score = item.get("score", "")
        content = str(item.get("content") or item.get("text") or item.get("preview") or "").strip()
        lines.append(f'<context index="{index}" source="{source}" score="{_xml(str(score))}">')
        if content:
            lines.append(_xml(content[:4000]))
        lines.append("</context>")
    lines.append("</contexts>")
    return "\n".join(lines)


def format_optional_block(name: str, value: Any) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return f"<{name} />"
    if isinstance(value, str):
        content = value.strip()
    else:
        import json

        content = json.dumps(value, ensure_ascii=False, default=str)
    if not content:
        return f"<{name} />"
    return f"<{name}>\n{_xml(content[:6000])}\n</{name}>"


def format_available_tools(items: list[dict[str, Any]]) -> str:
    if not items:
        return "Available tools: none"
    lines = ["Available tools:"]
    for item in items:
        lines.append(f"- {item.get('name')}: {item.get('description', '')}")
    return "\n".join(lines)


def format_available_skills(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    lines = [
        "Available skills:",
        "If a skill matches the user request, call read_skill before applying it.",
    ]
    for item in items:
        lines.append(f"- {item.get('name')}: {item.get('description', '')}")
    return "\n".join(lines)


def scope_to_prompt_kbs(scope: KnowledgeBaseScope, knowledge_base_service: Any | None = None) -> list[dict[str, Any]]:
    result = []
    for kb_id in scope.selected_knowledge_base_ids:
        info = {"id": kb_id, "name": kb_id, "type": "document", "doc_count": 0, "description": "", "capabilities": ["chunks"]}
        if knowledge_base_service is not None:
            try:
                kb = knowledge_base_service.get(kb_id)
                data = kb.to_dict() if hasattr(kb, "to_dict") else dict(kb)
                aggregate = data.get("aggregate") if isinstance(data.get("aggregate"), dict) else {}
                info.update(
                    {
                        "id": data.get("id", kb_id),
                        "name": data.get("name", kb_id),
                        "type": data.get("type", "document"),
                        "description": data.get("description", ""),
                        "doc_count": aggregate.get("document_count", data.get("doc_count", data.get("documents", 0))),
                        "capabilities": _knowledge_base_capabilities(data),
                    }
                )
            except Exception:
                pass
        result.append(info)
    return result


def _knowledge_base_capabilities(data: dict[str, Any]) -> list[str]:
    strategy = data.get("indexing_strategy") if isinstance(data.get("indexing_strategy"), dict) else {}
    capabilities: list[str] = []
    if bool(strategy.get("dense_enabled", True)) or bool(strategy.get("keyword_enabled", True)):
        capabilities.append("chunks")
    if bool(strategy.get("wiki_enabled")) or str(data.get("type") or "").lower() == "wiki":
        capabilities.append("wiki")
    if bool(strategy.get("graph_enabled")):
        capabilities.append("graph")
    return capabilities or ["chunks"]


def _xml(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise PromptTemplateError(f"Failed to load prompt template file {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PromptTemplateError(f"Prompt template file must be a mapping: {path}")
    return parsed


def _generic_template_from_mapping(item: Any, path: Path) -> GenericPromptTemplate:
    if not isinstance(item, dict):
        raise PromptTemplateError(f"Each prompt template must be a mapping: {path}")
    template_id = str(item.get("id") or "").strip()
    content = str(item.get("content") or "").strip()
    if not template_id or not content:
        raise PromptTemplateError(f"Each prompt template requires id and content: {path}")
    placeholders = tuple(str(value).strip() for value in item.get("placeholders", []) if str(value).strip())
    modes = tuple(str(value).strip() for value in item.get("modes", []) if str(value).strip())
    return GenericPromptTemplate(
        id=template_id,
        name=str(item.get("name") or template_id),
        description=str(item.get("description") or ""),
        content=content,
        placeholders=placeholders,
        modes=modes,
        default_language=str(item.get("default_language") or "zh-CN"),
        required=bool(item.get("required", False)),
    )


def _validate_declared_placeholders(template_id: str, content: str, placeholders: tuple[str, ...]) -> None:
    declared = set(placeholders)
    used = set(__import__("re").findall(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}", content))
    undeclared = sorted(used - declared)
    if undeclared:
        raise PromptTemplateError(f"Prompt template {template_id} has undeclared placeholders: {undeclared}")


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in ("secret", "token", "api_key", "apikey", "password", "authorization"))
