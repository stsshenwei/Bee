import type {
  AgentEventKind,
  AgentEventStatus,
  AgentRunSummary,
  AgentStreamEvent,
  AgentTimelineStep,
  ChatMessage,
  SourceItem,
} from "./types";

const PRIVATE_KEYS = new Set(["chain_of_thought", "scratchpad", "private_reasoning", "raw_prompt", "memory_context", "raw_tool_payload"]);

const STAGE_LABELS: Record<string, string> = {
  AgentRuntimeStart: "开始检索准备",
  AgentRound: "选择检索步骤",
  RequireDeepRead: "阅读全文",
  AnalyzeQuestion: "已完成问题理解",
  PlanRetrieval: "规划检索",
  CheckPermissionScope: "检查权限范围",
  RunRetrieval: "运行检索工具",
  FuseEvidence: "整理证据",
  RerankEvidence: "重排证据",
  NeedMoreEvidence: "检查证据是否充分",
  BuildContext: "整理答案",
  GenerateAnswer: "生成回答",
  VerifyCitations: "校验引用",
  ReturnAnswer: "完成",
  UnderstandQuestion: "理解问题",
  RetrieveKnowledgeBase: "检索知识库",
  ReadEvidence: "引用文档",
  SynthesizeAnswer: "组织答案",
  Complete: "完成",
};

const DOMAIN_EVENT_LABELS: Record<string, string> = {
  agent_query: "理解问题",
  agent_thought: "证据检查",
  agent_reflection: "反思验证",
  agent_remedial_search: "补救检索",
  agent_references: "引用来源",
  agent_final_answer: "生成回答",
  agent_complete: "完成",
  agent_error: "执行失败",
};

const TOOL_LABELS: Record<string, string> = {
  thinking: "记录证据判断",
  todo_write: "记录执行计划",
  knowledge_search: "按含义查找资料",
  grep_chunks: "搜索关键词",
  list_knowledge_chunks: "查看文档",
  get_document_info: "查看文档信息",
  query_knowledge_graph: "查询知识图谱",
  wiki_search: "搜索 Wiki",
  wiki_read_page: "阅读 Wiki 页面",
  wiki_read_source_doc: "读取 Wiki 来源",
  read_skill: "读取技能说明",
  RawRAGTool: "检索知识库",
  KeywordSearchTool: "搜索关键词",
  GraphRetrieverTool: "查询图谱证据",
  DocumentChunkReaderTool: "查看文档",
  EvidenceFusionTool: "整理证据",
  CitationVerifierTool: "校验引用",
};

export type SearchSummaryStatus = "searching" | "completed" | "insufficient" | "citation_failed" | "empty";

export type SearchSummaryState = {
  status: SearchSummaryStatus;
  label: string;
  citedDocumentCount: number;
  sourceCount: number;
  insufficient: boolean;
  citationFailed: boolean;
};

export function scrubPrivateFields(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => scrubPrivateFields(item));
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  const clean: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (PRIVATE_KEYS.has(key)) continue;
    clean[key] = scrubPrivateFields(item);
  }
  return clean;
}

export function normalizeAgentPayload(
  kind:
    | "agent_trace"
    | "tool_call"
    | "tool_observation"
    | "agent_query"
    | "agent_thought"
    | "agent_tool_call"
    | "agent_tool_result"
    | "agent_reflection"
    | "agent_remedial_search"
    | "agent_references"
    | "agent_final_answer"
    | "agent_complete"
    | "agent_error"
    | "evidence_summary"
    | "citation_verification",
  payload: Record<string, unknown>,
  sequence: number,
  timestamp = Date.now(),
): AgentStreamEvent {
  if (kind === "agent_trace") return normalizeAgentTrace(payload, sequence, timestamp);
  if (kind === "agent_tool_result") return normalizeToolObservation(payload, sequence, timestamp, "agent_tool_result");
  if (kind === "agent_tool_call") return normalizeToolCall(payload, sequence, timestamp, "agent_tool_call");
  if (kind.startsWith("agent_")) return normalizeDomainEvent(kind as AgentEventKind, payload, sequence, timestamp);
  if (kind === "tool_observation") return normalizeToolObservation(payload, sequence, timestamp);
  if (kind === "tool_call") return normalizeToolCall(payload, sequence, timestamp);
  if (kind === "evidence_summary") return normalizeEvidenceSummary(payload, sequence, timestamp);
  if (kind === "citation_verification") return normalizeCitationVerification(payload, sequence, timestamp);
  return normalizeAgentTrace(payload, sequence, timestamp);
}

