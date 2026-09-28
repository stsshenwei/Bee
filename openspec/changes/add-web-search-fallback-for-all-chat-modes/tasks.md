## 1. Shared Fallback Foundation

- [x] 1.1 Add a shared web-search fallback service that reuses the configured HTTP JSON search provider and plugin/runtime guardrails.
- [x] 1.2 Add a normalized fallback result model for web results, web source records, fallback notice text, and safe debug metadata.
- [x] 1.3 Add evidence sufficiency helpers that classify empty hits, missing sources, low confidence, and usable internal evidence.
- [x] 1.4 Add configuration wiring for fallback availability, provider timeout, result limit, and sufficiency threshold.
- [x] 1.5 Add unit tests for provider availability, result normalization, failure metadata, and sufficiency decisions.

## 2. Quick Chat Integration

- [x] 2.1 Add a Chat/RAG pipeline stage that runs after parent recall and before source/reasoning emission.
- [x] 2.2 Update quick pipeline state to carry fallback status, web results, web sources, and web answer context.
- [x] 2.3 Update quick completion prompting so web fallback answers begin with the required disclosure notice.
- [x] 2.4 Wire the legacy raw quick-chat path through the same fallback service before emitting sources.
- [x] 2.5 Add tests for quick pipeline fallback, quick raw fallback, source-before-token ordering, and no-fallback behavior when internal evidence is sufficient.

## 3. Agent, Wiki, And RAG-Wiki Integration

- [x] 3.1 Ensure effective runtime policies can expose `web_search` fallback for `reasoning`, `wiki`, and `rag_wiki` without bypassing plugin guardrails.
- [x] 3.2 Add deterministic terminal fallback for Agent Runtime paths when internal evidence remains insufficient after the mode's normal retrieval/tool flow.
- [x] 3.3 Update mode-specific prompt/context assembly so web fallback answers preserve the exact required disclosure notice.
- [x] 3.4 Ensure Wiki-only fallback triggers when Wiki pages and scoped source documents are insufficient.
- [x] 3.5 Ensure RAG-Wiki fallback triggers only when both raw RAG and Wiki evidence are insufficient.
- [x] 3.6 Add tests covering fallback and non-fallback behavior for `reasoning`, `wiki`, and `rag_wiki`.

## 4. Streaming, Persistence, And Safety

- [x] 4.1 Emit web fallback sources with `source_type: "web"` while preserving the existing `sources` SSE payload shape.
- [x] 4.2 Persist assistant metadata indicating fallback attempted, fallback used, trigger reason, result count, and safe failure details.
- [x] 4.3 Add reasoning or agent trace events that identify the fallback decision without leaking provider credentials or private stack traces.
- [x] 4.4 Prevent web fallback result text from being indexed into the knowledge base, Wiki, graph, feedback documents, or vector store.
- [x] 4.5 Prevent memory storage from saving web result contents as durable memory facts unless an explicit future policy allows it.
- [x] 4.6 Add replay tests proving stored fallback source/token/final/done event order remains compatible.

## 5. Failure Behavior

- [x] 5.1 Return a safe insufficient-evidence answer when web search is disabled or unconfigured.
- [x] 5.2 Return a safe insufficient-evidence answer when web search times out or errors.
- [x] 5.3 Return a safe insufficient-evidence answer when web search returns no usable results.
- [x] 5.4 Add tests that verify failure answers do not fabricate facts and do not include the web fallback disclosure as if web evidence was used.

## 6. Documentation And Validation

- [x] 6.1 Update `docs/ARCHITECTURE.md` with the cross-mode web-search fallback flow.
- [x] 6.2 Update `docs/design-docs/backend-rag-pipeline.md` with fallback stage order, sufficiency rules, and source metadata.
- [x] 6.3 Update `docs/PLUGINS.md` with web-search fallback configuration and failure behavior.
- [x] 6.4 Run focused backend tests for chat pipeline, agent runtime tools, plugin management, stream replay, and RAG API routes.
- [x] 6.5 Run a manual smoke test for `quick`, `reasoning`, `wiki`, and `rag_wiki` covering internal-hit answers, web fallback answers, unavailable web search, and stream replay.
