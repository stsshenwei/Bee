from __future__ import annotations

import copy
import json
import hashlib
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from app.models.agent_runtime import (
    RuntimeStateDelta,
    RuntimeToolError,
    RuntimeToolResult,
    ToolExecutionClass,
)
from app.models.knowledge_base import KnowledgeBaseScope
from app.services.infrastructure.logging_config import get_trace_id, sanitize_payload, truncate_text

logger = logging.getLogger(__name__)

TOOL_ERROR_HINT = "\n\n[Analyze the error above and try a different approach.]"


class RuntimeTool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]
    execution_class: ToolExecutionClass

    def execute(self, arguments: dict[str, Any], context: "RuntimeToolContext") -> RuntimeToolResult:
        ...


@dataclass
class RuntimeToolContext:
    question: str
    scope: KnowledgeBaseScope
    rag_service: Any
    graph_retriever: Any | None = None
    skills_manager: Any | None = None
    tool_config: Mapping[str, Any] = field(default_factory=dict)
    state: Mapping[str, Any] = field(default_factory=dict)

    def snapshot(self) -> "RuntimeToolContext":
        state = copy.deepcopy(dict(self.state))
        return RuntimeToolContext(
            question=self.question,
            scope=self.scope,
            rag_service=self.rag_service,
            graph_retriever=self.graph_retriever,
            skills_manager=self.skills_manager,
            tool_config=MappingProxyType(dict(self.tool_config)),
            state=MappingProxyType(state),
        )


class ToolRegistry:
    def __init__(self, *, max_output_chars: int = 6000):
        self._tools: dict[str, RuntimeTool] = {}
        self.max_output_chars = max(1, int(max_output_chars or 6000))

    def register(self, tool: RuntimeTool) -> None:
        if tool.name in self._tools:
            logger.warning("agent_runtime.tool.duplicate_rejected", extra={"tool": tool.name, "trace_id": get_trace_id()})
            return
        self._tools[tool.name] = tool

    def get(self, name: str) -> RuntimeTool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        return sorted(self._tools)

    def metadata(self) -> list[dict[str, str]]:
        return [
            {
                "name": name,
                "description": self._tools[name].description,
                "execution_class": self.execution_class(name).value,
            }
            for name in self.list_tools()
        ]

    def execution_class(self, name: str) -> ToolExecutionClass:
        tool = self.get(name)
        value = getattr(tool, "execution_class", ToolExecutionClass.EXCLUSIVE)
        try:
            return value if isinstance(value, ToolExecutionClass) else ToolExecutionClass(str(value))
        except ValueError:
            return ToolExecutionClass.EXCLUSIVE

    def function_definitions(self, enabled_tools: tuple[str, ...] | list[str] | set[str] | None = None) -> list[dict[str, Any]]:
        allowed = set(enabled_tools) if enabled_tools is not None else None
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in (self._tools[name] for name in self.list_tools() if allowed is None or name in allowed)
        ]

    def prepare(
        self,
        name: str,
        arguments: dict[str, Any] | str | None,
    ) -> tuple[dict[str, Any] | None, RuntimeToolResult | None]:
        tool = self.get(name)
        if tool is None:
            error = RuntimeToolError("tool_not_found", f"tool not found: {name}", fatal=False)
            return None, RuntimeToolResult(
                success=False,
                error=f"{error.message}{TOOL_ERROR_HINT}",
                structured_error=error,
                metadata={"status": "unavailable", "error_code": error.code},
            )
        try:
            args = _parse_arguments(arguments)
            args = _coerce_arguments(args, tool.parameters)
        except Exception as exc:
            error = RuntimeToolError("invalid_arguments", str(exc), fatal=False)
            return None, RuntimeToolResult(
                success=False,
                error=f"{error.message}{TOOL_ERROR_HINT}",
                structured_error=error,
                metadata={"status": "unavailable", "error_code": error.code},
            )
        validation_errors = _validate_arguments(args, tool.parameters)
        if validation_errors:
            error = RuntimeToolError("validation_failed", "; ".join(validation_errors), fatal=False)
            return None, RuntimeToolResult(
                success=False,
                error=f"{error.message}{TOOL_ERROR_HINT}",
                structured_error=error,
                metadata={
                    "validation_errors": validation_errors,
                    "status": "unavailable",
                    "error_code": error.code,
                },
            )
        return args, None

    def execute_prepared(self, name: str, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        started = time.perf_counter()
        logger.info("agent_runtime.tool.start", extra={"tool": name, "trace_id": get_trace_id()})
        tool = self.get(name)
        if tool is None:
            error = RuntimeToolError("tool_not_found", f"tool not found: {name}", fatal=False)
            return RuntimeToolResult(
                success=False,
                error=f"{error.message}{TOOL_ERROR_HINT}",
                structured_error=error,
            )
        try:
            result = tool.execute(arguments, context)
        except Exception as exc:
            logger.exception("agent_runtime.tool.failed", extra={"tool": name, "trace_id": get_trace_id()})
            message = str(exc)
            fatal = "knowledge-base scope" in message.lower() or "authorization" in message.lower()
            error = RuntimeToolError(
                "request_scope_failed" if fatal else "tool_execution_failed",
                message,
                fatal=fatal,
            )
            return RuntimeToolResult(
                success=False,
                error=f"{error.message}{TOOL_ERROR_HINT}",
                structured_error=error,
                metadata={"error_code": error.code},
            )
        result.output = _truncate_output(result.output or result.observation or result.error, self.max_output_chars)
        result.metadata = sanitize_payload(result.metadata, limit=1024) if isinstance(result.metadata, dict) else {}
        result.metadata.setdefault("tool", name)
        result.metadata.setdefault("execution_class", self.execution_class(name).value)
        result.metadata.setdefault("duration_ms", int((time.perf_counter() - started) * 1000))
        result.metadata.setdefault("status", "completed" if result.success else "unavailable")
        if result.error:
            result.metadata.setdefault("error_class", "ToolError")
        logger.info(
            "agent_runtime.tool.done",
            extra={"tool": name, "success": result.success, "trace_id": get_trace_id(), "source_chunks": len(result.source_chunk_ids)},
        )
        return result

    def execute(self, name: str, arguments: dict[str, Any] | str | None, context: RuntimeToolContext) -> RuntimeToolResult:
        prepared, error = self.prepare(name, arguments)
        if error is not None:
            return error
        return self.execute_prepared(name, prepared or {}, context)

    def cleanup(self) -> None:
        for tool in self._tools.values():
            cleanup = getattr(tool, "cleanup", None)
            if callable(cleanup):
                cleanup()


class ThinkingTool:
    name = "thinking"
    execution_class = ToolExecutionClass.PARALLEL_SAFE
    description = "Record a concise user-safe public thought or reflection status without exposing hidden chain-of-thought."
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "A concise user-safe status summary."},
            "phase": {"type": "string", "description": "Public phase label such as initial_scan, deep_read, reflection, or final_check."},
            "validity": {"type": "string", "description": "Public evidence validity summary."},
            "gap": {"type": "string", "description": "Public evidence gap, if any."},
            "correction_query": {"type": "string", "description": "A follow-up knowledge-base query that can repair the gap."},
            "completion_status": {"type": "string", "description": "sufficient, needs_more_evidence, insufficient, or complete."},
            "source_chunk_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        summary = truncate_text(str(arguments.get("summary") or "Analyzing the question."), 240)
        thought = {
            "phase": truncate_text(str(arguments.get("phase") or "reflection"), 80),
            "summary": summary,
            "validity": truncate_text(str(arguments.get("validity") or ""), 240),
            "gap": truncate_text(str(arguments.get("gap") or ""), 240),
            "correction_query": truncate_text(str(arguments.get("correction_query") or ""), 240),
            "completion_status": truncate_text(str(arguments.get("completion_status") or ""), 80),
            "source_chunk_ids": _string_list(arguments.get("source_chunk_ids")),
        }
        replacements = {}
        if thought["gap"]:
            replacements["reflection_gap"] = thought["gap"]
        if thought["correction_query"]:
            replacements["correction_query"] = thought["correction_query"]
        if thought["completion_status"]:
            replacements["reflection_completion_status"] = thought["completion_status"]
        return RuntimeToolResult(
            success=True,
            output=json.dumps(thought, ensure_ascii=False),
            observation=summary,
            metadata=thought,
            source_chunk_ids=thought["source_chunk_ids"],
            state_delta=RuntimeStateDelta(
                append_values={"thinking": [thought]},
                replace_values=replacements,
            ),
        )


