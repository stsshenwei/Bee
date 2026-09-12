import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  buildAgentTimeline,
  countUniqueSourceDocuments,
  deriveAgentRunSummary,
  deriveSearchSummary,
  normalizeAgentPayload,
  scrubPrivateFields,
  stageLabel,
  toolLabel,
} from "./agent-stream.ts";

test("normalizes supported agent stream payloads", () => {
  const trace = normalizeAgentPayload("agent_trace", { stage: "AnalyzeQuestion", summary: "按问题检索" }, 1, 1000);
  const call = normalizeAgentPayload("tool_call", { tool: "RawRAGTool", action: "search", input_summary: "搜索 \"Redis\"" }, 2, 1100);
  const result = normalizeAgentPayload(
    "tool_observation",
    {
      tool: "RawRAGTool",
      action: "search",
      output_summary: "命中 2 条",
      source_chunk_ids: ["c1", "c2", "c2"],
      metadata: { evidence_items: 2, citations: 2 },
    },
    3,
    1200,
  );
  const evidence = normalizeAgentPayload(
    "evidence_summary",
    { evidence_items: 2, citations: 2, used_chunks: 2, graph_paths: 0, sufficient: true, confidence: 0.8 },
    4,
    1300,
  );
  const citation = normalizeAgentPayload(
    "citation_verification",
    { valid: true, verified_chunks: ["c1", "c2"], invalid_chunks: [] },
    5,
    1400,
  );

  assert.equal(trace.kind, "agent_trace");
  assert.equal(trace.stage, "AnalyzeQuestion");
  assert.equal(call.kind, "tool_call");
  assert.equal(call.tool, "RawRAGTool");
  assert.equal(result.kind, "tool_result");
  assert.deepEqual(result.sourceChunkIds, ["c1", "c2"]);
  assert.equal(result.counts?.evidenceItems, 2);
  assert.equal(evidence.status, "completed");
  assert.equal(evidence.counts?.confidence, 0.8);
  assert.equal(citation.status, "completed");
  assert.equal(citation.counts?.verifiedChunks, 2);
});

test("scrubs private reasoning fields recursively", () => {
  const clean = scrubPrivateFields({
    summary: "公开摘要",
    chain_of_thought: "hidden",
    nested: { scratchpad: "hidden", keep: "visible" },
    list: [{ raw_prompt: "hidden", value: 1 }],
  });

  assert.deepEqual(clean, {
    summary: "公开摘要",
    nested: { keep: "visible" },
    list: [{ value: 1 }],
  });
});

test("pairs tool call and result into one completed product timeline step", () => {
  const events = [
    normalizeAgentPayload("tool_call", { tool: "KeywordSearchTool", action: "search", input_summary: "搜索 \"k3s\"" }, 1, 1000),
    normalizeAgentPayload(
      "tool_observation",
      { tool: "KeywordSearchTool", action: "search", output_summary: "命中 3 条", source_chunk_ids: ["k1", "k2", "k3"] },
      2,
      1250,
    ),
  ];

  const steps = buildAgentTimeline(events);
  assert.equal(steps.length, 1);
  assert.equal(steps[0].status, "completed");
  assert.equal(steps[0].title, "搜索关键词：k3s");
  assert.equal(steps[0].summary, "找到 3 个匹配片段");
  assert.equal(steps[0].elapsedMs, 250);
  assert.deepEqual(steps[0].sourceChunkIds, ["k1", "k2", "k3"]);
});

