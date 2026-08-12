import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from time import sleep
from types import SimpleNamespace

from app.models.document_models import Chunk
from app.models.processing_config import DurableProcessingWorkerConfig
from app.schemas import WikiProcessingTaskResponse
from app.services.agent.agent_prompt_templates import PromptTemplateCatalog
from app.services.documents.document_repository import DocumentRepository
from app.services.knowledge.knowledge_base_repository import KnowledgeBaseRepository
from app.services.knowledge.knowledge_base_service import KnowledgeBaseService
from app.services.processing.processing_task_repository import ProcessingTaskRepository
from app.services.processing.processing_span_tracker import ProcessingSpanTracker
from app.services.processing.processing_worker import DocumentProcessingWorker, drain_worker
from app.services.wiki.wiki_ingest_service import (
    WikiCandidateOutput,
    WikiClassificationOutput,
    WikiFinalizePayload,
    WikiIngestConfig,
    WikiIngestPayload,
    WikiIngestService,
    WikiSupersededError,
)
from app.services.wiki.wiki_repository import WikiRepository
from app.services.wiki.wiki_service import WikiPageService, WikiValidationError
from tests.test_rag_service_structured_ingest import FakeParser, FakeVectorStore, make_service


class _FakeCompletions:
    def create(self, *, messages, **_kwargs):
        prompt = str(messages[-1]["content"])
        if "Candidate limit:" in prompt:
            payload = {
                "candidates": [
                    {"title": "ONU DH-P204", "slug": "onu-dh-p204", "page_type": "entity", "aliases": ["DH-P204"]},
                    {"title": "GPON", "slug": "gpon", "page_type": "concept", "aliases": [],},
                ]
            }
        elif "Return JSON only with keys summary and content_markdown" in prompt and "Current generated page" not in prompt:
            payload = {
                "summary": "DH-P204 is a managed GPON ONU with PoE and Wi-Fi 6 capabilities.",
                "content_markdown": "# DH-P204\n\n## Overview\n\nA managed GPON ONU with PoE and Wi-Fi 6.",
            }
        elif "Return {\"matches\"" in prompt:
            payload = {
                "matches": [
                    {"slug": "onu-dh-p204", "chunk_ids": ["chunk-overview"], "summary": "Managed ONU model."},
                    {"slug": "gpon", "chunk_ids": ["chunk-overview", "chunk-table"], "summary": "Supported access technology."},
                ]
            }
        elif "one coherent tree" in prompt:
            payload = {
                "assignments": [
                    {"slug": "onu-dh-p204", "path": ["网络设备", "光接入"]},
                    {"slug": "gpon", "path": ["网络技术", "无源光网络"]},
                ]
            }
        elif "key introduction" in prompt:
            payload = {"introduction": "This index is maintained automatically as Wiki pages change."}
        else:
            payload = {}
        message = SimpleNamespace(content=json.dumps(payload))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeLLMClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=_FakeCompletions())


class _InvalidJsonCompletions:
    def __init__(self):
        self.calls = 0
        self.temperatures = []

    def create(self, **kwargs):
        self.calls += 1
        self.temperatures.append(kwargs.get("temperature"))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="not-json"))])


class _FinalizationCompletions:
    def create(self, *, messages, **_kwargs):
        prompt = str(messages[-1]["content"])
        if "one coherent tree" in prompt:
            payload = {
                "assignments": [
                    {"slug": "optical-network-unit", "path": ["网络设备", "光接入"]},
                ]
            }
        elif "strict deduplication system" in prompt:
            payload = {
                "merges": {"optical-network-unit": "onu"},
                "reasons": {"optical-network-unit": "ONU is the established abbreviation for the same device."},
            }
        else:
            payload = {}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])


class _NetworkingChunker:
    def chunk(self, parsed):
        return [
            Chunk(
                "chunk-overview", parsed.doc_id, None, "parent", "ONU DH-P204",
                "DH-P204 is a managed GPON ONU with PoE and Wi-Fi 6 capabilities.",
                "DH-P204 is a managed GPON ONU with PoE and Wi-Fi 6 capabilities.",
                1, 1, 16, {},
            ),
            Chunk(
                "chunk-table", parsed.doc_id, None, "table", "GPON specifications",
                "| Interface | Value |\n| GPON | 1 |\n| PoE | 4 |",
                "| Interface | Value |\n| GPON | 1 |\n| PoE | 4 |",
                1, 1, 10, {},
            ),
        ]