class TodoWriteTool:
    name = "todo_write"
    execution_class = ToolExecutionClass.SERIAL
    description = "Track a concise plan for multi-step evidence gathering."
    parameters = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short plan items.",
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        items = [truncate_text(str(item), 160) for item in arguments.get("items", []) if str(item).strip()]
        output = "\n".join(f"- {item}" for item in items[:12]) or "No plan items recorded."
        return RuntimeToolResult(
            success=True,
            output=output,
            observation=f"记录了 {len(items[:12])} 个计划项",
            metadata={"todo_count": len(items[:12])},
            state_delta=RuntimeStateDelta(replace_values={"todos": items[:12]}),
        )


class KnowledgeSearchTool:
    name = "knowledge_search"
    execution_class = ToolExecutionClass.PARALLEL_SAFE
    description = "Semantic/hybrid search over the selected knowledge bases."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Maximum candidates."},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        query = str(arguments.get("query") or context.question).strip()
        top_k = int(arguments.get("top_k") or 8)
        hits, debug = _call_scoped_with_debug(
            context.rag_service,
            "hybrid_retrieve_hits",
            query,
            scope=context.scope,
        )
        hits = list(hits or [])[:top_k]
        items = [_hit_summary(hit, index) for index, hit in enumerate(hits, start=1)]
        candidate_ids = _candidate_ids(items)
        source_titles = _source_titles(items)
        return RuntimeToolResult(
            success=True,
            output=json.dumps({"query": query, "results": items}, ensure_ascii=False),
            observation=f"找到 {len(items)} 条语义候选",
            metadata={
                "result_count": len(items),
                "doc_count": len(source_titles),
                "query": query,
                "retrieval_debug": debug,
            },
            source_chunk_ids=candidate_ids,
            source_titles=source_titles,
            candidate_ids=candidate_ids,
            state_delta=RuntimeStateDelta(
                candidate_ids=candidate_ids,
                source_titles=source_titles,
                flags={"semantic_search_performed": True},
                debug={"tool": self.name, "query": query, "retrieval": debug},
            ),
            debug=debug,
        )


class GrepChunksTool:
    name = "grep_chunks"
    execution_class = ToolExecutionClass.PARALLEL_SAFE
    description = (
        "Keyword search over selected knowledge-base chunks. For one search objective, make one call and pack "
        "the 2-3 highest-value synonyms or aliases into a single simple alternation query such as "
        "'risk control system|risk control platform|Enterprise Risk'. Use structured fields only when genuinely "
        "distinct hard constraints cannot be represented by one packed query."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Preferred input: one simple alternation query containing 2-3 high-value terms separated by |.",
            },
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Bounded query variants including useful aliases, abbreviations, translations, and field-name variants.",
            },
            "required_terms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional anchors derived from the requested relation, action, or hard constraints.",
            },
            "match_mode": {
                "type": "string",
                "enum": ["any_query", "any_query_with_required_terms", "any_query_with_optional_required_terms"],
            },
            "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        plan = _normalize_grep_arguments(arguments, context.question)
        if plan.get("error"):
            return RuntimeToolResult(
                success=False,
                error=f"{plan['error']}{TOOL_ERROR_HINT}",
                observation=str(plan["error"]),
                metadata={"validation_errors": [plan["error"]], "status": "unavailable"},
            )
        queries = list(plan["queries"])
        top_k = int(plan["top_k"])
        hit_map: dict[str, dict[str, Any]] = {}
        for query in queries:
            hits = _call_scoped(context.rag_service, "keyword_retrieve_hits", query, top_k=top_k, scope=context.scope)[:top_k]
            for hit in hits:
                key = _hit_identity(hit)
                current = hit_map.get(key)
                if current is None:
                    current = dict(hit)
                    current["metadata"] = dict(hit.get("metadata", {}) or {})
                    current["metadata"]["matched_grep_queries"] = []
                    hit_map[key] = current
                current["metadata"].setdefault("matched_grep_queries", [])
                current["metadata"]["matched_grep_queries"].append(query)
                current["keyword_score"] = max(
                    float(current.get("keyword_score", 0.0) or 0.0),
                    float(hit.get("keyword_score", hit.get("bm25_score", 0.0)) or 0.0),
                )
        hits = sorted(hit_map.values(), key=lambda hit: float(hit.get("keyword_score", 0.0) or 0.0), reverse=True)[:top_k]
        snippet_query = " | ".join(list(plan["display_queries"])[:4])
        items = [_hit_summary(hit, index, include_snippet=True, query=snippet_query) for index, hit in enumerate(hits, start=1)]
        candidate_ids = _candidate_ids(items)
        source_titles = _source_titles(items)
        return RuntimeToolResult(
            success=True,
            output=json.dumps({"queries": plan["display_queries"], "matches": items}, ensure_ascii=False),
            observation=f"Found {len(items)} keyword matches.",
            metadata={
                "result_count": len(items),
                "matched_chunks": len(items),
                "total_matches": len(items),
                "doc_count": len(source_titles),
                "query_count": len(queries),
                "required_term_count": len(plan["required_terms"]),
                "match_mode": plan["match_mode"],
            },
            source_chunk_ids=candidate_ids,
            source_titles=source_titles,
            candidate_ids=candidate_ids,
            state_delta=RuntimeStateDelta(
                candidate_ids=candidate_ids,
                source_titles=source_titles,
                flags={"grep_first_performed": True},
                counters={"grep_query_count": len(queries)},
                debug={"tool": self.name, "queries": list(plan["display_queries"])},
            ),
        )


