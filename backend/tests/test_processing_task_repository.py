import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.models.knowledge_base import KnowledgeBaseScope
from app.services.processing.processing_task_repository import (
    ProcessingTaskRepository,
    TASK_CANCELED,
    TASK_COMPLETED,
    TASK_DEAD_LETTERED,
    TASK_PENDING,
    TASK_PROCESSING,
    TASK_RETRYING,
    deterministic_task_id,
)


class ProcessingTaskRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.scope = KnowledgeBaseScope("default-workspace", ("default-knowledge-base",))

    def _repo(self, tmp: str) -> ProcessingTaskRepository:
        return ProcessingTaskRepository(Path(tmp) / "metadata.sqlite3")

    def test_create_task_uses_deterministic_id_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            payload = {"upload_path": "uploads/a.txt", "stage": "parse"}

            first = repo.create_task(
                "document.parse",
                self.scope,
                payload=payload,
                document_id="doc-1",
                upload_batch_id="batch-1",
                upload_file_id="file-1",
            )
            second = repo.create_task(
                "document.parse",
                self.scope,
                payload=dict(reversed(payload.items())),
                document_id="doc-1",
                upload_batch_id="batch-1",
                upload_file_id="file-1",
            )

            self.assertEqual(first["id"], second["id"])
            self.assertEqual(
                deterministic_task_id(
                    "document.parse",
                    self.scope,
                    document_id="doc-1",
                    upload_batch_id="batch-1",
                    upload_file_id="file-1",
                    payload=payload,
                ),
                first["id"],
            )
            self.assertEqual(TASK_PENDING, first["status"])
            self.assertEqual(payload, first["payload"])
            self.assertEqual(1, len(repo.list_tasks(self.scope)))

    def test_create_task_deduplicates_by_nonempty_idempotency_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            first = repo.create_task(
                "wiki.ingest",
                self.scope,
                task_id="task-first",
                payload={"generation_run_id": "run-1"},
                idempotency_key="wiki-ingest:kb:doc:rev",
            )
            duplicate = repo.create_task(
                "wiki.ingest",
                self.scope,
                task_id="task-duplicate",
                payload={"generation_run_id": "run-2"},
                idempotency_key="wiki-ingest:kb:doc:rev",
            )

            self.assertEqual(first["id"], duplicate["id"])
            self.assertEqual("run-1", duplicate["payload"]["generation_run_id"])
            self.assertEqual(1, len(repo.list_tasks(self.scope)))

    def test_claim_orders_runnable_tasks_and_refreshes_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            later = datetime.now() + timedelta(minutes=10)
            first = repo.create_task("document.parse", self.scope, task_id="task-first", document_id="doc-1")
            repo.create_task("document.parse", self.scope, task_id="task-later", document_id="doc-2", run_after=later)

            claimed = repo.claim_next("worker-1", lease_seconds=30)
            refreshed = repo.heartbeat(claimed["id"], "worker-1", lease_seconds=90)

            self.assertEqual(first["id"], claimed["id"])
            self.assertEqual(TASK_PROCESSING, claimed["status"])
            self.assertEqual(1, claimed["attempt"])
            self.assertEqual("worker-1", claimed["lease_owner"])
            self.assertGreater(refreshed["lease_expires_at"], claimed["lease_expires_at"])
            self.assertIsNone(repo.claim_next("worker-1"))

    def test_stale_processing_lease_can_be_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            repo.create_task("document.parse", self.scope, task_id="task-stale", document_id="doc-1")
            stale = repo.claim_next("worker-old", lease_seconds=0)

            reclaimed = repo.claim_next("worker-new", lease_seconds=30)

            self.assertEqual(stale["id"], reclaimed["id"])
            self.assertEqual(2, reclaimed["attempt"])
            self.assertEqual("worker-new", reclaimed["lease_owner"])

    def test_claim_task_claims_only_named_runnable_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            repo.create_task("document.parse", self.scope, task_id="task-target", document_id="doc-1")
            repo.create_task("document.parse", self.scope, task_id="task-other", document_id="doc-2")

            claimed = repo.claim_task("task-target", worker_id="celery-worker", lease_seconds=30)
            skipped = repo.claim_task("task-target", worker_id="second-worker", lease_seconds=30)

            self.assertEqual("task-target", claimed["id"])
            self.assertEqual(TASK_PROCESSING, claimed["status"])
            self.assertEqual("celery-worker", claimed["lease_owner"])
            self.assertIsNone(skipped)
            self.assertEqual(TASK_PENDING, repo.get_task("task-other")["status"])

    def test_record_broker_dispatch_stores_metadata_in_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            task = repo.create_task("wiki.ingest", self.scope, task_id="task-dispatch", payload={"schema_version": 1})

            updated = repo.record_broker_dispatch(task["id"], broker_task_id="broker-1", queue_name="wiki")

            self.assertEqual(1, updated["payload"]["schema_version"])
            self.assertEqual("broker-1", updated["payload"]["_async_runtime"]["broker_task_id"])
            self.assertEqual("wiki", updated["payload"]["_async_runtime"]["queue_name"])

    def test_runnable_finalize_payloads_are_coalesced_per_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            first = repo.create_task(
                "wiki.finalize",
                self.scope,
                task_id="finalize-1",
                payload={
                    "schema_version": 1,
                    "generation_run_id": "run-1",
                    "generation_run_ids": ["run-1"],
                    "document_ids": ["doc-1"],
                    "affected_slugs": ["onu"],
                },
            )
            merged = repo.merge_runnable_task_payload(
                self.scope,
                "wiki.finalize",
                {"generation_run_id": "run-2", "generation_run_ids": ["run-2"], "document_ids": ["doc-2"], "affected_slugs": ["gpon", "onu"]},
            )

            self.assertEqual(first["id"], merged["id"])
            self.assertEqual(["doc-1", "doc-2"], merged["payload"]["document_ids"])
            self.assertEqual(["onu", "gpon"], merged["payload"]["affected_slugs"])
            self.assertEqual(["run-1", "run-2"], merged["payload"]["generation_run_ids"])

    def test_retry_complete_cancel_and_dead_letter_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            repo.create_task("document.parse", self.scope, task_id="task-retry", document_id="doc-1", trace_id="trace-1")
            claimed = repo.claim_next("worker-1")
            retried = repo.retry(
                claimed["id"],
                error_code="PARSER_TEMPORARY",
                error_message=" temporary\nprovider failure ",
                delay_seconds=0,
                worker_id="worker-1",
            )
            claimed_again = repo.claim_next("worker-2")
            completed = repo.complete(claimed_again["id"], worker_id="worker-2")

            repo.create_task("document.parse", self.scope, task_id="task-cancel", document_id="doc-2")
            canceled_count = repo.cancel_for_document(self.scope, "doc-2", reason="user deleted")
            canceled = repo.get_task("task-cancel")

            repo.create_task("document.parse", self.scope, task_id="task-dead", document_id="doc-3", trace_id="trace-3")
            dead_claimed = repo.claim_next("worker-3")
            dead = repo.dead_letter(
                dead_claimed["id"],
                error_code="TASK_TIMEOUT",
                error_message="retry budget exhausted",
                worker_id="worker-3",
            )
            letters = repo.list_dead_letters(self.scope)

            self.assertEqual(TASK_RETRYING, retried["status"])
            self.assertEqual("PARSER_TEMPORARY", retried["last_error_code"])
            self.assertEqual(TASK_COMPLETED, completed["status"])
            self.assertEqual(1, canceled_count)
            self.assertEqual(TASK_CANCELED, canceled["status"])
            self.assertEqual(TASK_DEAD_LETTERED, dead["status"])
            self.assertEqual(1, len(letters))
            self.assertEqual("task-dead", letters[0]["task_id"])
            self.assertEqual("TASK_TIMEOUT", letters[0]["error_code"])
            self.assertEqual("trace-3", letters[0]["trace_id"])


if __name__ == "__main__":
    unittest.main()