test("describes wiki search pages and query candidates without chunk wording", () => {
  const events = [
    normalizeAgentPayload("agent_tool_call", { tool: "wiki_search", call_id: "w1", input_summary: "认证" }, 1, 1000),
    normalizeAgentPayload(
      "agent_tool_result",
      {
        tool: "wiki_search",
        call_id: "w1",
        metadata: {
          result_count: 0,
          query_count: 4,
          source_titles: ["日志", "索引", "DH-P5000-08GP-AC", "ACL"],
        },
      },
      2,
      1100,
    ),
  ];

  const steps = buildAgentTimeline(events);

  assert.equal(steps[0].title, "搜索 Wiki");
  assert.equal(steps[0].summary, "未命中 Wiki 页面");
  assert.equal(steps[0].detail, "0 个 Wiki 页面，4 个关键词");
  assert.doesNotMatch(steps[0].detail || "", /匹配片段|相关内容/);
  assert.deepEqual(steps[0].sourceTitles, ["日志", "索引", "DH-P5000-08GP-AC", "ACL"]);
});

test("dedupes legacy tool events when matching domain tool events are present", () => {
  const events = [
    normalizeAgentPayload("agent_tool_call", { tool: "knowledge_search", action: "execute", call_id: "a1", input_summary: "Redis" }, 1, 1000),
    normalizeAgentPayload("tool_call", { tool: "knowledge_search", action: "execute", call_id: "a1", input_summary: "Redis" }, 2, 1005),
    normalizeAgentPayload(
      "agent_tool_result",
      { tool: "knowledge_search", action: "execute", call_id: "a1", output_summary: "1 hit", source_chunk_ids: ["c1"] },
      3,
      1200,
    ),
    normalizeAgentPayload(
      "tool_observation",
      { tool: "knowledge_search", action: "execute", call_id: "a1", output_summary: "1 hit", source_chunk_ids: ["c1"] },
      4,
      1205,
    ),
  ];

  const steps = buildAgentTimeline(events);
  const summary = deriveAgentRunSummary(events, steps, true);

  assert.equal(steps.length, 1);
  assert.equal(steps[0].title, "语义检索");
  assert.equal(steps[0].status, "completed");
  assert.equal(summary.toolCalls, 1);
});

test("normalizes domain agent lifecycle events into Weknora-like timeline steps", () => {
  const events = [
    normalizeAgentPayload("agent_query", { summary: "received" }, 1, 1000),
    normalizeAgentPayload("agent_thought", { summary: "initial scan", phase: "initial_scan" }, 2, 1050),
    normalizeAgentPayload("agent_tool_call", { tool: "knowledge_search", action: "execute", call_id: "a1", input_summary: "Redis" }, 3, 1100),
    normalizeAgentPayload(
      "agent_tool_result",
      { tool: "knowledge_search", action: "execute", call_id: "a1", output_summary: "1 hit", source_chunk_ids: ["c1"] },
      4,
      1200,
    ),
    normalizeAgentPayload(
      "agent_reflection",
      { summary: "missing version", gap: "missing version", correction_query: "Redis version", completion_status: "needs_more_evidence" },
      5,
      1250,
    ),
    normalizeAgentPayload("agent_remedial_search", { correction_query: "Redis version", summary: "repair gap" }, 6, 1300),
    normalizeAgentPayload("agent_references", { items: [{ source: "manual.md" }], source_chunk_ids: ["c1"] }, 7, 1400),
    normalizeAgentPayload("agent_final_answer", { answer: "Redis version is 7.2.", citation_count: 1 }, 8, 1450),
    normalizeAgentPayload("agent_complete", { remedial_used: true, summary: "complete" }, 9, 1500),
  ];

  const steps = buildAgentTimeline(events);
  const summary = deriveAgentRunSummary(events, steps, true);

  assert.equal(steps.find((step) => step.kind === "reflection")?.title, "反思验证");
  assert.equal(steps.find((step) => step.title === "补救检索")?.summary, "根据证据缺口补充检索：Redis version");
  assert.equal(steps.find((step) => step.kind === "references")?.title, "引用来源");
  assert.deepEqual(steps.find((step) => step.kind === "references")?.sourceTitles, ["manual.md"]);
  assert.equal(summary.toolCalls, 1);
  assert.equal(summary.referencedDocuments, 1);
  assert.equal(summary.remedialUsed, true);
});

