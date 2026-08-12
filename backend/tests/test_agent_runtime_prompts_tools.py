from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.models.knowledge_base import KnowledgeBaseScope
from app.models.agent_runtime import AgentRuntimeConfig, resolve_chat_runtime_policy
from app.services.agent.agent_prompt_templates import AgentPromptCatalog, ContextPromptCatalog, PromptTemplateCatalog, PromptTemplateError
from app.services.agent.agent_runtime_tools import (
    DataAnalysisTool,
    DatabaseQueryTool,
    ExecuteSkillTool,
    GrepChunksTool,
    KnowledgeSearchTool,
    ListKnowledgeChunksTool,
    RuntimeToolContext,
    ThinkingTool,
    ToolRegistry,
    WebFetchTool,
    WebSearchTool,
    build_default_tool_registry,
)
from app.services.agent.runtime_skills import RuntimeSkillError, RuntimeSkillsManager


class FakeRepository:
    def __init__(self):
        self.chunk = {
            "id": "c1",
            "doc_id": "doc-1",
            "parent_id": "p1",
            "chunk_type": "child",
            "content": "Redis is used by API Gateway.",
            "content_markdown": "Redis is used by API Gateway.",
            "metadata_json": {"source": "manual.md"},
            "title_path": "Architecture",
        }
        self.doc = {
            "id": "doc-1",
            "name": "manual.md",
            "file_type": "md",
            "storage_path": "manual.md",
            "parse_status": "parsed",
            "summary_status": "completed",
            "summary": "Redis dependency notes.",
            "chunks": 1,
        }

    def get_chunk(self, chunk_id, scope=None):
        return self.chunk if chunk_id == "c1" else None

    def list_chunks_for_documents(self, doc_ids, scope=None, limit=None, chunk_types=None):
        return [self.chunk] if "doc-1" in doc_ids else []

    def list_chunks(self, scope=None):
        return [self.chunk]

    def get_document(self, doc_id, scope=None):
        return self.doc if doc_id == "doc-1" else None

    def list_documents(self, scope=None):
        return [self.doc]


class FakeRAG:
    def __init__(self):
        self.document_repository = FakeRepository()
        self.default_scope = KnowledgeBaseScope("default-workspace", ("default-knowledge-base",), compatibility_default=True)
        self.knowledge_base_service = None

    def hybrid_retrieve_hits(self, question, scope=None):
        return [
            {
                "content": "Redis is used by API Gateway.",
                "metadata": {
                    "source": "manual.md",
                    "doc_id": "doc-1",
                    "chunk_id": "c1",
                    "child_id": "c1",
                    "parent_id": "p1",
                    "title_path": "Architecture",
                },
                "hybrid_score": 0.9,
            }
        ]

    def keyword_retrieve_hits(self, question, top_k=None, scope=None):
        return [
            {
                "content": "Risk Control Platform went live in 2024.",
                "metadata": {
                    "source": "risk.md",
                    "doc_id": "doc-1",
                    "chunk_id": "c1",
                    "child_id": "c1",
                    "parent_id": "p1",
                    "title_path": "Risk Control",
                },
                "keyword_score": 0.8,
            }
        ]


