from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

from app.services.memory.principal import Principal, anonymous_principal, session_owner_id_from_context
from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import PostgresSchemaConfig, inspect_postgres_startup_storage, qname


"""PostgreSQL chat history repository.

The relational database is authoritative for durable session and message
history. Redis/StreamManager may buffer current SSE events and temporary
web-search state, but there is no Redis cache layer above history tables.
"""

ROLE_ORDER = {"user": 0, "assistant": 1}
_TIMESTAMP_LOCK = threading.Lock()
_LAST_TIMESTAMP: datetime | None = None


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
        if os.getenv("CHAT_HISTORY_AUTO_MIGRATE", "").strip().lower() in {"1", "true", "yes", "on"}:
            self._ensure_history_columns()

    def _ensure_history_columns(self) -> None:
        statements = (
            f"alter table {self._table('conversation')} add column if not exists tenant_id text not null default ''",
            f"alter table {self._table('conversation')} add column if not exists user_id text not null default ''",
            f"alter table {self._table('conversation')} add column if not exists agent_config jsonb not null default '{{}}'::jsonb",
            f"alter table {self._table('conversation')} add column if not exists deleted_at timestamptz",
            f"alter table {self._table('conversation_message')} add column if not exists request_id text not null default ''",
            f"alter table {self._table('conversation_message')} add column if not exists is_completed boolean not null default true",
            f"alter table {self._table('conversation_message')} add column if not exists updated_at timestamptz",
            f"alter table {self._table('conversation_message')} add column if not exists deleted_at timestamptz",
            f"update {self._table('conversation_message')} set updated_at = created_at where updated_at is null",
            f"create index if not exists idx_conversation_owner on {self._table('conversation')}(tenant_id, user_id, updated_at)",
            f"create index if not exists idx_conversation_message_request on {self._table('conversation_message')}(conversation_id, request_id, role)",
        )
        with self.database.transaction() as conn:
            for statement in statements:
                self._execute_in_conn(conn, statement, ())

    def create_conversation(
        self,
        title: str = "",
        *,
        principal: Principal | None = None,
        agent_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        principal = principal or anonymous_principal()
        now = _now()
        conversation = {
            "id": f"conv_{uuid.uuid4().hex}",
            "tenant_id": principal.tenant_id,
            "user_id": session_owner_id_from_context(principal),
            "title": title,
            "summary": "",
            "agent_config": agent_config or {},
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
        }
        self._execute(
            f"""
            insert into {self._table('conversation')}
            (id, tenant_id, user_id, title, summary, agent_config, created_at, updated_at)
            values (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                conversation["id"],
                conversation["tenant_id"],
                conversation["user_id"],
                conversation["title"],
                conversation["summary"],
                _dump(conversation["agent_config"]),
                conversation["created_at"],
                conversation["updated_at"],
            ),
        )
        return conversation

    def get_conversation(self, conversation_id: str, *, principal: Principal | None = None) -> dict[str, Any] | None:
        principal = principal or anonymous_principal()
        row = self._fetch_one(
            f"""
            select * from {self._table('conversation')}
            where id = %s and tenant_id = %s and deleted_at is null
            and (user_id = %s or user_id is null or user_id = '')
            """,
            (conversation_id, principal.tenant_id, session_owner_id_from_context(principal)),
        )
        return self._decode_conversation(row) if row else None

    def update_summary(self, conversation_id: str, summary: str, *, principal: Principal | None = None) -> None:
        principal = principal or anonymous_principal()
        self._execute(
            f"""
            update {self._table('conversation')} set summary = %s, updated_at = %s
            where id = %s and tenant_id = %s and (user_id = %s or user_id is null or user_id = '')
            """,
            (summary, _now(), conversation_id, principal.tenant_id, session_owner_id_from_context(principal)),
        )

    def rename_conversation(self, conversation_id: str, title: str, *, principal: Principal | None = None) -> dict[str, Any] | None:
        principal = principal or anonymous_principal()
        rowcount = self._execute(
            f"""
            update {self._table('conversation')} set title = %s, updated_at = %s
            where id = %s and tenant_id = %s and deleted_at is null
            and (user_id = %s or user_id is null or user_id = '')
            """,
            (title.strip(), _now(), conversation_id, principal.tenant_id, session_owner_id_from_context(principal)),
        )
        return self.get_conversation(conversation_id, principal=principal) if rowcount else None

    def delete_conversation(self, conversation_id: str, *, principal: Principal | None = None) -> bool:
        principal = principal or anonymous_principal()
        now = _now()
        with self.database.transaction() as conn:
            rowcount = self._execute_in_conn(
                conn,
                f"""
                update {self._table('conversation')} set deleted_at = %s, updated_at = %s
                where id = %s and tenant_id = %s and deleted_at is null
                and (user_id = %s or user_id is null or user_id = '')
                """,
                (now, now, conversation_id, principal.tenant_id, session_owner_id_from_context(principal)),
            )
            if rowcount:
                self._execute_in_conn(
                    conn,
                    f"update {self._table('conversation_message')} set deleted_at = %s, updated_at = %s where conversation_id = %s and deleted_at is null",
                    (now, now, conversation_id),
                )
        return bool(rowcount)

    def list_conversations(self, *, limit: int = 20, principal: Principal | None = None) -> list[dict[str, Any]]:
        principal = principal or anonymous_principal()
        bounded_limit = max(1, min(int(limit), 100))
        rows = self._fetch_all(
            f"""
            select * from {self._table('conversation')}
            where tenant_id = %s and deleted_at is null
            and (user_id = %s or user_id is null or user_id = '')
            order by updated_at desc, created_at desc, id desc
            limit %s
            """,
            (principal.tenant_id, session_owner_id_from_context(principal), bounded_limit),
        )
        conversations = [self._decode_conversation(row) for row in rows]
        for conversation in conversations:
            latest_user = self._fetch_one(
                f"""
                select content from {self._table('conversation_message')}
                where conversation_id = %s and role = 'user' and deleted_at is null
                order by created_at desc, id desc
                limit 1
                """,
                (conversation["id"],),
            )
            preview = str(dict(latest_user).get("content") if latest_user else "").strip()
            running_message = self._fetch_one(
                f"""
                select id from {self._table('conversation_message')}
                where conversation_id = %s and role = 'assistant' and is_completed = false and deleted_at is null
                limit 1
                """,
                (conversation["id"],),
            )
            conversation["last_message_preview"] = preview
            conversation["display_title"] = _display_title(conversation, preview)
            conversation["is_running"] = running_message is not None
        return conversations

    def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata_json: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
        is_completed: bool = True,
    ) -> dict[str, Any]:
        now = _now()
        message = {
            "id": f"msg_{uuid.uuid4().hex}",
            "conversation_id": conversation_id,
            "request_id": request_id or f"req_{uuid.uuid4().hex}",
            "role": role,
            "content": content,
            "metadata_json": metadata_json or {},
            "is_completed": bool(is_completed),
            "created_at": now,
            "updated_at": now,
            "deleted_at": None,
        }
        with self.database.transaction() as conn:
            self._execute_in_conn(
                conn,
                f"""
                insert into {self._table('conversation_message')}
                (id, conversation_id, request_id, role, content, metadata_json, is_completed, created_at, updated_at)
                values (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                """,
                (
                    message["id"],
                    message["conversation_id"],
                    message["request_id"],
                    message["role"],
                    message["content"],
                    _dump(message["metadata_json"]),
                    message["is_completed"],
                    message["created_at"],
                    message["updated_at"],
                ),
            )
            self._execute_in_conn(
                conn,
                f"update {self._table('conversation')} set updated_at = %s where id = %s",
                (now, conversation_id),
            )
        return message

    def create_turn(
        self,
        conversation_id: str,
        user_content: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        request_id = f"req_{uuid.uuid4().hex}"
        user = self.append_message(conversation_id, "user", user_content, metadata_json or {}, request_id=request_id)
        assistant = self.append_message(conversation_id, "assistant", "", metadata_json or {}, request_id=request_id, is_completed=False)
        return user, assistant, request_id

    def get_message(
        self,
        conversation_id: str,
        message_id: str,
        *,
        principal: Principal | None = None,
    ) -> dict[str, Any] | None:
        if self.get_conversation(conversation_id, principal=principal) is None:
            return None
        row = self._fetch_one(
            f"""
            select * from {self._table('conversation_message')}
            where conversation_id = %s and id = %s and deleted_at is null
            """,
            (conversation_id, message_id),
        )
        return self._decode_message(row) if row else None

    def complete_assistant_message(
        self,
        conversation_id: str,
        message_id: str,
        content: str,
        metadata_json: dict[str, Any] | None = None,
        *,
        stopped: bool = False,
    ) -> dict[str, Any] | None:
        existing = self._fetch_one(
            f"""
            select * from {self._table('conversation_message')}
            where conversation_id = %s and id = %s and role = 'assistant' and deleted_at is null
            """,
            (conversation_id, message_id),
        )
        existing = self._decode_message(existing) if existing else None
        if existing is None:
            return None
        metadata = dict(existing.get("metadata_json") or {})
        if metadata_json:
            metadata.update(metadata_json)
        if stopped:
            metadata["stopped"] = True
        now = _now()
        with self.database.transaction() as conn:
            self._execute_in_conn(
                conn,
                f"""
                update {self._table('conversation_message')}
                set content = %s, metadata_json = %s::jsonb, is_completed = true, updated_at = %s
                where conversation_id = %s and id = %s and role = 'assistant' and is_completed = false
                """,
                (content, _dump(metadata), now, conversation_id, message_id),
            )
            self._execute_in_conn(
                conn,
                f"update {self._table('conversation')} set updated_at = %s where id = %s",
                (now, conversation_id),
            )
        row = self._fetch_one(
            f"""
            select * from {self._table('conversation_message')}
            where conversation_id = %s and id = %s and deleted_at is null
            """,
            (conversation_id, message_id),
        )
        return self._decode_message(row) if row else None

    def list_messages(self, conversation_id: str, *, principal: Principal | None = None) -> list[dict[str, Any]]:
        if self.get_conversation(conversation_id, principal=principal) is None:
            return []
        rows = self._fetch_all(
            f"""
            select * from {self._table('conversation_message')}
            where conversation_id = %s and deleted_at is null
            order by created_at asc, request_id asc,
                case role when 'user' then 0 when 'assistant' then 1 else 2 end asc,
                id asc
            """,
            (conversation_id,),
        )
        return [self._decode_message(row) for row in rows]

    def list_recent_messages(self, conversation_id: str, limit: int, *, principal: Principal | None = None) -> list[dict[str, Any]]:
        if self.get_conversation(conversation_id, principal=principal) is None:
            return []
        rows = self._fetch_all(
            f"""
            select * from {self._table('conversation_message')}
            where conversation_id = %s and deleted_at is null
            order by created_at desc, id desc
            limit %s
            """,
            (conversation_id, int(limit)),
        )
        return _sort_messages([self._decode_message(row) for row in rows])

    def list_messages_before_time(
        self,
        conversation_id: str,
        before_time: str | None = None,
        *,
        limit: int = 20,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        if self.get_conversation(conversation_id, principal=principal) is None:
            return {"items": [], "hasMoreHistory": False}
        bounded_limit = max(1, min(int(limit), 100))
        params: list[Any] = [conversation_id]
        cursor_clause = ""
        if before_time:
            cursor_clause = "and created_at < %s"
            params.append(before_time)
        params.append(bounded_limit + 1)
        rows = self._fetch_all(
            f"""
            select * from {self._table('conversation_message')}
            where conversation_id = %s and deleted_at is null {cursor_clause}
            order by created_at desc, id desc
            limit %s
            """,
            tuple(params),
        )
        decoded = [self._decode_message(row) for row in rows]
        return {"items": _sort_messages(decoded[:bounded_limit]), "hasMoreHistory": len(decoded) > bounded_limit}

    def _decode_conversation(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["agent_config"] = _load_json(data.get("agent_config"), {})
        return data

    def _decode_message(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["metadata_json"] = _load_json(data["metadata_json"], {})
        data["is_completed"] = bool(data.get("is_completed", True))
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
    global _LAST_TIMESTAMP
    with _TIMESTAMP_LOCK:
        current = datetime.now()
        if _LAST_TIMESTAMP is not None and current <= _LAST_TIMESTAMP:
            current = _LAST_TIMESTAMP + timedelta(microseconds=1)
        _LAST_TIMESTAMP = current
        return current.isoformat(timespec="microseconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value or _dump(default))


def _sort_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        messages,
        key=lambda item: (
            str(item.get("created_at") or ""),
            str(item.get("request_id") or ""),
            ROLE_ORDER.get(str(item.get("role") or ""), 2),
            str(item.get("id") or ""),
        ),
    )


def _display_title(conversation: dict[str, Any], preview: str) -> str:
    title = str(conversation.get("title") or "").strip()
    text = title or preview or "新对话"
    return text if len(text) <= 36 else f"{text[:36]}..."
