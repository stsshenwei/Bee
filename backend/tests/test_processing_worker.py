import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from app.models.processing_config import DurableProcessingWorkerConfig
from app.services.documents.document_repository import DocumentRepository
from app.services.knowledge.knowledge_base_repository import KnowledgeBaseRepository
from app.services.knowledge.knowledge_base_service import KnowledgeBaseService
from app.services.processing.processing_task_repository import (
    TASK_CANCELED,
    TASK_COMPLETED,
    TASK_DEAD_LETTERED,
    TASK_PENDING,
    TASK_PROCESSING,
    TASK_RETRYING,
    ProcessingTaskRepository,
)
from app.services.processing.processing_span_tracker import ProcessingSpanRepository, ProcessingSpanTracker
from app.services.processing.processing_trace import ProcessingTraceRecorder
from app.services.processing.processing_worker import DocumentProcessingWorker, drain_worker
from app.services.documents.document_parser import stable_doc_id
from app.services.async_runtime.task_routes import (
    CHUNK_EXTRACT_TASK,
    EMBEDDING_INDEX_TASK,
    GRAPH_EXTRACTION_TASK,
    KNOWLEDGE_POSTPROCESS_TASK,
    SUMMARY_GENERATION_TASK,
    UPLOAD_FILE_TASK,
    WIKI_INGEST_TASK,
)
from tests.test_rag_service_structured_ingest import FakeAsyncQueue, FakeParser, FakeVectorStore, make_service


