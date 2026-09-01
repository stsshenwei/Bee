import threading
import unittest
from types import SimpleNamespace

from app.services.chat_pipeline import (
    ChatPipelineContext,
    ChatPipelineExecutor,
    ChatPipelineEvent,
    ChatPipelineRegistry,
    ChatPipelineRequest,
    ChatPipelineRuntime,
    StageId,
    default_chat_pipeline_registry,
    run_quick_rag_pipeline,
    run_retrieval_only_pipeline,
)


class Scope:
    compatibility_default = True
    selected_knowledge_base_ids = ("default-knowledge-base",)

    def to_dict(self):
        return {"knowledge_base_ids": list(self.selected_knowledge_base_ids)}


class FakeConversationService:
    def __init__(self):
        self.appended = []
        self.summarized = []

    @property
    def repository(self):
        return self

    def build_context(self, conversation_id):
        return {"conversation_id": conversation_id, "summary": "Earlier", "recent_messages": []}

    def append_message(self, conversation_id, role, content, metadata_json=None):
        self.appended.append(
            {
                "conversation_id": conversation_id,
                "role": role,
                "content": content,
                "metadata_json": metadata_json or {},
            }
        )
        return {"id": f"msg-{len(self.appended)}"}

    def maybe_summarize(self, conversation_id):
        self.summarized.append(conversation_id)
        return ""


class FakeMemoryService:
    def __init__(self):
        self.recall_calls = []
        self.process_calls = []

    def recall_memories(self, question, limit=8, scope=None):
        self.recall_calls.append(question)
        return [{"scope": "user", "type": "preference", "content": "use Chinese"}]

    def format_prompt_context(self, memories):
        return "\n".join(memory["content"] for memory in memories)

    def process_exchange(self, **kwargs):
        self.process_calls.append(kwargs)
        return [{"action": "upserted", "id": "mem-1"}] if kwargs.get("memory_enabled") else []


class FakeRagService:
    top_k = 4

    def __init__(self):
        self.query_understanding = SimpleNamespace(
            understand=lambda question: SimpleNamespace(
                to_dict=lambda: {
                    "original_query": question,
                    "normalized_query": question,
                    "retrieval_queries": [question],
                    "source": "fallback",
                }
            )
        )
        self._last_retrieval_debug = {}
        self.stream_calls = []

    def hybrid_retrieve_hits(self, question, scope=None):
        self._last_retrieval_debug = {"rerank": {"fallback_used": False}}
        return [{"content": "child", "metadata": {"source": "manual.md", "chunk_id": "c1"}, "hybrid_score": 0.8}]

    def recall_parent_hits(self, hits, scope=None):
        return [{"content": "parent", "metadata": {"source": "manual.md", "matched_child_ids": ["c1"]}, "hybrid_score": 0.8}]

    def extract_sources(self, hits):
        return [{"source": "manual.md", "score": 0.8}]

    def build_reasoning_summary(self, question, hits):
        return {"question": question, "evidence": [{"source": "manual.md"}]}

    def build_chat_agent_trace(self, question, hits):
        return [{"stage": "RetrieveKnowledgeBase", "status": "completed"}]

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
        yield "hello"
        yield " world"


class EmptyRagService(FakeRagService):
    def hybrid_retrieve_hits(self, question, scope=None):
        self._last_retrieval_debug = {"rerank": {"fallback_used": False}, "fused_results": []}
        return []

    def recall_parent_hits(self, hits, scope=None):
        return []

    def extract_sources(self, hits):
        return []


class StopAfterFirstTokenRagService(FakeRagService):
    def __init__(self, stop_signal):
        super().__init__()
        self.stop_signal = stop_signal

    def stream_answer(self, question, hits=None, conversation_context=None, memory_context=None, scope=None):
        yield "partial"
        self.stop_signal.set()
        yield "ignored"


class RecordingStage:
    def __init__(self, stage_id, events=None):
        self.stage_id = stage_id
        self.events = events or []

    def run(self, context):
        context.state.stage_progress.append({"custom": self.stage_id})
        return [ChatPipelineEvent("token", {"token": token}) for token in self.events]


