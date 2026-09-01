from __future__ import annotations

import json
from typing import Any

from app.services.processing.processing_span_tracker import (
    SPAN_STAGE,
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_RUNNING,
    _dependent_stages,
    _json,
    _utc_now,
)
from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import (
    PostgresSchemaConfig,
    inspect_postgres_startup_storage,
    qname,
)
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings


class PostgresProcessingSpanRepository:
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

    def next_attempt(self, knowledge_id: str) -> int:
        row = self._fetch_one(
            f"select coalesce(max(attempt), 0) + 1 as attempt from {self._table()} where knowledge_id = %s",
            (knowledge_id,),
        )
        return int(_row_get(row, "attempt", 1) or 1)

    def latest_attempt(self, knowledge_id: str) -> int:
        row = self._fetch_one(
            f"select coalesce(max(attempt), 0) as attempt from {self._table()} where knowledge_id = %s",
            (knowledge_id,),
        )
        return int(_row_get(row, "attempt", 0) or 0)

    def insert_span(
        self,
        *,
        knowledge_id: str,
        attempt: int,
        span_id: str,
        parent_span_id: str | None,
        name: str,
        kind: str,
        status: str,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_ms: int = 0,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self.database.transaction() as conn:
            self._execute_in_conn(
                conn,
                f"""
                insert into {self._table()}
                (knowledge_id, attempt, span_id, parent_span_id, name, kind, status,
                 input_json, output_json, metadata_json, started_at, finished_at, duration_ms, created_at, updated_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s)
                on conflict(knowledge_id, attempt, parent_span_id, name, kind) do update set
                    status = excluded.status,
                    input_json = excluded.input_json,
                    output_json = excluded.output_json,
                    metadata_json = excluded.metadata_json,
                    error_code = '',
                    error_message = '',
                    error_detail = '',
                    started_at = coalesce(excluded.started_at, {self._table()}.started_at),
                    finished_at = excluded.finished_at,
                    duration_ms = excluded.duration_ms,
                    updated_at = excluded.updated_at
                """,
                (
                    knowledge_id,
                    attempt,
                    span_id,
                    parent_span_id,
                    name,
                    kind,
                    status,
                    _json(input or {}),
                    _json(output or {}),
                    _json(metadata or {}),
                    _timestamp_param(started_at),
                    _timestamp_param(finished_at),
                    int(duration_ms or 0),
                    now,
                    now,
                ),
            )
            row = self._fetch_one_in_conn(
                conn,
                f"""
                select * from {self._table()}
                where knowledge_id = %s and attempt = %s and parent_span_id is not distinct from %s and name = %s and kind = %s
                """,
                (knowledge_id, attempt, parent_span_id, name, kind),
            )
        return _decode_span_row(row)

    def update_span(
        self,
        span_id: str,
        *,
        status: str | None = None,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        error_code: str = "",
        error_message: str = "",
        error_detail: str = "",
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        assignments = ["updated_at = %s"]
        params: list[Any] = [_utc_now()]
        if status is not None:
            assignments.append("status = %s")
            params.append(status)
        if input is not None:
            assignments.append("input_json = %s::jsonb")
            params.append(_json(input))
        if output is not None:
            assignments.append("output_json = %s::jsonb")
            params.append(_json(output))
        if metadata is not None:
            assignments.append("metadata_json = %s::jsonb")
            params.append(_json(metadata))
        if error_code:
            assignments.append("error_code = %s")
            params.append(error_code)
        if error_message:
            assignments.append("error_message = %s")
            params.append(error_message)
        if error_detail:
            assignments.append("error_detail = %s")
            params.append(error_detail[:8192])
        if started_at is not None:
            assignments.append("started_at = coalesce(started_at, %s)")
            params.append(_timestamp_param(started_at))
        if finished_at is not None:
            assignments.append("finished_at = %s")
            params.append(_timestamp_param(finished_at))
        if duration_ms is not None:
            assignments.append("duration_ms = %s")
            params.append(max(0, int(duration_ms)))
        params.append(span_id)
        self._execute(f"update {self._table()} set {', '.join(assignments)} where span_id = %s", tuple(params))

    def get_stage(self, knowledge_id: str, attempt: int, name: str) -> dict[str, Any] | None:
        row = self._fetch_one(
            f"""
            select * from {self._table()}
            where knowledge_id = %s and attempt = %s and name = %s and kind = %s
            order by id desc limit 1
            """,
            (knowledge_id, attempt, name, SPAN_STAGE),
        )
        return _decode_span_row(row) if row else None

    def get_span_by_name(
        self,
        knowledge_id: str,
        attempt: int,
        name: str,
        *,
        parent_span_id: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["knowledge_id = %s", "attempt = %s", "name = %s"]
        params: list[Any] = [knowledge_id, attempt, name]
        if parent_span_id is None:
            clauses.append("parent_span_id is null")
        else:
            clauses.append("parent_span_id = %s")
            params.append(parent_span_id)
        if kind is not None:
            clauses.append("kind = %s")
            params.append(kind)
        row = self._fetch_one(
            f"select * from {self._table()} where {' and '.join(clauses)} order by id desc limit 1",
            tuple(params),
        )
        return _decode_span_row(row) if row else None

    def supersede_open_span_by_name(
        self,
        knowledge_id: str,
        attempt: int,
        name: str,
        *,
        parent_span_id: str | None,
        kind: str,
        reason: str = "retry re-entry",
    ) -> None:
        now = _utc_now()
        self._execute(
            f"""
            update {self._table()}
            set status = %s, error_code = 'SUPERSEDED', error_message = %s,
                finished_at = coalesce(finished_at, %s), updated_at = %s
            where knowledge_id = %s and attempt = %s and parent_span_id is not distinct from %s and name = %s and kind = %s
              and status in (%s, %s)
            """,
            (STATUS_CANCELLED, reason, now, now, knowledge_id, attempt, parent_span_id, name, kind, STATUS_PENDING, STATUS_RUNNING),
        )

    def heartbeat_span(self, span_id: str) -> None:
        self._execute(f"update {self._table()} set updated_at = %s where span_id = %s", (_utc_now(), span_id))

    def list_attempt(self, knowledge_id: str, attempt: int | None = None) -> tuple[int, list[dict[str, Any]]]:
        if attempt is None:
            row = self._fetch_one(f"select max(attempt) as attempt from {self._table()} where knowledge_id = %s", (knowledge_id,))
            attempt = int(_row_get(row, "attempt", 0) or 0)
        if attempt <= 0:
            return 0, []
        rows = self._fetch_all(
            f"""
            select * from {self._table()}
            where knowledge_id = %s and attempt = %s
            order by case kind when 'root' then 0 when 'stage' then 1 when 'subspan' then 2 else 3 end, id
            """,
            (knowledge_id, attempt),
        )
        return attempt, [_decode_span_row(row) for row in rows]

    def reassign_knowledge_id(self, old_knowledge_id: str, new_knowledge_id: str, attempt: int) -> None:
        if old_knowledge_id == new_knowledge_id:
            return
        self._execute(
            f"update {self._table()} set knowledge_id = %s, updated_at = %s where knowledge_id = %s and attempt = %s",
            (new_knowledge_id, _utc_now(), old_knowledge_id, attempt),
        )

    def cancel_dependents(self, knowledge_id: str, attempt: int, failed_stage: str, reason: str) -> None:
        dependents = _dependent_stages(failed_stage)
        if not dependents:
            return
        now = _utc_now()
        self._execute(
            f"""
            update {self._table()}
            set status = %s, error_message = %s, finished_at = coalesce(finished_at, %s), updated_at = %s
            where knowledge_id = %s and attempt = %s and kind = %s and name in ({_placeholders(dependents)})
              and status in (%s, %s)
            """,
            (STATUS_CANCELLED, reason, now, now, knowledge_id, attempt, SPAN_STAGE, *dependents, STATUS_PENDING, STATUS_RUNNING),
        )

    def cancel_descendants(self, knowledge_id: str, attempt: int, parent_span_id: str, reason: str) -> int:
        descendants = self._descendants(knowledge_id, attempt, parent_span_id)
        if not descendants:
            return 0
        now = _utc_now()
        return self._execute(
            f"""
            update {self._table()}
            set status = %s, error_code = 'CANCELLED', error_message = %s,
                finished_at = coalesce(finished_at, %s), updated_at = %s
            where knowledge_id = %s and attempt = %s and span_id in ({_placeholders(descendants)})
              and status in (%s, %s)
            """,
            (STATUS_CANCELLED, reason, now, now, knowledge_id, attempt, *descendants, STATUS_PENDING, STATUS_RUNNING),
        )

    def cancel_all_open_spans(self, knowledge_id: str, attempt: int, reason: str) -> int:
        now = _utc_now()
        return self._execute(
            f"""
            update {self._table()}
            set status = %s, error_code = 'CANCELLED', error_message = %s,
                finished_at = coalesce(finished_at, %s), updated_at = %s
            where knowledge_id = %s and attempt = %s and status in (%s, %s)
            """,
            (STATUS_CANCELLED, reason, now, now, knowledge_id, attempt, STATUS_PENDING, STATUS_RUNNING),
        )

    def _descendants(self, knowledge_id: str, attempt: int, parent_span_id: str) -> list[str]:
        rows = self._fetch_all(
            f"""
            with recursive descendants(span_id) as (
                select span_id from {self._table()} where knowledge_id = %s and attempt = %s and parent_span_id = %s
                union all
                select child.span_id
                from {self._table()} child
                join descendants d on child.parent_span_id = d.span_id
                where child.knowledge_id = %s and child.attempt = %s
            )
            select span_id from descendants
            """,
            (knowledge_id, attempt, parent_span_id, knowledge_id, attempt),
        )
        return [str(_row_get(row, "span_id")) for row in rows]

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

    def _table(self) -> str:
        return qname(self.schema, "knowledge_processing_spans")


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


def _decode_span_row(row: Any | None) -> dict[str, Any]:
    if row is None:
        return {}
    data = dict(row)
    for key in ("input_json", "output_json", "metadata_json"):
        data[key] = _load_json(data.get(key), {})
    _normalize_timestamp_fields(data, ("started_at", "finished_at", "created_at", "updated_at"))
    return data


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


def _timestamp_param(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _normalize_timestamp_fields(data: dict[str, Any], fields: tuple[str, ...]) -> None:
    for field in fields:
        value = data.get(field)
        if hasattr(value, "isoformat"):
            data[field] = value.isoformat(timespec="seconds")


def _placeholders(items: Any) -> str:
    count = len(tuple(items))
    if count <= 0:
        raise ValueError("At least one value is required for SQL placeholders")
    return ", ".join("%s" for _ in range(count))