export function buildAgentTimeline(events: AgentStreamEvent[]): AgentTimelineStep[] {
  const steps: AgentTimelineStep[] = [];
  const pendingTools: Array<{ key: string; step: AgentTimelineStep }> = [];

  for (const event of dedupeLegacyToolEvents(events)) {
    if (event.kind === "tool_call" || event.kind === "agent_tool_call") {
      closeStaleRunningStageSteps(steps, event.timestamp);
      const key = toolPairKey(event);
      const step: AgentTimelineStep = {
        id: event.id,
        kind: "tool",
        title: toolCallTitle(event),
        status: "running",
        summary: event.inputSummary || "正在检索相关证据",
        tool: event.tool,
        action: event.action,
        startedAt: event.timestamp,
        sourceChunkIds: [],
        sourceTitles: event.sourceTitles,
        counts: event.counts,
      };
      pendingTools.push({ key, step });
      steps.push(step);
      continue;
    }

    if (event.kind === "tool_result" || event.kind === "agent_tool_result") {
      const key = toolPairKey(event);
      const matchIndex = pendingTools.findIndex((item) => item.key === key);
      const matched = matchIndex >= 0 ? pendingTools.splice(matchIndex, 1)[0]?.step : undefined;
      if (matched) {
        matched.status = event.status || "completed";
        matched.title = toolResultTitle(event, matched.title);
        matched.summary = toolObservationSummary(event);
        matched.detail = toolResultDetail(event);
        matched.finishedAt = event.timestamp;
        matched.elapsedMs = Math.max(0, event.timestamp - matched.startedAt);
        matched.sourceChunkIds = event.sourceChunkIds;
        matched.sourceTitles = event.sourceTitles;
        matched.counts = event.counts;
        continue;
      }
      steps.push(eventToTimelineStep(event));
      continue;
    }

    if (event.kind === "agent_complete" && (event.status || "completed") === "completed") {
      closeRunningSteps(steps, event.timestamp);
      pendingTools.length = 0;
    }

    closeStaleRunningStageSteps(steps, event.timestamp);
    const step = eventToTimelineStep(event);
    if (step.title === "完成" && steps.some((item) => item.title === "完成")) {
      continue;
    }
    steps.push(step);
  }

  return steps;
}

function closeStaleRunningStageSteps(steps: AgentTimelineStep[], timestamp: number): void {
  for (const step of steps) {
    if (step.status !== "running") continue;
    if (step.kind === "tool") continue;
    completeRunningStep(step, timestamp);
  }
}

function closeRunningSteps(steps: AgentTimelineStep[], timestamp: number): void {
  for (const step of steps) {
    if (step.status !== "running") continue;
    completeRunningStep(step, timestamp);
  }
}

function completeRunningStep(step: AgentTimelineStep, timestamp: number): void {
  step.status = "completed";
  step.finishedAt = timestamp;
  step.elapsedMs = Math.max(0, timestamp - step.startedAt);
  step.summary = completedStatusText(step.summary);
  step.detail = completedStatusText(step.detail);
}

function completedStatusText(value?: string): string | undefined {
  if (!value) return value;
  return value
    .replace(/\u72b6\u6001\s*[:\uff1a]\s*\u8fdb\u884c\u4e2d/g, "\u72b6\u6001\uff1a\u5b8c\u6210")
    .replace(/\bstatus\s*[:：]\s*running\b/gi, "status: completed")
    .replace(/\bcompletion_status\s*[:：]\s*running\b/gi, "completion_status: completed");
}

