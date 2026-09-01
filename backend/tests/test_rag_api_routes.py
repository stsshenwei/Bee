import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.services.documents.document_repository import DocumentRepository
from app.models.knowledge_base import KnowledgeBaseScope
from app.services.knowledge.knowledge_base_repository import KnowledgeBaseRepository
from app.services.knowledge.knowledge_base_service import KnowledgeBaseService
from app.services.documents.temporary_attachment_repository import TemporaryAttachmentRepository
from app.services.wiki.wiki_repository import WikiRepository
from app.services.wiki.wiki_service import WikiPageService
from app.services.chat_streaming.event_bus import ChatStreamEvent
from app.services.chat_streaming.stream_manager import MemoryStreamManager, StreamIdentity
from tests.test_runtime_config import postgres_runtime_patches


class FakeCollection:
    def load(self):
        pass

    def num_entities(self):
        return 0


class FakeRagService:
    def __init__(self):
        self.deleted = []
        self.delete_calls = []
        self.upload_calls = []
        self.ingest_calls = []
        self.query_calls = []
        self.feedback_calls = []
        self.stream_calls = []
        self.list_document_calls = []
        self.upload_batches = {}
        self.upload_batch_calls = []
        self.preview_calls = []
        self.agent_trace_stream_enabled = False

    def needs_reingest(self):
        return False

    def resolve_scope(self, knowledge_base_ids=None, document_ids=None):
        return KnowledgeBaseScope(
            workspace_id="default-workspace",
            selected_knowledge_base_ids=tuple(knowledge_base_ids or ["default-knowledge-base"]),
            document_ids=tuple(document_ids or []),
            compatibility_default=not bool(knowledge_base_ids),
        )

    def save_uploaded_document(self, filename, content, relative_path=None, batch_id=None, scope=None):
        self.upload_calls.append(
            {
                "filename": filename,
                "content": content,
                "relative_path": relative_path,
                "batch_id": batch_id,
                "scope": scope,
            }
        )
        return {
            "doc_id": "doc-1",
            "source": "uploads/batch-001/folder/manual.md" if relative_path else "uploads/manual.md",
            "filename": filename,
            "size": len(content),
            "parse_status": "parsed",
            "chunks": 2,
        }

    def parse_document(self, source, scope=None):
        self.preview_calls.append({"source": source, "scope": scope})
        if source == "unsupported.exe":
            raise ValueError("Unsupported document type: .exe")
        return {
            "doc_id": "doc-preview",
            "source": source,
            "extension": ".md",
            "characters": 1200,
            "parent_chunks": 1,
            "child_chunks": 4,
            "table_chunks": 0,
            "preview": "# Manual\n\nBody",
            "parser_diagnostics": {
                "requested_engine": "missing-engine",
                "effective_engine": "builtin",
                "fallback_reason": "requested engine unavailable",
            },
            "document_metadata": {"page_count": 1},
            "chunk_diagnostics": {
                "selected_strategy": "recursive",
                "attempts": [
                    {"strategy": "heading", "accepted": False, "reason": "too many tiny chunks"},
                    {"strategy": "recursive", "accepted": True, "reason": ""},
                ],
            },
            "chunk_statistics": {"count": 4, "min": 80, "max": 420, "average": 300.0, "tiny_count": 1},
            "chunk_previews": [
                {"id": "c1", "chunk_type": "child", "preview": "one", "strategy": "recursive"},
                {"id": "c2", "chunk_type": "child", "preview": "two", "strategy": "recursive"},
            ],
        }

    def _batch_payload(self, batch_id="batch-1", scope=None, status="draft", files=None, settings=None):
        return {
            "id": batch_id,
            "workspace_id": scope.workspace_id if scope else "default-workspace",
            "knowledge_base_id": scope.knowledge_base_id if scope else "default-knowledge-base",
            "status": status,
            "settings": settings or {"chunk_size": 800},
            "effective_settings": {"chunk_size": 800, "multimodal_enabled": False, "audio_enabled": False},
            "aggregate": {"total": len(files or []), "uploaded": len(files or []), "processing": 0, "completed": 0, "failed": 0, "canceled": 0},
            "files": files or [],
            "error_message": "",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "confirmed_at": None,
            "completed_at": None,
        }

    def create_upload_batch(self, scope, settings=None):
        self.upload_batch_calls.append({"action": "create", "scope": scope, "settings": settings or {}})
        batch = self._batch_payload(scope=scope, settings=settings or {})
        self.upload_batches[batch["id"]] = batch
        return batch

    def get_upload_batch(self, batch_id, scope):
        self.upload_batch_calls.append({"action": "get", "scope": scope, "batch_id": batch_id})
        if batch_id not in self.upload_batches:
            raise KeyError(batch_id)
        return self.upload_batches[batch_id]

    def update_upload_batch_settings(self, batch_id, scope, settings=None):
        self.upload_batch_calls.append({"action": "settings", "scope": scope, "batch_id": batch_id, "settings": settings or {}})
        batch = self.get_upload_batch(batch_id, scope)
        batch["settings"] = settings or {}
        return batch

    def add_upload_batch_file(self, batch_id, filename, content, scope, relative_path=None):
        self.upload_batch_calls.append(
            {
                "action": "file",
                "scope": scope,
                "batch_id": batch_id,
                "filename": filename,
                "content": content,
                "relative_path": relative_path,
            }
        )
        batch = self.get_upload_batch(batch_id, scope)
        file_task = {
            "id": "file-1",
            "batch_id": batch_id,
            "workspace_id": scope.workspace_id,
            "knowledge_base_id": scope.knowledge_base_id,
            "original_name": filename,
            "relative_path": relative_path or filename,
            "storage_path": "uploads/batch-1/manual.md",
            "size": len(content),
            "status": "uploaded",
            "document_id": None,
            "chunks": 0,
            "error_message": "",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        batch["files"] = [file_task]
        batch["status"] = "ready_to_process"
        batch["aggregate"] = {**batch["aggregate"], "total": 1, "uploaded": 1}
        return file_task

    def confirm_upload_batch(self, batch_id, scope):
        self.upload_batch_calls.append({"action": "confirm", "scope": scope, "batch_id": batch_id})
        batch = self.get_upload_batch(batch_id, scope)
        batch["status"] = "completed"
        batch["confirmed_at"] = "2026-01-01T00:00:01"
        batch["completed_at"] = "2026-01-01T00:00:02"
        return batch

    def start_upload_batch_processing(self, batch_id, scope):
        self.upload_batch_calls.append({"action": "start_processing", "scope": scope, "batch_id": batch_id})
        batch = self.get_upload_batch(batch_id, scope)
        batch["status"] = "processing"
        batch["confirmed_at"] = "2026-01-01T00:00:01"
        return batch

    def process_upload_batch(self, batch_id, scope):
        self.upload_batch_calls.append({"action": "process_background", "scope": scope, "batch_id": batch_id})
        batch = self.get_upload_batch(batch_id, scope)
        batch["status"] = "completed"
        batch["completed_at"] = "2026-01-01T00:00:02"

    def retry_upload_batch_file(self, batch_id, file_id, scope):
        self.upload_batch_calls.append({"action": "retry", "scope": scope, "batch_id": batch_id, "file_id": file_id})
        return self.get_upload_batch(batch_id, scope)

    def retry_document_processing_task(self, doc_id, scope):
        return {
            "id": doc_id,
            "workspace_id": scope.workspace_id,
            "knowledge_base_id": scope.knowledge_base_id,
            "name": "manual.md",
            "file_type": "md",
            "storage_path": "uploads/manual.md",
            "parse_status": "processing",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "metadata_json": {},
            "chunks": 1,
            "source": "uploads/manual.md",
            "size": 8,
            "processing_task_id": "processing-1",
            "processing_task_type": "process_document",
            "processing_task_status": "retrying",
            "processing_task_queue": "core",
            "processing_broker_task_id": "broker-1",
            "processing_task_attempt": 0,
            "processing_task_max_attempts": 3,
            "processing_dead_lettered": False,
            "processing_last_error": "parser unavailable",
            "processing_dead_letter_reason": "parser unavailable",
            "processing_retry_available": False,
        }

    def cancel_upload_batch(self, batch_id, scope):
        self.upload_batch_calls.append({"action": "cancel", "scope": scope, "batch_id": batch_id})
        batch = self.get_upload_batch(batch_id, scope)
        batch["status"] = "canceled"
        return batch

    def ingest_document_by_id(self, doc_id, scope=None):
        self.ingest_calls.append({"doc_id": doc_id, "scope": scope})
        return {"doc_id": doc_id, "parse_status": "parsed", "chunk_count": 3, "vector_count": 2}

    def answer_query(self, question, top_k=None, filters=None, scope=None):
        self.query_calls.append({"question": question, "scope": scope})
        return {
            "answer": "answer",
            "citations": [{"doc_id": "doc-1", "chunk_id": "c1", "parent_id": "p1"}],
            "used_chunks": ["c1"],
            "used_entities": [],
            "graph_paths": [],
            "confidence": 0.8,
            "agent_trace": [{"stage": "AnalyzeQuestion", "status": "completed", "summary": "routed"}],
            "tool_calls": [{"tool": "RawRAGTool", "status": "completed"}],
            "evidence_summary": {"tool_counts": {"RawRAGTool": 1}},
            "debug_info": {"question": question, "top_k": top_k, "filters": filters},
        }

    def hybrid_retrieve_hits(self, question, scope=None):
        return [{"content": "child", "metadata": {"source": "manual.txt", "parent_id": "p1", "chunk_id": "c1"}, "hybrid_score": 0.8}]

    def recall_parent_hits(self, child_hits, scope=None):
        return [{"content": "parent context", "metadata": {"source": "manual.txt", "matched_child_ids": ["c1"]}, "hybrid_score": 0.8}]

    def extract_sources(self, hits):
        return [{"source": "manual.txt", "score": 0.8}]

    def build_reasoning_summary(self, question, hits):
        return {
            "question": question,
            "normalized_query": "8个RJ-45",
            "retrieval_queries": [question, "8个RJ-45"],
            "term_mappings": ["电口 -> RJ-45"],
            "evidence": [{"source": "manual.txt", "score": 0.8, "preview": "parent context"}],
        }

    def build_chat_agent_trace(self, question, hits):
        if not self.agent_trace_stream_enabled:
            return []
        return [{"stage": "AnalyzeQuestion", "status": "completed", "summary": "routed"}]

    def stream_answer(self, question, hits=None, conversation_context=None, memory_context=None, scope=None):
        self.stream_calls.append(
            {
                "question": question,
                "hits": hits,
                "conversation_context": conversation_context,
                "memory_context": memory_context,
                "scope": scope,
            }
        )
        yield "answer"

    def delete_document(self, doc_id, scope=None):
        self.deleted.append(doc_id)
        self.delete_calls.append({"doc_id": doc_id, "scope": scope})

    def list_documents(self, scope=None, filters=None):
        self.list_document_calls.append({"scope": scope, "filters": filters or {}})
        return [
            {
                "id": "doc-1",
                "workspace_id": scope.workspace_id if scope else "default-workspace",
                "knowledge_base_id": scope.knowledge_base_id if scope else "default-knowledge-base",
                "name": "manual.md",
                "file_type": "md",
                "storage_path": "uploads/manual.md",
                "parse_status": "parsed",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
                "metadata_json": {},
                "chunks": 1,
                "source": "uploads/manual.md",
                "size": 8,
            }
        ]

    def create_feedback_document(self, question, answer, scope=None):
        self.feedback_calls.append({"question": question, "answer": answer, "scope": scope})
        return {"title": "修正", "source": "feedback/fix.md", "chunks": 1}


class FakeConversationService:
    def __init__(self):
        self.created = 0
        self.appended = []
        self.contexts = []
        self.summarized = []

    def get_or_create_conversation(self, conversation_id):
        if conversation_id:
            return {"id": conversation_id, "title": "Existing", "summary": "Earlier summary"}
        self.created += 1
        return {"id": "conv-new", "title": "", "summary": ""}

    def build_context(self, conversation_id):
        context = {
            "conversation_id": conversation_id,
            "summary": "Earlier summary" if conversation_id == "conv-existing" else "",
            "recent_messages": [{"role": "user", "content": "previous question"}],
        }
        self.contexts.append(context)
        return context

    def maybe_summarize(self, conversation_id):
        self.summarized.append(conversation_id)
        return ""

    @property
    def repository(self):
        return self

    def append_message(self, conversation_id, role, content, metadata_json=None):
        message = {
            "id": f"msg-{len(self.appended) + 1}",
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "metadata_json": metadata_json or {},
        }
        self.appended.append(message)
        return message


class FakeMemoryService:
    def __init__(self):
        self.recall_calls = []
        self.extract_calls = []
        self.memories = [{"id": "mem-1", "scope": "user", "type": "preference", "content": "用户偏好中文回答。", "confidence": 0.9}]
        self.deleted = []

    def recall_memories(self, question, limit=8, scope=None):
        self.recall_calls.append({"question": question, "limit": limit, "scope": scope})
        return [{"id": "mem-1", "scope": "user", "type": "preference", "content": "用户偏好中文回答。"}]

    def format_prompt_context(self, memories):
        return "[长期记忆]\n- (user/preference) 用户偏好中文回答。"

    def process_exchange(self, user_message, assistant_message, conversation_id, user_message_id, memory_enabled=True):
        self.extract_calls.append(
            {
                "user_message": user_message,
                "assistant_message": assistant_message,
                "conversation_id": conversation_id,
                "user_message_id": user_message_id,
                "memory_enabled": memory_enabled,
            }
        )
        return [{"action": "upserted", "id": "mem-1", "content": "用户偏好中文回答。"}] if memory_enabled else []

    def list_active_memories(self):
        return list(self.memories)

    def delete_memory(self, memory_id):
        if memory_id == "missing":
            return False
        self.deleted.append(memory_id)
        self.memories = [memory for memory in self.memories if memory["id"] != memory_id]
        return True


class RagApiRouteTests(unittest.TestCase):
    def import_main(self):
        sys.modules.pop("app.main", None)
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": "",
                "VECTOR_STORE_DIR": str(Path(tmpdir) / "vector_db"),
                "DATABASE_URL": "postgresql://rag:rag@localhost:5432/rag_test",
                "POSTGRES_SCHEMA": "rag",
                "RAG_DATA_DIR": str(Path(tmpdir) / "data"),
                "AUTO_INGEST_ON_STARTUP": "false",
                "WIKI_INGEST_ENABLED": "false",
            }
            with patch.dict(os.environ, env, clear=False):
                with postgres_runtime_patches():
                    return importlib.import_module("app.main")

    def test_rag_upload_ingest_query_and_delete_routes(self):
        module = self.import_main()
        fake_service = FakeRagService()
        module.rag_service = fake_service

        with TestClient(module.app) as client:
            upload = client.post("/rag/documents/upload", files={"file": ("manual.md", b"# Manual", "text/markdown")})
            ingest = client.post("/rag/documents/doc-1/ingest")
            query = client.post("/rag/query", json={"question": "q", "doc_ids": ["doc-1"], "top_k": 8, "filters": {"a": "b"}})
            delete = client.delete("/rag/documents/doc-1")

        self.assertEqual({"doc_id": "doc-1", "status": "uploaded"}, upload.json())
        self.assertEqual({"doc_id": "doc-1", "parse_status": "parsed", "chunk_count": 3, "vector_count": 2}, ingest.json())
        self.assertEqual("answer", query.json()["answer"])
        self.assertEqual(["c1"], query.json()["used_chunks"])
        self.assertEqual([], query.json()["used_entities"])
        self.assertEqual([], query.json()["graph_paths"])
        self.assertEqual(0.8, query.json()["confidence"])
        self.assertEqual("AnalyzeQuestion", query.json()["agent_trace"][0]["stage"])
        self.assertEqual("RawRAGTool", query.json()["tool_calls"][0]["tool"])
        self.assertEqual({"RawRAGTool": 1}, query.json()["evidence_summary"]["tool_counts"])
        self.assertEqual("doc-1", query.json()["citations"][0]["doc_id"])
        self.assertEqual({"doc_id": "doc-1", "status": "deleted"}, delete.json())
        self.assertEqual(["doc-1"], fake_service.deleted)
        scopes = [
            fake_service.upload_calls[0]["scope"],
            fake_service.ingest_calls[0]["scope"],
            fake_service.query_calls[0]["scope"],
            fake_service.delete_calls[0]["scope"],
        ]
        self.assertTrue(all(scope.compatibility_default for scope in scopes))
        self.assertTrue(all(scope.selected_knowledge_base_ids == ("default-knowledge-base",) for scope in scopes))

    def test_feedback_requires_one_target_kb_and_preserves_scope(self):
        module = self.import_main()
        fake_service = FakeRagService()
        module.rag_service = fake_service

        with TestClient(module.app) as client:
            rejected = client.post(
                "/feedback/answer",
                json={"question": "q", "answer": "a", "knowledge_base_ids": ["kb-a", "kb-b"]},
            )
            accepted = client.post(
                "/feedback/answer",
                json={"question": "q", "answer": "a", "knowledge_base_id": "kb-a"},
            )

        self.assertEqual(400, rejected.status_code)
        self.assertEqual(200, accepted.status_code)
        self.assertEqual(("kb-a",), fake_service.feedback_calls[0]["scope"].selected_knowledge_base_ids)

    def test_knowledge_base_lifecycle_routes_and_aggregates(self):
        module = self.import_main()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "metadata.sqlite3"
            documents = DocumentRepository(path)
            service = KnowledgeBaseService(KnowledgeBaseRepository(path))
            module.rag_service = SimpleNamespace(knowledge_base_service=service, needs_reingest=lambda: False)

            with TestClient(module.app) as client:
                workspace = client.get("/workspaces/default")
                created = client.post(
                    "/knowledge-bases",
                    json={"name": "产品资料", "description": "设备资料", "type": "document"},
                )
                knowledge_base_id = created.json()["id"]
                documents.upsert_document(
                    id="doc-failed",
                    name="failed.md",
                    file_type="md",
                    storage_path="failed.md",
                    parse_status="failed",
                    knowledge_base_id=knowledge_base_id,
                )
                listed = client.get("/knowledge-bases")
                updated = client.patch(
                    f"/knowledge-bases/{knowledge_base_id}", json={"name": "产品与方案", "description": "更新"}
                )
                archived = client.delete(f"/knowledge-bases/{knowledge_base_id}")
                active_after_archive = client.get("/knowledge-bases")
                restored = client.post(f"/knowledge-bases/{knowledge_base_id}/restore")
                unsupported = client.post("/knowledge-bases", json={"name": "Future", "type": "future"})

        self.assertEqual(200, workspace.status_code)
        self.assertEqual("default-workspace", workspace.json()["id"])
        self.assertEqual(201, created.status_code)
        listed_item = next(item for item in listed.json()["items"] if item["id"] == knowledge_base_id)
        self.assertEqual(1, listed_item["aggregate"]["document_count"])
        self.assertEqual(1, listed_item["aggregate"]["failed_count"])
        self.assertEqual("产品与方案", updated.json()["name"])
        self.assertEqual("archived", archived.json()["status"])
        self.assertNotIn(knowledge_base_id, [item["id"] for item in active_after_archive.json()["items"]])
        self.assertEqual("active", restored.json()["status"])
        self.assertEqual(400, unsupported.status_code)

    def test_wiki_routes_are_scoped_and_reviewable(self):
        module = self.import_main()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "metadata.sqlite3"
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(path))
            wiki_service = WikiPageService(WikiRepository(path), kb_service)
            kb = kb_service.create("Wiki KB", knowledge_base_type="wiki")
            other_kb = kb_service.create("Other Wiki", knowledge_base_type="wiki")
            module.rag_service = SimpleNamespace(
                knowledge_base_service=kb_service,
                wiki_page_service=wiki_service,
                resolve_scope=kb_service.resolve_scope,
                needs_reingest=lambda: False,
            )

            with TestClient(module.app) as client:
                created = client.post(
                    f"/knowledge-bases/{kb.id}/wiki/pages",
                    json={"slug": "redis", "title": "Redis", "aliases": ["cache"], "status": "published", "content_markdown": "Cache page"},
                )
                client.post(f"/knowledge-bases/{kb.id}/wiki/pages", json={"slug": "gpon", "title": "GPON", "status": "published"})
                first_page = client.get(f"/knowledge-bases/{kb.id}/wiki/pages?status=published&limit=1")
                second_page = client.get(f"/knowledge-bases/{kb.id}/wiki/pages?status=published&limit=1&cursor={first_page.json()['next_cursor']}")
                alias_search = client.get(f"/knowledge-bases/{kb.id}/wiki/pages?q=cache&status=published")
                read = client.get(f"/knowledge-bases/{kb.id}/wiki/pages/redis")
                graph = client.get(f"/knowledge-bases/{kb.id}/wiki/graph?limit=1")
                cross_scope = client.get(f"/knowledge-bases/{other_kb.id}/wiki/pages/redis")
                issue = client.post(
                    f"/knowledge-bases/{kb.id}/wiki/issues",
                    json={"slug": "redis", "description": "Needs source"},
                )
                proposal = client.post(
                    f"/knowledge-bases/{kb.id}/wiki/proposals",
                    json={
                        "action": "replace_text",
                        "slug": "redis",
                        "payload": {"old_text": "Cache", "new_text": "Caching"},
                    },
                )
                applied = client.post(
                    f"/knowledge-bases/{kb.id}/wiki/proposals/{proposal.json()['id']}/apply"
                )
                missing_cleanup = client.post(f"/knowledge-bases/{kb.id}/wiki/issues/missing/cleanup")

        self.assertEqual(201, created.status_code)
        self.assertEqual("redis", read.json()["slug"])
        self.assertEqual(1, graph.json()["meta"]["node_count"])
        self.assertNotEqual(first_page.json()["items"][0]["id"], second_page.json()["items"][0]["id"])
        self.assertEqual(["redis"], [item["slug"] for item in alias_search.json()["items"]])
        self.assertEqual(404, cross_scope.status_code)
        self.assertEqual(404, missing_cleanup.status_code)
        self.assertEqual("open", issue.json()["status"])
        self.assertEqual("applied", applied.json()["proposal"]["status"])
        self.assertIn("Caching", applied.json()["page"]["content_markdown"])

    def test_document_upload_route_passes_folder_metadata(self):
        module = self.import_main()
        fake_service = FakeRagService()
        module.rag_service = fake_service

        with TestClient(module.app) as client:
            upload = client.post(
                "/documents/upload",
                files={"file": ("manual.md", b"# Manual", "text/markdown")},
                data={"relative_path": "folder/manual.md", "batch_id": "batch-001"},
            )

        self.assertEqual(200, upload.status_code)
        self.assertEqual(
            {
                "doc_id": "doc-1",
                "source": "uploads/batch-001/folder/manual.md",
                "filename": "manual.md",
                "size": 8,
                "parse_status": "parsed",
                "chunks": 2,
                "error": None,
            },
            upload.json(),
        )
        self.assertEqual("folder/manual.md", fake_service.upload_calls[0]["relative_path"])
        self.assertEqual("batch-001", fake_service.upload_calls[0]["batch_id"])

    def test_processing_preview_api_returns_trace_stats_scope_and_capabilities(self):
        module = self.import_main()
        fake_service = FakeRagService()
        module.rag_service = fake_service

        with TestClient(module.app) as client:
            preview = client.post(
                "/documents/parse",
                json={"source": "uploads/manual.md", "knowledge_base_id": "kb-a"},
            )
            unsupported = client.post(
                "/documents/parse",
                json={"source": "unsupported.exe", "knowledge_base_id": "kb-a"},
            )
            engines = client.get("/parser-engines")

        payload = preview.json()
        self.assertEqual(200, preview.status_code)
        self.assertEqual(400, unsupported.status_code)
        self.assertEqual("builtin", payload["parser_diagnostics"]["effective_engine"])
        self.assertEqual("heading", payload["chunk_diagnostics"]["attempts"][0]["strategy"])
        self.assertFalse(payload["chunk_diagnostics"]["attempts"][0]["accepted"])
        self.assertEqual(4, payload["chunk_statistics"]["count"])
        self.assertEqual(2, len(payload["chunk_previews"]))
        self.assertEqual(("kb-a",), fake_service.preview_calls[0]["scope"].selected_knowledge_base_ids)
        self.assertEqual([], fake_service.upload_calls)
        self.assertEqual([], fake_service.ingest_calls)
        self.assertEqual(200, engines.status_code)
        self.assertEqual("builtin", engines.json()["default"])
        self.assertTrue(all("name" in item for item in engines.json()["items"]))
        self.assertTrue(all("available" in item for item in engines.json()["items"]))

    def test_documents_route_passes_scoped_filter_parameters(self):
        module = self.import_main()
        fake_service = FakeRagService()
        module.rag_service = fake_service

        with TestClient(module.app) as client:
            response = client.get(
                "/documents",
                params={
                    "knowledge_base_id": "kb-a",
                    "q": "manual",
                    "tag": "network",
                    "file_type": "md",
                    "status": "parsed",
                    "source": "uploads",
                    "created_from": "2026-01-01",
                    "created_to": "2026-12-31",
                },
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual(("kb-a",), fake_service.list_document_calls[0]["scope"].selected_knowledge_base_ids)
        self.assertEqual("manual", fake_service.list_document_calls[0]["filters"]["q"])
        self.assertEqual("network", fake_service.list_document_calls[0]["filters"]["tag"])
        self.assertEqual("md", fake_service.list_document_calls[0]["filters"]["file_type"])

    def test_staged_upload_batch_routes_are_scoped(self):
        module = self.import_main()
        fake_service = FakeRagService()
        module.rag_service = fake_service

        with TestClient(module.app) as client:
            created = client.post(
                "/knowledge-bases/kb-a/upload-batches",
                json={"settings": {"chunk_size": 800}},
            )
            uploaded = client.post(
                "/knowledge-bases/kb-a/upload-batches/batch-1/files",
                files={"file": ("manual.md", b"# Manual", "text/markdown")},
                data={"relative_path": "folder/manual.md"},
            )
            settings = client.patch(
                "/knowledge-bases/kb-a/upload-batches/batch-1/settings",
                json={"settings": {"chunk_size": 900}},
            )
            fetched = client.get("/knowledge-bases/kb-a/upload-batches/batch-1")
            confirmed = client.post("/knowledge-bases/kb-a/upload-batches/batch-1/confirm")
            retried = client.post("/knowledge-bases/kb-a/upload-batches/batch-1/files/file-1/retry")
            canceled = client.post("/knowledge-bases/kb-a/upload-batches/batch-1/cancel")

        self.assertEqual(201, created.status_code)
        self.assertEqual("ready_to_process", uploaded.json()["status"])
        self.assertEqual("folder/manual.md", uploaded.json()["files"][0]["relative_path"])
        self.assertEqual({"chunk_size": 900}, settings.json()["settings"])
        self.assertEqual("batch-1", fetched.json()["id"])
        self.assertEqual("processing", confirmed.json()["status"])
        self.assertEqual(200, retried.status_code)
        self.assertEqual("canceled", canceled.json()["status"])
        scopes = [call["scope"].selected_knowledge_base_ids for call in fake_service.upload_batch_calls if "scope" in call]
        self.assertTrue(scopes)
        self.assertTrue(all(scope == ("kb-a",) for scope in scopes))
        self.assertIn("process_background", [call["action"] for call in fake_service.upload_batch_calls])

    def test_staged_upload_batch_api_rejects_archived_cross_kb_and_reset_required(self):
        module = self.import_main()

        class RejectingUploadService(FakeRagService):
            def create_upload_batch(self, scope, settings=None):
                if scope.knowledge_base_id == "archived-kb":
                    raise ValueError("Knowledge base is archived")
                if scope.knowledge_base_id == "reset-kb":
                    raise ValueError("Knowledge storage reset is required before uploading documents")
                return super().create_upload_batch(scope, settings)

            def get_upload_batch(self, batch_id, scope):
                if batch_id == "other-kb-batch":
                    raise KeyError(batch_id)
                return super().get_upload_batch(batch_id, scope)

        fake_service = RejectingUploadService()
        module.rag_service = fake_service

        with TestClient(module.app) as client:
            archived = client.post("/knowledge-bases/archived-kb/upload-batches", json={"settings": {}})
            reset = client.post("/knowledge-bases/reset-kb/upload-batches", json={"settings": {}})
            cross_kb = client.get("/knowledge-bases/kb-a/upload-batches/other-kb-batch")

        self.assertEqual(409, archived.status_code)
        self.assertEqual(409, reset.status_code)
        self.assertIn("reset", reset.json()["detail"])
        self.assertEqual(404, cross_kb.status_code)
        self.assertEqual({"detail": "Upload batch not found"}, cross_kb.json())

    def test_chat_stream_sends_reasoning_summary_event_before_tokens(self):
        module = self.import_main()
        module.rag_service = FakeRagService()
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()

        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "8个电口"})

        payload = response.text
        self.assertIn('"sources"', payload)
        self.assertIn('"reasoning"', payload)
        self.assertIn('"term_mappings": ["电口 -> RJ-45"]', payload)
        self.assertLess(payload.index('"reasoning"'), payload.index('"token"'))
        self.assertNotIn('"agent_trace"', payload)

    def test_chat_stream_optionally_sends_agent_trace_before_tokens(self):
        module = self.import_main()
        fake_rag = FakeRagService()
        fake_rag.agent_trace_stream_enabled = True
        module.rag_service = fake_rag
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()

        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "Does API depend on Redis?"})

        payload = response.text
        self.assertIn('"agent_trace"', payload)
        self.assertLess(payload.index('"agent_trace"'), payload.index('"token"'))
        self.assertIn('"sources"', payload)
        self.assertIn("[DONE]", payload)

    def test_chat_stream_quick_trace_keeps_sse_order_and_all_public_stages(self):
        module = self.import_main()

        class RichQuickTraceRagService(FakeRagService):
            def build_chat_agent_trace(self, question, hits):
                if not self.agent_trace_stream_enabled:
                    return []
                return [
                    {"stage": "UnderstandQuestion", "status": "completed", "summary": "理解问题"},
                    {"stage": "RetrieveKnowledgeBase", "status": "completed", "summary": "检索知识库"},
                    {"stage": "ReadEvidence", "status": "completed", "summary": "引用了 1 篇文档"},
                    {"stage": "SynthesizeAnswer", "status": "completed", "summary": "思考"},
                    {"stage": "Complete", "status": "completed", "summary": "完成"},
                ]

        fake_rag = RichQuickTraceRagService()
        fake_rag.agent_trace_stream_enabled = True
        module.rag_service = fake_rag
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()

        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "TSFP-CU1M-DAC 可适配哪些交换机？", "chat_mode": "quick"})

        payload = response.text
        self.assertLess(payload.index('"sources"'), payload.index('"reasoning"'))
        self.assertLess(payload.index('"reasoning"'), payload.index('"agent_trace"'))
        self.assertLess(payload.index('"agent_trace"'), payload.index('"token"'))
        self.assertLess(payload.index('"token"'), payload.index("[DONE]"))
        for stage in ["UnderstandQuestion", "RetrieveKnowledgeBase", "ReadEvidence", "SynthesizeAnswer", "Complete"]:
            self.assertIn(stage, payload)
        self.assertNotIn('"tool_call"', payload)

    def test_chat_stream_creates_conversation_and_emits_memory_update(self):
        module = self.import_main()
        fake_rag = FakeRagService()
        fake_conversation = FakeConversationService()
        fake_memory = FakeMemoryService()
        module.rag_service = fake_rag
        module.conversation_service = fake_conversation
        module.memory_service = fake_memory

        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "以后请用中文回答。"})

        payload = response.text
        self.assertIn('"conversation_id": "conv-new"', payload)
        self.assertIn('"memory_updated"', payload)
        self.assertLess(payload.index('"conversation_id"'), payload.index('"sources"'))
        self.assertLess(payload.index('"memory_updated"'), payload.index("[DONE]"))
        self.assertEqual(["user", "assistant"], [item["role"] for item in fake_conversation.appended])
        self.assertEqual("answer", fake_conversation.appended[1]["content"])
        self.assertEqual("conv-new", fake_memory.extract_calls[0]["conversation_id"])
        self.assertIn("长期记忆", fake_rag.stream_calls[0]["memory_context"])
        self.assertTrue(fake_rag.stream_calls[0]["scope"].compatibility_default)
        self.assertEqual(
            ("default-knowledge-base",),
            fake_rag.stream_calls[0]["scope"].selected_knowledge_base_ids,
        )

    def test_chat_attachment_upload_binds_to_current_stream_without_indexing(self):
        module = self.import_main()
        fake_rag = FakeRagService()
        fake_conversation = FakeConversationService()
        module.rag_service = fake_rag
        module.conversation_service = fake_conversation
        module.memory_service = FakeMemoryService()
        with tempfile.TemporaryDirectory() as tmpdir:
            module.temporary_attachment_repository = TemporaryAttachmentRepository(Path(tmpdir))
            with TestClient(module.app) as client:
                upload = client.post(
                    "/chat/attachments",
                    files={"file": ("notes.txt", "DH-P5000 supports SN binding".encode("utf-8"), "text/plain")},
                )
                attachment_id = upload.json()["id"]
                response = client.post(
                    "/chat/stream",
                    json={"message": "Summarize attachment", "chat_mode": "quick", "attachment_ids": [attachment_id]},
                )
                documents = client.get("/documents")

        self.assertEqual(200, upload.status_code)
        payload = response.text
        self.assertIn("临时附件: notes.txt", payload)
        self.assertIn('"source_type": "temporary_attachment"', payload)
        self.assertIn("DH-P5000 supports SN binding", fake_rag.stream_calls[0]["memory_context"])
        self.assertEqual([attachment_id], fake_conversation.appended[0]["metadata_json"]["temporary_attachment_ids"])
        self.assertNotIn("notes.txt", documents.text)

    def test_chat_stream_rejects_missing_temporary_attachment(self):
        module = self.import_main()
        module.rag_service = FakeRagService()
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()
        with tempfile.TemporaryDirectory() as tmpdir:
            module.temporary_attachment_repository = TemporaryAttachmentRepository(Path(tmpdir))
            with TestClient(module.app) as client:
                response = client.post(
                    "/chat/stream",
                    json={"message": "Use missing file", "chat_mode": "quick", "attachment_ids": ["att_missing"]},
                )

        self.assertEqual(400, response.status_code)
        self.assertIn("not found or expired", response.json()["detail"])

    def test_chat_stream_continues_existing_conversation(self):
        module = self.import_main()
        fake_rag = FakeRagService()
        fake_conversation = FakeConversationService()
        module.rag_service = fake_rag
        module.conversation_service = fake_conversation
        module.memory_service = FakeMemoryService()

        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "continue", "conversation_id": "conv-existing"})

        payload = response.text
        self.assertIn('"conversation_id": "conv-existing"', payload)
        self.assertEqual("conv-existing", fake_conversation.appended[0]["conversation_id"])
        self.assertEqual("Earlier summary", fake_rag.stream_calls[0]["conversation_context"]["summary"])

    def test_document_processing_retry_route_returns_queue_runtime_fields(self):
        module = self.import_main()
        module.rag_service = FakeRagService()

        with TestClient(module.app) as client:
            response = client.post("/documents/doc-1/processing/retry?knowledge_base_id=kb-a")

        payload = response.json()
        self.assertEqual(200, response.status_code)
        self.assertEqual("retrying", payload["processing_task_status"])
        self.assertEqual("process_document", payload["processing_task_type"])
        self.assertEqual("core", payload["processing_task_queue"])
        self.assertEqual("broker-1", payload["processing_broker_task_id"])
        self.assertFalse(payload["processing_retry_available"])

    def test_chat_stream_replays_existing_stream_before_completion(self):
        module = self.import_main()
        module.rag_service = FakeRagService()
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()
        module.chat_stream_manager = MemoryStreamManager()
        identity = StreamIdentity("conv-existing", "msg-stream")
        module.chat_stream_manager.append(identity, ChatStreamEvent("conversation_id", {"conversation_id": "conv-existing"}))
        module.chat_stream_manager.append(identity, ChatStreamEvent("token", {"token": "partial"}))

        with TestClient(module.app) as client:
            response = client.post(
                "/chat/stream",
                json={
                    "message": "resume",
                    "conversation_id": "conv-existing",
                    "stream_message_id": "msg-stream",
                    "stream_offset": 1,
                },
            )

        self.assertEqual(200, response.status_code)
        self.assertIn('"token": "partial"', response.text)
        self.assertIn('"offset": 2', response.text)
        self.assertNotIn("[DONE]", response.text)
        self.assertEqual([], module.rag_service.stream_calls)

    def test_chat_stream_replays_completed_stream_with_done_marker(self):
        module = self.import_main()
        module.rag_service = FakeRagService()
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()
        module.chat_stream_manager = MemoryStreamManager()
        identity = StreamIdentity("conv-existing", "msg-done")
        module.chat_stream_manager.append(identity, ChatStreamEvent("token", {"token": "answer"}))
        module.chat_stream_manager.append(identity, ChatStreamEvent("done", {}, terminal=True))

        with TestClient(module.app) as client:
            response = client.post(
                "/chat/stream",
                json={
                    "message": "resume",
                    "conversation_id": "conv-existing",
                    "stream_message_id": "msg-done",
                    "stream_offset": 0,
                },
            )

        self.assertIn('"token": "answer"', response.text)
        self.assertIn("[DONE]", response.text)
        self.assertEqual([], module.rag_service.stream_calls)

    def test_chat_stream_replays_fatal_error_before_done_marker(self):
        module = self.import_main()
        module.rag_service = FakeRagService()
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()
        module.chat_stream_manager = MemoryStreamManager()
        identity = StreamIdentity("conv-existing", "msg-error")
        module.chat_stream_manager.append(identity, ChatStreamEvent("error", {"error": "provider failed"}))
        module.chat_stream_manager.append(identity, ChatStreamEvent("done", {}, terminal=True))

        with TestClient(module.app) as client:
            response = client.post(
                "/chat/stream",
                json={
                    "message": "resume",
                    "conversation_id": "conv-existing",
                    "stream_message_id": "msg-error",
                    "stream_offset": 0,
                },
            )

        self.assertLess(response.text.index('"error": "provider failed"'), response.text.index("[DONE]"))

    def test_chat_stream_stop_persists_replayable_stop_event(self):
        module = self.import_main()
        module.rag_service = FakeRagService()
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()
        module.chat_stream_manager = MemoryStreamManager()

        with TestClient(module.app) as client:
            stopped = client.post(
                "/chat/stream/stop",
                json={"conversation_id": "conv-existing", "stream_message_id": "msg-stop", "reason": "user_cancel"},
            )
            replay = client.post(
                "/chat/stream",
                json={
                    "message": "resume",
                    "conversation_id": "conv-existing",
                    "stream_message_id": "msg-stop",
                    "stream_offset": 0,
                },
            )

        self.assertEqual("stopped", stopped.json()["status"])
        self.assertIn('"reason": "user_cancel"', replay.text)
        self.assertIn("[DONE]", replay.text)

    def test_chat_stream_old_client_payload_still_streams_with_metadata(self):
        module = self.import_main()
        module.rag_service = FakeRagService()
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()
        module.chat_stream_manager = MemoryStreamManager()

        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "old client"})

        self.assertIn('"conversation_id": "conv-new"', response.text)
        self.assertIn('"stream_message_id": "msg-1"', response.text)
        self.assertIn('"_stream"', response.text)
        self.assertIn("[DONE]", response.text)

    def test_chat_stream_respects_memory_disabled(self):
        module = self.import_main()
        fake_rag = FakeRagService()
        fake_memory = FakeMemoryService()
        module.rag_service = fake_rag
        module.conversation_service = FakeConversationService()
        module.memory_service = fake_memory

        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "以后请用中文回答。", "memory_enabled": False})

        payload = response.text
        self.assertNotIn('"memory_updated"', payload)
        self.assertEqual([], fake_memory.recall_calls)
        self.assertFalse(fake_memory.extract_calls[0]["memory_enabled"])
        self.assertEqual("", fake_rag.stream_calls[0]["memory_context"])

    def test_memory_management_routes_list_and_delete_memories(self):
        module = self.import_main()
        fake_memory = FakeMemoryService()
        module.rag_service = FakeRagService()
        module.memory_service = fake_memory

        with TestClient(module.app) as client:
            listed = client.get("/memories")
            deleted = client.delete("/memories/mem-1")
            listed_after_delete = client.get("/memories")

        self.assertEqual(200, listed.status_code)
        self.assertEqual("mem-1", listed.json()["items"][0]["id"])
        self.assertEqual({"id": "mem-1", "status": "deleted"}, deleted.json())
        self.assertEqual([], listed_after_delete.json()["items"])

    def test_memory_delete_unknown_returns_404(self):
        module = self.import_main()
        module.rag_service = FakeRagService()
        module.memory_service = FakeMemoryService()

        with TestClient(module.app) as client:
            response = client.delete("/memories/missing")

        self.assertEqual(404, response.status_code)


if __name__ == "__main__":
    unittest.main()
