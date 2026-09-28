from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Generator
from uuid import uuid4

from app.models.agent_runtime import (
    ActionBatch,
    ActionToolCall,
    ActionToolExecution,
    AgentEventBus,
    AgentEventSequencer,
    AgentRuntimeConfig,
    AgentRuntimeEvent,
    ChatRuntimePolicy,
    RuntimeStateDelta,
    RuntimeToolError,
    RuntimeToolResult,
    ToolExecutionClass,
    agent_event,
    apply_runtime_state_delta,
    resolve_chat_runtime_policy,
    scrub_private_fields,
    tool_call_to_agent_event,
    tool_result_to_agent_event,
)
from app.models.knowledge_base import KnowledgeBaseScope
from app.services.agent.agent_prompt_templates import (
    AgentPromptCatalog,
    ContextPromptCatalog,
    scope_to_prompt_kbs,
)
from app.services.agent.agent_runtime_spans import AgentRuntimeSpanRepository
from app.services.agent.agent_runtime_tools import RuntimeToolContext, ToolRegistry
from app.services.agent.runtime_skills import RequestSkillsManager, RuntimeSkillError
from app.services.infrastructure.logging_config import get_trace_id, sanitize_payload, truncate_text
from app.services.infrastructure.observability import activate_observation, get_observability_sink

