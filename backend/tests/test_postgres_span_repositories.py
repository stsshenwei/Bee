import unittest
from datetime import datetime, timezone

from app.services.agent.postgres_agent_runtime_spans import PostgresAgentRuntimeSpanRepository
from app.services.processing.postgres_processing_span_repository import PostgresProcessingSpanRepository


class _FakeDatabase:
    settings = type("Settings", (), {"schema": "rag"})()

    def __init__(self):
        self.statements = []

    def transaction(self):
        return self

    def connection(self):
        return self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        self.rowcount = 1

    def fetchone(self):
        return {
            "id": 1,
            "knowledge_id": "doc-1",
            "attempt": 1,
            "span_id": "span-1",
            "parent_span_id": None,
            "name": "chunking",
            "kind": "stage",
            "status": "running",
            "input_json": {"api_key": "secret"},
            "output_json": {},
            "metadata_json": {},
            "error_code": "",
            "error_message": "",
            "error_detail": "",
            "started_at": "",
            "finished_at": "",
            "duration_ms": 0,
            "created_at": "",
            "updated_at": "",
        }

    def fetchall(self):
        return [{"span_id": "child-1"}, {"span_id": "child-2"}]


class RecordingProcessingSpanRepository(PostgresProcessingSpanRepository):
    def __init__(self):
        self.db = _FakeDatabase()
        super().__init__(self.db, schema="rag", validate_schema=False)


class PostgresSpanRepositoryTests(unittest.TestCase):
    def test_processing_span_insert_uses_jsonb_and_null_safe_conflict_lookup(self):
        repo = RecordingProcessingSpanRepository()

        span = repo.insert_span(
            knowledge_id="doc-1",
            attempt=1,
            span_id="span-1",
            parent_span_id=None,
            name="chunking",
            kind="stage",
            status="running",
            input={"api_key": "secret"},
        )

        insert_sql, insert_params = repo.db.statements[0]
        lookup_sql, _ = repo.db.statements[1]
        self.assertEqual("span-1", span["span_id"])
        self.assertIn("%s::jsonb", insert_sql)
        self.assertIn("on conflict", insert_sql.lower())
        self.assertIn("is not distinct from", lookup_sql.lower())
        self.assertEqual("secret", span["input_json"]["api_key"])
        self.assertTrue(any('"secret"' in str(param) for param in insert_params))

    def test_processing_span_descendant_cancel_uses_recursive_cte(self):
        repo = RecordingProcessingSpanRepository()

        canceled = repo.cancel_descendants("doc-1", 1, "parent-1", "fallback")

        select_sql, _ = repo.db.statements[0]
        update_sql, _ = repo.db.statements[1]
        self.assertEqual(1, canceled)
        self.assertIn("with recursive descendants", select_sql.lower())
        self.assertIn("span_id in (%s, %s)", update_sql)

    def test_processing_span_update_converts_empty_timestamp_to_null(self):
        repo = RecordingProcessingSpanRepository()

        repo.update_span("span-1", status="running", started_at="2026-08-23T15:00:00", finished_at="")

        update_sql, params = repo.db.statements[0]
        self.assertIn("finished_at = %s", update_sql)
        self.assertIn("started_at = coalesce(started_at, %s)", update_sql)
        self.assertIsNone(params[3])

    def test_processing_span_decode_converts_postgres_timestamps_to_strings(self):
        db = _FakeDatabase()
        started_at = datetime(2026, 8, 23, 15, 0, 0, tzinfo=timezone.utc)
        db.fetchone = lambda: {
            **_FakeDatabase().fetchone(),
            "started_at": started_at,
            "finished_at": None,
            "created_at": started_at,
            "updated_at": started_at,
        }
        repo = PostgresProcessingSpanRepository(db, schema="rag", validate_schema=False)

        span = repo.get_stage("doc-1", 1, "chunking")

        self.assertEqual("2026-08-23T15:00:00+00:00", span["started_at"])
        self.assertEqual("2026-08-23T15:00:00+00:00", span["created_at"])

    def test_agent_runtime_spans_write_jsonb_start_and_finish(self):
        db = _FakeDatabase()
        repo = PostgresAgentRuntimeSpanRepository(db, schema="rag", validate_schema=False)

        span = repo.start_span(run_id="run-1", name="round", kind="llm", input={"prompt": "hello"})
        repo.finish_span(span, output={"answer": "world"})

        start_sql, start_params = db.statements[0]
        finish_sql, finish_params = db.statements[1]
        self.assertIsNotNone(span)
        self.assertIn("agent_runtime_spans", start_sql)
        self.assertIn("%s::jsonb", start_sql)
        self.assertIn("%s::jsonb", finish_sql)
        self.assertIn('{"prompt": "hello"}', start_params)
        self.assertIn('{"answer": "world"}', finish_params)


if __name__ == "__main__":
    unittest.main()