export function deriveAgentRunSummary(events: AgentStreamEvent[], steps: AgentTimelineStep[], completed: boolean): AgentRunSummary {
  const visibleEvents = dedupeLegacyToolEvents(events);
  const first = visibleEvents[0]?.timestamp ?? Date.now();
  const last = visibleEvents[visibleEvents.length - 1]?.timestamp ?? first;
  const elapsedMs = Math.max(0, last - first);
  const failed = steps.find((step) => step.status === "failed");
  const partial = steps.find((step) => step.status === "partial");
  const running = steps.find((step) => step.status === "running");
  const citation = [...visibleEvents].reverse().find((event) => event.kind === "citation_verification");
  const evidence = [...visibleEvents].reverse().find((event) => event.kind === "evidence_summary");
  const completedSteps = steps.filter((step) => ["completed", "skipped"].includes(step.status)).length;
  const toolCalls = visibleEvents.filter((event) => event.kind === "tool_call" || event.kind === "agent_tool_call").length;
  const references = [...visibleEvents].reverse().find((event) => event.kind === "agent_references");
  const complete = [...visibleEvents].reverse().find((event) => event.kind === "agent_complete");
  const rounds = new Set(
    visibleEvents
      .map((event) => numberValue(asRecord(event.metadata.metadata).round ?? event.metadata.round))
      .filter((round): round is number => round !== undefined),
  );
  const status: AgentEventStatus = failed ? "failed" : partial ? "partial" : !completed || running ? "running" : "completed";

  return {
    status,
    completedSteps,
    totalSteps: steps.length,
    elapsedMs,
    evidenceCount: evidence?.counts?.evidenceItems,
    citationStatus: citation?.counts?.valid === undefined ? "unknown" : citation.counts.valid ? "passed" : "failed",
    failureSummary: failed?.summary || partial?.summary,
    reasoningRounds: Math.max(1, rounds.size),
    toolCalls,
    referencedDocuments: references?.counts?.citations,
    remedialUsed: booleanValue(complete?.metadata.remedial_used) || visibleEvents.some((event) => event.kind === "agent_remedial_search"),
  };
}

export function deriveSearchSummary(message: ChatMessage, streaming: boolean): SearchSummaryState {
  const citation = asRecord(message.citationVerification);
  const evidence = asRecord(message.evidenceSummary);
  const valid = booleanValue(citation.valid);
  const sufficient = booleanValue(evidence.sufficient);
  const invalidChunks = sourceChunkIds(citation.invalid_chunks);
  const citationFailed = valid === false || invalidChunks.length > 0;
  const insufficient = sufficient === false || hasInsufficientText(message.content) || hasInsufficientText(stringValue(evidence.sufficiency_reason));
  const citedDocumentCount = countUniqueSourceDocuments(message.sources || []);
  const sourceCount = message.sources?.length || 0;
  const hasAgentActivity = Boolean(message.agentEvents?.length);

  if (citationFailed) {
    return {
      status: "citation_failed",
      label: "引用校验失败",
      citedDocumentCount,
      sourceCount,
      insufficient,
      citationFailed,
    };
  }
  if (insufficient && !sourceCount) {
    return {
      status: "insufficient",
      label: "证据不足",
      citedDocumentCount,
      sourceCount,
      insufficient,
      citationFailed,
    };
  }
  if (sourceCount || valid === true) {
    const count = citedDocumentCount || sourceCount || numberValue(citation.verified_chunks_count) || 0;
    return {
      status: insufficient ? "insufficient" : "completed",
      label: insufficient ? `证据不足 · 已检索 ${count} 篇文档` : `检索完成 · 引用了 ${count} 篇文档`,
      citedDocumentCount: count,
      sourceCount,
      insufficient,
      citationFailed,
    };
  }
  if (streaming || (hasAgentActivity && !message.agentCompleted)) {
    return {
      status: "searching",
      label: "检索中...",
      citedDocumentCount,
      sourceCount,
      insufficient,
      citationFailed,
    };
  }
  return {
    status: insufficient ? "insufficient" : "empty",
    label: insufficient ? "证据不足" : "未找到可引用文档",
    citedDocumentCount,
    sourceCount,
    insufficient,
    citationFailed,
  };
}

export function countUniqueSourceDocuments(sources: SourceItem[]): number {
  return new Set(sources.map((source) => normalizeSourcePath(source.source)).filter(Boolean)).size;
}

export function stageLabel(stage?: string): string {
  if (!stage) return "Agent 步骤";
  return STAGE_LABELS[stage] || stage;
}

export function toolLabel(tool?: string): string {
  if (!tool) return "检索工具";
  return TOOL_LABELS[tool] || tool;
}

export function statusLabel(status: AgentEventStatus): string {
  if (status === "running") return "进行中";
  if (status === "completed") return "完成";
  if (status === "partial") return "部分完成";
  if (status === "failed") return "失败";
  if (status === "skipped") return "跳过";
  const labels: Record<AgentEventStatus, string> = {
    running: "执行中",
    completed: "完成",
    partial: "部分完成",
    failed: "失败",
    skipped: "跳过",
  };
  return labels[status];
}

export function formatElapsed(ms: number): string {
  if (ms < 1000) return `${Math.max(0, Math.round(ms))}ms`;
  return `${Math.round(ms / 1000)}s`;
}

