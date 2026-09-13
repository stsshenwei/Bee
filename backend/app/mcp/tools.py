from __future__ import annotations

import base64
import binascii
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.mcp.config import MCPRuntimeConfig
from app.mcp.result import MCPToolError, MCPToolResult, error_result, sanitize_payload, success_result
from app.mcp.runtime import MCPBackendServices
from app.services.memory.principal import Principal


MAX_LIMIT = 1000


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]


class RAGMCPToolService:
    def __init__(self, services: MCPBackendServices, config: MCPRuntimeConfig | None = None):
        self.services = services
        self.config = config or MCPRuntimeConfig()
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "kb_list": self._kb_list,
            "kb_view": self._kb_view,
            "kb_create": self._kb_create,
            "kb_delete": self._kb_delete,
            "doc_list": self._doc_list,
            "doc_view": self._doc_view,
            "doc_upload": self._doc_upload,
            "chunk_list": self._chunk_list,
            "search_chunks": self._search_chunks,
            "rag_query": self._rag_query,
            "chat": self._chat,
            "wiki_search": self._wiki_search,
            "wiki_read_page": self._wiki_read_page,
        }
        self._specs = _tool_specs()

    def list_tools(self) -> list[ToolSpec]:
        return [self._specs[name] for name in self._handlers]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> MCPToolResult:
        started = time.monotonic()
        args = arguments or {}
        handler = self._handlers.get(name)
        if handler is None:
            return error_result("tool_not_found", f"Unknown MCP tool: {name}", max_text_chars=self.config.max_output_chars)
        try:
            payload = handler(args)
            payload.setdefault("_mcp", {})
            payload["_mcp"].update({"tool": name, "duration_ms": int((time.monotonic() - started) * 1000)})
            safe_payload = sanitize_payload(payload, max_text_chars=self.config.max_output_chars)
            return success_result(safe_payload, max_text_chars=self.config.max_output_chars)
        except MCPToolError as exc:
            return error_result(exc.code, exc.message, detail=exc.detail, max_text_chars=self.config.max_output_chars)
        except Exception as exc:
            return error_result(
                "tool_execution_failed",
                str(exc),
                detail={"tool": name, "error_type": exc.__class__.__name__},
                max_text_chars=self.config.max_output_chars,
            )

    def _scope(self, args: dict[str, Any], *, require_single_kb: bool = False):
        kb_ids = _string_list(args.get("knowledge_base_ids"))
        if not kb_ids and _clean(args.get("knowledge_base_id")):
            kb_ids = [_clean(args.get("knowledge_base_id"))]
        if not kb_ids and _clean(args.get("kb_id")):
            kb_ids = [_clean(args.get("kb_id"))]
        doc_ids = _string_list(args.get("doc_ids"))
        if not doc_ids and _clean(args.get("doc_id")) and args.get("_doc_as_scope"):
            doc_ids = [_clean(args.get("doc_id"))]
        resolver = getattr(self.services.rag_service, "resolve_scope", None)
        if not callable(resolver) and self.services.knowledge_base_service is not None:
            resolver = getattr(self.services.knowledge_base_service, "resolve_scope", None)
        if not callable(resolver):
            raise MCPToolError("scope_unavailable", "Knowledge-base scope resolution is unavailable")
        scope = resolver(kb_ids or None, doc_ids or None)
        if require_single_kb:
            try:
                _ = scope.knowledge_base_id
            except Exception as exc:
                raise MCPToolError("invalid_scope", "Tool requires exactly one knowledge base", detail={"knowledge_base_ids": kb_ids}) from exc
        return scope

    def _kb_list(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self._knowledge_base_service()
        items = service.list(_clean(args.get("workspace_id")) or None, bool(args.get("include_archived", False)))
        return {"items": [_to_dict(item) for item in items]}

    def _kb_view(self, args: dict[str, Any]) -> dict[str, Any]:
        kb_id = _required(args, "kb_id")
        service = self._knowledge_base_service()
        try:
            item = service.get(kb_id)
        except KeyError as exc:
            raise MCPToolError("knowledge_base_not_found", f"Knowledge base not found: {kb_id}") from exc
        return {"item": _to_dict(item)}

    def _kb_create(self, args: dict[str, Any]) -> dict[str, Any]:
        name = _required(args, "name")
        description = _required(args, "description")
        knowledge_base_type = _clean(args.get("knowledge_base_type")) or _clean(args.get("type")) or "document"
        service = self._knowledge_base_service()
        try:
            item = service.create(
                name=name,
                description=description,
                knowledge_base_type=knowledge_base_type,
                is_default=bool(args.get("is_default", False)),
                workspace_id=_clean(args.get("workspace_id")) or None,
                indexing_strategy=_dict_arg(args, "indexing_strategy"),
                provider_config=_dict_arg(args, "provider_config"),
            )
        except ValueError as exc:
            raise MCPToolError("knowledge_base_validation_failed", str(exc)) from exc
        return {"item": _to_dict(item)}

    def _kb_delete(self, args: dict[str, Any]) -> dict[str, Any]:
        kb_id = _required(args, "kb_id")
        service = self._knowledge_base_service()
        try:
            item = service.archive(kb_id)
        except KeyError as exc:
            raise MCPToolError("knowledge_base_not_found", f"Knowledge base not found: {kb_id}") from exc
        except ValueError as exc:
            raise MCPToolError("knowledge_base_validation_failed", str(exc)) from exc
        return {"item": _to_dict(item), "status": "archived"}

    def _doc_list(self, args: dict[str, Any]) -> dict[str, Any]:
        _required(args, "kb_id")
        scope = self._scope(args, require_single_kb=True)
        page = _int_arg(args, "page", 1, minimum=1)
        page_size = _int_arg(args, "page_size", 20, minimum=1, maximum=MAX_LIMIT)
        filters = {
            "q": args.get("q") or args.get("keyword"),
            "tag": args.get("tag"),
            "file_type": args.get("file_type"),
            "status": args.get("status"),
            "source": args.get("source"),
            "created_from": args.get("created_from") or args.get("start_time"),
            "created_to": args.get("created_to") or args.get("end_time"),
        }
        docs = self.services.rag_service.list_documents(scope=scope, filters=filters)
        total = len(docs)
        start = (page - 1) * page_size
        return {"items": docs[start : start + page_size], "page": page, "page_size": page_size, "total": total}

    def _doc_view(self, args: dict[str, Any]) -> dict[str, Any]:
        doc_id = _required(args, "doc_id")
        scope = self._scope(args)
        doc = self._document_repository().get_document(doc_id, scope)
        if doc is None:
            raise MCPToolError("document_not_found", f"Document not found: {doc_id}")
        return {"item": doc}

    def _doc_upload(self, args: dict[str, Any]) -> dict[str, Any]:
        _required(args, "kb_id")
        filename = _required(args, "filename")
        scope = self._scope(args, require_single_kb=True)
        content = _upload_content(args, self.config.max_upload_bytes)
        if not content:
            raise MCPToolError("invalid_arguments", "document content cannot be empty")
        if len(content) > self.config.max_upload_bytes:
            raise MCPToolError(
                "upload_too_large",
                f"document content exceeds MCP_MAX_UPLOAD_BYTES ({self.config.max_upload_bytes})",
                detail={"size": len(content), "max_upload_bytes": self.config.max_upload_bytes},
            )
        try:
            item = self.services.rag_service.save_uploaded_document(
                filename=filename,
                content=content,
                relative_path=_clean(args.get("relative_path")) or None,
                scope=scope,
            )
        except ValueError as exc:
            raise MCPToolError("document_upload_validation_failed", str(exc)) from exc
        return {"item": item, "scope": scope.to_dict()}

    def _chunk_list(self, args: dict[str, Any]) -> dict[str, Any]:
        doc_id = _required(args, "doc_id")
        limit = _int_arg(args, "limit", 50, minimum=1, maximum=MAX_LIMIT)
        args = {**args, "_doc_as_scope": False}
        scope = self._scope(args)
        repo = self._document_repository()
        if repo.get_document(doc_id, scope) is None:
            raise MCPToolError("document_not_found", f"Document not found: {doc_id}")
        chunks = repo.list_chunks(doc_id=doc_id, scope=scope)
        return {
            "chunks": chunks[:limit],
            "total": len(chunks),
            "truncated_at_limit": len(chunks) > limit,
        }

    def _search_chunks(self, args: dict[str, Any]) -> dict[str, Any]:
        query = _required(args, "query")
        limit = _int_arg(args, "limit", 10, minimum=1, maximum=MAX_LIMIT)
        scope = self._scope(args)
        hits = self.services.rag_service.hybrid_retrieve_hits(query, scope=scope)
        recall_parent = bool(args.get("recall_parent_context", True))
        if recall_parent and hasattr(self.services.rag_service, "recall_parent_hits"):
            hits = self.services.rag_service.recall_parent_hits(hits, scope=scope)
        sources = []
        extractor = getattr(self.services.rag_service, "extract_sources", None)
        if callable(extractor):
            sources = extractor(hits[:limit])
        return {
            "results": [_format_hit(hit) for hit in hits[:limit]],
            "sources": sources,
            "limit": limit,
            "scope": scope.to_dict(),
            "debug_info": getattr(self.services.rag_service, "_last_retrieval_debug", None),
        }

    def _rag_query(self, args: dict[str, Any]) -> dict[str, Any]:
        question = _required(args, "question")
        top_k = _int_arg(args, "top_k", 8, minimum=1, maximum=MAX_LIMIT)
        scope = self._scope(args)
        result = self.services.rag_service.answer_query(
            question,
            top_k=top_k,
            filters={**(args.get("filters") or {}), "doc_ids": list(scope.document_ids)},
            scope=scope,
        )
        return {"result": result, "scope": scope.to_dict()}

    def _chat(self, args: dict[str, Any]) -> dict[str, Any]:
        message = _required(args, "message")
        max_events = _int_arg(args, "max_events", self.config.max_events, minimum=1, maximum=max(self.config.max_events, 1))
        scope = self._scope(args)
        session_id = _clean(args.get("session_id")) or _clean(args.get("conversation_id")) or f"mcp-{uuid.uuid4().hex}"
        assistant_message_id = f"mcp-msg-{uuid.uuid4().hex}"
        conversation_context: dict[str, Any] = {}
        persist = bool(args.get("persist", False))
        if persist and self.services.conversation_service is not None:
            conversation_context = self._prepare_conversation(session_id, message, assistant_message_id, scope)

        events: list[dict[str, Any]] = []
        answer_parts: list[str] = []
        hits = self.services.rag_service.recall_parent_hits(
            self.services.rag_service.hybrid_retrieve_hits(message, scope=scope),
            scope=scope,
        )
        sources = self.services.rag_service.extract_sources(hits)
        events.append({"type": "sources", "sources": sources})
        started = time.monotonic()
        terminal = False
        for token in self.services.rag_service.stream_answer(
            message,
            hits=hits,
            conversation_context=conversation_context,
            scope=scope,
        ):
            if time.monotonic() - started > self.config.tool_timeout_seconds:
                raise MCPToolError(
                    "chat_timeout",
                    "Chat generation exceeded MCP_TOOL_TIMEOUT_SECONDS",
                    detail={"session_id": session_id, "assistant_message_id": assistant_message_id},
                )
            text = str(token)
            answer_parts.append(text)
            if len(events) < max_events:
                events.append({"type": "token", "token": text})
        terminal = True
        answer = "".join(answer_parts)
        if persist and self.services.conversation_service is not None:
            self._complete_conversation(session_id, assistant_message_id, answer, sources, scope)
        if terminal and len(events) < max_events:
            events.append({"type": "final", "answer": answer})
        return {
            "events": events,
            "events_truncated": len(answer_parts) + 2 > len(events),
            "answer": answer,
            "sources": sources,
            "session_id": session_id,
            "conversation_id": session_id,
            "assistant_message_id": assistant_message_id,
            "scope": scope.to_dict(),
        }

    def _wiki_search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = _required(args, "query")
        _required(args, "kb_id")
        limit = _int_arg(args, "limit", 10, minimum=1, maximum=100)
        scope = self._scope(args, require_single_kb=True)
        service = self._wiki_service()
        return {"items": service.search_pages(scope, query, limit=limit), "scope": scope.to_dict()}

    def _wiki_read_page(self, args: dict[str, Any]) -> dict[str, Any]:
        slug = _required(args, "slug")
        _required(args, "kb_id")
        scope = self._scope(args, require_single_kb=True)
        service = self._wiki_service()
        try:
            page = service.get_page(scope, slug)
        except KeyError as exc:
            raise MCPToolError("wiki_page_not_found", f"Wiki page not found: {slug}") from exc
        return {"page": _to_dict(page), "scope": scope.to_dict()}

    def _prepare_conversation(self, session_id: str, message: str, assistant_message_id: str, scope: Any) -> dict[str, Any]:
        service = self.services.conversation_service
        principal = Principal(kind="api_tenant")
        conversation = service.get_or_create_conversation(
            session_id,
            principal=principal,
            agent_config={"channel": "mcp", "knowledge_base_scope": scope.to_dict()},
        )
        conversation_id = str(conversation.get("id") or session_id)
        repo = service.repository
        append = getattr(repo, "append_message", None)
        if callable(append):
            append(conversation_id, "user", message, {"channel": "mcp", "knowledge_base_scope": scope.to_dict()})
        return service.build_context(conversation_id, principal=principal)

    def _complete_conversation(self, session_id: str, assistant_message_id: str, answer: str, sources: list[dict[str, Any]], scope: Any) -> None:
        service = self.services.conversation_service
        repo = service.repository
        complete = getattr(repo, "complete_assistant_message", None)
        metadata = {"channel": "mcp", "sources": sources, "knowledge_base_scope": scope.to_dict()}
        if callable(complete):
            try:
                complete(session_id, assistant_message_id, answer, metadata)
                return
            except Exception:
                pass
        append = getattr(repo, "append_message", None)
        if callable(append):
            append(session_id, "assistant", answer, metadata)

    def _knowledge_base_service(self):
        if self.services.knowledge_base_service is None:
            raise MCPToolError("service_unavailable", "Knowledge-base service is unavailable")
        return self.services.knowledge_base_service

    def _document_repository(self):
        if self.services.document_repository is None:
            raise MCPToolError("service_unavailable", "Document repository is unavailable")
        return self.services.document_repository

    def _wiki_service(self):
        if self.services.wiki_page_service is None:
            raise MCPToolError("service_unavailable", "Wiki service is unavailable")
        return self.services.wiki_page_service


def _tool_specs() -> dict[str, ToolSpec]:
    read = {"destructiveHint": False, "readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}
    chat = {"destructiveHint": False, "readOnlyHint": False, "idempotentHint": False, "openWorldHint": True}
    write = {"destructiveHint": False, "readOnlyHint": False, "idempotentHint": False, "openWorldHint": False}
    destructive = {"destructiveHint": True, "readOnlyHint": False, "idempotentHint": False, "openWorldHint": False}
    return {
        "kb_list": ToolSpec("kb_list", "List visible knowledge bases.", _schema({}), read),
        "kb_view": ToolSpec("kb_view", "Fetch a knowledge base by ID.", _schema({"kb_id": "string"}, ["kb_id"]), read),
        "kb_create": ToolSpec(
            "kb_create",
            "Create a knowledge base. Clients should prompt for both name and description.",
            _schema(
                {
                    "name": {"type": "string", "description": "Knowledge-base name to show to users."},
                    "description": {"type": "string", "description": "Human-readable purpose and scope of the knowledge base."},
                    "knowledge_base_type": {"type": "string", "enum": ["document", "faq", "wiki"]},
                    "is_default": "boolean",
                    "workspace_id": "string",
                    "indexing_strategy": "object",
                    "provider_config": "object",
                },
                ["name", "description"],
            ),
            write,
        ),
        "kb_delete": ToolSpec("kb_delete", "Archive a knowledge base by ID.", _schema({"kb_id": "string"}, ["kb_id"]), destructive),
        "doc_list": ToolSpec("doc_list", "List documents in a knowledge base with bounded pagination.", _schema({"kb_id": "string", "page": "integer", "page_size": "integer", "q": "string", "status": "string", "file_type": "string"}, ["kb_id"]), read),
        "doc_view": ToolSpec("doc_view", "Fetch document metadata by ID.", _schema({"doc_id": "string", "kb_id": "string"}, ["doc_id"]), read),
        "doc_upload": ToolSpec(
            "doc_upload",
            "Upload and index a document into a knowledge base.",
            _schema(
                {
                    "kb_id": "string",
                    "filename": "string",
                    "content": {"type": "string", "description": "UTF-8 text content to upload."},
                    "content_base64": {"type": "string", "description": "Base64-encoded file bytes to upload."},
                    "file_path": {"type": "string", "description": "Optional server-local path to read and upload."},
                    "relative_path": "string",
                },
                ["kb_id", "filename"],
            ),
            write,
        ),
        "chunk_list": ToolSpec("chunk_list", "List chunks for a document with a bounded limit.", _schema({"doc_id": "string", "kb_id": "string", "limit": "integer"}, ["doc_id"]), read),
        "search_chunks": ToolSpec("search_chunks", "Run hybrid retrieval and return ranked chunks.", _schema({"query": "string", "kb_id": "string", "limit": "integer"}, ["query"]), read),
        "rag_query": ToolSpec("rag_query", "Run a grounded RAG query.", _schema({"question": "string", "kb_id": "string", "top_k": "integer"}, ["question"]), read),
        "chat": ToolSpec("chat", "Run a buffered RAG chat turn.", _schema({"message": "string", "kb_id": "string", "session_id": "string", "persist": "boolean"}, ["message"]), chat),
        "wiki_search": ToolSpec("wiki_search", "Search read-only Wiki pages.", _schema({"kb_id": "string", "query": "string", "limit": "integer"}, ["kb_id", "query"]), read),
        "wiki_read_page": ToolSpec("wiki_read_page", "Read a Wiki page by slug.", _schema({"kb_id": "string", "slug": "string"}, ["kb_id", "slug"]), read),
    }


def _schema(properties: dict[str, str | dict[str, Any]], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            name: (dict(spec) if isinstance(spec, dict) else {"type": spec})
            for name, spec in properties.items()
        },
        "required": required or [],
        "additionalProperties": True,
    }


def _required(args: dict[str, Any], name: str) -> str:
    value = _clean(args.get(name))
    if not value:
        if name == "kb_id":
            value = _clean(args.get("knowledge_base_id"))
        if not value:
            raise MCPToolError("invalid_arguments", f"{name} is required")
    return value


def _int_arg(args: dict[str, Any], name: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
    raw = args.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise MCPToolError("invalid_arguments", f"{name} must be an integer") from exc
    if value < minimum:
        raise MCPToolError("invalid_arguments", f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise MCPToolError("invalid_arguments", f"{name} must be <= {maximum}")
    return value


def _dict_arg(args: dict[str, Any], name: str) -> dict[str, Any] | None:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MCPToolError("invalid_arguments", f"{name} must be an object")
    return dict(value)


def _upload_content(args: dict[str, Any], max_upload_bytes: int) -> bytes:
    present = [
        name
        for name in ("content", "content_base64", "file_path")
        if args.get(name) is not None and _clean(args.get(name))
    ]
    if len(present) != 1:
        raise MCPToolError("invalid_arguments", "provide exactly one of content, content_base64, or file_path")
    source = present[0]
    raw = args[source]
    if source == "content":
        return str(raw).encode("utf-8")
    if source == "content_base64":
        try:
            return base64.b64decode(str(raw), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MCPToolError("invalid_arguments", "content_base64 must be valid base64") from exc
    path = Path(str(raw)).expanduser()
    if not path.exists() or not path.is_file():
        raise MCPToolError("file_not_found", f"Upload file not found: {path}")
    size = path.stat().st_size
    if size > max_upload_bytes:
        raise MCPToolError(
            "upload_too_large",
            f"document content exceeds MCP_MAX_UPLOAD_BYTES ({max_upload_bytes})",
            detail={"size": size, "max_upload_bytes": max_upload_bytes},
        )
    return path.read_bytes()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _to_dict(value: Any) -> dict[str, Any]:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if isinstance(value, dict):
        return dict(value)
    return {"value": value}


def _format_hit(hit: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(hit.get("metadata") or {})
    return {
        "content": hit.get("content", ""),
        "score": hit.get("score", hit.get("hybrid_score", metadata.get("score", 0.0))),
        "metadata": metadata,
        "source": metadata.get("source") or metadata.get("document_name") or metadata.get("doc_id") or "",
    }
