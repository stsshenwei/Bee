from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.models.agent_runtime import AgentEventBus, AgentRuntimeConfig, AgentRuntimeEvent, agent_event, resolve_chat_runtime_policy
from app.models.knowledge_base import KnowledgeBaseScope
from app.services.agent.agent_prompt_templates import AgentPromptCatalog
from app.services.agent.agent_runtime import AgentRuntime, _input_summary, _question_needs_exact_grep_anchor
from app.services.agent.agent_runtime_spans import AgentRuntimeSpanRepository
from app.services.agent.agent_runtime_tools import build_default_tool_registry
from app.services.infrastructure.observability import NoopObservabilitySink, ObservationHandle, ObservabilitySink, set_observability_sink


class FakeMessage:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self):
        return {"content": self.content, "tool_calls": self.tool_calls}


class FakeChoice:
    def __init__(self, message):
        self.message = message


class FakeResponse:
    def __init__(self, message):
        self.choices = [FakeChoice(message)]


class FakeCompletions:
    def __init__(self):
        self.calls = 0
        self.last_kwargs = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.calls == 1:
            return FakeResponse(
                FakeMessage(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "knowledge_search", "arguments": '{"query":"Redis"}'},
                        }
                    ]
                )
            )
        if self.calls == 2:
            return FakeResponse(FakeMessage(content="Redis is used by API Gateway."))
        if self.calls == 3:
            return FakeResponse(
                FakeMessage(
                    tool_calls=[
                        {
                            "id": "call-2",
                            "type": "function",
                            "function": {"name": "list_knowledge_chunks", "arguments": '{"chunk_ids":["c1"]}'},
                        }
                    ]
                )
            )
        return FakeResponse(FakeMessage(content="Redis is used by API Gateway."))


class RemedialCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return FakeResponse(
                FakeMessage(
                    tool_calls=[
                        {
                            "id": "think-1",
                            "type": "function",
                            "function": {
                                "name": "thinking",
                                "arguments": '{"summary":"Need version evidence","phase":"reflection","gap":"missing version","correction_query":"Redis version","completion_status":"needs_more_evidence"}',
                            },
                        }
                    ]
                )
            )
        return FakeResponse(FakeMessage(content="Redis version is 7.2."))


class NoCorrectionThinkingCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return FakeResponse(
                FakeMessage(
                    tool_calls=[
                        {
                            "id": "think-no-correction",
                            "type": "function",
                            "function": {
                                "name": "thinking",
                                "arguments": '{"summary":"Need missing evidence","phase":"reflection","gap":"missing version","completion_status":"needs_more_evidence"}',
                            },
                        }
                    ]
                )
            )
        return FakeResponse(FakeMessage(content="Redis version is 7.2."))


class QuickDirectCompletions:
    def __init__(self):
        self.calls = 0
        self.last_kwargs = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return FakeResponse(FakeMessage(content="Redis is used by API Gateway."))


class DisallowedToolCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return FakeResponse(
                FakeMessage(
                    tool_calls=[
                        {
                            "id": "blocked-1",
                            "type": "function",
                            "function": {"name": "knowledge_search", "arguments": '{"query":"Redis"}'},
                        }
                    ]
                )
            )
        return FakeResponse(FakeMessage(content="Redis is used by API Gateway."))


class DuplicateRemedialCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return FakeResponse(
                FakeMessage(
                    tool_calls=[
                        {
                            "id": "call-search",
                            "type": "function",
                            "function": {"name": "knowledge_search", "arguments": '{"query":"Redis"}'},
                        }
                    ]
                )
            )
        if self.calls == 2:
            return FakeResponse(
                FakeMessage(
                    tool_calls=[
                        {
                            "id": "call-read",
                            "type": "function",
                            "function": {"name": "list_knowledge_chunks", "arguments": '{"chunk_ids":["c1"]}'},
                        }
                    ]
                )
            )
        if self.calls == 3:
            return FakeResponse(
                FakeMessage(
                    tool_calls=[
                        {
                            "id": "think-gap",
                            "type": "function",
                            "function": {
                                "name": "thinking",
                                "arguments": '{"summary":"Need more","phase":"reflection","gap":"missing duplicate-only evidence","correction_query":"Redis","completion_status":"needs_more_evidence"}',
                            },
                        }
                    ]
                )
            )
        return FakeResponse(FakeMessage(content="Unsupported answer"))


class SkipsGrepThenCorrectsCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return FakeResponse(FakeMessage(content="Redis is used by API Gateway."))
        if self.calls == 2:
            return FakeResponse(
                FakeMessage(
                    tool_calls=[
                        {
                            "id": "grep-1",
                            "type": "function",
                            "function": {
                                "name": "grep_chunks",
                                "arguments": '{"queries":["Redis","API Gateway"],"required_terms":["used by"],"top_k":2}',
                            },
                        }
                    ]
                )
            )
        if self.calls == 3:
            return FakeResponse(
                FakeMessage(
                    tool_calls=[
                        {
                            "id": "read-1",
                            "type": "function",
                            "function": {"name": "list_knowledge_chunks", "arguments": '{"chunk_ids":["c1"]}'},
                        }
                    ]
                )
            )
        return FakeResponse(FakeMessage(content="Redis is used by API Gateway."))


class GrepThenSkipsDeepReadCompletions:
    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return FakeResponse(
                FakeMessage(
                    tool_calls=[
                        {
                            "id": "grep-1",
                            "type": "function",
                            "function": {"name": "grep_chunks", "arguments": '{"query":"Redis|API Gateway","top_k":2}'},
                        }
                    ]
                )
            )
        if self.calls == 2:
            return FakeResponse(FakeMessage(content="Redis is used by API Gateway."))
        if self.calls == 3:
            return FakeResponse(
                FakeMessage(
                    tool_calls=[
                        {
                            "id": "read-1",
                            "type": "function",
                            "function": {"name": "list_knowledge_chunks", "arguments": '{"chunk_ids":["c1"]}'},
                        }
                    ]
                )
            )
        return FakeResponse(FakeMessage(content="Redis is used by API Gateway."))


class FakeChat:
    def __init__(self, completions=None):
        self.completions = completions or FakeCompletions()


class FakeClient:
    def __init__(self, completions=None):
        self.chat = FakeChat(completions)


class RecordingObservation:
    def __init__(self, name, input=None):
        self.name = name
        self.input = input


class RecordingObservabilitySink(ObservabilitySink):
    def __init__(self):
        self.started = []
        self.finished = []

    def start_span(self, *, name, input=None, metadata=None):
        self.started.append({"name": name, "type": "span", "input": input, "metadata": metadata})
        return ObservationHandle(self, raw=RecordingObservation(name, input), observation_id=name)

    def start_generation(self, *, name, model, input=None, metadata=None, model_parameters=None):
        self.started.append({"name": name, "type": "generation", "model": model, "input": input, "metadata": metadata})
        return ObservationHandle(self, raw=RecordingObservation(name, input), observation_id=name)

    def finish_observation(self, handle, *, output=None, metadata=None, error=None, usage=None):
        self.finished.append({"name": handle.raw.name, "output": output, "error": error, "usage": usage})


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

    def get_chunk(self, chunk_id, scope=None):
        if chunk_id == "c1":
            return self.chunk
        if chunk_id == "c2":
            chunk = dict(self.chunk)
            chunk["id"] = "c2"
            chunk["content"] = "Redis version is 7.2."
            chunk["content_markdown"] = "Redis version is 7.2."
            return chunk
        return None

    def list_chunks_for_documents(self, doc_ids, scope=None, limit=None, chunk_types=None):
        return [self.chunk] if "doc-1" in doc_ids else []

    def list_chunks(self, scope=None):
        return [self.chunk]


