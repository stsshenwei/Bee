## Why

Bee currently answers from selected knowledge bases, Wiki, or agent tools depending on chat mode. When the selected internal evidence is missing or insufficient, users need a consistent fallback instead of receiving an unsupported answer or a mode-specific failure.

This change adds a shared web-search fallback policy for every chat mode, with an explicit disclosure that the knowledge base did not contain enough evidence and the answer is based on web search results.

## What Changes

- Add a shared `web_search` fallback policy used by `quick`, `reasoning`, `wiki`, and `rag_wiki` chat modes.
- Detect insufficient internal evidence using retrieval/source/citation state, not only a strict zero-hit result.
- When fallback is used, prepend a required user-visible notice: `知识库无答案，以下来自网络搜索`
- Normalize web search results into source records distinguishable from knowledge-base and Wiki sources.
- Reuse the configured HTTP JSON web search provider and plugin guardrails instead of adding a new network provider dependency.
- Fail closed when `web_search` is unavailable, unconfigured, times out, or returns no usable results.
- Preserve the existing `/chat/stream` SSE contract and stream replay behavior.
- Update backend architecture and plugin documentation for the shared fallback behavior.

## Capabilities

### New Capabilities
- `web-search-fallback`: Covers the cross-mode policy, triggering, disclosure, source shaping, failure behavior, and observability for web-search fallback.

### Modified Capabilities
- None.

## Impact

- Backend chat orchestration in `backend/app/main.py`.
- Quick Chat/RAG pipeline stages in `backend/app/services/chat_pipeline/`.
- Agent Runtime web search provider/tool reuse in `backend/app/services/agent/agent_runtime_tools.py`.
- Chat mode policies and prompts for `quick`, `reasoning`, `wiki`, and `rag_wiki`.
- SSE metadata, source records, assistant persistence metadata, memory storage safeguards, and audit/debug traces.
- Tests for chat streaming, plugin/runtime configuration, web-search provider behavior, and all fallback modes.
- Documentation in `docs/ARCHITECTURE.md`, `docs/design-docs/backend-rag-pipeline.md`, and `docs/PLUGINS.md`.
