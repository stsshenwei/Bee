import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.models.document_models import Chunk
from app.services.documents.document_enrichment import (
    DocumentEnrichmentResult,
    DocumentEnrichmentService,
    OpenAIDocumentEnrichmentProvider,
    PromptBackedOpenAIDocumentEnrichmentProvider,
)
from app.services.agent.agent_prompt_templates import PromptTemplateCatalog
from app.services.documents.document_repository import DocumentRepository
from app.services.knowledge.knowledge_base_repository import KnowledgeBaseRepository
from app.services.knowledge.knowledge_base_service import KnowledgeBaseService
from app.services.processing.processing_task_repository import TASK_COMPLETED, TASK_RETRYING, ProcessingTaskRepository
from app.services.processing.processing_worker import DocumentProcessingWorker


class FakeProvider:
    model_ref = "summary-test"

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def generate(self, document_name, content, *, partial=False):
        self.calls.append({"name": document_name, "content": content, "partial": partial})
        if self.error:
            raise self.error
        return DocumentEnrichmentResult(
            summary=f"概要 {len(self.calls)}",
            keywords=["设备", "设备", "配置"],
            suggested_questions=["如何配置？", "如何配置？"],
        )


class DocumentEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "metadata.sqlite3"
        self.repository = DocumentRepository(self.path)
        self.knowledge_bases = KnowledgeBaseService(KnowledgeBaseRepository(self.path))
        self.scope = self.knowledge_bases.resolve_scope()
        self.repository.upsert_document("doc-1", "manual.md", "md", "manual.md", "parsed")
        self.chunks = [
            Chunk(
                "parent-1",
                "doc-1",
                None,
                "parent",
                "Guide",
                "配置说明",
                "配置说明",
                1,
                1,
                900,
                {"workspace_id": self.scope.workspace_id, "knowledge_base_id": self.scope.knowledge_base_id},
            ),
            Chunk(
                "child-1",
                "doc-1",
                "parent-1",
                "child",
                "Guide",
                "配置说明",
                "配置说明",
                1,
                1,
                50,
                {"workspace_id": self.scope.workspace_id, "knowledge_base_id": self.scope.knowledge_base_id},
            ),
        ]
        self.repository.replace_chunks("doc-1", self.chunks, self.scope)

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_persists_bounded_metadata_source_chunks_and_version(self):
        provider = FakeProvider()
        service = DocumentEnrichmentService(
            self.repository, provider, enabled=True, asynchronous=False
        )

        service.enqueue("doc-1", self.chunks, self.scope)
        document = self.repository.get_document("doc-1", self.scope)

        self.assertEqual("completed", document["summary_status"])
        self.assertEqual("概要 1", document["summary"])
        self.assertEqual(["设备", "配置"], document["keywords_json"])
        self.assertEqual(["如何配置？"], document["suggested_questions_json"])
        self.assertEqual(["parent-1"], document["summary_source_chunk_ids_json"])
        self.assertEqual(1, document["summary_version"])
        self.assertEqual("summary-test", document["summary_model_ref"])
        tasks = self.repository.list_enrichment_tasks("doc-1", self.scope)
        self.assertEqual(["completed"], [task["status"] for task in tasks])
        self.assertEqual(["parent-1"], tasks[0]["source_chunk_ids"])
        self.assertEqual(tasks[0]["id"], document["current_enrichment_task_id"])

    def test_disabled_or_missing_provider_keeps_document_searchable(self):
        service = DocumentEnrichmentService(self.repository, None, enabled=False, asynchronous=False)

        service.enqueue("doc-1", self.chunks, self.scope)
        document = self.repository.get_document("doc-1", self.scope)

        self.assertEqual("none", document["summary_status"])
        self.assertEqual("parsed", document["parse_status"])
        self.assertIsNotNone(self.repository.get_chunk("child-1", self.scope))

    def test_provider_failure_isolated_and_retry_does_not_reparse_chunks(self):
        provider = FakeProvider(error=TimeoutError("Authorization Bearer secret timed out"))
        service = DocumentEnrichmentService(
            self.repository, provider, enabled=True, asynchronous=False, max_retries=2
        )

        service.enqueue("doc-1", self.chunks, self.scope)
        failed = self.repository.get_document("doc-1", self.scope)
        before_chunks = self.repository.list_chunks("doc-1", scope=self.scope)
        provider.error = None
        service.retry("doc-1", self.scope)
        completed = self.repository.get_document("doc-1", self.scope)
        after_chunks = self.repository.list_chunks("doc-1", scope=self.scope)

        self.assertEqual("failed", failed["summary_status"])
        self.assertNotIn("secret", failed["summary_error"])
        self.assertEqual("parsed", failed["parse_status"])
        self.assertEqual("completed", completed["summary_status"])
        self.assertEqual(2, completed["summary_version"])
        tasks = self.repository.list_enrichment_tasks("doc-1", self.scope)
        self.assertEqual(["failed", "completed"], [task["status"] for task in tasks])
        self.assertEqual([row["id"] for row in before_chunks], [row["id"] for row in after_chunks])

    def test_long_document_uses_partial_batches_then_final_summary(self):
        second = Chunk(
            "parent-2",
            "doc-1",
            None,
            "parent",
            "Guide",
            "更多说明",
            "更多说明",
            2,
            2,
            900,
            self.chunks[0].metadata,
        )
        chunks = [self.chunks[0], second, self.chunks[1]]
        self.repository.replace_chunks("doc-1", chunks, self.scope)
        provider = FakeProvider()
        service = DocumentEnrichmentService(
            self.repository,
            provider,
            enabled=True,
            asynchronous=False,
            max_batch_tokens=1000,
        )

        service.enqueue("doc-1", chunks, self.scope)

        self.assertEqual(3, len(provider.calls))
        self.assertEqual([True, True, False], [call["partial"] for call in provider.calls])
        self.assertEqual(
            ["parent-1", "parent-2"],
            self.repository.get_document("doc-1", self.scope)["summary_source_chunk_ids_json"],
        )

    def test_cross_kb_document_cannot_receive_enrichment_result(self):
        other = self.knowledge_bases.create("其他")
        other_scope = self.knowledge_bases.resolve_scope([other.id])
        service = DocumentEnrichmentService(
            self.repository, FakeProvider(), enabled=True, asynchronous=False
        )

        with self.assertRaises(KeyError):
            service.retry("doc-1", other_scope)

    def test_openai_provider_rejects_invalid_json(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="not-json"))]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kwargs: response)
            )
        )
        provider = OpenAIDocumentEnrichmentProvider(client, "model")

        with self.assertRaises(Exception):
            provider.generate("manual.md", "content")

    def test_prompt_backed_openai_provider_uses_summary_template(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"summary":"ok","keywords":["GPON"],"suggested_questions":["支持什么？"]}'
                    )
                )
            ]
        )
        calls = []
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or response)
            )
        )
        catalog = PromptTemplateCatalog.load_directory("config/prompt_templates", required_ids={"generate_summary"})
        provider = PromptBackedOpenAIDocumentEnrichmentProvider(client, "model", prompt_catalog=catalog)

        result = provider.generate("manual.md", "GPON content")

        self.assertEqual("ok", result.summary)
        self.assertIn("Generate Summary", catalog.get("generate_summary").name)
        self.assertIn("manual.md", calls[0]["messages"][0]["content"])
        self.assertIn("GPON content", calls[0]["messages"][0]["content"])

    def test_async_runtime_routes_summary_generation_to_processing_queue(self):
        class FakeQueue:
            enabled = True

            def __init__(self):
                self.tasks = []

            def enqueue(self, task):
                self.tasks.append(task)
                return SimpleNamespace(broker_task_id=f"broker-{task['id']}", queue="enrichment")

        processing_repo = ProcessingTaskRepository(self.path)
        queue = FakeQueue()
        service = DocumentEnrichmentService(
            self.repository,
            FakeProvider(),
            enabled=True,
            asynchronous=True,
            processing_repository=processing_repo,
            async_queue=queue,
        )

        result = service.enqueue("doc-1", self.chunks, self.scope)
        tasks = processing_repo.list_tasks(self.scope, document_id="doc-1")

        self.assertIsNone(result)
        self.assertEqual(1, len(queue.tasks))
        self.assertEqual("summary.generation", queue.tasks[0]["task_type"])
        self.assertEqual(["summary.generation"], [task["task_type"] for task in tasks])
        self.assertEqual("enrichment", tasks[0]["payload"]["_async_runtime"]["queue_name"])
        self.assertEqual(["parent-1"], tasks[0]["payload"]["source_chunk_ids"])

    def test_worker_processes_summary_generation_task_from_repository_chunks(self):
        processing_repo = ProcessingTaskRepository(self.path)
        service = DocumentEnrichmentService(
            self.repository,
            FakeProvider(),
            enabled=True,
            asynchronous=False,
            processing_repository=processing_repo,
        )
        enrichment_task = self.repository.create_enrichment_task(
            "doc-1",
            self.scope,
            provider_ref="summary-test",
            source_chunk_ids=["parent-1"],
        )
        processing_repo.create_task(
            "summary.generation",
            self.scope,
            document_id="doc-1",
            payload={"schema_version": 1, "doc_id": "doc-1", "enrichment_task_id": enrichment_task["id"]},
            max_attempts=2,
        )
        worker = DocumentProcessingWorker(
            repository=processing_repo,
            rag_service=SimpleNamespace(document_enrichment_service=service),
        )

        self.assertTrue(worker.run_once())

        document = self.repository.get_document("doc-1", self.scope)
        processing_task = processing_repo.list_tasks(self.scope, document_id="doc-1")[0]
        self.assertEqual("completed", document["summary_status"])
        self.assertEqual(TASK_COMPLETED, processing_task["status"])

    def test_worker_retries_failed_summary_generation_task(self):
        processing_repo = ProcessingTaskRepository(self.path)
        service = DocumentEnrichmentService(
            self.repository,
            FakeProvider(error=TimeoutError("temporary provider outage")),
            enabled=True,
            asynchronous=False,
            processing_repository=processing_repo,
        )
        enrichment_task = self.repository.create_enrichment_task(
            "doc-1",
            self.scope,
            provider_ref="summary-test",
            source_chunk_ids=["parent-1"],
        )
        processing_repo.create_task(
            "summary.generation",
            self.scope,
            document_id="doc-1",
            payload={"schema_version": 1, "doc_id": "doc-1", "enrichment_task_id": enrichment_task["id"]},
            max_attempts=2,
        )
        worker = DocumentProcessingWorker(
            repository=processing_repo,
            rag_service=SimpleNamespace(document_enrichment_service=service),
        )

        self.assertTrue(worker.run_once())

        document = self.repository.get_document("doc-1", self.scope)
        processing_task = processing_repo.list_tasks(self.scope, document_id="doc-1")[0]
        self.assertEqual("failed", document["summary_status"])
        self.assertEqual(TASK_RETRYING, processing_task["status"])


if __name__ == "__main__":
    unittest.main()
