import unittest
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from app.models.knowledge_base import KnowledgeBaseScope
from app.services.async_runtime.config import AsyncRuntimeConfig
from app.services.async_runtime.diagnostics import async_runtime_diagnostics
from app.services.async_runtime.queue import CeleryProcessingQueue, DisabledProcessingQueue
from app.services.async_runtime.reconciliation import reconcile_processing_queue
from app.services.async_runtime.task_envelope import ProcessingTaskEnvelope
from app.services.async_runtime.task_routes import (
    CORE_QUEUE,
    ENRICHMENT_QUEUE,
    MAINTENANCE_QUEUE,
    POSTPROCESS_QUEUE,
    SHARED_QUEUE,
    WIKI_QUEUE,
    route_for_task_type,
)


class AsyncRuntimeTests(unittest.TestCase):
    def test_config_does_not_default_to_hardcoded_redis_url(self):
        config = AsyncRuntimeConfig.from_settings()

        self.assertEqual("", config.broker_url)

    def test_config_normalizes_worker_pool_settings(self):
        config = AsyncRuntimeConfig.from_settings(
            {
                "enabled": "true",
                "mode": "celery",
                "broker_url": "redis://localhost:6379/1",
                "core_concurrency": "0",
                "wiki": {"queue": "wiki-priority", "concurrency": "9"},
            }
        )

        self.assertTrue(config.celery_enabled)
        self.assertEqual("redis://localhost:6379/1", config.broker_url)
        self.assertEqual(1, config.core.concurrency)
        self.assertEqual("wiki-priority", config.wiki.queue)
        self.assertEqual(9, config.wiki.concurrency)

    def test_task_routes_match_weknora_style_pools(self):
        self.assertEqual(CORE_QUEUE, route_for_task_type("document.process"))
        self.assertEqual(CORE_QUEUE, route_for_task_type("upload_file.process"))
        self.assertEqual(POSTPROCESS_QUEUE, route_for_task_type("knowledge.post_process"))
        self.assertEqual(ENRICHMENT_QUEUE, route_for_task_type("summary.generation"))
        self.assertEqual(ENRICHMENT_QUEUE, route_for_task_type("image.multimodal"))
        self.assertEqual(WIKI_QUEUE, route_for_task_type("wiki.ingest"))
        self.assertEqual(WIKI_QUEUE, route_for_task_type("wiki.finalize"))
        self.assertEqual(MAINTENANCE_QUEUE, route_for_task_type("maintenance.reconcile"))
        self.assertEqual(SHARED_QUEUE, route_for_task_type("unknown.task"))

    def test_processing_task_envelope_serializes_scope_and_payload(self):
        envelope = ProcessingTaskEnvelope.from_task(
            {
                "id": "task-1",
                "task_type": "wiki.ingest",
                "workspace_id": "workspace-1",
                "knowledge_base_id": "kb-1",
                "document_id": "doc-1",
                "payload": {"schema_version": 1},
                "payload_schema_version": 2,
                "idempotency_key": "wiki:doc-1:rev-1",
            }
        )

        encoded = envelope.to_dict()
        self.assertEqual("task-1", encoded["task_id"])
        self.assertEqual("wiki", encoded["queue"])
        self.assertEqual({"schema_version": 1}, encoded["payload"])
        self.assertEqual(KnowledgeBaseScope("workspace-1", ("kb-1",)), envelope.scope)

    def test_disabled_queue_is_noop(self):
        result = DisabledProcessingQueue().enqueue({"id": "task-1", "task_type": "document.process"})

        self.assertFalse(result.dispatched)
        self.assertEqual("core", result.queue)
        self.assertEqual("disabled", result.mode)

    def test_celery_queue_dispatches_with_stable_task_id_and_queue(self):
        sent = {}

        class FakeCelery:
            def send_task(self, name, **kwargs):
                sent.update({"name": name, **kwargs})
                return SimpleNamespace(id=f"broker-{kwargs['task_id']}")

        config = AsyncRuntimeConfig.from_settings({"enabled": True, "mode": "celery"})
        result = CeleryProcessingQueue(FakeCelery(), config).enqueue(
            {
                "id": "task-1",
                "task_type": "wiki.finalize",
                "workspace_id": "workspace-1",
                "knowledge_base_id": "kb-1",
            }
        )

        self.assertTrue(result.dispatched)
        self.assertEqual("wiki", result.queue)
        self.assertEqual("task-1", sent["task_id"])
        self.assertNotIn("countdown", sent)
        self.assertEqual("app.workers.processing_tasks.process_processing_task", sent["name"])
        self.assertEqual("wiki.finalize", sent["args"][0]["task_type"])

    def test_celery_queue_keeps_countdown_for_future_retry(self):
        sent = {}

        class FakeCelery:
            def send_task(self, name, **kwargs):
                sent.update({"name": name, **kwargs})
                return SimpleNamespace(id=kwargs["task_id"])

        config = AsyncRuntimeConfig.from_settings({"enabled": True, "mode": "celery"})
        CeleryProcessingQueue(FakeCelery(), config).enqueue(
            {
                "id": "task-1",
                "task_type": "wiki.finalize",
                "workspace_id": "workspace-1",
                "knowledge_base_id": "kb-1",
                "next_run_at": "2999-01-01T00:00:00+00:00",
            }
        )

        self.assertGreater(sent["countdown"], 0)

    def test_celery_queue_treats_local_time_with_utc_marker_as_due(self):
        sent = {}

        class FakeCelery:
            def send_task(self, name, **kwargs):
                sent.update({"name": name, **kwargs})
                return SimpleNamespace(id=kwargs["task_id"])

        config = AsyncRuntimeConfig.from_settings({"enabled": True, "mode": "celery"})
        CeleryProcessingQueue(FakeCelery(), config).enqueue(
            {
                "id": "task-1",
                "task_type": "upload_file.process",
                "workspace_id": "workspace-1",
                "knowledge_base_id": "kb-1",
                "next_run_at": datetime.now().isoformat(timespec="seconds") + "+00:00",
            }
        )

        self.assertNotIn("countdown", sent)

    def test_celery_queue_uses_configured_physical_queue_name(self):
        sent = {}

        class FakeCelery:
            def send_task(self, name, **kwargs):
                sent["queue"] = kwargs["queue"]
                return SimpleNamespace(id=kwargs["task_id"])

        config = AsyncRuntimeConfig.from_settings(
            {"enabled": True, "mode": "celery", "wiki": {"queue": "wiki-priority", "concurrency": 2}}
        )
        result = CeleryProcessingQueue(FakeCelery(), config).enqueue(
            {"id": "task-1", "task_type": "wiki.ingest", "workspace_id": "workspace-1", "knowledge_base_id": "kb-1"}
        )

        self.assertEqual("wiki-priority", sent["queue"])
        self.assertEqual("wiki-priority", result.queue)

    def test_diagnostics_reports_routes_without_broker_connection(self):
        config = AsyncRuntimeConfig.from_settings({"enabled": True, "mode": "celery"})
        diagnostics = async_runtime_diagnostics(config, {"ok": True})

        self.assertTrue(diagnostics["celery_enabled"])
        self.assertEqual("core", diagnostics["queues"]["core"])
        self.assertEqual("wiki", diagnostics["routes"]["wiki.ingest"])
        self.assertEqual({"ok": True}, diagnostics["health"])

    def test_reconciliation_dispatches_non_terminal_tasks_without_broker_metadata_or_stale_broker(self):
        class FakeRepository:
            def __init__(self):
                self.recorded = []

            def list_tasks(self, statuses, task_types=None):
                return [
                    {
                        "id": "task-1",
                        "task_type": "wiki.ingest",
                        "workspace_id": "workspace-1",
                        "knowledge_base_id": "kb-1",
                        "status": "pending",
                        "payload": {},
                    },
                    {
                        "id": "task-2",
                        "task_type": "wiki.ingest",
                        "workspace_id": "workspace-1",
                        "knowledge_base_id": "kb-1",
                        "status": "retrying",
                        "payload": {"_async_runtime": {"broker_task_id": "broker-2"}},
                        "updated_at": "2020-01-01T00:00:00+00:00",
                    },
                    {
                        "id": "task-3",
                        "task_type": "wiki.ingest",
                        "workspace_id": "workspace-1",
                        "knowledge_base_id": "kb-1",
                        "status": "pending",
                        "payload": {"_async_runtime": {"broker_task_id": "broker-3", "dispatched_at": "2999-01-01T00:00:00+00:00"}},
                    },
                ]

            def record_broker_dispatch(self, task_id, broker_task_id, queue_name):
                self.recorded.append((task_id, broker_task_id, queue_name))

        class FakeQueue:
            enabled = True

            def enqueue(self, task):
                return SimpleNamespace(broker_task_id=f"broker-{task['id']}", queue_name="wiki", queue="wiki")

        repo = FakeRepository()
        result = reconcile_processing_queue(repo, FakeQueue())

        self.assertEqual({"enabled": True, "scanned": 3, "dispatched": 2, "skipped": 1}, result)
        self.assertEqual([("task-1", "broker-task-1", "wiki"), ("task-2", "broker-task-2", "wiki")], repo.recorded)

    def test_reconciliation_dispatches_expired_processing_lease(self):
        class FakeRepository:
            def __init__(self):
                self.recorded = []

            def list_tasks(self, statuses, task_types=None):
                return [
                    {
                        "id": "task-stale",
                        "task_type": "wiki.ingest",
                        "workspace_id": "workspace-1",
                        "knowledge_base_id": "kb-1",
                        "status": "processing",
                        "lease_expires_at": "2020-01-01T00:00:00+00:00",
                        "payload": {"_async_runtime": {"broker_task_id": "old-broker"}},
                    },
                    {
                        "id": "task-fresh",
                        "task_type": "wiki.ingest",
                        "workspace_id": "workspace-1",
                        "knowledge_base_id": "kb-1",
                        "status": "processing",
                        "lease_expires_at": "2999-01-01T00:00:00+00:00",
                        "payload": {},
                    },
                ]

            def record_broker_dispatch(self, task_id, broker_task_id, queue_name):
                self.recorded.append((task_id, broker_task_id, queue_name))

        class FakeQueue:
            enabled = True

            def enqueue(self, task):
                return SimpleNamespace(broker_task_id=f"broker-{task['id']}", queue_name="wiki", queue="wiki")

        repo = FakeRepository()
        result = reconcile_processing_queue(repo, FakeQueue())

        self.assertEqual({"enabled": True, "scanned": 2, "dispatched": 1, "skipped": 1}, result)
        self.assertEqual([("task-stale", "broker-task-stale", "wiki")], repo.recorded)

    def test_celery_task_retries_when_claim_misses_still_pending_task(self):
        with patch.dict(sys.modules, {"app.workers.celery_app": SimpleNamespace(celery_app=SimpleNamespace(task=lambda *args, **kwargs: (lambda func: func)))}):
            sys.modules.pop("app.workers.processing_tasks", None)
            from app.workers.processing_tasks import _handle_unclaimed_task

        class FakeRepository:
            def claim_task(self, *args, **kwargs):
                return None

            def get_task(self, task_id):
                return {
                    "id": task_id,
                    "status": "pending",
                    "next_run_at": "2000-01-01T00:00:00+00:00",
                    "max_attempts": 3,
                }

        class FakeWorker:
            config = SimpleNamespace(lease_timeout_seconds=60)
            repository = FakeRepository()

        class RetryRaised(Exception):
            pass

        class FakeTaskSelf:
            def retry(self, **kwargs):
                raise RetryRaised(kwargs)

        envelope = ProcessingTaskEnvelope.from_task(
            {
                "id": "task-early",
                "task_type": "embedding.index",
                "workspace_id": "workspace-1",
                "knowledge_base_id": "kb-1",
                "payload": {},
            }
        )

        with self.assertRaises(RetryRaised) as raised:
            _handle_unclaimed_task(FakeTaskSelf(), FakeWorker(), envelope)

        self.assertEqual(1, raised.exception.args[0]["countdown"])
        self.assertEqual(3, raised.exception.args[0]["max_retries"])

    def test_celery_task_skips_terminal_unclaimed_task(self):
        with patch.dict(sys.modules, {"app.workers.celery_app": SimpleNamespace(celery_app=SimpleNamespace(task=lambda *args, **kwargs: (lambda func: func)))}):
            sys.modules.pop("app.workers.processing_tasks", None)
            from app.workers.processing_tasks import _handle_unclaimed_task

        class FakeRepository:
            def get_task(self, task_id):
                return {"id": task_id, "status": "completed"}

        class FakeWorker:
            repository = FakeRepository()

        envelope = ProcessingTaskEnvelope.from_task(
            {
                "id": "task-done",
                "task_type": "embedding.index",
                "workspace_id": "workspace-1",
                "knowledge_base_id": "kb-1",
                "payload": {},
            }
        )

        result = _handle_unclaimed_task(SimpleNamespace(), FakeWorker(), envelope)

        self.assertEqual({"task_id": "task-done", "status": "skipped"}, result)


if __name__ == "__main__":
    unittest.main()
