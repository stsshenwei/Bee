from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.async_runtime.queue import AsyncProcessingQueue
from app.services.processing.processing_task_repository import TASK_PENDING, TASK_PROCESSING, TASK_RETRYING

logger = logging.getLogger(__name__)


def reconcile_processing_queue(
    repository: Any,
    queue: AsyncProcessingQueue,
    *,
    task_types: set[str] | None = None,
    limit: int = 100,
    stale_broker_after_seconds: int = 300,
    requeue_stale_processing: bool = True,
) -> dict[str, Any]:
    if not getattr(queue, "enabled", False):
        return {"enabled": False, "scanned": 0, "dispatched": 0, "skipped": 0}
    candidates = repository.list_tasks(
        statuses={TASK_PENDING, TASK_RETRYING, TASK_PROCESSING},
        task_types=task_types,
    )
    scanned = 0
    dispatched = 0
    skipped = 0
    for task in candidates[: max(1, int(limit))]:
        scanned += 1
        is_processing = str(task.get("status") or "") == TASK_PROCESSING
        processing_lease_is_stale = False
        if is_processing:
            processing_lease_is_stale = _processing_lease_is_stale(
                task,
                stale_after_seconds=stale_broker_after_seconds,
            )
            if not processing_lease_is_stale:
                skipped += 1
                continue
        payload = dict(task.get("payload") or {})
        broker_meta = payload.get("_async_runtime") if isinstance(payload.get("_async_runtime"), dict) else {}
        if is_processing and not requeue_stale_processing:
            skipped += 1
            continue
        if not processing_lease_is_stale and broker_meta.get("broker_task_id") and not _broker_dispatch_is_stale(
            broker_meta,
            task,
            stale_after_seconds=stale_broker_after_seconds,
        ):
            skipped += 1
            continue
        dispatch = queue.enqueue(task)
        record_dispatch = getattr(repository, "record_broker_dispatch", None)
        if callable(record_dispatch):
            record_dispatch(str(task["id"]), broker_task_id=dispatch.broker_task_id, queue_name=dispatch.queue)
        dispatched += 1
    logger.info(
        "async_runtime.reconcile",
        extra={"scanned": scanned, "dispatched": dispatched, "skipped": skipped},
    )
    return {"enabled": True, "scanned": scanned, "dispatched": dispatched, "skipped": skipped}


def _broker_dispatch_is_stale(broker_meta: dict[str, Any], task: dict[str, Any], *, stale_after_seconds: int) -> bool:
    if stale_after_seconds <= 0:
        return False
    status = str(task.get("status") or "")
    if status not in {TASK_PENDING, TASK_RETRYING}:
        return False
    dispatched_at = str(broker_meta.get("dispatched_at") or task.get("updated_at") or task.get("created_at") or "")
    try:
        dispatched = datetime.fromisoformat(dispatched_at)
    except ValueError:
        return True
    if dispatched.tzinfo is None:
        return datetime.now() - dispatched >= timedelta(seconds=stale_after_seconds)
    return datetime.now(timezone.utc) - dispatched >= timedelta(seconds=stale_after_seconds)


def _processing_lease_is_stale(task: dict[str, Any], *, stale_after_seconds: int) -> bool:
    lease_expires_at = str(task.get("lease_expires_at") or "")
    if not lease_expires_at:
        return False
    return _timestamp_is_stale(lease_expires_at, stale_after_seconds=max(0, int(stale_after_seconds)))


def _timestamp_is_stale(timestamp: str, *, stale_after_seconds: int) -> bool:
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        return datetime.now() - parsed >= timedelta(seconds=stale_after_seconds)
    aware_seconds = datetime.now(timezone.utc) - parsed
    local_seconds = datetime.now() - parsed.replace(tzinfo=None)
    if abs(aware_seconds.total_seconds() - local_seconds.total_seconds()) >= 3600:
        return local_seconds >= timedelta(seconds=stale_after_seconds)
    return aware_seconds >= timedelta(seconds=stale_after_seconds)