class FakeRAG:
    def __init__(self):
        self.document_repository = FakeRepository()
        self.default_scope = KnowledgeBaseScope("default-workspace", ("default-knowledge-base",), compatibility_default=True)
        self.knowledge_base_service = None

    def hybrid_retrieve_hits(self, question, scope=None):
        chunk_id = "c2" if "version" in question.lower() else "c1"
        content = "Redis version is 7.2." if chunk_id == "c2" else "Redis is used by API Gateway."
        return [
            {
                "content": content,
                "metadata": {
                    "source": "manual.md",
                    "doc_id": "doc-1",
                    "chunk_id": chunk_id,
                    "child_id": chunk_id,
                    "parent_id": "p1",
                    "title_path": "Architecture",
                },
                "hybrid_score": 0.9,
            }
        ]

    def keyword_retrieve_hits(self, question, top_k=None, scope=None):
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
                "keyword_score": 0.8,
            }
        ]

    def recall_parent_hits(self, hits, scope=None):
        return hits

    def extract_sources(self, hits):
        return [
            {
                "source": "manual.md",
                "score": 0.9,
                "chunk_id": hit.get("metadata", {}).get("chunk_id", ""),
                "doc_id": hit.get("metadata", {}).get("doc_id", ""),
            }
            for hit in hits
        ]


def build_runtime(completions=None, enabled_tools=("knowledge_search", "list_knowledge_chunks")) -> AgentRuntime:
    rag = FakeRAG()
    return AgentRuntime(
        llm_client=FakeClient(completions),
        chat_model="fake",
        rag_service=rag,
        prompt_catalog=AgentPromptCatalog.load("config/prompt_templates/agent_system_prompt.yaml"),
        tool_registry=build_default_tool_registry(
            enabled_tools=enabled_tools,
            max_output_chars=4000,
            skills_enabled=False,
        ),
        config=AgentRuntimeConfig(enabled=True, max_iterations=4),
    )


