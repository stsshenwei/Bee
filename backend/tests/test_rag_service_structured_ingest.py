import tempfile
import json
import time
import unittest
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

from app.models.document_models import Chunk, ParsedDocument, ParsedElement, ParsedImage
from app.models.processing_config import PROCESSING_VERSION, DurableProcessingWorkerConfig, ProcessingRuntimeDefaults
from app.services.documents.document_chunker import DocumentChunker
from app.services.documents.document_repository import DocumentRepository
from app.services.documents.image_repository import ImageRepository
from app.services.knowledge.knowledge_base_repository import KnowledgeBaseRepository
from app.services.knowledge.knowledge_base_service import KnowledgeBaseService
from app.services.processing.processing_span_tracker import ProcessingSpanRepository, ProcessingSpanTracker
from app.services.processing.processing_task_repository import ProcessingTaskRepository
from app.services.processing.processing_trace import ProcessingTraceRecorder
from app.services.processing.processing_worker import DocumentProcessingWorker
from app.services.async_runtime.task_routes import route_for_task_type
from app.services.retrieval.rag_service import RAGService


class FakeParser:
    def __init__(self, ocr_error=None, images=None):
        self.ocr_error = ocr_error
        self.images = images or []
        self.ocr_calls = 0
        self.parse_calls = []

    def parse(self, file_path):
        self.parse_calls.append(Path(file_path).name)
        return ParsedDocument(
            doc_id="doc-1",
            file_name=file_path.name,
            file_type=file_path.suffix.lstrip("."),
            elements=[
                ParsedElement("e1", "title", "Manual", "# Manual", "", 1, 1, 1, "Manual", {}),
                ParsedElement("e2", "paragraph", "Body", "Body", "", 1, 1, None, "Manual", {}),
            ],
            images=list(self.images),
        )

    def extract_ocr_elements(self, file_path):
        self.ocr_calls += 1
        if self.ocr_error:
            raise self.ocr_error
        return [
            ParsedElement(
                element_id="ocr-1",
                type="image",
                text="ERR_CODE_42 from screenshot",
                markdown="ERR_CODE_42 from screenshot",
                html="",
                page_start=1,
                page_end=1,
                level=None,
                title_path="Manual",
                metadata={"parse_source": "docling_ocr", "provider": "docling", "confidence": 0.9},
            )
        ]


class FakeChunker:
    def chunk(self, parsed):
        return [
            Chunk("p1", parsed.doc_id, None, "parent", "Manual", "Full parent context", "Full parent context", 1, 1, 10, {}),
            Chunk("c1", parsed.doc_id, "p1", "child", "Manual", "Body", "Body", 1, 1, 3, {}),
        ]


class ManyChunker:
    parent_max_tokens = 600
    child_max_tokens = 100
    child_overlap_tokens = 10
    strategy = "recursive"

    def __init__(self, count):
        self.count = count

    def chunk(self, parsed):
        return [
            Chunk(
                f"c{index}", parsed.doc_id, None, "child", "Manual",
                f"Body {index}", f"Body {index}", 1, 1, 2,
                {"strategy": "recursive"},
            )
            for index in range(self.count)
        ]


class SelectiveFailParser(FakeParser):
    def __init__(self, fail_names=None):
        super().__init__()
        self.fail_names = set(fail_names or [])

    def parse(self, file_path):
        if Path(file_path).name in self.fail_names:
            self.parse_calls.append(Path(file_path).name)
            raise RuntimeError("parse failed for test fixture")
        return super().parse(file_path)


class SlowParser(FakeParser):
    def __init__(self, delay_seconds):
        super().__init__()
        self.delay_seconds = delay_seconds

    def parse(self, file_path):
        time.sleep(self.delay_seconds)
        return super().parse(file_path)


class ErrorParser(FakeParser):
    def __init__(self, message):
        super().__init__()
        self.message = message

    def parse(self, file_path):
        raise RuntimeError(self.message)


class PageCountParser(FakeParser):
    def __init__(self, page_count):
        super().__init__()
        self.page_count = page_count

    def parse(self, file_path):
        parsed = super().parse(file_path)
        return ParsedDocument(
            doc_id=parsed.doc_id,
            file_name=parsed.file_name,
            file_type=parsed.file_type,
            elements=parsed.elements,
            markdown=parsed.markdown,
            images=parsed.images,
            metadata={"page_count": self.page_count},
            diagnostics=parsed.diagnostics,
        )


class FakeVectorStore:
    def __init__(self, persist_dir):
        self.persist_dir = Path(persist_dir)
        self.indexed = []
        self.replaced_doc_ids = []
        self.replace_scopes = []

    def upsert_chunks(self, chunks):
        indexable = [chunk for chunk in chunks if chunk.chunk_type in {"child", "table", "ocr", "image_ocr", "image_caption"}]
        by_id = {chunk.id: chunk for chunk in self.indexed}
        for chunk in indexable:
            by_id[chunk.id] = chunk
        self.indexed = list(by_id.values())

    def replace_document_chunks(self, doc_id, chunks, scope=None):
        self.replaced_doc_ids.append(doc_id)
        self.replace_scopes.append(scope)
        self.indexed = [chunk for chunk in chunks if chunk.chunk_type in {"child", "table", "ocr", "image_ocr", "image_caption"}]

    def reset_collection(self):
        self.indexed = []

    def delete_document(self, doc_id, scope=None):
        self.replaced_doc_ids.append(doc_id)
        self.replace_scopes.append(scope)
        self.indexed = [chunk for chunk in self.indexed if chunk.doc_id != doc_id]

    def query(self, question, top_k):
        return []

    def count(self):
        return len(self.indexed)


class ReindexRequiredVectorStore(FakeVectorStore):
    def __init__(self, persist_dir):
        super().__init__(persist_dir)
        self.reset_required = True
        self.reset_calls = 0
        self.delete_calls = 0

    def reset_collection(self):
        self.reset_calls += 1
        self.reset_required = False
        super().reset_collection()

    def delete_knowledge_base(self, scope):
        self.delete_calls += 1


class FakeObjectStorage:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def put(self, data, *, suffix="", prefix="media"):
        key = f"{prefix}/generated{suffix}"
        self.objects[key] = data
        return key

    def read(self, key):
        return self.objects[key]

    def delete(self, key):
        self.objects.pop(key, None)

    def exists(self, key):
        return key in self.objects


