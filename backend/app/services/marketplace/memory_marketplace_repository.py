from __future__ import annotations

import uuid
from typing import Any

from app.services.marketplace.marketplace_models import (
    MarketplaceOwner,
    MarketplacePackage,
    MarketplacePackageVersion,
    MarketplaceSnapshot,
    MarketplaceToken,
)
from app.services.marketplace.postgres_marketplace_repository import hash_token


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class InMemoryMarketplaceRepository:
    """In-memory implementation of the marketplace repository interface.

    Mirrors :class:`PostgresMarketplaceRepository` semantics for tests and
    lightweight deployments.
    """

    def __init__(self) -> None:
        self.owners: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.packages: dict[str, dict[str, Any]] = {}
        self.versions: dict[tuple[str, str], dict[str, Any]] = {}
        self.snapshot: dict[str, Any] | None = None

    # ------------------------------------------------------------------ owners

    def get_owner(self, handle: str) -> MarketplaceOwner | None:
        row = self.owners.get(handle)
        if row is None:
            return None
        return MarketplaceOwner(
            handle=handle,
            kind=row["kind"],
            display_name=row["display_name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_or_create_owner(self, handle: str, *, kind: str = "user", display_name: str = "") -> MarketplaceOwner:
        row = self.owners.get(handle)
        if row is None:
            row = {"kind": kind, "display_name": display_name or handle, "created_at": _now(), "updated_at": _now()}
            self.owners[handle] = row
        return self.get_owner(handle)

    # ------------------------------------------------------------------ tokens

    def upsert_token(self, token: str, owner_handle: str, *, scopes: tuple[str, ...]) -> MarketplaceToken:
        self.get_or_create_owner(owner_handle)
        token_hash = hash_token(token)
        row = self.tokens.get(token_hash)
        if row is None:
            row = {"id": uuid.uuid4().hex, "owner_handle": owner_handle, "scopes": list(scopes), "created_at": _now()}
            self.tokens[token_hash] = row
        else:
            row["owner_handle"] = owner_handle
            row["scopes"] = list(scopes)
            row["revoked_at"] = None
        return MarketplaceToken(
            id=row["id"],
            token_hash=token_hash,
            owner_handle=owner_handle,
            scopes=tuple(scopes),
            created_at=row["created_at"],
        )

    def resolve_token(self, token: str) -> MarketplaceToken | None:
        row = self.tokens.get(hash_token(token))
        if row is None or row.get("revoked_at"):
            return None
        return MarketplaceToken(
            id=row["id"],
            token_hash=hash_token(token),
            owner_handle=row["owner_handle"],
            scopes=tuple(row["scopes"]),
            created_at=row["created_at"],
            last_used_at=row.get("last_used_at") or "",
        )

    def touch_token(self, token_id: str) -> None:
        for row in self.tokens.values():
            if row["id"] == token_id:
                row["last_used_at"] = _now()

    # ---------------------------------------------------------------- packages

    def get_package(self, name: str) -> MarketplacePackage | None:
        row = self.packages.get(name)
        if row is None:
            return None
        return MarketplacePackage(
            name=name,
            owner_handle=row["owner_handle"],
            description=row["description"],
            category=row["category"],
            keywords=tuple(row["keywords"]),
            visibility=row["visibility"],
            latest_version=row["latest_version"],
            components=dict(row.get("components") or {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_packages(
        self,
        *,
        owner_handle: str | None = None,
        query: str = "",
        category: str = "",
        include_private: bool = False,
    ) -> list[MarketplacePackage]:
        results = []
        for name in sorted(self.packages):
            package = self.get_package(name)
            if package is None:
                continue
            if owner_handle and package.owner_handle != owner_handle:
                continue
            if not include_private and package.visibility != "public":
                continue
            if query.strip():
                needle = query.strip().lower()
                haystack = f"{package.name} {package.description} {package.category}".lower()
                if needle not in haystack:
                    continue
            if category.strip() and package.category != category.strip():
                continue
            results.append(package)
        return results

    def insert_package(
        self,
        *,
        owner_handle: str,
        name: str,
        description: str = "",
        category: str = "",
        keywords: tuple[str, ...] = (),
        visibility: str = "public",
    ) -> MarketplacePackage:
        self.get_or_create_owner(owner_handle)
        if name in self.packages:
            raise KeyError(f"package exists: {name}")
        now = _now()
        self.packages[name] = {
            "owner_handle": owner_handle,
            "description": description,
            "category": category,
            "keywords": list(keywords),
            "visibility": visibility,
            "latest_version": "",
            "components": {},
            "created_at": now,
            "updated_at": now,
        }
        return self.get_package(name)

    def update_package(
        self,
        name: str,
        *,
        description: str | None = None,
        category: str | None = None,
        keywords: tuple[str, ...] | None = None,
        visibility: str | None = None,
    ) -> MarketplacePackage:
        row = self.packages.get(name)
        if row is None:
            raise KeyError(name)
        if description is not None:
            row["description"] = description
        if category is not None:
            row["category"] = category
        if keywords is not None:
            row["keywords"] = list(keywords)
        if visibility is not None:
            row["visibility"] = visibility
        row["updated_at"] = _now()
        return self.get_package(name)

    def delete_package(self, name: str) -> None:
        self.packages.pop(name, None)
        for key in [key for key in self.versions if key[0] == name]:
            self.versions.pop(key)

    def recompute_latest_version(self, package_name: str) -> str:
        row = self.packages.get(package_name)
        if row is None:
            return ""
        published = [
            (key[1], entry)
            for key, entry in self.versions.items()
            if key[0] == package_name and entry["status"] == "published"
        ]
        published.sort(key=lambda item: (item[1]["published_at"], item[0]), reverse=True)
        row["latest_version"] = published[0][0] if published else ""
        return row["latest_version"]

    # ---------------------------------------------------------------- versions

    def get_version(self, package_name: str, version: str) -> MarketplacePackageVersion | None:
        row = self.versions.get((package_name, version))
        if row is None:
            return None
        return self._version_row_to_model(package_name, version, row)

    def list_versions(self, package_name: str) -> list[MarketplacePackageVersion]:
        rows = [(key[1], row) for key, row in self.versions.items() if key[0] == package_name]
        rows.sort(key=lambda item: (item[1]["published_at"], item[0]), reverse=True)
        return [self._version_row_to_model(package_name, version, row) for version, row in rows]

    def latest_published_version(self, package_name: str) -> MarketplacePackageVersion | None:
        published = [
            (version, row)
            for (name, version), row in self.versions.items()
            if name == package_name and row["status"] == "published"
        ]
        if not published:
            return None
        published.sort(key=lambda item: (item[1]["published_at"], item[0]), reverse=True)
        version, row = published[0]
        return self._version_row_to_model(package_name, version, row)

    def insert_version(
        self,
        *,
        package_name: str,
        version: str,
        manifest: dict[str, Any],
        components: dict[str, Any],
        content_hash: str,
        size_bytes: int,
        storage_key: str,
        published_by_owner_handle: str,
    ) -> MarketplacePackageVersion:
        published_at = _now()
        self.versions[(package_name, version)] = {
            "manifest": dict(manifest),
            "components": dict(components),
            "content_hash": content_hash,
            "size_bytes": size_bytes,
            "storage_key": storage_key,
            "status": "published",
            "published_by": published_by_owner_handle,
            "published_at": published_at,
        }
        package = self.get_package(package_name)
        if package is not None:
            self.packages[package_name]["components"] = dict(components)
        return MarketplacePackageVersion(
            package_name=package_name,
            owner_handle=package.owner_handle if package else "",
            version=version,
            manifest=dict(manifest),
            components=dict(components),
            content_hash=content_hash,
            size_bytes=size_bytes,
            storage_key=storage_key,
            status="published",
            published_at=published_at,
        )

    def set_version_status(self, package_name: str, version: str, status: str) -> None:
        row = self.versions.get((package_name, version))
        if row is not None:
            row["status"] = status

    def delete_version(self, package_name: str, version: str) -> None:
        self.versions.pop((package_name, version), None)

    # --------------------------------------------------------------- snapshots

    def get_snapshot(self) -> MarketplaceSnapshot | None:
        if self.snapshot is None:
            return None
        return MarketplaceSnapshot(
            revision=self.snapshot["revision"],
            catalog=dict(self.snapshot["catalog"]),
            package_count=self.snapshot["package_count"],
            built_at=self.snapshot["built_at"],
        )

    def upsert_snapshot(
        self,
        *,
        revision: str,
        catalog: dict[str, Any],
        package_count: int,
        built_at: str,
    ) -> MarketplaceSnapshot:
        self.snapshot = {
            "revision": revision,
            "catalog": dict(catalog),
            "package_count": package_count,
            "built_at": built_at,
        }
        return self.get_snapshot()

    # ----------------------------------------------------------------- helpers

    def _version_row_to_model(
        self, package_name: str, version: str, row: dict[str, Any]
    ) -> MarketplacePackageVersion:
        package = self.get_package(package_name)
        return MarketplacePackageVersion(
            package_name=package_name,
            owner_handle=package.owner_handle if package else "",
            version=version,
            manifest=dict(row["manifest"]),
            components=dict(row["components"]),
            content_hash=row["content_hash"],
            size_bytes=row["size_bytes"],
            storage_key=row["storage_key"],
            status=row["status"],
            published_at=row["published_at"],
        )
