import tempfile
import unittest
from pathlib import Path

from app.models.document_models import Chunk
from app.services.documents.document_repository import DocumentRepository
from app.services.agent.agent_runtime_tools import RuntimeToolContext, build_default_tool_registry
from app.services.knowledge.knowledge_base_repository import KnowledgeBaseRepository
from app.services.knowledge.knowledge_base_service import KnowledgeBaseService
from app.services.wiki.wiki_repository import WikiRepository
from app.services.wiki.wiki_service import WikiPageService


class FakeRAG:
    def __init__(self, wiki_service, scope):
        self.wiki_page_service = wiki_service
        self.default_scope = scope


class WikiAgentToolTests(unittest.TestCase):
    def _context(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "metadata.sqlite3"
        kb_service = KnowledgeBaseService(KnowledgeBaseRepository(path))
        doc_repo = DocumentRepository(path)
        wiki_service = WikiPageService(WikiRepository(path), kb_service, doc_repo)
        kb = kb_service.create("Wiki KB", knowledge_base_type="wiki")
        scope = kb_service.resolve_scope([kb.id])
        doc_repo.upsert_document(
            id="doc-1",
            name="manual.md",
            file_type="md",
            storage_path="manual.md",
            parse_status="parsed",
            metadata_json={},
            workspace_id=scope.workspace_id,
            knowledge_base_id=scope.knowledge_base_id,
        )
        doc_repo.replace_chunks(
            "doc-1",
            [
                Chunk(
                    id="chunk-1",
                    doc_id="doc-1",
                    parent_id=None,
                    chunk_type="child",
                    title_path="Runtime",
                    content="Redis is used by API Gateway. Limit is 1000 requests per minute.",
                    content_markdown="Redis is used by API Gateway. Limit is 1000 requests per minute.",
                    page_start=None,
                    page_end=None,
                    token_count=14,
                    metadata={},
                )
            ],
            scope,
        )
        wiki_service.create_page(
            scope,
            {
                "slug": "redis",
                "title": "Redis",
                "status": "published",
                "summary": "Redis cache notes.",
                "content_markdown": "Redis is used by API Gateway.",
                "source_refs": [{"doc_id": "doc-1", "chunk_id": "chunk-1", "title": "manual.md"}],
                "chunk_refs": ["chunk-1"],
            },
        )
        return RuntimeToolContext("What uses Redis?", scope, FakeRAG(wiki_service, scope))

    def test_wiki_tools_are_unavailable_until_flagged_on(self):
        context = self._context()
        registry = build_default_tool_registry(
            enabled_tools=("wiki_search",),
            max_output_chars=1000,
            skills_enabled=False,
            wiki_tools_enabled=False,
        )

        result = registry.execute("wiki_search", {"query": "Redis"}, context)

        self.assertFalse(result.success)
        self.assertIn("disabled", result.error)

    def test_wiki_search_read_and_write_proposal_tools(self):
        context = self._context()
        registry = build_default_tool_registry(
            enabled_tools=("wiki_search", "wiki_read_page", "wiki_write_page"),
            max_output_chars=4000,
            skills_enabled=False,
            wiki_tools_enabled=True,
            wiki_maintenance_tools_enabled=True,
        )

        search = registry.execute("wiki_search", {"query": "Redis"}, context)
        read = registry.execute("wiki_read_page", {"slug": "redis"}, context)
        proposal = registry.execute(
            "wiki_write_page",
            {"slug": "api-gateway", "title": "API Gateway", "content_markdown": "Uses [[redis]]."},
            context,
        )

        self.assertTrue(search.success)
        self.assertIn("redis", search.output)
        self.assertTrue(read.success)
        self.assertTrue(read.deep_read)
        self.assertTrue(proposal.success)
        self.assertIn("write_page", proposal.output)

    def test_wiki_guided_source_fallback_and_conflict_issue_tool(self):
        context = self._context()
        registry = build_default_tool_registry(
            enabled_tools=("wiki_search", "wiki_read_page", "wiki_read_source_doc", "wiki_flag_issue"),
            max_output_chars=4000,
            skills_enabled=False,
            wiki_tools_enabled=True,
        )

        search = registry.execute("wiki_search", {"query": "Redis"}, context)
        read_page = registry.execute("wiki_read_page", {"slug": "redis"}, context)
        source = registry.execute("wiki_read_source_doc", {"doc_id": "doc-1", "chunk_ids": ["chunk-1"]}, context)
        issue = registry.execute(
            "wiki_flag_issue",
            {
                "slug": "redis",
                "issue_type": "conflict",
                "description": "Wiki summary omits exact rate limit found in raw source.",
                "suspected_doc_ids": ["doc-1"],
                "suspected_chunk_ids": ["chunk-1"],
            },
            context,
        )

        self.assertTrue(search.success)
        self.assertTrue(read_page.deep_read)
        self.assertTrue(source.success)
        self.assertIn("1000 requests", source.output)
        self.assertIn("chunk-1", source.source_chunk_ids)
        self.assertTrue(issue.success)
        self.assertIn("conflict", issue.output)


if __name__ == "__main__":
    unittest.main()
