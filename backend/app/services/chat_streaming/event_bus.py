from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from app.models.agent_runtime import PRIVATE_TRACE_KEYS


@dataclass(frozen=True)
class ChatStreamEvent:
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    offset: int = 0
    terminal: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "offset": self.offset,
            "terminal": self.terminal,
            "created_at": self.created_at,
        }


class ChatEventBus:
    def __init__(self):
        self._subscribers: list[Callable[[ChatStreamEvent], None]] = []

    def subscribe(self, handler: Callable[[ChatStreamEvent], None]) -> None:
        self._subscribers.append(handler)

    def publish(self, event_type: str, payload: dict[str, Any] | None = None, *, terminal: bool = False) -> ChatStreamEvent:
        event = ChatStreamEvent(event_type=event_type, payload=_sanitize_payload(payload or {}), terminal=terminal)
        for handler in list(self._subscribers):
            handler(event)
        return event


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in PRIVATE_TRACE_KEYS:
                continue
            sanitized[str(key)] = _sanitize_payload(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    return value
