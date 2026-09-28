import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.models.agent_runtime import AgentRuntimeEvent
from tests.test_agentic_tools_workflow import EmptyGraphRetriever, FakeGraphRetriever, FakeRAGService as WorkflowFakeRAGService
from tests.test_rag_api_routes import FakeConversationService, FakeMemoryService, FakeRagService as RouteFakeRagService
from tests.test_runtime_config import postgres_runtime_patches


def build_workflow(rag=None, graph_retriever=None):
    from app.models.agentic_retrieval import AgenticRetrievalConfig
    from app.services.agent.agent_tools import GraphRetrieverTool, KeywordSearchTool, RawRAGTool
    from app.services.agent.agentic_workflow import AgenticRetrievalWorkflow
    from app.services.retrieval.citation_verifier import CitationVerifier
    from app.services.agent.query_router import QueryRouter
    from app.services.agent.retrieval_planner import RetrievalPlanner

    rag = rag or WorkflowFakeRAGService()
    graph_retriever = graph_retriever or FakeGraphRetriever()
    return AgenticRetrievalWorkflow(
        router=QueryRouter(),
        planner=RetrievalPlanner(),
        tools={
            "RawRAGTool": RawRAGTool(rag),
            "KeywordSearchTool": KeywordSearchTool(rag),
            "GraphRetrieverTool": GraphRetrieverTool(graph_retriever),
        },
        citation_verifier=CitationVerifier(rag.document_repository),
        rag_service=rag,
        config=AgenticRetrievalConfig(enabled=True),
    )


class ChatAgentFakeRAGService(WorkflowFakeRAGService):
    def __init__(self):
        super().__init__()
        self.chat_agentic_workflow_enabled = True
        self.agent_trace_stream_enabled = False
        self.stream_calls = []

    def needs_reingest(self):
        return False

    def build_reasoning_summary(self, question, hits):
        return {"question": question, "evidence": [{"source": "manual.md", "score": 0.82}]}

    def stream_answer(self, question, hits=None, conversation_context=None, memory_context=None):
        self.stream_calls.append(
            {
                "question": question,
                "hits": hits,
                "conversation_context": conversation_context,
                "memory_context": memory_context,
            }
        )
        yield "Redis "
        yield "answer"


class FakeAgentRuntime:
    def __init__(self):
        self.calls = []

    def stream_query_events(self, question, conversation_context=None, memory_context=None, scope=None, attachments=None, mode="reasoning"):
        self.calls.append({"question": question, "mode": mode})
        yield AgentRuntimeEvent("agent_query", {"summary": "query", "sequence": 1})
        yield AgentRuntimeEvent("agent_thought", {"summary": "thinking", "sequence": 2})
        yield AgentRuntimeEvent("agent_trace", {"stage": "AgentRuntimeStart", "status": "running", "summary": "start"})
        batch_metadata = {"batch_id": "batch-1", "round": 1}
        yield AgentRuntimeEvent(
            "agent_tool_call",
            {
                "tool": "grep_chunks",
                "input_summary": "Redis|cache",
                "call_id": "call-1",
                "metadata": {**batch_metadata, "call_index": 1},
            },
        )
        yield AgentRuntimeEvent(
            "tool_call",
            {"tool": "grep_chunks", "input_summary": "Redis|cache", "call_id": "call-1"},
        )
        yield AgentRuntimeEvent(
            "agent_tool_call",
            {
                "tool": "knowledge_search",
                "input_summary": question,
                "call_id": "call-2",
                "metadata": {**batch_metadata, "call_index": 2},
            },
        )
        yield AgentRuntimeEvent(
            "tool_call",
            {"tool": "knowledge_search", "input_summary": question, "call_id": "call-2"},
        )
        yield AgentRuntimeEvent(
            "agent_tool_result",
            {
                "tool": "knowledge_search",
                "status": "completed",
                "output_summary": "找到 1 条语义候选",
                "call_id": "call-2",
                "metadata": {**batch_metadata, "call_index": 2, "physical_completion_index": 1},
            },
        )
        yield AgentRuntimeEvent(
            "tool_observation",
            {"tool": "knowledge_search", "status": "completed", "output_summary": "找到 1 条语义候选", "call_id": "call-2"},
        )
        yield AgentRuntimeEvent(
            "agent_tool_result",
            {
                "tool": "grep_chunks",
                "status": "completed",
                "output_summary": "找到 1 条关键词候选",
                "call_id": "call-1",
                "metadata": {**batch_metadata, "call_index": 1, "physical_completion_index": 2},
            },
        )
        yield AgentRuntimeEvent(
            "tool_observation",
            {"tool": "grep_chunks", "status": "completed", "output_summary": "找到 1 条关键词候选", "call_id": "call-1"},
        )
        yield AgentRuntimeEvent("agent_reflection", {"summary": "evidence ok", "completion_status": "sufficient"})
        yield AgentRuntimeEvent("agent_references", {"items": [{"source": "manual.md", "score": 0.9}], "summary": "refs"})
        yield AgentRuntimeEvent("sources", {"items": [{"source": "manual.md", "score": 0.9}]})
        yield AgentRuntimeEvent("evidence_summary", {"sufficient": True, "used_chunks": 1})
        yield AgentRuntimeEvent("agent_final_answer", {"answer": "runtime answer"})
        yield AgentRuntimeEvent("token", {"token": "runtime "})
        yield AgentRuntimeEvent("token", {"token": "answer"})
        yield AgentRuntimeEvent("agent_complete", {"summary": "complete"})
        yield AgentRuntimeEvent("final", {"answer": "runtime answer", "citations": [{"source": "manual.md", "score": 0.9}]})


