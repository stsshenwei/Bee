import { buildAgentTimeline, deriveAgentRunSummary, normalizeAgentPayload } from "./agent-stream";
import type { AgentStreamEvent, ChatHistoryMessage, ChatMessage, ReasoningSummary, SourceItem } from "./types";

type ChatMode = "quick" | "reasoning" | "wiki" | "rag_wiki";
type RestorableAgentEventKind =
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
  | "citation_verification";

const RESTORABLE_AGENT_EVENT_KINDS = new Set<string>([
  "agent_trace",
  "tool_call",
  "tool_observation",
  "agent_query",
  "agent_thought",
  "agent_tool_call",
  "agent_tool_result",
  "agent_reflection",
  "agent_remedial_search",
  "agent_references",
  "agent_final_answer",
  "agent_complete",
  "agent_error",
  "evidence_summary",
  "citation_verification",
]);

export function historyMessageToChatMessage(item: ChatHistoryMessage): ChatMessage {
  const metadata = item.metadata_json || {};
  const metadataChatMode = String(metadata.chat_mode || "");
  const chatModeValue: ChatMode =
    metadataChatMode === "reasoning" || metadataChatMode === "wiki" || metadataChatMode === "rag_wiki"
      ? metadataChatMode
      : "quick";
  const agentEvents = restoreAgentEvents(metadata.agent_events, item.created_at);
  const agentTimeline = agentEvents.length ? buildAgentTimeline(agentEvents) : undefined;
  return {
    id: item.id,
    session_id: item.session_id,
    conversation_id: item.conversation_id,
    request_id: item.request_id,
    assistant_message_id: item.role === "assistant" ? item.id : undefined,
    role: item.role,
    content: item.content,
    chatMode: chatModeValue,
    sources: Array.isArray(metadata.sources) ? metadata.sources as SourceItem[] : undefined,
    reasoning: isRecord(metadata.reasoning) ? metadata.reasoning as ReasoningSummary : undefined,
    agentEvents: agentEvents.length ? agentEvents : undefined,
    agentTimeline,
    agentSummary: agentEvents.length ? deriveAgentRunSummary(agentEvents, agentTimeline || [], item.is_completed) : undefined,
    evidenceSummary: isRecord(metadata.evidence_summary) ? metadata.evidence_summary : undefined,
    citationVerification: isRecord(metadata.citation_verification) ? metadata.citation_verification : undefined,
    is_completed: item.is_completed,
    agentCompleted: item.role === "assistant" ? item.is_completed : undefined,
    stopped: Boolean(metadata.stopped),
    created_at: item.created_at,
    updated_at: item.updated_at,
  };
}

export function mergeUniqueMessages(current: ChatMessage[], incoming: ChatMessage[], prepend = false): ChatMessage[] {
  const seen = new Set(current.map((item) => item.id).filter(Boolean) as string[]);
  const filtered = incoming.filter((item) => !item.id || !seen.has(item.id));
  return prepend ? [...filtered, ...current] : [...current, ...filtered];
}

function restoreAgentEvents(value: unknown, fallbackTime: string): AgentStreamEvent[] {
  if (!Array.isArray(value)) return [];
  const fallbackTimestamp = Date.parse(fallbackTime) || Date.now();
  return value.flatMap((item, index) => {
    const record = isRecord(item) ? item : {};
    const kind = String(record.kind || "");
    if (!RESTORABLE_AGENT_EVENT_KINDS.has(kind)) return [];
    const payload = isRecord(record.payload) ? record.payload : {};
    const sequence = typeof record.sequence === "number" && Number.isFinite(record.sequence) ? record.sequence : index + 1;
    const timestamp = typeof record.timestamp === "number" && Number.isFinite(record.timestamp) ? record.timestamp : fallbackTimestamp + index;
    return [
      normalizeAgentPayload(
        kind as RestorableAgentEventKind,
        payload,
        sequence,
        timestamp,
      ),
    ];
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
