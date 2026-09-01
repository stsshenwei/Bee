import unittest

from app.models.document_models import Chunk
from app.models.kg_models import Entity
from app.models.knowledge_base import KnowledgeBaseScope
from app.services.kg.entity_vector_store import PostgresEntityVectorStore
from app.services.retrieval.postgres_vector_store import PostgresVectorDimensionError, PostgresVectorStore


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
        return {"count": 1}

    def fetchall(self):
        sql = self.raw.calls[-1][0]
        if "entity_embedding" in sql and "select" in sql.lower():
            return [
                {
                    "entity_id": "entity-redis",
                    "entity_type": "Middleware",
                    "entity_name": "Redis",
                    "description": "Cache",
                    "aliases_json": ["cache"],
                    "metadata_json": {"workspace_id": "workspace-1", "knowledge_base_id": "kb-1"},
                    "distance": 0.1,
                }
            ]
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


class _EmbeddingProvider:
    def __init__(self, dim=3):
        self.dim = dim

    def embed_batch(self, texts):
        return [[float(index + 1)] * self.dim for index, _ in enumerate(texts)]

    def embed_text(self, text):
        return [0.1] * self.dim


class RecordingVectorStore(PostgresVectorStore):
    def __init__(self):
        super().__init__(_FakeDatabase(), 3, _EmbeddingProvider(), schema="rag", validate_schema=False)
        self.fetch_all_calls = []
        self.execute_calls = []

    def count(self):
        return 1

    def _fetch_all(self, sql, params):
        self.fetch_all_calls.append((sql, params))
        return [
            {
                "chunk_id": "chunk-1",
                "doc_id": "doc-1",
                "workspace_id": "workspace-1",
                "knowledge_base_id": "kb-1",
                "parent_id": "parent-1",
                "chunk_type": "child",
                "title_path": "Manual",
                "page_start": 1,
                "page_end": 2,
                "metadata_json": {
                    "strategy": "legacy",
                    "processing_version": "v1",
                    "size_unit": "chars",
                    "source_type": "manual",
                },
                "distance": 0.2,
            }
        ]

    def _execute(self, sql, params):
        self.execute_calls.append((sql, params))
        return 1