logger = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(
        self,
        *,
        llm_client: Any,
        chat_model: str,
        rag_service: Any,
        prompt_catalog: AgentPromptCatalog,
        tool_registry: ToolRegistry,
        config: AgentRuntimeConfig,
        context_catalog: ContextPromptCatalog | None = None,
        skills_manager: Any | None = None,
        graph_retriever: Any | None = None,
        span_repository: AgentRuntimeSpanRepository | None = None,
    ):
        self.llm_client = llm_client
        self.chat_model = chat_model
        self.rag_service = rag_service
        self.prompt_catalog = prompt_catalog
        self.context_catalog = context_catalog or ContextPromptCatalog.load("config/prompt_templates/context_template.yaml")
        self.tool_registry = tool_registry
        self.config = config
        self.skills_manager = skills_manager
        self.graph_retriever = graph_retriever
        self.span_repository = span_repository or AgentRuntimeSpanRepository.disabled()
        self._parallel_tool_calls_support: dict[str, bool] = {}

    def stream_query_events(
        self,
        question: str,
        *,
        conversation_context: str | dict[str, Any] | None = None,
        memory_context: str | None = None,
        scope: KnowledgeBaseScope | None = None,
        attachments: list[dict[str, Any]] | None = None,
        mode: str = "reasoning",
        resolved_skills: tuple = (),
    ) -> Generator[AgentRuntimeEvent, None, None]:
        yield from self.execute(
            question,
            conversation_context=conversation_context,
            memory_context=memory_context,
            scope=scope,
            attachments=attachments,
            mode=mode,
            resolved_skills=resolved_skills,
        )

    def execute(
        self,
        question: str,
        *,
        conversation_context: str | dict[str, Any] | None = None,
        memory_context: str | None = None,
        scope: KnowledgeBaseScope | None = None,
        attachments: list[dict[str, Any]] | None = None,
        mode: str = "reasoning",
        resolved_skills: tuple = (),
    ) -> Generator[AgentRuntimeEvent, None, None]:
        scope = scope or self.rag_service.default_scope
        policy = resolve_chat_runtime_policy(mode, self.config)
        yield from self.execute_loop(
            question,
            resolved_skills=resolved_skills,
            policy=policy,
            conversation_context=conversation_context,
            memory_context=memory_context,
            scope=scope,
            attachments=attachments,
        )

    def execute_loop(
        self,
        question: str,
        *,
        policy: ChatRuntimePolicy,
        conversation_context: str | dict[str, Any] | None = None,
        memory_context: str | None = None,
        scope: KnowledgeBaseScope,
        attachments: list[dict[str, Any]] | None = None,
        resolved_skills: tuple = (),
    ) -> Generator[AgentRuntimeEvent, None, None]:
        if resolved_skills and not self.config.skills_enabled:
            raise RuntimeSkillError("Runtime skills are disabled")
        request_skills = RequestSkillsManager(self.skills_manager, resolved_skills) if resolved_skills else self.skills_manager
        run_id = uuid4().hex
        event_sequence = AgentEventSequencer()
        event_bus = AgentEventBus()
        run_started = time.time()
        run_started_monotonic = time.monotonic()
        trace: list[dict[str, Any]] = []
        tool_events: list[dict[str, Any]] = []
        state: dict[str, Any] = {
            "question": question,
            "search_candidate_ids": [],
            "deep_read_ids": [],
            "sources": [],
            "tool_counts": {},
            "remedial_attempts": 0,
            "remedial_used": False,
            "previous_candidate_ids": [],
            "previous_deep_read_ids": [],
            "agent_event_run_id": run_id,
            "chat_mode": policy.mode,
            "grep_first_performed": False,
            "grep_first_guard_used": False,
            "llm_calls": 0,
            "proposed_tool_calls": 0,
            "executed_tool_calls": 0,
            "action_rounds": 0,
            "run_started_monotonic": run_started_monotonic,
            "stop_reason": "",
            "last_action_signature": "",
            "last_action_evidence_fingerprint": "",
            "repeated_action_batches": 0,
            "terminal_synthesis_reserved": True,
            "provider_parallel_fallback": False,
        }
        preloaded_hits: list[dict[str, Any]] = []
        preloaded_context = ""
        answer_guidance = ""
        if policy.preload_retrieval:
            preloaded_hits = self._preload_retrieval(question, scope)
            state["sources"] = self.rag_service.extract_sources(preloaded_hits) if hasattr(self.rag_service, "extract_sources") else []
            state["deep_read_ids"] = self._source_chunk_ids_from_hits(preloaded_hits)
            state["preloaded_hit_count"] = len(preloaded_hits)
            state["preloaded_source_count"] = len(state["sources"])
            preloaded_context = self._build_retrieved_context(preloaded_hits)
            answer_guidance = self._build_answer_guidance(question, preloaded_context)
        context = RuntimeToolContext(
            question=question,
            scope=scope,
            rag_service=self.rag_service,
            graph_retriever=self.graph_retriever,
            skills_manager=request_skills,
            state=state,
        )
        messages = self._build_messages(
            question,
            scope,
            conversation_context,
            memory_context,
            attachments,
            policy=policy,
            contexts=preloaded_context,
            answer_guidance=answer_guidance,
            request_skills=request_skills,
        )
        if resolved_skills:
            yield AgentRuntimeEvent("skills_loaded", {"items": [item.public_metadata() for item in resolved_skills]})
        tools = self.tool_registry.function_definitions(self._allowed_registered_tools(policy))
        root_span = self.span_repository.start_span(
            run_id=run_id,
            name="agent.execute",
            kind="root",
            input={"question_len": len(question), "tool_count": len(tools)},
            metadata={"trace_id": get_trace_id(), "knowledge_base_scope": scope.to_dict()},
        )
        obs_root = get_observability_sink().start_span(
            name="agent.execute",
            input={"question": question, "tool_count": len(tools)},
            metadata={"trace_id": get_trace_id(), "knowledge_base_scope": scope.to_dict()},
        )

        yield event_bus.emit(agent_event(
            "agent_query",
            run_id=run_id,
            sequence=event_sequence.next(),
            status="running",
            payload={
                "summary": "Received user question.",
                "question": truncate_text(question, 500),
                "chat_mode": policy.mode,
                "knowledge_base_scope": scope.to_dict(),
                "metadata": {"trace_id": get_trace_id(), "policy": policy.mode},
            },
        ))
        if policy.emit_initial_thought:
            yield event_bus.emit(agent_event(
                "agent_thought",
                run_id=run_id,
                sequence=event_sequence.next(),
                status="running",
                payload={
                    "phase": "llm_decision",
                    "summary": "Analyzing the question and selecting the next action.",
                    "completion_status": "running",
                    "metadata": {
                        "source": "runtime_phase",
                        "tool_count": len(tools),
                        "trace_id": get_trace_id(),
                        "policy": policy.mode,
                    },
                },
            ))

        start_event = self._record_trace(
            trace,
            "AgentRuntimeStart",
            "running",
            "开始智能推理，由模型选择回答或下一步工具。",
            metadata={"tool_count": len(tools), "knowledge_base_scope": scope.to_dict(), "trace_id": get_trace_id(), "policy": policy.mode},
        )
        yield start_event

        empty_retries = 0
        repeated_responses = 0
        last_content = ""
        final_answer = ""
        final_status = "partial"
        try:
            if policy.quick and policy.preload_retrieval and not preloaded_hits:
                final_answer = self._fallback_answer_from_state(state)
                final_status = "partial"
            for round_number in range(1, policy.max_iterations + 1):
                if final_answer:
                    break
                stop_reason = self._pre_round_stop_reason(state, policy)
                if stop_reason:
                    state["stop_reason"] = stop_reason
                    break
                state["action_rounds"] = round_number
                round_start = time.time()
                round_span = self.span_repository.start_span(
                    run_id=run_id,
                    name=f"agent.round.{round_number}",
                    kind="round",
                    parent_span_id=root_span.span_id if root_span is not None else "",
                    input={"round": round_number, "message_count": len(messages)},
                    metadata={"trace_id": get_trace_id()},
                )
                obs_round = get_observability_sink().start_span(
                    name=f"agent.round.{round_number}",
                    input={"round": round_number, "message_count": len(messages), "max_iterations": policy.max_iterations},
                    metadata={"trace_id": get_trace_id(), "round": round_number, "policy": policy.mode},
                )
                yield self._record_trace(
                    trace,
                    "AgentRound",
                    "running",
                    f"第 {round_number} 轮：分析问题并选择下一步工具。",
                    metadata={"round": round_number, "trace_id": get_trace_id()},
                )
                response_message = self.run_react_iteration(
                    policy=policy,
                    messages=messages,
                    tools=tools,
                    state=state,
                    round_number=round_number,
                )
                content = str(response_message.get("content") or "").strip()
                tool_calls = list(response_message.get("tool_calls") or [])

                if content and content == last_content:
                    repeated_responses += 1
                else:
                    repeated_responses = 0
                last_content = content

                if not tool_calls:
                    if content and self._should_block_for_grep_first(state, policy):
                        self._append_grep_first_guard_message(messages, question)
                        yield self._record_trace(
                            trace,
                            "RequireGrepFirst",
                            "running",
                            "Knowledge-base retrieval requires exact term anchoring before semantic search or final answer.",
                            metadata={
                                "round": round_number,
                                "trace_id": get_trace_id(),
                                "policy": policy.mode,
                                "grep_first_required": True,
                            },
                        )
                        state["grep_first_guard_used"] = True
                        self.span_repository.finish_span(round_span, status="partial", output={"guard": "grep_first_required"})
                        obs_round.finish(output={"guard": "grep_first_required", "status": "partial"})
                        continue
                    if content and self._final_allowed(state, policy):
                        if self._has_unresolved_reflection_gap(state):
                            final_answer = self._fallback_answer_from_state(state)
                            final_status = "partial"
                            self.span_repository.finish_span(round_span, status="partial", output={"final": False, "reason": "unresolved_reflection_gap"})
                            obs_round.finish(output={"final": False, "reason": "unresolved_reflection_gap", "status": "partial"})
                        else:
                            final_answer = content
                            final_status = "completed"
                            state["terminal_stream_tokens"] = list(
                                response_message.get("_stream_tokens") or []
                            )
                            self.span_repository.finish_span(round_span, status="completed", output={"final": True, "content_len": len(content)})
                            obs_round.finish(output={"final": True, "content_len": len(content), "status": "completed"})
                        break
                    if content and not self._final_allowed(state, policy):
                        messages.append(
                            {
                                "role": "user",
                                "content": "Runtime guard: search returned candidate evidence. Deep-read the relevant chunks with list_knowledge_chunks or get_document_info before final answer.",
                            }
                        )
                        yield self._record_trace(
                            trace,
                            "RequireDeepRead",
                            "running",
                            "已找到候选证据，继续深度读取后再回答。",
                            metadata={"round": round_number, "trace_id": get_trace_id()},
                        )
                        self.span_repository.finish_span(round_span, status="partial", output={"guard": "deep_read_required"})
                        obs_round.finish(output={"guard": "deep_read_required", "status": "partial"})
                        continue
                    empty_retries += 1
                    if empty_retries > policy.max_empty_retries:
                        final_answer = "无法从当前知识库证据中确定答案。"
                        self.span_repository.finish_span(round_span, status="failed", output={"empty_retries": empty_retries})
                        obs_round.finish(output={"empty_retries": empty_retries, "status": "failed"})
                        break
                    messages.append({"role": "user", "content": "Please continue by using an available tool or provide a final answer."})
                    self.span_repository.finish_span(round_span, status="partial", output={"empty_retry": empty_retries})
                    obs_round.finish(output={"empty_retry": empty_retries, "status": "partial"})
                    continue

                if self._should_block_web_search_until_knowledge_base_checked(tool_calls, state, policy):
                    self._append_knowledge_base_before_web_guard_message(messages, question)
                    yield self._record_trace(
                        trace,
                        "RequireKnowledgeBaseBeforeWebSearch",
                        "running",
                        "Web search is available only after knowledge-base retrieval and deep reading are insufficient.",
                        metadata={
                            "round": round_number,
                            "trace_id": get_trace_id(),
                            "policy": policy.mode,
                            "knowledge_base_first_required": True,
                        },
                    )
                    self.span_repository.finish_span(round_span, status="partial", output={"guard": "knowledge_base_before_web_search"})
                    obs_round.finish(output={"guard": "knowledge_base_before_web_search", "status": "partial"})
                    continue

                if self._should_block_tool_calls_for_grep_first(tool_calls, state, policy):
                    self._append_grep_first_guard_message(messages, question)
                    yield self._record_trace(
                        trace,
                        "RequireGrepFirst",
                        "running",
                        "Knowledge-base retrieval requires exact term anchoring before semantic expansion.",
                        metadata={
                            "round": round_number,
                            "trace_id": get_trace_id(),
                            "policy": policy.mode,
                            "grep_first_required": True,
                        },
                    )
                    state["grep_first_guard_used"] = True
                    self.span_repository.finish_span(round_span, status="partial", output={"guard": "grep_first_required"})
                    obs_round.finish(output={"guard": "grep_first_required", "status": "partial"})
                    continue

                empty_retries = 0
                state["proposed_tool_calls"] = int(state.get("proposed_tool_calls") or 0) + len(tool_calls)
                batch = self._build_action_batch(
                    tool_calls,
                    round_number=round_number,
                    policy=policy,
                    remaining_tool_calls=max(
                        0,
                        int(policy.max_tool_calls) - int(state.get("executed_tool_calls") or 0),
                    ),
                )
                action_signature = self._action_batch_signature(batch)
                evidence_fingerprint = self._evidence_fingerprint(state)
                if self._is_repeated_action_batch(
                    action_signature,
                    evidence_fingerprint,
                    state,
                    policy,
                ):
                    state["stop_reason"] = "repeated_action_batch"
                    self.span_repository.finish_span(
                        round_span,
                        status="partial",
                        output={"stop_reason": "repeated_action_batch", "batch_id": batch.batch_id},
                    )
                    obs_round.finish(
                        output={"stop_reason": "repeated_action_batch", "batch_id": batch.batch_id, "status": "partial"}
                    )
                    break
                assistant_message = {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": [_tool_call_message(call) for call in tool_calls],
                }
                messages.append(assistant_message)

                for call in batch.calls:
                    call_payload = {
                        "call_id": call.call_id,
                        "tool": call.tool_name,
                        "action": "execute",
                        "input_summary": _input_summary(call.tool_name, call.arguments),
                        "metadata": {
                            "round": round_number,
                            "batch_id": batch.batch_id,
                            "call_index": call.index,
                            "execution_class": call.execution_class.value,
                            "trace_id": get_trace_id(),
                            "policy": policy.mode,
                        },
                    }
                    tool_events.append(call_payload)
                    yield event_bus.emit(tool_call_to_agent_event(call_payload, run_id=run_id, sequence=event_sequence.next()))
                    yield AgentRuntimeEvent("tool_call", call_payload)

                batch_started = time.perf_counter()
                executions = self._execute_action_batch(
                    batch,
                    context=context,
                    run_id=run_id,
                    round_span=round_span,
                    policy=policy,
                )
                batch_wall_ms = int((time.perf_counter() - batch_started) * 1000)

                for execution in executions:
                    call = execution.call
                    result = execution.result
                    if (
                        call.validation_error is None
                        and (result.structured_error is None or result.structured_error.code != "batch_cancelled")
                    ):
                        state["executed_tool_calls"] = int(state.get("executed_tool_calls") or 0) + 1
                    self._record_tool_state(call.tool_name, result, state)
                    observation = result.to_observation_text()
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "name": call.tool_name,
                            "content": observation,
                        }
                    )
                    observation_payload = {
                        "call_id": call.call_id,
                        "tool": call.tool_name,
                        "action": "execute",
                        "status": "completed" if result.success else "failed",
                        "output_summary": result.observation or truncate_text(observation, 240),
                        "source_chunk_ids": result.source_chunk_ids,
                        "metadata": {
                            **result.metadata,
                            "round": round_number,
                            "batch_id": batch.batch_id,
                            "call_id": call.call_id,
                            "call_index": call.index,
                            "execution_class": call.execution_class.value,
                            "queue_ms": execution.queue_ms,
                            "tool_duration_ms": execution.duration_ms,
                            "batch_wall_ms": batch_wall_ms,
                            "source_titles": result.source_titles,
                            "trace_id": get_trace_id(),
                        },
                    }
                    tool_events.append(observation_payload)
                    if call.tool_name == "thinking":
                        yield event_bus.emit(
                            self._thought_event_from_tool_result(
                                result,
                                state,
                                run_id,
                                event_sequence.next(),
                                call_id=call.call_id,
                            )
                        )
                    yield event_bus.emit(tool_result_to_agent_event(observation_payload, run_id=run_id, sequence=event_sequence.next()))
                    yield AgentRuntimeEvent("tool_observation", observation_payload)
                    if call.tool_name == "thinking" and self._should_run_remedial_retrieval(state, policy):
                        yield from self._run_remedial_retrieval(
                            context=context,
                            state=state,
                            trace=trace,
                            tool_events=tool_events,
                            run_id=run_id,
                            event_sequence=event_sequence,
                            event_bus=event_bus,
                            round_number=round_number,
                        )

                state["last_batch_wall_ms"] = batch_wall_ms
                if int(state.get("executed_tool_calls") or 0) >= int(policy.max_tool_calls):
                    state["stop_reason"] = "tool_call_budget"

                yield self._record_trace(
                    trace,
                    "AgentRound",
                    "completed",
                    f"第 {round_number} 轮完成，调用 {len(tool_calls)} 次工具。",
                    metadata={
                        "round": round_number,
                        "tool_calls": len(tool_calls),
                        "batch_id": batch.batch_id,
                        "batch_wall_ms": batch_wall_ms,
                        "parallel_workers": min(policy.max_parallel_workers, len(batch.calls)),
                        "duration_ms": int((time.time() - round_start) * 1000),
                        "trace_id": get_trace_id(),
                    },
                )
                self.span_repository.finish_span(
                    round_span,
                    status="completed",
                    output={
                        "tool_calls": len(tool_calls),
                        "batch_id": batch.batch_id,
                        "batch_wall_ms": batch_wall_ms,
                        "duration_ms": int((time.time() - round_start) * 1000),
                    },
                )
                obs_round.finish(
                    output={
                        "tool_calls": len(tool_calls),
                        "batch_id": batch.batch_id,
                        "batch_wall_ms": batch_wall_ms,
                        "duration_ms": int((time.time() - round_start) * 1000),
                        "status": "completed",
                    }
                )
                if state.get("stop_reason"):
                    break
                if repeated_responses > policy.max_repeated_responses:
                    final_answer = "无法从当前知识库证据中确定答案。"
                    break
            else:
                state["stop_reason"] = state.get("stop_reason") or "action_round_budget"

            if not final_answer:
                final_answer, final_status = self._terminal_answer_from_state(
                    question=question,
                    messages=messages,
                    state=state,
                    policy=policy,
                )

            sources = self._sources_from_state(state)
            reflection_status = "completed" if final_status == "completed" else "partial"
            yield event_bus.emit(self._reflection_event_from_state(
                state,
                run_id=run_id,
                sequence=event_sequence.next(),
                status=reflection_status,
                final_status=final_status,
            ))
            yield event_bus.emit(
                agent_event(
                    "agent_references",
                    run_id=run_id,
                    sequence=event_sequence.next(),
                    status="completed" if sources else "partial",
                    payload={
                        "items": sources,
                        "source_chunk_ids": list(state.get("deep_read_ids") or []),
                        "summary": f"Prepared {len(sources)} referenced source(s)." if sources else "No traceable references were available.",
                        "metadata": {"trace_id": get_trace_id(), "policy": policy.mode},
                    },
                )
            )
            yield AgentRuntimeEvent("sources", {"items": sources})
            yield AgentRuntimeEvent(
                "evidence_summary",
                {
                    "sufficient": final_status == "completed",
                    "sufficiency_reason": "已完成深度读取并生成回答。" if final_status == "completed" else "达到运行边界或证据不足。",
                    "tool_counts": dict(state.get("tool_counts") or {}),
                    "used_chunks": len(state.get("deep_read_ids") or []),
                    "source_chunk_ids": list(state.get("deep_read_ids") or []),
                },
            )
            yield event_bus.emit(
                agent_event(
                    "agent_final_answer",
                    run_id=run_id,
                    sequence=event_sequence.next(),
                    status="completed" if final_status == "completed" else "partial",
                    payload={
                        "answer": final_answer,
                        "answer_length": len(final_answer),
                        "citation_count": len(sources),
                        "metadata": {"trace_id": get_trace_id(), "policy": policy.mode},
                    },
                )
            )
            answer_tokens = list(state.get("terminal_stream_tokens") or [])
            if not answer_tokens:
                answer_tokens = (
                    _answer_token_chunks(final_answer)
                    if policy.terminal_streaming_mode == "on"
                    else [final_answer]
                )
            for index, token in enumerate(answer_tokens):
                if index == 0:
                    state["terminal_first_token_ms"] = int(
                        (time.monotonic() - run_started_monotonic) * 1000
                    )
                yield AgentRuntimeEvent(
                    "token",
                    {
                        "token": token,
                        "streamed": len(answer_tokens) > 1,
                        "index": index,
                    },
                )
            final_payload = {
                "answer": final_answer,
                "citations": sources,
                "used_chunks": list(state.get("deep_read_ids") or []),
                "agent_trace": trace,
                "tool_calls": tool_events,
                "confidence": 0.7 if final_status == "completed" else 0.3,
            }
            yield self._record_trace(
                trace,
                "ReturnAnswer",
                "completed" if final_status == "completed" else "partial",
                "智能推理已完成。",
                metadata={
                    "rounds": min(policy.max_iterations, len([item for item in tool_events if item.get("call_id")]) or (1 if final_answer else 0)),
                    "tool_calls": len([item for item in tool_events if item.get("tool")]),
                    "duration_ms": int((time.time() - run_started) * 1000),
                    "remedial_used": bool(state.get("remedial_used")),
                    "stop_reason": state.get("stop_reason") or "model_final",
                    "llm_calls": int(state.get("llm_calls") or 0),
                    "proposed_tool_calls": int(state.get("proposed_tool_calls") or 0),
                    "executed_tool_calls": int(state.get("executed_tool_calls") or 0),
                    "terminal_synthesis_used": bool(state.get("terminal_synthesis_used")),
                    "terminal_first_token_ms": int(state.get("terminal_first_token_ms") or 0),
                    "provider_parallel_fallback": bool(state.get("provider_parallel_fallback")),
                    "trace_id": get_trace_id(),
                },
            )
            self.span_repository.finish_span(
                root_span,
                status="completed" if final_status == "completed" else "partial",
                output={
                    "answer_len": len(final_answer),
                    "tool_counts": dict(state.get("tool_counts") or {}),
                    "stop_reason": state.get("stop_reason") or "model_final",
                    "llm_calls": int(state.get("llm_calls") or 0),
                    "executed_tool_calls": int(state.get("executed_tool_calls") or 0),
                    "terminal_first_token_ms": int(state.get("terminal_first_token_ms") or 0),
                    "provider_parallel_fallback": bool(state.get("provider_parallel_fallback")),
                },
            )
            obs_root.finish(
                output={
                    "answer_len": len(final_answer),
                    "tool_counts": dict(state.get("tool_counts") or {}),
                    "status": final_status,
                    "stop_reason": state.get("stop_reason") or "model_final",
                    "llm_calls": int(state.get("llm_calls") or 0),
                    "executed_tool_calls": int(state.get("executed_tool_calls") or 0),
                    "terminal_first_token_ms": int(state.get("terminal_first_token_ms") or 0),
                }
            )
            yield event_bus.emit(
                agent_event(
                    "agent_complete",
                    run_id=run_id,
                    sequence=event_sequence.next(),
                    status="completed" if final_status == "completed" else "partial",
                    payload={
                        "summary": "Agent run completed." if final_status == "completed" else "Agent run completed with insufficient evidence.",
                        "duration_ms": int((time.time() - run_started) * 1000),
                        "tool_counts": dict(state.get("tool_counts") or {}),
                        "source_count": len(sources),
                        "remedial_used": bool(state.get("remedial_used")),
                        "chat_mode": policy.mode,
                        "stop_reason": state.get("stop_reason") or "model_final",
                        "llm_calls": int(state.get("llm_calls") or 0),
                        "proposed_tool_calls": int(state.get("proposed_tool_calls") or 0),
                        "executed_tool_calls": int(state.get("executed_tool_calls") or 0),
                        "terminal_synthesis_used": bool(state.get("terminal_synthesis_used")),
                        "metadata": {
                            "trace_id": get_trace_id(),
                            "policy": policy.mode,
                            "provider_parallel_fallback": bool(state.get("provider_parallel_fallback")),
                            "max_action_rounds": policy.max_iterations,
                            "max_llm_calls": policy.max_llm_calls,
                            "max_tool_calls": policy.max_tool_calls,
                            "max_wall_clock_seconds": policy.max_wall_clock_seconds,
                            "max_parallel_workers": policy.max_parallel_workers,
                        },
                    },
                )
            )
            yield AgentRuntimeEvent("final", final_payload)
        except Exception as exc:
            self.span_repository.finish_span(root_span, status="failed", error_message=str(exc))
            obs_root.finish(error=exc)
            yield event_bus.emit(
                agent_event(
                    "agent_error",
                    run_id=run_id,
                    sequence=event_sequence.next(),
                    status="failed",
                    payload={
                        "summary": truncate_text(str(exc), 240),
                        "chat_mode": policy.mode,
                        "metadata": {"trace_id": get_trace_id(), "policy": policy.mode},
                    },
                )
            )
            yield event_bus.emit(
                agent_event(
                    "agent_complete",
                    run_id=run_id,
                    sequence=event_sequence.next(),
                    status="failed",
                    payload={
                        "summary": "Agent run failed.",
                        "duration_ms": int((time.time() - run_started) * 1000),
                        "tool_counts": dict(state.get("tool_counts") or {}),
                        "chat_mode": policy.mode,
                        "metadata": {"trace_id": get_trace_id(), "policy": policy.mode},
                    },
                )
            )
            raise
        finally:
            event_bus.close()
            self.tool_registry.cleanup()

    def _thought_event_from_tool_result(
        self,
        result: RuntimeToolResult,
        state: dict[str, Any],
        run_id: str,
        sequence: int,
        *,
        call_id: str = "",
    ) -> AgentRuntimeEvent:
        metadata = dict(result.metadata or {})
        return agent_event(
            "agent_thought",
            run_id=run_id,
            sequence=sequence,
            status="partial" if metadata.get("gap") else "completed",
            payload={
                "phase": metadata.get("phase") or "reflection",
                "summary": metadata.get("summary") or result.observation or "Recorded public reasoning status.",
                "validity": metadata.get("validity") or "",
                "gap": metadata.get("gap") or "",
                "correction_query": metadata.get("correction_query") or "",
                "completion_status": metadata.get("completion_status") or state.get("reflection_completion_status") or "",
                "source_chunk_ids": metadata.get("source_chunk_ids") or result.source_chunk_ids,
                "metadata": {
                    "source": "thinking_tool",
                    "call_id": call_id,
                    "trace_id": get_trace_id(),
                },
            },
        )

    def run_react_iteration(
        self,
        *,
        policy: ChatRuntimePolicy,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        state: dict[str, Any],
        round_number: int,
    ) -> dict[str, Any]:
        state["llm_calls"] = int(state.get("llm_calls") or 0) + 1
        if policy.terminal_streaming_mode == "on":
            response = self._call_model_stream_buffered(
                messages,
                tools,
                tool_choice=policy.tool_choice,
                parallel_tool_calls_mode=policy.parallel_tool_calls_mode,
                state=state,
            )
        else:
            response = self._call_model(
                messages,
                tools,
                tool_choice=policy.tool_choice,
                parallel_tool_calls_mode=policy.parallel_tool_calls_mode,
                state=state,
            )
        state["_last_react_phase"] = {
            "round": round_number,
            "policy": policy.mode,
            "tool_calls": len(response.get("tool_calls") or []),
            "has_content": bool(str(response.get("content") or "").strip()),
            "model_latency_ms": int(response.get("_model_latency_ms") or 0),
            "model_first_byte_ms": int(response.get("_model_first_byte_ms") or 0),
            "provider_parallel_fallback": bool(response.get("_provider_parallel_fallback")),
        }
        return response

    def _build_action_batch(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        round_number: int,
        policy: ChatRuntimePolicy,
        remaining_tool_calls: int,
    ) -> ActionBatch:
        batch_id = f"round-{round_number}-{uuid4().hex[:8]}"
        calls: list[ActionToolCall] = []
        for index, raw_call in enumerate(tool_calls, start=1):
            function = raw_call.get("function") or {}
            tool_name = str(function.get("name") or "")
            call_id = str(raw_call.get("id") or f"round-{round_number}-tool-{index}")
            execution_class = self.tool_registry.execution_class(tool_name)
            arguments: dict[str, Any] = {}
            validation_error: RuntimeToolError | None = None
            if not self._policy_allows_tool(policy, tool_name):
                validation_error = RuntimeToolError(
                    "tool_not_allowed",
                    f"tool not allowed by active policy: {tool_name}",
                    fatal=False,
                )
            elif index > remaining_tool_calls:
                validation_error = RuntimeToolError(
                    "tool_call_budget_exceeded",
                    "tool call was refused because the per-request tool budget is exhausted",
                    fatal=False,
                )
            else:
                prepared, error_result = self.tool_registry.prepare(
                    tool_name,
                    function.get("arguments") or "{}",
                )
                arguments = prepared or {}
                if error_result is not None:
                    validation_error = error_result.structured_error or RuntimeToolError(
                        "validation_failed",
                        error_result.error or "tool arguments are invalid",
                        fatal=False,
                    )
            calls.append(
                ActionToolCall(
                    index=index,
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    execution_class=execution_class,
                    validation_error=validation_error,
                )
            )
        return ActionBatch(batch_id=batch_id, round_number=round_number, calls=tuple(calls))

    def _execute_action_batch(
        self,
        batch: ActionBatch,
        *,
        context: RuntimeToolContext,
        run_id: str,
        round_span: Any,
        policy: ChatRuntimePolicy,
    ) -> list[ActionToolExecution]:
        results: dict[int, ActionToolExecution] = {}
        calls = list(batch.calls)
        queued_at_by_index = {call.index: time.perf_counter() for call in calls}
        cursor = 0
        while cursor < len(calls):
            call = calls[cursor]
            if (
                call.execution_class == ToolExecutionClass.PARALLEL_SAFE
                and policy.local_concurrency_enabled
            ):
                end = cursor
                while (
                    end < len(calls)
                    and calls[end].execution_class == ToolExecutionClass.PARALLEL_SAFE
                ):
                    end += 1
                group = calls[cursor:end]
                workers = min(max(1, int(policy.max_parallel_workers)), len(group))
                if workers > 1:
                    with ThreadPoolExecutor(
                        max_workers=workers,
                        thread_name_prefix="agent-tool",
                    ) as executor:
                        futures = {
                            executor.submit(
                                self._execute_action_call,
                                item,
                                context.snapshot(),
                                run_id,
                                round_span,
                                batch.batch_id,
                                batch.round_number,
                                queued_at_by_index[item.index],
                            ): item.index
                            for item in group
                        }
                        for future in as_completed(futures):
                            results[futures[future]] = future.result()
                else:
                    for item in group:
                        results[item.index] = self._execute_action_call(
                            item,
                            context.snapshot(),
                            run_id,
                            round_span,
                            batch.batch_id,
                            batch.round_number,
                            queued_at_by_index[item.index],
                        )
                if any(
                    execution.result.structured_error is not None
                    and execution.result.structured_error.fatal
                    for execution in (results[item.index] for item in group)
                ):
                    self._cancel_remaining_batch_calls(calls[end:], results)
                    break
                cursor = end
                continue
            results[call.index] = self._execute_action_call(
                call,
                context.snapshot(),
                run_id,
                round_span,
                batch.batch_id,
                batch.round_number,
                queued_at_by_index[call.index],
            )
            if (
                results[call.index].result.structured_error is not None
                and results[call.index].result.structured_error.fatal
            ):
                self._cancel_remaining_batch_calls(calls[cursor + 1 :], results)
                break
            cursor += 1
        ordered_results = [results[index] for index in sorted(results)]
        if ordered_results:
            batch_origin = min(item.queued_at for item in ordered_results)
            completion_order = {
                item.call.index: rank
                for rank, item in enumerate(
                    sorted(ordered_results, key=lambda execution: execution.finished_at),
                    start=1,
                )
            }
            for item in ordered_results:
                item.result.metadata.setdefault(
                    "physical_completion_index",
                    completion_order[item.call.index],
                )
                item.result.metadata.setdefault(
                    "tool_started_offset_ms",
                    max(0, int((item.started_at - batch_origin) * 1000)),
                )
                item.result.metadata.setdefault(
                    "tool_finished_offset_ms",
                    max(0, int((item.finished_at - batch_origin) * 1000)),
                )
        return ordered_results

    def _cancel_remaining_batch_calls(
        self,
        calls: list[ActionToolCall],
        results: dict[int, ActionToolExecution],
    ) -> None:
        for call in calls:
            now = time.perf_counter()
            error = RuntimeToolError(
                "batch_cancelled",
                "tool call was cancelled after a fatal authorization or request-scope failure",
                fatal=True,
            )
            results[call.index] = ActionToolExecution(
                call=call,
                result=RuntimeToolResult(
                    success=False,
                    error=error.message,
                    observation=error.message,
                    structured_error=error,
                    metadata={"status": "cancelled", "error_code": error.code},
                ),
                queued_at=now,
                started_at=now,
                finished_at=now,
            )

    def _execute_action_call(
        self,
        call: ActionToolCall,
        context: RuntimeToolContext,
        run_id: str,
        round_span: Any,
        batch_id: str,
        round_number: int,
        queued_at: float,
    ) -> ActionToolExecution:
        started_at = time.perf_counter()
        if call.validation_error is not None:
            result = RuntimeToolResult(
                success=False,
                error=call.validation_error.message,
                observation=call.validation_error.message,
                structured_error=call.validation_error,
                metadata={
                    "status": "unavailable",
                    "error_code": call.validation_error.code,
                },
            )
            return ActionToolExecution(
                call=call,
                result=result,
                queued_at=queued_at,
                started_at=started_at,
                finished_at=time.perf_counter(),
            )

        tool_span = self.span_repository.start_span(
            run_id=run_id,
            name=f"tool.{call.tool_name}",
            kind="tool",
            parent_span_id=round_span.span_id if round_span is not None else "",
            input={"tool": call.tool_name, "input_summary": _input_summary(call.tool_name, call.arguments)},
            metadata={
                "round": round_number,
                "batch_id": batch_id,
                "call_id": call.call_id,
                "execution_class": call.execution_class.value,
                "trace_id": get_trace_id(),
            },
        )
        obs_tool = get_observability_sink().start_span(
            name=f"agent.tool.{call.tool_name}",
            input={
                "tool": call.tool_name,
                "input_summary": _input_summary(call.tool_name, call.arguments),
                "call_id": call.call_id,
            },
            metadata={
                "round": round_number,
                "batch_id": batch_id,
                "call_id": call.call_id,
                "execution_class": call.execution_class.value,
                "trace_id": get_trace_id(),
            },
        )
        obs_tool_context = activate_observation(obs_tool)
        obs_tool_context.__enter__()
        try:
            result = self.tool_registry.execute_prepared(
                call.tool_name,
                call.arguments,
                context,
            )
        finally:
            obs_tool_context.__exit__(None, None, None)
        finished_at = time.perf_counter()
        self.span_repository.finish_span(
            tool_span,
            status="completed" if result.success else "failed",
            output={
                "observation": result.observation,
                "source_chunk_ids": result.source_chunk_ids,
                "metadata": result.metadata,
                "duration_ms": int((finished_at - started_at) * 1000),
            },
            error_message=result.error,
        )
        obs_tool.finish(
            output={
                "success": result.success,
                "observation": result.observation,
                "source_chunk_ids": result.source_chunk_ids,
                "metadata": result.metadata,
                "duration_ms": int((finished_at - started_at) * 1000),
            },
            error=RuntimeError(result.error) if result.error else None,
        )
        return ActionToolExecution(
            call=call,
            result=result,
            queued_at=queued_at,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _pre_round_stop_reason(
        self,
        state: dict[str, Any],
        policy: ChatRuntimePolicy,
    ) -> str:
        elapsed = time.monotonic() - float(state.get("run_started_monotonic") or time.monotonic())
        if elapsed >= float(policy.max_wall_clock_seconds):
            return "wall_clock_budget"
        if int(state.get("llm_calls") or 0) >= int(policy.max_llm_calls):
            return "llm_call_budget"
        if int(state.get("executed_tool_calls") or 0) >= int(policy.max_tool_calls):
            return "tool_call_budget"
        return ""

    def _action_batch_signature(self, batch: ActionBatch) -> str:
        payload = [
            {"tool": call.tool_name, "arguments": call.arguments}
            for call in batch.calls
        ]
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _evidence_fingerprint(self, state: dict[str, Any]) -> str:
        payload = {
            "candidates": sorted(str(item) for item in state.get("search_candidate_ids") or []),
            "deep_reads": sorted(str(item) for item in state.get("deep_read_ids") or []),
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _is_repeated_action_batch(
        self,
        signature: str,
        evidence_fingerprint: str,
        state: dict[str, Any],
        policy: ChatRuntimePolicy,
    ) -> bool:
        if int(policy.max_repeated_tool_batches) <= 0:
            return False
        if (
            signature == state.get("last_action_signature")
            and evidence_fingerprint == state.get("last_action_evidence_fingerprint")
        ):
            state["repeated_action_batches"] = int(state.get("repeated_action_batches") or 0) + 1
        else:
            state["repeated_action_batches"] = 0
        state["last_action_signature"] = signature
        state["last_action_evidence_fingerprint"] = evidence_fingerprint
        return int(state.get("repeated_action_batches") or 0) >= int(policy.max_repeated_tool_batches)

    def _allowed_registered_tools(self, policy: ChatRuntimePolicy) -> tuple[str, ...]:
        registered = set(self.tool_registry.list_tools())
        return tuple(name for name in policy.enabled_tools if name in registered)

    def _policy_allows_tool(self, policy: ChatRuntimePolicy, tool_name: str) -> bool:
        return tool_name in set(self._allowed_registered_tools(policy))

    def _preload_retrieval(self, question: str, scope: KnowledgeBaseScope) -> list[dict[str, Any]]:
        hits = self.rag_service.recall_parent_hits(self.rag_service.hybrid_retrieve_hits(question, scope=scope), scope=scope)
        return list(hits or [])

    def _build_retrieved_context(self, hits: list[dict[str, Any]]) -> str:
        builder = getattr(self.rag_service, "_build_context", None)
        if callable(builder):
            return str(builder(hits) or "")
        parts = []
        for index, hit in enumerate(hits[:8], start=1):
            metadata = hit.get("metadata", {}) if isinstance(hit, dict) else {}
            source = metadata.get("source") or f"source-{index}"
            content = str(hit.get("content") or "").strip() if isinstance(hit, dict) else ""
            if content:
                parts.append(f"[{index}] {source}\n{content}")
        return "\n\n".join(parts)

    def _build_answer_guidance(self, question: str, context: str) -> str:
        builder = getattr(self.rag_service, "_build_answer_style_guidance", None)
        if callable(builder):
            return str(builder(question, context) or "")
        return (
            "- Answer in concise, structured Markdown.\n"
            "- Use only the provided evidence.\n"
            "- If evidence is insufficient, say the knowledge base does not contain enough information."
        )

    def _source_chunk_ids_from_hits(self, hits: list[dict[str, Any]]) -> list[str]:
        chunk_ids: list[str] = []
        for hit in hits:
            metadata = hit.get("metadata", {}) if isinstance(hit, dict) else {}
            for key in ("chunk_id", "child_id", "parent_id"):
                value = str(metadata.get(key) or "").strip()
                if value:
                    chunk_ids.append(value)
                    break
            matched = metadata.get("matched_child_ids")
            if isinstance(matched, list):
                chunk_ids.extend(str(item) for item in matched if item)
        return list(dict.fromkeys(chunk_ids))

    def _reflection_event_from_state(
        self,
        state: dict[str, Any],
        *,
        run_id: str,
        sequence: int,
        status: str,
        final_status: str,
    ) -> AgentRuntimeEvent:
        gap = str(state.get("reflection_gap") or "").strip()
        correction_query = str(state.get("correction_query") or "").strip()
        sufficient = final_status == "completed" and not gap
        summary = (
            "Evidence has been deep-read and is sufficient for a sourced answer."
            if sufficient
            else gap or "Evidence remained insufficient for a certain answer."
        )
        return agent_event(
            "agent_reflection",
            run_id=run_id,
            sequence=sequence,
            status=status,
            payload={
                "phase": "final_check",
                "summary": summary,
                "validity": "sufficient" if sufficient else "insufficient_or_partial",
                "gap": "" if sufficient else gap,
                "correction_query": correction_query,
                "completion_status": "sufficient" if sufficient else "insufficient",
                "source_chunk_ids": list(state.get("deep_read_ids") or []),
                "metadata": {
                    "trace_id": get_trace_id(),
                    "remedial_attempts": int(state.get("remedial_attempts") or 0),
                    "remedial_used": bool(state.get("remedial_used")),
                },
            },
        )

    def _should_run_remedial_retrieval(self, state: dict[str, Any], policy: ChatRuntimePolicy) -> bool:
        if not policy.remedial_retrieval_enabled:
            return False
        if not str(state.get("reflection_gap") or "").strip():
            return False
        if not str(state.get("correction_query") or "").strip():
            return False
        if int(state.get("remedial_attempts") or 0) >= int(policy.max_remedial_retrieval_attempts):
            return False
        return self.tool_registry.get("knowledge_search") is not None

    def _has_unresolved_reflection_gap(self, state: dict[str, Any]) -> bool:
        if not str(state.get("reflection_gap") or "").strip():
            return False
        completion_status = str(state.get("reflection_completion_status") or "").strip().lower()
        return completion_status not in {"sufficient", "complete", "completed"}

    def _run_remedial_retrieval(
        self,
        *,
        context: RuntimeToolContext,
        state: dict[str, Any],
        trace: list[dict[str, Any]],
        tool_events: list[dict[str, Any]],
        run_id: str,
        event_sequence: AgentEventSequencer,
        event_bus: AgentEventBus,
        round_number: int,
    ) -> Generator[AgentRuntimeEvent, None, None]:
        correction_query = str(state.get("correction_query") or "").strip()
        if not correction_query:
            return
        attempt = int(state.get("remedial_attempts") or 0) + 1
        state["remedial_attempts"] = attempt
        state["remedial_used"] = True
        before_candidates = set(state.get("search_candidate_ids") or set())
        before_deep_reads = set(state.get("deep_read_ids") or set())
        yield event_bus.emit(
            agent_event(
                "agent_remedial_search",
                run_id=run_id,
                sequence=event_sequence.next(),
                status="running",
                payload={
                    "summary": "Running a follow-up knowledge-base search to repair an evidence gap.",
                    "gap": state.get("reflection_gap") or "",
                    "correction_query": correction_query,
                    "attempt": attempt,
                    "metadata": {"round": round_number, "trace_id": get_trace_id()},
                },
            )
        )
        search_result = yield from self._execute_controller_tool(
            tool_name="knowledge_search",
            arguments={"query": correction_query, "top_k": 8},
            context=context,
            state=state,
            trace=trace,
            tool_events=tool_events,
            run_id=run_id,
            event_sequence=event_sequence,
            event_bus=event_bus,
            round_number=round_number,
            call_id=f"remedial-{attempt}-search",
        )
        candidate_ids = [
            item
            for item in (search_result.candidate_ids or search_result.source_chunk_ids)
            if item and item not in before_candidates and item not in before_deep_reads
        ]
        candidate_ids = list(dict.fromkeys(candidate_ids))[:8]
        if not candidate_ids or self.tool_registry.get("list_knowledge_chunks") is None:
            state["reflection_completion_status"] = "insufficient"
            return
        read_result = yield from self._execute_controller_tool(
            tool_name="list_knowledge_chunks",
            arguments={"chunk_ids": candidate_ids, "knowledge_ids": candidate_ids, "limit": 8},
            context=context,
            state=state,
            trace=trace,
            tool_events=tool_events,
            run_id=run_id,
            event_sequence=event_sequence,
            event_bus=event_bus,
            round_number=round_number,
            call_id=f"remedial-{attempt}-read",
        )
        new_reads = [item for item in read_result.source_chunk_ids if item not in before_deep_reads]
        state["last_remedial_new_chunk_ids"] = new_reads
        if new_reads:
            state.pop("reflection_gap", None)
            state.pop("correction_query", None)
            state["reflection_completion_status"] = "sufficient"

    def _execute_controller_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        context: RuntimeToolContext,
        state: dict[str, Any],
        trace: list[dict[str, Any]],
        tool_events: list[dict[str, Any]],
        run_id: str,
        event_sequence: AgentEventSequencer,
        event_bus: AgentEventBus,
        round_number: int,
        call_id: str,
    ) -> Generator[AgentRuntimeEvent, None, RuntimeToolResult]:
        input_summary = _input_summary(tool_name, arguments)
        call_payload = {
            "call_id": call_id,
            "tool": tool_name,
            "action": "execute",
            "input_summary": input_summary,
            "metadata": {"round": round_number, "controller": "remedial_retrieval", "trace_id": get_trace_id()},
        }
        tool_events.append(call_payload)
        yield event_bus.emit(tool_call_to_agent_event(call_payload, run_id=run_id, sequence=event_sequence.next()))
        yield AgentRuntimeEvent("tool_call", call_payload)
        result = self.tool_registry.execute(tool_name, arguments, context)
        self._record_tool_state(tool_name, result, state)
        observation = result.to_observation_text()
        observation_payload = {
            "call_id": call_id,
            "tool": tool_name,
            "action": "execute",
            "status": "completed" if result.success else "failed",
            "output_summary": result.observation or truncate_text(observation, 240),
            "source_chunk_ids": result.source_chunk_ids,
            "metadata": {
                **result.metadata,
                "round": round_number,
                "call_id": call_id,
                "source_titles": result.source_titles,
                "controller": "remedial_retrieval",
                "trace_id": get_trace_id(),
            },
        }
        tool_events.append(observation_payload)
        yield event_bus.emit(tool_result_to_agent_event(observation_payload, run_id=run_id, sequence=event_sequence.next()))
        yield AgentRuntimeEvent("tool_observation", observation_payload)
        return result

    def _build_messages(
        self,
        question: str,
        scope: KnowledgeBaseScope,
        conversation_context: str | dict[str, Any] | None,
        memory_context: str | None,
        attachments: list[dict[str, Any]] | None,
        *,
        policy: ChatRuntimePolicy | None = None,
        contexts: list[dict[str, Any]] | str | None = None,
        answer_guidance: str = "",
        request_skills=None,
    ) -> list[dict[str, Any]]:
        policy = policy or resolve_chat_runtime_policy("reasoning", self.config)
        system_prompt = self.prompt_catalog.render(
            policy.prompt_template_id,
            language="zh-CN",
            web_search_enabled=self.config.web_search_enabled,
            knowledge_bases=scope_to_prompt_kbs(scope, getattr(self.rag_service, "knowledge_base_service", None)),
            tools=[item for item in self.tool_registry.metadata() if item.get("name") in set(self._allowed_registered_tools(policy))],
            skills=(request_skills or self.skills_manager).metadata() if (request_skills or self.skills_manager) is not None else [],
        )
        knowledge_bases = scope_to_prompt_kbs(scope, getattr(self.rag_service, "knowledge_base_service", None))
        user_content = self.context_catalog.render(
            policy.context_template_id,
            query=question,
            language="zh-CN",
            contexts=contexts,
            conversation_context=conversation_context,
            memory_context=memory_context,
            temporary_attachments=attachments or [],
            knowledge_base_scope=scope.to_dict(),
            knowledge_bases=knowledge_bases,
            answer_guidance=answer_guidance,
        )
        messages = [{"role": "system", "content": system_prompt}]
        if isinstance(request_skills, RequestSkillsManager) and request_skills.resolved:
            messages.append({"role": "user", "content": request_skills.instruction_message()})
        messages.append({"role": "user", "content": user_content})
        return messages

    def _call_model(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto",
        parallel_tool_calls_mode: str = "off",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        kwargs = {
            "model": self.chat_model,
            "messages": messages,
            "temperature": 0.2,
        }
        if tools and tool_choice != "none":
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
            cached_support = self._parallel_tool_calls_support.get(self.chat_model)
            if parallel_tool_calls_mode in {"auto", "on"} and cached_support is not False:
                kwargs["parallel_tool_calls"] = True
        generation = get_observability_sink().start_generation(
            name="chat.completion",
            model=self.chat_model,
            input={"messages": messages, "tool_count": len(tools)},
            metadata={"has_tools": bool(tools), "trace_id": get_trace_id()},
            model_parameters={
                "temperature": 0.2,
                "tool_choice": kwargs.get("tool_choice", ""),
                "parallel_tool_calls": kwargs.get("parallel_tool_calls"),
            },
        )
        try:
            response = self.llm_client.chat.completions.create(**kwargs)
        except Exception as exc:
            if "parallel_tool_calls" in kwargs and _is_unsupported_parallel_tool_calls_error(exc):
                kwargs.pop("parallel_tool_calls", None)
                self._parallel_tool_calls_support[self.chat_model] = False
                if state is not None:
                    state["provider_parallel_fallback"] = True
                try:
                    response = self.llm_client.chat.completions.create(**kwargs)
                except Exception as retry_exc:
                    generation.finish(error=retry_exc)
                    raise
            else:
                generation.finish(error=exc)
                raise
        else:
            if "parallel_tool_calls" in kwargs:
                self._parallel_tool_calls_support[self.chat_model] = True
        message = response.choices[0].message
        if hasattr(message, "model_dump"):
            data = message.model_dump()
        elif isinstance(message, dict):
            data = dict(message)
        else:
            data = {
                "content": getattr(message, "content", ""),
                "tool_calls": getattr(message, "tool_calls", None),
            }
        data["tool_calls"] = [_normalize_tool_call(call) for call in (data.get("tool_calls") or [])]
        data["_model_latency_ms"] = int((time.perf_counter() - started) * 1000)
        data["_provider_parallel_fallback"] = bool(
            state is not None and state.get("provider_parallel_fallback")
        )
        usage = getattr(response, "usage", None)
        generation.finish(
            output={
                "content": data.get("content") or "",
                "tool_calls": data["tool_calls"],
                "finish_reason": getattr(response.choices[0], "finish_reason", ""),
                "model_latency_ms": data["_model_latency_ms"],
                "provider_parallel_fallback": data["_provider_parallel_fallback"],
            },
            usage=_token_usage(usage),
        )
        return data

    def _call_model_stream_buffered(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto",
        parallel_tool_calls_mode: str = "off",
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": self.chat_model,
            "messages": messages,
            "temperature": 0.2,
            "stream": True,
        }
        if tools and tool_choice != "none":
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
            cached_support = self._parallel_tool_calls_support.get(self.chat_model)
            if parallel_tool_calls_mode in {"auto", "on"} and cached_support is not False:
                kwargs["parallel_tool_calls"] = True
        try:
            response = self.llm_client.chat.completions.create(**kwargs)
        except Exception as exc:
            if "parallel_tool_calls" in kwargs and _is_unsupported_parallel_tool_calls_error(exc):
                kwargs.pop("parallel_tool_calls", None)
                self._parallel_tool_calls_support[self.chat_model] = False
                if state is not None:
                    state["provider_parallel_fallback"] = True
                response = self.llm_client.chat.completions.create(**kwargs)
            else:
                raise

        if not hasattr(response, "__iter__"):
            data = _response_message_data(response)
            data["_stream_tokens"] = _answer_token_chunks(str(data.get("content") or ""))
            data["_model_first_byte_ms"] = int((time.perf_counter() - started) * 1000)
            data["_model_latency_ms"] = data["_model_first_byte_ms"]
            return data

        content_parts: list[str] = []
        tool_parts: dict[int, dict[str, Any]] = {}
        first_byte_ms = 0
        finish_reason = ""
        for chunk in response:
            if not first_byte_ms:
                first_byte_ms = int((time.perf_counter() - started) * 1000)
            choices = getattr(chunk, "choices", None) or (
                chunk.get("choices") if isinstance(chunk, dict) else []
            )
            if not choices:
                continue
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            if delta is None and isinstance(choice, dict):
                delta = choice.get("delta") or {}
            finish_reason = str(
                getattr(choice, "finish_reason", "")
                or (choice.get("finish_reason") if isinstance(choice, dict) else "")
                or finish_reason
            )
            content = _field(delta, "content")
            if content:
                content_parts.append(str(content))
            for raw_tool_delta in _field(delta, "tool_calls") or []:
                index = int(_field(raw_tool_delta, "index") or 0)
                current = tool_parts.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                current["id"] += str(_field(raw_tool_delta, "id") or "")
                function = _field(raw_tool_delta, "function") or {}
                current["function"]["name"] += str(_field(function, "name") or "")
                current["function"]["arguments"] += str(_field(function, "arguments") or "")
        content = "".join(content_parts)
        tool_calls = [tool_parts[index] for index in sorted(tool_parts)]
        return {
            "content": content,
            "tool_calls": tool_calls,
            "finish_reason": finish_reason,
            "_stream_tokens": list(content_parts),
            "_model_first_byte_ms": first_byte_ms,
            "_model_latency_ms": int((time.perf_counter() - started) * 1000),
            "_provider_parallel_fallback": bool(
                state is not None and state.get("provider_parallel_fallback")
            ),
        }

    def _final_allowed(self, state: dict[str, Any], policy: ChatRuntimePolicy | None = None) -> bool:
        if policy is not None and not policy.require_deep_read:
            return True
        candidates = set(state.get("search_candidate_ids") or set())
        if not candidates:
            return True
        return bool(set(state.get("deep_read_ids") or set()))

    def _record_tool_state(self, tool_name: str, result: RuntimeToolResult, state: dict[str, Any]) -> None:
        counts = state.setdefault("tool_counts", {})
        counts[tool_name] = int(counts.get(tool_name, 0)) + 1
        delta = result.state_delta
        has_explicit_delta = bool(
            delta.candidate_ids
            or delta.deep_read_ids
            or delta.source_titles
            or delta.flags
            or delta.counters
            or delta.append_values
            or delta.replace_values
            or delta.debug
        )
        apply_runtime_state_delta(state, delta)
        if not has_explicit_delta:
            fallback_delta = RuntimeStateDelta(
                candidate_ids=list(result.candidate_ids),
                deep_read_ids=list(result.source_chunk_ids or result.candidate_ids)
                if result.deep_read or tool_name in {"list_knowledge_chunks", "get_document_info"}
                else [],
                source_titles=list(result.source_titles),
                flags={
                    "grep_first_performed": bool(tool_name == "grep_chunks" and result.success),
                    "semantic_search_performed": bool(tool_name == "knowledge_search" and result.success),
                },
            )
            apply_runtime_state_delta(state, fallback_delta)

    def _should_block_for_grep_first(self, state: dict[str, Any], policy: ChatRuntimePolicy) -> bool:
        return False

    def _should_block_web_search_until_knowledge_base_checked(
        self,
        tool_calls: list[dict[str, Any]],
        state: dict[str, Any],
        policy: ChatRuntimePolicy,
    ) -> bool:
        if policy.quick:
            return False
        tool_names = [str((call.get("function") or {}).get("name") or "") for call in tool_calls]
        if "web_search" not in tool_names:
            return False
        if "web_search" not in set(self._allowed_registered_tools(policy)):
            return False
        if not _question_needs_knowledge_base_check(str(state.get("question") or "")):
            return False
        has_searched_kb = bool(state.get("grep_first_performed") or state.get("semantic_search_performed"))
        has_candidates = bool(state.get("search_candidate_ids"))
        has_deep_read = bool(state.get("deep_read_ids"))
        return not ((has_searched_kb and not has_candidates) or has_deep_read)

    def _should_block_tool_calls_for_grep_first(
        self,
        tool_calls: list[dict[str, Any]],
        state: dict[str, Any],
        policy: ChatRuntimePolicy,
    ) -> bool:
        if not policy.grep_first_enabled or policy.quick or state.get("grep_first_performed"):
            return False
        if "grep_chunks" not in set(self._allowed_registered_tools(policy)):
            return False
        if not _question_needs_exact_grep_anchor(str(state.get("question") or "")):
            return False
        tool_names = [str((call.get("function") or {}).get("name") or "") for call in tool_calls]
        if "grep_chunks" in tool_names:
            return False
        retrieval_tools = {"knowledge_search", "query_knowledge_graph", "list_knowledge_chunks", "get_document_info"}
        return any(name in retrieval_tools for name in tool_names)

    def _append_grep_first_guard_message(self, messages: list[dict[str, Any]], question: str) -> None:
        messages.append(
            {
                "role": "user",
                "content": (
                    "Runtime guard: the first LLM-selected knowledge-base retrieval batch must include grep_chunks. "
                    "Use your language and domain knowledge to include "
                    "synonyms, aliases, abbreviations, English names, legacy names, product names, and time/action "
                    f"variants for this question: {question}"
                ),
            }
        )

    def _append_knowledge_base_before_web_guard_message(self, messages: list[dict[str, Any]], question: str) -> None:
        messages.append(
            {
                "role": "user",
                "content": (
                    "Runtime guard: web_search cannot be the first evidence step for this factual or domain-specific "
                    "question. Search the selected knowledge base first with grep_chunks and/or knowledge_search, "
                    "then deep-read relevant results with list_knowledge_chunks or get_document_info. Use web_search "
                    "only after the knowledge-base evidence is empty or insufficient. "
                    f"Question: {question}"
                ),
            }
        )

    def _sources_from_state(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        sources = []
        seen = set()
        for item in state.get("sources", []):
            source = str(item.get("source") or "").strip()
            if source and source not in seen:
                sources.append(item)
                seen.add(source)
        return sources

    def _terminal_answer_from_state(
        self,
        *,
        question: str,
        messages: list[dict[str, Any]],
        state: dict[str, Any],
        policy: ChatRuntimePolicy,
    ) -> tuple[str, str]:
        if not state.get("deep_read_ids") or self._has_unresolved_reflection_gap(state):
            state["terminal_synthesis_reserved"] = False
            return self._fallback_answer_from_state(state), "partial"
        synthesis_messages = copy.deepcopy(messages)
        synthesis_messages.append(
            {
                "role": "user",
                "content": (
                    "Terminal synthesis: tools are now disabled. Answer the original question using only the "
                    "full-content evidence already returned by tools. If that evidence is insufficient, say so "
                    f"briefly and do not invent facts. Original question: {question}"
                ),
            }
        )
        state["terminal_synthesis_reserved"] = False
        state["terminal_synthesis_used"] = True
        state["llm_calls"] = int(state.get("llm_calls") or 0) + 1
        try:
            if policy.terminal_streaming_mode == "off":
                response = self._call_model(
                    synthesis_messages,
                    [],
                    tool_choice="none",
                    parallel_tool_calls_mode="off",
                    state=state,
                )
            else:
                response = self._call_model_stream_buffered(
                    synthesis_messages,
                    [],
                    tool_choice="none",
                    parallel_tool_calls_mode="off",
                    state=state,
                )
        except Exception:
            logger.exception(
                "agent_runtime.terminal_synthesis_failed",
                extra={"trace_id": get_trace_id(), "stop_reason": state.get("stop_reason")},
            )
            state["terminal_synthesis_failed"] = True
            return self._fallback_answer_from_state(state), "partial"
        answer = str(response.get("content") or "").strip()
        if not answer:
            state["terminal_synthesis_failed"] = True
            return self._fallback_answer_from_state(state), "partial"
        state["terminal_model_latency_ms"] = int(response.get("_model_latency_ms") or 0)
        state["terminal_first_byte_ms"] = int(response.get("_model_first_byte_ms") or 0)
        state["terminal_stream_tokens"] = list(
            response.get("_stream_tokens") or _answer_token_chunks(answer)
        )
        return answer, "completed"

    def _fallback_answer_from_state(self, state: dict[str, Any]) -> str:
        reason = (
            "evidence was retrieved but remained insufficient for a certain answer"
            if state.get("deep_read_ids")
            else "no sufficient knowledge-base evidence was found"
        )
        if state.get("deep_read_ids"):
            return (
                "已检索并读取到部分知识库证据，但当前证据仍不足以形成确定答案。"
                f"原因：{reason}。建议补充更精确的对象标识、来源材料，或调整部分条件后重试。"
            )
        return "无法从当前知识库证据中确定答案。建议补充相关文档，或使用更明确的实体、关系和条件重新检索。"

    def _record_trace(
        self,
        trace: list[dict[str, Any]],
        stage: str,
        status: str,
        summary: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRuntimeEvent:
        event = self._trace_event(stage, status, summary, metadata=metadata)
        trace.append(event.payload)
        return event

    def _trace_event(self, stage: str, status: str, summary: str, *, metadata: dict[str, Any] | None = None) -> AgentRuntimeEvent:
        logger.info(
            "agent_runtime.trace",
            extra={"stage": stage, "status": status, "trace_id": get_trace_id(), **(metadata or {})},
        )
        return AgentRuntimeEvent(
            "agent_trace",
            {
                "stage": stage,
                "status": status,
                "summary": summary,
                "metadata": scrub_private_fields(sanitize_payload(metadata or {}, limit=1024)),
            },
        )


def _normalize_tool_call(call: Any) -> dict[str, Any]:
    if hasattr(call, "model_dump"):
        data = call.model_dump()
    elif isinstance(call, dict):
        data = dict(call)
    else:
        function = getattr(call, "function", None)
        data = {
            "id": getattr(call, "id", ""),
            "type": getattr(call, "type", "function"),
            "function": {
                "name": getattr(function, "name", "") if function is not None else "",
                "arguments": getattr(function, "arguments", "{}") if function is not None else "{}",
            },
        }
    function = data.get("function") or {}
    if hasattr(function, "model_dump"):
        function = function.model_dump()
    data["function"] = dict(function)
    return data


def _tool_call_message(call: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(call.get("id") or ""),
        "type": "function",
        "function": {
            "name": str((call.get("function") or {}).get("name") or ""),
            "arguments": str((call.get("function") or {}).get("arguments") or "{}"),
        },
    }


def _input_summary(tool_name: str, arguments: Any) -> str:
    query = ""
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        if isinstance(parsed.get("queries"), list):
            queries = _display_query_variants(parsed.get("queries") or [])
            query = " | ".join(queries) if queries else f"{len(parsed.get('queries') or [])} query variants"
        else:
            query = str(parsed.get("query") or parsed.get("skill_name") or "")
    except Exception:
        query = ""
    if query:
        return f"{tool_name}: {truncate_text(query, 320)}"
    return tool_name


def _display_query_variants(values: list[Any]) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()
    for value in values:
        query = str(value or "").strip()
        if not query:
            continue
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        queries.append(query)
        if len(queries) >= 12:
            break
    return queries


def _token_usage(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or (prompt + completion))
    return {"input": prompt, "output": completion, "total": total, "unit": "TOKENS"} if total else {}


def _question_needs_exact_grep_anchor(question: str) -> bool:
    text = str(question or "")
    if not text.strip():
        return False
    exact_patterns = [
        r"[<>]=?\s*\d",
        r"\d+(?:\.\d+)?\s*(?:Mpps|Gbps|Mbps|GE|G|T|U|K)\b",
        r"\b[A-Z]{2,}[A-Z0-9+._/-]*\b",
        r"\b[A-Za-z]+[-_/]?\d+[A-Za-z0-9+._/-]*\b",
        r"\d+\s*[*xX]\s*[A-Za-z0-9+]+",
        r"[A-Za-z0-9]+:[A-Za-z0-9.]+",
    ]
    return any(re.search(pattern, text) for pattern in exact_patterns)


def _question_needs_knowledge_base_check(question: str) -> bool:
    text = re.sub(r"\s+", " ", str(question or "").strip())
    if not text:
        return False
    lowered = text.lower()
    direct_chat = {
        "hi",
        "hello",
        "hey",
        "thanks",
        "thank you",
        "你好",
        "您好",
        "谢谢",
        "多谢",
        "早上好",
        "晚上好",
    }
    if lowered in direct_chat:
        return False
    if len(text) <= 8 and any(item in text for item in ("你好", "谢谢", "嗨", "hi", "hello")):
        return False
    return True


def _is_unsupported_parallel_tool_calls_error(exc: Exception) -> bool:
    message = str(exc or "").lower()
    return "parallel_tool_calls" in message and any(
        marker in message
        for marker in ("unsupported", "unknown", "unexpected", "not allowed", "invalid")
    )


def _response_message_data(response: Any) -> dict[str, Any]:
    choices = getattr(response, "choices", None) or (
        response.get("choices") if isinstance(response, dict) else []
    )
    if not choices:
        return {"content": "", "tool_calls": []}
    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None and isinstance(choice, dict):
        message = choice.get("message") or {}
    if hasattr(message, "model_dump"):
        data = message.model_dump()
    elif isinstance(message, dict):
        data = dict(message)
    else:
        data = {
            "content": getattr(message, "content", ""),
            "tool_calls": getattr(message, "tool_calls", None),
        }
    data["tool_calls"] = [
        _normalize_tool_call(call)
        for call in (data.get("tool_calls") or [])
    ]
    data["finish_reason"] = getattr(choice, "finish_reason", "") or (
        choice.get("finish_reason", "") if isinstance(choice, dict) else ""
    )
    return data


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _answer_token_chunks(content: str, *, max_chars: int = 48) -> list[str]:
    text = str(content or "")
    if not text:
        return []
    return [text[index : index + max_chars] for index in range(0, len(text), max_chars)]
