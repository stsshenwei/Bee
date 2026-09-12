import assert from "node:assert/strict";
import { registerHooks } from "node:module";

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (context.parentURL?.endsWith(".ts") && specifier.startsWith(".") && !/\.[cm]?[jt]sx?$/.test(specifier)) {
      return nextResolve(`${specifier}.ts`, context);
    }
    return nextResolve(specifier, context);
  },
});

const { historyMessageToChatMessage, mergeUniqueMessages } = await import("../app/lib/chat-history-state.ts");

const assistant = historyMessageToChatMessage({
  id: "msg-a",
  session_id: "session-1",
  conversation_id: "session-1",
  request_id: "req-1",
  role: "assistant",
  content: "partial",
  metadata_json: { chat_mode: "reasoning", sources: [{ source: "doc.md", score: 0.9 }], stopped: true },
  is_completed: false,
  created_at: "2026-01-01T00:00:00.000000",
  updated_at: null,
});

assert.equal(assistant.assistant_message_id, "msg-a");
assert.equal(assistant.chatMode, "reasoning");
assert.equal(assistant.agentCompleted, false);
assert.equal(assistant.stopped, true);
assert.equal(assistant.sources.length, 1);

const ragWiki = historyMessageToChatMessage({
  ...assistant,
  id: "msg-rag-wiki",
  metadata_json: { chat_mode: "rag_wiki" },
});
assert.equal(ragWiki.chatMode, "rag_wiki");

const withAgentProcess = historyMessageToChatMessage({
  ...assistant,
  id: "msg-agent-process",
  is_completed: true,
  metadata_json: {
    chat_mode: "rag_wiki",
    sources: [{ source: "DH-P7004.txt", score: 0.91 }],
    reasoning: { question: "支持哪些安全认证", normalized_query: "安全认证", retrieval_queries: ["安全认证"], term_mappings: [], evidence: [] },
    agent_events: [
      {
        kind: "agent_tool_call",
        payload: { tool: "wiki_search", action: "execute", input_summary: "安全认证", metadata: { call_id: "call-1" } },
        sequence: 1,
        timestamp: 1700000000000,
      },
      {
        kind: "agent_tool_result",
        payload: {
          tool: "wiki_search",
          action: "execute",
          status: "completed",
          output_summary: "Wiki search returned 2 pages.",
          source_chunk_ids: ["chunk-1"],
          metadata: { call_id: "call-1", result_count: 2, source_titles: ["安全认证"] },
        },
        sequence: 2,
        timestamp: 1700000000120,
      },
    ],
  },
});
assert.equal(withAgentProcess.agentEvents.length, 2);
assert.equal(withAgentProcess.agentTimeline.length, 1);
assert.equal(withAgentProcess.agentTimeline[0].tool, "wiki_search");
assert.equal(withAgentProcess.reasoning.normalized_query, "安全认证");

const current = [
  { id: "msg-2", role: "assistant", content: "newer" },
  { id: "msg-3", role: "user", content: "latest" },
];
const older = [
  { id: "msg-1", role: "user", content: "oldest" },
  { id: "msg-2", role: "assistant", content: "duplicate boundary" },
];

assert.deepEqual(
  mergeUniqueMessages(current, older, true).map((item) => item.id),
  ["msg-1", "msg-2", "msg-3"],
);

const withoutIds = mergeUniqueMessages(current, [{ role: "assistant", content: "local stream" }], false);
assert.equal(withoutIds.at(-1).content, "local stream");

console.log("chat history state tests passed");