class WikiIngestPipelineTests(unittest.TestCase):
    def test_finalization_is_idempotent_and_bounded_for_large_affected_page_sets(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metadata.sqlite3"
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(db_path))
            documents = DocumentRepository(db_path)
            repository = WikiRepository(db_path)
            page_service = WikiPageService(repository, kb_service, documents)
            scope = kb_service.resolve_scope([kb_service.create("Large Wiki", knowledge_base_type="wiki").id])
            service = WikiIngestService(
                repository=repository,
                page_service=page_service,
                processing_repository=ProcessingTaskRepository(db_path),
                llm_client=None,
                model="test",
                prompt_catalog=PromptTemplateCatalog.load_directory("config/prompt_templates"),
            )
            for index in range(105):
                repository.create_page(
                    scope,
                    slug=f"concept-{index:03d}",
                    title=f"网络概念 {index:03d}",
                    page_type="concept",
                    status="published",
                    summary=f"第 {index} 个网络概念。",
                )

            service._plan_taxonomy(scope)
            first = service._rebuild_index(scope, "large-run")
            duplicate = service._rebuild_index(scope, "large-run")

            self.assertEqual(first.version, duplicate.version)
            self.assertLessEqual(len(duplicate.content_markdown), service.config.max_source_chars)
            self.assertEqual(100, duplicate.content_markdown.count("[["))

    def test_multi_document_chinese_networking_corpus_converges_taxonomy_links_aliases_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metadata.sqlite3"
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(db_path))
            documents = DocumentRepository(db_path)
            repository = WikiRepository(db_path)
            page_service = WikiPageService(repository, kb_service, documents)
            scope = kb_service.resolve_scope([kb_service.create("PON 产品 Wiki", knowledge_base_type="wiki").id])
            service = WikiIngestService(
                repository=repository,
                page_service=page_service,
                processing_repository=ProcessingTaskRepository(db_path),
                llm_client=None,
                model="test",
                prompt_catalog=PromptTemplateCatalog.load_directory("config/prompt_templates"),
            )
            corpus = [
                ("doc-onu", "chunk-onu", "ONU 通过 GPON 连接 OLT，并提供 PoE 与 Wi-Fi 6。"),
                ("doc-olt", "chunk-olt", "OLT 统一管理 ONU，提供 GPON 接入与业务配置。"),
                ("doc-gpon", "chunk-gpon", "GPON 是 OLT 与 ONU 之间的无源光网络技术。"),
            ]
            for doc_id, chunk_id, text in corpus:
                documents.upsert_document(
                    id=doc_id, name=f"{doc_id}.txt", file_type="txt", storage_path=f"uploads/{doc_id}.txt",
                    parse_status="parsed", metadata_json={"chunks": 1},
                    workspace_id=scope.workspace_id, knowledge_base_id=scope.knowledge_base_id,
                )
                documents.replace_chunks(doc_id, [Chunk(chunk_id, doc_id, None, "parent", "PON", text, text, 1, 1, 12, {})], scope)

            def item(slug, title, page_type, text, doc_id, chunk_id, aliases):
                return {
                    "page_slug": slug, "title": title, "page_type": page_type,
                    "summary": text, "content_markdown": text, "aliases": aliases,
                    "source_refs": [{"doc_id": doc_id, "chunk_id": chunk_id, "title": doc_id}],
                    "source_chunk_ids": [chunk_id],
                }

            repository.replace_document_contributions(scope, document_id="doc-onu", document_revision="r1", generation_run_id="corpus-run", contributions=[
                item("onu", "ONU", "entity", corpus[0][2], "doc-onu", "chunk-onu", ["光网络单元", "光猫"]),
                item("gpon", "GPON", "concept", corpus[0][2], "doc-onu", "chunk-onu", ["吉比特无源光网络"]),
            ])
            repository.replace_document_contributions(scope, document_id="doc-olt", document_revision="r1", generation_run_id="corpus-run", contributions=[
                item("olt", "OLT", "entity", corpus[1][2], "doc-olt", "chunk-olt", ["光线路终端"]),
                item("gpon", "GPON", "concept", corpus[1][2], "doc-olt", "chunk-olt", ["吉比特无源光网络"]),
            ])
            repository.replace_document_contributions(scope, document_id="doc-gpon", document_revision="r1", generation_run_id="corpus-run", contributions=[
                item("gpon", "GPON", "concept", corpus[2][2], "doc-gpon", "chunk-gpon", ["吉比特无源光网络"]),
            ])
            for slug in ["gpon", "olt", "onu"]:
                self.assertTrue(service._reduce_slug(scope, slug, "corpus-run"))
            service._plan_taxonomy(scope)
            page_service.refresh_links(scope)
            versions = {slug: repository.get_page_by_slug(scope, slug).version for slug in ["gpon", "olt", "onu"]}
            for slug in ["gpon", "olt", "onu"]:
                self.assertTrue(service._reduce_slug(scope, slug, "corpus-run"))

            gpon = repository.get_page_by_slug(scope, "gpon")
            onu = repository.get_page_by_slug(scope, "onu")
            self.assertEqual({"doc-onu", "doc-olt", "doc-gpon"}, {ref.doc_id for ref in gpon.source_refs})
            self.assertIn("吉比特无源光网络", gpon.aliases)
            self.assertIn("光网络单元", onu.aliases)
            self.assertEqual(("概念",), gpon.category_path)
            self.assertEqual(("实体",), onu.category_path)
            self.assertTrue({"gpon", "olt"}.issubset(set(onu.out_links)))
            self.assertEqual(versions, {slug: repository.get_page_by_slug(scope, slug).version for slug in versions})

    def test_map_boundaries_cover_invalid_json_tables_ocr_sparse_text_limits_and_stale_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metadata.sqlite3"
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(db_path))
            documents = DocumentRepository(db_path)
            repository = WikiRepository(db_path)
            page_service = WikiPageService(repository, kb_service, documents)
            scope = kb_service.resolve_scope([kb_service.create("Map Wiki", knowledge_base_type="wiki").id])
            completions = _InvalidJsonCompletions()
            service = WikiIngestService(
                repository=repository,
                page_service=page_service,
                processing_repository=ProcessingTaskRepository(db_path),
                llm_client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
                model="test",
                prompt_catalog=PromptTemplateCatalog.load_directory("config/prompt_templates"),
                config=WikiIngestConfig(max_candidates=2, max_source_chars=1000, min_source_chars=50),
            )
            chunks = [
                {"id": "table-1", "chunk_type": "table", "title_path": "光网络单元", "content_markdown": "| 型号 | 端口 |\n| DH-P204 | 4 |"},
                {"id": "ocr-1", "chunk_type": "ocr", "title_path": "设备铭牌", "content": "DH-P204 GPON ONU"},
                {"id": "parent-1", "chunk_type": "parent", "title_path": "光网络单元", "content": "支持 PoE 与 Wi-Fi 6"},
            ]
            source_text, selected = service._source_text(chunks)
            candidates = service._extract_candidates("ONU 技术规格", source_text, selected)

            self.assertIn("[chunk:table-1]", source_text)
            self.assertIn("[chunk:ocr-1]", source_text)
            self.assertLessEqual(len(candidates), 2)
            self.assertEqual(len({item["slug"] for item in candidates}), len(candidates))
            self.assertEqual(6, completions.calls)
            self.assertEqual([0.3] * 6, completions.temperatures)

            documents.upsert_document(
                id="doc-sparse", name="稀疏.txt", file_type="txt", storage_path="uploads/sparse.txt", parse_status="parsed",
                metadata_json={"chunks": 1}, workspace_id=scope.workspace_id, knowledge_base_id=scope.knowledge_base_id,
            )
            documents.replace_chunks("doc-sparse", [Chunk("sparse-1", "doc-sparse", None, "parent", "", "x", "x", 1, 1, 1, {})], scope)
            sparse_document = documents.get_document("doc-sparse", scope)
            self.assertEqual([], service._map_document(scope, sparse_document, None))
            self.assertEqual(6, completions.calls)

            stale_task = {
                "workspace_id": scope.workspace_id,
                "knowledge_base_id": scope.knowledge_base_id,
                "document_id": "doc-sparse",
                "source_revision": "stale",
                "upload_batch_id": "run-stale",
                "payload_schema_version": 1,
                "payload": {"schema_version": 1, "document_id": "doc-sparse", "document_revision": "stale", "generation_run_id": "run-stale"},
            }
            with self.assertRaises(WikiSupersededError):
                service.process_ingest_task(stale_task)

    def test_finalization_preserves_manual_folder_and_creates_idempotent_duplicate_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metadata.sqlite3"
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(db_path))
            documents = DocumentRepository(db_path)
            repository = WikiRepository(db_path)
            page_service = WikiPageService(repository, kb_service, documents)
            scope = kb_service.resolve_scope([kb_service.create("Finalize Wiki", knowledge_base_type="wiki").id])
            folder = page_service.create_folder(scope, {"name": "用户分类"})
            page_service.create_page(scope, {"slug": "onu", "title": "ONU", "page_type": "entity", "status": "published", "aliases": ["光网络单元"]})
            page_service.move_page(scope, "onu", folder_id=folder.id, category_path=["用户分类"])
            page_service.create_page(scope, {"slug": "optical-network-unit", "title": "光网络单元", "page_type": "entity", "status": "published", "aliases": ["ONU"]})
            service = WikiIngestService(
                repository=repository,
                page_service=page_service,
                processing_repository=ProcessingTaskRepository(db_path),
                llm_client=SimpleNamespace(chat=SimpleNamespace(completions=_FinalizationCompletions())),
                model="test",
                prompt_catalog=PromptTemplateCatalog.load_directory("config/prompt_templates"),
            )

            service._plan_taxonomy(scope)
            service._lint_pages(scope)
            service._lint_pages(scope)

            manual = repository.get_page_by_slug(scope, "onu")
            automatic = repository.get_page_by_slug(scope, "optical-network-unit")
            issues = repository.list_issues(scope, status="open", limit=20)
            proposals = repository.list_proposals(scope, status="pending", limit=20)
            self.assertEqual(folder.id, manual.folder_id)
            self.assertEqual(("用户分类",), manual.category_path)
            self.assertEqual(("网络设备", "光接入"), automatic.category_path)
            self.assertEqual(1, len([issue for issue in issues if issue.issue_type == "duplicate_page"]))
            self.assertEqual(1, len([proposal for proposal in proposals if proposal.action == "merge_pages"]))

    def test_reduce_converges_multi_document_retractions_duplicates_and_deleted_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metadata.sqlite3"
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(db_path))
            documents = DocumentRepository(db_path)
            repository = WikiRepository(db_path)
            page_service = WikiPageService(repository, kb_service, documents)
            scope = kb_service.resolve_scope([kb_service.create("Convergence Wiki", knowledge_base_type="wiki").id])
            for doc_id, chunk_id in [("doc-1", "chunk-1"), ("doc-2", "chunk-2")]:
                documents.upsert_document(
                    id=doc_id, name=f"{doc_id}.md", file_type="md", storage_path=f"uploads/{doc_id}.md",
                    parse_status="parsed", metadata_json={"chunks": 1},
                    workspace_id=scope.workspace_id, knowledge_base_id=scope.knowledge_base_id,
                )
                documents.replace_chunks(
                    doc_id,
                    [Chunk(chunk_id, doc_id, None, "parent", "GPON", f"{doc_id} supports GPON", f"{doc_id} supports GPON", 1, 1, 4, {})],
                    scope,
                )
            service = WikiIngestService(
                repository=repository,
                page_service=page_service,
                processing_repository=ProcessingTaskRepository(db_path),
                llm_client=None,
                model="test",
                prompt_catalog=PromptTemplateCatalog.load_directory("config/prompt_templates"),
                config=WikiIngestConfig(min_source_chars=1),
            )

            def contribution(doc_id, chunk_id):
                return {
                    "page_slug": "gpon", "title": "GPON", "page_type": "concept",
                    "summary": f"Supported by {doc_id}", "content_markdown": f"Supported by {doc_id}",
                    "aliases": ["吉比特无源光网络"],
                    "source_refs": [{"doc_id": doc_id, "chunk_id": chunk_id, "title": doc_id}],
                    "source_chunk_ids": [chunk_id],
                }

            repository.replace_document_contributions(scope, document_id="doc-1", document_revision="r1", generation_run_id="run-1", contributions=[contribution("doc-1", "chunk-1")])
            repository.replace_document_contributions(scope, document_id="doc-2", document_revision="r1", generation_run_id="run-2", contributions=[contribution("doc-2", "chunk-2")])
            self.assertTrue(service._reduce_slug(scope, "gpon", "run-2"))
            merged = repository.get_page_by_slug(scope, "gpon")
            self.assertEqual({"doc-1", "doc-2"}, {ref.doc_id for ref in merged.source_refs})

            self.assertTrue(service._reduce_slug(scope, "gpon", "run-2"))
            self.assertEqual(merged.version, repository.get_page_by_slug(scope, "gpon").version)

            repository.replace_document_contributions(scope, document_id="doc-1", document_revision="r2", generation_run_id="run-3", contributions=[])
            self.assertTrue(service._reduce_slug(scope, "gpon", "run-3"))
            remaining = repository.get_page_by_slug(scope, "gpon")
            self.assertEqual({"doc-2"}, {ref.doc_id for ref in remaining.source_refs})

            documents.delete_document("doc-2", scope)
            self.assertFalse(service._reduce_slug(scope, "gpon", "run-4"))
            self.assertEqual("archived", repository.get_page_by_slug(scope, "gpon", include_archived=True).status)

    def test_combined_dense_and_wiki_ingest_writes_vectors_and_publishes_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "metadata.sqlite3"
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(db_path))
            document_repository = DocumentRepository(db_path)
            wiki_repository = WikiRepository(db_path)
            wiki_service = WikiPageService(wiki_repository, kb_service, document_repository)
            processing_repository = ProcessingTaskRepository(db_path)
            knowledge_base = kb_service.create(
                "Combined Wiki",
                knowledge_base_type="wiki",
                indexing_strategy={
                    "dense_enabled": True,
                    "keyword_enabled": False,
                    "graph_enabled": False,
                    "wiki_enabled": True,
                },
            )
            scope = kb_service.resolve_scope([knowledge_base.id])
            vector_store = FakeVectorStore(root / "vectors")
            ingest_service = WikiIngestService(
                repository=wiki_repository,
                page_service=wiki_service,
                processing_repository=processing_repository,
                llm_client=_FakeLLMClient(),
                model="test-model",
                prompt_catalog=PromptTemplateCatalog.load_directory("config/prompt_templates"),
                config=WikiIngestConfig(debounce_seconds=0, followup_seconds=0, min_source_chars=10),
            )
            rag_service = make_service(
                root,
                document_repository,
                vector_store,
                FakeParser(),
                knowledge_base_service=kb_service,
                chunker=_NetworkingChunker(),
            )
            worker = DocumentProcessingWorker(
                repository=processing_repository,
                rag_service=rag_service,
                wiki_ingest_service=ingest_service,
                config=DurableProcessingWorkerConfig(enabled=True, retry_backoff_seconds=(0,)),
                worker_id="combined-worker",
            )
            rag_service.processing_worker = worker
            rag_service.wiki_page_service = wiki_service
            file_path = root / "DH-P204.txt"
            file_path.write_text("DH-P204 is a managed GPON ONU with PoE and Wi-Fi 6 capabilities.", encoding="utf-8")

            result = rag_service.parse_and_index_document(file_path, scope=scope)
            processed = drain_worker(worker)
            pages, _ = wiki_repository.list_pages(scope, status="published", limit=20)

            self.assertGreater(result["indexed_chunks"], 0)
            self.assertTrue(vector_store.indexed)
            self.assertEqual(2, processed)
            self.assertTrue({"summary", "entity", "concept", "index", "log"}.issubset({page.page_type for page in pages}))

    def test_structured_map_outputs_normalize_chinese_slugs_aliases_and_citations(self):
        candidate = WikiCandidateOutput.from_value(
            {
                "title": "  光网络单元  ",
                "slug": "设备 / 光网络单元 ",
                "page_type": "ENTITY",
                "aliases": ["ONU", "ONU", " 光猫 "],
            }
        )
        citation = WikiClassificationOutput.from_value(
            {"slug": "设备/光网络单元", "chunk_ids": ["table-1", "table-1", "ocr-1"]},
            valid_slugs={candidate.slug},
            valid_chunk_ids={"table-1", "ocr-1"},
        )

        self.assertEqual("设备/光网络单元", candidate.slug)
        self.assertEqual(("ONU", "光猫"), candidate.aliases)
        self.assertEqual(("table-1", "ocr-1"), citation.chunk_ids)
        with self.assertRaises(WikiValidationError):
            WikiCandidateOutput.from_value({"title": "", "page_type": "other"})

    def test_reduce_runs_distinct_slugs_concurrently_and_serializes_same_slug(self):
        service = object.__new__(WikiIngestService)
        service.config = WikiIngestConfig(reduce_concurrency=2)
        service.span_tracker = ProcessingSpanTracker.disabled()
        service._page_lock_guard = Lock()
        service._page_locks = {}
        service._page_type_for_slug = lambda _scope, _slug: "concept"
        scope = SimpleNamespace(knowledge_base_id="kb-1")
        active = 0
        max_active = 0
        state_lock = Lock()

        def reduce_slug(_scope, _slug, _run_id):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            sleep(0.04)
            with state_lock:
                active -= 1
            return True

        service._reduce_slug = reduce_slug
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda slug: service._reduce_affected_slug(scope, slug, "run-1", None), ["a", "b"]))
        self.assertEqual(2, max_active)

        max_active = 0
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda slug: service._reduce_affected_slug(scope, slug, "run-1", None), ["same", "same"]))
        self.assertEqual(1, max_active)

    def test_typed_wiki_payloads_validate_schema_and_required_fields(self):
        ingest = WikiIngestPayload.from_task(
            {
                "payload_schema_version": 1,
                "document_id": "doc-1",
                "source_revision": "rev-1",
                "upload_batch_id": "run-1",
                "payload": {"schema_version": 1, "generation_task_id": "generation-1"},
            }
        )
        finalize = WikiFinalizePayload.from_task(
            {
                "payload_schema_version": 1,
                "upload_batch_id": "run-1",
                "payload": {
                    "schema_version": 1,
                    "document_ids": ["doc-1", ""],
                    "affected_slugs": ["ONU DH-P204"],
                },
            }
        )

        self.assertEqual("doc-1", ingest.document_id)
        self.assertEqual(("doc-1",), finalize.document_ids)
        self.assertEqual(("onu-dh-p204",), finalize.affected_slugs)
        with self.assertRaisesRegex(WikiValidationError, "schema_version"):
            WikiIngestPayload.from_task({"payload": {"schema_version": 2}})
        with self.assertRaisesRegex(WikiValidationError, "document_revision"):
            WikiIngestPayload.from_task(
                {"payload": {"schema_version": 1, "document_id": "doc-1", "generation_run_id": "run-1"}}
            )

    def test_enrichment_status_updates_do_not_supersede_the_current_source_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metadata.sqlite3"
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(db_path))
            document_repository = DocumentRepository(db_path)
            wiki_repository = WikiRepository(db_path)
            scope = kb_service.resolve_scope([kb_service.create("Stable revision Wiki", knowledge_base_type="wiki").id])
            document_repository.upsert_document(
                id="doc-stable",
                name="stable.txt",
                file_type="txt",
                storage_path="uploads/stable.txt",
                parse_status="parsed",
                metadata_json={
                    "processing_trace_id": "trace-source-v1",
                    "processing_version": "test-v1",
                    "chunks": 1,
                },
                workspace_id=scope.workspace_id,
                knowledge_base_id=scope.knowledge_base_id,
            )
            service = WikiIngestService(
                repository=wiki_repository,
                page_service=WikiPageService(wiki_repository, kb_service, document_repository),
                processing_repository=ProcessingTaskRepository(db_path),
                llm_client=None,
                model="test",
                prompt_catalog=PromptTemplateCatalog.load_directory("config/prompt_templates"),
            )
            before = service._document_revision(document_repository.get_document("doc-stable", scope))

            document_repository.update_enrichment("doc-stable", scope, status="pending")
            after = service._document_revision(document_repository.get_document("doc-stable", scope))

            self.assertEqual("trace-source-v1:test-v1:1", before)
            self.assertEqual(before, after)

    def test_wiki_only_ingest_publishes_content_index_and_log_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metadata.sqlite3"
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(db_path))
            document_repository = DocumentRepository(db_path)
            wiki_repository = WikiRepository(db_path)
            wiki_service = WikiPageService(wiki_repository, kb_service, document_repository)
            processing_repository = ProcessingTaskRepository(db_path)
            knowledge_base = kb_service.create("Networking Wiki", knowledge_base_type="wiki")
            scope = kb_service.resolve_scope([knowledge_base.id])
            self.assertFalse(knowledge_base.indexing_strategy.needs_embedding)

            document_repository.upsert_document(
                id="doc-onu",
                name="PON/DH-P204.txt",
                file_type="txt",
                storage_path="uploads/DH-P204.txt",
                parse_status="parsed",
                metadata_json={"processing_version": "test", "chunks": 2},
                workspace_id=scope.workspace_id,
                knowledge_base_id=scope.knowledge_base_id,
            )
            document_repository.replace_chunks(
                "doc-onu",
                [
                    Chunk(
                        "chunk-overview", "doc-onu", None, "parent", "ONU DH-P204",
                        "DH-P204 is a managed GPON ONU with four PoE ports and Wi-Fi 6.",
                        "DH-P204 is a managed GPON ONU with four PoE ports and Wi-Fi 6.",
                        1, 1, 16, {},
                    ),
                    Chunk(
                        "chunk-table", "doc-onu", None, "table", "GPON specifications",
                        "| Interface | Value |\n| GPON | 1 |\n| PoE | 4 |",
                        "| Interface | Value |\n| GPON | 1 |\n| PoE | 4 |",
                        1, 1, 10, {},
                    ),
                ],
                scope,
            )
            ingest_service = WikiIngestService(
                repository=wiki_repository,
                page_service=wiki_service,
                processing_repository=processing_repository,
                llm_client=_FakeLLMClient(),
                model="test-model",
                prompt_catalog=PromptTemplateCatalog.load_directory("config/prompt_templates"),
                config=WikiIngestConfig(debounce_seconds=0, followup_seconds=0, min_source_chars=10),
            )
            wiki_service.ingest_service = ingest_service
            worker = DocumentProcessingWorker(
                repository=processing_repository,
                rag_service=SimpleNamespace(),
                wiki_ingest_service=ingest_service,
                config=DurableProcessingWorkerConfig(enabled=True, default_max_attempts=1, retry_backoff_seconds=(0,)),
                worker_id="wiki-test-worker",
            )

            queued = ingest_service.enqueue_document(scope, "doc-onu", debounce_seconds=0)
            duplicate = ingest_service.enqueue_document(scope, "doc-onu", debounce_seconds=0)
            self.assertEqual("queued", queued["task"]["status"])
            self.assertEqual(queued["task"]["id"], duplicate["task"]["id"])
            self.assertEqual(queued["processing_task"]["id"], duplicate["processing_task"]["id"])
            self.assertEqual(1, len(wiki_repository.list_pending(scope)))
            WikiProcessingTaskResponse(**queued["processing_task"])
            self.assertTrue(worker.run_once())
            self.assertTrue(worker.run_once())
            third_run = worker.run_once()
            self.assertFalse(third_run, processing_repository.list_tasks(scope))

            pages, _ = wiki_repository.list_pages(scope, status="published", limit=20)
            by_type = {page.page_type: page for page in pages}
            self.assertTrue({"summary", "entity", "concept", "index", "log"}.issubset(by_type))
            self.assertEqual(("chunk-overview",), by_type["entity"].chunk_refs)
            self.assertEqual(("网络设备", "光接入"), by_type["entity"].category_path)
            self.assertEqual("completed", wiki_repository.get_generation_task(scope, queued["task"]["id"]).status)
            task_statuses = {task["task_type"]: task["status"] for task in processing_repository.list_tasks(scope)}
            self.assertEqual({"wiki.ingest": "completed", "wiki.finalize": "completed"}, task_statuses)
            overview = wiki_service.overview(scope)
            logs = wiki_service.list_logs(scope, limit=20)
            self.assertEqual(1, overview["page_counts"]["index"])
            self.assertEqual(0, overview["active_task_count"])
            self.assertEqual(["wiki_finalized", "document_ingested"], [item["event_type"] for item in logs["items"]])


if __name__ == "__main__":
    unittest.main()