function normalizeAgentTrace(payload: Record<string, unknown>, sequence: number, timestamp: number): AgentStreamEvent {
  const stage = stringValue(payload.stage);
  const metadata = asRecord(payload.metadata);
  return baseEvent("agent_trace", payload, sequence, timestamp, {
    stage,
    status: eventStatus(payload.status) || "completed",
    summary: publicSummary(stringValue(payload.summary)),
    sourceChunkIds: sourceChunkIds(payload.source_chunk_ids ?? metadata.source_chunk_ids),
  });
}

function normalizeDomainEvent(kind: AgentEventKind, payload: Record<string, unknown>, sequence: number, timestamp: number): AgentStreamEvent {
  const metadata = asRecord(payload.metadata);
  const items = Array.isArray(payload.items) ? payload.items : [];
  const sourceIds = sourceChunkIds(payload.source_chunk_ids ?? metadata.source_chunk_ids);
  const itemSources = items.flatMap((item) => {
    const record = asRecord(item);
    return stringList(record.source_title ?? record.title ?? record.source);
  });
  const sourceTitles = uniqueStrings([...stringList(metadata.source_titles ?? metadata.source), ...itemSources]);
  const citationCount = kind === "agent_references" ? sourceTitles.length || sourceIds.length : numberValue(payload.citation_count) ?? items.length;
  return baseEvent(kind, payload, sequence, timestamp, {
    stage: stringValue(payload.phase) || kind,
    status: eventStatus(payload.status) || "completed",
    summary: publicSummary(
      stringValue(payload.summary) ||
        stringValue(payload.gap) ||
        stringValue(payload.correction_query) ||
        stringValue(payload.answer),
    ),
    sourceChunkIds: sourceIds,
    sourceTitles,
    counts: {
      citations: citationCount,
      usedChunks: numberValue(payload.used_chunks) ?? sourceIds.length,
      toolCounts: numberRecord(payload.tool_counts),
    },
  });
}

function normalizeToolCall(
  payload: Record<string, unknown>,
  sequence: number,
  timestamp: number,
  kind: "tool_call" | "agent_tool_call" = "tool_call",
): AgentStreamEvent {
  const metadata = asRecord(payload.metadata);
  return baseEvent(kind, payload, sequence, timestamp, {
    tool: stringValue(payload.tool),
    action: stringValue(payload.action),
    status: "running",
    inputSummary: publicSummary(stringValue(payload.input_summary)),
    required: booleanValue(payload.required),
    limits: asRecord(payload.limits),
    sourceChunkIds: sourceChunkIds(metadata.source_chunk_ids),
    sourceTitles: stringList(metadata.source_titles ?? metadata.source),
  });
}

function normalizeToolObservation(
  payload: Record<string, unknown>,
  sequence: number,
  timestamp: number,
  kind: "tool_result" | "agent_tool_result" = "tool_result",
): AgentStreamEvent {
  const metadata = asRecord(payload.metadata);
  const chunks = sourceChunkIds(payload.source_chunk_ids);
  const listedQueries = stringList(metadata.queries ?? payload.queries);
  const queryCandidateCount =
    numberValue(metadata.query_count ?? metadata.query_candidates ?? payload.query_count ?? payload.query_candidates) ??
    (listedQueries.length || undefined);
  return baseEvent(kind, payload, sequence, timestamp, {
    tool: stringValue(payload.tool),
    action: stringValue(payload.action),
    status: eventStatus(payload.status) || "completed",
    summary: publicSummary(stringValue(payload.summary)),
    outputSummary: publicSummary(stringValue(payload.output_summary) || stringValue(payload.observation) || stringValue(payload.error)),
    sourceChunkIds: chunks,
    sourceTitles: stringList(metadata.source_titles ?? metadata.source),
    counts: {
      evidenceItems: numberValue(metadata.evidence_items ?? payload.evidence_items),
      citations: numberValue(metadata.citations ?? payload.citations),
      usedChunks: numberValue(metadata.used_chunks) ?? chunks.length,
      resultCount: numberValue(metadata.result_count ?? payload.result_count),
      queryCandidates: queryCandidateCount,
      docCount: numberValue(metadata.doc_count ?? payload.doc_count),
      matchedChunks: numberValue(metadata.matched_chunks ?? metadata.total_matches ?? payload.matched_chunks ?? payload.total_matches),
      readChunks: numberValue(metadata.chunk_count ?? metadata.fetched_chunks ?? payload.chunk_count ?? payload.fetched_chunks),
      requestedChunks: numberValue(metadata.requested_count ?? metadata.total_chunks ?? payload.requested_count ?? payload.total_chunks),
      entities: numberValue(metadata.entities ?? payload.entities),
      graphPaths: numberValue(metadata.graph_paths ?? payload.graph_paths),
    },
  });
}

