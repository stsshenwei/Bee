import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.models.document_models import Chunk
from app.models.knowledge_base import KnowledgeBaseScope
from app.services.documents.document_repository import DocumentRepository
from app.services.retrieval.rag_service import RAGService
from app.services.retrieval.query_understanding import (
    QueryUnderstandingConfig,
    QueryUnderstandingService,
)
from app.services.retrieval.retrieval_models import RetrievedChunk


class FakeVectorStore:
    def __init__(self):
        self.persist_dir = Path(tempfile.mkdtemp())
        self.items = []
        self.bm25_hits = []
        self.bm25_queries = []
        self.dense_hits = []
        self.dense_queries = []
        self.deleted_documents = []

    def count(self):
        return 0

    def query(self, question, top_k):
        self.dense_queries.append((question, top_k))
        return self.dense_hits[:top_k]

    def query_dense(self, question, top_k):
        return self.query(question, top_k)

    def query_bm25(self, question, top_k):
        self.bm25_queries.append((question, top_k))
        return self.bm25_hits[:top_k]

    def delete_document(self, doc_id):
        self.deleted_documents.append(doc_id)


class QueryAwareVectorStore(FakeVectorStore):
    def __init__(self, dense_by_query=None, bm25_by_query=None):
        super().__init__()
        self.dense_by_query = dense_by_query or {}
        self.bm25_by_query = bm25_by_query or {}

    def query(self, question, top_k):
        self.dense_queries.append((question, top_k))
        return list(self.dense_by_query.get(question, []))[:top_k]

    def query_bm25(self, question, top_k):
        self.bm25_queries.append((question, top_k))
        return list(self.bm25_by_query.get(question, []))[:top_k]


class FakeKeywordSearch:
    def __init__(self, hits=None, hits_by_query=None):
        self.hits = hits or []
        self.hits_by_query = hits_by_query or {}
        self.queries = []

    def search(self, query, top_k, filters=None):
        self.queries.append((query, top_k, filters))
        hits = self.hits_by_query.get(query, self.hits)
        return list(hits)[:top_k]


class FakeReranker:
    def __init__(self, scores=None, error=None):
        self.scores = scores or {}
        self.error = error

    def rerank(self, question, chunks, top_k):
        if self.error:
            raise self.error
        ranked = []
        for chunk in chunks:
            metadata = chunk.get("metadata", {})
            chunk_id = metadata.get("chunk_id") or metadata.get("child_id")
            ranked.append({**chunk, "reranker_score": self.scores.get(chunk_id, 0.0)})
        ranked.sort(key=lambda item: item["reranker_score"], reverse=True)
        return ranked[:top_k]


class FakeGraphRetriever:
    def __init__(self):
        self.calls = []

    def entity_search(self, question):
        self.calls.append(("entity_search", question))
        return None


class FakeStreamingCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        delta = SimpleNamespace(content="answer")
        choice = SimpleNamespace(delta=delta)
        return [SimpleNamespace(choices=[choice])]


class FakeStreamingClient:
    def __init__(self):
        self.completions = FakeStreamingCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


