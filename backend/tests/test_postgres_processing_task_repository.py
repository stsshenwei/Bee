import unittest
from datetime import datetime, timezone

from app.models.knowledge_base import KnowledgeBaseScope
from app.services.processing.postgres_processing_task_repository import (
    PostgresProcessingTaskRepository,
    deterministic_task_id,
)


class _FakeDatabase:
    settings = type("Settings", (), {"schema": "rag"})()


class RecordingProcessingRepository(PostgresProcessingTaskRepository):
    def __init__(self):
        super().__init__(_FakeDatabase(), schema="rag", validate_schema=False)
        self.fetch_one_in_conn_calls = []
        self.execute_in_conn_calls = []
        self.fetch_all_calls = []
        self.task_row = {
            "id": "task-1",
            "task_type": "document.parse",
            "workspace_id": "workspace-1",
            "knowledge_base_id": "kb-1",
            "document_id": "doc-1",
            "upload_batch_id": "",
            "upload_file_id": "",
            "status": "pending",
            "payload_json": {"doc_id": "doc-1"},
            "attempt": 0,
            "max_attempts": 3,
            "next_run_at": "2026-08-13T00:00:00",
            "lease_owner": "",
            "lease_expires_at": None,
            "last_error_code": "",
            "last_error_message": "",
            "trace_id": "trace-1",
            "created_at": "2026-08-13T00:00:00",
            "updated_at": "2026-08-13T00:00:00",
            "started_at": None,
            "finished_at": None,
            "payload_schema_version": 1,
            "idempotency_key": "",
            "source_revision": "",
            "parent_trace_id": "",
        }

    def _txn(self):
        return self

    @property
    def database(self):
        return type("Db", (), {"settings": _FakeDatabase.settings, "transaction": self._txn, "connection": self._txn})()

    @database.setter
    def database(self, value):
        self._database = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def _fetch_one_in_conn(self, conn, sql, params):
        self.fetch_one_in_conn_calls.append((sql, params))
        if "select *" in sql.lower() and "document_processing_task" in sql:
            row = dict(self.task_row)
            if "where id = %s" in sql.lower() and params[0] != "task-1":
                row["id"] = params[0]
            if any("attempt = attempt + 1" in item[0] for item in self.execute_in_conn_calls):
                row["attempt"] = 1
                row["status"] = "processing"
                row["lease_owner"] = "worker-1"
            return row
        return None

    def _execute_in_conn(self, conn, sql, params):
        self.execute_in_conn_calls.append((sql, params))
        return 1

    def _fetch_all(self, sql, params):
        self.fetch_all_calls.append((sql, params))
        return [self.task_row]

    def _execute(self, sql, params):
        self.execute_in_conn_calls.append((sql, params))
        return 1


