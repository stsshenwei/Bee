import unittest

from app.models.evaluation import EvalResultRecord
from app.models.kg_models import EntityMention
from app.services.evaluation.postgres_evaluation_repository import PostgresEvaluationRepository
from app.services.kg.postgres_kg_repository import PostgresKGRepository
from app.services.knowledge.postgres_audit_repository import PostgresKnowledgeAuditRepository
from app.services.memory.postgres_memory_repository import PostgresMemoryRepository


class _FakeDatabase:
    settings = type("Settings", (), {"schema": "rag"})()


class _Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeCursor:
    def __init__(self, raw):
        self.raw = raw
        self.rowcount = 1

    def execute(self, sql, params=()):
        self.raw.calls.append((sql, tuple(params or ())))

    def executemany(self, sql, rows):
        self.raw.calls.append((sql, tuple(rows)))

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _RawConnection:
    def __init__(self):
        self.calls = []

    def cursor(self):
        return _FakeCursor(self)


class _RecordingDatabase(_FakeDatabase):
    def __init__(self):
        self.raw = _RawConnection()

    def transaction(self):
        return _Context(self.raw)

    def connection(self):
        return _Context(self.raw)


class RecordingAuditRepository(PostgresKnowledgeAuditRepository):
    def __init__(self):
        super().__init__(_FakeDatabase(), validate_schema=False)
        self.execute_calls = []

    def _execute(self, sql, params):
        self.execute_calls.append((sql, params))
        return 1


class RecordingMemoryRepository(PostgresMemoryRepository):
    def __init__(self):
        super().__init__(_FakeDatabase(), validate_schema=False)
        self.execute_calls = []
        self.fetch_calls = []
        self.memory_row = {
            "id": "mem_1",
            "scope": "user",
            "type": "preference",
            "normalized_key": "language",
            "memory_key": "language",
            "content": "User prefers concise Chinese answers.",
            "confidence": 0.95,
            "status": "active",
            "source_conversation_id": "conv-2",
            "source_message_id": "msg-2",
            "metadata_json": {},
            "created_at": "2026-08-13T00:00:00",
            "updated_at": "2026-08-13T00:00:01",
        }

    def _fetch_one(self, sql, params):
        self.fetch_calls.append((sql, params))
        if "where id = %s" in sql:
            return self.memory_row
        return None

    def _execute(self, sql, params):
        self.execute_calls.append((sql, params))
        return 1


class RecordingEvaluationRepository(PostgresEvaluationRepository):
    def __init__(self):
        super().__init__(_FakeDatabase(), validate_schema=False)
        self.execute_calls = []
        self.result_row = {
            "id": "result-1",
            "run_id": "run-1",
            "case_id": "case-1",
            "status": "passed",
            "question": "Question?",
            "query_type": "fact",
            "tags": ["smoke"],
            "case_snapshot": {},
            "answer": "Answer",
            "response_snapshot": {},
            "evidence_snapshot": {},
            "metric_scores": {"faithfulness": 1.0},
            "latency_ms": 12.0,
            "error_message": "",
            "created_at": "2026-08-13T00:00:00",
            "knowledge_base_ids_json": ["kb-1"],
        }

    def _execute(self, sql, params):
        self.execute_calls.append((sql, params))
        return 1

    def _fetch_one(self, sql, params):
        return self.result_row


class PostgresDomainRepositoryTests(unittest.TestCase):
    def test_audit_query_writes_jsonb_scope_fields(self):
        repo = RecordingAuditRepository()
        scope = type("Scope", (), {"workspace_id": "workspace-1", "selected_knowledge_base_ids": ("kb-1", "kb-2")})()

        query_id = repo.start_query("How many ONUs?", scope, "fact")
        repo.finish_query(query_id, status="completed", citation_chunk_ids=["chunk-1", "chunk-1"])

        insert_sql, insert_params = repo.execute_calls[0]
        update_sql, update_params = repo.execute_calls[1]
        self.assertIn('"rag"."query_log"', insert_sql)
        self.assertIn("%s::jsonb", insert_sql)
        self.assertEqual("[\"kb-1\", \"kb-2\"]", insert_params[2])
        self.assertIn("citation_chunk_ids_json = %s::jsonb", update_sql)
        self.assertIn("[\"chunk-1\"]", update_params)

    def test_memory_upsert_preserves_existing_contract_columns(self):
        repo = RecordingMemoryRepository()

        memory = repo.upsert_memory("user", "preference", "language", "User prefers Chinese.", 0.9)

        sql, params = repo.execute_calls[-1]
        self.assertEqual("mem_1", memory["id"])
        self.assertIn("scope, type, normalized_key, memory_key", sql)
        self.assertIn("on conflict(scope, normalized_key, status)", sql)
        self.assertIn('"rag"."memory"', sql)
        self.assertEqual(("user", "language"), repo.fetch_calls[0][1])
        self.assertEqual("language", params[3])

    def test_kg_mentions_batch_upsert_jsonb_metadata(self):
        database = _RecordingDatabase()
        repo = PostgresKGRepository(database, validate_schema=False)
        mention = EntityMention(
            id="mention-1",
            entity_id="entity-1",
            entity_type="Concept",
            entity_name="ONU",
            doc_id="doc-1",
            chunk_id="chunk-1",
            parent_id="chunk-1",
            page_start=1,
            page_end=1,
            mention_text="ONU",
            confidence=0.9,
            metadata={"workspace_id": "workspace-1", "knowledge_base_id": "kb-1"},
        )

        repo.insert_entity_mentions([mention])

        sql, rows = database.raw.calls[-1]
        self.assertIn('"rag"."entity_mention"', sql)
        self.assertIn("%s::jsonb", sql)
        self.assertIn("on conflict(id) do update", sql)
        self.assertEqual("workspace-1", rows[0][1])
        self.assertIn('"knowledge_base_id": "kb-1"', rows[0][15])

    def test_evaluation_result_writes_jsonb_snapshots(self):
        repo = RecordingEvaluationRepository()
        result = EvalResultRecord(
            id="result-1",
            run_id="run-1",
            case_id="case-1",
            status="passed",
            question="Question?",
            query_type="fact",
            tags=["smoke"],
            case_snapshot={},
            answer="Answer",
            response_snapshot={},
            evidence_snapshot={},
            metric_scores={"faithfulness": 1.0},
            latency_ms=12.0,
            knowledge_base_ids=["kb-1"],
        )

        saved = repo.add_result(result)

        sql, params = repo.execute_calls[-1]
        self.assertEqual("result-1", saved["id"])
        self.assertIn('"rag"."eval_result"', sql)
        self.assertGreaterEqual(sql.count("::jsonb"), 6)
        self.assertIn("[\"smoke\"]", params)
        self.assertIn("{\"faithfulness\": 1.0}", params)


if __name__ == "__main__":
    unittest.main()
