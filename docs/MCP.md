# MCP Server

Bee can expose a curated Model Context Protocol server for external AI clients. The MCP server is a backend-owned adapter over existing RAG, document, Wiki, and chat services; it does not replace the FastAPI API and does not expose every backend route.

## Supported Transports

- `stdio`: local client integration for tools such as Claude Desktop, Cursor, and IDE agents.
- `streamable-http` or `http`: network transport served at `/mcp`.

Network transport requires `MCP_SERVER_AUTH_TOKEN`. The server refuses to start without it.

## Environment

Common backend variables still apply, including `DATABASE_URL`, `OPENAI_API_KEY`, retrieval settings, and PostgreSQL settings.

MCP-specific variables:

- `MCP_TRANSPORT`: `stdio`, `streamable-http`, or `http`; default `stdio`.
- `MCP_HOST`: network bind host; default `127.0.0.1`.
- `MCP_PORT`: network bind port; default `8765`.
- `MCP_SERVER_AUTH_TOKEN`: required for network transports.
- `MCP_TOOL_TIMEOUT_SECONDS`: chat/tool timeout; default `300`.
- `MCP_MAX_OUTPUT_CHARS`: maximum serialized tool output size; default `6000`.
- `MCP_MAX_EVENTS`: maximum buffered chat events; default `200`.
- `MCP_MAX_UPLOAD_BYTES`: maximum document upload payload accepted by `doc_upload`; default `10485760`.

## Start The Server

Local stdio:

```powershell
cd backend
python -m app.mcp --transport stdio
```

Streamable HTTP:

```powershell
cd backend
$env:MCP_SERVER_AUTH_TOKEN="replace-with-a-strong-secret"
python -m app.mcp --transport streamable-http --host 127.0.0.1 --port 8765
```

Clients call:

```text
http://127.0.0.1:8765/mcp
Authorization: Bearer replace-with-a-strong-secret
```

## Default Tool Inventory

The default tool surface is intentionally allowlisted:

- `kb_list`: list visible knowledge bases
- `kb_view`: view a knowledge base
- `kb_create`: create a knowledge base; `name` and `description` are required so clients can prompt for both
- `kb_delete`: archive a knowledge base by ID
- `doc_list`: list documents in a knowledge base
- `doc_view`: view document metadata
- `doc_upload`: upload and index a document into a knowledge base using `content`, `content_base64`, or a server-local `file_path`
- `chunk_list`: list bounded chunks for a document
- `search_chunks`: run hybrid retrieval
- `rag_query`: run a grounded RAG query
- `chat`: run a buffered RAG chat turn
- `wiki_search`: search read-only Wiki pages
- `wiki_read_page`: read a Wiki page

`kb_delete` follows the backend's safe-delete behavior and archives the knowledge base; it does not physically remove persisted records. `doc_upload` reuses the existing backend upload path, so a successful call stores, parses, chunks, and indexes the document. When using `file_path`, the path is resolved on the MCP server host.

The default MCP server does not expose document deletion, settings, storage reset, feedback write-back, Wiki mutation, or hard-delete tools.

## Example Client Config

Stdio-style client config:

```json
{
  "mcpServers": {
    "bee-rag": {
      "command": "python",
      "args": ["-m", "app.mcp", "--transport", "stdio"],
      "cwd": "D:/python_project/new-rag-project/backend",
      "env": {
        "DATABASE_URL": "postgresql://rag:rag@localhost:5432/rag",
        "OPENAI_API_KEY": "your-api-key"
      }
    }
  }
}
```

Network clients should point at `/mcp` and send the bearer token configured in `MCP_SERVER_AUTH_TOKEN`.

## Validation

Run focused tests:

```powershell
cd backend
python -m pytest tests/test_mcp_tools.py
```

Smoke checks:

1. Start stdio mode and confirm your MCP client can list tools.
2. Start Streamable HTTP without `MCP_SERVER_AUTH_TOKEN`; startup should fail.
3. Start Streamable HTTP with `MCP_SERVER_AUTH_TOKEN`; requests without the bearer token should return unauthorized.
4. Call `kb_list`, `kb_create`, `doc_upload`, `search_chunks`, and `rag_query` from an MCP client.