function normalizeEvidenceSummary(payload: Record<string, unknown>, sequence: number, timestamp: number): AgentStreamEvent {
  return baseEvent("evidence_summary", payload, sequence, timestamp, {
    status: booleanValue(payload.sufficient) === false ? "partial" : "completed",
    summary: publicSummary(stringValue(payload.sufficiency_reason)),
    sourceChunkIds: sourceChunkIds(payload.source_chunk_ids),
    counts: {
      toolCounts: numberRecord(payload.tool_counts),
      evidenceItems: numberValue(payload.evidence_items),
      citations: numberValue(payload.citations),
      usedChunks: numberValue(payload.used_chunks),
      entities: numberValue(payload.used_entities),
      graphPaths: numberValue(payload.graph_paths),
      confidence: numberValue(payload.confidence),
      sufficient: booleanValue(payload.sufficient),
    },
  });
}

function normalizeCitationVerification(payload: Record<string, unknown>, sequence: number, timestamp: number): AgentStreamEvent {
  const valid = booleanValue(payload.valid);
  const verified = sourceChunkIds(payload.verified_chunks);
  const invalid = sourceChunkIds(payload.invalid_chunks);
  return baseEvent("citation_verification", payload, sequence, timestamp, {
    status: valid === false ? "failed" : "completed",
    summary: publicSummary(stringValue(payload.summary)),
    sourceChunkIds: verified,
    counts: {
      valid,
      verifiedChunks: verified.length,
      invalidChunks: invalid.length,
    },
  });
}

function baseEvent(
  kind: AgentEventKind,
  payload: Record<string, unknown>,
  sequence: number,
  timestamp: number,
  fields: Partial<AgentStreamEvent>,
): AgentStreamEvent {
  const cleanMetadata = (scrubPrivateFields(payload) || {}) as Record<string, unknown>;
  return {
    id: stringValue(payload.event_id) || `${kind}-${sequence}`,
    kind,
    timestamp: numberValue(payload.created_at) || timestamp,
    sequence,
    sourceChunkIds: [],
    metadata: cleanMetadata,
    ...fields,
  };
}

function eventToTimelineStep(event: AgentStreamEvent): AgentTimelineStep {
  if (
    event.kind === "agent_query" ||
    event.kind === "agent_thought" ||
    event.kind === "agent_reflection" ||
    event.kind === "agent_remedial_search" ||
    event.kind === "agent_references" ||
    event.kind === "agent_final_answer" ||
    event.kind === "agent_complete" ||
    event.kind === "agent_error"
  ) {
    return domainEventToTimelineStep(event);
  }
  if (event.kind === "evidence_summary") {
    return {
      id: event.id,
      kind: "evidence",
      title: "整理证据",
      status: event.status || "completed",
      summary: evidenceDetail(event),
      detail: event.summary,
      startedAt: event.timestamp,
      finishedAt: event.timestamp,
      sourceChunkIds: event.sourceChunkIds,
      sourceTitles: event.sourceTitles,
      counts: event.counts,
    };
  }
  if (event.kind === "citation_verification") {
    return {
      id: event.id,
      kind: "citation",
      title: event.status === "failed" ? "引用校验失败" : "校验引用",
      status: event.status || "completed",
      summary: citationDetail(event),
      detail: event.summary,
      startedAt: event.timestamp,
      finishedAt: event.timestamp,
      sourceChunkIds: event.sourceChunkIds,
      sourceTitles: event.sourceTitles,
      counts: event.counts,
    };
  }
  if (event.kind === "tool_result" || event.kind === "agent_tool_result") {
    return {
      id: event.id,
      kind: "tool",
      title: `${toolLabel(event.tool)}结果`,
      status: event.status || "completed",
      summary: toolObservationSummary(event),
      detail: toolResultDetail(event),
      tool: event.tool,
      action: event.action,
      startedAt: event.timestamp,
      finishedAt: event.timestamp,
      sourceChunkIds: event.sourceChunkIds,
      sourceTitles: event.sourceTitles,
      counts: event.counts,
    };
  }
  return {
    id: event.id,
    kind: "stage",
    title: stageLabel(event.stage),
    status: event.status || "completed",
    summary: event.summary,
    startedAt: event.timestamp,
    finishedAt: event.status === "running" ? undefined : event.timestamp,
    sourceChunkIds: event.sourceChunkIds,
    sourceTitles: event.sourceTitles,
    counts: event.counts,
  };
}

