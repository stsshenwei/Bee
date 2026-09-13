from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MCPToolResult:
    structured_content: dict[str, Any]
    text: str
    is_error: bool = False


class MCPToolError(Exception):
    def __init__(self, code: str, message: str, *, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def success_result(payload: dict[str, Any], *, max_text_chars: int = 6000) -> MCPToolResult:
    payload = dict(payload)
    if len(json.dumps(payload, ensure_ascii=False, default=str)) > max_text_chars:
        meta = payload.setdefault("_mcp", {})
        if isinstance(meta, dict):
            meta["truncated"] = True
    text = _json_text(payload, max_text_chars=max_text_chars)
    return MCPToolResult(structured_content=payload, text=text, is_error=False)


def error_result(
    code: str,
    message: str,
    *,
    detail: dict[str, Any] | None = None,
    max_text_chars: int = 6000,
) -> MCPToolResult:
    payload = {
        "error": {
            "code": code,
            "message": _safe_message(message),
            "detail": sanitize_payload(detail or {}, max_text_chars=max_text_chars),
        }
    }
    text = _json_text(payload, max_text_chars=max_text_chars)
    return MCPToolResult(structured_content=payload, text=text, is_error=True)


def sanitize_payload(value: Any, *, max_text_chars: int = 6000) -> Any:
    truncated = {"value": False}
    sanitized = _sanitize(value, max_text_chars=max_text_chars, truncated=truncated)
    if isinstance(sanitized, dict) and truncated["value"]:
        meta = sanitized.setdefault("_mcp", {})
        if isinstance(meta, dict):
            meta["truncated"] = True
    return sanitized


def _sanitize(value: Any, *, max_text_chars: int, truncated: dict[str, bool]) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > max_text_chars:
            truncated["value"] = True
            return value[:max_text_chars] + "...[truncated]"
        return value
    if isinstance(value, (list, tuple)):
        items = [_sanitize(item, max_text_chars=max_text_chars, truncated=truncated) for item in value[:200]]
        if len(value) > 200:
            truncated["value"] = True
            items.append({"_mcp_truncated_items": len(value) - 200})
        return items
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item, max_text_chars=max_text_chars, truncated=truncated)
            for key, item in value.items()
            if not _looks_secret(str(key))
        }
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _sanitize(to_dict(), max_text_chars=max_text_chars, truncated=truncated)
    return _safe_message(str(value))


def _json_text(payload: dict[str, Any], *, max_text_chars: int) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > max_text_chars:
        return text[:max_text_chars] + "...[truncated]"
    return text


def _safe_message(message: str) -> str:
    text = str(message or "").strip()
    return text or "Tool execution failed"


def _looks_secret(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in ("api_key", "secret", "token", "password", "credential"))
