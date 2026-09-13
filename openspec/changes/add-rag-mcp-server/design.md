## Context

The backend is a FastAPI RAG service with PostgreSQL/pgvector storage, document repositories, Wiki services, conversation history, and a browser-facing `/chat/stream` SSE contract. External MCP clients need access to this knowledge system, but the existing HTTP API is shaped for the web UI and includes mutating operations that should not automatically become agent-callable.

WeKnora provides two useful precedents: a standalone Python MCP server that adapts HTTP APIs to MCP transports, and a newer curated CLI MCP surface that intentionally exposes only a small allowlisted set of tools. This project should borrow the standalone Python deployment shape and the curated safety model.

## Goals / Non-Goals

**Goals:**

- Provide a first-party MCP server for external AI clients.
- Support local `stdio` usage and authenticated network usage through Streamable HTTP.
- Reuse existing backend services instead of duplicating RAG, Wiki, or chat logic.
- Expose a curated initial tool set for KB discovery, controlled KB creation/archive, document upload, document/chunk inspection, retrieval, RAG query, chat, and read-only Wiki access.
- Return structured MCP results with bounded output, stable errors, and useful tool annotations.
- Keep the existing FastAPI routes and frontend SSE behavior unchanged.

**Non-Goals:**

- Do not expose every backend API as an MCP tool.
- Do not expose broad write/mutation tools for document deletion, hard delete, Wiki edits, feedback, settings, or storage reset.
- Do not change the retrieval pipeline, vector store schema, conversation schema, or Wiki persistence model.
- Do not require the frontend to know about MCP.
- Do not implement third-party MCP client management inside Bee in this change.

## Decisions

### Decision: Run MCP as a thin backend-owned adapter

Implement the MCP server under `backend/app/mcp/` or `backend/app/mcp_server.py`, with its own command entrypoint that constructs or imports the same backend services used by FastAPI.

Rationale: MCP has different transport and lifecycle requirements from the browser API. Keeping it as a sibling entrypoint avoids disturbing `/chat/stream` and makes local `stdio` integration natural.

Alternatives considered:

- Mount MCP directly inside the existing FastAPI app. This reduces process count but couples MCP transport behavior to the web app and makes local stdio awkward.
- Build a separate service that calls Bee over HTTP only. This is operationally simple but duplicates auth, scope handling, and event projection logic outside the backend codebase.

### Decision: Default to a curated allowlisted tool surface

Register only the initial tools needed for agent-safe knowledge access and controlled operator actions: `kb_list`, `kb_view`, `kb_create`, `kb_delete`, `doc_list`, `doc_view`, `doc_upload`, `chunk_list`, `search_chunks`, `rag_query`, `chat`, `wiki_search`, and `wiki_read_page`.

Rationale: MCP clients are often autonomous agents. The callable surface should be treated as an external API contract, not a mirror of all backend endpoints. The default surface includes the operator workflows needed to provision a knowledge base and add documents while still omitting high-blast-radius actions.

Alternatives considered:

- Expose all existing HTTP routes as MCP tools. This is faster to generate but unsafe and noisy for agents.
- Expose all upload/delete/write routes. This would be convenient but too broad; this change only includes KB create, KB archive, and document upload/index.

### Decision: Use existing services and scope resolution

Tool handlers should call existing service methods such as `RAGService.resolve_scope`, `hybrid_retrieve_hits`, `recall_parent_hits`, `extract_sources`, `answer_query`, `list_documents`, `WikiPageService.search_pages`, and `WikiPageService.get_page`.

Rationale: Retrieval correctness, KB scoping, citation formatting, and Wiki behavior already live in those layers. MCP should not become a parallel RAG implementation.

Alternatives considered:

- Call internal FastAPI endpoints from MCP tools. This adds HTTP overhead and makes in-process tests harder.
- Reimplement retrieval in MCP handlers. This risks diverging ranking, filtering, and citation behavior.

### Decision: Buffer streaming chat into MCP results

The `chat` tool should consume existing chat streaming behavior internally and return a bounded event projection in `structuredContent`, including answer text, sources, session identifiers, and selected lifecycle events. It should not expose the raw frontend SSE stream as the MCP result.

Rationale: MCP `tools/call` is request/response oriented for many clients. A bounded projection mirrors WeKnora's approach and avoids making MCP clients parse Bee's browser-specific SSE vocabulary.

Alternatives considered:

- Return only final answer text. This loses citations and useful metadata.
- Proxy the raw SSE stream. This couples MCP clients to a private web contract and complicates compatibility.

### Decision: Authenticate network transports explicitly

`stdio` mode relies on local process boundaries and backend environment configuration. Streamable HTTP mode must require a configured token, accepted through `Authorization: Bearer <token>` or another documented header, and must fail closed if the token is missing.

Rationale: MCP exposes high-value knowledge access. Network mode needs a simple mandatory gate before any tool listing or invocation.

Alternatives considered:

- Allow unauthenticated local-network HTTP by default. This is convenient but risky.
- Implement full OAuth in the first change. That is desirable for enterprise deployments but too large for the first server-facing capability.

### Decision: Provide structured output and bounded payloads

Every tool result should include `structuredContent` for machine use and a compact text fallback for clients that render text. Large fields such as chunk content, document content, and event arrays must be capped with truncation metadata.

Rationale: Agent clients need predictable JSON, but unbounded RAG output can overflow context windows or expose unnecessary data.

Alternatives considered:

- Return plain text only. This is easier but brittle for agents.
- Return full raw records. This increases token cost and exposure risk.

## Risks / Trade-offs

- MCP SDK or transport behavior can drift across protocol versions -> Pin and document the supported SDK/protocol version, and cover initialization/tool-call flows in tests.
- Buffered chat can take a long time for slow LLM responses -> Add configurable timeouts and return structured stream-aborted errors with session/message identifiers where available.
- Tool outputs may leak internal implementation details -> Sanitize errors, omit secrets, and cap diagnostic metadata.
- Running MCP in a separate process may duplicate service startup cost -> Keep the adapter thin and use the same dependency construction path as FastAPI where practical.
- Curated tools may feel incomplete for operators -> Document the safety stance and add future mutation tools only behind explicit scopes or approval.

## Migration Plan

1. Add the MCP dependency and server entrypoint behind explicit operator configuration.
2. Ship `stdio` first for local clients, with Streamable HTTP enabled only when `MCP_SERVER_AUTH_TOKEN` or equivalent is configured.
3. Add tests for tool listing, validation failures, auth rejection, scoped retrieval, Wiki reads, and buffered chat output.
4. Document client configuration examples and deployment notes.
5. Rollback by disabling the MCP entrypoint or removing it from process supervision; existing FastAPI routes remain unchanged.

## Open Questions

- Should the initial network auth token map to a Bee principal, or should MCP start as a service-level principal with default workspace scope?
- Should `chat` create durable conversation history by default, or should it support a `temporary` flag as the safer default for MCP callers?
- Should document raw content/download be included in the first release, or should agents use chunk-level access only?
