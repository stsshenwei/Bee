from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import PostgresSchemaConfig, inspect_postgres_startup_storage, qname


class PostgresMemoryRepository:
    def __init__(
        self,
        database: PostgresDatabase,
        *,
        schema: str | None = None,
        validate_schema: bool = True,
    ):
        self.database = database
        self.schema = schema or database.settings.schema
        if validate_schema:
            inspect_postgres_startup_storage(database, config=PostgresSchemaConfig(schema=self.schema))

    def upsert_memory(
        self,
        scope: str,
        memory_type: str,
        normalized_key: str,
        content: str,
        confidence: float,
        source_conversation_id: str | None = None,
        source_message_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        existing = self._fetch_one(
            f"""
            select * from {self._table('memory')}
            where scope = %s and normalized_key = %s and status = 'active'
            """,
            (scope, normalized_key),
        )
        memory_id = str(_row_get(existing, "id")) if existing else f"mem_{uuid.uuid4().hex}"
        created_at = str(_row_get(existing, "created_at")) if existing else now
        self._execute(
            f"""
            insert into {self._table('memory')}(
                id, scope, type, normalized_key, memory_key, content, confidence, status,
                source_conversation_id, source_message_id, metadata_json, created_at, updated_at
            ) values (%s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, '{{}}'::jsonb, %s, %s)
            on conflict(scope, normalized_key, status) do update set
                id = excluded.id,
                type = excluded.type,
                memory_key = excluded.memory_key,
                content = excluded.content,
                confidence = excluded.confidence,
                source_conversation_id = excluded.source_conversation_id,
                source_message_id = excluded.source_message_id,
                updated_at = excluded.updated_at
            """,
            (
                memory_id,
                scope,
                memory_type,
                normalized_key,
                normalized_key,
                content,
                float(confidence),
                source_conversation_id,
                source_message_id,
                created_at,
                now,
            ),
        )
        loaded = self.get_memory(memory_id)
        if loaded is None:
            raise RuntimeError("Failed to upsert memory")
        return loaded

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        row = self._fetch_one(f"select * from {self._table('memory')} where id = %s", (memory_id,))
        return self._decode_row(row) if row else None

    def list_active_memories(self, scope: str | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = "where status = 'active'"
        if scope:
            where += " and scope = %s"
            params.append(scope)
        rows = self._fetch_all(
            f"select * from {self._table('memory')} {where} order by updated_at desc, id",
            tuple(params),
        )
        return [self._decode_row(row) for row in rows]

    def delete_memory(self, memory_id: str) -> bool:
        rowcount = self._execute(
            f"update {self._table('memory')} set status = 'deleted', updated_at = %s where id = %s and status != 'deleted'",
            (_now(), memory_id),
        )
        return rowcount > 0

    def _decode_row(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["metadata_json"] = _load_json(data.get("metadata_json"), {})
        return data

    def _fetch_one(self, sql: str, params: tuple[Any, ...]) -> Any | None:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()

    def _fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())

    def _execute(self, sql: str, params: tuple[Any, ...]) -> int:
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return int(getattr(cur, "rowcount", 0) or 0)

    def _table(self, table: str) -> str:
        return qname(self.schema, table)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value or json.dumps(default))


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    return row.get(key, default) if hasattr(row, "get") else row[key]
