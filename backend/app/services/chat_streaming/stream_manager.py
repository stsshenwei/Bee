from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from app.services.chat_streaming.event_bus import ChatStreamEvent


@dataclass(frozen=True)
class StreamIdentity:
    session_id: str
    message_id: str

    @property
    def key(self) -> str:
        return f"{self.session_id}:{self.message_id}"


class StreamManager(Protocol):
    def append(self, identity: StreamIdentity, event: ChatStreamEvent) -> ChatStreamEvent:
        ...

    def read_after(self, identity: StreamIdentity, offset: int, *, limit: int = 100) -> list[ChatStreamEvent]:
        ...

    def is_terminal(self, identity: StreamIdentity) -> bool:
        ...

    def cleanup(self, identity: StreamIdentity) -> None:
        ...


class MemoryStreamManager:
    def __init__(self):
        self._events: dict[str, list[ChatStreamEvent]] = {}
        self._terminal: set[str] = set()
        self._lock = threading.Lock()

    def append(self, identity: StreamIdentity, event: ChatStreamEvent) -> ChatStreamEvent:
        with self._lock:
            events = self._events.setdefault(identity.key, [])
            stored = ChatStreamEvent(
                event_type=event.event_type,
                payload=dict(event.payload),
                offset=len(events) + 1,
                terminal=event.terminal,
                created_at=event.created_at,
            )
            events.append(stored)
            if stored.terminal:
                self._terminal.add(identity.key)
            return stored

    def read_after(self, identity: StreamIdentity, offset: int, *, limit: int = 100) -> list[ChatStreamEvent]:
        with self._lock:
            events = list(self._events.get(identity.key, []))
        return [event for event in events if event.offset > int(offset)][: max(1, int(limit))]

    def is_terminal(self, identity: StreamIdentity) -> bool:
        return identity.key in self._terminal

    def cleanup(self, identity: StreamIdentity) -> None:
        with self._lock:
            self._events.pop(identity.key, None)
            self._terminal.discard(identity.key)


class RedisStreamManager:
    def __init__(self, redis_url: str, *, key_prefix: str = "chat:stream", ttl_seconds: int = 86400):
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("redis package is not installed. Run `pip install -r requirements.txt`.") from exc
        self.client = redis.Redis.from_url(redis_url, decode_responses=True)
        self.key_prefix = key_prefix.strip(":") or "chat:stream"
        self.ttl_seconds = max(1, int(ttl_seconds))

    def append(self, identity: StreamIdentity, event: ChatStreamEvent) -> ChatStreamEvent:
        key = self._key(identity)
        offset = int(self.client.incr(f"{key}:offset"))
        stored = ChatStreamEvent(
            event_type=event.event_type,
            payload=dict(event.payload),
            offset=offset,
            terminal=event.terminal,
            created_at=event.created_at,
        )
        self.client.rpush(key, json.dumps(stored.to_dict(), ensure_ascii=False, sort_keys=True))
        self.client.expire(key, self.ttl_seconds)
        self.client.expire(f"{key}:offset", self.ttl_seconds)
        if stored.terminal:
            self.client.setex(f"{key}:terminal", self.ttl_seconds, "1")
        return stored

    def read_after(self, identity: StreamIdentity, offset: int, *, limit: int = 100) -> list[ChatStreamEvent]:
        start = max(0, int(offset))
        end = start + max(1, int(limit)) - 1
        rows = self.client.lrange(self._key(identity), start, end)
        return [_event_from_json(row) for row in rows]

    def is_terminal(self, identity: StreamIdentity) -> bool:
        return bool(self.client.exists(f"{self._key(identity)}:terminal"))

    def cleanup(self, identity: StreamIdentity) -> None:
        key = self._key(identity)
        self.client.delete(key, f"{key}:offset", f"{key}:terminal")

    def _key(self, identity: StreamIdentity) -> str:
        return f"{self.key_prefix}:{identity.session_id}:{identity.message_id}"


def _event_from_json(raw: str) -> ChatStreamEvent:
    data = json.loads(raw)
    return ChatStreamEvent(
        event_type=str(data.get("event_type") or ""),
        payload=dict(data.get("payload") or {}),
        offset=int(data.get("offset") or 0),
        terminal=bool(data.get("terminal")),
        created_at=str(data.get("created_at") or ""),
    )
