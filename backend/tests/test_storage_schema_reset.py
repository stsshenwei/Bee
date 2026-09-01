import json
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from app.services.storage.storage_reset import (
    KnowledgeStorageResetCoordinator,
    ManagedFilesResetProvider,
    MilvusCollectionsResetProvider,
    Neo4jStorageResetProvider,
    RESET_CONFIRMATION,
    ResetPlanItem,
    SQLiteStorageResetProvider,
)
from app.services.storage.storage_schema import (
    METADATA_SCHEMA_VERSION,
    StorageResetRequired,
    initialize_evaluation_database,
    initialize_metadata_database,
)


class FailingProvider:
    name = "failing"

    def plan(self):
        return []

    def backup(self, backup_root):
        return {}

    def reset(self):
        raise RuntimeError("reset failed")

    def initialize(self):
        return {}


class RecordingResetProvider:
    def __init__(self, name, targets):
        self.name = name
        self.targets = set(targets)
        self.initialized = False

    def plan(self):
        return [ResetPlanItem(self.name, target, "drop", True) for target in sorted(self.targets)]

    def backup(self, backup_root):
        return {"supported": False}

    def reset(self):
        dropped = sorted(self.targets)
        self.targets.clear()
        return {"dropped": dropped}

    def initialize(self):
        self.initialized = True
        return {"created": [self.name]}


class FakeCollection:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class FakeNeo4jResult:
    def single(self):
        return {"removed": 2}


class FakeNeo4jSession:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, query):
        self.calls.append(query)
        return FakeNeo4jResult()


class FakeNeo4jDriver:
    def __init__(self, calls):
        self.calls = calls

    def session(self):
        return FakeNeo4jSession(self.calls)

    def close(self):
        pass


