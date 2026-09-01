from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import PostgresSchemaConfig, inspect_postgres_startup_storage, qname


class PostgresConversationRepository:
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

    def create_conversation(self, title: str = "") -> dict[str, Any]:
        now = _now()
        conversation = {
            "id": f"conv_{uuid.uuid4().hex}",
            "title": title,
            "summary": "",
            "created_at": now,
            "updated_at": now,
        }
        self._execute(
            f"""
            insert into {self._table('conversation')} (id, title, summary, created_at, updated_at)
            values (%s, %s, %s, %s, %s)
            """,
            (
                conversation["id"],
                conversation["title"],
                conversation["summary"],
                conversation["created_at"],
                conversation["updated_at"],
            ),
        )
        return conversation

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        row = self._fetch_one(f"select * from {self._table('conversation')} where id = %s", (conversation_id,))
        return dict(row) if row else None

    def update_summary(self, conversation_id: str, summary: str) -> None:
        self._execute(
            f"update {self._table('conversation')} set summary = %s, updated_at = %s where id = %s",
            (summary, _now(), conversation_id),
        )

    def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        message = {
            "id": f"msg_{uuid.uuid4().hex}",
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "metadata_json": metadata_json or {},
            "created_at": now,
        }
        with self.database.transaction() as conn:
            self._execute_in_conn(
                conn,
                f"""
                insert into {self._table('conversation_message')}
                (id, conversation_id, role, content, metadata_json, created_at)
                values (%s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    message["id"],
                    message["conversation_id"],
                    message["role"],
                    message["content"],
                    _dump(message["metadata_json"]),
                    message["created_at"],
                ),
            )
            self._execute_in_conn(
                conn,
                f"update {self._table('conversation')} set updated_at = %s where id = %s",
                (now, conversation_id),
            )
        return message

    def list_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        rows = self._fetch_all(
            f"select * from {self._table('conversation_message')} where conversation_id = %s order by created_at, id",
            (conversation_id,),
        )
        return [self._decode_message(row) for row in rows]

    def list_recent_messages(self, conversation_id: str, limit: int) -> list[dict[str, Any]]:
        rows = self._fetch_all(
            f"""
            select * from (
                select * from {self._table('conversation_message')}
                where conversation_id = %s
                order by created_at desc, id desc
                limit %s
            ) recent
            order by created_at, id
            """,
            (conversation_id, int(limit)),
        )
        return [self._decode_message(row) for row in rows]

    def _decode_message(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["metadata_json"] = _load_json(data["metadata_json"], {})
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
            return self._execute_in_conn(conn, sql, params)

    def _execute_in_conn(self, conn: Any, sql: str, params: tuple[Any, ...]) -> int:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return int(getattr(cur, "rowcount", 0) or 0)

    def _table(self, table: str) -> str:
        return qname(self.schema, table)


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value or _dump(default))
