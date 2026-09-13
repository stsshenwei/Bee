from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from app.services.marketplace.marketplace_models import (
    MarketplaceOwner,
    MarketplacePackage,
    MarketplacePackageVersion,
    MarketplaceSnapshot,
    MarketplaceToken,
)
from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import (
    PostgresSchemaConfig,
    ensure_postgres_marketplace_schema,
    qname,
)


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token).strip().encode("utf-8")).hexdigest()


def _new_id() -> str:
    return uuid.uuid4().hex


class PostgresMarketplaceRepository:
    """PostgreSQL persistence for marketplace owners, tokens, packages, and snapshots.

    Keying: owners are keyed by ``handle``; packages are keyed by their
    globally-unique ``name``; versions hang off ``(package_name, version)``.
    """

    def __init__(self, database: PostgresDatabase, *, schema: str | None = None):
        self.database = database
        self.schema = schema or database.settings.schema
        ensure_postgres_marketplace_schema(database, config=PostgresSchemaConfig(schema=self.schema))

    # ------------------------------------------------------------------ owners

    def get_owner(self, handle: str) -> MarketplaceOwner | None:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"select handle, kind, display_name, created_at, updated_at "
                    f"from {qname(self.schema, 'marketplace_owner')} where handle = %s",
                    (handle,),
                )
                row = cur.fetchone()
        return _owner_from_row(row) if row else None

    def get_or_create_owner(self, handle: str, *, kind: str = "user", display_name: str = "") -> MarketplaceOwner:
        existing = self.get_owner(handle)
        if existing is not None:
            return existing
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    insert into {qname(self.schema, 'marketplace_owner')}(handle, kind, display_name, created_at, updated_at)
                    values (%s, %s, %s, now(), now())
                    on conflict(handle) do update set updated_at = now()
                    returning handle, kind, display_name, created_at, updated_at
                    """,
                    (handle, kind, display_name or handle),
                )
                row = cur.fetchone()
        return _owner_from_row(row)

    # ------------------------------------------------------------------ tokens

    def upsert_token(self, token: str, owner_handle: str, *, scopes: tuple[str, ...]) -> MarketplaceToken:
        self.get_or_create_owner(owner_handle)
        token_hash = hash_token(token)
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    insert into {qname(self.schema, 'marketplace_token')}(
                        id, token_hash, owner_handle, scopes_json, created_at
                    )
                    values (%s, %s, %s, %s::jsonb, now())
                    on conflict(token_hash) do update set
                        owner_handle = excluded.owner_handle,
                        scopes_json = excluded.scopes_json,
                        revoked_at = null
                    returning id, created_at, last_used_at, revoked_at
                    """,
                    (
                        _new_id(),
                        token_hash,
                        owner_handle,
                        json.dumps(list(scopes), ensure_ascii=False),
                    ),
                )
                row = cur.fetchone()
        return MarketplaceToken(
            id=str(row["id"]),
            token_hash=token_hash,
            owner_handle=owner_handle,
            scopes=tuple(scopes),
            created_at=str(row.get("created_at") or ""),
            last_used_at=str(row.get("last_used_at") or ""),
            revoked_at=str(row.get("revoked_at") or ""),
        )

    def resolve_token(self, token: str) -> MarketplaceToken | None:
        token_hash = hash_token(token)
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"select id, token_hash, owner_handle, scopes_json, created_at, last_used_at, revoked_at "
                    f"from {qname(self.schema, 'marketplace_token')} where token_hash = %s",
                    (token_hash,),
                )
                row = cur.fetchone()
        if row is None or row.get("revoked_at"):
            return None
        return MarketplaceToken(
            id=str(row["id"]),
            token_hash=str(row["token_hash"]),
            owner_handle=str(row["owner_handle"]),
            scopes=tuple(str(item) for item in _load_json(row.get("scopes_json"), []) if str(item)),
            created_at=str(row.get("created_at") or ""),
            last_used_at=str(row.get("last_used_at") or ""),
            revoked_at=str(row.get("revoked_at") or ""),
        )

    def touch_token(self, token_id: str) -> None:
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"update {qname(self.schema, 'marketplace_token')} set last_used_at = now() where id = %s",
                    (token_id,),
                )

    # ---------------------------------------------------------------- packages

    def get_package(self, name: str) -> MarketplacePackage | None:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_package_select(self.schema) + " where p.name = %s", (name,))
                row = cur.fetchone()
        return _package_from_row(row) if row else None

    def list_packages(
        self,
        *,
        owner_handle: str | None = None,
        query: str = "",
        category: str = "",
        include_private: bool = False,
    ) -> list[MarketplacePackage]:
        conditions = ["1 = 1"]
        params: list[Any] = []
        if owner_handle:
            conditions.append("p.owner_handle = %s")
            params.append(owner_handle)
        if not include_private:
            conditions.append("p.visibility = 'public'")
        if query.strip():
            conditions.append("(p.name ilike %s or p.description ilike %s or p.category ilike %s)")
            like = f"%{query.strip()}%"
            params.extend([like, like, like])
        if category.strip():
            conditions.append("p.category = %s")
            params.append(category.strip())
        where = " where " + " and ".join(conditions)
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_package_select(self.schema) + where + " order by p.name", tuple(params))
                rows = cur.fetchall()
        return [_package_from_row(row) for row in rows]

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
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    insert into {qname(self.schema, 'marketplace_package')}(
                        name, owner_handle, description, category, keywords_json, visibility, created_at, updated_at
                    )
                    values (%s, %s, %s, %s, %s::jsonb, %s, now(), now())
                    """,
                    (
                        name,
                        owner_handle,
                        description,
                        category,
                        json.dumps(list(keywords), ensure_ascii=False),
                        visibility,
                    ),
                )
                cur.execute(_package_select(self.schema) + " where p.name = %s", (name,))
                row = cur.fetchone()
        return _package_from_row(row)

    def update_package(
        self,
        name: str,
        *,
        description: str | None = None,
        category: str | None = None,
        keywords: tuple[str, ...] | None = None,
        visibility: str | None = None,
    ) -> MarketplacePackage:
        assignments = ["updated_at = now()"]
        params: list[Any] = []
        if description is not None:
            assignments.append("description = %s")
            params.append(description)
        if category is not None:
            assignments.append("category = %s")
            params.append(category)
        if keywords is not None:
            assignments.append("keywords_json = %s::jsonb")
            params.append(json.dumps(list(keywords), ensure_ascii=False))
        if visibility is not None:
            assignments.append("visibility = %s")
            params.append(visibility)
        params.append(name)
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"update {qname(self.schema, 'marketplace_package')} set "
                    + ", ".join(assignments)
                    + " where name = %s",
                    tuple(params),
                )
                cur.execute(_package_select(self.schema) + " where p.name = %s", (name,))
                row = cur.fetchone()
        if row is None:
            raise KeyError(name)
        return _package_from_row(row)

    def delete_package(self, name: str) -> None:
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"delete from {qname(self.schema, 'marketplace_package')} where name = %s",
                    (name,),
                )

    def recompute_latest_version(self, package_name: str) -> str:
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    update {qname(self.schema, 'marketplace_package')} p
                    set latest_version = coalesce(v.version, ''), updated_at = now()
                    from (
                        select package_name, version
                        from {qname(self.schema, 'marketplace_package_version')}
                        where package_name = %s and status = 'published'
                        order by published_at desc, version desc
                        limit 1
                    ) v
                    where p.name = v.package_name
                    returning p.latest_version
                    """,
                    (package_name,),
                )
                row = cur.fetchone()
        return str(row["latest_version"]) if row else ""

    # ---------------------------------------------------------------- versions

    def get_version(self, package_name: str, version: str) -> MarketplacePackageVersion | None:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _version_select(self.schema) + " where v.package_name = %s and v.version = %s",
                    (package_name, version),
                )
                row = cur.fetchone()
        return _version_from_row(row) if row else None

    def list_versions(self, package_name: str) -> list[MarketplacePackageVersion]:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _version_select(self.schema)
                    + " where v.package_name = %s order by v.published_at desc, v.version desc",
                    (package_name,),
                )
                rows = cur.fetchall()
        return [_version_from_row(row) for row in rows]

    def latest_published_version(self, package_name: str) -> MarketplacePackageVersion | None:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    _version_select(self.schema)
                    + """
                    where v.package_name = %s and v.status = 'published'
                    order by v.published_at desc, v.version desc
                    limit 1
                    """,
                    (package_name,),
                )
                row = cur.fetchone()
        return _version_from_row(row) if row else None

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
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    insert into {qname(self.schema, 'marketplace_package_version')}(
                        id, package_name, version, manifest_json, components_json, content_hash,
                        size_bytes, storage_key, status, published_by_owner_handle, published_at
                    )
                    values (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, 'published', %s, now())
                    returning published_at
                    """,
                    (
                        _new_id(),
                        package_name,
                        version,
                        json.dumps(manifest, ensure_ascii=False),
                        json.dumps(components, ensure_ascii=False),
                        content_hash,
                        size_bytes,
                        storage_key,
                        published_by_owner_handle,
                    ),
                )
                row = cur.fetchone()
        package = self.get_package(package_name)
        return MarketplacePackageVersion(
            package_name=package.name if package else package_name,
            owner_handle=package.owner_handle if package else "",
            version=version,
            manifest=dict(manifest),
            components=dict(components),
            content_hash=content_hash,
            size_bytes=size_bytes,
            storage_key=storage_key,
            status="published",
            published_at=str(row.get("published_at") or ""),
        )

    def set_version_status(self, package_name: str, version: str, status: str) -> None:
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"update {qname(self.schema, 'marketplace_package_version')} "
                    "set status = %s where package_name = %s and version = %s",
                    (status, package_name, version),
                )

    def delete_version(self, package_name: str, version: str) -> None:
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"delete from {qname(self.schema, 'marketplace_package_version')} "
                    "where package_name = %s and version = %s",
                    (package_name, version),
                )

    # --------------------------------------------------------------- snapshots

    def get_snapshot(self) -> MarketplaceSnapshot | None:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"select revision, catalog_json, package_count, built_at "
                    f"from {qname(self.schema, 'marketplace_snapshot')} where id = 'live'"
                )
                row = cur.fetchone()
        if row is None:
            return None
        return MarketplaceSnapshot(
            revision=str(row["revision"]),
            catalog=dict(_load_json(row.get("catalog_json"), {})),
            package_count=int(row.get("package_count") or 0),
            built_at=str(row.get("built_at") or ""),
        )

    def upsert_snapshot(
        self,
        *,
        revision: str,
        catalog: dict[str, Any],
        package_count: int,
        built_at: str,
    ) -> MarketplaceSnapshot:
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    insert into {qname(self.schema, 'marketplace_snapshot')}(
                        id, revision, catalog_json, package_count, built_at
                    )
                    values ('live', %s, %s::jsonb, %s, %s)
                    on conflict(id) do update set
                        revision = excluded.revision,
                        catalog_json = excluded.catalog_json,
                        package_count = excluded.package_count,
                        built_at = excluded.built_at
                    """,
                    (
                        revision,
                        json.dumps(catalog, ensure_ascii=False),
                        package_count,
                        built_at,
                    ),
                )
        return MarketplaceSnapshot(
            revision=revision,
            catalog=catalog,
            package_count=package_count,
            built_at=built_at,
        )