class ListKnowledgeChunksTool:
    name = "list_knowledge_chunks"
    execution_class = ToolExecutionClass.PARALLEL_SAFE
    description = "Deep-read full chunk content by chunk id, document id, or parent id."
    parameters = {
        "type": "object",
        "properties": {
            "chunk_ids": {"type": "array", "items": {"type": "string"}},
            "knowledge_ids": {"type": "array", "items": {"type": "string"}, "description": "Document ids."},
            "parent_ids": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        limit = int(arguments.get("limit") or 12)
        chunks: list[dict[str, Any]] = []
        repo = context.rag_service.document_repository
        already_read = {str(item) for item in (context.state.get("deep_read_ids") or []) if item}
        for chunk_id in _string_list(arguments.get("chunk_ids")):
            chunk = repo.get_chunk(chunk_id, context.scope)
            if chunk:
                chunks.append(chunk)
        doc_ids = _string_list(arguments.get("knowledge_ids"))
        if doc_ids:
            chunks.extend(repo.list_chunks_for_documents(doc_ids, scope=context.scope, limit=limit))
        parent_ids = set(_string_list(arguments.get("parent_ids")))
        if parent_ids:
            for chunk in repo.list_chunks(scope=context.scope):
                if str(chunk.get("parent_id") or "") in parent_ids:
                    chunks.append(chunk)
        deduped = _dedupe_chunks(chunks)
        unread = [
            chunk
            for chunk in deduped
            if str(chunk.get("id") or chunk.get("chunk_id") or "") not in already_read
        ][:limit]
        skipped_count = max(0, len(deduped) - len(unread))
        items = [_chunk_summary(chunk, index, include_content=True) for index, chunk in enumerate(unread, start=1)]
        read_ids = _candidate_ids(items)
        source_titles = _source_titles(items)
        return RuntimeToolResult(
            success=True,
            output=json.dumps({"chunks": items}, ensure_ascii=False),
            observation=(
                f"Deep-read {len(items)} new chunks."
                if items
                else "No new chunks to deep-read; requested evidence was already read or unavailable."
            ),
            metadata={
                "chunk_count": len(items),
                "fetched_chunks": len(items),
                "total_chunks": len(items),
                "skipped_already_read": skipped_count,
            },
            source_chunk_ids=read_ids,
            source_titles=source_titles,
            deep_read=bool(items),
            state_delta=RuntimeStateDelta(
                deep_read_ids=read_ids,
                source_titles=source_titles,
            ),
        )


class GetDocumentInfoTool:
    name = "get_document_info"
    execution_class = ToolExecutionClass.PARALLEL_SAFE
    description = "Read bounded document metadata and summaries from selected knowledge bases."
    parameters = {
        "type": "object",
        "properties": {
            "knowledge_ids": {"type": "array", "items": {"type": "string"}, "description": "Document ids."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        repo = context.rag_service.document_repository
        requested = set(_string_list(arguments.get("knowledge_ids")))
        limit = int(arguments.get("limit") or 20)
        docs = []
        if requested:
            for doc_id in requested:
                doc = repo.get_document(doc_id, context.scope)
                if doc:
                    docs.append(doc)
        else:
            docs = repo.list_documents(context.scope)[:limit]
        items = []
        for doc in docs[:limit]:
            metadata = doc.get("metadata_json") or {}
            items.append(
                {
                    "doc_id": doc.get("id"),
                    "name": doc.get("name") or doc.get("storage_path"),
                    "type": doc.get("file_type"),
                    "summary": truncate_text(str(doc.get("summary") or metadata.get("summary") or ""), 800),
                    "parse_status": doc.get("parse_status"),
                    "summary_status": doc.get("summary_status"),
                    "chunks": doc.get("chunks", 0),
                    "updated_at": doc.get("updated_at"),
                    "source": doc.get("storage_path"),
                }
            )
        read_ids = [str(item["doc_id"]) for item in items if item.get("doc_id")]
        source_titles = [str(item.get("name")) for item in items if item.get("name")]
        return RuntimeToolResult(
            success=True,
            output=json.dumps({"documents": items}, ensure_ascii=False),
            observation=f"读取了 {len(items)} 个文档信息",
            metadata={"document_count": len(items)},
            source_chunk_ids=read_ids,
            source_titles=source_titles,
            deep_read=bool(items),
            state_delta=RuntimeStateDelta(
                deep_read_ids=read_ids,
                source_titles=source_titles,
            ),
        )


class QueryKnowledgeGraphTool:
    name = "query_knowledge_graph"
    execution_class = ToolExecutionClass.PARALLEL_SAFE
    description = "Read-only graph evidence for relationships, dependency, impact, and troubleshooting questions."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "action": {"type": "string", "enum": ["entity_search", "neighbor_search", "path_search"]},
            "source": {"type": "string"},
            "target": {"type": "string"},
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        graph = context.graph_retriever
        if graph is None:
            return RuntimeToolResult(success=True, output="Knowledge graph retrieval is disabled.", observation="图谱检索未启用")
        action = str(arguments.get("action") or "entity_search")
        query = str(arguments.get("query") or context.question)
        if action == "path_search":
            source = str(arguments.get("source") or query)
            target = str(arguments.get("target") or query)
            result = _call_scoped(graph, "path_search", source, target, max_depth=int(arguments.get("max_depth") or 3), scope=context.scope)
        elif action == "neighbor_search":
            result = _call_scoped(graph, "neighbor_search", query, depth=int(arguments.get("max_depth") or 1), scope=context.scope)
        else:
            result = _call_scoped(graph, "entity_search", query, scope=context.scope)
        payload = result.to_dict() if hasattr(result, "to_dict") else (result or {})
        source_chunk_ids = [str(item) for item in payload.get("source_chunk_ids", []) if item]
        return RuntimeToolResult(
            success=True,
            output=json.dumps(payload, ensure_ascii=False),
            observation=f"图谱返回 {len(payload.get('entities', []) or [])} 个实体、{len(payload.get('paths', []) or [])} 条路径",
            metadata={"entities": len(payload.get("entities", []) or []), "graph_paths": len(payload.get("paths", []) or [])},
            source_chunk_ids=source_chunk_ids,
        )


class WikiSearchTool:
    name = "wiki_search"
    execution_class = ToolExecutionClass.PARALLEL_SAFE
    description = (
        "Search LLM Wiki pages in the selected knowledge base. Prefer 1-3 compact keyword queries or a simple "
        "alternation query such as 'SN认证|Password认证|Loid认证' instead of passing the full user question. "
        "Use wiki_read_page before relying on a Wiki result."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "One compact keyword query, or a simple alternation query with terms separated by |.",
            },
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional keyword variants, aliases, product codes, or Chinese/English terms to search.",
            },
            "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
            "status": {"type": "string", "enum": ["published", "draft", "stale", ""]},
        },
        "additionalProperties": False,
    }

    def __init__(self, *, enabled: bool = False):
        self.enabled = enabled

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        service = _wiki_service_from_context(context, self.name, self.enabled)
        if not service:
            return _unavailable(self.name, "Wiki tools are disabled or the Wiki service is unavailable.")
        query_plan = _normalize_wiki_search_arguments(arguments, context.question)
        if query_plan.get("error"):
            return RuntimeToolResult(
                success=False,
                error=f"{query_plan['error']}{TOOL_ERROR_HINT}",
                observation=str(query_plan["error"]),
                metadata={"validation_errors": [query_plan["error"]], "status": "unavailable"},
            )
        queries = list(query_plan["queries"])
        top_k = min(20, max(1, int(arguments.get("top_k") or 8)))
        status = str(arguments.get("status") if arguments.get("status") is not None else "published")
        item_map: dict[str, dict[str, Any]] = {}
        for query in queries:
            for item in service.search_pages(context.scope, query, limit=top_k, status=status):
                key = str(item.get("page_id") or item.get("slug") or "")
                if not key:
                    key = hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
                if key in item_map:
                    continue
                item_map[key] = item
                if len(item_map) >= top_k:
                    break
            if len(item_map) >= top_k:
                break
        items = list(item_map.values())
        page_ids = [str(item.get("page_id") or "") for item in items if item.get("page_id")]
        titles = [str(item.get("title") or item.get("slug") or "") for item in items if item.get("title") or item.get("slug")]
        return RuntimeToolResult(
            success=True,
            output=json.dumps({"queries": queries, "results": items}, ensure_ascii=False),
            observation=f"Wiki search returned {len(items)} pages across {len(queries)} query candidates.",
            metadata={"result_count": len(items), "query_count": len(queries), "queries": queries, "status_filter": status},
            candidate_ids=page_ids,
            source_titles=titles,
            state_delta=RuntimeStateDelta(
                candidate_ids=page_ids,
                source_titles=titles,
                flags={"wiki_search_performed": True},
            ),
        )


class WikiReadPageTool:
    name = "wiki_read_page"
    execution_class = ToolExecutionClass.PARALLEL_SAFE
    description = "Deep-read a Wiki page by slug, including Markdown, links, source refs, chunk refs, and optional bounded index."
    parameters = {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "include_index": {"type": "boolean"},
            "index_limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["slug"],
        "additionalProperties": False,
    }

    def __init__(self, *, enabled: bool = False):
        self.enabled = enabled

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        service = _wiki_service_from_context(context, self.name, self.enabled)
        if not service:
            return _unavailable(self.name, "Wiki tools are disabled or the Wiki service is unavailable.")
        slug = str(arguments.get("slug") or "").strip()
        page = service.get_page(context.scope, slug).to_dict()
        payload: dict[str, Any] = {"page": page}
        if bool(arguments.get("include_index")):
            payload["index"] = service.list_pages(context.scope, limit=int(arguments.get("index_limit") or 20))["items"]
        source_ids = [str(ref.get("chunk_id") or ref.get("doc_id") or "") for ref in page.get("source_refs", []) if ref]
        source_ids.extend(page.get("chunk_refs") or [])
        source_ids.append(str(page.get("id") or ""))
        return RuntimeToolResult(
            success=True,
            output=json.dumps(payload, ensure_ascii=False),
            observation=f"Read Wiki page: {page.get('title') or slug}",
            metadata={
                "slug": page.get("slug"),
                "status": page.get("status"),
                "version": page.get("version"),
                "source_ref_count": len(page.get("source_refs") or []),
            },
            source_chunk_ids=list(dict.fromkeys(item for item in source_ids if item)),
            source_titles=[str(page.get("title") or page.get("slug") or "")],
            deep_read=True,
            state_delta=RuntimeStateDelta(
                deep_read_ids=[str(page.get("id") or page.get("slug") or "")],
                source_titles=[str(page.get("title") or page.get("slug") or "")],
                flags={"wiki_page_read": True},
            ),
        )


class WikiReadSourceDocTool:
    name = "wiki_read_source_doc"
    execution_class = ToolExecutionClass.PARALLEL_SAFE
    description = "Deep-read raw source document chunks referenced by Wiki pages. Prefer this for exact quotes, numbers, code, and tables."
    parameters = {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string"},
            "chunk_ids": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "additionalProperties": False,
    }

    def __init__(self, *, enabled: bool = False):
        self.enabled = enabled

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        service = _wiki_service_from_context(context, self.name, self.enabled)
        if not service:
            return _unavailable(self.name, "Wiki tools are disabled or the Wiki service is unavailable.")
        payload = service.read_source_doc(
            context.scope,
            doc_id=str(arguments.get("doc_id") or ""),
            chunk_ids=_string_list(arguments.get("chunk_ids")),
            limit=min(20, max(1, int(arguments.get("limit") or 8))),
        )
        chunk_ids = [str(item.get("chunk_id") or "") for item in payload.get("chunks", []) if item.get("chunk_id")]
        doc = payload.get("document") or {}
        titles = [str(doc.get("name") or doc.get("storage_path") or "")] if doc else []
        return RuntimeToolResult(
            success=True,
            output=json.dumps(payload, ensure_ascii=False),
            observation=f"Read {len(payload.get('chunks', []) or [])} raw source chunks for Wiki.",
            metadata={"chunk_count": len(payload.get("chunks", []) or []), "doc_id": arguments.get("doc_id") or ""},
            source_chunk_ids=chunk_ids,
            source_titles=[title for title in titles if title],
            deep_read=bool(chunk_ids),
            state_delta=RuntimeStateDelta(deep_read_ids=chunk_ids, source_titles=[title for title in titles if title]),
        )


class WikiFlagIssueTool:
    name = "wiki_flag_issue"
    execution_class = ToolExecutionClass.SERIAL
    description = "Flag a Wiki page issue such as missing source, outdated content, contradiction, or incorrect summary."
    parameters = {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "issue_type": {"type": "string", "enum": ["incorrect", "outdated", "missing_source", "conflict", "other"]},
            "description": {"type": "string"},
            "suspected_doc_ids": {"type": "array", "items": {"type": "string"}},
            "suspected_chunk_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["slug", "description"],
        "additionalProperties": False,
    }

    def __init__(self, *, enabled: bool = False):
        self.enabled = enabled

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        service = _wiki_service_from_context(context, self.name, self.enabled)
        if not service:
            return _unavailable(self.name, "Wiki tools are disabled or the Wiki service is unavailable.")
        issue = service.create_issue(context.scope, {**arguments, "reported_by": "agent"}).to_dict()
        return RuntimeToolResult(
            success=True,
            output=json.dumps({"issue": issue}, ensure_ascii=False),
            observation=f"Flagged Wiki issue {issue['id']} for {issue['slug']}.",
            metadata={"issue_id": issue["id"], "slug": issue["slug"], "status": issue["status"]},
        )


class WikiReadIssueTool:
    name = "wiki_read_issue"
    execution_class = ToolExecutionClass.PARALLEL_SAFE
    description = "Read one Wiki issue by id, or list bounded issues by slug/status."
    parameters = {
        "type": "object",
        "properties": {
            "issue_id": {"type": "string"},
            "slug": {"type": "string"},
            "status": {"type": "string", "enum": ["open", "resolved", "wontfix", ""]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "additionalProperties": False,
    }

    def __init__(self, *, enabled: bool = False):
        self.enabled = enabled

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        service = _wiki_service_from_context(context, self.name, self.enabled)
        if not service:
            return _unavailable(self.name, "Wiki tools are disabled or the Wiki service is unavailable.")
        issue_id = str(arguments.get("issue_id") or "").strip()
        if issue_id:
            payload = {"issue": service.get_issue(context.scope, issue_id).to_dict()}
        else:
            payload = {
                "issues": service.list_issues(
                    context.scope,
                    slug=str(arguments.get("slug") or ""),
                    status=str(arguments.get("status") if arguments.get("status") is not None else "open"),
                    limit=int(arguments.get("limit") or 20),
                )
            }
        count = 1 if "issue" in payload else len(payload.get("issues", []))
        return RuntimeToolResult(
            success=True,
            output=json.dumps(payload, ensure_ascii=False),
            observation=f"Read {count} Wiki issue records.",
            metadata={"issue_count": count},
        )


class WikiUpdateIssueTool:
    name = "wiki_update_issue"
    execution_class = ToolExecutionClass.SERIAL
    description = "Update a Wiki issue status after it has been resolved or rejected."
    parameters = {
        "type": "object",
        "properties": {
            "issue_id": {"type": "string"},
            "status": {"type": "string", "enum": ["open", "resolved", "wontfix"]},
        },
        "required": ["issue_id", "status"],
        "additionalProperties": False,
    }

    def __init__(self, *, enabled: bool = False):
        self.enabled = enabled

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        service = _wiki_service_from_context(context, self.name, self.enabled)
        if not service:
            return _unavailable(self.name, "Wiki maintenance tools are disabled or the Wiki service is unavailable.")
        issue = service.update_issue_status(context.scope, str(arguments.get("issue_id") or ""), str(arguments.get("status") or "")).to_dict()
        return RuntimeToolResult(
            success=True,
            output=json.dumps({"issue": issue}, ensure_ascii=False),
            observation=f"Updated Wiki issue {issue['id']} to {issue['status']}.",
            metadata={"issue_id": issue["id"], "status": issue["status"]},
        )


class WikiProposalTool:
    execution_class = ToolExecutionClass.SERIAL
    action = "write_page"

    def __init__(self, *, enabled: bool = False):
        self.enabled = enabled

    def _create(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        service = _wiki_service_from_context(context, self.name, self.enabled)
        if not service:
            return _unavailable(self.name, "Wiki maintenance tools are disabled or the Wiki service is unavailable.")
        proposal = service.create_proposal(
            context.scope,
            {
                **arguments,
                "action": self.action,
                "created_by": "agent",
                "payload": self._payload(arguments),
            },
        ).to_dict()
        return RuntimeToolResult(
            success=True,
            output=json.dumps({"proposal": proposal}, ensure_ascii=False),
            observation=f"Created Wiki proposal {proposal['id']} ({proposal['action']}).",
            metadata={"proposal_id": proposal["id"], "action": proposal["action"], "status": proposal["status"]},
        )

    def _payload(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return dict(arguments)


class WikiWritePageTool(WikiProposalTool):
    name = "wiki_write_page"
    action = "write_page"
    description = "Create a proposal to write or update a Wiki page. Does not directly modify published Wiki content."
    parameters = {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "title": {"type": "string"},
            "content_markdown": {"type": "string"},
            "summary": {"type": "string"},
            "page_type": {"type": "string", "enum": ["summary", "entity", "concept", "synthesis", "manual"]},
            "status": {"type": "string", "enum": ["draft", "published", "stale"]},
            "source_refs": {"type": "array", "items": {"type": "object"}},
            "chunk_refs": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
        "required": ["slug", "title", "content_markdown"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        return self._create(arguments, context)


class WikiReplaceTextTool(WikiProposalTool):
    name = "wiki_replace_text"
    action = "replace_text"
    description = "Create a proposal to replace exact text in a Wiki page. Does not directly mutate the page."
    parameters = {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["slug", "old_text", "new_text"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        return self._create(arguments, context)


class WikiRenamePageTool(WikiProposalTool):
    name = "wiki_rename_page"
    action = "rename_page"
    description = "Create a proposal to rename a Wiki page slug and/or title."
    parameters = {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "new_slug": {"type": "string"},
            "title": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["slug", "new_slug"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        return self._create(arguments, context)


class WikiDeletePageTool(WikiProposalTool):
    name = "wiki_delete_page"
    action = "delete_page"
    description = "Create a proposal to archive/delete a Wiki page."
    parameters = {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["slug"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        return self._create(arguments, context)


class ReadSkillTool:
    name = "read_skill"
    execution_class = ToolExecutionClass.PARALLEL_SAFE
    description = "Load full instructions for one enabled preloaded runtime skill."
    parameters = {
        "type": "object",
        "properties": {"skill_name": {"type": "string", "description": "Configured skill name."}},
        "required": ["skill_name"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        manager = context.skills_manager
        if manager is None or not manager.enabled:
            return RuntimeToolResult(success=False, error="Runtime skills are disabled.")
        skill_name = str(arguments.get("skill_name") or "")
        content = manager.read_skill(skill_name)
        return RuntimeToolResult(
            success=True,
            output=content,
            observation=f"已读取技能：{skill_name}",
            metadata={"skill_name": skill_name},
            state_delta=RuntimeStateDelta(append_values={"skills_read": [skill_name]}),
        )


class WebSearchTool:
    name = "web_search"
    execution_class = ToolExecutionClass.PARALLEL_SAFE
    description = "Search the web through a configured provider. Returns unavailable when disabled or unconfigured."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, *, enabled: bool = False, provider: Any | None = None):
        self.enabled = enabled
        self.provider = provider

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        if not self.enabled or self.provider is None:
            return _unavailable(self.name, "Web search is disabled or no search provider is configured.")
        query = str(arguments.get("query") or "").strip()
        top_k = int(arguments.get("top_k") or 5)
        results = self.provider.search(query, top_k=top_k)
        bounded = results[:top_k] if isinstance(results, list) else []
        return RuntimeToolResult(
            success=True,
            output=json.dumps({"query": query, "results": bounded}, ensure_ascii=False),
            observation=f"web_search returned {len(bounded)} results",
            metadata={"query": query, "result_count": len(bounded)},
        )


class HTTPJSONSearchProvider:
    def __init__(self, endpoint: str, *, timeout_seconds: float = 5.0, max_results: int = 10):
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.max_results = max_results

    def search(self, query: str, *, top_k: int = 5) -> list[dict[str, Any]]:
        separator = "&" if "?" in self.endpoint else "?"
        url = f"{self.endpoint}{separator}q={urllib.parse.quote(query)}"
        request = urllib.request.Request(url, headers={"User-Agent": "new-rag-project-agent/1.0"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        raw_results = payload.get("results", payload if isinstance(payload, list) else [])
        if not isinstance(raw_results, list):
            return []
        results: list[dict[str, Any]] = []
        for item in raw_results[: min(top_k, self.max_results)]:
            if not isinstance(item, dict):
                continue
            results.append(
                {
                    "title": truncate_text(str(item.get("title") or ""), 160),
                    "url": truncate_text(str(item.get("url") or item.get("link") or ""), 500),
                    "snippet": truncate_text(str(item.get("snippet") or item.get("content") or ""), 500),
                }
            )
        return results


class WebFetchTool:
    name = "web_fetch"
    execution_class = ToolExecutionClass.PARALLEL_SAFE
    description = "Fetch a web page from configured allowlisted domains with timeout and bounded text extraction."
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        *,
        enabled: bool = False,
        allowed_domains: tuple[str, ...] = (),
        timeout_seconds: float = 5.0,
        max_chars: int = 4000,
    ):
        self.enabled = enabled
        self.allowed_domains = tuple(domain.lower() for domain in allowed_domains if domain.strip())
        self.timeout_seconds = timeout_seconds
        self.max_chars = max_chars

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        if not self.enabled:
            return _unavailable(self.name, "Web fetch is disabled.")
        url = str(arguments.get("url") or "").strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return _unavailable(self.name, "Only http and https URLs are supported.")
        host = parsed.hostname.lower() if parsed.hostname else ""
        if self.allowed_domains and not _host_allowed(host, self.allowed_domains):
            return _unavailable(self.name, f"Domain is not allowlisted: {host}")
        request = urllib.request.Request(url, headers={"User-Agent": "new-rag-project-agent/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                content_type = response.headers.get("content-type", "")
                raw = response.read(min(1024 * 1024, max(self.max_chars * 4, 4096)))
        except (urllib.error.URLError, TimeoutError) as exc:
            return RuntimeToolResult(
                success=False,
                error=f"web_fetch failed: {exc}{TOOL_ERROR_HINT}",
                observation="web_fetch failed",
                metadata={"url": url, "error_class": exc.__class__.__name__},
            )
        text = raw.decode("utf-8", errors="replace")
        text = re.sub(r"<(script|style).*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return RuntimeToolResult(
            success=True,
            output=json.dumps({"url": url, "content_type": content_type, "text": truncate_text(text, self.max_chars)}, ensure_ascii=False),
            observation=f"web_fetch returned {min(len(text), self.max_chars)} chars",
            metadata={"url": url, "host": host, "content_type": content_type, "chars": min(len(text), self.max_chars)},
        )


class DataAnalysisTool:
    name = "data_analysis"
    execution_class = ToolExecutionClass.PARALLEL_SAFE
    description = "Read-only bounded analysis over inline JSON records. Disabled unless explicitly enabled."
    parameters = {
        "type": "object",
        "properties": {
            "records": {"type": "array", "items": {"type": "object"}},
            "operation": {"type": "string", "enum": ["count", "describe"]},
        },
        "required": ["records"],
        "additionalProperties": False,
    }

    def __init__(self, *, enabled: bool = False, max_records: int = 200):
        self.enabled = enabled
        self.max_records = max_records

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        if not self.enabled:
            return _unavailable(self.name, "Data analysis is disabled.")
        records = arguments.get("records") or []
        if not isinstance(records, list):
            return RuntimeToolResult(success=False, error=f"records must be an array{TOOL_ERROR_HINT}")
        bounded = [record for record in records[: self.max_records] if isinstance(record, dict)]
        operation = str(arguments.get("operation") or "count")
        payload: dict[str, Any] = {"row_count": len(bounded), "sampled": len(records) > len(bounded)}
        if operation == "describe":
            numeric: dict[str, list[float]] = {}
            for record in bounded:
                for key, value in record.items():
                    if isinstance(value, (int, float)):
                        numeric.setdefault(str(key), []).append(float(value))
            payload["numeric"] = {
                key: {"count": len(values), "min": min(values), "max": max(values), "avg": sum(values) / len(values)}
                for key, values in numeric.items()
                if values
            }
        return RuntimeToolResult(
            success=True,
            output=json.dumps(payload, ensure_ascii=False),
            observation=f"data_analysis processed {len(bounded)} rows",
            metadata={"row_count": len(bounded), "operation": operation},
        )


class DatabaseQueryTool:
    name = "database_query"
    execution_class = ToolExecutionClass.PARALLEL_SAFE
    description = "Read-only SQLite query against explicitly configured data sources."
    parameters = {
        "type": "object",
        "properties": {
            "data_source": {"type": "string"},
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["data_source", "query"],
        "additionalProperties": False,
    }

    def __init__(self, *, enabled: bool = False, allowed_sources: dict[str, str] | None = None):
        self.enabled = enabled
        self.allowed_sources = allowed_sources or {}

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        if not self.enabled:
            return _unavailable(self.name, "Database query is disabled.")
        source = str(arguments.get("data_source") or "").strip()
        db_path = self.allowed_sources.get(source)
        if not db_path:
            return _unavailable(self.name, f"Data source is not allowlisted: {source}")
        query = str(arguments.get("query") or "").strip()
        if not _read_only_sql(query):
            return _unavailable(self.name, "Only single read-only SELECT/WITH queries are allowed.")
        limit = int(arguments.get("limit") or 50)
        safe_query = f"SELECT * FROM ({query.rstrip(';')}) LIMIT {limit}"
        conn = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute(safe_query).fetchall()]
        except Exception as exc:
            return RuntimeToolResult(
                success=False,
                error=f"database_query failed: {exc}{TOOL_ERROR_HINT}",
                observation="database_query failed",
                metadata={"data_source": source, "error_class": exc.__class__.__name__},
            )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return RuntimeToolResult(
            success=True,
            output=json.dumps({"data_source": source, "rows": rows}, ensure_ascii=False, default=str),
            observation=f"database_query returned {len(rows)} rows",
            metadata={"data_source": source, "row_count": len(rows)},
        )


class ExecuteSkillTool:
    name = "execute_skill"
    execution_class = ToolExecutionClass.EXCLUSIVE
    description = "Executable skill boundary. Always unavailable until a secure sandbox is implemented."
    parameters = {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string"},
            "arguments": {"type": "object"},
        },
        "required": ["skill_name"],
        "additionalProperties": False,
    }

    def execute(self, arguments: dict[str, Any], context: RuntimeToolContext) -> RuntimeToolResult:
        return _unavailable(self.name, "Executable skills are disabled because no secure sandbox is configured.")


def build_default_tool_registry(
    *,
    enabled_tools: tuple[str, ...] | list[str],
    max_output_chars: int,
    skills_enabled: bool,
    web_search_enabled: bool = False,
    web_search_endpoint: str = "",
    web_fetch_enabled: bool = False,
    web_fetch_allowed_domains: tuple[str, ...] = (),
    web_fetch_timeout_seconds: float = 5.0,
    data_analysis_enabled: bool = False,
    database_query_enabled: bool = False,
    database_allowed_sources: dict[str, str] | None = None,
    wiki_tools_enabled: bool = False,
    wiki_maintenance_tools_enabled: bool = False,
) -> ToolRegistry:
    registry = ToolRegistry(max_output_chars=max_output_chars)
    available: dict[str, RuntimeTool] = {
        "thinking": ThinkingTool(),
        "todo_write": TodoWriteTool(),
        "knowledge_search": KnowledgeSearchTool(),
        "grep_chunks": GrepChunksTool(),
        "list_knowledge_chunks": ListKnowledgeChunksTool(),
        "get_document_info": GetDocumentInfoTool(),
        "query_knowledge_graph": QueryKnowledgeGraphTool(),
        "web_search": WebSearchTool(
            enabled=web_search_enabled,
            provider=HTTPJSONSearchProvider(web_search_endpoint, timeout_seconds=web_fetch_timeout_seconds)
            if web_search_endpoint
            else None,
        ),
        "web_fetch": WebFetchTool(
            enabled=web_fetch_enabled,
            allowed_domains=web_fetch_allowed_domains,
            timeout_seconds=web_fetch_timeout_seconds,
        ),
        "data_analysis": DataAnalysisTool(enabled=data_analysis_enabled),
        "database_query": DatabaseQueryTool(
            enabled=database_query_enabled,
            allowed_sources=database_allowed_sources,
        ),
        "wiki_search": WikiSearchTool(enabled=wiki_tools_enabled),
        "wiki_read_page": WikiReadPageTool(enabled=wiki_tools_enabled),
        "wiki_read_source_doc": WikiReadSourceDocTool(enabled=wiki_tools_enabled),
        "wiki_flag_issue": WikiFlagIssueTool(enabled=wiki_tools_enabled),
        "wiki_read_issue": WikiReadIssueTool(enabled=wiki_tools_enabled),
        "wiki_update_issue": WikiUpdateIssueTool(enabled=wiki_maintenance_tools_enabled),
        "wiki_write_page": WikiWritePageTool(enabled=wiki_maintenance_tools_enabled),
        "wiki_replace_text": WikiReplaceTextTool(enabled=wiki_maintenance_tools_enabled),
        "wiki_rename_page": WikiRenamePageTool(enabled=wiki_maintenance_tools_enabled),
        "wiki_delete_page": WikiDeletePageTool(enabled=wiki_maintenance_tools_enabled),
        "execute_skill": ExecuteSkillTool(),
    }
    if skills_enabled:
        available["read_skill"] = ReadSkillTool()
    for name in enabled_tools:
        tool = available.get(name)
        if tool is not None:
            registry.register(tool)
    return registry


def _unavailable(tool_name: str, reason: str) -> RuntimeToolResult:
    return RuntimeToolResult(
        success=False,
        error=f"{reason}{TOOL_ERROR_HINT}",
        observation=reason,
        metadata={"tool": tool_name, "status": "unavailable", "reason": reason},
    )


def _wiki_service_from_context(context: RuntimeToolContext, tool_name: str, enabled: bool):
    if not enabled:
        return None
    service = getattr(context.rag_service, "wiki_page_service", None)
    if service is None:
        logger.info("agent_runtime.wiki_tool.unavailable", extra={"tool": tool_name, "trace_id": get_trace_id()})
        return None
    return service


def _host_allowed(host: str, allowed_domains: tuple[str, ...]) -> bool:
    normalized = host.lower().strip(".")
    return any(normalized == domain or normalized.endswith(f".{domain}") for domain in allowed_domains)


def _read_only_sql(query: str) -> bool:
    normalized = query.strip().rstrip(";")
    lowered = normalized.lower()
    if ";" in normalized:
        return False
    if not (lowered.startswith("select ") or lowered.startswith("with ")):
        return False
    forbidden = {
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "create",
        "replace",
        "truncate",
        "attach",
        "detach",
        "pragma",
        "vacuum",
    }
    tokens = set(re.findall(r"[a-z_]+", lowered))
    return not bool(tokens & forbidden)


def _parse_arguments(arguments: dict[str, Any] | str | None) -> dict[str, Any]:
    if arguments is None:
        return {}
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        if not arguments.strip():
            return {}
        parsed = json.loads(arguments)
        if not isinstance(parsed, dict):
            raise ValueError("tool arguments must be an object")
        return parsed
    raise ValueError("tool arguments must be an object")


def _coerce_arguments(args: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties") or {}
    coerced = dict(args)
    for key, definition in properties.items():
        if key not in coerced:
            continue
        value = coerced[key]
        target_type = definition.get("type")
        if target_type == "integer" and isinstance(value, str) and value.strip().isdigit():
            coerced[key] = int(value)
        elif target_type == "boolean" and isinstance(value, str) and value.lower() in {"true", "false"}:
            coerced[key] = value.lower() == "true"
        elif target_type == "array" and isinstance(value, str):
            coerced[key] = [value]
    return coerced


def _validate_arguments(args: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = schema.get("required") or []
    for key in required:
        if key not in args or args[key] is None or args[key] == "":
            errors.append(f"missing required argument: {key}")
    properties = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        for key in args:
            if key not in properties:
                errors.append(f"unexpected argument: {key}")
    for key, value in args.items():
        definition = properties.get(key)
        if not definition:
            continue
        expected = definition.get("type")
        if expected == "string" and not isinstance(value, str):
            errors.append(f"{key} must be string")
        elif expected == "integer" and not isinstance(value, int):
            errors.append(f"{key} must be integer")
        elif expected == "array" and not isinstance(value, list):
            errors.append(f"{key} must be array")
        if isinstance(value, int):
            if "minimum" in definition and value < int(definition["minimum"]):
                errors.append(f"{key} must be >= {definition['minimum']}")
            if "maximum" in definition and value > int(definition["maximum"]):
                errors.append(f"{key} must be <= {definition['maximum']}")
        if "enum" in definition and value not in definition["enum"]:
            errors.append(f"{key} must be one of {definition['enum']}")
    return errors


def _truncate_output(output: str, max_chars: int) -> str:
    if len(output) <= max_chars:
        return output
    suffix = "\n... [tool output truncated]"
    if max_chars <= len(suffix):
        return output[:max_chars]
    return output[: max_chars - len(suffix)] + suffix


def _normalize_grep_arguments(arguments: dict[str, Any], fallback_query: str) -> dict[str, Any]:
    top_k = min(20, max(1, int(arguments.get("top_k") or 8)))
    match_mode = str(arguments.get("match_mode") or "any_query_with_optional_required_terms").strip()
    allowed_modes = {"any_query", "any_query_with_required_terms", "any_query_with_optional_required_terms"}
    if match_mode not in allowed_modes:
        return {"error": f"unsupported match_mode: {match_mode}"}

    variants: list[str] = []
    for item in _string_list(arguments.get("queries")):
        variants.extend(_split_simple_alternation(item))
    legacy_query = str(arguments.get("query") or "").strip()
    if legacy_query:
        variants.extend(_split_simple_alternation(legacy_query))
    if not variants and fallback_query:
        variants.append(fallback_query)

    display_queries = _bounded_unique_strings(variants, max_count=12, max_chars=160)
    required_terms = _bounded_unique_strings(_string_list(arguments.get("required_terms")), max_count=8, max_chars=80)
    if not display_queries:
        return {"error": "grep_chunks requires query or queries"}

    executable = list(display_queries)
    if required_terms and match_mode in {"any_query_with_required_terms", "any_query_with_optional_required_terms"}:
        executable = []
        for query in display_queries:
            executable.append(query)
            executable.extend(f"{query} {term}" for term in required_terms)
    queries = _bounded_unique_strings(executable, max_count=24, max_chars=240)
    return {
        "queries": queries,
        "display_queries": display_queries,
        "required_terms": required_terms,
        "match_mode": match_mode,
        "top_k": top_k,
    }


def _normalize_wiki_search_arguments(arguments: dict[str, Any], fallback_query: str) -> dict[str, Any]:
    variants: list[str] = []
    for item in _string_list(arguments.get("queries")):
        variants.extend(_split_simple_alternation(item))
    legacy_query = str(arguments.get("query") or "").strip()
    if legacy_query:
        variants.extend(_split_simple_alternation(legacy_query))
    if not variants and fallback_query:
        variants.extend(_split_simple_alternation(fallback_query))

    queries = _bounded_unique_strings(variants, max_count=12, max_chars=160)
    if not queries:
        return {"error": "wiki_search requires query or queries"}
    return {"queries": queries}


def _split_simple_alternation(value: str) -> list[str]:
    text = value.strip()
    if not text:
        return []
    if "|" not in text:
        return [text]
    return [part.strip() for part in text.split("|") if part.strip()]


def _bounded_unique_strings(values: list[str], *, max_count: int, max_chars: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = re.sub(r"\s+", " ", str(value).strip())
        if not cleaned:
            continue
        cleaned = truncate_text(cleaned, max_chars)
        key = cleaned.lower()
        if key in seen:
            continue
        result.append(cleaned)
        seen.add(key)
        if len(result) >= max_count:
            break
    return result


def _hit_identity(hit: dict[str, Any]) -> str:
    metadata = hit.get("metadata", {}) or {}
    for key in ("chunk_id", "child_id", "doc_id", "parent_id"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    return hashlib.sha256(str(hit.get("content", "")).encode("utf-8", errors="ignore")).hexdigest()


def _call_scoped(target, method_name: str, *args, scope: KnowledgeBaseScope | None = None, **kwargs):
    method = getattr(target, method_name)
    try:
        return method(*args, scope=scope, **kwargs)
    except TypeError:
        if scope is not None and not scope.compatibility_default:
            raise RuntimeError(f"{method_name} does not support knowledge-base scope")
        return method(*args, **kwargs)


def _call_scoped_with_debug(
    target,
    method_name: str,
    *args,
    scope: KnowledgeBaseScope | None = None,
    **kwargs,
) -> tuple[Any, dict[str, Any]]:
    runner = getattr(target, "run_with_retrieval_debug", None)
    if callable(runner):
        return runner(
            lambda: _call_scoped(target, method_name, *args, scope=scope, **kwargs)
        )
    return _call_scoped(target, method_name, *args, scope=scope, **kwargs), {}


def _hit_summary(hit: dict[str, Any], index: int, *, include_snippet: bool = False, query: str = "") -> dict[str, Any]:
    metadata = hit.get("metadata", {}) or {}
    content = str(hit.get("content") or "")
    chunk_id = str(metadata.get("chunk_id") or metadata.get("child_id") or "")
    item = {
        "rank": index,
        "doc_id": metadata.get("doc_id", ""),
        "chunk_id": chunk_id,
        "parent_id": metadata.get("parent_id", ""),
        "source": _hit_source(hit, metadata),
        "title_path": metadata.get("title_path", ""),
        "score": float(hit.get("hybrid_score", hit.get("keyword_score", hit.get("vector_score", 0.0))) or 0.0),
        "preview": truncate_text(content.strip().replace("\n", " "), 360),
    }
    if include_snippet:
        item["match_snippet"] = _match_snippet(content, query)
    return item


def _chunk_summary(chunk: dict[str, Any], index: int, *, include_content: bool = False) -> dict[str, Any]:
    metadata = chunk.get("metadata_json") or {}
    content = str(chunk.get("content_markdown") or chunk.get("content") or "")
    item = {
        "rank": index,
        "doc_id": chunk.get("doc_id", ""),
        "chunk_id": chunk.get("id", ""),
        "parent_id": chunk.get("parent_id", ""),
        "source": metadata.get("source") or chunk.get("storage_path") or chunk.get("doc_id", ""),
        "title_path": chunk.get("title_path", ""),
        "chunk_type": chunk.get("chunk_type", ""),
        "page_start": chunk.get("page_start"),
        "page_end": chunk.get("page_end"),
    }
    item["content" if include_content else "preview"] = truncate_text(content, 3000 if include_content else 360)
    return item


def _candidate_ids(items: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for item in items:
        for key in ("chunk_id", "doc_id", "parent_id"):
            value = str(item.get(key) or "")
            if value:
                ids.append(value)
    return list(dict.fromkeys(ids))


def _source_titles(items: list[dict[str, Any]]) -> list[str]:
    titles = []
    for item in items:
        title = str(item.get("source") or item.get("name") or "").strip()
        if title and title.lower() != "unknown":
            titles.append(title)
    return list(dict.fromkeys(titles))


def _hit_source(hit: dict[str, Any], metadata: dict[str, Any]) -> str:
    for value in (
        metadata.get("source"),
        metadata.get("storage_path"),
        metadata.get("file_path"),
        metadata.get("filename"),
        metadata.get("document_name"),
        metadata.get("name"),
        hit.get("source"),
        hit.get("storage_path"),
        metadata.get("doc_id"),
    ):
        text = str(value or "").strip()
        if text and text.lower() != "unknown":
            return text
    return ""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _dedupe_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for chunk in chunks:
        key = str(chunk.get("id") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        result.append(chunk)
    return result


def _match_snippet(content: str, query: str) -> str:
    terms = [term for term in re.split(r"\||\s+", query) if term]
    lowered = content.lower()
    index = -1
    for term in terms:
        index = lowered.find(term.lower())
        if index >= 0:
            break
    if index < 0:
        return truncate_text(content.strip().replace("\n", " "), 260)
    start = max(0, index - 80)
    end = min(len(content), index + 180)
    return truncate_text(content[start:end].strip().replace("\n", " "), 300)
