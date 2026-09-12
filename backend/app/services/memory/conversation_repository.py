import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.services.memory.principal import Principal, anonymous_principal, session_owner_id_from_context


"""SQLite/local chat history repository.

The relational database is the only source of truth for durable session and
message history. Redis/StreamManager is intentionally limited to transient SSE
replay buffers and temporary web-search knowledge state, never history reads.
"""

ROLE_ORDER = {"user": 0, "assistant": 1}
_TIMESTAMP_LOCK = threading.Lock()
_LAST_TIMESTAMP: datetime | None = None


class ConversationRepository:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists conversation (
                    id text primary key,
                    tenant_id text not null default '',
                    user_id text not null default '',
                    title text not null,
                    summary text not null,
                    agent_config text not null default '{}',
                    created_at text not null,
                    updated_at text not null,
                    deleted_at text
                )
                """
            )
            conn.execute(
                """
                create table if not exists conversation_message (
                    id text primary key,
                    conversation_id text not null,
                    request_id text not null default '',
                    role text not null,
                    content text not null,
                    metadata_json text not null,
                    is_completed integer not null default 1,
                    created_at text not null,
                    updated_at text not null,
                    deleted_at text,
                    foreign key(conversation_id) references conversation(id)
                )
                """
            )
            self._ensure_columns(conn)

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        self._ensure_column(conn, "conversation", "tenant_id", "text not null default ''")
        self._ensure_column(conn, "conversation", "user_id", "text not null default ''")
        self._ensure_column(conn, "conversation", "agent_config", "text not null default '{}'")
        self._ensure_column(conn, "conversation", "deleted_at", "text")
        self._ensure_column(conn, "conversation_message", "request_id", "text not null default ''")
        self._ensure_column(conn, "conversation_message", "is_completed", "integer not null default 1")
        self._ensure_column(conn, "conversation_message", "updated_at", "text")
        self._ensure_column(conn, "conversation_message", "deleted_at", "text")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"alter table {table} add column {column} {definition}")

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
        with self._connect() as conn:
            conn.execute(
                """
                insert into conversation (id, tenant_id, user_id, title, summary, agent_config, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation["id"],
                    conversation["tenant_id"],
                    conversation["user_id"],
                    conversation["title"],
                    conversation["summary"],
                    json.dumps(conversation["agent_config"], ensure_ascii=False),
                    conversation["created_at"],
                    conversation["updated_at"],
                ),
            )
        return conversation

    def get_conversation(self, conversation_id: str, *, principal: Principal | None = None) -> dict[str, Any] | None:
        principal = principal or anonymous_principal()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                select * from conversation
                where id = ? and tenant_id = ? and deleted_at is null
                and (user_id = ? or user_id is null or user_id = '')
                """,
                (conversation_id, principal.tenant_id, session_owner_id_from_context(principal)),
            ).fetchone()
        return self._decode_conversation(row) if row else None

    def update_summary(self, conversation_id: str, summary: str, *, principal: Principal | None = None) -> None:
        principal = principal or anonymous_principal()
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                update conversation set summary = ?, updated_at = ?
                where id = ? and tenant_id = ? and (user_id = ? or user_id is null or user_id = '')
                """,
                (summary, now, conversation_id, principal.tenant_id, session_owner_id_from_context(principal)),
            )

    def rename_conversation(self, conversation_id: str, title: str, *, principal: Principal | None = None) -> dict[str, Any] | None:
        principal = principal or anonymous_principal()
        now = _now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update conversation set title = ?, updated_at = ?
                where id = ? and tenant_id = ? and deleted_at is null
                and (user_id = ? or user_id is null or user_id = '')
                """,
                (title.strip(), now, conversation_id, principal.tenant_id, session_owner_id_from_context(principal)),
            )
        return self.get_conversation(conversation_id, principal=principal) if cursor.rowcount else None

    def delete_conversation(self, conversation_id: str, *, principal: Principal | None = None) -> bool:
        principal = principal or anonymous_principal()
        now = _now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update conversation set deleted_at = ?, updated_at = ?
                where id = ? and tenant_id = ? and deleted_at is null
                and (user_id = ? or user_id is null or user_id = '')
                """,
                (now, now, conversation_id, principal.tenant_id, session_owner_id_from_context(principal)),
            )
            if cursor.rowcount:
                conn.execute("update conversation_message set deleted_at = ?, updated_at = ? where conversation_id = ? and deleted_at is null", (now, now, conversation_id))
        return bool(cursor.rowcount)

    def list_conversations(self, *, limit: int = 20, principal: Principal | None = None) -> list[dict[str, Any]]:
        principal = principal or anonymous_principal()
        bounded_limit = max(1, min(int(limit), 100))
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from conversation
                where tenant_id = ? and deleted_at is null
                and (user_id = ? or user_id is null or user_id = '')
                order by updated_at desc, created_at desc, id desc
                limit ?
                """,
                (principal.tenant_id, session_owner_id_from_context(principal), bounded_limit),
            ).fetchall()
            conversations = [self._decode_conversation(row) for row in rows]
            for conversation in conversations:
                latest_user = conn.execute(
                    """
                    select content from conversation_message
                    where conversation_id = ? and role = 'user' and deleted_at is null
                    order by created_at desc, id desc
                    limit 1
                    """,
                    (conversation["id"],),
                ).fetchone()
                preview = str(latest_user["content"] if latest_user else "").strip()
                running_message = conn.execute(
                    """
                    select id from conversation_message
                    where conversation_id = ? and role = 'assistant' and is_completed = 0 and deleted_at is null
                    limit 1
                    """,
                    (conversation["id"],),
                ).fetchone()
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
        with self._connect() as conn:
            conn.execute(
                """
                insert into conversation_message
                (id, conversation_id, request_id, role, content, metadata_json, is_completed, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message["id"],
                    message["conversation_id"],
                    message["request_id"],
                    message["role"],
                    message["content"],
                    json.dumps(message["metadata_json"], ensure_ascii=False),
                    1 if message["is_completed"] else 0,
                    message["created_at"],
                    message["updated_at"],
                ),
            )
            conn.execute("update conversation set updated_at = ? where id = ?", (now, conversation_id))
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
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from conversation_message where conversation_id = ? and id = ? and deleted_at is null",
                (conversation_id, message_id),
            ).fetchone()
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
        now = _now()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            existing_row = conn.execute(
                "select * from conversation_message where conversation_id = ? and id = ? and role = 'assistant' and deleted_at is null",
                (conversation_id, message_id),
            ).fetchone()
        existing = self._decode_message(existing_row) if existing_row else None
        if existing is None:
            return None
        metadata = dict(existing.get("metadata_json") or {})
        if metadata_json:
            metadata.update(metadata_json)
        if stopped:
            metadata["stopped"] = True
        with self._connect() as conn:
            conn.execute(
                """
                update conversation_message
                set content = ?, metadata_json = ?, is_completed = 1, updated_at = ?
                where conversation_id = ? and id = ? and role = 'assistant' and is_completed = 0
                """,
                (content, json.dumps(metadata, ensure_ascii=False), now, conversation_id, message_id),
            )
            conn.execute("update conversation set updated_at = ? where id = ?", (now, conversation_id))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "select * from conversation_message where conversation_id = ? and id = ? and deleted_at is null",
                (conversation_id, message_id),
            ).fetchone()
        return self._decode_message(row) if row else None

    def list_messages(self, conversation_id: str, *, principal: Principal | None = None) -> list[dict[str, Any]]:
        if self.get_conversation(conversation_id, principal=principal) is None:
            return []
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from conversation_message
                where conversation_id = ? and deleted_at is null
                order by created_at asc, request_id asc,
                    case role when 'user' then 0 when 'assistant' then 1 else 2 end asc,
                    id asc
                """,
                (conversation_id,),
            ).fetchall()
        return [self._decode_message(row) for row in rows]

    def list_recent_messages(self, conversation_id: str, limit: int, *, principal: Principal | None = None) -> list[dict[str, Any]]:
        if self.get_conversation(conversation_id, principal=principal) is None:
            return []
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select * from conversation_message
                where conversation_id = ? and deleted_at is null
                order by created_at desc, id desc
                limit ?
                """,
                (conversation_id, limit),
            ).fetchall()
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
            cursor_clause = "and created_at < ?"
            params.append(before_time)
        params.append(bounded_limit + 1)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                select * from conversation_message
                where conversation_id = ? and deleted_at is null {cursor_clause}
                order by created_at desc, id desc
                limit ?
                """,
                tuple(params),
            ).fetchall()
        decoded = [self._decode_message(row) for row in rows]
        return {"items": _sort_messages(decoded[:bounded_limit]), "hasMoreHistory": len(decoded) > bounded_limit}

    def _decode_conversation(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["agent_config"] = json.loads(data.get("agent_config") or "{}")
        return data

    def _decode_message(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["metadata_json"] = json.loads(data["metadata_json"] or "{}")
        data["is_completed"] = bool(data.get("is_completed", 1))
        return data


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


def _now() -> str:
    global _LAST_TIMESTAMP
    with _TIMESTAMP_LOCK:
        current = datetime.now()
        if _LAST_TIMESTAMP is not None and current <= _LAST_TIMESTAMP:
            current = _LAST_TIMESTAMP + timedelta(microseconds=1)
        _LAST_TIMESTAMP = current
        return current.isoformat(timespec="microseconds")


def _display_title(conversation: dict[str, Any], preview: str) -> str:
    title = str(conversation.get("title") or "").strip()
    text = title or preview or "新对话"
    return text if len(text) <= 36 else f"{text[:36]}..."
