from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from app.services.async_runtime.config import AsyncRuntimeConfig
from app.services.async_runtime.task_envelope import ProcessingTaskEnvelope
from app.services.async_runtime.task_routes import (
    CORE_QUEUE,
    ENRICHMENT_QUEUE,
    MAINTENANCE_QUEUE,
    POSTPROCESS_QUEUE,
    SHARED_QUEUE,
    WIKI_QUEUE,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrokerDispatchResult:
    task_id: str
    broker_task_id: str
    queue: str
    dispatched: bool
    mode: str


class AsyncProcessingQueue(Protocol):
    enabled: bool

    def enqueue(self, task: dict[str, Any]) -> BrokerDispatchResult:
        ...

    def health(self) -> dict[str, Any]:
        ...


class DisabledProcessingQueue:
    enabled = False

    def enqueue(self, task: dict[str, Any]) -> BrokerDispatchResult:
        envelope = ProcessingTaskEnvelope.from_task(task)
        return BrokerDispatchResult(
            task_id=envelope.task_id,
            broker_task_id="",
            queue=envelope.queue,
            dispatched=False,
            mode="disabled",
        )

    def health(self) -> dict[str, Any]:
        return {"enabled": False, "mode": "disabled", "ok": True, "queues": {}}


class CeleryProcessingQueue:
    def __init__(self, app: Any, config: AsyncRuntimeConfig):
        self.app = app
        self.config = config
        self.enabled = bool(config.celery_enabled)

    def enqueue(self, task: dict[str, Any]) -> BrokerDispatchResult:
        envelope = ProcessingTaskEnvelope.from_task(task)
        if not self.enabled:
            return BrokerDispatchResult(
                task_id=envelope.task_id,
                broker_task_id="",
                queue=envelope.queue,
                dispatched=False,
                mode=self.config.mode,
            )
        queue_name = _configured_queue_name(envelope.queue, self.config)
        send_kwargs: dict[str, Any] = {
            "args": [envelope.to_dict()],
            "task_id": envelope.task_id,
            "queue": queue_name,
        }
        countdown = _countdown_seconds(envelope.next_run_at)
        if countdown > 0:
            send_kwargs["countdown"] = countdown
        async_result = self.app.send_task(self.config.task_name, **send_kwargs)
        broker_task_id = str(getattr(async_result, "id", "") or envelope.task_id)
        logger.info(
            "async_runtime.queue.dispatched",
            extra={
                "task_id": envelope.task_id,
                "task_type": envelope.task_type,
                "queue": queue_name,
                "broker_task_id": broker_task_id,
            },
        )
        return BrokerDispatchResult(
            task_id=envelope.task_id,
            broker_task_id=broker_task_id,
            queue=queue_name,
            dispatched=True,
            mode="celery",
        )

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.config.mode,
            "broker_url": _safe_url(self.config.broker_url),
            "queues": self.config.queues,
            "concurrency": self.config.concurrency,
        }


def _safe_url(value: str) -> str:
    if "@" not in value:
        return value
    scheme, rest = value.split("://", 1) if "://" in value else ("", value)
    host = rest.split("@", 1)[1]
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"


def _configured_queue_name(logical_queue: str, config: AsyncRuntimeConfig) -> str:
    return {
        CORE_QUEUE: config.core.queue,
        POSTPROCESS_QUEUE: config.postprocess.queue,
        ENRICHMENT_QUEUE: config.enrichment.queue,
        MAINTENANCE_QUEUE: config.maintenance.queue,
        SHARED_QUEUE: config.shared.queue,
        WIKI_QUEUE: config.wiki.queue,
    }.get(logical_queue, config.shared.queue)


def _countdown_seconds(next_run_at: str) -> int:
    if not next_run_at:
        return 0
    try:
        target = datetime.fromisoformat(next_run_at)
    except ValueError:
        return 0
    if target.tzinfo is None:
        now = datetime.now()
        return max(0, int((target - now).total_seconds()))
    else:
        aware_seconds = int((target - datetime.now(timezone.utc)).total_seconds())
        local_seconds = int((target.replace(tzinfo=None) - datetime.now()).total_seconds())
        if abs(aware_seconds - local_seconds) >= 3600:
            return max(0, local_seconds)
        return max(0, aware_seconds)
