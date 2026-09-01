from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.services.async_runtime.task_envelope import ProcessingTaskEnvelope
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.processing_tasks.process_processing_task", bind=True)
def process_processing_task(self, envelope_data: dict[str, Any]) -> dict[str, Any]:
    envelope = ProcessingTaskEnvelope.from_task(envelope_data)
    from app.main import rag_service

    worker = getattr(rag_service, "processing_worker", None)
    if worker is None:
        raise RuntimeError("Processing worker service is not configured")
    claimed = worker.repository.claim_task(
        envelope.task_id,
        worker_id=f"celery-{getattr(self.request, 'hostname', 'worker')}",
        lease_seconds=worker.config.lease_timeout_seconds,
    )
    if claimed is None:
        return _handle_unclaimed_task(self, worker, envelope)
    worker._process_claimed_task(claimed)
    latest = worker.repository.get_task(envelope.task_id)
    if str(latest.get("status")) == "retrying":
        raise self.retry(
            countdown=_retry_countdown_seconds(str(latest.get("next_run_at") or "")),
            max_retries=max(0, int(latest.get("max_attempts") or 1)),
        )
    return {"task_id": envelope.task_id, "status": latest.get("status")}


def _handle_unclaimed_task(task_self: Any, worker: Any, envelope: ProcessingTaskEnvelope) -> dict[str, Any]:
    latest = worker.repository.get_task(envelope.task_id)
    latest_status = str((latest or {}).get("status") or "")
    if latest_status in {"pending", "retrying"}:
        countdown = max(1, _retry_countdown_seconds(str((latest or {}).get("next_run_at") or "")))
        logger.info(
            "async_runtime.task.claim_deferred",
            extra={
                "task_id": envelope.task_id,
                "task_type": envelope.task_type,
                "status": latest_status,
                "countdown": countdown,
            },
        )
        raise task_self.retry(
            countdown=countdown,
            max_retries=max(0, int((latest or {}).get("max_attempts") or 1)),
        )
    logger.info("async_runtime.task.skipped", extra={"task_id": envelope.task_id, "task_type": envelope.task_type})
    return {"task_id": envelope.task_id, "status": "skipped"}


def _retry_countdown_seconds(next_run_at: str) -> int:
    try:
        target = datetime.fromisoformat(next_run_at)
    except ValueError:
        return 0
    if target.tzinfo is None:
        return max(0, int((target - datetime.now()).total_seconds()))
    aware_seconds = int((target - datetime.now(timezone.utc)).total_seconds())
    local_seconds = int((target.replace(tzinfo=None) - datetime.now()).total_seconds())
    if abs(aware_seconds - local_seconds) >= 3600:
        return max(0, local_seconds)
    return max(0, aware_seconds)