class ProcessingWorkerTests(unittest.TestCase):
    def test_process_claimed_task_uses_claim_lease_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = ProcessingTaskRepository(Path(tmpdir) / "metadata.sqlite3")
            scope = KnowledgeBaseService(KnowledgeBaseRepository(repo.db_path, repo.defaults)).resolve_scope()
            worker = DocumentProcessingWorker(
                repository=repo,
                rag_service=SimpleNamespace(),
                config=DurableProcessingWorkerConfig(enabled=True),
                worker_id="local-worker",
            )
            worker.register_handler("wiki.ingest", lambda _task: None)
            task = repo.create_task("wiki.ingest", scope, task_id="wiki-external-lease")
            claimed = repo.claim_task(task["id"], worker_id="celery-worker")

            worker._process_claimed_task(claimed)

            self.assertEqual(TASK_COMPLETED, repo.get_task(task["id"])["status"])

    def test_heartbeat_stops_cleanly_for_terminal_task(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = ProcessingTaskRepository(Path(tmpdir) / "metadata.sqlite3")
            scope = KnowledgeBaseService(KnowledgeBaseRepository(repo.db_path, repo.defaults)).resolve_scope()
            worker = DocumentProcessingWorker(
                repository=repo,
                rag_service=SimpleNamespace(),
                config=DurableProcessingWorkerConfig(enabled=True),
                worker_id="local-worker",
            )
            task = repo.create_task("wiki.ingest", scope, task_id="wiki-terminal-heartbeat")
            claimed = repo.claim_task(task["id"], worker_id="celery-worker")
            repo.cancel_task(claimed["id"], reason="document deleted")

            self.assertTrue(worker._log_stopped_heartbeat(claimed["id"], "celery-worker"))

    def test_typed_worker_honors_rate_limit_and_recovers_dead_letter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = ProcessingTaskRepository(Path(tmpdir) / "metadata.sqlite3")
            scope = KnowledgeBaseService(KnowledgeBaseRepository(repo.db_path, repo.defaults)).resolve_scope()
            worker = DocumentProcessingWorker(
                repository=repo,
                rag_service=SimpleNamespace(),
                config=DurableProcessingWorkerConfig(enabled=True, retry_backoff_seconds=(0,)),
                worker_id="typed-worker",
            )
            calls = 0

            class ProviderRateLimitError(RuntimeError):
                retry_after_seconds = 7

            def rate_limited_once(_task):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ProviderRateLimitError("provider rate limited")

            worker.register_handler("wiki.ingest", rate_limited_once)
            task = repo.create_task("wiki.ingest", scope, task_id="wiki-rate-limit", max_attempts=2)
            before = datetime.now()
            self.assertTrue(worker.run_once())
            retrying = repo.get_task(task["id"])
            self.assertEqual(TASK_RETRYING, retrying["status"])
            self.assertGreaterEqual((datetime.fromisoformat(retrying["next_run_at"]) - before).total_seconds(), 6)
            repo.retry(task["id"], error_code="TEST", error_message="run now", delay_seconds=0)
            self.assertTrue(worker.run_once())
            self.assertEqual(TASK_COMPLETED, repo.get_task(task["id"])["status"])
            rate_attempts = repo.list_attempts(task["id"])
            self.assertEqual(["retrying", "completed"], [item["status"] for item in rate_attempts])
            self.assertEqual("ProviderRateLimitError", rate_attempts[0]["error_code"])

            worker.register_handler("wiki.finalize", lambda _task: (_ for _ in ()).throw(RuntimeError("fatal")))
            dead = repo.create_task("wiki.finalize", scope, task_id="wiki-dead", max_attempts=1)
            self.assertTrue(worker.run_once())
            self.assertEqual(TASK_DEAD_LETTERED, repo.get_task(dead["id"])["status"])
            repo.retry_dead_letter(dead["id"], delay_seconds=0)
            worker.register_handler("wiki.finalize", lambda _task: None)
            self.assertTrue(worker.run_once())
            self.assertEqual(TASK_COMPLETED, repo.get_task(dead["id"])["status"])
            self.assertEqual(["dead_lettered", "completed"], [item["status"] for item in repo.list_attempts(dead["id"])])

    def test_worker_enqueues_and_processes_upload_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = DocumentRepository(tmp / "metadata.sqlite3")
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(repo.db_path, repo.defaults))
            scope = kb_service.resolve_scope()
            service = make_service(tmp, repo, FakeVectorStore(tmp / "vectors"), FakeParser(), knowledge_base_service=kb_service)
            task_repo = ProcessingTaskRepository(repo.db_path, repo.defaults)
            worker = DocumentProcessingWorker(
                repository=task_repo,
                rag_service=service,
                config=DurableProcessingWorkerConfig(enabled=True, retry_backoff_seconds=(0,), default_max_attempts=2),
                worker_id="test-worker",
            )
            service.processing_worker = worker

            batch = service.create_upload_batch(scope)
            uploaded = service.add_upload_batch_file(batch["id"], filename="manual.md", content=b"# Manual\n\nBody", scope=scope)
            started = service.start_upload_batch_processing(batch["id"], scope)
            expected_doc_id = stable_doc_id(tmp / str(uploaded["storage_path"]))
            pending_documents = service.list_documents(scope)
            queued_file = service.get_upload_batch(batch["id"], scope)["files"][0]
            queued_task = task_repo.list_tasks(scope, upload_batch_id=batch["id"])[0]

            self.assertEqual("processing", started["status"])
            self.assertEqual(1, len(pending_documents))
            self.assertEqual(expected_doc_id, pending_documents[0]["id"])
            self.assertEqual("pending", pending_documents[0]["parse_status"])
            self.assertEqual(expected_doc_id, queued_file["document_id"])
            self.assertEqual(1, len(task_repo.list_tasks(scope, upload_batch_id=batch["id"])))
            self.assertEqual(expected_doc_id, queued_task["document_id"])
            self.assertEqual(1, drain_worker(worker))
            task = task_repo.list_tasks(scope, upload_batch_id=batch["id"])[0]
            self.assertEqual(TASK_COMPLETED, task["status"])
            completed = service.get_upload_batch(batch["id"], scope)
            self.assertEqual("completed", completed["status"])
            self.assertEqual("completed", completed["files"][0]["status"])

    def test_async_upload_splits_parse_chunk_and_embedding_index_tasks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = DocumentRepository(tmp / "metadata.sqlite3")
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(repo.db_path, repo.defaults))
            scope = kb_service.resolve_scope()
            vector = FakeVectorStore(tmp / "vectors")
            service = make_service(tmp, repo, vector, FakeParser(), knowledge_base_service=kb_service)
            task_repo = ProcessingTaskRepository(repo.db_path, repo.defaults)
            queue = FakeAsyncQueue()
            worker = DocumentProcessingWorker(
                repository=task_repo,
                rag_service=service,
                config=DurableProcessingWorkerConfig(enabled=True, retry_backoff_seconds=(0,)),
                worker_id="async-worker",
                async_queue=queue,
                start_local_worker=False,
            )
            service.processing_worker = worker
            service.async_processing_queue = queue

            batch = service.create_upload_batch(scope)
            service.add_upload_batch_file(batch["id"], filename="manual.md", content=b"# Manual\n\nBody", scope=scope)
            service.start_upload_batch_processing(batch["id"], scope)

            self.assertTrue(worker.run_once())
            tasks = task_repo.list_tasks(scope, upload_batch_id=batch["id"])
            by_type = {task["task_type"]: task for task in tasks}
            self.assertEqual({UPLOAD_FILE_TASK, CHUNK_EXTRACT_TASK}, set(by_type))
            self.assertEqual("core", by_type[CHUNK_EXTRACT_TASK]["payload"]["_async_runtime"]["queue_name"])
            self.assertEqual(0, len(vector.indexed))

            self.assertTrue(worker.run_once())
            tasks = task_repo.list_tasks(scope, upload_batch_id=batch["id"])
            by_type = {task["task_type"]: task for task in tasks}
            self.assertEqual({UPLOAD_FILE_TASK, CHUNK_EXTRACT_TASK, EMBEDDING_INDEX_TASK}, set(by_type))
            self.assertEqual("child", repo.get_chunk("c1", scope)["chunk_type"])
            self.assertEqual(0, len(vector.indexed))

            self.assertTrue(worker.run_once())
            tasks = task_repo.list_tasks(scope, upload_batch_id=batch["id"])
            by_type = {task["task_type"]: task for task in tasks}
            self.assertEqual(
                {UPLOAD_FILE_TASK, CHUNK_EXTRACT_TASK, EMBEDDING_INDEX_TASK, KNOWLEDGE_POSTPROCESS_TASK},
                set(by_type),
            )
            self.assertEqual("child", vector.indexed[0].chunk_type)
            self.assertEqual("completed", service.get_upload_batch(batch["id"], scope)["files"][0]["status"])

            self.assertTrue(worker.run_once())
            statuses = {task["task_type"]: task["status"] for task in task_repo.list_tasks(scope, upload_batch_id=batch["id"])}
            self.assertEqual(TASK_COMPLETED, statuses[CHUNK_EXTRACT_TASK])
            self.assertEqual(TASK_COMPLETED, statuses[EMBEDDING_INDEX_TASK])
            self.assertEqual(TASK_COMPLETED, statuses[KNOWLEDGE_POSTPROCESS_TASK])
            service.enqueue_upload_file_chunk_stage(service.get_upload_batch(batch["id"], scope)["files"][0], scope)
            deduped = [task for task in task_repo.list_tasks(scope, upload_batch_id=batch["id"]) if task["task_type"] == CHUNK_EXTRACT_TASK]
            self.assertEqual(1, len(deduped))

    def test_embedding_stage_retries_without_duplicate_vectors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = DocumentRepository(tmp / "metadata.sqlite3")
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(repo.db_path, repo.defaults))
            scope = kb_service.resolve_scope()
            vector = FailOnceVectorStore(tmp / "vectors")
            service = make_service(tmp, repo, vector, FakeParser(), knowledge_base_service=kb_service)
            task_repo = ProcessingTaskRepository(repo.db_path, repo.defaults)
            queue = FakeAsyncQueue()
            worker = DocumentProcessingWorker(
                repository=task_repo,
                rag_service=service,
                config=DurableProcessingWorkerConfig(enabled=True, retry_backoff_seconds=(0,), embedding_max_attempts=2),
                worker_id="async-worker",
                async_queue=queue,
                start_local_worker=False,
            )
            service.processing_worker = worker
            service.async_processing_queue = queue
            batch = service.create_upload_batch(scope)
            service.add_upload_batch_file(batch["id"], filename="manual.md", content=b"# Manual\n\nBody", scope=scope)
            service.start_upload_batch_processing(batch["id"], scope)

            self.assertTrue(worker.run_once())
            self.assertTrue(worker.run_once())
            self.assertTrue(worker.run_once())
            embedding = next(task for task in task_repo.list_tasks(scope, upload_batch_id=batch["id"]) if task["task_type"] == EMBEDDING_INDEX_TASK)
            self.assertEqual(TASK_RETRYING, embedding["status"])
            self.assertEqual(0, len(vector.indexed))

            self.assertTrue(worker.run_once())

            embedding = next(task for task in task_repo.list_tasks(scope, upload_batch_id=batch["id"]) if task["task_type"] == EMBEDDING_INDEX_TASK)
            self.assertEqual(TASK_COMPLETED, embedding["status"])
            self.assertEqual(["c1"], [chunk.id for chunk in vector.indexed])
            self.assertEqual(2, vector.replace_attempts)

    def test_stale_chunk_revision_is_canceled_before_side_effects(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = DocumentRepository(tmp / "metadata.sqlite3")
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(repo.db_path, repo.defaults))
            scope = kb_service.resolve_scope()
            service = make_service(tmp, repo, FakeVectorStore(tmp / "vectors"), FakeParser(), knowledge_base_service=kb_service)
            task_repo = ProcessingTaskRepository(repo.db_path, repo.defaults)
            queue = FakeAsyncQueue()
            worker = DocumentProcessingWorker(
                repository=task_repo,
                rag_service=service,
                config=DurableProcessingWorkerConfig(enabled=True, retry_backoff_seconds=(0,)),
                worker_id="async-worker",
                async_queue=queue,
                start_local_worker=False,
            )
            service.processing_worker = worker
            service.async_processing_queue = queue
            batch = service.create_upload_batch(scope)
            service.add_upload_batch_file(batch["id"], filename="manual.md", content=b"# Manual\n\nBody", scope=scope)
            service.start_upload_batch_processing(batch["id"], scope)
            self.assertTrue(worker.run_once())
            chunk_task = next(task for task in task_repo.list_tasks(scope, upload_batch_id=batch["id"]) if task["task_type"] == CHUNK_EXTRACT_TASK)
            document = repo.get_document(str(chunk_task["document_id"]), scope)
            metadata = dict(document["metadata_json"])
            metadata["source_revision"] = "newer-revision"
            repo.upsert_document(
                str(document["id"]),
                str(document["name"]),
                str(document["file_type"]),
                str(document["storage_path"]),
                str(document["parse_status"]),
                metadata,
                workspace_id=scope.workspace_id,
                knowledge_base_id=scope.knowledge_base_id,
            )

            self.assertTrue(worker.run_once())

            updated = task_repo.get_task(chunk_task["id"])
            self.assertEqual(TASK_CANCELED, updated["status"])
            self.assertEqual([], repo.list_chunks(doc_id=str(chunk_task["document_id"]), scope=scope))

    def test_worker_retries_then_dead_letters_failed_upload_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = DocumentRepository(tmp / "metadata.sqlite3")
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(repo.db_path, repo.defaults))
            scope = kb_service.resolve_scope()
            service = make_service(tmp, repo, FakeVectorStore(tmp / "vectors"), AlwaysFailParser(), knowledge_base_service=kb_service)
            task_repo = ProcessingTaskRepository(repo.db_path, repo.defaults)
            worker = DocumentProcessingWorker(
                repository=task_repo,
                rag_service=service,
                config=DurableProcessingWorkerConfig(enabled=True, retry_backoff_seconds=(0,), default_max_attempts=2),
                worker_id="test-worker",
            )
            service.processing_worker = worker

            batch = service.create_upload_batch(scope)
            service.add_upload_batch_file(batch["id"], filename="manual.md", content=b"# Manual\n\nBody", scope=scope)
            service.start_upload_batch_processing(batch["id"], scope)

            self.assertTrue(worker.run_once())
            task = task_repo.list_tasks(scope, upload_batch_id=batch["id"])[0]
            self.assertEqual(TASK_RETRYING, task["status"])
            self.assertTrue(worker.run_once())
            task = task_repo.list_tasks(scope, upload_batch_id=batch["id"])[0]
            self.assertEqual(TASK_DEAD_LETTERED, task["status"])
            self.assertEqual(1, len(task_repo.list_dead_letters(scope)))
            failed = service.get_upload_batch(batch["id"], scope)
            self.assertEqual("failed", failed["status"])
            self.assertEqual("failed", failed["files"][0]["status"])

    def test_cancel_upload_batch_cancels_queued_tasks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = DocumentRepository(tmp / "metadata.sqlite3")
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(repo.db_path, repo.defaults))
            scope = kb_service.resolve_scope()
            service = make_service(tmp, repo, FakeVectorStore(tmp / "vectors"), FakeParser(), knowledge_base_service=kb_service)
            task_repo = ProcessingTaskRepository(repo.db_path, repo.defaults)
            worker = DocumentProcessingWorker(
                repository=task_repo,
                rag_service=service,
                config=DurableProcessingWorkerConfig(enabled=True),
                worker_id="test-worker",
            )
            service.processing_worker = worker

            batch = service.create_upload_batch(scope)
            service.add_upload_batch_file(batch["id"], filename="manual.md", content=b"# Manual\n\nBody", scope=scope)
            service.start_upload_batch_processing(batch["id"], scope)
            service.cancel_upload_batch(batch["id"], scope)

            task = task_repo.list_tasks(scope, upload_batch_id=batch["id"])[0]
            self.assertEqual(TASK_CANCELED, task["status"])
            self.assertFalse(worker.run_once())

    def test_worker_recovers_stale_leased_upload_task(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = DocumentRepository(tmp / "metadata.sqlite3")
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(repo.db_path, repo.defaults))
            scope = kb_service.resolve_scope()
            service = make_service(tmp, repo, FakeVectorStore(tmp / "vectors"), FakeParser(), knowledge_base_service=kb_service)
            task_repo = ProcessingTaskRepository(repo.db_path, repo.defaults)
            stalled_worker = DocumentProcessingWorker(
                repository=task_repo,
                rag_service=service,
                config=DurableProcessingWorkerConfig(enabled=True, lease_timeout_seconds=0),
                worker_id="stalled-worker",
            )
            service.processing_worker = stalled_worker

            batch = service.create_upload_batch(scope)
            service.add_upload_batch_file(batch["id"], filename="manual.md", content=b"# Manual\n\nBody", scope=scope)
            service.start_upload_batch_processing(batch["id"], scope)
            claimed = task_repo.claim_next("stalled-worker", lease_seconds=0)
            self.assertIsNotNone(claimed)
            self.assertEqual(TASK_PROCESSING, claimed["status"])

            recovery_worker = DocumentProcessingWorker(
                repository=task_repo,
                rag_service=service,
                config=DurableProcessingWorkerConfig(enabled=True, retry_backoff_seconds=(0,)),
                worker_id="recovery-worker",
            )
            service.processing_worker = recovery_worker
            self.assertTrue(recovery_worker.run_once())
            task = task_repo.list_tasks(scope, upload_batch_id=batch["id"])[0]
            self.assertEqual(TASK_COMPLETED, task["status"])

    def test_list_documents_exposes_processing_runtime_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = DocumentRepository(tmp / "metadata.sqlite3")
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(repo.db_path, repo.defaults))
            scope = kb_service.resolve_scope()
            trace_recorder = ProcessingTraceRecorder.from_env(
                tmp / "traces",
                span_tracker=ProcessingSpanTracker(ProcessingSpanRepository(repo.db_path, repo.defaults)),
            )
            service = make_service(
                tmp,
                repo,
                FakeVectorStore(tmp / "vectors"),
                FakeParser(),
                knowledge_base_service=kb_service,
                processing_trace_recorder=trace_recorder,
            )
            task_repo = ProcessingTaskRepository(repo.db_path, repo.defaults)
            worker = DocumentProcessingWorker(
                repository=task_repo,
                rag_service=service,
                config=DurableProcessingWorkerConfig(enabled=True),
                worker_id="test-worker",
            )
            service.processing_worker = worker
            repo.upsert_document("doc-1", "manual.md", "md", "manual.md", "parsed")
            repo.update_enrichment(scope=scope, doc_id="doc-1", status="completed", summary="Short summary")
            _, attempt = trace_recorder.span_tracker.open_attempt(knowledge_id="doc-1", input={"file": "manual.md"})
            task = task_repo.create_task(
                "process_document",
                scope,
                document_id="doc-1",
                payload={"doc_id": "doc-1"},
                max_attempts=2,
            )
            task_repo.record_broker_dispatch(task["id"], broker_task_id="broker-1", queue_name="core")
            claimed = task_repo.claim_next("test-worker")
            self.assertEqual(task["id"], claimed["id"])
            task_repo.dead_letter(claimed["id"], error_code="PARSER", error_message="parser unavailable", worker_id="test-worker")

            document = service.list_documents(scope)[0]

            self.assertTrue(document["summary_available"])
            self.assertEqual(TASK_DEAD_LETTERED, document["processing_task_status"])
            self.assertEqual("process_document", document["processing_task_type"])
            self.assertEqual("core", document["processing_task_queue"])
            self.assertEqual("broker-1", document["processing_broker_task_id"])
            self.assertTrue(document["processing_dead_lettered"])
            self.assertEqual("parser unavailable", document["processing_last_error"])
            self.assertEqual("parser unavailable", document["processing_dead_letter_reason"])
            self.assertTrue(document["processing_retry_available"])
            self.assertEqual(1, document["processing_task_attempt"])
            self.assertEqual(2, document["processing_task_max_attempts"])
            self.assertEqual(attempt, document["processing_latest_attempt"])

            retried = service.retry_document_processing_task("doc-1", scope)

            self.assertEqual(TASK_RETRYING, retried["processing_task_status"])
            self.assertFalse(retried["processing_retry_available"])

    def test_runtime_status_prefers_active_wiki_task_over_later_completed_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = DocumentRepository(tmp / "metadata.sqlite3")
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(repo.db_path, repo.defaults))
            scope = kb_service.resolve_scope()
            trace_recorder = ProcessingTraceRecorder.from_env(
                tmp / "traces",
                span_tracker=ProcessingSpanTracker(ProcessingSpanRepository(repo.db_path, repo.defaults)),
            )
            service = make_service(
                tmp,
                repo,
                FakeVectorStore(tmp / "vectors"),
                FakeParser(),
                knowledge_base_service=kb_service,
                processing_trace_recorder=trace_recorder,
            )
            task_repo = ProcessingTaskRepository(repo.db_path, repo.defaults)
            worker = DocumentProcessingWorker(
                repository=task_repo,
                rag_service=service,
                config=DurableProcessingWorkerConfig(enabled=True),
                worker_id="test-worker",
            )
            service.processing_worker = worker
            repo.upsert_document("doc-1", "manual.md", "md", "manual.md", "parsed")
            repo.update_enrichment(scope=scope, doc_id="doc-1", status="completed", summary="Short summary")
            trace_recorder.span_tracker.open_attempt(knowledge_id="doc-1", input={"file": "manual.md"})

            for task_type, queue_name in (
                (UPLOAD_FILE_TASK, "core"),
                (CHUNK_EXTRACT_TASK, "core"),
                (EMBEDDING_INDEX_TASK, "core"),
                (KNOWLEDGE_POSTPROCESS_TASK, "postprocess"),
                (GRAPH_EXTRACTION_TASK, "enrichment"),
            ):
                task = task_repo.create_task(task_type, scope, document_id="doc-1", payload={"doc_id": "doc-1"})
                task_repo.record_broker_dispatch(task["id"], broker_task_id=f"broker-{task_type}", queue_name=queue_name)
                task_repo.complete(task["id"])
            wiki_task = task_repo.create_task(WIKI_INGEST_TASK, scope, document_id="doc-1", payload={"doc_id": "doc-1"})
            task_repo.record_broker_dispatch(wiki_task["id"], broker_task_id="broker-wiki", queue_name="wiki")
            task_repo.claim_task(wiki_task["id"], worker_id="wiki-worker")
            summary_task = task_repo.create_task(SUMMARY_GENERATION_TASK, scope, document_id="doc-1", payload={"doc_id": "doc-1"})
            task_repo.record_broker_dispatch(summary_task["id"], broker_task_id="broker-summary", queue_name="enrichment")
            task_repo.complete(summary_task["id"])

            document = service.list_documents(scope)[0]
            trace = service.get_document_processing_trace("doc-1", scope)
            stages = {stage["name"]: stage["status"] for stage in trace["trace"]["children"]}

            self.assertEqual(WIKI_INGEST_TASK, document["processing_task_type"])
            self.assertEqual(TASK_PROCESSING, document["processing_task_status"])
            self.assertEqual("wiki", document["processing_task_queue"])
            self.assertEqual("postprocess", trace["current_stage"])
            self.assertEqual("done", stages["embedding"])
            self.assertEqual("running", stages["postprocess"])
            self.assertEqual("running", trace["trace"]["status"])

    def test_retry_document_processing_redispatches_pending_task(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = DocumentRepository(tmp / "metadata.sqlite3")
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(repo.db_path, repo.defaults))
            scope = kb_service.resolve_scope()
            service = make_service(
                tmp,
                repo,
                FakeVectorStore(tmp / "vectors"),
                FakeParser(),
                knowledge_base_service=kb_service,
            )
            task_repo = ProcessingTaskRepository(repo.db_path, repo.defaults)
            queue = FakeAsyncQueue()
            worker = DocumentProcessingWorker(
                repository=task_repo,
                rag_service=service,
                config=DurableProcessingWorkerConfig(enabled=True),
                worker_id="test-worker",
                async_queue=queue,
                start_local_worker=False,
            )
            service.processing_worker = worker
            service.async_processing_queue = queue
            repo.upsert_document("doc-1", "manual.md", "md", "manual.md", "chunked")
            task = task_repo.create_task(
                EMBEDDING_INDEX_TASK,
                scope,
                document_id="doc-1",
                payload={"doc_id": "doc-1"},
                max_attempts=2,
            )

            retried = service.retry_document_processing_task("doc-1", scope)
            latest = task_repo.get_task(task["id"])

            self.assertEqual(TASK_PENDING, retried["processing_task_status"])
            self.assertTrue(retried["processing_retry_available"])
            self.assertEqual([task["id"]], [item["id"] for item in queue.tasks])
            self.assertEqual(f"broker-{task['id']}", latest["payload"]["_async_runtime"]["broker_task_id"])

    def test_delete_document_cancels_tasks_and_open_spans(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo = DocumentRepository(tmp / "metadata.sqlite3")
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(repo.db_path, repo.defaults))
            scope = kb_service.resolve_scope()
            trace_recorder = ProcessingTraceRecorder.from_env(
                tmp / "traces",
                span_tracker=ProcessingSpanTracker(ProcessingSpanRepository(repo.db_path, repo.defaults)),
            )
            service = make_service(
                tmp,
                repo,
                FakeVectorStore(tmp / "vectors"),
                FakeParser(),
                knowledge_base_service=kb_service,
                processing_trace_recorder=trace_recorder,
            )
            task_repo = ProcessingTaskRepository(repo.db_path, repo.defaults)
            worker = DocumentProcessingWorker(
                repository=task_repo,
                rag_service=service,
                config=DurableProcessingWorkerConfig(enabled=True),
                worker_id="test-worker",
            )
            service.processing_worker = worker
            repo.upsert_document("doc-1", "manual.md", "md", "manual.md", "parsing")
            task = task_repo.create_task("process_document", scope, document_id="doc-1", payload={"doc_id": "doc-1"})
            root, attempt = trace_recorder.span_tracker.open_attempt(knowledge_id="doc-1", input={"file": "manual.md"})
            self.assertIsNotNone(root)
            trace_recorder.span_tracker.begin_stage("doc-1", attempt, "docreader", {"parser": "fixture"})

            service.delete_document("doc-1", scope)

            self.assertEqual(TASK_CANCELED, task_repo.get_task(task["id"])["status"])
            tree = trace_recorder.span_tracker.latest_tree("doc-1")
            self.assertIsNotNone(tree)
            self.assertEqual("cancelled", tree["root"]["status"])


class AlwaysFailParser(FakeParser):
    def parse(self, file_path):
        raise RuntimeError("parser unavailable")


class FailOnceVectorStore(FakeVectorStore):
    def __init__(self, persist_dir):
        super().__init__(persist_dir)
        self.replace_attempts = 0

    def replace_document_chunks(self, doc_id, chunks, scope=None):
        self.replace_attempts += 1
        if self.replace_attempts == 1:
            raise RuntimeError("temporary embedding outage")
        return super().replace_document_chunks(doc_id, chunks, scope=scope)


if __name__ == "__main__":
    unittest.main()