class AgenticChatStreamWorkflowTests(unittest.TestCase):
    def build_workflow(self, rag=None, graph_retriever=None):
        return build_workflow(rag, graph_retriever)

    def test_stream_query_events_emits_tool_events_before_tokens_and_final_metadata(self):
        workflow = self.build_workflow()

        events = list(
            workflow.stream_query_events(
                "What is Redis?",
                conversation_context={"summary": "earlier"},
                memory_context="[memory]\n- preference",
            )
        )
        event_types = [event.event_type for event in events]

        self.assertEqual("agent_trace", event_types[0])
        self.assertIn("tool_call", event_types)
        self.assertIn("tool_observation", event_types)
        self.assertIn("evidence_summary", event_types)
        self.assertIn("sources", event_types)
        self.assertIn("reasoning", event_types)
        self.assertIn("citation_verification", event_types)
        self.assertIn("token", event_types)
        self.assertEqual("final", event_types[-1])
        self.assertLess(event_types.index("tool_call"), event_types.index("tool_observation"))
        document_reads = [event for event in events if event.event_type == "tool_call" and event.payload.get("tool") == "DocumentChunkReaderTool"]
        self.assertTrue(document_reads)
        self.assertIn("查看文章", document_reads[0].payload["input_summary"])
        self.assertLess(event_types.index("sources"), event_types.index("token"))
        self.assertLess(event_types.index("reasoning"), event_types.index("token"))
        self.assertEqual("Redis is used by API Gateway. [manual.md]", events[-1].payload["answer"])
        self.assertNotIn("chain_of_thought", str([event.to_dict() for event in events]))
        self.assertNotIn("preference", str([event.to_dict() for event in events if event.event_type == "tool_call"]))

    def test_stream_query_events_streams_insufficient_evidence_for_missing_dependency_path(self):
        workflow = self.build_workflow(graph_retriever=EmptyGraphRetriever())

        events = list(workflow.stream_query_events("Does API Gateway depend on Redis?"))
        tokens = [event.payload["token"] for event in events if event.event_type == "token"]
        verification = [event for event in events if event.event_type == "citation_verification"][-1]

        self.assertIn("cannot determine", "".join(tokens))
        self.assertIn("valid", verification.payload)
        self.assertEqual("final", events[-1].event_type)


