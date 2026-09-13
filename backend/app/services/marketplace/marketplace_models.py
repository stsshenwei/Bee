from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping


KEBAB_CASE_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)

PACKAGE_VISIBILITIES = {"public", "private"}
VERSION_STATUSES = {"published", "yanked"}
OWNER_KINDS = {"user", "org"}
TOKEN_SCOPES = {"publish", "admin"}


class MarketplaceError(Exception):
    """Base error for the plugin marketplace domain."""


class MarketplaceValidationError(MarketplaceError):
    """Raised when an upload or update fails validation (HTTP 400)."""


class MarketplaceNotFoundError(MarketplaceError):
    """Raised when a package, version, or snapshot is missing (HTTP 404)."""


class MarketplaceConflictError(MarketplaceError):
    """Raised on duplicate names or versions (HTTP 409)."""


class MarketplaceAuthError(MarketplaceError):
    """Raised when bearer authentication fails (HTTP 401)."""


class MarketplaceForbiddenError(MarketplaceError):
    """Raised when the token lacks rights over the target owner (HTTP 403)."""


@dataclass(frozen=True)
class MarketplacePrincipal:
    """Identity resolved from a bearer token."""

    owner_handle: str
    scopes: tuple[str, ...] = ()

    @property
    def is_admin(self) -> bool:
        return "admin" in self.scopes

    @property
    def can_publish(self) -> bool:
        return self.is_admin or "publish" in self.scopes

    def to_dict(self) -> dict[str, Any]:
        return {"owner_handle": self.owner_handle, "scopes": list(self.scopes)}


@dataclass(frozen=True)
class MarketplaceSettings:
    marketplace_name: str = "bee-plugins"
    marketplace_description: str = "Bee 插件市场"
    owner_display_name: str = "Bee"
    storage_dir: str = os.path.join("data", "marketplace")
    max_upload_bytes: int = 20 * 1024 * 1024
    max_entry_count: int = 4000
    max_total_uncompressed_bytes: int = 512 * 1024 * 1024
    admin_token: str = ""
    owner_tokens: Mapping[str, str] = field(default_factory=dict)
    git_mirror_enabled: bool = True

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "MarketplaceSettings":
        env = environ if environ is not None else os.environ
        name = str(env.get("MARKETPLACE_MARKETPLACE_NAME") or "").strip() or "bee-plugins"
        if not KEBAB_CASE_PATTERN.match(name):
            raise MarketplaceValidationError("MARKETPLACE_MARKETPLACE_NAME 必须是 kebab-case")
        storage_dir = str(env.get("MARKETPLACE_STORAGE_DIR") or "").strip() or os.path.join("data", "marketplace")
        max_upload = _positive_int(env, "MARKETPLACE_MAX_UPLOAD_BYTES", 20 * 1024 * 1024)
        max_entries = _positive_int(env, "MARKETPLACE_MAX_ENTRY_COUNT", 4000)
        return cls(
            marketplace_name=name,
            marketplace_description=str(env.get("MARKETPLACE_DESCRIPTION") or "").strip() or "Bee 插件市场",
            owner_display_name=str(env.get("MARKETPLACE_OWNER_NAME") or "").strip() or "Bee",
            storage_dir=storage_dir,
            max_upload_bytes=max_upload,
            max_entry_count=max_entries,
            admin_token=str(env.get("MARKETPLACE_ADMIN_TOKEN") or "").strip(),
            owner_tokens=_parse_owner_tokens(env.get("MARKETPLACE_OWNER_TOKENS") or ""),
            git_mirror_enabled=_parse_bool(env.get("MARKETPLACE_GIT_MIRROR_ENABLED"), default=True),
        )

    def validate_bundle_size(self, size: int) -> None:
        if size > self.max_upload_bytes:
            raise MarketplaceValidationError(
                f"插件包超过大小上限：{size} > {self.max_upload_bytes} 字节"
            )


@dataclass(frozen=True)
class MarketplaceOwner:
    handle: str
    kind: str = "user"
    display_name: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "kind": self.kind,
            "display_name": self.display_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class MarketplaceToken:
    id: str
    token_hash: str
    owner_handle: str
    scopes: tuple[str, ...] = ()
    created_at: str = ""
    last_used_at: str = ""
    revoked_at: str = ""

    def to_dict(self, *, include_hash: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "owner_handle": self.owner_handle,
            "scopes": list(self.scopes),
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "revoked_at": self.revoked_at,
        }
        if include_hash:
            payload["token_hash"] = self.token_hash
        return payload


@dataclass(frozen=True)
class MarketplacePackage:
    name: str
    owner_handle: str
    description: str = ""
    category: str = ""
    keywords: tuple[str, ...] = ()
    visibility: str = "public"
    latest_version: str = ""
    components: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "owner": self.owner_handle,
            "description": self.description,
            "category": self.category,
            "keywords": list(self.keywords),
            "visibility": self.visibility,
            "latest_version": self.latest_version,
            "components": dict(self.components),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class MarketplacePackageVersion:
    package_name: str
    owner_handle: str
    version: str
    manifest: dict[str, Any] = field(default_factory=dict)
    components: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    size_bytes: int = 0
    storage_key: str = ""
    status: str = "published"
    published_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package_name,
            "owner": self.owner_handle,
            "version": self.version,
            "manifest": dict(self.manifest),
            "components": dict(self.components),
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "published_at": self.published_at,
        }


@dataclass(frozen=True)
class MarketplaceSnapshot:
    revision: str
    catalog: dict[str, Any]
    package_count: int = 0
    built_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "package_count": self.package_count,
            "built_at": self.built_at,
        }


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    value = str(raw or "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = str(env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise MarketplaceValidationError(f"{key} 必须是整数") from exc
    if value <= 0:
        raise MarketplaceValidationError(f"{key} 必须大于 0")
    return value


def _parse_owner_tokens(raw: str) -> dict[str, str]:
    """Parse ``token=owner:scope`` pairs separated by ``;``."""

    tokens: dict[str, str] = {}
    for item in str(raw or "").split(";"):
        if "=" not in item:
            continue
        token, assignment = item.split("=", 1)
        token = token.strip()
        assignment = assignment.strip()
        if not token or ":" not in assignment:
            continue
        owner, scope = assignment.split(":", 1)
        owner = owner.strip()
        scope = scope.strip().lower()
        if not owner or scope not in TOKEN_SCOPES:
            continue
        tokens[token] = f"{owner}:{scope}"
    return tokens