test("quick unified runtime domain events render without artificial tools", () => {
  const events = [
    normalizeAgentPayload("agent_query", { summary: "received", chat_mode: "quick" }, 1, 1000),
    normalizeAgentPayload("agent_references", { items: [{ source: "manual.md" }], metadata: { policy: "quick" } }, 2, 1050),
    normalizeAgentPayload("agent_final_answer", { answer: "Redis is used by API Gateway.", citation_count: 1 }, 3, 1100),
    normalizeAgentPayload("agent_complete", { chat_mode: "quick", summary: "complete", metadata: { policy: "quick" } }, 4, 1150),
  ];

  const steps = buildAgentTimeline(events);
  const summary = deriveAgentRunSummary(events, steps, true);

  assert.equal(summary.toolCalls, 0);
  assert.equal(summary.status, "completed");
  assert.equal(summary.referencedDocuments, 1);
  assert.equal(steps.some((step) => step.kind === "tool"), false);
  assert.equal(steps.find((step) => step.kind === "references")?.title, "引用来源");
  assert.deepEqual(steps.find((step) => step.kind === "references")?.sourceTitles, ["manual.md"]);
});

test("reference events count visible unique source documents", () => {
  const events = [
    normalizeAgentPayload(
      "agent_references",
      {
        items: [
          { source: "TSFP-CU3M-DAC.txt" },
          { source: "TSFP-CU3M-DAC.txt" },
          { source: "DH-WBC5-08AC-20.txt" },
          { source: "DH-WBC5-08AC-20.txt" },
        ],
      },
      1,
      1000,
    ),
  ];

  const steps = buildAgentTimeline(events);
  const summary = deriveAgentRunSummary(events, steps, true);

  assert.equal(steps[0].summary, "已准备 2 个引用来源");
  assert.equal(steps[0].counts?.citations, 2);
  assert.equal(summary.referencedDocuments, 2);
});

test("unknown additive agent event can fall back without crashing", () => {
  const event = normalizeAgentPayload("agent_thought", { event_id: "custom", summary: "safe", raw_tool_payload: "hidden" }, 1, 1000);
  const steps = buildAgentTimeline([event]);

  assert.equal(event.id, "custom");
  assert.equal(steps.length, 1);
  assert.equal(steps[0].kind, "thought");
  assert.equal(event.metadata.raw_tool_payload, undefined);
});

test("summaries cover running, completed, partial, failed, and skipped states", () => {
  const runningEvents = [normalizeAgentPayload("tool_call", { tool: "RawRAGTool", action: "search" }, 1, 1000)];
  const runningSteps = buildAgentTimeline(runningEvents);
  assert.equal(deriveAgentRunSummary(runningEvents, runningSteps, false).status, "running");

  const completedEvents = [
    normalizeAgentPayload("agent_trace", { stage: "AnalyzeQuestion", status: "skipped" }, 1, 1000),
    normalizeAgentPayload("citation_verification", { valid: true, verified_chunks: ["c1"] }, 2, 1100),
  ];
  const completedSteps = buildAgentTimeline(completedEvents);
  assert.equal(deriveAgentRunSummary(completedEvents, completedSteps, true).status, "completed");

  const partialEvents = [normalizeAgentPayload("evidence_summary", { sufficient: false, sufficiency_reason: "证据不足" }, 1, 1000)];
  const partialSteps = buildAgentTimeline(partialEvents);
  assert.equal(deriveAgentRunSummary(partialEvents, partialSteps, true).status, "partial");

  const failedEvents = [normalizeAgentPayload("citation_verification", { valid: false, invalid_chunks: ["bad"] }, 1, 1000)];
  const failedSteps = buildAgentTimeline(failedEvents);
  assert.equal(deriveAgentRunSummary(failedEvents, failedSteps, true).status, "failed");
});

