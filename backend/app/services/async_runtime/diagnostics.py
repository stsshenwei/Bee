from __future__ import annotations

from typing import Any

from app.services.async_runtime.config import AsyncRuntimeConfig
from app.services.async_runtime.task_routes import TASK_TYPE_TO_QUEUE


def async_runtime_diagnostics(config: AsyncRuntimeConfig, queue_health: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "mode": config.mode,
        "celery_enabled": config.celery_enabled,
        "queues": config.queues,
        "concurrency": config.concurrency,
        "routes": dict(sorted(TASK_TYPE_TO_QUEUE.items())),
        "health": queue_health or {},
    }