class PostgresProcessingTaskRepositoryTests(unittest.TestCase):
    def test_deterministic_id_matches_scope_and_payload_contract(self):
        scope = KnowledgeBaseScope("workspace-1", ("kb-1",))

        first = deterministic_task_id("document.parse", scope, document_id="doc-1", payload={"b": 2, "a": 1})
        second = deterministic_task_id("document.parse", scope, document_id="doc-1", payload={"a": 1, "b": 2})

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("processing-"))

    def test_claim_next_uses_row_locking_and_attempt_audit_insert(self):
        repo = RecordingProcessingRepository()

        claimed = repo.claim_next("worker-1", task_types={"document.parse"}, now="2026-08-13T00:00:00")

        claim_sql, claim_params = repo.fetch_one_in_conn_calls[0]
        self.assertEqual("task-1", claimed["id"])
        self.assertEqual(1, claimed["attempt"])
        self.assertIn("for update skip locked", claim_sql.lower())
        self.assertIn("task_type in (%s)", claim_sql)
        self.assertIn("worker-1", repo.execute_in_conn_calls[0][1])
        self.assertTrue(any("document_processing_task_attempt" in sql for sql, _ in repo.execute_in_conn_calls))
        self.assertIn("document.parse", claim_params)

    def test_claim_task_uses_named_row_locking(self):
        repo = RecordingProcessingRepository()

        claimed = repo.claim_task("task-1", worker_id="celery-worker", lease_seconds=30, now="2026-08-13T00:00:00")

        claim_sql, claim_params = repo.fetch_one_in_conn_calls[0]
        self.assertEqual("task-1", claimed["id"])
        self.assertIn("where id = %s", claim_sql.lower())
        self.assertIn("for update skip locked", claim_sql.lower())
        self.assertEqual("task-1", claim_params[0])
        self.assertIn("celery-worker", repo.execute_in_conn_calls[0][1])

    def test_record_broker_dispatch_updates_payload_metadata(self):
        repo = RecordingProcessingRepository()

        repo.record_broker_dispatch("task-1", broker_task_id="broker-1", queue_name="core")

        update_sql, update_params = repo.execute_in_conn_calls[-1]
        self.assertIn("payload_json = %s::jsonb", update_sql)
        self.assertIn("\"broker_task_id\": \"broker-1\"", update_params[0])
        self.assertIn("\"queue_name\": \"core\"", update_params[0])

    def test_create_task_casts_postgres_insert_parameters(self):
        repo = RecordingProcessingRepository()
        scope = KnowledgeBaseScope("workspace-1", ("kb-1",))

        created = repo.create_task(
            "wiki.ingest",
            scope,
            payload={"schema_version": 1, "document_ids": ["doc-1"]},
            task_id="wiki-task-1",
            document_id="doc-1",
            upload_batch_id="wiki-run-1",
            trace_id="trace-1",
            idempotency_key="wiki-ingest:workspace-1:kb-1:doc-1:rev-1",
            source_revision="rev-1",
        )

        insert_sql, insert_params = next(
            (sql, params)
            for sql, params in repo.execute_in_conn_calls
            if "insert into" in sql.lower() and "document_processing_task" in sql
        )
        self.assertEqual("wiki-task-1", created["id"])
        self.assertIn("%s::text, %s::text, %s::text, %s::text, %s::text, %s::text, %s::text", insert_sql)
        self.assertIn("%s::integer, %s::timestamptz", insert_sql)
        self.assertEqual("wiki-run-1", insert_params[5])
        self.assertEqual("trace-1", insert_params[10])

    def test_dead_letter_writes_jsonb_payload_snapshot(self):
        repo = RecordingProcessingRepository()

        repo.dead_letter("task-1", error_code="TASK_TIMEOUT", error_message="retry budget exhausted")

        insert_sql, insert_params = next(
            (sql, params)
            for sql, params in repo.execute_in_conn_calls
            if "document_processing_dead_letter" in sql and "insert into" in sql.lower()
        )
        self.assertIn("%s::jsonb", insert_sql)
        self.assertIn("{\"doc_id\": \"doc-1\"}", insert_params)

    def test_cancel_for_upload_batch_preserves_terminal_tasks(self):
        repo = RecordingProcessingRepository()
        scope = KnowledgeBaseScope("workspace-1", ("kb-1",))

        canceled = repo.cancel_for_upload_batch(scope, "batch-1", reason="user canceled")

        sql, params = repo.execute_in_conn_calls[-1]
        self.assertEqual(1, canceled)
        self.assertIn("status not in ('completed', 'failed', 'canceled', 'dead_lettered')", sql)
        self.assertIn("upload_batch_id = %s", sql)
        self.assertEqual("batch-1", params[-1])

    def test_decodes_postgres_datetime_fields_for_api_responses(self):
        repo = RecordingProcessingRepository()
        now = datetime(2026, 8, 25, 1, 2, 3, tzinfo=timezone.utc)
        repo.task_row.update(
            {
                "next_run_at": now,
                "created_at": now,
                "updated_at": now,
                "started_at": now,
                "finished_at": now,
            }
        )

        task = repo.get_task("task-1")

        self.assertEqual("2026-08-25T01:02:03+00:00", task["next_run_at"])
        self.assertEqual("2026-08-25T01:02:03+00:00", task["created_at"])
        self.assertEqual("2026-08-25T01:02:03+00:00", task["updated_at"])
        self.assertEqual("2026-08-25T01:02:03+00:00", task["started_at"])
        self.assertEqual("2026-08-25T01:02:03+00:00", task["finished_at"])


if __name__ == "__main__":
    unittest.main()