test("agent complete closes stale running timeline steps", () => {
  const events = [
    normalizeAgentPayload("agent_trace", { stage: "AgentRound", status: "running" }, 1, 1000),
    normalizeAgentPayload("agent_trace", { stage: "ReturnAnswer", status: "completed" }, 2, 1400),
    normalizeAgentPayload("agent_complete", { summary: "complete" }, 3, 1500),
  ];

  const steps = buildAgentTimeline(events);
  const summary = deriveAgentRunSummary(events, steps, true);

  assert.equal(steps.find((step) => step.title === "选择检索步骤")?.status, "completed");
  assert.equal(summary.status, "completed");
});

test("new timeline activity closes previous non-tool running steps while streaming", () => {
  const events = [
    normalizeAgentPayload("agent_query", { status: "running", summary: "query" }, 1, 1000),
    normalizeAgentPayload("agent_thought", { status: "running", phase: "initial_scan", summary: "thinking" }, 2, 1100),
    normalizeAgentPayload("agent_trace", { stage: "AgentRuntimeStart", status: "running", summary: "start" }, 3, 1200),
    normalizeAgentPayload("agent_trace", { stage: "AgentRound", status: "running", summary: "round" }, 4, 1300),
    normalizeAgentPayload("agent_tool_call", { tool: "grep_chunks", call_id: "g1", input_summary: "grep_chunks: 48*SFP+ | 40G QSFP+ | 4.8T | VLAN:4K" }, 5, 1400),
  ];

  const steps = buildAgentTimeline(events);
  const nonToolRunning = steps.filter((step) => step.kind !== "tool" && step.status === "running");
  const toolStep = steps.find((step) => step.kind === "tool");

  assert.equal(nonToolRunning.length, 0);
  assert.equal(toolStep?.status, "running");
  assert.equal(toolStep?.title, "搜索关键词：48*SFP+ | 40G QSFP+ | 4.8T | VLAN:4K");
});

test("closed running steps do not keep stale running detail text", () => {
  const events = [
    normalizeAgentPayload(
      "agent_thought",
      { status: "running", phase: "llm_decision", summary: "thinking", completion_status: "running" },
      1,
      1000,
    ),
    normalizeAgentPayload("agent_tool_call", { tool: "wiki_search", call_id: "w1", input_summary: "security" }, 2, 1100),
  ];

  const steps = buildAgentTimeline(events);
  const thought = steps.find((step) => step.kind === "thought");

  assert.equal(thought?.status, "completed");
  assert.doesNotMatch(thought?.detail || "", /\u8fdb\u884c\u4e2d|running/i);
});

test("batched tools complete by call id even when physical results arrive out of order", () => {
  const events = [
    normalizeAgentPayload(
      "agent_thought",
      {
        status: "running",
        phase: "llm_decision",
        summary: "Analyzing the question and selecting the next action.",
        metadata: { source: "runtime_phase" },
      },
      1,
      1000,
    ),
    normalizeAgentPayload(
      "agent_tool_call",
      { tool: "grep_chunks", call_id: "g1", metadata: { batch_id: "b1", call_index: 1 } },
      2,
      1100,
    ),
    normalizeAgentPayload(
      "agent_tool_call",
      { tool: "knowledge_search", call_id: "s1", metadata: { batch_id: "b1", call_index: 2 } },
      3,
      1110,
    ),
    normalizeAgentPayload(
      "agent_tool_result",
      { tool: "knowledge_search", call_id: "s1", output_summary: "semantic done", metadata: { batch_id: "b1", call_index: 2 } },
      4,
      1160,
    ),
    normalizeAgentPayload(
      "agent_tool_result",
      { tool: "grep_chunks", call_id: "g1", output_summary: "grep done", metadata: { batch_id: "b1", call_index: 1 } },
      5,
      1200,
    ),
    normalizeAgentPayload("agent_complete", { summary: "complete" }, 6, 1250),
  ];

  const steps = buildAgentTimeline(events);
  const tools = steps.filter((step) => step.kind === "tool");

  assert.deepEqual(tools.map((step) => step.tool), ["grep_chunks", "knowledge_search"]);
  assert.deepEqual(tools.map((step) => step.status), ["completed", "completed"]);
  assert.equal(steps.some((step) => step.status === "running"), false);
  assert.equal(events[0].metadata.metadata.source, "runtime_phase");
});