class FakeOCRProvider:
    name = "fake-ocr"
    available = True

    def __init__(self, fail_payloads=None, delay_seconds=0.0):
        self.fail_payloads = set(fail_payloads or [])
        self.delay_seconds = delay_seconds
        self.calls = []
        self._lock = Lock()
        self._active = 0
        self.max_active = 0

    def extract_text(self, image, mime_type):
        from app.services.documents.multimodal_processing import MultimodalResult

        with self._lock:
            self.calls.append((image, mime_type))
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
            if image in self.fail_payloads:
                raise RuntimeError("ocr failed for fixture")
            return MultimodalResult(f"OCR:{image.decode('utf-8')}", self.name, 0.91)
        finally:
            with self._lock:
                self._active -= 1


class FakeCaptionProvider:
    name = "fake-caption"
    available = True

    def __init__(self):
        self.calls = []

    def describe(self, image, mime_type):
        from app.services.documents.multimodal_processing import MultimodalResult

        self.calls.append((image, mime_type))
        return MultimodalResult(f"Caption:{image.decode('utf-8')}", self.name, 0.82)


class FakeAsyncQueue:
    enabled = True

    def __init__(self):
        self.tasks = []

    def enqueue(self, task):
        self.tasks.append(task)
        return SimpleNamespace(broker_task_id=f"broker-{task['id']}", queue=route_for_task_type(str(task.get("task_type") or "")))

    def health(self):
        return {"enabled": True, "ok": True}


class ScopeCapturingKGService:
    def __init__(self):
        self.calls = []

    def enrich_document(self, doc_id, chunks, scope=None):
        self.calls.append({"doc_id": doc_id, "chunks": list(chunks), "scope": scope})


class ScopeCapturingEnrichmentService:
    enabled = True

    def __init__(self):
        self.calls = []

    def enqueue(self, doc_id, chunks, scope):
        self.calls.append({"doc_id": doc_id, "chunks": list(chunks), "scope": scope})


class ScopeCapturingWikiService:
    def __init__(self):
        self.calls = []

    def generate_draft_for_document(self, scope, doc_id):
        self.calls.append({"doc_id": doc_id, "scope": scope})
        return {"task": {"status": "completed"}, "page": None, "proposal": None}


def make_service(
    tmp,
    repo,
    vector,
    parser,
    *,
    ocr_enabled=False,
    chunker=None,
    knowledge_base_service=None,
    object_storage=None,
    ocr_provider_service=None,
    caption_provider_service=None,
    multimodal_max_workers=2,
    processing_defaults=None,
    kg_service=None,
    kg_enabled=False,
    document_enrichment_service=None,
    processing_trace_recorder=None,
):
    return RAGService(
        vector_store=vector,
        llm_client=SimpleNamespace(),
        chat_model="test",
        system_prompt="test",
        data_dir=tmp,
        top_k=3,
        min_relevance_score=0.0,
        chunk_size=600,
        chunk_overlap=80,
        ocr_enabled=ocr_enabled,
        document_repository=repo,
        knowledge_base_service=knowledge_base_service,
        document_parser=parser,
        document_chunker=chunker or FakeChunker(),
        object_storage=object_storage,
        ocr_provider_service=ocr_provider_service,
        caption_provider_service=caption_provider_service,
        multimodal_max_workers=multimodal_max_workers,
        processing_defaults=processing_defaults,
        kg_service=kg_service,
        kg_extraction_enabled=kg_enabled,
        document_enrichment_service=document_enrichment_service,
        processing_trace_recorder=processing_trace_recorder,
    )


