import unittest

from app.models.document_models import ParsedImage
from app.models.knowledge_base import KnowledgeBaseScope
from app.services.documents.postgres_image_repository import PostgresImageRepository
from app.services.documents.postgres_upload_batch_repository import PostgresUploadBatchRepository


class _FakeDatabase:
    settings = type("Settings", (), {"schema": "rag"})()

    def connection(self):
        return self

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class RecordingImageRepository(PostgresImageRepository):
    def __init__(self):
        super().__init__(_FakeDatabase(), schema="rag", validate_schema=False)
        self.fetch_one_calls = []
        self.fetch_all_calls = []
        self.execute_calls = []

    def _fetch_one(self, sql, params):
        self.fetch_one_calls.append((sql, params))
        if "document_image_resource" in sql and "select *" in sql.lower():
            return {
                "id": "img-1",
                "workspace_id": "workspace-1",
                "knowledge_base_id": "kb-1",
                "doc_id": "doc-1",
                "storage_key": "media/img.jpg",
                "storage_provider": "local",
                "source_type": "scanned_pdf",
                "page_number": 2,
                "mime_type": "image/jpeg",
                "width": None,
                "height": None,
                "metadata_json": {"caption": "cover"},
                "created_at": "2026-08-13T00:00:00",
            }
        if "document_image_operation" in sql and "select *" in sql.lower():
            return {"id": "op-1", "status": "failed", "attempt": 1}
        if "storage_key" in sql:
            return {"storage_key": "media/img.jpg"}
        return None

    def _fetch_all(self, sql, params):
        self.fetch_all_calls.append((sql, params))
        return [{"storage_key": "media/img.jpg"}]

    def _execute(self, sql, params):
        self.execute_calls.append((sql, params))
        return 1

    def _fetch_all_in_conn(self, conn, sql, params):
        return self._fetch_all(sql, params)

    def _execute_in_conn(self, conn, sql, params):
        return self._execute(sql, params)


class RecordingUploadBatchRepository(PostgresUploadBatchRepository):
    def __init__(self):
        super().__init__(_FakeDatabase(), schema="rag", validate_schema=False)
        self.fetch_all_calls = []
        self.execute_calls = []

    def _fetch_all(self, sql, params):
        self.fetch_all_calls.append((sql, params))
        return []

    def _fetch_one(self, sql, params):
        if "knowledge_upload_batch" in sql:
            return {
                "id": params[0],
                "workspace_id": params[1],
                "knowledge_base_id": params[2],
                "status": "failed",
                "settings_json": {"chunk_size": 800},
                "error_message": "",
                "created_at": "2026-08-13T00:00:00",
                "updated_at": "2026-08-13T00:00:00",
                "confirmed_at": None,
                "completed_at": "2026-08-13T00:00:00",
            }
        if "knowledge_upload_file" in sql:
            return {
                "id": params[0],
                "batch_id": "batch-1",
                "workspace_id": params[1],
                "knowledge_base_id": params[2],
                "original_name": "manual.md",
                "relative_path": "manual.md",
                "storage_path": "uploads/manual.md",
                "size": 1,
                "status": "uploaded",
                "document_id": None,
                "chunks": 0,
                "error_message": "",
                "phases_json": [],
                "warnings_json": [],
                "errors_json": [],
                "retry_eligible": True,
                "created_at": "2026-08-13T00:00:00",
                "updated_at": "2026-08-13T00:00:00",
            }
        return None

    def _execute(self, sql, params):
        self.execute_calls.append((sql, params))
        return 1


class PostgresMediaUploadRepositoryTests(unittest.TestCase):
    def test_image_repository_uses_scope_and_jsonb_metadata(self):
        repo = RecordingImageRepository()
        scope = KnowledgeBaseScope("workspace-1", ("kb-1",))

        image = repo.get_image("img-1", scope)
        repo.list_operations("doc-1", scope, status="failed")
        repo.update_operation("op-1", scope, status="processing", increment_attempt=True)

        self.assertEqual({"caption": "cover"}, image["metadata"])
        self.assertEqual(("img-1", "workspace-1", "kb-1"), repo.fetch_one_calls[0][1])
        self.assertIn("status = %s", repo.fetch_all_calls[-1][0])
        self.assertIn("attempt = attempt + 1", repo.execute_calls[-1][0])

    def test_image_cleanup_keeps_postgres_not_exists_boundary(self):
        repo = RecordingImageRepository()
        scope = KnowledgeBaseScope("workspace-1", ("kb-1",))

        keys = repo.cleanup_abandoned_staged_resources("doc-1", scope)

        self.assertEqual(["media/img.jpg"], keys)
        self.assertIn("not exists", repo.fetch_all_calls[-1][0].lower())

    def test_upload_batch_repository_uses_jsonb_and_terminal_filtering(self):
        repo = RecordingUploadBatchRepository()
        scope = KnowledgeBaseScope("workspace-1", ("kb-1",))

        repo.list_batches(scope, include_terminal=False)
        repo.update_batch("batch-1", scope, status="failed", settings={"chunk_size": 800})
        repo.update_file("file-1", scope, phases=[{"name": "parse", "status": "completed"}], retry_eligible=True)

        list_sql, list_params = repo.fetch_all_calls[0]
        batch_sql, batch_params = repo.execute_calls[0]
        file_sql, file_params = repo.execute_calls[1]
        self.assertIn("status not in", list_sql)
        self.assertEqual(2 + 4, len(list_params))
        self.assertIn("settings_json = %s::jsonb", batch_sql)
        self.assertIn("{\"chunk_size\": 800}", batch_params)
        self.assertIn("phases_json = %s::jsonb", file_sql)
        self.assertIn("retry_eligible = %s", file_sql)
        self.assertIn(True, file_params)


if __name__ == "__main__":
    unittest.main()
