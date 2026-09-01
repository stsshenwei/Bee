from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from app.models.document_models import ParsedImage
from app.models.knowledge_base import KnowledgeBaseScope
from app.services.documents.image_repository import IMAGE_OPERATION_STATUSES, IMAGE_OPERATION_TYPES
from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import (
    PostgresSchemaConfig,
    inspect_postgres_startup_storage,
    qname,
)
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings


class PostgresImageRepository:
    def __init__(
        self,
        database: PostgresDatabase,
        defaults: DefaultKnowledgeBaseSettings | None = None,
        *,
        schema: str | None = None,
        validate_schema: bool = True,
    ):
        self.database = database
        self.defaults = defaults or DefaultKnowledgeBaseSettings()
        self.schema = schema or database.settings.schema
        if validate_schema:
            inspect_postgres_startup_storage(database, config=PostgresSchemaConfig(schema=self.schema))

    def add_image(
        self,
        doc_id: str,
        image: ParsedImage,
        scope: KnowledgeBaseScope,
        *,
        storage_provider: str = "local",
    ) -> dict:
        now = _now()
        with self.database.transaction() as conn:
            self._assert_document(conn, doc_id, scope)
            self._execute_in_conn(
                conn,
                f"""
                insert into {self._table('document_image_resource')}
                (id, workspace_id, knowledge_base_id, doc_id, storage_key, storage_provider, source_type, page_number,
                 mime_type, width, height, metadata_json, created_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    image.image_id,
                    scope.workspace_id,
                    scope.knowledge_base_id,
                    doc_id,
                    image.storage_key,
                    storage_provider,
                    image.source_type,
                    image.page_number,
                    image.mime_type,
                    image.width,
                    image.height,
                    _jsonb_param(image.metadata),
                    now,
                ),
            )
        return self.get_image(image.image_id, scope)

    def get_image(self, image_id: str, scope: KnowledgeBaseScope) -> dict:
        row = self._fetch_one(
            f"""
            select * from {self._table('document_image_resource')}
            where id = %s and workspace_id = %s and knowledge_base_id = %s
            """,
            (image_id, scope.workspace_id, scope.knowledge_base_id),
        )
        if not row:
            raise FileNotFoundError(image_id)
        return self._decode_image(row)

    def list_images(self, doc_id: str, scope: KnowledgeBaseScope) -> list[dict]:
        rows = self._fetch_all(
            f"""
            select * from {self._table('document_image_resource')}
            where doc_id = %s and workspace_id = %s and knowledge_base_id = %s
            order by page_number, created_at, id
            """,
            (doc_id, scope.workspace_id, scope.knowledge_base_id),
        )
        return [self._decode_image(row) for row in rows]

    def create_operation(self, image_id: str, doc_id: str, operation_type: str, scope: KnowledgeBaseScope) -> dict:
        if operation_type not in IMAGE_OPERATION_TYPES:
            raise ValueError("Unsupported image operation")
        now = _now()
        operation_id = uuid4().hex
        with self.database.transaction() as conn:
            self._assert_document(conn, doc_id, scope)
            self._assert_image(conn, image_id, doc_id, scope)
            existing = self._fetch_one_in_conn(
                conn,
                f"""
                select * from {self._table('document_image_operation')}
                where image_id = %s and workspace_id = %s and knowledge_base_id = %s and operation_type = %s
                """,
                (image_id, scope.workspace_id, scope.knowledge_base_id, operation_type),
            )
            if existing is not None:
                return dict(existing)
            self._execute_in_conn(
                conn,
                f"""
                insert into {self._table('document_image_operation')}
                (id, image_id, workspace_id, knowledge_base_id, doc_id, operation_type, status, created_at, updated_at)
                values (%s, %s, %s, %s, %s, %s, 'pending', %s, %s)
                """,
                (operation_id, image_id, scope.workspace_id, scope.knowledge_base_id, doc_id, operation_type, now, now),
            )
        return self.get_operation(operation_id, scope)

    def get_operation(self, operation_id: str, scope: KnowledgeBaseScope) -> dict:
        row = self._fetch_one(
            f"""
            select * from {self._table('document_image_operation')}
            where id = %s and workspace_id = %s and knowledge_base_id = %s
            """,
            (operation_id, scope.workspace_id, scope.knowledge_base_id),
        )
        if not row:
            raise FileNotFoundError(operation_id)
        return dict(row)

    def list_operations(
        self,
        doc_id: str,
        scope: KnowledgeBaseScope,
        *,
        status: str | None = None,
    ) -> list[dict]:
        if status is not None and status not in IMAGE_OPERATION_STATUSES:
            raise ValueError("Invalid operation status")
        clauses = ["doc_id = %s", "workspace_id = %s", "knowledge_base_id = %s"]
        params: list[Any] = [doc_id, scope.workspace_id, scope.knowledge_base_id]
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        rows = self._fetch_all(
            f"select * from {self._table('document_image_operation')} where {' and '.join(clauses)} order by created_at, id",
            tuple(params),
        )
        return [dict(row) for row in rows]

    def update_operation(
        self,
        operation_id: str,
        scope: KnowledgeBaseScope,
        *,
        status: str,
        provider_ref: str = "",
        result_chunk_id: str | None = None,
        error_message: str = "",
        increment_attempt: bool = False,
    ) -> dict:
        if status not in IMAGE_OPERATION_STATUSES:
            raise ValueError("Invalid operation status")
        attempt_assignment = "attempt = attempt + 1," if increment_attempt else ""
        rowcount = self._execute(
            f"""
            update {self._table('document_image_operation')} set status = %s, provider_ref = %s, result_chunk_id = %s,
               error_message = %s, {attempt_assignment} updated_at = %s
            where id = %s and workspace_id = %s and knowledge_base_id = %s
            """,
            (
                status,
                provider_ref,
                result_chunk_id,
                error_message[:1000],
                _now(),
                operation_id,
                scope.workspace_id,
                scope.knowledge_base_id,
            ),
        )
        if rowcount != 1:
            raise FileNotFoundError(operation_id)
        return self.get_operation(operation_id, scope)

    def retry_operation(self, operation_id: str, scope: KnowledgeBaseScope) -> dict:
        operation = self.get_operation(operation_id, scope)
        if operation["status"] in {"processing", "completed"}:
            raise ValueError("Only pending, failed, or canceled image operations can be retried")
        rowcount = self._execute(
            f"""
            update {self._table('document_image_operation')}
            set status = 'pending', provider_ref = '', result_chunk_id = null,
                error_message = '', attempt = attempt + 1, updated_at = %s
            where id = %s and workspace_id = %s and knowledge_base_id = %s
            """,
            (_now(), operation_id, scope.workspace_id, scope.knowledge_base_id),
        )
        if rowcount != 1:
            raise FileNotFoundError(operation_id)
        return self.get_operation(operation_id, scope)

    def cancel_document_operations(self, doc_id: str, scope: KnowledgeBaseScope) -> int:
        return self._execute(
            f"""
            update {self._table('document_image_operation')}
            set status = 'canceled', updated_at = %s
            where doc_id = %s and workspace_id = %s and knowledge_base_id = %s
              and status in ('pending', 'processing')
            """,
            (_now(), doc_id, scope.workspace_id, scope.knowledge_base_id),
        )

    def delete_image(self, image_id: str, scope: KnowledgeBaseScope) -> str:
        with self.database.transaction() as conn:
            row = self._fetch_one_in_conn(
                conn,
                f"""
                select storage_key from {self._table('document_image_resource')}
                where id = %s and workspace_id = %s and knowledge_base_id = %s
                """,
                (image_id, scope.workspace_id, scope.knowledge_base_id),
            )
            if row is None:
                raise FileNotFoundError(image_id)
            self._execute_in_conn(
                conn,
                f"""
                delete from {self._table('document_image_resource')}
                where id = %s and workspace_id = %s and knowledge_base_id = %s
                """,
                (image_id, scope.workspace_id, scope.knowledge_base_id),
            )
        return str(_row_get(row, "storage_key"))

    def delete_document_images(self, doc_id: str, scope: KnowledgeBaseScope) -> list[str]:
        with self.database.transaction() as conn:
            rows = self._fetch_all_in_conn(
                conn,
                f"""
                select storage_key from {self._table('document_image_resource')}
                where doc_id = %s and workspace_id = %s and knowledge_base_id = %s
                """,
                (doc_id, scope.workspace_id, scope.knowledge_base_id),
            )
            keys = [str(_row_get(row, "storage_key")) for row in rows]
            self._execute_in_conn(
                conn,
                f"""
                delete from {self._table('document_image_resource')}
                where doc_id = %s and workspace_id = %s and knowledge_base_id = %s
                """,
                (doc_id, scope.workspace_id, scope.knowledge_base_id),
            )
        return keys

    def cleanup_abandoned_staged_resources(self, doc_id: str, scope: KnowledgeBaseScope) -> list[str]:
        with self.database.transaction() as conn:
            rows = self._fetch_all_in_conn(
                conn,
                f"""
                select r.storage_key
                from {self._table('document_image_resource')} r
                where r.doc_id = %s and r.workspace_id = %s and r.knowledge_base_id = %s
                  and not exists (
                      select 1
                      from {self._table('document_image_operation')} o
                      where o.image_id = r.id
                        and o.workspace_id = r.workspace_id
                        and o.knowledge_base_id = r.knowledge_base_id
                        and o.status in ('processing', 'completed')
                  )
                order by r.created_at, r.id
                """,
                (doc_id, scope.workspace_id, scope.knowledge_base_id),
            )
            keys = [str(_row_get(row, "storage_key")) for row in rows]
            if keys:
                self._execute_in_conn(
                    conn,
                    f"""
                    delete from {self._table('document_image_resource')}
                    where doc_id = %s and workspace_id = %s and knowledge_base_id = %s
                      and storage_key in ({_placeholders(keys)})
                    """,
                    (doc_id, scope.workspace_id, scope.knowledge_base_id, *keys),
                )
        return keys

    def _assert_document(self, conn: Any, doc_id: str, scope: KnowledgeBaseScope) -> None:
        row = self._fetch_one_in_conn(
            conn,
            f"""
            select 1 from {self._table('document')}
            where id = %s and workspace_id = %s and knowledge_base_id = %s
            """,
            (doc_id, scope.workspace_id, scope.knowledge_base_id),
        )
        if row is None:
            raise FileNotFoundError(doc_id)

    def _assert_image(self, conn: Any, image_id: str, doc_id: str, scope: KnowledgeBaseScope) -> None:
        row = self._fetch_one_in_conn(
            conn,
            f"""
            select 1 from {self._table('document_image_resource')}
            where id = %s and doc_id = %s and workspace_id = %s and knowledge_base_id = %s
            """,
            (image_id, doc_id, scope.workspace_id, scope.knowledge_base_id),
        )
        if row is None:
            raise FileNotFoundError(image_id)

    def _decode_image(self, row: Any) -> dict:
        result = dict(row)
        result["metadata"] = _load_json(result.pop("metadata_json", None), {})
        return result

    def _fetch_one(self, sql: str, params: tuple[Any, ...]) -> Any | None:
        with self.database.connection() as conn:
            return self._fetch_one_in_conn(conn, sql, params)

    def _fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
        with self.database.connection() as conn:
            return self._fetch_all_in_conn(conn, sql, params)

    def _execute(self, sql: str, params: tuple[Any, ...]) -> int:
        with self.database.transaction() as conn:
            return self._execute_in_conn(conn, sql, params)

    def _fetch_one_in_conn(self, conn: Any, sql: str, params: tuple[Any, ...]) -> Any | None:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _fetch_all_in_conn(self, conn: Any, sql: str, params: tuple[Any, ...]) -> list[Any]:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def _execute_in_conn(self, conn: Any, sql: str, params: tuple[Any, ...]) -> int:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return int(getattr(cur, "rowcount", 0) or 0)

    def _table(self, table: str) -> str:
        return qname(self.schema, table)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _jsonb_param(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _load_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value or json.dumps(default))
        except json.JSONDecodeError:
            return default
    return value


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


def _placeholders(items: Any) -> str:
    count = len(tuple(items))
    if count <= 0:
        raise ValueError("At least one value is required for SQL placeholders")
    return ", ".join("%s" for _ in range(count))