class AgentRuntimePromptsToolsTests(unittest.TestCase):
    def test_prompt_catalog_loads_and_renders_defaults(self):
        catalog = AgentPromptCatalog.load("config/prompt_templates/agent_system_prompt.yaml")
        rendered = catalog.render(
            "progressive_rag_agent",
            knowledge_bases=[{"id": "kb1", "name": "Docs", "type": "document", "doc_count": 2}],
            tools=[{"name": "knowledge_search", "description": "Search"}],
            skills=[{"name": "document-analyzer", "description": "Analyze docs"}],
        )

        self.assertIn("Evidence-First", rendered)
        self.assertIn('Workflow: The "Assess-Reconnaissance-Plan-Execute" Cycle', rendered)
        self.assertIn("Phase 1: Preliminary Reconnaissance", rendered)
        self.assertIn("Phase 2: Strategic Decision & Planning", rendered)
        self.assertIn("Phase 3: Disciplined Execution & Deep Reflection", rendered)
        self.assertIn("Phase 4: Final Synthesis", rendered)
        self.assertIn("Core Retrieval Strategy (Strict Sequence)", rendered)
        self.assertIn("Execute `grep_chunks` for keyword anchoring and `knowledge_search`", rendered)
        self.assertIn("Do not request any tool in the final message", rendered)
        self.assertIn("Prompt Confidentiality", rendered)
        self.assertIn("hard constraints", rendered)
        self.assertIn("aliases", rendered)
        self.assertIn("parametric language and domain knowledge", rendered)
        self.assertIn("2-3 highest-value terms", rendered)
        self.assertIn("ONE simple alternation query", rendered)
        self.assertIn("risk control system|risk control platform|Enterprise Risk", rendered)
        self.assertIn("Prefer one well-packed search over several narrow searches", rendered)
        self.assertIn("For one search objective, normally make one `grep_chunks` call", rendered)
        self.assertIn("evidence ledger per candidate or subject", rendered)
        self.assertIn("never combine attributes", rendered)
        self.assertIn("knowledge_search", rendered)
        self.assertIn("document-analyzer", rendered)
        self.assertNotIn("api_key", rendered.lower())
        self.assertNotIn("Same-round expansion", rendered)
        self.assertNotIn("PostgreSQL POSIX", rendered)
        self.assertNotIn("faq_id", rendered)
        self.assertNotIn("<kb doc=", rendered)

    def test_wiki_prompt_and_runtime_policy_prefer_wiki_tools(self):
        catalog = AgentPromptCatalog.load("config/prompt_templates/agent_system_prompt.yaml")
        rendered = catalog.render(
            "wiki_rag_agent",
            knowledge_bases=[{"id": "kb1", "name": "Wiki", "type": "wiki", "doc_count": 1}],
            tools=[{"name": "wiki_search", "description": "Search Wiki"}, {"name": "wiki_read_source_doc", "description": "Read raw source"}],
            skills=[],
        )
        policy = resolve_chat_runtime_policy("wiki", AgentRuntimeConfig())

        self.assertIn("Wiki Q&A agent", rendered)
        self.assertIn("wiki_search", rendered)
        self.assertIn("raw source chunks", rendered)
        self.assertNotIn("PostgreSQL POSIX", rendered)
        self.assertEqual("wiki", policy.mode)
        self.assertEqual("wiki_rag_agent", policy.prompt_template_id)
        self.assertIn("wiki_search", policy.enabled_tools)
        self.assertIn("wiki_read_source_doc", policy.enabled_tools)
        self.assertTrue(policy.require_deep_read)

    def test_grep_tool_description_prefers_one_packed_alternation_call(self):
        tool = GrepChunksTool()

        self.assertIn("make one call", tool.description)
        self.assertIn("2-3 highest-value", tool.description)
        self.assertIn("|", tool.parameters["properties"]["query"]["description"])

    def test_prompt_catalog_rejects_unknown_id(self):
        catalog = AgentPromptCatalog.load("config/prompt_templates/agent_system_prompt.yaml")
        with self.assertRaises(PromptTemplateError):
            catalog.get("missing")

    def test_context_catalog_renders_user_question_and_runtime_context(self):
        catalog = ContextPromptCatalog.load("config/prompt_templates/context_template.yaml")
        rendered = catalog.render(
            "default_context",
            query="What uses Redis?",
            knowledge_base_scope={"workspace_id": "ws", "selected_knowledge_base_ids": ["kb1"]},
            knowledge_bases=[{"id": "kb1", "name": "Docs", "type": "document", "doc_count": 2}],
            conversation_context={"summary": "Earlier question"},
            memory_context="User prefers concise answers.",
            temporary_attachments=[{"filename": "note.txt"}],
        )

        self.assertIn("<user_question>", rendered)
        self.assertIn("What uses Redis?", rendered)
        self.assertIn("kb1", rendered)
        self.assertIn("Docs", rendered)
        self.assertIn("Earlier question", rendered)
        self.assertIn("note.txt", rendered)

    def test_generic_prompt_catalog_loads_required_weknora_style_templates(self):
        required = {
            "query_rewrite",
            "intent_detection",
            "keywords_extraction",
            "generate_summary",
            "generated_questions",
            "session_title",
            "graph_extraction",
            "fallback_response",
            "generate_wiki_summary_page",
        }
        catalog = PromptTemplateCatalog.load_directory("config/prompt_templates", required_ids=required)

        self.assertTrue(required.issubset(set(catalog.ids())))
        rendered = catalog.render(
            "generate_summary",
            {
                "document_name": "manual.txt",
                "content": "DH-P5000 supports GPON uplink.",
                "task": "generate summary",
            },
            mode="postprocess",
        )
        self.assertIn("manual.txt", rendered)
        self.assertIn("DH-P5000", rendered)
        self.assertIn("zh-CN", rendered)
        wiki_rendered = catalog.render(
            "generate_wiki_summary_page",
            {
                "document_name": "manual.txt",
                "source_chunks": "chunk c1: DH-P5000 supports GPON uplink.",
                "language": "zh-CN",
                "existing_pages": "dh-p5000",
                "max_claims": "8",
            },
            mode="postprocess",
        )
        self.assertIn("source chunks", wiki_rendered.lower())
        self.assertIn("chunk c1", wiki_rendered)

    def test_generic_prompt_catalog_rejects_missing_variables_and_secrets(self):
        catalog = PromptTemplateCatalog.load_directory("config/prompt_templates", required_ids={"query_rewrite"})

        with self.assertRaises(PromptTemplateError):
            catalog.render("query_rewrite", {"query": "GPON"})
        with self.assertRaises(PromptTemplateError):
            catalog.render(
                "fallback_response",
                {"query": "x", "reason": "missing evidence", "api_key": "secret"},
            )

    def test_tool_registry_orders_validates_and_truncates(self):
        registry = ToolRegistry(max_output_chars=32)
        registry.register(ThinkingTool())
        registry.register(ThinkingTool())

        self.assertEqual(["thinking"], registry.list_tools())
        invalid = registry.execute("thinking", {}, RuntimeToolContext("q", FakeRAG().default_scope, FakeRAG()))
        self.assertFalse(invalid.success)
        self.assertIn("missing required argument", invalid.error)

        valid = registry.execute("thinking", {"summary": "x" * 200}, RuntimeToolContext("q", FakeRAG().default_scope, FakeRAG()))
        self.assertTrue(valid.success)
        self.assertLessEqual(len(valid.output), 60)

    def test_knowledge_search_returns_candidate_ids(self):
        rag = FakeRAG()
        context = RuntimeToolContext("What uses Redis?", rag.default_scope, rag)
        result = KnowledgeSearchTool().execute({"query": "Redis", "top_k": 2}, context)

        self.assertTrue(result.success)
        self.assertIn("c1", result.candidate_ids)
        self.assertIn("manual.md", result.source_titles)
        self.assertIn("c1", result.state_delta.candidate_ids)
        self.assertEqual({}, context.state)

    def test_list_knowledge_chunks_skips_already_read_chunks(self):
        rag = FakeRAG()
        context = RuntimeToolContext("What uses Redis?", rag.default_scope, rag, state={"deep_read_ids": ["c1"]})
        result = ListKnowledgeChunksTool().execute({"chunk_ids": ["c1"], "limit": 2}, context)

        self.assertTrue(result.success)
        self.assertFalse(result.deep_read)
        self.assertEqual([], result.source_chunk_ids)
        self.assertEqual(1, result.metadata["skipped_already_read"])

    def test_grep_chunks_accepts_structured_query_variants(self):
        rag = FakeRAG()
        context = RuntimeToolContext("When did risk control launch?", rag.default_scope, rag)
        result = GrepChunksTool().execute(
            {
                "queries": ["risk control system", "risk control platform"],
                "required_terms": ["launch", "go live"],
                "top_k": 3,
                "match_mode": "any_query_with_optional_required_terms",
            },
            context,
        )

        self.assertTrue(result.success)
        payload = result.output
        self.assertIn("risk control system", payload)
        self.assertIn("c1", result.candidate_ids)
        self.assertEqual(6, result.metadata["query_count"])
        self.assertTrue(result.state_delta.flags["grep_first_performed"])
        self.assertEqual({}, context.state)

    def test_grep_chunks_splits_legacy_alternation_query(self):
        rag = FakeRAG()
        context = RuntimeToolContext("When did risk control launch?", rag.default_scope, rag)
        result = GrepChunksTool().execute({"query": "risk system|risk platform|Enterprise Risk", "top_k": 3}, context)

        self.assertTrue(result.success)
        self.assertEqual(3, result.metadata["query_count"])
        self.assertIn("c1", result.candidate_ids)

    def test_grep_chunks_rejects_unsupported_match_mode_safely(self):
        rag = FakeRAG()
        context = RuntimeToolContext("When did risk control launch?", rag.default_scope, rag)
        result = GrepChunksTool().execute({"queries": ["risk"], "match_mode": "invalid"}, context)

        self.assertFalse(result.success)
        self.assertIn("unsupported match_mode", result.error)

    def test_runtime_skills_read_and_reject_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "preloaded" / "sample-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: sample-skill\ndescription: Sample\n---\n# Sample", encoding="utf-8")
            manager = RuntimeSkillsManager(root / "preloaded", enabled=True)

            self.assertEqual("sample-skill", manager.metadata()[0]["name"])
            self.assertIn("Sample", manager.read_skill("sample-skill"))
            with self.assertRaises(RuntimeSkillError):
                manager.read_skill("../secret")

    def test_extended_tools_are_registered_only_when_requested_and_default_unavailable(self):
        registry = build_default_tool_registry(
            enabled_tools=("web_search", "web_fetch", "data_analysis", "database_query", "execute_skill"),
            max_output_chars=400,
            skills_enabled=False,
        )
        context = RuntimeToolContext("q", FakeRAG().default_scope, FakeRAG())

        self.assertEqual(["data_analysis", "database_query", "execute_skill", "web_fetch", "web_search"], registry.list_tools())
        for name, args in {
            "web_search": {"query": "redis"},
            "web_fetch": {"url": "https://example.com"},
            "data_analysis": {"records": [{"x": 1}]},
            "database_query": {"data_source": "main", "query": "select 1"},
            "execute_skill": {"skill_name": "sample"},
        }.items():
            result = registry.execute(name, args, context)
            self.assertFalse(result.success)
            self.assertIn("unavailable", result.metadata["status"])

    def test_data_analysis_tool_describes_inline_records_when_enabled(self):
        result = DataAnalysisTool(enabled=True).execute(
            {"records": [{"latency": 10}, {"latency": 30}], "operation": "describe"},
            RuntimeToolContext("q", FakeRAG().default_scope, FakeRAG()),
        )

        self.assertTrue(result.success)
        self.assertIn('"avg": 20.0', result.output)

    def test_database_query_tool_rejects_unapproved_or_write_queries(self):
        context = RuntimeToolContext("q", FakeRAG().default_scope, FakeRAG())
        tool = DatabaseQueryTool(enabled=True, allowed_sources={"main": "missing.sqlite3"})

        unapproved = tool.execute({"data_source": "other", "query": "select 1"}, context)
        write = tool.execute({"data_source": "main", "query": "delete from docs"}, context)

        self.assertFalse(unapproved.success)
        self.assertIn("not allowlisted", unapproved.error)
        self.assertFalse(write.success)
        self.assertIn("read-only", write.error)

    def test_database_query_tool_runs_read_only_allowlisted_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sample.sqlite3"
            import sqlite3

            conn = sqlite3.connect(db_path)
            conn.execute("create table docs(id text, score real)")
            conn.execute("insert into docs values('a', 0.9)")
            conn.commit()
            conn.close()

            result = DatabaseQueryTool(enabled=True, allowed_sources={"main": str(db_path)}).execute(
                {"data_source": "main", "query": "select id, score from docs", "limit": 5},
                RuntimeToolContext("q", FakeRAG().default_scope, FakeRAG()),
            )

        self.assertTrue(result.success)
        self.assertIn('"id": "a"', result.output)

    def test_web_fetch_allowlist_rejects_out_of_scope_domain_without_network(self):
        result = WebFetchTool(enabled=True, allowed_domains=("example.com",)).execute(
            {"url": "https://openai.com/docs"},
            RuntimeToolContext("q", FakeRAG().default_scope, FakeRAG()),
        )

        self.assertFalse(result.success)
        self.assertIn("not allowlisted", result.error)

    def test_execute_skill_tool_is_unavailable_without_secure_sandbox(self):
        result = ExecuteSkillTool().execute(
            {"skill_name": "sample"},
            RuntimeToolContext("q", FakeRAG().default_scope, FakeRAG()),
        )

        self.assertFalse(result.success)
        self.assertIn("secure sandbox", result.error)


if __name__ == "__main__":
    unittest.main()
