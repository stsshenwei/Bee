from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from app.models.knowledge_base import KnowledgeBaseScope
from app.services.documents.upload_batch_repository import (
    BATCH_STATUSES,
    FILE_STATUSES,
    PROCESSING_PHASES,
    TERMINAL_BATCH_STATUSES,
    _aggregate,
    _normalize_phases,
    _sanitize_error,
    initial_phase_report,
)
from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import (
    PostgresSchemaConfig,
    inspect_postgres_startup_storage,
    qname,
)
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings


class PostgresUploadBatchRepository:
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

    def create_batch(self, scope: KnowledgeBaseScope, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        batch_id = f"upload-{uuid4().hex}"
        now = _now()
        with self.database.transaction() as conn:
            self._assert_active_knowledge_base(conn, scope)
            self._execute_in_conn(
                conn,
                f"""
                insert into {self._table('knowledge_upload_batch')}(
                    id, workspace_id, knowledge_base_id, status, settings_json, error_message,
                    created_at, updated_at, confirmed_at, completed_at
                ) values (%s, %s, %s, 'draft', %s::jsonb, '', %s, %s, null, null)
                """,
                (batch_id, scope.workspace_id, scope.knowledge_base_id, _jsonb_param(settings or {}), now, now),
            )
        return self.get_batch(batch_id, scope)

    def get_batch(self, batch_id: str, scope: KnowledgeBaseScope) -> dict[str, Any]:
        batch = self._fetch_one(
            f"""
            select * from {self._table('knowledge_upload_batch')}
            where id = %s and workspace_id = %s and knowledge_base_id = %s
            """,
            (batch_id, scope.workspace_id, scope.knowledge_base_id),
        )
        if batch is None:
            raise KeyError(batch_id)
        files = self._fetch_all(
            f"""
            select * from {self._table('knowledge_upload_file')}
            where batch_id = %s and workspace_id = %s and knowledge_base_id = %s
            order by created_at, id
            """,
            (batch_id, scope.workspace_id, scope.knowledge_base_id),
        )
        data = self._decode_batch(batch)
        decoded_files = [self._decode_file(file_row) for file_row in files]
        data["files"] = decoded_files
        data["aggregate"] = _aggregate(decoded_files)
        return data

    def list_batches(self, scope: KnowledgeBaseScope, include_terminal: bool = True) -> list[dict[str, Any]]:
        clauses = ["workspace_id = %s", "knowledge_base_id = %s"]
        params: list[Any] = [scope.workspace_id, scope.knowledge_base_id]
        if not include_terminal:
            clauses.append(f"status not in ({_placeholders(TERMINAL_BATCH_STATUSES)})")
            params.extend(sorted(TERMINAL_BATCH_STATUSES))
        rows = self._fetch_all(
            f"select * from {self._table('knowledge_upload_batch')} where {' and '.join(clauses)} order by updated_at desc",
            tuple(params),
        )
        return [self.get_batch(str(_row_get(row, "id")), scope) for row in rows]

    def update_batch(
        self,
        batch_id: str,
        scope: KnowledgeBaseScope,
        *,
        status: str | None = None,
        settings: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        if status is not None and status not in BATCH_STATUSES:
            raise ValueError("Unsupported upload batch status")
        assignments = ["updated_at = %s"]
        values: list[Any] = [_now()]
        if status is not None:
            assignments.append("status = %s")
            values.append(status)
            if status == "processing":
                assignments.append("confirmed_at = coalesce(confirmed_at, %s)")
                values.append(_now())
            if status in TERMINAL_BATCH_STATUSES:
                assignments.append("completed_at = coalesce(completed_at, %s)")
                values.append(_now())
        if settings is not None:
            assignments.append("settings_json = %s::jsonb")
            values.append(_jsonb_param(settings))
        if error_message is not None:
            assignments.append("error_message = %s")
            values.append(_sanitize_error(error_message))
        values.extend([batch_id, scope.workspace_id, scope.knowledge_base_id])
        rowcount = self._execute(
            f"""
            update {self._table('knowledge_upload_batch')}
            set {', '.join(assignments)}
            where id = %s and workspace_id = %s and knowledge_base_id = %s
            """,
            tuple(values),
        )
        if rowcount == 0:
            raise KeyError(batch_id)
        return self.get_batch(batch_id, scope)

    def add_file(
        self,
        batch_id: str,
        scope: KnowledgeBaseScope,
        *,
        original_name: str,
        relative_path: str,
        storage_path: str,
        size: int,
    ) -> dict[str, Any]:
        file_id = f"upload-file-{uuid4().hex}"
        now = _now()
        with self.database.transaction() as conn:
            batch = self._fetch_one_in_conn(
                conn,
                f"""
                select status from {self._table('knowledge_upload_batch')}
                where id = %s and workspace_id = %s and knowledge_base_id = %s
                """,
                (batch_id, scope.workspace_id, scope.knowledge_base_id),
            )
            if batch is None:
                raise KeyError(batch_id)
            if str(_row_get(batch, "status")) not in {"draft", "uploading", "ready_to_process"}:
                raise ValueError("Upload batch does not accept more files")
            self._execute_in_conn(
                conn,
                f"""
                insert into {self._table('knowledge_upload_file')}(
                    id, batch_id, workspace_id, knowledge_base_id, original_name, relative_path,
                    storage_path, size, status, document_id, chunks, error_message, phases_json,
                    warnings_json, errors_json, retry_eligible, created_at, updated_at
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, 'uploaded', null, 0, '', %s::jsonb, '[]'::jsonb, '[]'::jsonb, false, %s, %s)
                """,
                (
                    file_id,
                    batch_id,
                    scope.workspace_id,
                    scope.knowledge_base_id,
                    original_name,
                    relative_path,
                    storage_path,
                    int(size),
                    _jsonb_param(initial_phase_report()),
                    now,
                    now,
                ),
            )
            self._execute_in_conn(
                conn,
                f"""
                update {self._table('knowledge_upload_batch')}
                set status = case when status = 'draft' then 'uploading' else status end,
                    updated_at = %s
                where id = %s and workspace_id = %s and knowledge_base_id = %s
                """,
                (now, batch_id, scope.workspace_id, scope.knowledge_base_id),
            )
        return self.get_file(file_id, scope)

    def get_file(self, file_id: str, scope: KnowledgeBaseScope) -> dict[str, Any]:
        row = self._fetch_one(
            f"""
            select * from {self._table('knowledge_upload_file')}
            where id = %s and workspace_id = %s and knowledge_base_id = %s
            """,
            (file_id, scope.workspace_id, scope.knowledge_base_id),
        )
        if row is None:
            raise KeyError(file_id)
        return self._decode_file(row)

    def update_file(
        self,
        file_id: str,
        scope: KnowledgeBaseScope,
        *,
        status: str | None = None,
        document_id: str | None = None,
        chunks: int | None = None,
        error_message: str | None = None,
        phases: list[dict[str, Any]] | None = None,
        warnings: list[str] | None = None,
        errors: list[str] | None = None,
        retry_eligible: bool | None = None,
    ) -> dict[str, Any]:
        if status is not None and status not in FILE_STATUSES:
            raise ValueError("Unsupported upload file status")
        assignments = ["updated_at = %s"]
        values: list[Any] = [_now()]
        if status is not None:
            assignments.append("status = %s")
            values.append(status)
        if document_id is not None:
            assignments.append("document_id = %s")
            values.append(document_id)
        if chunks is not None:
            assignments.append("chunks = %s")
            values.append(int(chunks))
        if error_message is not None:
            assignments.append("error_message = %s")
            values.append(_sanitize_error(error_message))
        if phases is not None:
            assignments.append("phases_json = %s::jsonb")
            values.append(_jsonb_param(_normalize_phases(phases)))
        if warnings is not None:
            assignments.append("warnings_json = %s::jsonb")
            values.append(_jsonb_param([_sanitize_error(item) for item in warnings]))
        if errors is not None:
            assignments.append("errors_json = %s::jsonb")
            values.append(_jsonb_param([_sanitize_error(item) for item in errors]))
        if retry_eligible is not None:
            assignments.append("retry_eligible = %s")
            values.append(bool(retry_eligible))
        values.extend([file_id, scope.workspace_id, scope.knowledge_base_id])
        rowcount = self._execute(
            f"""
            update {self._table('knowledge_upload_file')}
            set {', '.join(assignments)}
            where id = %s and workspace_id = %s and knowledge_base_id = %s
            """,
            tuple(values),
        )
        if rowcount == 0:
            raise KeyError(file_id)
        return self.get_file(file_id, scope)

    def list_files(self, batch_id: str, scope: KnowledgeBaseScope) -> list[dict[str, Any]]:
        return list(self.get_batch(batch_id, scope)["files"])

    def cancel_batch(self, batch_id: str, scope: KnowledgeBaseScope) -> dict[str, Any]:
        now = _now()
        with self.database.transaction() as conn:
            rowcount = self._execute_in_conn(
                conn,
                f"""
                update {self._table('knowledge_upload_batch')}
                set status = 'canceled', updated_at = %s, completed_at = coalesce(completed_at, %s)
                where id = %s and workspace_id = %s and knowledge_base_id = %s
                """,
                (now, now, batch_id, scope.workspace_id, scope.knowledge_base_id),
            )
            if rowcount == 0:
                raise KeyError(batch_id)
            self._execute_in_conn(
                conn,
                f"""
                update {self._table('knowledge_upload_file')}
                set status = 'canceled', updated_at = %s
                where batch_id = %s and workspace_id = %s and knowledge_base_id = %s
                  and status in ('pending', 'uploaded')
                """,
                (now, batch_id, scope.workspace_id, scope.knowledge_base_id),
            )
        return self.get_batch(batch_id, scope)

    def _assert_active_knowledge_base(self, conn: Any, scope: KnowledgeBaseScope) -> None:
        row = self._fetch_one_in_conn(
            conn,
            f"""
            select 1 from {self._table('knowledge_base')}
            where id = %s and workspace_id = %s and status = 'active'
            """,
            (scope.knowledge_base_id, scope.workspace_id),
        )
        if row is None:
            raise ValueError("Knowledge base does not exist, is archived, or belongs to another workspace")

    def _decode_batch(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["settings"] = _load_json(data.pop("settings_json", None), {})
        _normalize_timestamp_fields(data, ("created_at", "updated_at", "confirmed_at", "completed_at"))
        return data

    def _decode_file(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["phases"] = _normalize_phases(_load_json(data.pop("phases_json", None), []))
        data["warnings"] = [_sanitize_error(item) for item in _load_json(data.pop("warnings_json", None), [])]
        data["errors"] = [_sanitize_error(item) for item in _load_json(data.pop("errors_json", None), [])]
        data["retry_eligible"] = bool(data.get("retry_eligible", False))
        _normalize_timestamp_fields(data, ("created_at", "updated_at"))
        return data

    def _fetch_one(self, sql: str, params: tuple[Any, ...]) -> Any | None:
        with self.database.connection() as conn:
            return self._fetch_one_in_conn(conn, sql, params)

    def _fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())

    def _execute(self, sql: str, params: tuple[Any, ...]) -> int:
        with self.database.transaction() as conn:
            return self._execute_in_conn(conn, sql, params)

    def _fetch_one_in_conn(self, conn: Any, sql: str, params: tuple[Any, ...]) -> Any | None:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

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


def _normalize_timestamp_fields(data: dict[str, Any], fields: tuple[str, ...]) -> None:
    for field in fields:
        value = data.get(field)
        if isinstance(value, datetime):
            data[field] = value.isoformat(timespec="seconds")


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
