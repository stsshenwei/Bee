from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WorkerPoolConfig:
    queue: str
    concurrency: int


@dataclass(frozen=True)
class AsyncRuntimeConfig:
    enabled: bool = False
    mode: str = "local"
    broker_url: str = ""
    result_backend_url: str = ""
    task_name: str = "app.workers.processing_tasks.process_processing_task"
    default_max_retries: int = 3
    default_time_limit_seconds: int = 3600
    default_soft_time_limit_seconds: int = 3300
    local_fallback_enabled: bool = True
    local_worker_enabled: bool = True
    broker_connection_timeout_seconds: float = 2.0
    core: WorkerPoolConfig = WorkerPoolConfig(queue="core", concurrency=8)
    postprocess: WorkerPoolConfig = WorkerPoolConfig(queue="postprocess", concurrency=2)
    enrichment: WorkerPoolConfig = WorkerPoolConfig(queue="enrichment", concurrency=12)
    maintenance: WorkerPoolConfig = WorkerPoolConfig(queue="maintenance", concurrency=4)
    shared: WorkerPoolConfig = WorkerPoolConfig(queue="shared", concurrency=6)
    wiki: WorkerPoolConfig = WorkerPoolConfig(queue="wiki", concurrency=8)

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None = None) -> "AsyncRuntimeConfig":
        raw = settings or {}
        return cls(
            enabled=_bool(raw.get("enabled"), cls.enabled),
            mode=_mode(raw.get("mode"), cls.mode),
            broker_url=_text(raw.get("broker_url"), cls.broker_url),
            result_backend_url=_text(raw.get("result_backend_url"), cls.result_backend_url),
            task_name=_text(raw.get("task_name"), cls.task_name),
            default_max_retries=max(0, _int(raw.get("default_max_retries"), cls.default_max_retries)),
            default_time_limit_seconds=max(1, _int(raw.get("default_time_limit_seconds"), cls.default_time_limit_seconds)),
            default_soft_time_limit_seconds=max(1, _int(raw.get("default_soft_time_limit_seconds"), cls.default_soft_time_limit_seconds)),
            local_fallback_enabled=_bool(raw.get("local_fallback_enabled"), cls.local_fallback_enabled),
            local_worker_enabled=_bool(raw.get("local_worker_enabled"), cls.local_worker_enabled),
            broker_connection_timeout_seconds=max(
                0.1,
                _float(raw.get("broker_connection_timeout_seconds"), cls.broker_connection_timeout_seconds),
            ),
            core=_pool(raw, "core", cls.core),
            postprocess=_pool(raw, "postprocess", cls.postprocess),
            enrichment=_pool(raw, "enrichment", cls.enrichment),
            maintenance=_pool(raw, "maintenance", cls.maintenance),
            shared=_pool(raw, "shared", cls.shared),
            wiki=_pool(raw, "wiki", cls.wiki),
        )

    @property
    def celery_enabled(self) -> bool:
        return self.enabled and self.mode == "celery"

    @property
    def queues(self) -> dict[str, str]:
        return {
            "core": self.core.queue,
            "postprocess": self.postprocess.queue,
            "enrichment": self.enrichment.queue,
            "maintenance": self.maintenance.queue,
            "shared": self.shared.queue,
            "wiki": self.wiki.queue,
        }

    @property
    def concurrency(self) -> dict[str, int]:
        return {
            "core": self.core.concurrency,
            "postprocess": self.postprocess.concurrency,
            "enrichment": self.enrichment.concurrency,
            "maintenance": self.maintenance.concurrency,
            "shared": self.shared.concurrency,
            "wiki": self.wiki.concurrency,
        }


def _pool(raw: dict[str, Any], name: str, default: WorkerPoolConfig) -> WorkerPoolConfig:
    nested = raw.get(name)
    if not isinstance(nested, dict):
        nested = {}
    queue_key = f"{name}_queue"
    concurrency_key = f"{name}_concurrency"
    return WorkerPoolConfig(
        queue=_text(nested.get("queue", raw.get(queue_key)), default.queue),
        concurrency=max(1, _int(nested.get("concurrency", raw.get(concurrency_key)), default.concurrency)),
    )


def _mode(value: Any, default: str) -> str:
    mode = _text(value, default).lower()
    if mode in {"celery", "local", "disabled"}:
        return mode
    return default


def _text(value: Any, default: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text or default


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