class HybridRetrievalTests(unittest.TestCase):
    def seed_document(self, repo):
        repo.upsert_document("doc-1", "manual.md", "md", "manual.md", "parsed")

    def make_service(self):
        service = RAGService(
            vector_store=FakeVectorStore(),
            llm_client=SimpleNamespace(),
            chat_model="test",
            system_prompt="test",
            data_dir=tempfile.mkdtemp(),
            top_k=3,
            min_relevance_score=0.0,
            chunk_size=80,
            chunk_overlap=10,
        )
        service.parent_store = {
            "p1": {
                "source": "a.md",
                "parent_index": 0,
                "text": "这是包含在线迁移和存储策略的完整父块。",
            },
            "p2": {
                "source": "b.md",
                "parent_index": 0,
                "text": "这是另一个关于备份恢复的完整父块。",
            },
        }
        return service

    def test_keyword_retrieve_hits_returns_child_matches_with_parent_id(self):
        service = self.make_service()
        service.keyword_items = [
            {
                "child_id": "c1",
                "parent_id": "p1",
                "source": "a.md",
                "child_text": "在线迁移适合主机维护",
            },
            {
                "child_id": "c2",
                "parent_id": "p2",
                "source": "b.md",
                "child_text": "备份恢复用于数据保护",
            },
        ]

        hits = service.keyword_retrieve_hits("在线迁移", top_k=2)

        self.assertEqual(1, len(hits))
        self.assertEqual("p1", hits[0]["metadata"]["parent_id"])
        self.assertGreater(hits[0]["keyword_score"], 0)

    def test_keyword_retrieve_uses_postgres_keyword_search(self):
        vector_store = FakeVectorStore()
        keyword_search = FakeKeywordSearch(
            [
                RetrievedChunk(
                    chunk_id="c1",
                    doc_id="doc-1",
                    parent_id="p1",
                    content="",
                    score=2.4,
                    bm25_score=2.4,
                )
            ]
        )
        service = RAGService(
            vector_store=vector_store,
            llm_client=SimpleNamespace(),
            chat_model="test",
            system_prompt="test",
            data_dir=tempfile.mkdtemp(),
            top_k=3,
            min_relevance_score=0.0,
            chunk_size=80,
            chunk_overlap=10,
            milvus_bm25_enabled=True,
            bm25_recall_top_n=50,
            keyword_search=keyword_search,
        )

        hits = service.keyword_retrieve_hits("ERR_CODE_42", top_k=5)

        self.assertEqual([], vector_store.bm25_queries)
        self.assertEqual([("ERR_CODE_42", 5)], [(query, top_k) for query, top_k, _ in keyword_search.queries])
        self.assertEqual("c1", hits[0]["metadata"]["chunk_id"])
        self.assertEqual(2.4, hits[0]["keyword_score"])

    def test_keyword_retrieve_uses_sqlite_fts_when_milvus_bm25_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            self.seed_document(repo)
            repo.replace_chunks(
                "doc-1",
                [
                    Chunk("p1", "doc-1", None, "parent", "CLI", "Parent context", "Parent context", 7, 7, 3, {"source": "manual.md"}),
                    Chunk("c1", "doc-1", "p1", "child", "CLI/Errors", "ERR_CODE_42 requires --timeout", "ERR_CODE_42 requires --timeout", 7, 7, 5, {"source": "manual.md"}),
                ],
            )
            service = RAGService(
                vector_store=FakeVectorStore(),
                llm_client=SimpleNamespace(),
                chat_model="test",
                system_prompt="test",
                data_dir=tmp,
                top_k=3,
                min_relevance_score=0.0,
                chunk_size=80,
                chunk_overlap=10,
                document_repository=repo,
            )
            service.keyword_items = [{"child_id": "legacy", "parent_id": "legacy-parent", "source": "legacy.md", "child_text": "ERR_CODE_42"}]

            hits = service.keyword_retrieve_hits("ERR_CODE_42 --timeout", top_k=5)

        self.assertEqual(["c1"], [hit["metadata"]["chunk_id"] for hit in hits])
        self.assertEqual("doc-1", hits[0]["metadata"]["doc_id"])
        self.assertEqual("p1", hits[0]["metadata"]["parent_id"])
        self.assertEqual("manual.md", hits[0]["metadata"]["source"])
        self.assertGreater(hits[0]["keyword_score"], 0)

    def test_recall_parent_hits_deduplicates_by_best_child_score(self):
        service = self.make_service()
        child_hits = [
            {
                "content": "在线迁移",
                "metadata": {"parent_id": "p1", "source": "a.md", "child_id": "c1"},
                "hybrid_score": 0.5,
            },
            {
                "content": "主机维护",
                "metadata": {"parent_id": "p1", "source": "a.md", "child_id": "c2"},
                "hybrid_score": 0.9,
            },
        ]

        parent_hits = service.recall_parent_hits(child_hits)

        self.assertEqual(1, len(parent_hits))
        self.assertEqual("这是包含在线迁移和存储策略的完整父块。", parent_hits[0]["content"])
        self.assertEqual(0.9, parent_hits[0]["hybrid_score"])
        self.assertEqual(["c2", "c1"], parent_hits[0]["metadata"]["matched_child_ids"])

    def test_recall_parent_hits_keeps_same_parent_id_separate_by_scope(self):
        service = self.make_service()
        workspace_id = service.default_scope.workspace_id
        scope = KnowledgeBaseScope(workspace_id=workspace_id, selected_knowledge_base_ids=("kb-a", "kb-b"))
        child_hits = [
            {
                "content": "same parent from kb a",
                "metadata": {
                    "parent_id": "p1",
                    "source": "a.md",
                    "child_id": "c-a",
                    "workspace_id": workspace_id,
                    "knowledge_base_id": "kb-a",
                },
                "hybrid_score": 0.8,
            },
            {
                "content": "same parent from kb b",
                "metadata": {
                    "parent_id": "p1",
                    "source": "b.md",
                    "child_id": "c-b",
                    "workspace_id": workspace_id,
                    "knowledge_base_id": "kb-b",
                },
                "hybrid_score": 0.7,
            },
        ]

        parent_hits = service.recall_parent_hits(child_hits, scope=scope)

        self.assertEqual(2, len(parent_hits))
        self.assertEqual([["c-a"], ["c-b"]], [hit["metadata"]["matched_child_ids"] for hit in parent_hits])
        self.assertEqual(["kb-a", "kb-b"], [hit["metadata"]["knowledge_base_id"] for hit in parent_hits])

    def test_recall_parent_hits_expands_short_parent_context_with_neighbors(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            self.seed_document(repo)
            repo.replace_chunks(
                "doc-1",
                [
                    Chunk("p0", "doc-1", None, "parent", "T", "Before", "Before", 1, 1, 1, {"source": "manual.md"}),
                    Chunk("p1", "doc-1", None, "parent", "T", "Middle", "Middle", 2, 2, 1, {"source": "manual.md"}),
                    Chunk("p2", "doc-1", None, "parent", "T", "After", "After", 3, 3, 1, {"source": "manual.md"}),
                    Chunk("c1", "doc-1", "p1", "child", "T", "needle", "needle", 2, 2, 1, {"source": "manual.md"}),
                ],
            )
            service = RAGService(
                vector_store=FakeVectorStore(),
                llm_client=SimpleNamespace(),
                chat_model="test",
                system_prompt="test",
                data_dir=tmp,
                top_k=3,
                min_relevance_score=0.0,
                chunk_size=80,
                chunk_overlap=10,
                document_repository=repo,
                context_short_chunk_min_chars=16,
                context_expanded_chunk_max_chars=80,
            )

            hits = service.recall_parent_hits(
                [
                    {
                        "content": "needle",
                        "metadata": {"chunk_id": "c1", "doc_id": "doc-1", "parent_id": "p1", "chunk_type": "child"},
                        "hybrid_score": 0.9,
                    }
                ]
            )

        self.assertIn("Before", hits[0]["content"])
        self.assertIn("Middle", hits[0]["content"])
        self.assertIn("After", hits[0]["content"])
        self.assertEqual(["p0", "p2"], hits[0]["metadata"]["expanded_neighbor_ids"])
        self.assertEqual(1, hits[0]["metadata"]["page_start"])
        self.assertEqual(3, hits[0]["metadata"]["page_end"])

    def test_context_assembly_merges_repeated_windows_from_same_document(self):
        service = self.make_service()
        scope = service.default_scope
        hits = [
            {
                "content": "alpha beta gamma",
                "metadata": {"doc_id": "doc-1", "parent_id": "p1", "matched_child_ids": ["c1"]},
                "hybrid_score": 0.7,
            },
            {
                "content": "alpha beta gamma",
                "metadata": {"doc_id": "doc-1", "parent_id": "p2", "matched_child_ids": ["c2"]},
                "hybrid_score": 0.6,
            },
        ]

        merged = service._assemble_final_context_hits(hits, scope)

        self.assertEqual(1, len(merged))
        self.assertEqual(["c1", "c2"], merged[0]["metadata"]["matched_child_ids"])

    def test_extract_sources_includes_trace_metadata(self):
        service = self.make_service()

        sources = service.extract_sources(
            [
                {
                    "distance": 0.2,
                    "metadata": {
                        "source": "manual.pdf",
                        "doc_id": "doc-1",
                        "chunk_id": "c1",
                        "parent_id": "p1",
                        "title_path": "Install/Errors",
                        "page_start": 4,
                        "page_end": 5,
                        "matched_child_ids": ["c1", "c2"],
                    },
                }
            ]
        )

        self.assertEqual("doc-1", sources[0]["doc_id"])
        self.assertEqual("c1", sources[0]["chunk_id"])
        self.assertEqual("p1", sources[0]["parent_id"])
        self.assertEqual("Install/Errors", sources[0]["title_path"])
        self.assertEqual(4, sources[0]["page_start"])
        self.assertEqual(5, sources[0]["page_end"])
        self.assertEqual(["c1", "c2"], sources[0]["matched_child_ids"])

    def test_hybrid_retrieve_uses_dense_and_keyword_fanout_with_rrf(self):
        vector_store = FakeVectorStore()
        vector_store.dense_hits = [
            {"content": "", "metadata": {"chunk_id": "shared", "parent_id": "p1"}, "distance": 0.1, "vector_score": 0.9},
            {"content": "", "metadata": {"chunk_id": "dense-only", "parent_id": "p2"}, "distance": 0.2, "vector_score": 0.8},
        ]
        keyword_search = FakeKeywordSearch(
            [
                RetrievedChunk("shared", "", "p1", content="", score=3.0, bm25_score=3.0),
                RetrievedChunk("bm25-only", "", "p3", content="", score=2.0, bm25_score=2.0),
            ]
        )
        service = RAGService(
            vector_store=vector_store,
            llm_client=SimpleNamespace(),
            chat_model="test",
            system_prompt="test",
            data_dir=tempfile.mkdtemp(),
            top_k=3,
            min_relevance_score=0.0,
            chunk_size=80,
            chunk_overlap=10,
            milvus_bm25_enabled=True,
            dense_recall_top_n=50,
            bm25_recall_top_n=50,
            fusion_top_k=30,
            keyword_search=keyword_search,
        )

        hits = service.hybrid_retrieve_hits("question")

        self.assertEqual([("question", 50)], vector_store.dense_queries)
        self.assertEqual([], vector_store.bm25_queries)
        self.assertEqual([("question", 50)], [(query, top_k) for query, top_k, _ in keyword_search.queries])
        self.assertEqual(["shared", "dense-only", "bm25-only"], [hit["metadata"]["chunk_id"] for hit in hits])
        self.assertGreater(hits[0]["hybrid_score"], hits[1]["hybrid_score"])
        self.assertEqual(0.9, hits[0]["vector_score"])
        self.assertEqual(3.0, hits[0]["bm25_score"])
        self.assertAlmostEqual(0.7 / 61, hits[0]["vector_contribution"], places=6)
        self.assertAlmostEqual(0.3 / 61, hits[0]["keyword_contribution"], places=6)
        self.assertEqual(1, hits[0]["dense_rank"])
        self.assertEqual(1, hits[0]["keyword_rank"])

    def test_hybrid_retrieve_keeps_single_channel_order_and_score_metadata(self):
        service = self.make_service()
        hits = service._fuse_retrieval_hits(
            [],
            [
                {"content": "first", "metadata": {"chunk_id": "k1"}, "keyword_score": 4.0, "bm25_score": 4.0},
                {"content": "second", "metadata": {"chunk_id": "k2"}, "keyword_score": 3.0, "bm25_score": 3.0},
            ],
        )

        self.assertEqual(["k1", "k2"], [hit["metadata"]["chunk_id"] for hit in hits])
        self.assertEqual([4.0, 3.0], [hit["bm25_score"] for hit in hits])
        self.assertEqual([1, 2], [hit["keyword_rank"] for hit in hits])
        self.assertEqual([0.0, 0.0], [hit["vector_score"] for hit in hits])

    def test_hybrid_retrieve_uses_llm_query_variants_and_deduplicates(self):
        vector_store = FakeVectorStore()

        class RewriteClient:
            def rewrite(self, query, understanding):
                return {"queries": ["8个RJ-45", "8个RJ45"]}

        def query_dense(question, top_k):
            vector_store.dense_queries.append((question, top_k))
            if "RJ-45" in question:
                return [
                    {"content": "", "metadata": {"chunk_id": "rj45", "parent_id": "p1"}, "distance": 0.1},
                    {"content": "", "metadata": {"chunk_id": "shared", "parent_id": "p2"}, "distance": 0.2},
                ]
            return [{"content": "", "metadata": {"chunk_id": "shared", "parent_id": "p2"}, "distance": 0.3}]

        vector_store.query_dense = query_dense
        keyword_search = FakeKeywordSearch(
            hits_by_query={
                "8个RJ45": [RetrievedChunk("rj45", "", "p1", content="", score=4.0, bm25_score=4.0)]
            }
        )
        query_understanding = QueryUnderstandingService(
            rewrite_client=RewriteClient(),
            config=QueryUnderstandingConfig(enabled=True, rewrite_enabled=True, max_queries=3),
        )
        service = RAGService(
            vector_store=vector_store,
            llm_client=SimpleNamespace(),
            chat_model="test",
            system_prompt="test",
            data_dir=tempfile.mkdtemp(),
            top_k=3,
            min_relevance_score=0.0,
            chunk_size=80,
            chunk_overlap=10,
            milvus_bm25_enabled=True,
            dense_recall_top_n=50,
            bm25_recall_top_n=50,
            fusion_top_k=30,
            retrieval_debug_enabled=True,
            query_understanding=query_understanding,
            keyword_search=keyword_search,
        )

        hits = service.hybrid_retrieve_hits("8个电口")

        dense_queries = [query for query, _ in vector_store.dense_queries]
        keyword_queries = [query for query, _, _ in keyword_search.queries]
        self.assertIn("8个电口", dense_queries)
        self.assertIn("8个RJ-45", dense_queries)
        self.assertEqual([], vector_store.bm25_queries)
        self.assertIn("8个RJ45", keyword_queries)
        self.assertEqual({"rj45", "shared"}, {hit["metadata"]["chunk_id"] for hit in hits})
        self.assertEqual(1, len([hit for hit in hits if hit["metadata"]["chunk_id"] == "rj45"]))
        self.assertIn("query_understanding", service._last_retrieval_debug)
        self.assertEqual("llm", service._last_retrieval_debug["query_understanding"]["source"])

    def test_hybrid_retrieve_reranker_reorders_when_enabled(self):
        vector_store = FakeVectorStore()
        vector_store.dense_hits = [
            {"content": "", "metadata": {"chunk_id": "a", "parent_id": "p1"}, "distance": 0.1, "vector_score": 0.9},
            {"content": "", "metadata": {"chunk_id": "b", "parent_id": "p2"}, "distance": 0.2, "vector_score": 0.8},
        ]
        service = RAGService(
            vector_store=vector_store,
            llm_client=SimpleNamespace(),
            chat_model="test",
            system_prompt="test",
            data_dir=tempfile.mkdtemp(),
            top_k=2,
            min_relevance_score=0.0,
            chunk_size=80,
            chunk_overlap=10,
            reranker_enabled=True,
            reranker_top_n=2,
            reranker_threshold=0.0,
            reranker=FakeReranker({"b": 0.99, "a": 0.1}),
        )

        hits = service.hybrid_retrieve_hits("question")

        self.assertEqual(["b", "a"], [hit["metadata"]["chunk_id"] for hit in hits])
        self.assertEqual(0.99, hits[0]["reranker_score"])

    def test_hybrid_retrieve_filters_reranked_candidates_by_threshold(self):
        vector_store = FakeVectorStore()
        vector_store.dense_hits = [
            {"content": "a", "metadata": {"chunk_id": "a", "parent_id": "p1"}, "distance": 0.1, "vector_score": 0.9},
            {"content": "b", "metadata": {"chunk_id": "b", "parent_id": "p2"}, "distance": 0.2, "vector_score": 0.8},
        ]
        service = RAGService(
            vector_store=vector_store,
            llm_client=SimpleNamespace(),
            chat_model="test",
            system_prompt="test",
            data_dir=tempfile.mkdtemp(),
            top_k=2,
            min_relevance_score=0.0,
            chunk_size=80,
            chunk_overlap=10,
            reranker_enabled=True,
            reranker_top_n=2,
            reranker_threshold=0.3,
            reranker=FakeReranker({"a": 0.7, "b": 0.2}),
        )

        hits = service.hybrid_retrieve_hits("question")

        self.assertEqual(["a"], [hit["metadata"]["chunk_id"] for hit in hits])
        self.assertEqual(1, service._last_retrieval_debug["rerank"]["filtered_count"])

    def test_hybrid_retrieve_keeps_top_reranked_fallback_candidate(self):
        vector_store = FakeVectorStore()
        vector_store.dense_hits = [
            {"content": "a", "metadata": {"chunk_id": "a", "parent_id": "p1"}, "distance": 0.1, "vector_score": 0.9},
            {"content": "b", "metadata": {"chunk_id": "b", "parent_id": "p2"}, "distance": 0.2, "vector_score": 0.8},
        ]
        service = RAGService(
            vector_store=vector_store,
            llm_client=SimpleNamespace(),
            chat_model="test",
            system_prompt="test",
            data_dir=tempfile.mkdtemp(),
            top_k=2,
            min_relevance_score=0.0,
            chunk_size=80,
            chunk_overlap=10,
            reranker_enabled=True,
            reranker_top_n=2,
            reranker_threshold=0.8,
            reranker_fallback_min_score=0.15,
            reranker=FakeReranker({"a": 0.2, "b": 0.1}),
        )

        hits = service.hybrid_retrieve_hits("question")

        self.assertEqual(["a"], [hit["metadata"]["chunk_id"] for hit in hits])
        self.assertTrue(service._last_retrieval_debug["rerank"]["fallback_used"])

    def test_hybrid_retrieve_hydrates_milvus_hit_content_before_reranking(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            self.seed_document(repo)
            repo.replace_chunks(
                "doc-1",
                [
                    Chunk("p1", "doc-1", None, "parent", "Guide", "Parent", "Parent", 1, 1, 1, {"source": "guide.md"}),
                    Chunk("c1", "doc-1", "p1", "child", "Guide", "Recovered evidence", "Recovered evidence", 1, 1, 2, {"source": "guide.md"}),
                ],
            )
            vector_store = FakeVectorStore()
            vector_store.dense_hits = [
                {"content": "", "metadata": {"chunk_id": "c1", "parent_id": "p1"}, "distance": 0.1, "vector_score": 0.9}
            ]
            service = RAGService(
                vector_store=vector_store,
                llm_client=SimpleNamespace(),
                chat_model="test",
                system_prompt="test",
                data_dir=tmp,
                top_k=1,
                min_relevance_score=0.0,
                chunk_size=80,
                chunk_overlap=10,
                document_repository=repo,
            )

            hits = service.hybrid_retrieve_hits("question")

        self.assertEqual("Recovered evidence", hits[0]["content"])
        self.assertEqual("doc-1", hits[0]["metadata"]["doc_id"])

    def test_hybrid_retrieve_falls_back_when_reranker_fails(self):
        vector_store = FakeVectorStore()
        vector_store.dense_hits = [
            {"content": "", "metadata": {"chunk_id": "a", "parent_id": "p1"}, "distance": 0.1, "vector_score": 0.9},
            {"content": "", "metadata": {"chunk_id": "b", "parent_id": "p2"}, "distance": 0.2, "vector_score": 0.8},
        ]
        service = RAGService(
            vector_store=vector_store,
            llm_client=SimpleNamespace(),
            chat_model="test",
            system_prompt="test",
            data_dir=tempfile.mkdtemp(),
            top_k=2,
            min_relevance_score=0.0,
            chunk_size=80,
            chunk_overlap=10,
            reranker_enabled=True,
            reranker_top_n=2,
            reranker=FakeReranker(error=RuntimeError("boom")),
        )

        hits = service.hybrid_retrieve_hits("question")

        self.assertEqual(["a", "b"], [hit["metadata"]["chunk_id"] for hit in hits])
        self.assertTrue(service._last_retrieval_debug["rerank"]["failed"])

    def test_low_recall_query_expansion_adds_second_pass_candidates(self):
        service = self.make_service()
        service.vector_store = QueryAwareVectorStore(
            dense_by_query={
                "weak query specification": [
                    {
                        "content": "expanded evidence",
                        "metadata": {"chunk_id": "expanded", "parent_id": "p1"},
                        "distance": 0.1,
                    }
                ]
            }
        )
        service.low_recall_query_expansion_enabled = True
        service.low_recall_min_candidates = 1
        service.low_recall_max_queries = 1

        hits = service.hybrid_retrieve_hits("weak query")

        self.assertEqual(["expanded"], [hit["metadata"]["chunk_id"] for hit in hits])
        self.assertTrue(service._last_retrieval_debug["query_expansion"]["used"])
        self.assertEqual(["weak query specification"], service._last_retrieval_debug["query_expansion"]["expanded_queries"])

    def test_rerank_degrades_threshold_when_all_strict_candidates_filtered(self):
        service = self.make_service()
        service.vector_store.dense_hits = [
            {"content": "a", "metadata": {"chunk_id": "a", "parent_id": "p1"}, "distance": 0.1},
            {"content": "b", "metadata": {"chunk_id": "b", "parent_id": "p2"}, "distance": 0.2},
        ]
        service.reranker_enabled = True
        service.reranker = FakeReranker({"a": 0.29, "b": 0.12})
        service.reranker_threshold = 0.3
        service.reranker_degradation_enabled = True
        service.reranker_degraded_threshold = 0.15

        hits = service.hybrid_retrieve_hits("question")

        self.assertEqual(["a"], [hit["metadata"]["chunk_id"] for hit in hits])
        self.assertTrue(service._last_retrieval_debug["rerank"]["threshold_degraded"])
        self.assertEqual("degraded_threshold", service._last_retrieval_debug["rerank"]["fallback_reason"])

    def test_mmr_selection_reduces_redundant_evidence(self):
        service = self.make_service()
        service.fusion_top_k = 3
        service.mmr_enabled = True
        service.mmr_lambda = 0.2
        service.mmr_top_k = 2
        service.vector_store.dense_hits = [
            {
                "content": "same device supports eight ports and gpon uplink",
                "metadata": {"chunk_id": "a", "parent_id": "p1"},
                "distance": 0.05,
            },
            {
                "content": "similar device supports eight ports and gpon uplink",
                "metadata": {"chunk_id": "b", "parent_id": "p2"},
                "distance": 0.06,
            },
            {
                "content": "firmware upgrade rollback process",
                "metadata": {"chunk_id": "c", "parent_id": "p3"},
                "distance": 0.2,
            },
        ]

        hits = service.hybrid_retrieve_hits("question")

        self.assertEqual(["a", "c"], [hit["metadata"]["chunk_id"] for hit in hits])
        self.assertTrue(service._last_retrieval_debug["mmr"]["used"])

    def test_near_duplicate_removal_uses_content_and_parent_signals(self):
        service = self.make_service()
        service.vector_store.dense_hits = [
            {"content": "duplicate content", "metadata": {"chunk_id": "a", "parent_id": "p1"}, "distance": 0.1},
            {"content": "duplicate content", "metadata": {"chunk_id": "b", "parent_id": "p2"}, "distance": 0.2},
            {"content": "unique content", "metadata": {"chunk_id": "c", "parent_id": "p3"}, "distance": 0.3},
        ]

        hits = service.hybrid_retrieve_hits("question")

        self.assertEqual(["a", "c"], [hit["metadata"]["chunk_id"] for hit in hits])
        self.assertEqual(1, service._last_retrieval_debug["duplicate_removal"]["removed_count"])

    def test_direct_load_small_selected_document_skips_similarity_retrieval(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            self.seed_document(repo)
            repo.replace_chunks(
                "doc-1",
                [
                    Chunk("p1", "doc-1", None, "parent", "Title", "Parent context", "Parent context", 1, 1, 2, {"source": "manual.md"}),
                    Chunk("c1", "doc-1", "p1", "child", "Title", "Direct evidence", "Direct evidence", 1, 1, 2, {"source": "manual.md"}),
                ],
            )
            vector_store = FakeVectorStore()
            service = RAGService(
                vector_store=vector_store,
                llm_client=SimpleNamespace(),
                chat_model="test",
                system_prompt="test",
                data_dir=tmp,
                top_k=3,
                min_relevance_score=0.0,
                chunk_size=80,
                chunk_overlap=10,
                document_repository=repo,
                direct_load_max_chunks=5,
                retrieval_debug_enabled=True,
            )
            scope = KnowledgeBaseScope("default-workspace", ("default-knowledge-base",), ("doc-1",), compatibility_default=True)

            hits = service.hybrid_retrieve_hits("question", scope=scope)

        self.assertEqual(["c1"], [hit["metadata"]["chunk_id"] for hit in hits])
        self.assertTrue(hits[0]["metadata"]["direct_loaded"])
        self.assertEqual([], vector_store.dense_queries)
        self.assertEqual("used", service._last_retrieval_debug["direct_load"]["decision"])

    def test_direct_load_large_selected_document_falls_back_to_scoped_retrieval(self):
        class ScopedVectorStore(FakeVectorStore):
            def query_dense(self, question, top_k, scope=None):
                self.dense_queries.append((question, top_k, scope))
                return self.dense_hits[:top_k]

        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            self.seed_document(repo)
            repo.replace_chunks(
                "doc-1",
                [
                    Chunk("p1", "doc-1", None, "parent", "Title", "Parent context", "Parent context", 1, 1, 2, {"source": "manual.md"}),
                    Chunk("c1", "doc-1", "p1", "child", "Title", "One", "One", 1, 1, 2, {"source": "manual.md"}),
                    Chunk("c2", "doc-1", "p1", "child", "Title", "Two", "Two", 1, 1, 2, {"source": "manual.md"}),
                ],
            )
            vector_store = ScopedVectorStore()
            vector_store.dense_hits = [
                {"content": "", "metadata": {"chunk_id": "c1", "doc_id": "doc-1", "parent_id": "p1"}, "distance": 0.1}
            ]
            service = RAGService(
                vector_store=vector_store,
                llm_client=SimpleNamespace(),
                chat_model="test",
                system_prompt="test",
                data_dir=tmp,
                top_k=3,
                min_relevance_score=0.0,
                chunk_size=80,
                chunk_overlap=10,
                document_repository=repo,
                direct_load_max_chunks=1,
                retrieval_debug_enabled=True,
            )
            scope = KnowledgeBaseScope("default-workspace", ("default-knowledge-base",), ("doc-1",), compatibility_default=True)

            hits = service.hybrid_retrieve_hits("question", scope=scope)

        self.assertEqual(["c1"], [hit["metadata"]["chunk_id"] for hit in hits])
        self.assertEqual(scope, vector_store.dense_queries[0][2])
        self.assertEqual("over_limit", service._last_retrieval_debug["direct_load"]["decision"])

    def test_answer_query_returns_debug_info_when_enabled(self):
        service = self.make_service()
        service.retrieval_debug_enabled = True
        service.vector_store.dense_hits = [
            {"content": "", "metadata": {"chunk_id": "c1", "parent_id": "p1"}, "distance": 0.1, "vector_score": 0.9}
        ]
        service.stream_answer = lambda question, hits=None: iter(["answer"])

        result = service.answer_query("question", top_k=2)

        self.assertEqual("answer", result["answer"])
        self.assertIn("dense_results", result["debug_info"])
        self.assertIn("bm25_results", result["debug_info"])
        self.assertIn("fused_results", result["debug_info"])
        self.assertIn("reranked_results", result["debug_info"])
        self.assertIn("selected_parent_chunks", result["debug_info"])
        self.assertIn("final_context_token_count", result["debug_info"])

    def test_answer_query_omits_debug_info_when_disabled(self):
        service = self.make_service()
        service.retrieval_debug_enabled = False
        service.vector_store.dense_hits = [
            {"content": "", "metadata": {"chunk_id": "c1", "parent_id": "p1"}, "distance": 0.1, "vector_score": 0.9}
        ]
        service.stream_answer = lambda question, hits=None: iter(["answer"])

        result = service.answer_query("question", top_k=2)

        self.assertIsNone(result["debug_info"])

    def test_answer_query_returns_future_compatible_fields_and_valid_used_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            self.seed_document(repo)
            repo.replace_chunks(
                "doc-1",
                [
                    Chunk("p1", "doc-1", None, "parent", "CLI", "Parent context", "Parent context", 1, 1, 3, {"source": "manual.md"}),
                    Chunk("c1", "doc-1", "p1", "child", "CLI", "ERR_CODE_42", "ERR_CODE_42", 1, 1, 3, {"source": "manual.md"}),
                ],
            )
            service = RAGService(
                vector_store=FakeVectorStore(),
                llm_client=SimpleNamespace(),
                chat_model="test",
                system_prompt="test",
                data_dir=tmp,
                top_k=3,
                min_relevance_score=0.0,
                chunk_size=80,
                chunk_overlap=10,
                document_repository=repo,
            )
            service.vector_store.dense_hits = [
                {"content": "", "metadata": {"chunk_id": "c1", "doc_id": "doc-1", "parent_id": "p1", "chunk_type": "child"}, "distance": 0.1, "vector_score": 0.9}
            ]
            service.stream_answer = lambda question, hits=None: iter(["answer"])

            result = service.answer_query("ERR_CODE_42", top_k=2)

        self.assertEqual([], result["used_entities"])
        self.assertEqual([], result["graph_paths"])
        self.assertGreater(result["confidence"], 0)
        self.assertEqual(["c1"], result["used_chunks"])
        self.assertEqual("doc-1", result["citations"][0]["doc_id"])
        self.assertEqual("p1", result["citations"][0]["parent_id"])

    def test_answer_query_does_not_call_graph_retriever_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            self.seed_document(repo)
            repo.replace_chunks(
                "doc-1",
                [
                    Chunk("p1", "doc-1", None, "parent", "CLI", "Parent context", "Parent context", 1, 1, 3, {"source": "manual.md"}),
                    Chunk("c1", "doc-1", "p1", "child", "CLI", "ERR_CODE_42", "ERR_CODE_42", 1, 1, 3, {"source": "manual.md"}),
                ],
            )
            graph_retriever = FakeGraphRetriever()
            service = RAGService(
                vector_store=FakeVectorStore(),
                llm_client=SimpleNamespace(),
                chat_model="test",
                system_prompt="test",
                data_dir=tmp,
                top_k=3,
                min_relevance_score=0.0,
                chunk_size=80,
                chunk_overlap=10,
                document_repository=repo,
                graph_retriever=graph_retriever,
            )
            service.vector_store.dense_hits = [
                {"content": "", "metadata": {"chunk_id": "c1", "doc_id": "doc-1", "parent_id": "p1", "chunk_type": "child"}, "distance": 0.1, "vector_score": 0.9}
            ]
            service.stream_answer = lambda question, hits=None: iter(["answer"])

            result = service.answer_query("ERR_CODE_42", top_k=2)

        self.assertEqual("answer", result["answer"])
        self.assertEqual([], result["used_entities"])
        self.assertEqual([], result["graph_paths"])
        self.assertEqual([], graph_retriever.calls)

    def test_answer_query_without_evidence_returns_insufficient_evidence_response(self):
        service = self.make_service()
        service.vector_store.dense_hits = []
        service.stream_answer = lambda question, hits=None: iter(["unsupported answer"])

        result = service.answer_query("unknown question", top_k=2)

        self.assertIn("cannot determine", result["answer"].lower())
        self.assertEqual([], result["citations"])
        self.assertEqual([], result["used_chunks"])
        self.assertEqual([], result["used_entities"])
        self.assertEqual([], result["graph_paths"])
        self.assertEqual(0.0, result["confidence"])

    def test_delete_document_removes_repository_fts_rows_and_vector_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            source = data_dir / "manual.md"
            source.write_text("# Manual", encoding="utf-8")
            repo = DocumentRepository(Path(tmp) / "rag.sqlite3")
            repo.upsert_document("doc-1", "manual.md", "md", "manual.md", "parsed", {"size": 8})
            repo.replace_chunks(
                "doc-1",
                [Chunk("c1", "doc-1", "p1", "child", "CLI", "DELETE_TOKEN", "DELETE_TOKEN", 1, 1, 3, {"source": "manual.md"})],
            )
            vector_store = FakeVectorStore()
            service = RAGService(
                vector_store=vector_store,
                llm_client=SimpleNamespace(),
                chat_model="test",
                system_prompt="test",
                data_dir=str(data_dir),
                top_k=3,
                min_relevance_score=0.0,
                chunk_size=80,
                chunk_overlap=10,
                document_repository=repo,
            )

            service.delete_document("doc-1")

            self.assertIsNone(repo.get_chunk("c1"))
            self.assertEqual([], repo.search_keyword_chunks("DELETE_TOKEN", top_k=5))
            self.assertEqual(["doc-1"], vector_store.deleted_documents)

    def test_stream_answer_includes_memory_and_conversation_context(self):
        client = FakeStreamingClient()
        service = RAGService(
            vector_store=FakeVectorStore(),
            llm_client=client,
            chat_model="test",
            system_prompt="system",
            data_dir=tempfile.mkdtemp(),
            top_k=3,
            min_relevance_score=0.0,
            chunk_size=80,
            chunk_overlap=10,
        )

        answer = "".join(
            service.stream_answer(
                "follow up",
                hits=[{"content": "document context", "metadata": {"source": "manual.md"}}],
                memory_context="[长期记忆]\n- 用户偏好中文回答。",
                conversation_context={"summary": "Earlier summary", "recent_messages": [{"role": "user", "content": "previous"}]},
            )
        )

        self.assertEqual("answer", answer)
        prompt = client.completions.calls[0]["messages"][1]["content"]
        self.assertIn("[长期记忆]", prompt)
        self.assertIn("用户偏好中文回答", prompt)
        self.assertIn("[会话上下文]", prompt)
        self.assertIn("Earlier summary", prompt)
        self.assertIn("previous", prompt)
        self.assertIn("document context", prompt)
        self.assertIn("回答要求", prompt)
        self.assertIn("直接结论", prompt)

    def test_stream_answer_uses_domain_agnostic_answer_guidance(self):
        client = FakeStreamingClient()
        service = RAGService(
            vector_store=FakeVectorStore(),
            llm_client=client,
            chat_model="test",
            system_prompt="system",
            data_dir=tempfile.mkdtemp(),
            top_k=3,
            min_relevance_score=0.0,
            chunk_size=80,
            chunk_overlap=10,
        )

        answer = "".join(
            service.stream_answer(
                "k3s 搭建步骤",
                hits=[{"content": "Run install command: curl -sfL https://example/install.sh | sh -", "metadata": {"source": "manual.md"}}],
            )
        )

        self.assertEqual("answer", answer)
        prompt = client.completions.calls[0]["messages"][1]["content"]
        self.assertIn("回答要求", prompt)
        self.assertIn("不要依赖关键词列表判断问题类型", prompt)
        self.assertIn("步骤使用有序列表", prompt)
        self.assertIn("根据提供的文档无法确定", prompt)

    def test_stream_answer_uses_generic_multi_constraint_evidence_guidance(self):
        client = FakeStreamingClient()
        service = RAGService(
            vector_store=FakeVectorStore(),
            llm_client=client,
            chat_model="test",
            system_prompt="system",
            data_dir=tempfile.mkdtemp(),
            top_k=3,
            min_relevance_score=0.0,
            chunk_size=80,
            chunk_overlap=10,
        )
        hits = [{"content": "DH-P7004 最大64个GPON接口，4个业务槽位。", "metadata": {"source": "DH-P7004.txt"}}]

        list(service.stream_answer("我需要一个能接28个分光器的OLT，帮我选一款", hits=hits))

        prompt = client.completions.calls[0]["messages"][1]["content"]
        self.assertIn("完整抽取硬性条件", prompt)
        self.assertIn("逐个候选、逐个条件核验", prompt)
        self.assertIn("同义词、别名、缩写", prompt)
        self.assertNotIn("PON口最大分路比", prompt)

    def test_build_reasoning_summary_explains_retrieval_without_hidden_chain(self):
        service = self.make_service()
        service._last_retrieval_debug = {
            "query_understanding": {
                "original_query": "8个电口",
                "normalized_query": "8个RJ-45",
                "retrieval_queries": ["8个电口", "8个RJ-45", "8个RJ45"],
                "expanded_terms": ["电口", "RJ-45", "RJ45"],
                "applied_terms": [{"term": "电口", "canonical": "RJ-45"}],
                "source": "dictionary",
            }
        }
        hits = [
            {
                "content": "8 x RJ-45 ports",
                "metadata": {
                    "source": "manual.txt",
                    "title_path": "规格",
                    "matched_child_ids": ["c1"],
                },
                "hybrid_score": 0.8,
            }
        ]

        summary = service.build_reasoning_summary("8个电口", hits)

        self.assertEqual("8个电口", summary["question"])
        self.assertEqual("8个RJ-45", summary["normalized_query"])
        self.assertIn("8个RJ45", summary["retrieval_queries"])
        self.assertEqual(["电口 -> RJ-45"], summary["term_mappings"])
        self.assertEqual("manual.txt", summary["evidence"][0]["source"])
        self.assertIn("8 x RJ-45 ports", summary["evidence"][0]["preview"])
        self.assertNotIn("chain_of_thought", summary)


if __name__ == "__main__":
    unittest.main()