class PostgresVectorStoreTests(unittest.TestCase):
    def test_upsert_chunks_writes_pgvector_rows_for_indexable_chunks(self):
        database = _RecordingDatabase()
        store = PostgresVectorStore(database, 3, _EmbeddingProvider(), schema="rag", validate_schema=False)
        chunks = [
            Chunk("chunk-1", "doc-1", "parent-1", "child", "Manual", "text", "text", 1, 2, 4, {"workspace_id": "workspace-1", "knowledge_base_id": "kb-1"}),
            Chunk("parent-1", "doc-1", None, "parent", "Manual", "parent", "parent", 1, 2, 4, {"workspace_id": "workspace-1", "knowledge_base_id": "kb-1"}),
        ]

        store.upsert_chunks(chunks)

        sql, rows = database.raw.calls[-1]
        self.assertIn('"rag"."document_chunk_embedding"', sql)
        self.assertIn("%s::vector", sql)
        self.assertIn("%s::jsonb", sql)
        self.assertIn("on conflict(chunk_id) do update", sql)
        self.assertEqual(1, len(rows))
        self.assertEqual("chunk-1", rows[0][0])
        self.assertEqual("[1.0,1.0,1.0]", rows[0][9])

    def test_dense_query_filters_scope_and_documents_before_ranking(self):
        store = RecordingVectorStore()
        scope = KnowledgeBaseScope("workspace-1", ("kb-1", "kb-2"), document_ids=("doc-1",))

        hits = store.query_dense("timeout", 5, scope)

        sql, params = store.fetch_all_calls[-1]
        self.assertEqual("chunk-1", hits[0]["metadata"]["chunk_id"])
        self.assertEqual(0.8, hits[0]["vector_score"])
        self.assertIn("e.workspace_id = %s", sql)
        self.assertIn("e.knowledge_base_id in (%s, %s)", sql)
        self.assertIn("e.doc_id in (%s)", sql)
        self.assertIn("order by e.embedding <=> %s::vector", sql)
        self.assertEqual(("[0.1,0.1,0.1]", "workspace-1", "kb-1", "kb-2", "doc-1", "[0.1,0.1,0.1]", 5), params)

    def test_delete_document_and_knowledge_base_are_scoped(self):
        store = RecordingVectorStore()
        scope = KnowledgeBaseScope("workspace-1", ("kb-1", "kb-2"))

        store.delete_document("doc-1", scope)
        store.delete_knowledge_base(scope)

        doc_sql, doc_params = store.execute_calls[0]
        kb_sql, kb_params = store.execute_calls[1]
        self.assertIn("doc_id = %s", doc_sql)
        self.assertEqual(("doc-1", "workspace-1", "kb-1", "kb-2"), doc_params)
        self.assertNotIn("doc_id = %s", kb_sql)
        self.assertEqual(("workspace-1", "kb-1", "kb-2"), kb_params)

    def test_replace_document_chunks_deletes_then_reinserts_document_vectors(self):
        database = _RecordingDatabase()
        store = PostgresVectorStore(database, 3, _EmbeddingProvider(), schema="rag", validate_schema=False)
        scope = KnowledgeBaseScope("workspace-1", ("kb-1",))
        chunk = Chunk("chunk-1", "doc-1", None, "child", "", "text", "text", None, None, 1, {"workspace_id": "workspace-1", "knowledge_base_id": "kb-1"})

        store.replace_document_chunks("doc-1", [chunk], scope=scope)

        delete_sql, delete_params = database.raw.calls[0]
        insert_sql, rows = database.raw.calls[1]
        self.assertIn("delete from", delete_sql.lower())
        self.assertEqual(("doc-1", "workspace-1", "kb-1"), delete_params)
        self.assertIn("insert into", insert_sql.lower())
        self.assertEqual("chunk-1", rows[0][0])

    def test_dimension_mismatch_fails_before_write(self):
        database = _RecordingDatabase()
        store = PostgresVectorStore(database, 3, _EmbeddingProvider(dim=2), schema="rag", validate_schema=False)
        chunk = Chunk("chunk-1", "doc-1", None, "child", "", "text", "text", None, None, 1, {})

        with self.assertRaises(PostgresVectorDimensionError):
            store.upsert_chunks([chunk])

        self.assertEqual([], database.raw.calls)

    def test_entity_vector_store_upserts_entities_with_scope_metadata(self):
        database = _RecordingDatabase()
        store = PostgresEntityVectorStore(database, 3, _EmbeddingProvider(), schema="rag", validate_schema=False)
        entity = Entity(
            id="entity-redis",
            type="Middleware",
            name="Redis",
            aliases=["cache"],
            metadata={"workspace_id": "workspace-1", "knowledge_base_id": "kb-1"},
        )

        store.upsert_entities([entity])

        sql, rows = database.raw.calls[-1]
        self.assertIn('"rag"."entity_embedding"', sql)
        self.assertIn("%s::vector", sql)
        self.assertIn("on conflict(workspace_id, knowledge_base_id, entity_id)", sql)
        self.assertEqual("workspace-1", rows[0][1])
        self.assertEqual("[1.0,1.0,1.0]", rows[0][8])

    def test_entity_vector_search_is_scoped_before_similarity_ranking(self):
        database = _RecordingDatabase()
        store = PostgresEntityVectorStore(database, 3, _EmbeddingProvider(), schema="rag", validate_schema=False)
        scope = KnowledgeBaseScope("workspace-1", ("kb-1", "kb-2"))

        matches = store.search_similar(Entity(id="", type="Middleware", name="Redis"), top_k=2, scope=scope)

        sql, params = database.raw.calls[-1]
        self.assertEqual("entity-redis", matches[0]["entity"].id)
        self.assertEqual(0.9, matches[0]["score"])
        self.assertIn("workspace_id = %s", sql)
        self.assertIn("knowledge_base_id in (%s, %s)", sql)
        self.assertIn("order by embedding <=> %s::vector", sql)
        self.assertEqual(("[0.1,0.1,0.1]", "workspace-1", "kb-1", "kb-2", "[0.1,0.1,0.1]", 2), params)


if __name__ == "__main__":
    unittest.main()
