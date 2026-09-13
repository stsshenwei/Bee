## Why

External AI clients need a standard way to use this project's RAG, document, Wiki, and chat capabilities without coupling to the existing browser-oriented HTTP/SSE API. MCP provides that integration surface while letting the project expose a deliberately curated, agent-safe tool set.

## What Changes

- Add a first-party MCP server for Bee RAG that can be run locally by MCP clients and, when configured, exposed over a network transport.
- Expose a curated initial tool surface for knowledge-base discovery, controlled knowledge-base creation/archive, document upload, document/chunk inspection, hybrid retrieval, RAG query, chat, and read-only Wiki access.
- Buffer chat/RAG stream events into MCP tool results so MCP clients receive structured results without depending on the frontend SSE contract.
- Require explicit authentication for network MCP transports and keep only explicitly allowlisted mutating tools in the default MCP surface.
- Add validation, output size limits, structured error payloads, and MCP tool annotations for safety and client usability.
- Document runtime configuration and client setup for local and network MCP usage.

## Capabilities

### New Capabilities

- `rag-mcp-server`: Provides an MCP-compatible server and curated tool surface for external clients to query and inspect the RAG knowledge system.

### Modified Capabilities

None.

## Impact

- Backend: new MCP server module, tool registry/handlers, configuration, and tests under `backend/`.
- Dependencies: add the Python MCP SDK and any transport runtime dependency needed for Streamable HTTP.
- Existing APIs: no breaking changes to FastAPI routes, `/chat/stream` SSE framing, RAG query behavior, ingestion, or vector storage.
- Docs: update backend/API/development docs with MCP server setup, transports, authentication, and tool inventory.