class AgenticChatStreamRouteTests(unittest.TestCase):
    def import_main(self, env=None):
        sys.modules.pop("app.main", None)
        with tempfile.TemporaryDirectory() as tmpdir:
            full_env = {
                "OPENAI_API_KEY": "test-key",
                "OPENAI_BASE_URL": "",
                "VECTOR_STORE_DIR": str(Path(tmpdir) / "vector_db"),
                "METADATA_DB_PATH": str(Path(tmpdir) / "metadata.sqlite3"),
                "DATABASE_URL": "postgresql://rag:rag@localhost:5432/rag_test",
                "POSTGRES_SCHEMA": "rag",
                "RAG_DATA_DIR": str(Path(tmpdir) / "data"),
                "AUTO_INGEST_ON_STARTUP": "false",
                "STREAM_MANAGER_TYPE": "memory",
                "LANGFUSE_ENABLED": "false",
                **(env or {}),
            }
            with patch.dict(os.environ, full_env, clear=False):
                with postgres_runtime_patches():
                    return importlib.import_module("app.main")

    def test_runtime_config_can_enable_chat_agent_without_rag_query_agent(self):
        module = self.import_main({"CHAT_AGENTIC_WORKFLOW_ENABLED": "true", "AGENTIC_RETRIEVAL_ENABLED": "false"})
        service = module.rag_service

        self.assertTrue(service.chat_agentic_workflow_enabled)
        self.assertFalse(service.agentic_retrieval_enabled)
        self.assertIsNotNone(service.agentic_workflow)

    def test_chat_stream_uses_agentic_workflow_when_enabled_and_preserves_memory(self):
        module = self.import_main({"CHAT_AGENTIC_WORKFLOW_ENABLED": "true"})
        fake_rag = ChatAgentFakeRAGService()
        fake_rag.chat_agentic_workflow_enabled = True
        fake_rag.agentic_workflow = build_workflow(fake_rag)
        module.rag_service = fake_rag
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()

        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "What is Redis?"})

        payload = response.text
        self.assertIn('"conversation_id": "conv-new"', payload)
        self.assertIn('"agent_trace"', payload)
        self.assertIn('"tool_call"', payload)
        self.assertIn('"tool_observation"', payload)
        self.assertIn('"evidence_summary"', payload)
        self.assertIn('"citation_verification"', payload)
        self.assertIn('"sources"', payload)
        self.assertIn('"reasoning"', payload)
        self.assertIn('"token"', payload)
        self.assertIn('"memory_updated"', payload)
        self.assertIn("[DONE]", payload)
        self.assertLess(payload.index('"conversation_id"'), payload.index('"agent_trace"'))
        self.assertLess(payload.index('"sources"'), payload.index('"token"'))
        self.assertEqual(["user", "assistant"], [message["role"] for message in module.conversation_service.appended])
        self.assertIn("Redis answer", module.conversation_service.appended[1]["content"])
        self.assertEqual("conv-new", module.memory_service.extract_calls[0]["conversation_id"])

    def test_chat_stream_quick_mode_forces_raw_path_when_agentic_enabled(self):
        module = self.import_main({"CHAT_AGENTIC_WORKFLOW_ENABLED": "true"})
        fake_rag = RouteFakeRagService()
        fake_rag.chat_agentic_workflow_enabled = True
        fake_rag.agentic_workflow = object()
        fake_runtime = FakeAgentRuntime()
        fake_rag.agent_runtime_enabled = True
        fake_rag.agent_runtime = fake_runtime
        module.rag_service = fake_rag
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()

        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "What is Redis?", "chat_mode": "quick"})

        payload = response.text
        self.assertIn('"token": "answer"', payload)
        self.assertNotIn('"tool_call"', payload)
        self.assertEqual("quick", module.conversation_service.appended[0]["metadata_json"]["chat_mode"])
        self.assertEqual("quick", module.conversation_service.appended[1]["metadata_json"]["chat_mode"])
        self.assertTrue(fake_rag.stream_calls)
        self.assertEqual([], fake_runtime.calls)

    def test_chat_stream_quick_mode_can_use_unified_runtime_when_enabled(self):
        module = self.import_main({"CHAT_UNIFIED_RUNTIME_ENABLED": "true"})
        fake_rag = RouteFakeRagService()
        fake_runtime = FakeAgentRuntime()
        fake_rag.unified_chat_runtime_enabled = True
        fake_rag.quick_runtime_enabled = True
        fake_rag.agent_runtime_enabled = False
        fake_rag.agent_runtime = fake_runtime
        fake_rag.agentic_workflow = None
        module.rag_service = fake_rag
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()

        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "What is Redis?", "chat_mode": "quick"})

        payload = response.text
        self.assertIn('"agent_query"', payload)
        self.assertIn('"agent_references"', payload)
        self.assertIn('"agent_complete"', payload)
        self.assertIn('"token": "runtime "', payload)
        self.assertIn('"token": "answer"', payload)
        self.assertEqual([{"question": "What is Redis?", "mode": "quick"}], fake_runtime.calls)
        self.assertEqual([], fake_rag.stream_calls)

    def test_chat_stream_reasoning_mode_uses_agent_runtime_when_enabled(self):
        module = self.import_main({"AGENT_RUNTIME_ENABLED": "false", "CHAT_AGENTIC_WORKFLOW_ENABLED": "false"})
        fake_rag = RouteFakeRagService()
        fake_rag.chat_agentic_workflow_enabled = False
        fake_rag.agentic_workflow = None
        fake_rag.agent_runtime_enabled = True
        fake_rag.agent_runtime = FakeAgentRuntime()
        module.rag_service = fake_rag
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()

        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "What is Redis?", "chat_mode": "reasoning"})

        payload = response.text
        self.assertIn('"agent_trace"', payload)
        self.assertIn('"agent_query"', payload)
        self.assertIn('"agent_thought"', payload)
        self.assertIn('"agent_tool_call"', payload)
        self.assertIn('"agent_tool_result"', payload)
        self.assertIn('"agent_reflection"', payload)
        self.assertIn('"agent_references"', payload)
        self.assertIn('"agent_final_answer"', payload)
        self.assertIn('"agent_complete"', payload)
        self.assertIn('"tool_call"', payload)
        self.assertIn('"tool_observation"', payload)
        self.assertIn('"evidence_summary"', payload)
        self.assertIn('"sources"', payload)
        self.assertIn('"token": "runtime "', payload)
        self.assertIn('"token": "answer"', payload)
        self.assertIn("[DONE]", payload)
        self.assertEqual(2, payload.count('"agent_tool_call"'))
        self.assertIn('"batch_id": "batch-1"', payload)
        self.assertLess(payload.index('"agent_references"'), payload.index('"token": "runtime "'))
        self.assertEqual(["user", "assistant"], [message["role"] for message in module.conversation_service.appended])
        self.assertEqual("runtime answer", module.conversation_service.appended[1]["content"])
        self.assertNotIn('"answer": "runtime answer"', payload[payload.index('"final"') :])

    def test_chat_stream_reasoning_mode_reports_error_when_unavailable(self):
        module = self.import_main({"CHAT_AGENTIC_WORKFLOW_ENABLED": "false"})
        fake_rag = ChatAgentFakeRAGService()
        fake_rag.chat_agentic_workflow_enabled = False
        fake_rag.agentic_workflow = None
        module.rag_service = fake_rag
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()

        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "What is Redis?", "chat_mode": "reasoning"})

        payload = response.text
        self.assertIn('"error"', payload)
        self.assertIn("智能推理暂不可用", payload)
        self.assertNotIn('"token"', payload)
        self.assertEqual(["user"], [message["role"] for message in module.conversation_service.appended])

    def test_chat_stream_disabled_keeps_raw_rag_fallback_shape(self):
        module = self.import_main({"CHAT_AGENTIC_WORKFLOW_ENABLED": "false"})
        fake_rag = RouteFakeRagService()
        fake_rag.chat_agentic_workflow_enabled = False
        module.rag_service = fake_rag
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()

        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "What is Redis?", "memory_enabled": False})

        payload = response.text
        self.assertIn('"sources"', payload)
        self.assertIn('"reasoning"', payload)
        self.assertIn('"token": "answer"', payload)
        self.assertNotIn('"tool_call"', payload)
        self.assertNotIn('"tool_observation"', payload)


if __name__ == "__main__":
    unittest.main()
