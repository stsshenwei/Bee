from __future__ import annotations

import os

from app.services.async_runtime.config import AsyncRuntimeConfig
from app.services.retrieval.rag_config import load_rag_config


def _build_config() -> AsyncRuntimeConfig:
    raw = dict(load_rag_config(os.getenv("RAG_CONFIG_PATH") or None).get("rag", {}).get("async_runtime", {}))
    raw.update(
        {
            "enabled": _env_bool("ASYNC_RUNTIME_ENABLED", bool(raw.get("enabled", False))),
            "mode": os.getenv("ASYNC_RUNTIME_MODE", str(raw.get("mode", "local"))),
            "broker_url": os.getenv("ASYNC_RUNTIME_BROKER_URL", str(raw.get("broker_url", "redis://localhost:6379/0"))),
            "result_backend_url": os.getenv("ASYNC_RUNTIME_RESULT_BACKEND_URL", str(raw.get("result_backend_url", ""))),
            "core_queue": os.getenv("ASYNC_RUNTIME_CORE_QUEUE", str(raw.get("core", {}).get("queue", "core"))),
            "postprocess_queue": os.getenv(
                "ASYNC_RUNTIME_POSTPROCESS_QUEUE",
                str(raw.get("postprocess", {}).get("queue", "postprocess")),
            ),
            "enrichment_queue": os.getenv(
                "ASYNC_RUNTIME_ENRICHMENT_QUEUE",
                str(raw.get("enrichment", {}).get("queue", "enrichment")),
            ),
            "maintenance_queue": os.getenv(
                "ASYNC_RUNTIME_MAINTENANCE_QUEUE",
                str(raw.get("maintenance", {}).get("queue", "maintenance")),
            ),
            "shared_queue": os.getenv("ASYNC_RUNTIME_SHARED_QUEUE", str(raw.get("shared", {}).get("queue", "shared"))),
            "wiki_queue": os.getenv("ASYNC_RUNTIME_WIKI_QUEUE", str(raw.get("wiki", {}).get("queue", "wiki"))),
        }
    )
    return AsyncRuntimeConfig.from_settings(raw)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def create_celery_app():
    try:
        from celery import Celery
        from kombu import Queue
    except ImportError as exc:
        raise RuntimeError("Celery is not installed. Run `pip install -r requirements.txt`.") from exc

    config = _build_config()
    result_backend = config.result_backend_url or None
    app = Celery("bee_async_runtime", broker=config.broker_url, backend=result_backend)
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        broker_connection_retry_on_startup=True,
        task_time_limit=config.default_time_limit_seconds,
        task_soft_time_limit=config.default_soft_time_limit_seconds,
        task_default_queue=config.shared.queue,
        task_queues=[
            Queue(config.core.queue),
            Queue(config.postprocess.queue),
            Queue(config.enrichment.queue),
            Queue(config.maintenance.queue),
            Queue(config.shared.queue),
            Queue(config.wiki.queue),
        ],
        task_routes={
            config.task_name: {"queue": config.shared.queue},
        },
        imports=("app.workers.processing_tasks",),
    )
    return app


celery_app = create_celery_app()