class AgentRuntimeLoopTests(unittest.TestCase):
    def tearDown(self):
        set_observability_sink(NoopObservabilitySink())

    def test_runtime_enforces_deep_read_before_final_answer(self):
        runtime = build_runtime()
        rag = runtime.rag_service

        events = list(runtime.stream_query_events("What uses Redis?", scope=rag.default_scope))
        event_types = [event.event_type for event in events]
        summaries = [event.payload.get("summary", "") for event in events if event.event_type == "agent_trace"]

        self.assertIn("tool_call", event_types)
        self.assertIn("tool_observation", event_types)
        self.assertIn("token", event_types)
        self.assertTrue(any("深度读取" in summary for summary in summaries))
        self.assertEqual("final", events[-1].event_type)
        self.assertEqual("Redis is used by API Gateway.", events[-1].payload["answer"])

    def test_policy_defaults_resolve_quick_and_reasoning(self):
        config = AgentRuntimeConfig(enabled=True, max_iterations=7, quick_runtime_enabled=True, quick_max_iterations=1)

        quick = resolve_chat_runtime_policy("quick", config)
        reasoning = resolve_chat_runtime_policy("reasoning", config)
        rag_wiki = resolve_chat_runtime_policy("rag_wiki", config)

        self.assertEqual("quick", quick.mode)
        self.assertEqual("quick_rag_agent", quick.prompt_template_id)
        self.assertFalse(quick.require_deep_read)
        self.assertFalse(quick.grep_first_enabled)
        self.assertEqual(1, quick.max_iterations)
        self.assertEqual((), quick.enabled_tools)
        self.assertEqual("reasoning", reasoning.mode)
        self.assertTrue(reasoning.require_deep_read)
        self.assertTrue(reasoning.grep_first_enabled)
        self.assertEqual(7, reasoning.max_iterations)
        self.assertEqual("rag_wiki", rag_wiki.mode)
        self.assertEqual("hybrid_rag_wiki_agent", rag_wiki.prompt_template_id)
        self.assertIn("wiki_search", rag_wiki.enabled_tools)
        self.assertIn("knowledge_search", rag_wiki.enabled_tools)

    def test_event_bus_sanitizes_publishes_and_cleans_subscribers(self):
        bus = AgentEventBus()
        received = []
        bus.on(received.append)

        event = bus.emit(AgentRuntimeEvent("agent_thought", {"summary": "safe", "raw_prompt": "hidden"}))
        bus.close()

        self.assertEqual("safe", event.payload["summary"])
        self.assertNotIn("raw_prompt", event.payload)
        self.assertEqual([event], received)
        self.assertTrue(bus.closed)

    def test_runtime_emits_agent_tool_and_generation_observations(self):
        sink = RecordingObservabilitySink()
        set_observability_sink(sink)
        runtime = build_runtime()

        list(runtime.stream_query_events("What uses Redis?", scope=runtime.rag_service.default_scope))

        names = [item["name"] for item in sink.started]
        self.assertIn("agent.execute", names)
        self.assertIn("agent.round.1", names)
        self.assertIn("agent.tool.knowledge_search", names)
        self.assertIn("chat.completion", names)

    def test_runtime_builds_user_message_from_context_template(self):
        runtime = build_runtime()
        rag = runtime.rag_service

        list(
            runtime.stream_query_events(
                "What uses Redis?",
                conversation_context={"summary": "Earlier Redis question"},
                memory_context="Prefer short answers.",
                attachments=[{"filename": "note.txt"}],
                scope=rag.default_scope,
            )
        )
        messages = runtime.llm_client.chat.completions.last_kwargs["messages"]
        user_message = messages[1]["content"]

        self.assertIn("<runtime_context>", user_message)
        self.assertIn("<user_question>", user_message)
        self.assertIn("What uses Redis?", user_message)
        self.assertIn("Earlier Redis question", user_message)
        self.assertIn("Prefer short answers.", user_message)
        self.assertIn("note.txt", user_message)

    def test_agent_runtime_span_repository_records_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "metadata.sqlite3"
            repository = AgentRuntimeSpanRepository(db_path)
            root = repository.start_span(run_id="run-1", name="agent.execute", kind="root", input={"q": "Redis"})
            tool = repository.start_span(run_id="run-1", name="tool.knowledge_search", kind="tool", parent_span_id=root.span_id)
            repository.finish_span(tool, status="completed", output={"result": 1})
            repository.finish_span(root, status="completed", output={"answer_len": 12})

            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute("select name, status from agent_runtime_spans where run_id = ? order by id", ("run-1",)).fetchall()
            finally:
                conn.close()

        self.assertEqual([("agent.execute", "completed"), ("tool.knowledge_search", "completed")], rows)

    def test_runtime_trace_order_sanitization_and_log_trace_id(self):
        runtime = build_runtime()
        rag = runtime.rag_service

        with self.assertLogs("app.services.agent.agent_runtime", level="INFO") as captured:
            events = list(runtime.stream_query_events("What uses Redis?", scope=rag.default_scope))

        trace_stages = [event.payload.get("stage") for event in events if event.event_type == "agent_trace"]
        event_types = [event.event_type for event in events]
        sanitized = AgentRuntimeEvent(
            "agent_trace",
            {"metadata": {"safe": "ok", "raw_prompt": "hidden", "token": "hidden"}},
        ).to_dict()

        self.assertEqual("AgentRuntimeStart", trace_stages[0])
        self.assertIn("RequireDeepRead", trace_stages)
        self.assertEqual("ReturnAnswer", trace_stages[-1])
        self.assertLess(event_types.index("sources"), event_types.index("final"))
        self.assertEqual({"safe": "ok"}, sanitized["payload"]["metadata"])
        self.assertTrue(
            any(record.getMessage() == "agent_runtime.trace" and hasattr(record, "trace_id") for record in captured.records)
        )

    def test_reasoning_model_decides_retrieval_without_lexical_forced_grep(self):
        completions = SkipsGrepThenCorrectsCompletions()
        runtime = build_runtime(
            completions=completions,
            enabled_tools=("grep_chunks", "knowledge_search", "list_knowledge_chunks"),
        )
        runtime.config.max_iterations = 5

        events = list(runtime.stream_query_events("What uses Redis?", scope=runtime.rag_service.default_scope))
        stages = [event.payload.get("stage") for event in events if event.event_type == "agent_trace"]
        tool_names = [event.payload.get("tool") for event in events if event.event_type == "agent_tool_call"]
        final = [event for event in events if event.event_type == "final"][-1]

        self.assertNotIn("RequireGrepFirst", stages)
        self.assertEqual([], tool_names)
        self.assertEqual(1, completions.calls)
        self.assertEqual("Redis is used by API Gateway.", final.payload["answer"])

    def test_exact_grep_anchor_detection_keeps_semantic_questions_autonomous(self):
        self.assertFalse(_question_needs_exact_grep_anchor("风控系统架构是什么"))
        self.assertTrue(
            _question_needs_exact_grep_anchor("48*SFP+ 光口 4.8T 40GE VLAN:4K MAC:128K")
        )

    def test_grep_query_variants_are_shown_with_pipe_separator(self):
        summary = _input_summary(
            "grep_chunks",
            {
                "queries": [
                    "48*SFP+",
                    "40G QSFP+",
                    "4.8T",
                    "VLAN:4K",
                ]
            },
        )

        self.assertEqual("grep_chunks: 48*SFP+ | 40G QSFP+ | 4.8T | VLAN:4K", summary)

    def test_reasoning_grep_candidates_still_require_deep_read(self):
        runtime = build_runtime(
            completions=GrepThenSkipsDeepReadCompletions(),
            enabled_tools=("grep_chunks", "list_knowledge_chunks"),
        )
        runtime.config.max_iterations = 5

        events = list(runtime.stream_query_events("What uses Redis?", scope=runtime.rag_service.default_scope))
        stages = [event.payload.get("stage") for event in events if event.event_type == "agent_trace"]
        tool_names = [event.payload.get("tool") for event in events if event.event_type == "agent_tool_call"]

        self.assertIn("RequireDeepRead", stages)
        self.assertEqual(["grep_chunks", "list_knowledge_chunks"], tool_names)

    def test_domain_events_are_ordered_and_sanitized(self):
        runtime = build_runtime()
        events = list(runtime.stream_query_events("What uses Redis?", scope=runtime.rag_service.default_scope))
        event_types = [event.event_type for event in events]
        sanitized = agent_event(
            "agent_thought",
            run_id="run-1",
            sequence=1,
            payload={
                "summary": "safe",
                "raw_prompt": "hidden",
                "nested": {"scratchpad": "hidden", "safe": "ok"},
                "raw_tool_payload": "hidden",
            },
        ).to_dict()

        self.assertEqual("agent_query", event_types[0])
        self.assertIn("agent_thought", event_types)
        self.assertIn("agent_tool_call", event_types)
        self.assertIn("agent_tool_result", event_types)
        self.assertIn("agent_reflection", event_types)
        self.assertIn("agent_references", event_types)
        self.assertIn("agent_final_answer", event_types)
        self.assertIn("agent_complete", event_types)
        self.assertLess(event_types.index("agent_references"), event_types.index("token"))
        self.assertLess(event_types.index("agent_references"), event_types.index("agent_final_answer"))
        self.assertEqual({"safe": "ok"}, sanitized["payload"]["nested"])
        self.assertNotIn("raw_prompt", sanitized["payload"])

    def test_structured_thinking_triggers_remedial_retrieval(self):
        runtime = build_runtime(
            completions=RemedialCompletions(),
            enabled_tools=("thinking", "knowledge_search", "list_knowledge_chunks"),
        )
        runtime.config.legacy_remedial_retrieval_enabled = True

        events = list(runtime.stream_query_events("Which Redis version?", scope=runtime.rag_service.default_scope))
        event_types = [event.event_type for event in events]
        tool_calls = [event.payload for event in events if event.event_type == "agent_tool_call"]
        complete = [event.payload for event in events if event.event_type == "agent_complete"][-1]

        self.assertIn("agent_remedial_search", event_types)
        self.assertTrue(any(call.get("call_id") == "remedial-1-search" for call in tool_calls))
        self.assertTrue(any(call.get("call_id") == "remedial-1-read" for call in tool_calls))
        self.assertTrue(complete["remedial_used"])
        self.assertEqual("Redis version is 7.2.", [event for event in events if event.event_type == "final"][-1].payload["answer"])

    def test_quick_policy_preloads_evidence_and_finishes_without_tool_loop(self):
        completions = QuickDirectCompletions()
        runtime = build_runtime(completions=completions, enabled_tools=("knowledge_search", "list_knowledge_chunks"))
        runtime.config.quick_runtime_enabled = True

        events = list(runtime.stream_query_events("What uses Redis?", mode="quick", scope=runtime.rag_service.default_scope))
        event_types = [event.event_type for event in events]
        final = [event for event in events if event.event_type == "final"][-1]
        complete = [event.payload for event in events if event.event_type == "agent_complete"][-1]

        self.assertEqual(1, completions.calls)
        self.assertNotIn("tool_call", event_types)
        self.assertIn("agent_query", event_types)
        self.assertIn("agent_references", event_types)
        self.assertEqual("Redis is used by API Gateway.", final.payload["answer"])
        self.assertEqual("quick", complete["chat_mode"])
        self.assertEqual([], completions.last_kwargs.get("tools", []))

    def test_policy_rejects_disallowed_tool_call(self):
        runtime = build_runtime(completions=DisallowedToolCompletions(), enabled_tools=("knowledge_search",))
        runtime.config.quick_runtime_enabled = True

        events = list(runtime.stream_query_events("What uses Redis?", mode="quick", scope=runtime.rag_service.default_scope))
        failed_results = [event.payload for event in events if event.event_type == "agent_tool_result" and event.payload.get("status") == "failed"]

        self.assertTrue(failed_results)
        self.assertIn("not allowed", failed_results[0]["output_summary"])

    def test_quick_mode_isolation_has_no_runtime_remedial_loop(self):
        runtime = build_runtime(
            completions=RemedialCompletions(),
            enabled_tools=("thinking",),
        )

        events = list(runtime.stream_query_events("Which Redis version?", scope=runtime.rag_service.default_scope))
        self.assertNotIn("agent_remedial_search", [event.event_type for event in events])

    def test_reflection_gap_without_correction_query_stops_as_insufficient(self):
        runtime = build_runtime(
            completions=NoCorrectionThinkingCompletions(),
            enabled_tools=("thinking", "knowledge_search", "list_knowledge_chunks"),
        )

        events = list(runtime.stream_query_events("Which Redis version?", scope=runtime.rag_service.default_scope))
        event_types = [event.event_type for event in events]
        final = [event for event in events if event.event_type == "final"][-1]

        self.assertNotIn("agent_remedial_search", event_types)
        self.assertNotEqual("Redis version is 7.2.", final.payload["answer"])
        self.assertNotIn("Answer in zh-CN", final.payload["answer"])
        self.assertIn("知识库证据", final.payload["answer"])
        self.assertEqual(0.3, final.payload["confidence"])

    def test_duplicate_only_remedial_results_do_not_clear_gap(self):
        runtime = build_runtime(
            completions=DuplicateRemedialCompletions(),
            enabled_tools=("thinking", "knowledge_search", "list_knowledge_chunks"),
        )
        runtime.config.legacy_remedial_retrieval_enabled = True

        events = list(runtime.stream_query_events("Which Redis version?", scope=runtime.rag_service.default_scope))
        tool_calls = [event.payload for event in events if event.event_type == "agent_tool_call"]
        final = [event for event in events if event.event_type == "final"][-1]

        self.assertTrue(any(call.get("call_id") == "remedial-1-search" for call in tool_calls))
        self.assertFalse(any(call.get("call_id") == "remedial-1-read" for call in tool_calls))
        self.assertNotEqual("Unsupported answer", final.payload["answer"])
        self.assertEqual(0.3, final.payload["confidence"])

    def test_exhausted_remedial_attempts_skip_follow_up_search(self):
        runtime = build_runtime(
            completions=RemedialCompletions(),
            enabled_tools=("thinking", "knowledge_search", "list_knowledge_chunks"),
        )
        runtime.config.max_remedial_retrieval_attempts = 0

        events = list(runtime.stream_query_events("Which Redis version?", scope=runtime.rag_service.default_scope))
        self.assertNotIn("agent_remedial_search", [event.event_type for event in events])


if __name__ == "__main__":
    unittest.main()
