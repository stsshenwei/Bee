import unittest
from datetime import datetime, timezone

from app.models.document_models import Chunk
from app.models.knowledge_base import KnowledgeBaseScope
from app.models.processing_config import PROCESSING_VERSION
from app.services.documents.postgres_document_repository import PostgresDocumentRepository


class _FakeDatabase:
    settings = type("Settings", (), {"schema": "rag"})()


class RecordingDocumentRepository(PostgresDocumentRepository):
    def __init__(self):
        super().__init__(_FakeDatabase(), schema="rag", validate_schema=False)
        self.fetch_one_calls = []
        self.fetch_all_calls = []
        self.execute_calls = []
        self.chunk_row = {
            "id": "chunk-1",
            "doc_id": "doc-1",
            "workspace_id": "workspace-1",
            "knowledge_base_id": "kb-1",
            "parent_id": "parent-1",
            "chunk_type": "child",
            "title_path": "CLI",
            "content": "ERR_CODE_42 use --timeout",
            "content_markdown": "ERR_CODE_42 use --timeout",
            "page_start": 1,
            "page_end": 1,
            "token_count": 4,
            "metadata_json": {"source": "manual.md"},
            "created_at": "2026-08-13T00:00:00",
        }
        self.document_row = {
            "id": "doc-1",
            "workspace_id": "workspace-1",
            "knowledge_base_id": "kb-1",
            "name": "manual.md",
            "file_type": "md",
            "storage_path": "manual.md",
            "parse_status": "parsed",
            "created_at": "2026-08-13T00:00:00",
            "updated_at": "2026-08-13T00:00:00",
            "metadata_json": {},
            "summary": "",
            "keywords_json": [],
            "suggested_questions_json": [],
            "summary_status": "none",
            "summary_error": "",
            "summary_model_ref": "",
            "summary_generated_at": None,
            "summary_version": 0,
            "summary_source_chunk_ids_json": [],
            "current_enrichment_task_id": None,
        }

    def _fetch_one(self, sql, params):
        self.fetch_one_calls.append((sql, params))
        if "from \"rag\".\"document_chunk\"" in sql:
            return self.chunk_row
        if "from \"rag\".\"document\"" in sql:
            return self.document_row
        return None

    def _fetch_all(self, sql, params):
        self.fetch_all_calls.append((sql, params))
        if "document_chunk" in sql:
            row = dict(self.chunk_row)
            row["keyword_score"] = 0.7
            return [row]
        return []

    def _execute(self, sql, params):
        self.execute_calls.append((sql, params))
        return 1


class PostgresDocumentRepositoryTests(unittest.TestCase):
    def test_chunk_lookup_uses_postgres_placeholders_and_scope_filters(self):
        repo = RecordingDocumentRepository()
        scope = KnowledgeBaseScope(
            workspace_id="workspace-1",
            selected_knowledge_base_ids=("kb-1", "kb-2"),
            document_ids=("doc-1",),
        )

        chunk = repo.get_chunk("chunk-1", scope)

        sql, params = repo.fetch_one_calls[-1]
        self.assertEqual("chunk-1", chunk["id"])
        self.assertIn("%s", sql)
        self.assertIn("knowledge_base_id in (%s, %s)", sql)
        self.assertIn("doc_id in (%s)", sql)
        self.assertEqual(("chunk-1", "workspace-1", "kb-1", "kb-2", "doc-1"), params)

    def test_keyword_search_uses_postgres_text_trigram_and_exact_fallback(self):
        repo = RecordingDocumentRepository()
        scope = KnowledgeBaseScope(workspace_id="workspace-1", selected_knowledge_base_ids=("kb-1",))

        hits = repo.search_keyword_chunks("ERR_CODE_42 --timeout", top_k=5, scope=scope)

        sql, params = repo.fetch_all_calls[-1]
        self.assertEqual(["chunk-1"], [hit["id"] for hit in hits])
        self.assertIn("websearch_to_tsquery", sql)
        self.assertIn("similarity", sql)
        self.assertIn("ilike", sql.lower())
        self.assertIn("%ERR_CODE_42%", params[2])
        self.assertEqual(5, params[-1])

    def test_metadata_defaults_match_sqlite_repository_contract(self):
        repo = RecordingDocumentRepository()
        child = Chunk("child-1", "doc-1", "parent-1", "child", "Manual", "text", "text", 1, 1, 1, {})
        image = Chunk("image-1", "doc-1", "parent-1", "image_ocr", "Manual", "ocr", "ocr", 1, 1, 1, {})

        child_metadata = repo._normalized_chunk_metadata(child)
        image_metadata = repo._normalized_chunk_metadata(image)

        self.assertEqual(PROCESSING_VERSION, child_metadata["processing_version"])
        self.assertEqual("legacy", child_metadata["strategy"])
        self.assertEqual("image_ocr", image_metadata["strategy"])
        self.assertTrue(image_metadata["generated_evidence"])

    def test_update_enrichment_writes_jsonb_fields_and_preserves_return_shape(self):
        repo = RecordingDocumentRepository()
        scope = KnowledgeBaseScope(workspace_id="workspace-1", selected_knowledge_base_ids=("kb-1",))

        updated = repo.update_enrichment(
            "doc-1",
            scope,
            status="completed",
            keywords=["ERR_CODE_42"],
            suggested_questions=["How to fix ERR_CODE_42?"],
            source_chunk_ids=["chunk-1"],
            increment_version=True,
        )

        sql, params = repo.execute_calls[-1]
        self.assertEqual("doc-1", updated["id"])
        self.assertIn("keywords_json = %s::jsonb", sql)
        self.assertIn("summary_source_chunk_ids_json = %s::jsonb", sql)
        self.assertIn("summary_version = summary_version + 1", sql)
        self.assertIn("[\"ERR_CODE_42\"]", params)

    def test_decode_row_converts_postgres_timestamps_to_strings(self):
        repo = RecordingDocumentRepository()
        row = dict(repo.document_row)
        row["created_at"] = datetime(2026, 8, 23, 14, 58, 53, tzinfo=timezone.utc)
        row["updated_at"] = datetime(2026, 8, 23, 14, 59, 10, tzinfo=timezone.utc)
        row["summary_generated_at"] = datetime(2026, 8, 23, 15, 0, tzinfo=timezone.utc)

        decoded = repo._decode_row(row)

        self.assertEqual("2026-08-23T14:58:53+00:00", decoded["created_at"])
        self.assertEqual("2026-08-23T14:59:10+00:00", decoded["updated_at"])
        self.assertEqual("2026-08-23T15:00:00+00:00", decoded["summary_generated_at"])


if __name__ == "__main__":
    unittest.main()