function domainEventToTimelineStep(event: AgentStreamEvent): AgentTimelineStep {
  const stepKind =
    event.kind === "agent_thought"
      ? "thought"
      : event.kind === "agent_reflection" || event.kind === "agent_remedial_search"
        ? "reflection"
        : event.kind === "agent_references"
          ? "references"
          : event.kind === "agent_final_answer"
            ? "answer"
            : event.kind === "agent_complete"
              ? "complete"
              : event.kind === "agent_error"
                ? "error"
                : "stage";
  return {
    id: event.id,
    kind: stepKind,
    title: DOMAIN_EVENT_LABELS[event.kind] || stageLabel(event.stage),
    status: event.status || "completed",
    summary: domainEventSummary(event),
    detail: domainEventDetail(event),
    startedAt: event.timestamp,
    finishedAt: event.status === "running" ? undefined : event.timestamp,
    sourceChunkIds: event.sourceChunkIds,
    sourceTitles: event.sourceTitles,
    counts: event.counts,
  };
}

function toolCallTitle(event: AgentStreamEvent): string {
  const query = extractQuery(event.inputSummary || event.summary);
  if (event.tool === "RawRAGTool") return query ? `检索知识库：${query}` : "检索知识库";
  if (event.tool === "KeywordSearchTool" || event.tool === "grep_chunks") return query ? `搜索关键词：${query}` : "搜索关键词";
  if (event.tool === "knowledge_search") return query ? `语义检索：${query}` : "语义检索";
  if (event.tool === "wiki_search") return query ? `搜索 Wiki：${query}` : "搜索 Wiki";
  if (event.tool === "GraphRetrieverTool") return query ? `查询图谱证据：${query}` : "查询图谱证据";
  if (event.tool === "DocumentChunkReaderTool" || event.tool === "list_knowledge_chunks") return query ? `查看 ${query}` : "查看文档";
  return toolLabel(event.tool);
}

function toolResultTitle(event: AgentStreamEvent, fallback: string): string {
  if (event.tool === "grep_chunks" || event.tool === "KeywordSearchTool") {
    const query = toolQuery(event);
    return query ? `搜索关键词：${query}` : fallback || "搜索关键词";
  }
  if (event.tool === "knowledge_search" || event.tool === "RawRAGTool") {
    const query = toolQuery(event);
    return query ? `语义检索：${query}` : fallback;
  }
  if (event.tool === "wiki_search") return "搜索 Wiki";
  if (event.tool === "list_knowledge_chunks" || event.tool === "DocumentChunkReaderTool") {
    const title = firstVisibleSourceTitle(event.sourceTitles);
    return title ? `查看 ${title}` : fallback;
  }
  return fallback;
}

function domainEventSummary(event: AgentStreamEvent): string | undefined {
  const metadata = asRecord(event.metadata);
  if (event.kind === "agent_reflection") {
    const gap = stringValue(metadata.gap);
    const correction = stringValue(metadata.correction_query);
    if (gap && correction) return `${gap}；补充检索：${correction}`;
    return gap || event.summary;
  }
  if (event.kind === "agent_remedial_search") {
    const correction = stringValue(metadata.correction_query);
    return correction ? `根据证据缺口补充检索：${correction}` : event.summary;
  }
  if (event.kind === "agent_references") {
    const count = event.sourceTitles?.length || event.counts?.citations || event.sourceChunkIds.length;
    return count ? `已准备 ${count} 个引用来源` : event.summary;
  }
  if (event.kind === "agent_complete") {
    const remedial = booleanValue(metadata.remedial_used) ? "，包含补救检索" : "";
    return event.summary ? `${event.summary}${remedial}` : `执行完成${remedial}`;
  }
  return event.summary;
}

function domainEventDetail(event: AgentStreamEvent): string | undefined {
  const metadata = asRecord(event.metadata);
  const parts = [
    stringValue(metadata.validity) ? `验证：${localizeStatusText(stringValue(metadata.validity))}` : "",
    stringValue(metadata.completion_status) ? `状态：${localizeStatusText(stringValue(metadata.completion_status))}` : "",
    stringValue(metadata.correction_query) ? `补充检索：${stringValue(metadata.correction_query)}` : "",
    booleanValue(metadata.remedial_used) ? "已进行补充检索" : "",
  ].filter(Boolean);
  return parts.join("；") || undefined;
}