class StorageSchemaResetTests(unittest.TestCase):
    def test_final_schema_is_idempotent_and_enforces_composite_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            initialize_metadata_database(path)
            initialize_metadata_database(path)
            with sqlite3.connect(path) as conn:
                conn.execute("pragma foreign_keys = on")
                version = conn.execute(
                    "select version from storage_schema where component = 'primary'"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in conn.execute(
                        "select name from sqlite_master where type = 'table'"
                    ).fetchall()
                }
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into document_chunk(
                            id, doc_id, workspace_id, knowledge_base_id, parent_id, chunk_type,
                            title_path, content, content_markdown, page_start, page_end,
                            token_count, metadata_json, created_at
                        ) values ('c','missing','default-workspace','default-knowledge-base',null,
                                  'child','','x','x',1,1,1,'{}','now')
                        """
                    )
            conn.close()
            self.assertEqual(METADATA_SCHEMA_VERSION, version)
            self.assertTrue(
                {
                    "document_enrichment_task",
                    "document_image_resource",
                    "document_image_operation",
                    "document_processing_task",
                    "document_processing_dead_letter",
                    "query_log",
                    "answer_feedback",
                    "document_chunk_fts",
                }.issubset(tables)
            )

    def test_legacy_schema_is_rejected_and_not_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
            with sqlite3.connect(path) as conn:
                conn.execute("create table document(id text primary key, content text)")
                conn.execute("insert into document values ('legacy', 'keep')")
            conn.close()

            with self.assertRaises(StorageResetRequired):
                initialize_metadata_database(path)

            with sqlite3.connect(path) as conn:
                self.assertEqual(("legacy", "keep"), conn.execute("select * from document").fetchone())
                self.assertIsNone(conn.execute(
                    "select 1 from sqlite_master where name = 'storage_schema'"
                ).fetchone())
            conn.close()

    def test_dry_run_confirmation_and_complete_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "legacy.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute("create table old_data(value text)")
                conn.execute("insert into old_data values ('legacy')")
            conn.close()
            uploads = root / "data" / "uploads"
            uploads.mkdir(parents=True)
            (uploads / "old.txt").write_text("legacy", encoding="utf-8")
            provider = SQLiteStorageResetProvider(
                "metadata",
                db_path,
                lambda: initialize_metadata_database(db_path),
            )
            files = ManagedFilesResetProvider(root / "data", ["uploads"], enabled=True)
            coordinator = KnowledgeStorageResetCoordinator(
                [provider, files], root / "state", root / "runtime.lock"
            )

            plan = coordinator.plan()
            self.assertTrue(db_path.exists())
            self.assertTrue(any(item.exists for item in plan))
            with self.assertRaises(ValueError):
                coordinator.execute(confirmation="WRONG")
            self.assertTrue(db_path.exists())

            manifest = coordinator.execute(confirmation=RESET_CONFIRMATION)

            self.assertEqual("completed", manifest["status"])
            self.assertFalse(uploads.exists())
            self.assertFalse((root / "state" / "maintenance.json").exists())
            stored = json.loads((root / "state" / "reset-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("completed", stored["status"])
            with sqlite3.connect(db_path) as conn:
                self.assertEqual(0, conn.execute("select count(*) from document").fetchone()[0])
                self.assertEqual(1, conn.execute("select count(*) from knowledge_base").fetchone()[0])
                self.assertEqual(0, conn.execute("select count(*) from document_chunk_fts").fetchone()[0])
            conn.close()

    def test_full_clean_rebuild_removes_legacy_business_vector_graph_eval_memory_and_media_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata = root / "metadata.sqlite3"
            evaluation = root / "evaluation.sqlite3"
            memory = root / "memory.sqlite3"
            data = root / "data"
            media = root / "vector" / "media"
            reports = root / "reports"
            ingest_state = root / "vector" / "ingest_state.json"
            data.joinpath("uploads").mkdir(parents=True)
            data.joinpath("feedback").mkdir()
            media.mkdir(parents=True)
            reports.mkdir()
            data.joinpath("uploads", "legacy.md").write_text("legacy corpus", encoding="utf-8")
            data.joinpath("feedback", "legacy.md").write_text("legacy feedback", encoding="utf-8")
            media.joinpath("legacy.jpg").write_bytes(b"legacy-media")
            reports.joinpath("legacy-report.json").write_text("{}", encoding="utf-8")
            ingest_state.write_text("{}", encoding="utf-8")
            conn = sqlite3.connect(metadata)
            try:
                conn.executescript(
                    """
                    create table auth_user(id text primary key);
                    create table tenant(id text primary key);
                    create table knowledge_base(id text primary key);
                    create table document(id text primary key);
                    create table parse_task(id text primary key);
                    create table chat_session(id text primary key);
                    create table graph_memory(id text primary key);
                    create virtual table document_chunk_fts using fts5(content);
                    insert into auth_user values ('legacy-user');
                    insert into tenant values ('legacy-tenant');
                    insert into knowledge_base values ('legacy-kb');
                    insert into document values ('legacy-doc');
                    insert into parse_task values ('legacy-task');
                    insert into chat_session values ('legacy-chat');
                    insert into graph_memory values ('legacy-graph');
                    insert into document_chunk_fts(content) values ('legacy-token');
                    """
                )
            finally:
                conn.close()
            conn = sqlite3.connect(evaluation)
            try:
                conn.executescript(
                    """
                    create table eval_run(id text primary key);
                    create table eval_result(id text primary key);
                    insert into eval_run values ('legacy-eval-run');
                    insert into eval_result values ('legacy-eval-result');
                    """
                )
            finally:
                conn.close()
            conn = sqlite3.connect(memory)
            try:
                conn.executescript(
                    """
                    create table memory_item(id text primary key);
                    insert into memory_item values ('legacy-memory');
                    """
                )
            finally:
                conn.close()
            vector_provider = RecordingResetProvider("milvus", ["rag_chunks", "kg_entities"])
            graph_provider = RecordingResetProvider("neo4j", ["nodes", "relationships"])
            coordinator = KnowledgeStorageResetCoordinator(
                [
                    SQLiteStorageResetProvider("metadata", metadata, lambda: initialize_metadata_database(metadata)),
                    SQLiteStorageResetProvider("evaluation", evaluation, lambda: initialize_evaluation_database(evaluation)),
                    SQLiteStorageResetProvider("memory", memory),
                    ManagedFilesResetProvider(data, ["uploads", "feedback"], enabled=True, name="managed-sources"),
                    ManagedFilesResetProvider(media.parent, [media.name], enabled=True, name="legacy-media"),
                    ManagedFilesResetProvider(reports.parent, [reports.name], enabled=True, name="evaluation-reports"),
                    ManagedFilesResetProvider(ingest_state.parent, [ingest_state.name], enabled=True, name="ingest-state"),
                    vector_provider,
                    graph_provider,
                ],
                root / "state",
                root / "runtime.lock",
            )

            manifest = coordinator.execute(confirmation=RESET_CONFIRMATION)

            self.assertEqual("completed", manifest["status"])
            self.assertTrue(vector_provider.initialized)
            self.assertTrue(graph_provider.initialized)
            self.assertFalse(data.joinpath("uploads").exists())
            self.assertFalse(data.joinpath("feedback").exists())
            self.assertFalse(media.exists())
            self.assertFalse(reports.exists())
            self.assertFalse(ingest_state.exists())
            self.assertFalse(memory.exists())
            conn = sqlite3.connect(metadata)
            try:
                tables = {
                    row[0]
                    for row in conn.execute("select name from sqlite_master where type = 'table'").fetchall()
                }
                self.assertFalse(
                    {
                        "auth_user",
                        "tenant",
                        "chat_session",
                        "graph_memory",
                    }
                    & tables
                )
                self.assertEqual(METADATA_SCHEMA_VERSION, conn.execute(
                    "select version from storage_schema where component = 'primary'"
                ).fetchone()[0])
                self.assertEqual(1, conn.execute("select count(*) from workspace").fetchone()[0])
                self.assertEqual(1, conn.execute("select count(*) from knowledge_base").fetchone()[0])
                self.assertEqual(0, conn.execute("select count(*) from document").fetchone()[0])
                self.assertEqual(0, conn.execute("select count(*) from parse_task").fetchone()[0])
                self.assertEqual(0, conn.execute("select count(*) from query_log").fetchone()[0])
                self.assertEqual(0, conn.execute("select count(*) from answer_feedback").fetchone()[0])
                self.assertEqual(0, conn.execute("select count(*) from kg_extraction_task").fetchone()[0])
                self.assertEqual(0, conn.execute("select count(*) from entity_mention").fetchone()[0])
                self.assertEqual(0, conn.execute("select count(*) from graph_community_summary").fetchone()[0])
                self.assertEqual(0, conn.execute("select count(*) from knowledge_upload_batch").fetchone()[0])
                self.assertEqual(0, conn.execute("select count(*) from document_processing_task").fetchone()[0])
                self.assertEqual(0, conn.execute("select count(*) from document_processing_dead_letter").fetchone()[0])
                self.assertEqual(0, conn.execute("select count(*) from document_chunk_fts").fetchone()[0])
            finally:
                conn.close()
            conn = sqlite3.connect(evaluation)
            try:
                self.assertEqual(0, conn.execute("select count(*) from eval_run").fetchone()[0])
                self.assertEqual(0, conn.execute("select count(*) from eval_result").fetchone()[0])
            finally:
                conn.close()

    def test_active_writer_and_partial_failure_keep_storage_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / "runtime.lock"
            lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            coordinator = KnowledgeStorageResetCoordinator([], root / "state", lock)
            with self.assertRaises(RuntimeError):
                coordinator.execute(confirmation=RESET_CONFIRMATION)
            lock.unlink()

            coordinator = KnowledgeStorageResetCoordinator(
                [FailingProvider()], root / "state", lock
            )
            with self.assertRaisesRegex(RuntimeError, "reset failed"):
                coordinator.execute(confirmation=RESET_CONFIRMATION)
            maintenance = json.loads((root / "state" / "maintenance.json").read_text(encoding="utf-8"))
            manifest = json.loads((root / "state" / "reset-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("maintenance", maintenance["status"])
            self.assertEqual("failed", manifest["status"])

    def test_managed_file_provider_rejects_targets_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                ManagedFilesResetProvider(Path(tmp), ["../escape"], enabled=True)

    def test_milvus_and_neo4j_providers_drop_and_initialize_final_structures(self):
        class Utility:
            collections = {"rag", "entity"}

            @classmethod
            def has_collection(cls, name):
                return name in cls.collections

            @classmethod
            def list_collections(cls):
                return sorted(cls.collections)

            @classmethod
            def drop_collection(cls, name):
                cls.collections.remove(name)

        provider = MilvusCollectionsResetProvider("uri", "token", "rag", "entity", 4, False)
        provider._connect = lambda: Utility
        self.assertEqual(2, len(provider.plan()))
        self.assertEqual({"dropped": ["entity", "rag"]}, provider.reset())
        rag = FakeCollection()
        entity = FakeCollection()
        with patch("app.services.retrieval.vector_store._create_or_load_collection", return_value=rag), patch(
            "app.services.kg.entity_vector_store._create_or_load_entity_collection", return_value=entity
        ):
            initialized = provider.initialize()
        self.assertEqual(["rag", "entity"], initialized["created"])
        self.assertTrue(rag.released and entity.released)

        calls = []
        graph = Neo4jStorageResetProvider(
            "bolt://test", "neo4j", "password", driver_factory=lambda *args, **kwargs: FakeNeo4jDriver(calls)
        )
        self.assertEqual(2, graph.reset()["removed_nodes"])
        self.assertEqual(["bee_entity_id"], graph.initialize()["constraints"])
        self.assertTrue(any("DETACH DELETE" in query for query in calls))
        self.assertTrue(any("CREATE CONSTRAINT" in query for query in calls))

    def test_cli_dry_run_then_executes_offline_clean_rebuild(self):
        from app.scripts.rebuild_knowledge_storage import main

        class FakePostgresResetProvider(RecordingResetProvider):
            def __init__(self, *args, **kwargs):
                super().__init__("postgres", ["postgres://user:***@localhost/db#public"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vector = root / "vector"
            data = root / "data"
            vector.mkdir()
            (data / "uploads").mkdir(parents=True)
            (data / "uploads" / "old.txt").write_text("old", encoding="utf-8")
            metadata = vector / "metadata.sqlite3"
            with sqlite3.connect(metadata) as conn:
                conn.execute("create table legacy(value text)")
            conn.close()
            env = {
                "VECTOR_STORE_DIR": str(vector),
                "METADATA_DB_PATH": str(metadata),
                "KG_METADATA_DB_PATH": str(metadata),
                "EVAL_DB_PATH": str(vector / "eval.sqlite3"),
                "MEMORY_DB_PATH": str(vector / "memory.sqlite3"),
                "EVAL_REPORT_DIR": str(vector / "reports"),
                "RAG_DATA_DIR": str(data),
                "STORAGE_RESET_STATE_DIR": str(vector / "reset-state"),
                "STORAGE_RUNTIME_LOCK": str(vector / "runtime.lock"),
                "DATABASE_URL": "postgresql://user:secret@localhost/db",
            }
            with patch.dict(os.environ, env, clear=False), patch(
                "app.scripts.rebuild_knowledge_storage.PostgresStorageResetProvider",
                FakePostgresResetProvider,
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(0, main(["--skip-milvus"]))
            with sqlite3.connect(metadata) as conn:
                self.assertEqual("legacy", conn.execute(
                    "select name from sqlite_master where name = 'legacy'"
                ).fetchone()[0])
            conn.close()

            with patch.dict(os.environ, env, clear=False), patch(
                "app.scripts.rebuild_knowledge_storage.PostgresStorageResetProvider",
                FakePostgresResetProvider,
            ), redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    main(
                        [
                            "--skip-milvus",
                            "--skip-neo4j",
                            "--execute",
                            "--environment",
                            "test",
                            "--confirm",
                            f"{RESET_CONFIRMATION}:test",
                        ]
                    ),
                )
            self.assertFalse(metadata.exists())
            self.assertFalse((data / "uploads").exists())


if __name__ == "__main__":
    unittest.main()
