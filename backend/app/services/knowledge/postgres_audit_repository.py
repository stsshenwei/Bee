from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from app.models.knowledge_base import KnowledgeBaseScope
from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import PostgresSchemaConfig, inspect_postgres_startup_storage, qname
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings


class PostgresKnowledgeAuditRepository:
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

    def start_query(self, question: str, scope: KnowledgeBaseScope, query_type: str = "") -> str:
        query_id = f"query-{uuid.uuid4().hex}"
        self._execute(
            f"""
            insert into {self._table('query_log')}(
                id, workspace_id, knowledge_base_ids_json, question, status, query_type,
                tool_calls_json, citation_chunk_ids_json, response_metadata_json,
                error_message, created_at, finished_at
            ) values (%s, %s, %s::jsonb, %s, 'running', %s, '[]'::jsonb, '[]'::jsonb, '{{}}'::jsonb, '', %s, null)
            """,
            (query_id, scope.workspace_id, _dump(list(scope.selected_knowledge_base_ids)), question, query_type, _now()),
        )
        return query_id

    def finish_query(
        self,
        query_id: str,
        *,
        status: str,
        tool_calls: list[dict[str, Any]] | None = None,
        citation_chunk_ids: list[str] | None = None,
        response_metadata: dict[str, Any] | None = None,
        error_message: str = "",
    ) -> None:
        rowcount = self._execute(
            f"""
            update {self._table('query_log')}
            set status = %s, tool_calls_json = %s::jsonb, citation_chunk_ids_json = %s::jsonb,
                response_metadata_json = %s::jsonb, error_message = %s, finished_at = %s
            where id = %s
            """,
            (
                status,
                _dump(tool_calls or []),
                _dump(list(dict.fromkeys(citation_chunk_ids or []))),
                _dump(response_metadata or {}),
                error_message[:1000],
                _now(),
                query_id,
            ),
        )
        if rowcount == 0:
            raise KeyError(query_id)

    def create_feedback(
        self,
        scope: KnowledgeBaseScope,
        *,
        correction: str,
        query_log_id: str | None = None,
        rating: str = "correction",
        source_chunk_ids: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        feedback_id = f"feedback-{uuid.uuid4().hex}"
        with self.database.transaction() as conn:
            if query_log_id:
                query = self._fetch_one_in_conn(
                    conn,
                    f"select workspace_id, knowledge_base_ids_json from {self._table('query_log')} where id = %s",
                    (query_log_id,),
                )
                if query is None:
                    raise KeyError(query_log_id)
                query_kbs = set(_load_json(_row_get(query, "knowledge_base_ids_json"), []))
                if str(_row_get(query, "workspace_id")) != scope.workspace_id or scope.knowledge_base_id not in query_kbs:
                    raise ValueError("Feedback target is outside the query knowledge base scope")
            self._execute_in_conn(
                conn,
                f"""
                insert into {self._table('answer_feedback')}(
                    id, query_log_id, workspace_id, knowledge_base_id, rating, correction,
                    source_chunk_ids_json, metadata_json, created_at
                ) values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                """,
                (
                    feedback_id,
                    query_log_id,
                    scope.workspace_id,
                    scope.knowledge_base_id,
                    rating,
                    correction,
                    _dump(list(dict.fromkeys(source_chunk_ids or []))),
                    _dump(metadata or {}),
                    _now(),
                ),
            )
        return self.get_feedback(feedback_id, scope)

    def get_query(self, query_id: str, scope: KnowledgeBaseScope) -> dict[str, Any] | None:
        row = self._fetch_one(
            f"select * from {self._table('query_log')} where id = %s and workspace_id = %s",
            (query_id, scope.workspace_id),
        )
        if row is None:
            return None
        data = _decode_query(row)
        if not set(data["knowledge_base_ids"]).issubset(set(scope.selected_knowledge_base_ids)):
            return None
        return data

    def list_queries(self, scope: KnowledgeBaseScope) -> list[dict[str, Any]]:
        rows = self._fetch_all(
            f"select * from {self._table('query_log')} where workspace_id = %s order by created_at, id",
            (scope.workspace_id,),
        )
        allowed = set(scope.selected_knowledge_base_ids)
        return [data for row in rows if set((data := _decode_query(row))["knowledge_base_ids"]).issubset(allowed)]

    def get_feedback(self, feedback_id: str, scope: KnowledgeBaseScope) -> dict[str, Any]:
        row = self._fetch_one(
            f"""
            select * from {self._table('answer_feedback')}
            where id = %s and workspace_id = %s and knowledge_base_id = %s
            """,
            (feedback_id, scope.workspace_id, scope.knowledge_base_id),
        )
        if row is None:
            raise KeyError(feedback_id)
        return _decode_feedback(row)

    def list_feedback(self, scope: KnowledgeBaseScope) -> list[dict[str, Any]]:
        placeholders = _placeholders(scope.selected_knowledge_base_ids)
        rows = self._fetch_all(
            f"""
            select * from {self._table('answer_feedback')}
            where workspace_id = %s and knowledge_base_id in ({placeholders})
            order by created_at
            """,
            (scope.workspace_id, *scope.selected_knowledge_base_ids),
        )
        return [_decode_feedback(row) for row in rows]

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


def _decode_query(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["knowledge_base_ids"] = _load_json(data.pop("knowledge_base_ids_json"), [])
    data["tool_calls"] = _load_json(data.pop("tool_calls_json"), [])
    data["citation_chunk_ids"] = _load_json(data.pop("citation_chunk_ids_json"), [])
    data["response_metadata"] = _load_json(data.pop("response_metadata_json"), {})
    return data


def _decode_feedback(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["source_chunk_ids"] = _load_json(data.pop("source_chunk_ids_json"), [])
    data["metadata"] = _load_json(data.pop("metadata_json"), {})
    return data


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value or _dump(default))


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    return row.get(key, default) if hasattr(row, "get") else row[key]


def _placeholders(items: Any) -> str:
    count = len(tuple(items))
    if count <= 0:
        raise ValueError("At least one value is required")
    return ", ".join("%s" for _ in range(count))