function toolObservationSummary(event: AgentStreamEvent): string {
  if (event.tool === "wiki_search") {
    const pages = event.counts?.resultCount ?? countVisibleSourceTitles(event.sourceTitles);
    return pages > 0 ? `命中 ${pages} 个 Wiki 页面` : "未命中 Wiki 页面";
  }
  if (event.tool === "grep_chunks" || event.tool === "KeywordSearchTool") {
    const chunks = event.counts?.matchedChunks ?? event.counts?.resultCount ?? event.sourceChunkIds.length;
    const docs = event.counts?.docCount ?? countVisibleSourceTitles(event.sourceTitles);
    if (chunks > 0) {
      return docs > 0 ? `找到 ${chunks} 个匹配片段，来自 ${docs} 个文档` : `找到 ${chunks} 个匹配片段`;
    }
    return "未找到关键词匹配";
  }
  if (event.tool === "list_knowledge_chunks" || event.tool === "DocumentChunkReaderTool") {
    const read = event.counts?.readChunks ?? event.counts?.usedChunks ?? event.sourceChunkIds.length;
    const requested = event.counts?.requestedChunks;
    if (read > 0) {
      return requested && requested >= read ? `已加载 ${read} / ${requested} 个分块` : `已加载 ${read} 个分块`;
    }
    return "未加载到可用分块";
  }
  const resultCount = event.counts?.evidenceItems ?? event.counts?.usedChunks ?? event.sourceChunkIds.length;
  if (resultCount !== undefined && resultCount > 0) {
    return `找到 ${resultCount} 条相关内容`;
  }
  return event.outputSummary || event.summary || "未找到可用内容";
}

function toolResultDetail(event: AgentStreamEvent): string | undefined {
  const counts = event.counts || {};
  if (event.tool === "wiki_search") {
    const pages = counts.resultCount ?? countVisibleSourceTitles(event.sourceTitles);
    const parts = [
      countText(pages, "个 Wiki 页面"),
      countText(counts.queryCandidates, "个关键词"),
    ].filter(Boolean);
    return parts.join("，");
  }
  const parts = [
    countText(counts.evidenceItems, "条证据"),
    countText(counts.citations, "条引用"),
    counts.evidenceItems === undefined ? countText(counts.usedChunks, "条相关内容") : "",
    countText(counts.matchedChunks ?? counts.resultCount, "个匹配片段"),
    countText(counts.docCount, "个文档"),
    countText(counts.readChunks, "个已加载分块"),
    countText(counts.entities, "个实体"),
    countText(counts.graphPaths, "条图谱路径"),
  ].filter(Boolean);
  return parts.join("，");
}

function evidenceDetail(event: AgentStreamEvent): string {
  const counts = event.counts || {};
  const parts = [
    countText(counts.evidenceItems, "条证据"),
    counts.evidenceItems === undefined ? countText(counts.usedChunks, "条相关内容") : "",
    countText(counts.graphPaths, "条图谱路径"),
    counts.confidence !== undefined ? `置信度 ${counts.confidence.toFixed(2)}` : "",
    counts.sufficient === false ? "证据不足" : counts.sufficient === true ? "证据充分" : "",
  ].filter(Boolean);
  return parts.join("，");
}

function citationDetail(event: AgentStreamEvent): string {
  const counts = event.counts || {};
  if (counts.valid === false) {
    return `引用校验失败：${counts.invalidChunks ?? 0} 条内容无法追溯`;
  }
  if (counts.valid === true) {
    return `引用校验通过：${counts.verifiedChunks ?? 0} 条内容可追溯`;
  }
  return "引用校验完成";
}

function countText(value: number | undefined, unit: string): string {
  return value === undefined ? "" : `${value} ${unit}`;
}

function localizeStatusText(value?: string): string {
  const normalized = (value || "").trim().toLowerCase();
  const labels: Record<string, string> = {
    running: "进行中",
    completed: "完成",
    complete: "完成",
    sufficient: "证据充分",
    needs_more_evidence: "需要更多证据",
    insufficient: "证据不足",
    failed: "失败",
    partial: "部分完成",
    skipped: "跳过",
  };
  return labels[normalized] || value || "";
}

function toolPairKey(event: AgentStreamEvent): string {
  const callId = stringValue(event.metadata.call_id) || stringValue(asRecord(event.metadata.metadata).call_id);
  return callId || `${event.tool || ""}:${event.action || ""}`;
}