test("visible labels are user-facing and do not include private internal strings", () => {
  assert.equal(stageLabel("PlanRetrieval"), "规划检索");
  assert.equal(stageLabel("AnalyzeQuestion"), "已完成问题理解");
  assert.equal(stageLabel("UnderstandQuestion"), "理解问题");
  assert.equal(stageLabel("RetrieveKnowledgeBase"), "检索知识库");
  assert.equal(stageLabel("ReadEvidence"), "引用文档");
  assert.equal(stageLabel("SynthesizeAnswer"), "组织答案");
  assert.equal(stageLabel("Complete"), "完成");
  assert.equal(toolLabel("thinking"), "记录证据判断");
  assert.equal(toolLabel("RawRAGTool"), "检索知识库");
  assert.equal(toolLabel("KeywordSearchTool"), "搜索关键词");
  assert.equal(toolLabel("GraphRetrieverTool"), "查询图谱证据");

  const componentSource = readFileSync(new URL("../components/AgentTimeline.tsx", import.meta.url), "utf8");
  assert.match(componentSource, /可审计执行摘要/);
  assert.doesNotMatch(componentSource, /chain_of_thought|scratchpad|private_reasoning|raw_prompt|memory_context/);
});

test("quick rag stage summary does not imply missing tool calls", () => {
  const events = [
    normalizeAgentPayload("agent_trace", { stage: "UnderstandQuestion", status: "completed" }, 1, 1000),
    normalizeAgentPayload("agent_trace", { stage: "RetrieveKnowledgeBase", status: "completed" }, 2, 1100),
    normalizeAgentPayload("agent_trace", { stage: "ReadEvidence", status: "completed" }, 3, 1200),
    normalizeAgentPayload("agent_trace", { stage: "SynthesizeAnswer", status: "completed" }, 4, 1300),
    normalizeAgentPayload("agent_trace", { stage: "Complete", status: "completed" }, 5, 1400),
  ];
  const steps = buildAgentTimeline(events);
  const summary = deriveAgentRunSummary(events, steps, true);
  const componentSource = readFileSync(new URL("../components/AgentTimeline.tsx", import.meta.url), "utf8");

  assert.equal(summary.toolCalls, 0);
  assert.equal(summary.status, "completed");
  assert.match(componentSource, /快速检索/);
});

test("derives search summary for raw rag and agentic messages", () => {
  const rawMessage = {
    role: "assistant",
    content: "answer",
    sources: [
      { source: "manual.md", score: 0.9 },
      { source: "manual.md#chunk-2", score: 0.8 },
      { source: "ops.md", score: 0.7 },
    ],
  };
  assert.equal(countUniqueSourceDocuments(rawMessage.sources), 2);
  assert.deepEqual(deriveSearchSummary(rawMessage, false).label, "检索完成 · 引用了 2 篇文档");

  const agentMessage = {
    role: "assistant",
    content: "无法确定",
    sources: [],
    agentEvents: [normalizeAgentPayload("evidence_summary", { sufficient: false }, 1, 1000)],
    evidenceSummary: { sufficient: false },
  };
  assert.equal(deriveSearchSummary(agentMessage, false).status, "insufficient");

  const failedCitation = {
    role: "assistant",
    content: "answer",
    citationVerification: { valid: false, invalid_chunks: ["missing"] },
  };
  assert.equal(deriveSearchSummary(failedCitation, false).label, "引用校验失败");
});

test("chat page renders markdown code blocks with copy affordance", () => {
  const chatPageSource = readFileSync(new URL("../chat/page.tsx", import.meta.url), "utf8");
  assert.match(chatPageSource, /function CodeBlock/);
  assert.match(chatPageSource, /复制代码/);
  assert.match(chatPageSource, /languageLabel/);
  assert.match(chatPageSource, /navigator\.clipboard/);
});
