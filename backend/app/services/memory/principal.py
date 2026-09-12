from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Principal:
    kind: str = "anonymous"
    tenant_id: str = ""
    user_id: str = ""


def anonymous_principal() -> Principal:
    return Principal()


def session_owner_id_from_context(principal: Principal | None) -> str:
    principal = principal or anonymous_principal()
    if principal.kind == "api_tenant" and not principal.user_id:
        return ""
    return principal.user_id or ""


def principal_from_request(request: Any | None) -> Principal:
    if request is None:
        return anonymous_principal()
    headers = getattr(request, "headers", {}) or {}
    tenant_id = _header(headers, "x-tenant-id") or _header(headers, "x-api-tenant-id")
    user_id = _header(headers, "x-user-id") or _header(headers, "x-web-user-id") or _header(headers, "x-im-user-id")
    kind = _header(headers, "x-principal-kind")
    if not kind:
        if _header(headers, "x-im-user-id"):
            kind = "im_user"
        elif _header(headers, "x-api-tenant-id"):
            kind = "api_tenant"
        elif user_id:
            kind = "web_user"
        else:
            kind = "anonymous"
    return Principal(kind=kind, tenant_id=tenant_id, user_id=user_id)


def _header(headers: Any, name: str) -> str:
    value = ""
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name) or getter(name.title()) or ""
    return str(value or "").strip()
