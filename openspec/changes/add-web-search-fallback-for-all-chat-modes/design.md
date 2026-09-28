## Context

Bee has several answer paths behind `/chat/stream`:

- `quick` can run through the `chat_pipeline` package when `CHAT_RAG_PIPELINE_ENABLED=true`, or through the legacy raw quick-chat helper when the flag is disabled.
- `reasoning`, `wiki`, and `rag_wiki` can run through Agent Runtime or agentic retrieval paths.
- Web search already exists as an Agent Runtime tool backed by a configured HTTP JSON endpoint, controlled by plugin/environment guardrails.

The user-facing behavior is not yet consistent when internal evidence is missing. Some paths produce an insufficient-evidence answer, some rely on the model or agent loop, and web search is only available where the runtime tool policy exposes it. This change introduces one shared fallback policy: every chat mode uses `web_search` when internal evidence is insufficient, and every web-based answer explicitly discloses that it came from network search rather than the knowledge base.

## Goals / Non-Goals

**Goals:**

- Apply one web-search fallback policy to `quick`, `reasoning`, `wiki`, and `rag_wiki`.
- Reuse the existing configured web search provider and plugin guardrails.
- Keep `/chat/stream` event names and old-client behavior compatible.
- Distinguish web sources from knowledge-base, Wiki, temporary attachment, and graph sources.
- Make fallback decisions visible in reasoning, agent trace, debug metadata, and persisted assistant metadata.
- Fail closed when web search is unavailable or returns no usable evidence.

**Non-Goals:**

- Do not add a built-in public search engine dependency.
- Do not use `web_fetch` as part of the initial fallback path.
- Do not ingest web search results into the knowledge base, feedback corpus, Wiki, memory, vector store, or graph.
- Do not silently blend web evidence with internal evidence when the answer is primarily web-based.
- Do not rename existing SSE payload keys.

## Decisions

### Decision: Create a shared web fallback service

Add a small backend service, for example `WebSearchFallbackService`, that owns:

- web search provider construction from the same effective runtime/plugin configuration used by Agent Runtime;
- bounded query execution and result normalization;
- fallback notice text;
- source shaping;
- generation context assembly for web results;
- debug metadata for availability, trigger reason, result count, and errors.

Rationale: `WebSearchTool` is currently shaped for Agent Runtime tool calls. Quick pipeline stages and legacy raw quick chat need the same provider behavior without pretending they are agent tool calls. A shared service can reuse `HTTPJSONSearchProvider` but expose a deterministic application API.

Alternative considered: make each chat mode call `WebSearchTool` directly. That would duplicate error handling, source formatting, and prompt disclosure across modes.

### Decision: Trigger fallback from an evidence decision, not only zero hits

Introduce a reusable evidence decision helper that considers:

- selected hit count;
- extracted source count;
- retrieval confidence or best score where available;
- existing insufficient-evidence debug flags;
- citation verification or mode-specific source availability.

Fallback triggers when the current mode has no usable internal evidence or evidence is below the configured sufficiency threshold. Direct document selections still count as internal evidence when their chunks are loaded successfully.

Rationale: a non-empty low-quality retrieval result can still be unsafe to answer from. The current trace already records `insufficient_evidence`; this change formalizes that signal across modes.

Alternative considered: trigger only on `hits == []`. That is simpler but fails the user's intended "query cannot answer from knowledge base" behavior.

### Decision: Put quick-mode fallback in the Chat/RAG pipeline and legacy raw path

For the pipeline path, add a fallback stage after internal retrieval/parent recall and before source/reasoning emission:

```text
retrieve
-> recall_parent_context
-> maybe_web_search_fallback
-> emit_sources
-> emit_reasoning
-> emit_agent_trace
-> chat_completion_stream
```

For the legacy raw quick-chat path, call the same fallback service before emitting `sources`.

Rationale: `EmitSourcesStage` must see final sources before old clients receive source events. Keeping the fallback before generation preserves the existing source-before-token order.

Alternative considered: call web search inside `stream_answer`. That hides retrieval provenance from `sources`, reasoning, trace, and persistence.

### Decision: Integrate Agent Runtime modes through runtime policy and terminal fallback

