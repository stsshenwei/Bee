## ADDED Requirements

### Requirement: MCP server entrypoint and transports
The system SHALL provide a first-party MCP server entrypoint for the RAG backend. The server SHALL support local `stdio` transport and authenticated Streamable HTTP transport without changing the existing FastAPI route contracts.

#### Scenario: Local stdio server starts
- **WHEN** an operator starts the MCP server in `stdio` mode with valid backend configuration
- **THEN** the server initializes MCP capabilities and accepts tool listing and tool call requests over standard input and output

#### Scenario: Existing HTTP APIs remain compatible
- **WHEN** the MCP server capability is added
- **THEN** existing FastAPI routes including `/chat/stream` and `/rag/query` keep their existing request and response contracts

### Requirement: Network MCP authentication
The system MUST require explicit authentication for every network MCP transport request. Streamable HTTP mode MUST fail closed when no server auth token is configured.

#### Scenario: Missing network token configuration
- **WHEN** an operator starts Streamable HTTP mode without the required MCP auth token configuration
- **THEN** the MCP server refuses to start and reports a configuration error

#### Scenario: Unauthorized network request
- **WHEN** a network client calls the MCP endpoint without a valid bearer token
- **THEN** the server rejects the request before listing tools or invoking tools

#### Scenario: Authorized network request
- **WHEN** a network client calls the MCP endpoint with the configured bearer token
- **THEN** the server processes MCP initialization, tool listing, and tool calls normally

### Requirement: Curated tool surface
The system SHALL expose only an allowlisted initial MCP tool set by default: `kb_list`, `kb_view`, `kb_create`, `kb_delete`, `doc_list`, `doc_view`, `doc_upload`, `chunk_list`, `search_chunks`, `rag_query`, `chat`, `wiki_search`, and `wiki_read_page`.

#### Scenario: Tool list is curated
- **WHEN** an MCP client lists available tools
- **THEN** the response contains the allowlisted tools and omits document deletion, hard delete, storage reset, settings, feedback write-back, and Wiki mutation tools

#### Scenario: Tool annotations describe safety
- **WHEN** an MCP client lists available tools
- **THEN** each tool includes annotations or equivalent metadata describing read-only, destructive, idempotent, and open-world behavior where supported by the MCP SDK

### Requirement: Knowledge base and document discovery tools
The system SHALL provide MCP tools for knowledge-base and document discovery that reuse existing backend repositories and scope rules.

#### Scenario: List knowledge bases
- **WHEN** an MCP client calls `kb_list`
- **THEN** the result contains visible knowledge bases with stable identifiers and summary metadata

#### Scenario: View a knowledge base
- **WHEN** an MCP client calls `kb_view` with a valid knowledge base id
- **THEN** the result contains the knowledge base record including status, indexing strategy, provider configuration, and aggregate metadata safe for external clients

#### Scenario: List documents in a knowledge base
- **WHEN** an MCP client calls `doc_list` with a knowledge base id and optional filters
- **THEN** the result contains matching documents with bounded pagination and document metadata

#### Scenario: View document metadata
- **WHEN** an MCP client calls `doc_view` with a valid document id
- **THEN** the result contains document metadata without reading arbitrary filesystem paths

### Requirement: Controlled knowledge-base mutation tools
The system SHALL provide MCP tools for creating and archiving knowledge bases through the existing knowledge-base service.

#### Scenario: Create knowledge base prompts for required metadata
- **WHEN** an MCP client lists the `kb_create` tool
- **THEN** the input schema requires both `name` and `description` so the client prompts the user for both values

#### Scenario: Create knowledge base
- **WHEN** an MCP client calls `kb_create` with a valid name and description
- **THEN** the system creates a knowledge base through the existing service and returns the created record

#### Scenario: Archive knowledge base
- **WHEN** an MCP client calls `kb_delete` with a valid knowledge base id
- **THEN** the system archives the knowledge base through the existing service and returns the archived record

### Requirement: Controlled document upload tool
The system SHALL provide an MCP tool for uploading and indexing a document into a selected knowledge base through the existing upload pipeline.