function dedupeLegacyToolEvents(events: AgentStreamEvent[]): AgentStreamEvent[] {
  const domainToolKeys = new Set(
    events
      .filter((event) => event.kind === "agent_tool_call" || event.kind === "agent_tool_result")
      .map(toolCompatibilityKey)
      .filter((key): key is string => Boolean(key)),
  );
  if (!domainToolKeys.size) return events;
  return events.filter((event) => {
    if (event.kind !== "tool_call" && event.kind !== "tool_result") return true;
    const key = toolCompatibilityKey(event);
    return !key || !domainToolKeys.has(key);
  });
}

function toolCompatibilityKey(event: AgentStreamEvent): string | undefined {
  const callId = stringValue(event.metadata.call_id) || stringValue(asRecord(event.metadata.metadata).call_id);
  if (!callId) return undefined;
  const phase = event.kind === "agent_tool_call" || event.kind === "tool_call" ? "call" : "result";
  return `${phase}:${callId}`;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function stringValue(value: unknown): string | undefined {
  if (typeof value === "string" && value.trim()) return value;
  return undefined;
}

function numberValue(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  return undefined;
}

function booleanValue(value: unknown): boolean | undefined {
  if (typeof value === "boolean") return value;
  if (value === "true") return true;
  if (value === "false") return false;
  return undefined;
}

function eventStatus(value: unknown): AgentEventStatus | undefined {
  if (value === "completed" || value === "partial" || value === "failed" || value === "skipped" || value === "running") {
    return value;
  }
  return undefined;
}

function sourceChunkIds(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return Array.from(new Set(value.map((item) => String(item)).filter(Boolean)));
}

function stringList(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => stringValue(item)).filter((item): item is string => Boolean(item));
  const single = stringValue(value);
  return single ? [single] : [];
}

function uniqueStrings(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}

function numberRecord(value: unknown): Record<string, number> | undefined {
  const record = asRecord(value);
  const entries = Object.entries(record)
    .map(([key, item]) => [key, numberValue(item)] as const)
    .filter((entry): entry is readonly [string, number] => entry[1] !== undefined);
  return entries.length ? Object.fromEntries(entries) : undefined;
}

function normalizeSourcePath(source: string): string {
  return source.split(" 路径")[0].split("#")[0].trim();
}

function hasInsufficientText(value?: string): boolean {
  if (!value) return false;
  return /无法确定|证据不足|没有足够|不能确定|无法从.*文档/.test(value);
}

function publicSummary(value?: string): string | undefined {
  if (!value) return undefined;
  const clean = value
    .replace(/chain_of_thought|scratchpad|private_reasoning|raw_prompt|memory_context/gi, "")
    .trim();
  return localizePublicSummary(clean);
}

function localizePublicSummary(value: string): string {
  const labels: Record<string, string> = {
    "Received user question.": "已收到用户问题。",
    "Understanding the question and preparing knowledge-base retrieval.": "正在理解问题，并准备检索知识库。",
    "Analyzing the question and selecting the next action.": "正在分析问题并选择下一步。",
    "Analyzing the question.": "正在分析问题。",
  };
  return labels[value] || value;
}

function extractQuery(value?: string): string | undefined {
  if (!value) return undefined;
  const quoted = value.match(/[“"']([^“”"']{2,320})[”"']/);
  if (quoted?.[1]) return quoted[1].trim();
  const colon = value.match(/^[^:：]{1,80}[:：]\s*(.{2,360})$/);
  if (colon?.[1]) return colon[1].trim();
  return undefined;
}

function toolQuery(event: AgentStreamEvent): string | undefined {
  const metadata = asRecord(event.metadata);
  return (
    stringValue(metadata.query) ||
    extractQuery(event.inputSummary || event.summary || event.outputSummary)
  );
}

function firstVisibleSourceTitle(values?: string[]): string | undefined {
  return values?.map((value) => normalizeSourcePath(value)).find((value) => value && !isOpaqueInternalId(value));
}

function countVisibleSourceTitles(values?: string[]): number {
  return uniqueStrings((values || []).map((value) => normalizeSourcePath(value)).filter((value) => value && !isOpaqueInternalId(value))).length;
}

function isOpaqueInternalId(value: string): boolean {
  const normalized = value.trim();
  if (!normalized || normalized.toLowerCase() === "unknown") return true;
  if (/^[a-f0-9]{24,}$/i.test(normalized)) return true;
  if (/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(normalized)) return true;
  return false;
}
