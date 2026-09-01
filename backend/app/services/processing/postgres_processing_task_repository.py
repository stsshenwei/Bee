from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from app.models.knowledge_base import KnowledgeBaseScope
from app.services.processing.processing_task_repository import (
    RUNNABLE_STATUSES,
    TASK_CANCELED,
    TASK_COMPLETED,
    TASK_DEAD_LETTERED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_PROCESSING,
    TASK_RETRYING,
    TERMINAL_STATUSES,
    _optional_text,
    _required_text,
    _sanitize_error,
)
from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import (
    PostgresSchemaConfig,
    inspect_postgres_startup_storage,
    qname,
)
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings


class PostgresProcessingTaskRepository:
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

    def create_task(
        self,
        task_type: str,
        scope: KnowledgeBaseScope,
        *,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        document_id: str = "",
        upload_batch_id: str = "",
        upload_file_id: str = "",
        max_attempts: int = 3,
        run_after: datetime | str | None = None,
        trace_id: str = "",
        payload_schema_version: int = 1,
        idempotency_key: str = "",
        source_revision: str = "",
        parent_trace_id: str = "",
    ) -> dict[str, Any]:
        task_type = _required_text(task_type, "task_type")
        payload = payload or {}
        task_id = task_id or deterministic_task_id(
            task_type,
            scope,
            document_id=document_id,
            upload_batch_id=upload_batch_id,
            upload_file_id=upload_file_id,
            payload=payload,
        )
        now = _now()
        next_run_at = _as_timestamp(run_after) if run_after else now
        with self.database.transaction() as conn:
            existing = None
            clean_idempotency_key = _optional_text(idempotency_key)
            if clean_idempotency_key:
                existing = self._fetch_one_in_conn(
                    conn,
                    f"""
                    select id from {self._table('document_processing_task')}
                    where workspace_id = %s and knowledge_base_id = %s and task_type = %s and idempotency_key = %s
                    order by created_at, id limit 1
                    """,
                    (scope.workspace_id, scope.knowledge_base_id, task_type, clean_idempotency_key),
                )
            if existing is not None:
                task_id = str(_row_get(existing, "id"))
            else:
                self._execute_in_conn(
                    conn,
                    f"""
                    insert into {self._table('document_processing_task')}(
                        id, task_type, workspace_id, knowledge_base_id, document_id, upload_batch_id,
                        upload_file_id, status, payload_json, attempt, max_attempts, next_run_at,
                        lease_owner, lease_expires_at, last_error_code, last_error_message,
                        trace_id, created_at, updated_at, started_at, finished_at,
                        payload_schema_version, idempotency_key, source_revision, parent_trace_id
                    ) values (
                        %s::text, %s::text, %s::text, %s::text, %s::text, %s::text, %s::text,
                        'pending', %s::jsonb, 0, %s::integer, %s::timestamptz,
                        ''::text, null, ''::text, ''::text,
                        %s::text, %s::timestamptz, %s::timestamptz, null, null,
                        %s::integer, %s::text, %s::text, %s::text
                    )
                    on conflict do nothing
                    """,
                    (
                        task_id,
                        task_type,
                        scope.workspace_id,
                        scope.knowledge_base_id,
                        _optional_text(document_id),
                        _optional_text(upload_batch_id),
                        _optional_text(upload_file_id),
                        _jsonb_param(payload),
                        max(1, int(max_attempts)),
                        next_run_at,
                        _optional_text(trace_id),
                        now,
                        now,
                        max(1, int(payload_schema_version or 1)),
                        clean_idempotency_key,
                        _optional_text(source_revision),
                        _optional_text(parent_trace_id),
                    ),
                )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        row = self._fetch_one(f"select * from {self._table('document_processing_task')} where id = %s", (task_id,))
        if row is None:
            raise KeyError(task_id)
        return _decode_task(row)

    def list_tasks(
        self,
        scope: KnowledgeBaseScope | None = None,
        *,
        statuses: set[str] | None = None,
        document_id: str | None = None,
        upload_batch_id: str | None = None,
        task_types: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope is not None:
            clauses.extend(["workspace_id = %s", "knowledge_base_id = %s"])
            params.extend([scope.workspace_id, scope.knowledge_base_id])
        if statuses:
            clauses.append(f"status in ({_placeholders(statuses)})")
            params.extend(sorted(statuses))
        if document_id is not None:
            clauses.append("document_id = %s")
            params.append(document_id)
        if upload_batch_id is not None:
            clauses.append("upload_batch_id = %s")
            params.append(upload_batch_id)
        if task_types:
            clauses.append(f"task_type in ({_placeholders(task_types)})")
            params.extend(sorted(task_types))
        where = f"where {' and '.join(clauses)}" if clauses else ""
        rows = self._fetch_all(
            f"select * from {self._table('document_processing_task')} {where} order by created_at, id",
            tuple(params),
        )
        return [_decode_task(row) for row in rows]

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        task_types: set[str] | None = None,
        now: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        worker_id = _required_text(worker_id, "worker_id")
        current = _as_timestamp(now) if now else _now()
        clauses = [
            "((status in ('pending', 'retrying') and next_run_at <= %s) "
            "or (status = 'processing' and lease_expires_at is not null and lease_expires_at <= %s))"
        ]
        params: list[Any] = [current, current]
        if task_types:
            clauses.append(f"task_type in ({_placeholders(task_types)})")
            params.extend(sorted(task_types))
        with self.database.transaction() as conn:
            row = self._fetch_one_in_conn(
                conn,
                f"""
                select * from {self._table('document_processing_task')}
                where {' and '.join(clauses)}
                order by next_run_at, created_at, id
                limit 1
                for update skip locked
                """,
                tuple(params),
            )
            if row is None:
                return None
            lease_expires_at = _add_seconds(current, lease_seconds)
            task_id = str(_row_get(row, "id"))
            self._execute_in_conn(
                conn,
                f"""
                update {self._table('document_processing_task')}
                set status = 'processing',
                    attempt = attempt + 1,
                    lease_owner = %s,
                    lease_expires_at = %s,
                    started_at = coalesce(started_at, %s),
                    updated_at = %s
                where id = %s
                """,
                (worker_id, lease_expires_at, current, current, task_id),
            )
            claimed = self._fetch_one_in_conn(
                conn,
                f"select * from {self._table('document_processing_task')} where id = %s",
                (task_id,),
            )
            if claimed is not None:
                self._execute_in_conn(
                    conn,
                    f"""
                    insert into {self._table('document_processing_task_attempt')}(
                        id, task_id, attempt, worker_id, status, started_at, created_at
                    ) values (%s, %s, %s, %s, 'processing', %s, %s)
                    """,
                    (
                        f"attempt-{uuid4().hex}",
                        task_id,
                        int(_row_get(claimed, "attempt", 0) or 0),
                        worker_id,
                        current,
                        current,
                    ),
                )
        return _decode_task(claimed) if claimed else None

    def claim_task(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        now: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        worker_id = _required_text(worker_id, "worker_id")
        clean_task_id = _required_text(task_id, "task_id")
        current = _as_timestamp(now) if now else _now()
        with self.database.transaction() as conn:
            row = self._fetch_one_in_conn(
                conn,
                f"""
                select * from {self._table('document_processing_task')}
                where id = %s
                  and (
                    status in ('pending', 'retrying')
                    or (status = 'processing' and lease_expires_at is not null and lease_expires_at <= %s)
                  )
                for update skip locked
                """,
                (clean_task_id, current),
            )
            if row is None:
                return None
            lease_expires_at = _add_seconds(current, lease_seconds)
            self._execute_in_conn(
                conn,
                f"""
                update {self._table('document_processing_task')}
                set status = 'processing',
                    attempt = attempt + 1,
                    lease_owner = %s,
                    lease_expires_at = %s,
                    started_at = coalesce(started_at, %s),
                    updated_at = %s
                where id = %s
                """,
                (worker_id, lease_expires_at, current, current, clean_task_id),
            )
            claimed = self._fetch_one_in_conn(
                conn,
                f"select * from {self._table('document_processing_task')} where id = %s",
                (clean_task_id,),
            )
            if claimed is not None:
                self._execute_in_conn(
                    conn,
                    f"""
                    insert into {self._table('document_processing_task_attempt')}(
                        id, task_id, attempt, worker_id, status, started_at, created_at
                    ) values (%s, %s, %s, %s, 'processing', %s, %s)
                    """,
                    (
                        f"attempt-{uuid4().hex}",
                        clean_task_id,
                        int(_row_get(claimed, "attempt", 0) or 0),
                        worker_id,
                        current,
                        current,
                    ),
                )
        return _decode_task(claimed) if claimed else None

    def heartbeat(self, task_id: str, worker_id: str, *, lease_seconds: int = 60) -> dict[str, Any]:
        now = _now()
        rowcount = self._execute(
            f"""
            update {self._table('document_processing_task')}
            set lease_expires_at = %s, updated_at = %s
            where id = %s and status = 'processing' and lease_owner = %s
            """,
            (_add_seconds(now, lease_seconds), now, task_id, worker_id),
        )
        if rowcount == 0:
            raise KeyError(task_id)
        return self.get_task(task_id)

    def complete(self, task_id: str, worker_id: str | None = None) -> dict[str, Any]:
        return self._finish(task_id, TASK_COMPLETED, worker_id=worker_id)

    def fail(self, task_id: str, *, error_code: str = "", error_message: str = "", worker_id: str | None = None) -> dict[str, Any]:
        return self._finish(task_id, TASK_FAILED, error_code=error_code, error_message=error_message, worker_id=worker_id)

    def cancel_task(self, task_id: str, *, reason: str = "cancelled") -> dict[str, Any]:
        return self._finish(task_id, TASK_CANCELED, error_code="CANCELED", error_message=reason)

    def cancel_for_document(self, scope: KnowledgeBaseScope, document_id: str, *, reason: str = "cancelled") -> int:
        return self._cancel_where(scope, "document_id = %s", [document_id], reason)

    def cancel_for_upload_file(self, scope: KnowledgeBaseScope, upload_file_id: str, *, reason: str = "cancelled") -> int:
        return self._cancel_where(scope, "upload_file_id = %s", [upload_file_id], reason)

    def cancel_for_upload_batch(self, scope: KnowledgeBaseScope, upload_batch_id: str, *, reason: str = "cancelled") -> int:
        return self._cancel_where(scope, "upload_batch_id = %s", [upload_batch_id], reason)

    def retry(
        self,
        task_id: str,
        *,
        error_code: str = "",
        error_message: str = "",
        delay_seconds: int = 30,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self.database.transaction() as conn:
            row = self._fetch_one_in_conn(
                conn,
                f"select * from {self._table('document_processing_task')} where id = %s for update",
                (task_id,),
            )
            if row is None:
                raise KeyError(task_id)
            if str(_row_get(row, "status")) in TERMINAL_STATUSES:
                return _decode_task(row)
            if worker_id is not None and _row_get(row, "lease_owner") and _row_get(row, "lease_owner") != worker_id:
                raise KeyError(task_id)
            self._execute_in_conn(
                conn,
                f"""
                update {self._table('document_processing_task')}
                set status = 'retrying',
                    next_run_at = %s,
                    lease_owner = '',
                    lease_expires_at = null,
                    last_error_code = %s,
                    last_error_message = %s,
                    updated_at = %s
                where id = %s
                """,
                (_add_seconds(now, delay_seconds), _optional_text(error_code), _sanitize_error(error_message), now, task_id),
            )
            self._finish_attempt(conn, task_id, TASK_RETRYING, error_code, error_message, now)
        return self.get_task(task_id)

    def dead_letter(
        self,
        task_id: str,
        *,
        error_code: str = "",
        error_message: str = "",
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self.database.transaction() as conn:
            row = self._fetch_one_in_conn(
                conn,
                f"select * from {self._table('document_processing_task')} where id = %s for update",
                (task_id,),
            )
            if row is None:
                raise KeyError(task_id)
            if worker_id is not None and _row_get(row, "lease_owner") and _row_get(row, "lease_owner") != worker_id:
                raise KeyError(task_id)
            self._execute_in_conn(
                conn,
                f"""
                insert into {self._table('document_processing_dead_letter')}(
                    id, task_id, task_type, workspace_id, knowledge_base_id, document_id,
                    upload_batch_id, upload_file_id, payload_json, error_code, error_message,
                    attempt, trace_id, created_at
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                """,
                (
                    f"dead-{uuid4().hex}",
                    _row_get(row, "id"),
                    _row_get(row, "task_type"),
                    _row_get(row, "workspace_id"),
                    _row_get(row, "knowledge_base_id"),
                    _row_get(row, "document_id"),
                    _row_get(row, "upload_batch_id"),
                    _row_get(row, "upload_file_id"),
                    _jsonb_param(_load_json(_row_get(row, "payload_json"), {})),
                    _optional_text(error_code),
                    _sanitize_error(error_message),
                    int(_row_get(row, "attempt", 0) or 0),
                    _row_get(row, "trace_id"),
                    now,
                ),
            )
            self._execute_in_conn(
                conn,
                f"""
                update {self._table('document_processing_task')}
                set status = 'dead_lettered',
                    lease_owner = '',
                    lease_expires_at = null,
                    last_error_code = %s,
                    last_error_message = %s,
                    finished_at = coalesce(finished_at, %s),
                    updated_at = %s
                where id = %s
                """,
                (_optional_text(error_code), _sanitize_error(error_message), now, now, task_id),
            )
            self._finish_attempt(conn, task_id, TASK_DEAD_LETTERED, error_code, error_message, now)
        return self.get_task(task_id)

    def list_dead_letters(self, scope: KnowledgeBaseScope | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope is not None:
            clauses.extend(["workspace_id = %s", "knowledge_base_id = %s"])
            params.extend([scope.workspace_id, scope.knowledge_base_id])
        where = f"where {' and '.join(clauses)}" if clauses else ""
        rows = self._fetch_all(
            f"select * from {self._table('document_processing_dead_letter')} {where} order by created_at, id",
            tuple(params),
        )
        return [_decode_dead_letter(row) for row in rows]

    def retry_dead_letter(self, task_id: str, *, delay_seconds: int = 0) -> dict[str, Any]:
        now = _now()
        with self.database.transaction() as conn:
            row = self._fetch_one_in_conn(
                conn,
                f"select * from {self._table('document_processing_task')} where id = %s for update",
                (task_id,),
            )
            if row is None or str(_row_get(row, "status")) != TASK_DEAD_LETTERED:
                raise KeyError(task_id)
            self._execute_in_conn(
                conn,
                f"""
                update {self._table('document_processing_task')}
                set status = 'retrying', attempt = 0, next_run_at = %s, lease_owner = '',
                    lease_expires_at = null, last_error_code = '', last_error_message = '',
                    started_at = null, finished_at = null, updated_at = %s
                where id = %s and status = 'dead_lettered'
                """,
                (_add_seconds(now, delay_seconds), now, task_id),
            )
        return self.get_task(task_id)

    def record_broker_dispatch(self, task_id: str, *, broker_task_id: str = "", queue_name: str = "") -> dict[str, Any]:
        now = _now()
        with self.database.transaction() as conn:
            row = self._fetch_one_in_conn(
                conn,
                f"select * from {self._table('document_processing_task')} where id = %s for update",
                (task_id,),
            )
            if row is None:
                raise KeyError(task_id)
            payload = _load_json(_row_get(row, "payload_json"), {})
            if not isinstance(payload, dict):
                payload = {}
            payload["_async_runtime"] = {
                "broker_task_id": _optional_text(broker_task_id),
                "queue_name": _optional_text(queue_name),
                "dispatched_at": now,
            }
            self._execute_in_conn(
                conn,
                f"update {self._table('document_processing_task')} set payload_json = %s::jsonb, updated_at = %s where id = %s",
                (_jsonb_param(payload), now, task_id),
            )
        return self.get_task(task_id)

    def find_by_idempotency(
        self,
        scope: KnowledgeBaseScope,
        task_type: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        clean_key = _optional_text(idempotency_key)
        if not clean_key:
            return None
        row = self._fetch_one(
            f"""
            select * from {self._table('document_processing_task')}
            where workspace_id = %s and knowledge_base_id = %s and task_type = %s and idempotency_key = %s
            order by created_at, id limit 1
            """,
            (scope.workspace_id, scope.knowledge_base_id, _required_text(task_type, "task_type"), clean_key),
        )
        return _decode_task(row) if row is not None else None

    def merge_runnable_task_payload(
        self,
        scope: KnowledgeBaseScope,
        task_type: str,
        payload: dict[str, Any],
        *,
        run_after: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        now = _now()
        with self.database.transaction() as conn:
            row = self._fetch_one_in_conn(
                conn,
                f"""
                select * from {self._table('document_processing_task')}
                where workspace_id = %s and knowledge_base_id = %s and task_type = %s
                  and status in ('pending', 'retrying')
                order by created_at, id limit 1
                for update skip locked
                """,
                (scope.workspace_id, scope.knowledge_base_id, _required_text(task_type, "task_type")),
            )
            if row is None:
                return None
            merged = _load_json(_row_get(row, "payload_json"), {})
            for key in {"generation_run_ids", "generation_task_ids", "document_ids", "affected_slugs"}:
                values = [*list(merged.get(key) or []), *list(payload.get(key) or [])]
                if key == "generation_run_ids" and payload.get("generation_run_id"):
                    values.append(payload["generation_run_id"])
                merged[key] = list(dict.fromkeys(str(value) for value in values if str(value)))
            task_id = str(_row_get(row, "id"))
            self._execute_in_conn(
                conn,
                f"update {self._table('document_processing_task')} set payload_json = %s::jsonb, next_run_at = %s, updated_at = %s where id = %s",
                (_jsonb_param(merged), _as_timestamp(run_after) if run_after else now, now, task_id),
            )
            updated = self._fetch_one_in_conn(
                conn,
                f"select * from {self._table('document_processing_task')} where id = %s",
                (task_id,),
            )
        return _decode_task(updated) if updated is not None else None

    def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._fetch_all(
            f"select * from {self._table('document_processing_task_attempt')} where task_id = %s order by id",
            (task_id,),
        )
        return [_normalize_timestamps(dict(row)) for row in rows]

    def _finish(
        self,
        task_id: str,
        status: str,
        *,
        error_code: str = "",
        error_message: str = "",
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        clauses = ["id = %s"]
        params: list[Any] = [status, _optional_text(error_code), _sanitize_error(error_message), now, now, task_id]
        if worker_id is not None:
            clauses.append("(lease_owner = %s or lease_owner = '')")
            params.append(worker_id)
        with self.database.transaction() as conn:
            rowcount = self._execute_in_conn(
                conn,
                f"""
                update {self._table('document_processing_task')}
                set status = %s,
                    lease_owner = '',
                    lease_expires_at = null,
                    last_error_code = %s,
                    last_error_message = %s,
                    finished_at = coalesce(finished_at, %s),
                    updated_at = %s
                where {' and '.join(clauses)}
                """,
                tuple(params),
            )
            if rowcount == 0:
                raise KeyError(task_id)
            self._finish_attempt(conn, task_id, status, error_code, error_message, now)
        return self.get_task(task_id)

    def _finish_attempt(
        self,
        conn: Any,
        task_id: str,
        status: str,
        error_code: str,
        error_message: str,
        finished_at: str,
    ) -> None:
        self._execute_in_conn(
            conn,
            f"""
            update {self._table('document_processing_task_attempt')}
            set status = %s, error_code = %s, error_message = %s, finished_at = %s
            where id = (
                select id from {self._table('document_processing_task_attempt')}
                where task_id = %s and finished_at is null order by created_at desc, id desc limit 1
            )
            """,
            (status, _optional_text(error_code), _sanitize_error(error_message), finished_at, task_id),
        )

    def _cancel_where(self, scope: KnowledgeBaseScope, condition: str, condition_params: list[Any], reason: str) -> int:
        now = _now()
        return self._execute(
            f"""
            update {self._table('document_processing_task')}
            set status = 'canceled',
                lease_owner = '',
                lease_expires_at = null,
                last_error_code = 'CANCELED',
                last_error_message = %s,
                finished_at = coalesce(finished_at, %s),
                updated_at = %s
            where workspace_id = %s and knowledge_base_id = %s
              and status not in ('completed', 'failed', 'canceled', 'dead_lettered')
              and {condition}
            """,
            (_sanitize_error(reason), now, now, scope.workspace_id, scope.knowledge_base_id, *condition_params),
        )

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


def deterministic_task_id(
    task_type: str,
    scope: KnowledgeBaseScope,
    *,
    document_id: str = "",
    upload_batch_id: str = "",
    upload_file_id: str = "",
    payload: dict[str, Any] | None = None,
) -> str:
    canonical = json.dumps(
        {
            "task_type": task_type,
            "workspace_id": scope.workspace_id,
            "knowledge_base_id": scope.knowledge_base_id,
            "document_id": document_id,
            "upload_batch_id": upload_batch_id,
            "upload_file_id": upload_file_id,
            "payload": payload or {},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "processing-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _decode_task(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["payload"] = _load_json(data.pop("payload_json", None), {})
    return _normalize_timestamps(data)


def _decode_dead_letter(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["payload"] = _load_json(data.pop("payload_json", None), {})
    return _normalize_timestamps(data)


def _normalize_timestamps(data: dict[str, Any]) -> dict[str, Any]:
    for key, value in list(data.items()):
        if isinstance(value, datetime):
            data[key] = value.isoformat(timespec="seconds")
    return data


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _as_timestamp(value: datetime | str | None) -> str:
    if value is None:
        return _now()
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _add_seconds(timestamp: str, seconds: int) -> str:
    base = datetime.fromisoformat(timestamp)
    return (base + timedelta(seconds=max(0, int(seconds)))).isoformat(timespec="seconds")


def _jsonb_param(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


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
