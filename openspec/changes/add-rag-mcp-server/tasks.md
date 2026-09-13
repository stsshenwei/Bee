## 1. MCP Dependency And Entrypoint

- [x] 1.1 Add the Python MCP SDK and any Streamable HTTP runtime dependencies to `backend/requirements.txt`.
- [x] 1.2 Create the backend MCP module structure and command entrypoint for `stdio` and Streamable HTTP modes.
- [x] 1.3 Reuse or factor backend service construction so MCP tools can access `RAGService`, knowledge-base service, document repository, Wiki service, and conversation service without duplicating RAG logic.
- [x] 1.4 Add MCP runtime configuration for transport, host, port, auth token, timeout, and output limits.

## 2. Transport And Authentication

- [x] 2.1 Implement `stdio` server startup with MCP initialization and tool registration.
- [x] 2.2 Implement Streamable HTTP startup with a documented `/mcp` endpoint.
- [x] 2.3 Add network auth middleware or request validation that requires the configured bearer token before MCP initialization, tool listing, or tool invocation.
- [x] 2.4 Fail server startup for network transport when the required auth token is missing.

## 3. Tool Result Infrastructure

- [x] 3.1 Implement shared helpers for successful MCP results with structured JSON content and text fallback.
- [x] 3.2 Implement structured error results for validation, scope, backend, timeout, and stream-aborted failures.
- [x] 3.3 Add output truncation helpers with truncation metadata for oversized text, chunks, events, and nested fields.
- [x] 3.4 Add allowlisted tool registration with MCP safety annotations where supported by the SDK.

## 4. Knowledge And Retrieval Tools

- [x] 4.1 Implement `kb_list` and `kb_view` using existing knowledge-base service methods.
- [x] 4.2 Implement `doc_list` and `doc_view` using existing document repository/service methods and bounded pagination.
- [x] 4.3 Implement `chunk_list` with scoped document lookup, bounded limit, and `truncated_at_limit` metadata.
- [x] 4.4 Implement `search_chunks` using existing scope resolution, hybrid retrieval, parent/context recall where appropriate, and source-ready result formatting.
- [x] 4.5 Validate required arguments and range limits before invoking backend retrieval.

## 5. RAG, Chat, And Wiki Tools

- [x] 5.1 Implement `rag_query` using `RAGService.answer_query` and existing KB/document scope handling.
- [x] 5.2 Implement `chat` with buffered event projection, final answer text, sources, and session/message identifiers.
- [x] 5.3 Add chat timeout and stream-aborted handling with retry-relevant metadata.
- [x] 5.4 Implement `wiki_search` using `WikiPageService.search_pages`.
- [x] 5.5 Implement `wiki_read_page` using `WikiPageService.get_page`.
- [x] 5.6 Ensure default tool listing omits ingestion, delete, storage reset, settings, and Wiki mutation tools.

## 6. Tests

- [x] 6.1 Add unit tests for tool listing, annotations, and the curated allowlist.
- [x] 6.2 Add unit tests for argument validation and structured error payloads.
- [x] 6.3 Add service-level tests for `kb_list`, `doc_list`, `chunk_list`, `search_chunks`, `rag_query`, `wiki_search`, and `wiki_read_page` with fake services or repositories.
- [x] 6.4 Add tests for buffered `chat` success, timeout, and stream-aborted behavior.
- [x] 6.5 Add network transport tests for missing auth token configuration, unauthorized requests, and authorized requests.
- [x] 6.6 Add regression tests that existing `/chat/stream` and `/rag/query` contracts remain unchanged.

## 7. Documentation And Validation

- [x] 7.1 Document MCP server configuration, supported transports, auth behavior, tool inventory, and safety stance.
- [x] 7.2 Add example MCP client configuration for local `stdio` usage.
- [x] 7.3 Add example network Streamable HTTP configuration with bearer token usage.
- [x] 7.4 Update development validation docs with MCP startup, tool listing, representative tool calls, auth rejection, and auth success checks.
- [x] 7.5 Run the relevant backend test suite and documented MCP smoke checks.

## 8. Controlled MCP Mutation Tools

- [x] 8.1 Add `kb_create` with required `name` and `description` inputs.
- [x] 8.2 Add `kb_delete` as knowledge-base archive through the existing service.
- [x] 8.3 Add `doc_upload` using the existing document upload, parse, chunk, and index path.
- [x] 8.4 Add tests and documentation for controlled mutation tools and upload limits.