class ChatPipelineTests(unittest.TestCase):
    def make_context(self, *, stop_signal=None):
        return ChatPipelineContext(
            request=ChatPipelineRequest(
                question="question",
                conversation_id="conv-1",
                stream_message_id="msg-1",
                scope=Scope(),
                user_message_id="user-msg-1",
                temporary_context="attachment context",
                temporary_sources=[{"source": "attachment.txt", "temporary_attachment_id": "att-1"}],
            ),
            runtime=ChatPipelineRuntime(
                rag_service=FakeRagService(),
                conversation_service=FakeConversationService(),
                memory_service=FakeMemoryService(),
                stop_signal=stop_signal,
            ),
        )

    def test_context_registry_and_ordered_execution(self):
        context = self.make_context()
        registry = ChatPipelineRegistry()
        registry.register(RecordingStage("first", ["a"]))
        registry.register(RecordingStage("second", ["b"]))

        events = list(ChatPipelineExecutor(registry).run(context, ["first", "second"]))

        self.assertEqual(["a", "b"], [event.payload["token"] for event in events])
        self.assertIn("first", registry.stage_ids())
        self.assertEqual("question", context.request.question)

    def test_missing_stage_emits_error_and_done(self):
        events = list(ChatPipelineExecutor(ChatPipelineRegistry()).run(self.make_context(), ["missing"]))

        self.assertEqual(["error", "done"], [event.event_type for event in events])
        self.assertTrue(events[-1].terminal)
        self.assertEqual("missing", events[0].payload["pipeline"]["stage_id"])

    def test_quick_rag_pipeline_preserves_public_order_and_persists(self):
        context = self.make_context()

        events = list(run_quick_rag_pipeline(context, registry=default_chat_pipeline_registry()))

        self.assertEqual(
            ["conversation_id", "sources", "reasoning", "agent_trace", "token", "token", "memory_updated", "done"],
            [event.event_type for event in events],
        )
        self.assertEqual("hello world", context.state.answer)
        self.assertEqual(["user", "assistant"][-1], context.runtime.conversation_service.appended[0]["role"])
        self.assertEqual("conv-1", context.runtime.memory_service.process_calls[0]["conversation_id"])
        self.assertEqual("attachment.txt", context.state.sources[-1]["source"])
        self.assertTrue(context.state.stage_progress)

    def test_stop_before_stage_emits_stop_and_done_without_persisting(self):
        signal = threading.Event()
        signal.set()
        context = self.make_context(stop_signal=signal)

        events = list(run_quick_rag_pipeline(context))

        self.assertEqual(["stop", "done"], [event.event_type for event in events])
        self.assertEqual([], context.runtime.conversation_service.appended)

    def test_empty_retrieval_still_emits_sources_reasoning_token_and_done(self):
        context = self.make_context()
        context.runtime.rag_service = EmptyRagService()

        events = list(run_quick_rag_pipeline(context))

        sources = next(event.payload["sources"] for event in events if event.event_type == "sources")
        self.assertEqual(["attachment.txt"], [source["source"] for source in sources])
        self.assertIn("reasoning", [event.event_type for event in events])
        self.assertIn("token", [event.event_type for event in events])
        self.assertEqual("done", events[-1].event_type)

    def test_stop_during_streaming_avoids_assistant_persistence(self):
        signal = threading.Event()
        context = self.make_context(stop_signal=signal)
        context.runtime.rag_service = StopAfterFirstTokenRagService(signal)

        events = list(run_quick_rag_pipeline(context))

        self.assertEqual(["token", "stop", "done"], [event.event_type for event in events[-3:]])
        self.assertEqual("partial", context.state.answer)
        self.assertEqual([], context.runtime.conversation_service.appended)

    def test_retrieval_only_pipeline_returns_hits_without_chat_completion(self):
        context = self.make_context()

        result = run_retrieval_only_pipeline(context)

        self.assertEqual("parent", result.state.hits[0]["content"])
        self.assertEqual([], result.state.answer_parts)
        self.assertEqual([], result.runtime.rag_service.stream_calls)


if __name__ == "__main__":
    unittest.main()