def _package_select(schema: str) -> str:
    return f"""
    select p.name, p.owner_handle, p.description, p.category,
           p.keywords_json, p.visibility, p.latest_version, p.created_at, p.updated_at,
           v.components_json
    from {qname(schema, 'marketplace_package')} p
    left join lateral (
        select components_json
        from {qname(schema, 'marketplace_package_version')}
        where package_name = p.name and status = 'published'
        order by published_at desc, version desc
        limit 1
    ) v on true
    """


def _version_select(schema: str) -> str:
    return f"""
    select v.id, v.version, v.manifest_json, v.components_json, v.content_hash,
           v.size_bytes, v.storage_key, v.status, v.published_at,
           v.package_name, p.owner_handle
    from {qname(schema, 'marketplace_package_version')} v
    join {qname(schema, 'marketplace_package')} p on p.name = v.package_name
    """


def _owner_from_row(row: dict[str, Any]) -> MarketplaceOwner:
    return MarketplaceOwner(
        handle=str(row["handle"]),
        kind=str(row.get("kind") or "user"),
        display_name=str(row.get("display_name") or ""),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


def _package_from_row(row: dict[str, Any]) -> MarketplacePackage:
    return MarketplacePackage(
        name=str(row["name"]),
        owner_handle=str(row["owner_handle"]),
        description=str(row.get("description") or ""),
        category=str(row.get("category") or ""),
        keywords=tuple(str(item) for item in _load_json(row.get("keywords_json"), []) if str(item)),
        visibility=str(row.get("visibility") or "public"),
        latest_version=str(row.get("latest_version") or ""),
        components=dict(_load_json(row.get("components_json"), {})),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


def _version_from_row(row: dict[str, Any]) -> MarketplacePackageVersion:
    return MarketplacePackageVersion(
        package_name=str(row["package_name"]),
        owner_handle=str(row["owner_handle"]),
        version=str(row["version"]),
        manifest=dict(_load_json(row.get("manifest_json"), {})),
        components=dict(_load_json(row.get("components_json"), {})),
        content_hash=str(row.get("content_hash") or ""),
        size_bytes=int(row.get("size_bytes") or 0),
        storage_key=str(row.get("storage_key") or ""),
        status=str(row.get("status") or "published"),
        published_at=str(row.get("published_at") or ""),
    )


def _load_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default
