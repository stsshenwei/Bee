import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.models.document_models import Chunk
from app.models.knowledge_base import ProviderReferences
from app.services.documents.document_repository import DocumentRepository
from app.services.knowledge.knowledge_base_repository import KnowledgeBaseRepository
from app.services.knowledge.knowledge_base_service import KnowledgeBaseService, KnowledgeBaseValidationError
from app.services.storage.storage_schema import METADATA_SCHEMA_VERSION, StorageResetRequired


class KnowledgeBaseRepositoryTests(unittest.TestCase):
    def test_fresh_database_creates_deterministic_defaults_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            first = KnowledgeBaseRepository(path)
            workspace, knowledge_base = first.ensure_defaults()
            second = KnowledgeBaseRepository(path)

            self.assertEqual("default-workspace", workspace.id)
            self.assertEqual("default-knowledge-base", knowledge_base.id)
            self.assertTrue(knowledge_base.is_default)
            self.assertEqual(1, len(second.list_knowledge_bases("default-workspace")))
            with sqlite3.connect(path) as conn:
                versions = conn.execute(
                    "select count(*) from storage_schema where version = ?", (METADATA_SCHEMA_VERSION,)
                ).fetchone()[0]
            conn.close()
            self.assertEqual(1, versions)

    def test_legacy_document_and_chunks_require_clean_rebuild_without_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "create table document(id text primary key, name text not null, file_type text not null, "
                    "storage_path text not null, parse_status text not null, created_at text not null, "
                    "updated_at text not null, metadata_json text not null)"
                )
                conn.execute(
                    "create table document_chunk(id text primary key, doc_id text not null, parent_id text, "
                    "chunk_type text not null, title_path text not null, content text not null, "
                    "content_markdown text not null, page_start integer, page_end integer, token_count integer not null, "
                    "metadata_json text not null, created_at text not null)"
                )
                conn.execute(
                    "insert into document values ('doc-1','manual.pdf','pdf','uploads/manual.pdf','parsed','a','b','{}')"
                )
                conn.execute(
                    "insert into document_chunk values ('chunk-1','doc-1',null,'child','Guide','original','original',1,1,2,'{}','a')"
                )
            conn.close()

            with self.assertRaises(StorageResetRequired):
                DocumentRepository(path)
            with sqlite3.connect(path) as conn:
                self.assertEqual("original", conn.execute("select content from document_chunk").fetchone()[0])
                self.assertIsNone(
                    conn.execute("select 1 from sqlite_master where name = 'storage_schema'").fetchone()
                )
            conn.close()

    def test_service_lifecycle_preserves_requested_and_effective_provider_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            DocumentRepository(path)
            repository = KnowledgeBaseRepository(path)
            service = KnowledgeBaseService(
                repository,
                default_providers=ProviderReferences(embedding="openai"),
                supported_provider_refs={"reranker": {"dashscope"}},
            )

            created = service.create(
                "产品资料",
                provider_config={"embedding": "unsupported-embedding", "reranker": "dashscope"},
            )
            archived = service.archive(created.id)
            restored = service.restore(created.id)

            self.assertEqual(("embedding",), created.provider_config.inactive_overrides)
            self.assertEqual("openai", created.provider_config.effective.embedding)
            self.assertEqual("dashscope", created.provider_config.effective.reranker)
            self.assertEqual("archived", archived.status)
            self.assertEqual("active", restored.status)

    def test_legacy_partial_ownership_is_not_backfilled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "create table document(id text primary key, workspace_id text, knowledge_base_id text, name text not null, "
                    "file_type text not null, storage_path text not null, parse_status text not null, created_at text not null, "
                    "updated_at text not null, metadata_json text not null)"
                )
                conn.execute(
                    "insert into document values ('legacy-doc', null, null, 'legacy.md', 'md', 'legacy.md', 'parsed', 'a', 'a', '{}')"
                )
            conn.close()

            with self.assertRaises(StorageResetRequired):
                DocumentRepository(path)
            with sqlite3.connect(path) as conn:
                self.assertEqual((None, None), conn.execute(
                    "select workspace_id, knowledge_base_id from document where id = 'legacy-doc'"
                ).fetchone())
            conn.close()

    def test_default_identity_conflict_rolls_back_migration_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "conflict.sqlite3"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    "create table workspace(id text primary key, name text not null, description text not null, "
                    "status text not null, created_at text not null, updated_at text not null)"
                )
                conn.execute(
                    "create table knowledge_base(id text primary key, workspace_id text not null, name text not null, "
                    "description text not null, type text not null, status text not null, indexing_strategy_json text not null, "
                    "provider_config_json text not null, reindex_required integer not null, created_at text not null, updated_at text not null)"
                )
                conn.execute("insert into workspace values ('other','Other','','active','a','a')")
                conn.execute(
                    "insert into knowledge_base values ('default-knowledge-base','other','Conflict','','document','active','{}','{}',0,'a','a')"
                )
            conn.close()

            with self.assertRaises(StorageResetRequired):
                KnowledgeBaseRepository(path)

            with sqlite3.connect(path) as conn:
                default_workspace = conn.execute(
                    "select 1 from workspace where id = 'default-workspace'"
                ).fetchone()
                schema = conn.execute(
                    "select 1 from sqlite_master where type = 'table' and name = 'storage_schema'"
                ).fetchone()
            conn.close()
            self.assertIsNone(default_workspace)
            self.assertIsNone(schema)

    def test_service_rejects_duplicate_empty_and_unknown_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            DocumentRepository(path)
            service = KnowledgeBaseService(KnowledgeBaseRepository(path))
            faq = service.create("FAQ", knowledge_base_type="faq")
            service.create("研发")

            with self.assertRaises(KnowledgeBaseValidationError):
                service.create("研发")
            with self.assertRaises(KnowledgeBaseValidationError):
                service.create("  ")
            with self.assertRaises(KnowledgeBaseValidationError):
                service.create("Future", knowledge_base_type="future")
            self.assertEqual("faq", faq.type)

    def test_wiki_creation_uses_preset_but_preserves_explicit_combined_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            service = KnowledgeBaseService(KnowledgeBaseRepository(path))

            wiki_only = service.create("Wiki only", knowledge_base_type="wiki")
            combined = service.create(
                "Combined Wiki",
                knowledge_base_type="wiki",
                indexing_strategy={"wiki_enabled": True, "dense_enabled": True, "keyword_enabled": False},
            )

            self.assertTrue(wiki_only.indexing_strategy.wiki_enabled)
            self.assertFalse(wiki_only.indexing_strategy.needs_embedding)
            self.assertTrue(combined.indexing_strategy.dense_enabled)
            self.assertFalse(combined.indexing_strategy.keyword_enabled)

    def test_wiki_migration_repairs_flag_without_disabling_existing_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            service = KnowledgeBaseService(KnowledgeBaseRepository(path))
            wiki = service.create(
                "Legacy Wiki",
                knowledge_base_type="wiki",
                indexing_strategy={"wiki_enabled": True, "dense_enabled": True, "keyword_enabled": True, "graph_enabled": True},
            )
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "update knowledge_base set indexing_strategy_json = ? where id = ?",
                    (json.dumps({"wiki_enabled": False, "dense_enabled": True, "keyword_enabled": True, "graph_enabled": True}), wiki.id),
                )
                conn.commit()
            finally:
                conn.close()
            migrated = KnowledgeBaseRepository(path).get_knowledge_base(wiki.id)

            self.assertTrue(migrated.indexing_strategy.wiki_enabled)
            self.assertTrue(migrated.indexing_strategy.dense_enabled)
            self.assertTrue(migrated.indexing_strategy.keyword_enabled)
            self.assertTrue(migrated.indexing_strategy.graph_enabled)

    def test_service_tracks_one_default_knowledge_base_per_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            DocumentRepository(path)
            service = KnowledgeBaseService(KnowledgeBaseRepository(path))
            original_scope = service.resolve_scope()

            new_default = service.create("new-default", is_default=True)
            current_scope = service.resolve_scope()
            original = service.get("default-knowledge-base")

            self.assertEqual(("default-knowledge-base",), original_scope.selected_knowledge_base_ids)
            self.assertEqual((new_default.id,), current_scope.selected_knowledge_base_ids)
            self.assertTrue(service.get(new_default.id).is_default)
            self.assertFalse(original.is_default)
            with self.assertRaises(KnowledgeBaseValidationError):
                service.archive(new_default.id)

            service.update("default-knowledge-base", is_default=True)
            self.assertEqual(("default-knowledge-base",), service.resolve_scope().selected_knowledge_base_ids)
            self.assertFalse(service.get(new_default.id).is_default)

    def test_chunk_write_rejects_cross_knowledge_base_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            documents = DocumentRepository(path)
            service = KnowledgeBaseService(KnowledgeBaseRepository(path))
            other = service.create("另一个库")
            documents.upsert_document(
                id="doc-1",
                name="manual.md",
                file_type="md",
                storage_path="manual.md",
                parse_status="parsed",
                workspace_id=other.workspace_id,
                knowledge_base_id=other.id,
            )
            chunk = Chunk(
                id="chunk-1",
                doc_id="doc-1",
                parent_id=None,
                chunk_type="parent",
                title_path="",
                content="content",
                content_markdown="content",
                page_start=1,
                page_end=1,
                token_count=1,
                metadata={"knowledge_base_id": "default-knowledge-base"},
            )

            with self.assertRaisesRegex(ValueError, "ownership"):
                documents.replace_chunks("doc-1", [chunk])

    def test_archived_knowledge_base_is_not_writable_or_resolvable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            documents = DocumentRepository(path)
            service = KnowledgeBaseService(KnowledgeBaseRepository(path))
            knowledge_base = service.create("待归档")
            service.archive(knowledge_base.id)

            with self.assertRaises(KeyError):
                service.resolve_scope([knowledge_base.id])
            with self.assertRaisesRegex(ValueError, "archived"):
                documents.upsert_document(
                    id="doc-1",
                    name="manual.md",
                    file_type="md",
                    storage_path="manual.md",
                    parse_status="parsed",
                    workspace_id=knowledge_base.workspace_id,
                    knowledge_base_id=knowledge_base.id,
                )

    def test_aggregate_counts_image_derived_chunks_as_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            documents = DocumentRepository(path)
            repository = KnowledgeBaseRepository(path)
            documents.upsert_document(
                id="doc-1",
                name="scan.pdf",
                file_type="pdf",
                storage_path="scan.pdf",
                parse_status="parsed",
            )
            documents.replace_chunks(
                "doc-1",
                [
                    Chunk("p1", "doc-1", None, "parent", "Manual", "parent", "parent", 1, 1, 1, {}),
                    Chunk("img-ocr", "doc-1", "p1", "image_ocr", "Manual", "IMAGE_TOKEN", "IMAGE_TOKEN", 1, 1, 1, {}),
                    Chunk("img-caption", "doc-1", "p1", "image_caption", "Manual", "CAPTION_TOKEN", "CAPTION_TOKEN", 1, 1, 1, {}),
                ],
            )

            kb = repository.get_knowledge_base("default-knowledge-base")

        self.assertEqual(1, kb.aggregate.document_count)
        self.assertEqual(2, kb.aggregate.indexed_chunk_count)


if __name__ == "__main__":
    unittest.main()