#### Scenario: Upload document content
- **WHEN** an MCP client calls `doc_upload` with a knowledge base id, filename, and exactly one content source
- **THEN** the system stores, parses, chunks, and indexes the document using the existing backend upload behavior

#### Scenario: Reject invalid upload payload
- **WHEN** an MCP client calls `doc_upload` without content, with multiple content sources, or with content exceeding the configured upload limit
- **THEN** the server returns a structured validation error without storing the document

### Requirement: Chunk inspection and hybrid search tools
The system SHALL provide MCP tools for bounded chunk inspection and hybrid retrieval using the existing RAG retrieval pipeline.

#### Scenario: List document chunks
- **WHEN** an MCP client calls `chunk_list` with a document id and limit
- **THEN** the result contains at most the requested number of scoped chunks and indicates whether additional chunks were omitted

#### Scenario: Search chunks
- **WHEN** an MCP client calls `search_chunks` with a query and knowledge-base scope
- **THEN** the system runs the existing dense and keyword retrieval path and returns ranked, scoped, source-ready chunk results

#### Scenario: Reject invalid retrieval input
- **WHEN** an MCP client calls `search_chunks` with an empty query or an out-of-range limit
- **THEN** the server returns a structured validation error without invoking retrieval

### Requirement: RAG query and chat tools
The system SHALL provide MCP tools for grounded answers that reuse existing RAG and chat services. The MCP result SHALL be buffered and structured rather than exposing the browser SSE stream directly.

#### Scenario: RAG query returns grounded answer
- **WHEN** an MCP client calls `rag_query` with a question and optional knowledge-base or document scope
- **THEN** the result contains an answer, citations, used chunks, confidence metadata, and available retrieval debug metadata

#### Scenario: Chat returns buffered event projection
- **WHEN** an MCP client calls `chat` with a message and optional conversation id
- **THEN** the result contains a bounded event projection, final answer text, sources, and session/message identifiers

#### Scenario: Chat timeout or aborted stream
- **WHEN** the underlying chat generation ends without a terminal event or exceeds the configured timeout
- **THEN** the server returns a structured stream error including retry-relevant session or message identifiers when available

### Requirement: Read-only Wiki tools
The system SHALL provide read-only MCP tools for Wiki search and page reading when Wiki services are available for the selected knowledge-base scope.

#### Scenario: Search Wiki pages
- **WHEN** an MCP client calls `wiki_search` with a knowledge base id and query
- **THEN** the result contains matching Wiki pages with title, slug, summary, and compact snippets

#### Scenario: Read Wiki page
- **WHEN** an MCP client calls `wiki_read_page` with a knowledge base id and slug
- **THEN** the result contains the page content and safe metadata including links and source references

#### Scenario: Wiki mutation tools are unavailable
- **WHEN** an MCP client lists default tools
- **THEN** Wiki write, replace, rename, delete, issue update, and proposal apply tools are not present

### Requirement: Structured results and output safety
The system SHALL return MCP tool results with machine-readable structured content, a compact text fallback, bounded output size, and sanitized errors.

#### Scenario: Successful tool result
- **WHEN** an MCP tool call succeeds
- **THEN** the result includes structured JSON content and a text fallback that summarizes the same payload

#### Scenario: Large result is truncated
- **WHEN** a tool result exceeds the configured output size limit
- **THEN** the server truncates the oversized fields and includes metadata that indicates truncation

#### Scenario: Tool execution fails
- **WHEN** a tool call fails due to validation, scope, backend, or timeout errors
- **THEN** the result contains a structured error code, a user-safe message, and no secrets or raw internal tracebacks

### Requirement: Documentation and configuration
The system SHALL document how operators run and configure the MCP server and how clients connect to it.

#### Scenario: Operator reads MCP documentation
- **WHEN** an operator opens the project documentation
- **THEN** it describes required environment variables, supported transports, auth behavior, client examples, and the default tool inventory

#### Scenario: Developer validates MCP behavior
- **WHEN** a developer runs the documented MCP validation commands
- **THEN** they can verify server initialization, tool listing, representative tool calls, network auth rejection, and network auth success