class RAGServiceStructuredIngestTests(unittest.TestCase):
    def test_full_ingest_rejects_incompatible_collection_until_clean_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = ReindexRequiredVectorStore(Path(tmp) / "vector")
            service = make_service(tmp, repo, vector, FakeParser())
            Path(tmp, "manual.md").write_text("# Manual\nBody", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "clean-rebuild"):
                service.ingest()

            self.assertEqual(0, vector.reset_calls)
            self.assertEqual(0, vector.delete_calls)
            self.assertTrue(vector.reset_required)

    def test_parse_and_index_document_persists_parent_and_indexes_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            service = make_service(tmp, repo, vector, FakeParser())
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual\nBody", encoding="utf-8")

            result = service.parse_and_index_document(file_path)

            self.assertEqual("doc-1", result["doc_id"])
            self.assertEqual("parsed", repo.list_documents()[0]["parse_status"])
            self.assertEqual("parent", repo.get_chunk("p1")["chunk_type"])
            self.assertEqual("child", vector.indexed[0].chunk_type)
            self.assertEqual(["doc-1"], vector.replaced_doc_ids)

    def test_async_runtime_routes_graph_extraction_to_enrichment_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "rag.sqlite3"
            repo = DocumentRepository(db_path)
            vector = FakeVectorStore(Path(tmp) / "vector")
            kg_service = ScopeCapturingKGService()
            service = make_service(tmp, repo, vector, FakeParser(), kg_service=kg_service, kg_enabled=True)
            task_repo = ProcessingTaskRepository(db_path)
            queue = FakeAsyncQueue()
            service.async_processing_queue = queue
            service.processing_worker = DocumentProcessingWorker(
                repository=task_repo,
                rag_service=service,
                config=DurableProcessingWorkerConfig(enabled=True),
                async_queue=queue,
                start_local_worker=False,
            )
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual\nBody", encoding="utf-8")

            service.parse_and_index_document(file_path)

            tasks = task_repo.list_tasks(service.default_scope, document_id="doc-1")
            self.assertEqual([], kg_service.calls)
            self.assertEqual(["knowledge.post_process"], [task["task_type"] for task in tasks])
            self.assertEqual("postprocess", tasks[0]["payload"]["_async_runtime"]["queue_name"])
            self.assertEqual("doc-1", tasks[0]["payload"]["doc_id"])

    def test_postprocess_worker_fans_out_graph_task_to_enrichment_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "rag.sqlite3"
            repo = DocumentRepository(db_path)
            vector = FakeVectorStore(Path(tmp) / "vector")
            kg_service = ScopeCapturingKGService()
            service = make_service(tmp, repo, vector, FakeParser(), kg_service=kg_service, kg_enabled=True)
            task_repo = ProcessingTaskRepository(db_path)
            queue = FakeAsyncQueue()
            service.async_processing_queue = queue
            worker = DocumentProcessingWorker(
                repository=task_repo,
                rag_service=service,
                config=DurableProcessingWorkerConfig(enabled=True),
                async_queue=queue,
                start_local_worker=False,
            )
            service.processing_worker = worker
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual\nBody", encoding="utf-8")
            service.parse_and_index_document(file_path)

            self.assertTrue(worker.run_once())

            tasks = task_repo.list_tasks(service.default_scope, document_id="doc-1")
            by_type = {task["task_type"]: task for task in tasks}
            self.assertEqual({"knowledge.post_process", "graph.extraction"}, set(by_type))
            self.assertEqual("enrichment", by_type["graph.extraction"]["payload"]["_async_runtime"]["queue_name"])

    def test_async_runtime_routes_multimodal_provider_work_to_enrichment_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "rag.sqlite3"
            repo = DocumentRepository(db_path)
            vector = FakeVectorStore(Path(tmp) / "vector")
            parser = FakeParser(images=[ParsedImage("img-1", "media/img-1.jpg", "scanned_pdf", page_number=1)])
            object_storage = FakeObjectStorage({"media/img-1.jpg": b"image bytes"})
            ocr_provider = FakeOCRProvider()
            service = make_service(
                tmp,
                repo,
                vector,
                parser,
                ocr_enabled=True,
                object_storage=object_storage,
                ocr_provider_service=ocr_provider,
            )
            task_repo = ProcessingTaskRepository(db_path)
            queue = FakeAsyncQueue()
            service.async_processing_queue = queue
            service.processing_worker = DocumentProcessingWorker(
                repository=task_repo,
                rag_service=service,
                config=DurableProcessingWorkerConfig(enabled=True),
                async_queue=queue,
                start_local_worker=False,
            )
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual\nBody", encoding="utf-8")

            service.parse_and_index_document(file_path)

            tasks = task_repo.list_tasks(service.default_scope, document_id="doc-1")
            by_type = {task["task_type"]: task for task in tasks}
            self.assertEqual([], ocr_provider.calls)
            self.assertEqual({"image.multimodal", "knowledge.post_process"}, set(by_type))
            self.assertEqual("enrichment", by_type["image.multimodal"]["payload"]["_async_runtime"]["queue_name"])
            self.assertEqual("postprocess", by_type["knowledge.post_process"]["payload"]["_async_runtime"]["queue_name"])
            self.assertEqual(1, by_type["image.multimodal"]["payload"]["image_count"])

    def test_wiki_only_processing_persists_chunks_and_skips_vector_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "rag.sqlite3"
            repo = DocumentRepository(db_path)
            knowledge_bases = KnowledgeBaseService(KnowledgeBaseRepository(db_path))
            wiki = knowledge_bases.create("Wiki KB", knowledge_base_type="wiki")
            scope = knowledge_bases.resolve_scope([wiki.id])
            vector = FakeVectorStore(Path(tmp) / "vector")
            service = make_service(tmp, repo, vector, FakeParser(), knowledge_base_service=knowledge_bases)
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual\nBody", encoding="utf-8")

            result = service.parse_and_index_document(file_path, scope=scope)

            self.assertEqual(0, result["indexed_chunks"])
            self.assertGreater(result["persisted_chunks"], 0)
            self.assertEqual("parent", repo.get_chunk("p1", scope)["chunk_type"])
            self.assertEqual([], vector.indexed)
            trace = json.loads((Path(result["processing_trace_dir"]) / "trace.json").read_text(encoding="utf-8"))
            embedding = next(span for span in trace["spans"] if span["name"] == "index")
            self.assertEqual("skipped", embedding["status"])
            self.assertEqual("dense_and_keyword_disabled", embedding["output"]["skip_reason"])

    def test_processing_stages_follow_each_indexing_strategy_combination(self):
        cases = {
            "wiki-only": ({"dense_enabled": False, "keyword_enabled": False, "graph_enabled": False, "wiki_enabled": True}, False, False, True),
            "vector-only": ({"dense_enabled": True, "keyword_enabled": False, "graph_enabled": False, "wiki_enabled": False}, True, False, False),
            "keyword-only": ({"dense_enabled": False, "keyword_enabled": True, "graph_enabled": False, "wiki_enabled": False}, True, False, False),
            "graph-only": ({"dense_enabled": False, "keyword_enabled": False, "graph_enabled": True, "wiki_enabled": False}, False, True, False),
            "combined": ({"dense_enabled": True, "keyword_enabled": True, "graph_enabled": True, "wiki_enabled": True}, True, True, True),
        }

        for name, (strategy, expects_vectors, expects_graph, expects_wiki) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "rag.sqlite3"
                repo = DocumentRepository(db_path)
                knowledge_bases = KnowledgeBaseService(KnowledgeBaseRepository(db_path))
                knowledge_base = knowledge_bases.create(name, indexing_strategy=strategy)
                scope = knowledge_bases.resolve_scope([knowledge_base.id])
                vector = FakeVectorStore(Path(tmp) / "vector")
                graph = ScopeCapturingKGService()
                wiki = ScopeCapturingWikiService()
                service = make_service(
                    tmp,
                    repo,
                    vector,
                    FakeParser(),
                    knowledge_base_service=knowledge_bases,
                    kg_service=graph,
                    kg_enabled=False,
                )
                service.wiki_page_service = wiki
                file_path = Path(tmp) / "manual.md"
                file_path.write_text("# Manual\nBody", encoding="utf-8")

                result = service.parse_and_index_document(file_path, scope=scope)

                self.assertEqual(expects_vectors, bool(vector.indexed))
                self.assertEqual(expects_vectors, result["indexed_chunks"] > 0)
                self.assertEqual(expects_graph, bool(graph.calls))
                self.assertEqual(expects_wiki, bool(wiki.calls))
                self.assertGreater(result["persisted_chunks"], 0)

    def test_parse_and_index_document_writes_processing_trace_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            service = make_service(tmp, repo, vector, FakeParser())
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual\nBody", encoding="utf-8")

            result = service.parse_and_index_document(file_path)

            trace_dir = Path(result["processing_trace_dir"])
            self.assertTrue((trace_dir / "trace.json").exists())
            self.assertTrue((trace_dir / "parsed.md").exists())
            self.assertTrue((trace_dir / "chunks.jsonl").exists())
            self.assertTrue((trace_dir / "report.md").exists())
            self.assertTrue((trace_dir / "chunks_preview.md").exists())
            trace = json.loads((trace_dir / "trace.json").read_text(encoding="utf-8"))
            self.assertEqual("completed", trace["status"])
            self.assertEqual("doc-1", trace["doc_id"])
            self.assertIn("chunk_strategy", [span["name"] for span in trace["spans"]])
            self.assertIn("Full parent context", (trace_dir / "chunks.jsonl").read_text(encoding="utf-8"))
            self.assertIn("文档处理报告", (trace_dir / "report.md").read_text(encoding="utf-8"))
            self.assertIn("切片预览", (trace_dir / "chunks_preview.md").read_text(encoding="utf-8"))

    def test_parse_and_index_document_writes_processing_span_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "rag.sqlite3"
            repo = DocumentRepository(db_path)
            vector = FakeVectorStore(Path(tmp) / "vector")
            span_tracker = ProcessingSpanTracker(ProcessingSpanRepository(db_path))
            trace_recorder = ProcessingTraceRecorder.from_env(
                Path(tmp) / "processing_traces",
                span_tracker=span_tracker,
            )
            service = make_service(
                tmp,
                repo,
                vector,
                FakeParser(),
                processing_trace_recorder=trace_recorder,
            )
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual\nBody", encoding="utf-8")

            result = service.parse_and_index_document(file_path)
            trace = service.get_document_processing_trace(result["doc_id"])

            self.assertEqual(1, trace["current_attempt"])
            self.assertEqual("knowledge_processing", trace["trace"]["name"])
            stages = {stage["name"]: stage for stage in trace["trace"]["children"]}
            self.assertEqual({"docreader", "chunking", "embedding", "multimodal", "postprocess"}, set(stages))
            self.assertEqual("done", stages["docreader"]["status"])
            self.assertEqual("done", stages["chunking"]["status"])
            self.assertEqual("done", stages["embedding"]["status"])
            self.assertEqual("done", stages["multimodal"]["status"])
            self.assertEqual("done", stages["postprocess"]["status"])
            self.assertEqual(2, stages["chunking"]["output"]["chunk_count"])
            self.assertEqual(["parser_call"], [span["name"] for span in stages["docreader"]["children"]])
            self.assertEqual(["chunk_strategy_attempt"], [span["name"] for span in stages["chunking"]["children"]])
            self.assertEqual(["embedding_batch"], [span["name"] for span in stages["embedding"]["children"]])
            self.assertEqual(["multimodal_provider_calls"], [span["name"] for span in stages["multimodal"]["children"]])
            postprocess_children = {span["name"]: span for span in stages["postprocess"]["children"]}
            self.assertEqual({"graph_extraction", "wiki_enqueue"}, set(postprocess_children))
            self.assertEqual("skipped", postprocess_children["graph_extraction"]["status"])
            self.assertEqual("graph_disabled_by_strategy", postprocess_children["graph_extraction"]["output"]["skip_reason"])
            self.assertEqual("skipped", postprocess_children["wiki_enqueue"]["status"])
            self.assertEqual("wiki_disabled_by_strategy", postprocess_children["wiki_enqueue"]["output"]["skip_reason"])

    def test_parse_failure_writes_processing_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            service = make_service(tmp, repo, vector, ErrorParser("boom"))
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual\nBody", encoding="utf-8")

            with self.assertRaises(RuntimeError) as raised:
                service.parse_and_index_document(file_path)

            trace_dir = Path(getattr(raised.exception, "processing_trace_dir"))
            self.assertTrue((trace_dir / "trace.json").exists())
            self.assertTrue((trace_dir / "error.txt").exists())
            self.assertIn("RuntimeError: boom", (trace_dir / "error.txt").read_text(encoding="utf-8"))

    def test_parse_and_index_document_appends_ocr_elements_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            parser = FakeParser()
            service = make_service(tmp, repo, vector, parser, ocr_enabled=True, chunker=DocumentChunker())
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual\nBody", encoding="utf-8")

            service.parse_and_index_document(file_path)

            self.assertEqual(1, parser.ocr_calls)
            self.assertTrue(any(chunk.chunk_type == "ocr" for chunk in vector.indexed))

    def test_parse_and_index_document_skips_ocr_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            parser = FakeParser()
            service = make_service(tmp, repo, vector, parser, ocr_enabled=False, chunker=DocumentChunker())
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual\nBody", encoding="utf-8")

            service.parse_and_index_document(file_path)

            self.assertEqual(0, parser.ocr_calls)

    def test_parse_and_index_document_continues_when_ocr_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            parser = FakeParser(ocr_error=RuntimeError("ocr failed"))
            service = make_service(tmp, repo, vector, parser, ocr_enabled=True, chunker=DocumentChunker())
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual\nBody", encoding="utf-8")

            result = service.parse_and_index_document(file_path)

            self.assertEqual("doc-1", result["doc_id"])
            self.assertEqual(1, parser.ocr_calls)

    def test_processing_preview_enforces_file_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            service = make_service(
                tmp,
                repo,
                vector,
                FakeParser(),
                processing_defaults=ProcessingRuntimeDefaults(preview_max_file_bytes=4),
            )
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("12345", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "file size"):
                service.parse_document("manual.md")

    def test_processing_preview_enforces_page_runtime_and_response_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            defaults = ProcessingRuntimeDefaults(
                preview_max_pages=1,
                preview_timeout_seconds=0.01,
                preview_max_chunks=3,
            )
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual", encoding="utf-8")

            page_limited = make_service(tmp, repo, vector, PageCountParser(2), processing_defaults=defaults)
            with self.assertRaisesRegex(ValueError, "page count"):
                page_limited.parse_document("manual.md")

            slow = make_service(tmp, repo, vector, SlowParser(0.02), processing_defaults=defaults)
            with self.assertRaisesRegex(ValueError, "runtime limit"):
                slow.parse_document("manual.md")

            bounded = make_service(
                tmp,
                repo,
                vector,
                FakeParser(),
                chunker=ManyChunker(9),
                processing_defaults=defaults,
            )
            preview = bounded.parse_document("manual.md")
            self.assertEqual(9, preview["chunk_statistics"]["count"])
            self.assertEqual(3, len(preview["chunk_previews"]))

    def test_processing_preview_sanitizes_parser_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual", encoding="utf-8")
            service = make_service(
                tmp,
                repo,
                vector,
                ErrorParser(f"parser failed at {tmp}\\secret.pdf with token abc"),
            )

            with self.assertRaises(ValueError) as captured:
                service.parse_document("manual.md")
            self.assertIn("<data-dir>", str(captured.exception))
            self.assertNotIn(str(Path(tmp).resolve()), str(captured.exception))

    def test_save_uploaded_document_keeps_existing_single_file_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            service = make_service(tmp, repo, vector, FakeParser())

            result = service.save_uploaded_document(filename="manual.md", content=b"# Manual")

            self.assertEqual("uploads/manual.md", result["source"])
            self.assertEqual("manual.md", result["filename"])
            self.assertEqual("parsed", result["parse_status"])
            self.assertEqual(2, result["chunks"])
            self.assertTrue((Path(tmp) / "uploads" / "manual.md").exists())

    def test_save_uploaded_document_preserves_safe_nested_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            service = make_service(tmp, repo, vector, FakeParser())

            result = service.save_uploaded_document(
                filename="ignored.md",
                content=b"# Manual",
                relative_path="产品资料包/一级/安装 手册.md",
                batch_id="batch-001",
            )

            self.assertEqual("uploads/batch-001/产品资料包/一级/安装-手册.md", result["source"])
            self.assertEqual("安装-手册.md", result["filename"])
            self.assertEqual("uploads/batch-001/产品资料包/一级/安装-手册.md", repo.list_documents()[0]["storage_path"])
            self.assertTrue((Path(tmp) / "uploads" / "batch-001" / "产品资料包" / "一级" / "安装-手册.md").exists())

    def test_staged_upload_saves_file_without_parsing_until_confirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            parser = FakeParser()
            service = make_service(tmp, repo, vector, parser)
            scope = service.resolve_scope()

            batch = service.create_upload_batch(scope, {"chunk_size": 700})
            service.add_upload_batch_file(
                batch["id"],
                filename="manual.md",
                content=b"# Manual",
                relative_path="folder/manual.md",
                scope=scope,
            )
            pending = service.get_upload_batch(batch["id"], scope)

            self.assertEqual("ready_to_process", pending["status"])
            self.assertEqual([], parser.parse_calls)
            self.assertEqual([], vector.indexed)
            self.assertEqual([], repo.list_documents())
            self.assertTrue((Path(tmp) / "uploads" / batch["id"] / "folder" / "manual.md").exists())

            confirmed = service.confirm_upload_batch(batch["id"], scope)

            self.assertEqual("completed", confirmed["status"])
            self.assertEqual(["manual.md"], parser.parse_calls)
            self.assertEqual("completed", confirmed["files"][0]["status"])
            self.assertEqual("doc-1", confirmed["files"][0]["document_id"])
            self.assertEqual(2, confirmed["files"][0]["chunks"])
            self.assertEqual("parsed", repo.list_documents()[0]["parse_status"])

    def test_staged_upload_settings_report_requested_and_effective_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            service = make_service(tmp, repo, vector, FakeParser())
            scope = service.resolve_scope()

            batch = service.create_upload_batch(
                scope,
                {
                    "parser_engine": "missing-engine",
                    "chunk_strategy": "semantic",
                    "child_chunk_size_chars": 100,
                    "child_chunk_overlap_chars": 90,
                    "ocr_enabled": True,
                },
            )

            settings = batch["effective_settings"]
            self.assertEqual("missing-engine", settings["requested"]["parser_engine"])
            self.assertEqual("builtin", settings["effective"]["parser_engine"])
            self.assertEqual("auto", settings["effective"]["chunk_strategy"])
            self.assertEqual(50, settings["child_chunk_overlap_chars"])
            self.assertEqual("chars", settings["size_unit"])
            self.assertIn("parser_engine", settings["inactive_overrides"])
            self.assertIn("chunk_strategy", settings["inactive_overrides"])

    def test_staged_upload_creates_durable_image_operations_after_text_indexing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            parser = FakeParser(images=[ParsedImage("img-1", "media/img-1.jpg", "scanned_pdf", page_number=2)])
            service = make_service(
                tmp,
                repo,
                vector,
                parser,
                object_storage=FakeObjectStorage({"media/img-1.jpg": b"scan"}),
                ocr_provider_service=FakeOCRProvider(),
            )
            scope = service.resolve_scope()

            batch = service.create_upload_batch(scope, {"ocr_enabled": True, "ocr_provider": "fake"})
            service.add_upload_batch_file(batch["id"], filename="scan.md", content=b"# Manual", scope=scope)
            confirmed = service.confirm_upload_batch(batch["id"], scope)

            images = ImageRepository(repo.db_path).list_images("doc-1", scope)
            operations = ImageRepository(repo.db_path).list_operations("doc-1", scope)
            self.assertEqual("completed", confirmed["status"])
            self.assertEqual("child", vector.indexed[0].chunk_type)
            self.assertEqual(["img-1"], [image["id"] for image in images])
            self.assertEqual([("ocr", "completed")], [(item["operation_type"], item["status"]) for item in operations])
            self.assertEqual("fake", confirmed["effective_settings"]["effective"]["ocr_provider"])

    def test_multimodal_success_persists_evidence_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            parser = FakeParser(images=[ParsedImage("img-1", "media/img-1.jpg", "scanned_pdf", page_number=1)])
            service = make_service(
                tmp,
                repo,
                vector,
                parser,
                object_storage=FakeObjectStorage({"media/img-1.jpg": b"scan"}),
                ocr_provider_service=FakeOCRProvider(),
                caption_provider_service=FakeCaptionProvider(),
            )
            scope = service.resolve_scope()

            batch = service.create_upload_batch(
                scope,
                {
                    "ocr_enabled": True,
                    "ocr_provider": "fake",
                    "caption_enabled": True,
                    "caption_provider": "fake",
                },
            )
            service.add_upload_batch_file(batch["id"], filename="scan.md", content=b"# Manual", scope=scope)
            service.confirm_upload_batch(batch["id"], scope)

            chunks = repo.list_chunks(doc_id="doc-1", scope=scope)
            by_type = {chunk["chunk_type"]: chunk for chunk in chunks}
            operations = ImageRepository(repo.db_path).list_operations("doc-1", scope)
            self.assertIn("image_ocr", by_type)
            self.assertIn("image_caption", by_type)
            self.assertEqual("p1", by_type["image_ocr"]["parent_id"])
            self.assertEqual("img-1", by_type["image_ocr"]["metadata_json"]["image_id"])
            self.assertTrue(by_type["image_caption"]["metadata_json"]["generated_evidence"])
            self.assertEqual({"completed"}, {item["status"] for item in operations})
            self.assertTrue(any(chunk.chunk_type == "image_ocr" for chunk in vector.indexed))
            self.assertTrue(any(chunk.chunk_type == "image_caption" for chunk in vector.indexed))

    def test_multimodal_concurrency_limit_and_source_parent_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            images = [
                ParsedImage(f"img-{index}", f"media/img-{index}.jpg", "scanned_pdf", page_number=1)
                for index in range(4)
            ]
            storage = FakeObjectStorage({f"media/img-{index}.jpg": f"scan-{index}".encode("utf-8") for index in range(4)})
            ocr = FakeOCRProvider(delay_seconds=0.03)
            service = make_service(
                tmp,
                repo,
                vector,
                FakeParser(images=images),
                object_storage=storage,
                ocr_provider_service=ocr,
                multimodal_max_workers=2,
            )
            scope = service.resolve_scope()
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual", encoding="utf-8")

            service.parse_and_index_document(
                file_path,
                scope=scope,
                processing_settings={"ocr_enabled": True, "ocr_provider": "fake"},
            )

            image_chunks = [chunk for chunk in repo.list_chunks(doc_id="doc-1", scope=scope) if chunk["chunk_type"] == "image_ocr"]
            self.assertEqual(4, len(image_chunks))
            self.assertLessEqual(ocr.max_active, 2)
            self.assertEqual({"p1"}, {chunk["parent_id"] for chunk in image_chunks})
            self.assertTrue(all(chunk["metadata_json"]["source_type"] == "scanned_pdf" for chunk in image_chunks))
            self.assertTrue(all(chunk["metadata_json"]["workspace_id"] == scope.workspace_id for chunk in image_chunks))
            self.assertTrue(all(chunk["metadata_json"]["knowledge_base_id"] == scope.knowledge_base_id for chunk in image_chunks))

    def test_multimodal_failure_isolated_and_targeted_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            parser = FakeParser(
                images=[
                    ParsedImage("img-ok", "media/ok.jpg", "scanned_pdf", page_number=1),
                    ParsedImage("img-bad", "media/bad.jpg", "scanned_pdf", page_number=1),
                ]
            )
            ocr = FakeOCRProvider(fail_payloads={b"bad"})
            service = make_service(
                tmp,
                repo,
                vector,
                parser,
                object_storage=FakeObjectStorage({"media/ok.jpg": b"ok", "media/bad.jpg": b"bad"}),
                ocr_provider_service=ocr,
            )
            scope = service.resolve_scope()

            batch = service.create_upload_batch(scope, {"ocr_enabled": True, "ocr_provider": "fake"})
            service.add_upload_batch_file(batch["id"], filename="scan.md", content=b"# Manual", scope=scope)
            service.confirm_upload_batch(batch["id"], scope)

            operations = ImageRepository(repo.db_path).list_operations("doc-1", scope)
            failed = [item for item in operations if item["status"] == "failed"]
            completed = [item for item in operations if item["status"] == "completed"]
            self.assertEqual(1, len(failed))
            self.assertEqual(1, len(completed))
            self.assertEqual(1, len([chunk for chunk in repo.list_chunks(doc_id="doc-1", scope=scope) if chunk["chunk_type"] == "image_ocr"]))

            ocr.fail_payloads.clear()
            retried = service.retry_image_operation(failed[0]["id"], scope)

            self.assertEqual(1, retried["summary"]["completed"])
            self.assertEqual("completed", retried["operation"]["status"])
            self.assertEqual(2, retried["operation"]["attempt"])
            self.assertEqual(2, len([chunk for chunk in repo.list_chunks(doc_id="doc-1", scope=scope) if chunk["chunk_type"] == "image_ocr"]))

    def test_multimodal_retry_and_image_chunks_are_knowledge_base_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "rag.sqlite3"
            repo = DocumentRepository(db_path)
            knowledge_bases = KnowledgeBaseService(KnowledgeBaseRepository(db_path))
            custom = knowledge_bases.create("Custom KB")
            custom_scope = knowledge_bases.resolve_scope([custom.id])
            default_scope = knowledge_bases.resolve_scope()
            vector = FakeVectorStore(Path(tmp) / "vector")
            parser = FakeParser(images=[ParsedImage("img-1", "media/img-1.jpg", "scanned_pdf", page_number=1)])
            ocr = FakeOCRProvider(fail_payloads={b"scan"})
            service = make_service(
                tmp,
                repo,
                vector,
                parser,
                knowledge_base_service=knowledge_bases,
                object_storage=FakeObjectStorage({"media/img-1.jpg": b"scan"}),
                ocr_provider_service=ocr,
            )
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual", encoding="utf-8")
            service.parse_and_index_document(
                file_path,
                scope=custom_scope,
                processing_settings={"ocr_enabled": True, "ocr_provider": "fake"},
            )
            failed = ImageRepository(repo.db_path).list_operations("doc-1", custom_scope, status="failed")[0]

            with self.assertRaises(FileNotFoundError):
                service.retry_image_operation(failed["id"], default_scope)

            ocr.fail_payloads.clear()
            retried = service.retry_image_operation(failed["id"], custom_scope)
            self.assertEqual("completed", retried["operation"]["status"])
            self.assertEqual([], repo.list_chunks(doc_id="doc-1", chunk_types={"image_ocr"}, scope=default_scope))
            custom_chunks = repo.list_chunks(doc_id="doc-1", chunk_types={"image_ocr"}, scope=custom_scope)
            self.assertEqual(1, len(custom_chunks))
            self.assertEqual(custom.id, custom_chunks[0]["knowledge_base_id"])
            self.assertEqual(custom.id, vector.indexed[-1].metadata["knowledge_base_id"])

    def test_reparse_rebuilds_image_operations_without_duplicate_image_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            parser = FakeParser(images=[ParsedImage("img-1", "media/img-1.jpg", "scanned_pdf", page_number=1)])
            service = make_service(
                tmp,
                repo,
                vector,
                parser,
                object_storage=FakeObjectStorage({"media/img-1.jpg": b"scan"}),
                ocr_provider_service=FakeOCRProvider(),
            )
            scope = service.resolve_scope()
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual", encoding="utf-8")

            settings = {"ocr_enabled": True, "ocr_provider": "fake"}
            service.parse_and_index_document(file_path, scope=scope, processing_settings=settings)
            service.parse_and_index_document(file_path, scope=scope, processing_settings=settings)

            image_chunks = [chunk for chunk in repo.list_chunks(doc_id="doc-1", scope=scope) if chunk["chunk_type"] == "image_ocr"]
            operations = ImageRepository(repo.db_path).list_operations("doc-1", scope)
            self.assertEqual(1, len(image_chunks))
            self.assertEqual(1, len(operations))
            self.assertEqual("completed", operations[0]["status"])

    def test_delete_document_removes_image_resources_operations_and_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            storage = FakeObjectStorage({"media/img-1.jpg": b"scan"})
            parser = FakeParser(images=[ParsedImage("img-1", "media/img-1.jpg", "scanned_pdf", page_number=1)])
            service = make_service(
                tmp,
                repo,
                vector,
                parser,
                object_storage=storage,
                ocr_provider_service=FakeOCRProvider(),
            )
            scope = service.resolve_scope()
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual", encoding="utf-8")
            service.parse_and_index_document(
                file_path,
                scope=scope,
                processing_settings={"ocr_enabled": True, "ocr_provider": "fake"},
            )

            service.delete_document("doc-1", scope)

            self.assertFalse(storage.exists("media/img-1.jpg"))
            self.assertEqual([], ImageRepository(repo.db_path).list_operations("doc-1", scope))
            with self.assertRaises(KeyError):
                repo.delete_document("doc-1", scope)

    def test_staged_upload_partial_failure_keeps_completed_documents_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            parser = SelectiveFailParser(fail_names={"bad.md"})
            service = make_service(tmp, repo, vector, parser)
            scope = service.resolve_scope()

            batch = service.create_upload_batch(scope)
            service.add_upload_batch_file(batch["id"], filename="good.md", content=b"# Good", scope=scope)
            service.add_upload_batch_file(batch["id"], filename="bad.md", content=b"# Bad", scope=scope)

            result = service.confirm_upload_batch(batch["id"], scope)

            statuses = {item["original_name"]: item["status"] for item in result["files"]}
            errors = {item["original_name"]: item["error_message"] for item in result["files"]}
            self.assertEqual("partial_failed", result["status"])
            self.assertEqual("completed", statuses["good.md"])
            self.assertEqual("failed", statuses["bad.md"])
            self.assertIn("parse failed", errors["bad.md"])
            parsed_documents = [item for item in repo.list_documents() if item["parse_status"] == "parsed"]
            self.assertEqual(1, len(parsed_documents))
            self.assertEqual("good.md", parsed_documents[0]["name"])

    def test_staged_upload_reports_phases_errors_and_retry_eligibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            parser = SelectiveFailParser(fail_names={"bad.md"})
            service = make_service(tmp, repo, vector, parser)
            scope = service.resolve_scope()

            batch = service.create_upload_batch(scope)
            service.add_upload_batch_file(batch["id"], filename="good.md", content=b"# Good", scope=scope)
            service.add_upload_batch_file(batch["id"], filename="bad.md", content=b"# Bad", scope=scope)

            result = service.confirm_upload_batch(batch["id"], scope)

            files = {item["original_name"]: item for item in result["files"]}
            good_phases = {item["name"]: item for item in files["good.md"]["phases"]}
            bad_phases = {item["name"]: item for item in files["bad.md"]["phases"]}
            self.assertEqual("completed", good_phases["parse"]["status"])
            self.assertEqual("completed", good_phases["chunk"]["status"])
            self.assertEqual("completed", good_phases["index"]["status"])
            self.assertEqual("skipped", good_phases["multimodal"]["status"])
            self.assertFalse(files["good.md"]["retry_eligible"])
            self.assertEqual("failed", bad_phases["parse"]["status"])
            self.assertEqual("skipped", bad_phases["index"]["status"])
            self.assertTrue(bad_phases["parse"]["retry_eligible"])
            self.assertTrue(files["bad.md"]["retry_eligible"])
            self.assertIn("parse failed", files["bad.md"]["errors"][0])

    def test_staged_upload_reports_partial_multimodal_phase_without_file_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            parser = FakeParser(images=[ParsedImage("img-bad", "media/bad.jpg", "scanned_pdf", page_number=1)])
            service = make_service(
                tmp,
                repo,
                vector,
                parser,
                object_storage=FakeObjectStorage({"media/bad.jpg": b"bad"}),
                ocr_provider_service=FakeOCRProvider(fail_payloads={b"bad"}),
            )
            scope = service.resolve_scope()

            batch = service.create_upload_batch(scope, {"ocr_enabled": True, "ocr_provider": "fake"})
            service.add_upload_batch_file(batch["id"], filename="scan.md", content=b"# Manual", scope=scope)
            result = service.confirm_upload_batch(batch["id"], scope)

            file_task = result["files"][0]
            phases = {item["name"]: item for item in file_task["phases"]}
            self.assertEqual("completed", file_task["status"])
            self.assertFalse(file_task["retry_eligible"])
            self.assertEqual("partial_failed", phases["multimodal"]["status"])
            self.assertTrue(phases["multimodal"]["retry_eligible"])
            self.assertIn("ocr failed", phases["multimodal"]["errors"][0])

    def test_retry_failed_staged_file_reprocesses_only_failed_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            parser = SelectiveFailParser(fail_names={"bad.md"})
            service = make_service(tmp, repo, vector, parser)
            scope = service.resolve_scope()

            batch = service.create_upload_batch(scope)
            service.add_upload_batch_file(batch["id"], filename="bad.md", content=b"# Bad", scope=scope)
            failed = service.confirm_upload_batch(batch["id"], scope)
            file_id = failed["files"][0]["id"]
            parser.fail_names.clear()

            retried = service.retry_upload_batch_file(batch["id"], file_id, scope)

            self.assertEqual("completed", retried["status"])
            self.assertEqual("completed", retried["files"][0]["status"])
            self.assertEqual(["bad.md", "bad.md"], parser.parse_calls)
            self.assertEqual(1, len(repo.list_documents()))

    def test_staged_upload_processing_preserves_custom_knowledge_base_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "rag.sqlite3"
            repo = DocumentRepository(db_path)
            knowledge_bases = KnowledgeBaseService(KnowledgeBaseRepository(db_path))
            custom = knowledge_bases.create("Custom KB")
            custom_scope = knowledge_bases.resolve_scope([custom.id])
            vector = FakeVectorStore(Path(tmp) / "vector")
            service = make_service(
                tmp,
                repo,
                vector,
                FakeParser(),
                knowledge_base_service=knowledge_bases,
            )

            batch = service.create_upload_batch(custom_scope)
            service.add_upload_batch_file(batch["id"], filename="manual.md", content=b"# Manual", scope=custom_scope)
            confirmed = service.confirm_upload_batch(batch["id"], custom_scope)

            document = repo.list_documents(scope=custom_scope)[0]
            chunk = repo.get_chunk("c1", scope=custom_scope)
            self.assertEqual("completed", confirmed["status"])
            self.assertEqual(custom.id, document["knowledge_base_id"])
            self.assertEqual(custom.id, chunk["knowledge_base_id"])
            self.assertEqual(custom.id, vector.indexed[0].metadata["knowledge_base_id"])
            self.assertIs(vector.replace_scopes[0], custom_scope)

    def test_parse_index_uses_one_scope_and_processing_version_across_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "rag.sqlite3"
            repo = DocumentRepository(db_path)
            knowledge_bases = KnowledgeBaseService(KnowledgeBaseRepository(db_path))
            custom = knowledge_bases.create("Scoped KB", indexing_strategy={"graph_enabled": True})
            custom_scope = knowledge_bases.resolve_scope([custom.id])
            vector = FakeVectorStore(Path(tmp) / "vector")
            kg_service = ScopeCapturingKGService()
            enrichment_service = ScopeCapturingEnrichmentService()
            service = make_service(
                tmp,
                repo,
                vector,
                FakeParser(),
                knowledge_base_service=knowledge_bases,
                kg_service=kg_service,
                kg_enabled=True,
                document_enrichment_service=enrichment_service,
            )
            file_path = Path(tmp) / "manual.md"
            file_path.write_text("# Manual\nBody", encoding="utf-8")

            result = service.parse_and_index_document(
                file_path,
                scope=custom_scope,
                processing_settings={"parser_engine": "missing-engine", "chunk_strategy": "auto"},
            )

            document = repo.get_document("doc-1", scope=custom_scope)
            chunk = repo.get_chunk("c1", scope=custom_scope)
            vector_chunk = vector.indexed[0]
            self.assertEqual(PROCESSING_VERSION, result["processing_version"])
            self.assertEqual(PROCESSING_VERSION, result["effective_processing"]["processing_version"])
            self.assertEqual(PROCESSING_VERSION, document["metadata_json"]["processing_version"])
            self.assertEqual(PROCESSING_VERSION, document["metadata_json"]["processing"]["effective"]["processing_version"])
            self.assertEqual(PROCESSING_VERSION, chunk["metadata_json"]["processing_version"])
            self.assertEqual(PROCESSING_VERSION, vector_chunk.metadata["processing_version"])
            self.assertEqual("chars", chunk["metadata_json"]["size_unit"])
            self.assertEqual("builtin", chunk["metadata_json"]["effective_parser_engine"])
            self.assertEqual("missing-engine", chunk["metadata_json"]["requested_parser_engine"])
            self.assertEqual(custom.id, document["knowledge_base_id"])
            self.assertEqual(custom.id, chunk["knowledge_base_id"])
            self.assertEqual(custom.id, vector_chunk.metadata["knowledge_base_id"])
            self.assertIs(vector.replace_scopes[0], custom_scope)
            self.assertIs(kg_service.calls[0]["scope"], custom_scope)
            self.assertIs(enrichment_service.calls[0]["scope"], custom_scope)
            self.assertEqual(PROCESSING_VERSION, kg_service.calls[0]["chunks"][0].metadata["processing_version"])
            self.assertEqual(custom.id, enrichment_service.calls[0]["chunks"][0].metadata["knowledge_base_id"])

    def test_save_uploaded_document_rejects_unsafe_paths_and_extensions(self):
        bad_cases = [
            {"relative_path": "../manual.md", "batch_id": "batch-001"},
            {"relative_path": "/tmp/manual.md", "batch_id": "batch-001"},
            {"relative_path": "C:/tmp/manual.md", "batch_id": "batch-001"},
            {"relative_path": "", "batch_id": "batch-001"},
            {"relative_path": "nested/manual.exe", "batch_id": "batch-001"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            service = make_service(tmp, repo, vector, FakeParser())

            for case in bad_cases:
                with self.subTest(case=case):
                    with self.assertRaises(ValueError):
                        service.save_uploaded_document(filename="manual.md", content=b"# Manual", **case)

            self.assertFalse((Path(tmp) / "uploads").exists())

    def test_duplicate_uploaded_document_gets_unique_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            service = make_service(tmp, repo, vector, FakeParser())

            first = service.save_uploaded_document(filename="manual.md", content=b"# Manual")
            second = service.save_uploaded_document(filename="manual.md", content=b"# Manual")

            self.assertEqual("uploads/manual.md", first["source"])
            self.assertNotEqual(first["source"], second["source"])
            self.assertTrue(second["source"].startswith("uploads/manual_"))

    def test_failed_upload_does_not_remove_previous_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            vector = FakeVectorStore(Path(tmp) / "vector")
            service = make_service(tmp, repo, vector, FakeParser())

            success = service.save_uploaded_document(filename="manual.md", content=b"# Manual")
            with self.assertRaises(ValueError):
                service.save_uploaded_document(filename="bad.exe", content=b"bad")

            self.assertTrue((Path(tmp) / success["source"]).exists())
            self.assertEqual(1, len(repo.list_documents()))


if __name__ == "__main__":
    unittest.main()