For `reasoning`, `wiki`, and `rag_wiki`, ensure the effective runtime policy can use `web_search` as a fallback tool regardless of mode. If the agent/runtime completes without sufficient internal evidence, the orchestrator performs a deterministic terminal fallback with the shared service before producing the final answer.

Rationale: Agent Runtime can use tools dynamically, but the product rule is deterministic: every mode falls back to web search. A terminal fallback protects against model/tool policy drift.

Alternative considered: only add `web_search` to enabled tools and rely on prompts. That does not guarantee fallback in every mode.

### Decision: Always disclose web fallback in the answer text

Every answer generated from web fallback MUST begin with:

```text
知识库无答案，以下来自网络搜索
```

Generation prompts must preserve this exact leading notice, and deterministic fallback failure messages must not imply that the knowledge base answered the question.

Rationale: users need a clear boundary between internal knowledge and external search. The notice is part of the product contract, not optional style guidance.

Alternative considered: add only a source badge in metadata/UI. That is easy to miss in streamed text and old clients.

### Decision: Web sources are explicit source records

Normalize search results into source items such as:

```json
{
  "source": "Result title",
  "source_type": "web",
  "url": "https://example.com/page",
  "snippet": "bounded snippet",
  "score": 0.0,
  "provider": "web_search"
}
```

The existing `sources` SSE event remains a list. Web source records are additive and old clients can ignore unknown fields.

Rationale: source provenance should be machine-readable for UI, persistence, audit, and evaluation.

Alternative considered: encode the URL into the `source` label only. That preserves old display behavior but makes provenance fragile.

### Decision: Web fallback results stay ephemeral

Web search results are used only for the current answer and persisted as assistant metadata/source records. They are not indexed, summarized into memory, written as feedback, or attached to Wiki pages.

Rationale: web results are external and time-sensitive. Ingesting them into durable knowledge would blur source ownership and freshness.

Alternative considered: write web results into temporary knowledge state. That might help multi-turn follow-up, but it needs TTL, citation freshness, and privacy design beyond this change.

## Risks / Trade-offs

- [Risk] External search can be slow or unavailable. -> Mitigation: bound timeout/result count, emit safe fallback failure messages, and avoid blocking indefinitely.
- [Risk] Web results can be lower trust than curated knowledge. -> Mitigation: require visible disclosure and web-specific source metadata.
- [Risk] Agent Runtime may independently call web search before deterministic fallback. -> Mitigation: record fallback path and source type; only prepend the required notice when the final answer is web-based because internal evidence was insufficient.
- [Risk] Different modes expose different traces today. -> Mitigation: store consistent fallback metadata in common assistant metadata and add mode-specific trace events where supported.
- [Risk] Legacy quick-chat path diverges from pipeline path. -> Mitigation: use the same shared service and add tests for both paths while the raw path remains.

## Migration Plan

1. Add the shared fallback service and unit tests using fake web search providers.
2. Add effective configuration wiring so runtime plugin settings and environment guardrails decide whether web fallback is available.
3. Add evidence sufficiency helpers and tests for empty, low-score, no-source, and valid-evidence cases.
4. Wire quick pipeline fallback before source emission and generation.
5. Wire legacy raw quick-chat fallback through the same service.
6. Wire `reasoning`, `wiki`, and `rag_wiki` with both web-search tool availability and deterministic terminal fallback.
7. Update prompts/context assembly so web fallback answers preserve the required leading notice.
8. Add SSE, persistence, replay, audit/debug, and mode coverage tests.
9. Update documentation.

Rollback is configuration-based: disable the web search plugin/environment values or the new fallback feature flag if one is introduced. When disabled, the system returns the existing insufficient-evidence behavior rather than attempting network search.

## Open Questions

- Should web fallback have its own explicit feature flag in addition to plugin availability, for example `WEB_SEARCH_FALLBACK_ENABLED`?
- Should the sufficiency threshold reuse `MIN_RELEVANCE_SCORE`, or should fallback have a dedicated `WEB_SEARCH_FALLBACK_MIN_CONFIDENCE`?
- Should the frontend render `source_type: "web"` with a distinct visual label in the first implementation, or only rely on existing source rendering plus answer disclosure?
