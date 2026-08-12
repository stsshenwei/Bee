## Why

Bee already exposes `wiki` as a knowledge-base type, but it currently behaves like a metadata label over the document evidence pipeline. Adding LLM Wiki turns selected knowledge bases into durable, browsable, source-bound pages that help users understand and maintain knowledge instead of only querying raw chunks.

This is timely because the project already has scoped knowledge bases, document enrichment, agent tool calling, graph retrieval, staged processing, and citation verification. The missing layer is a first-class Wiki domain with page provenance, safe agent tools, and a UI surface for browsing and maintenance.

## What Changes

- Add a first-class LLM Wiki page domain with scoped pages, folders, page issues, status, versioning, links, source document references, and source chunk references.
- Add an LLM Wiki generation flow that creates draft or published pages from indexed document evidence, preserving `KnowledgeBaseScope` and evidence traceability.
- Add read-first Wiki runtime tools inspired by WeKnora: `wiki_search`, `wiki_read_page`, `wiki_read_source_doc`, and `wiki_flag_issue`.
- Add maintenance-oriented Wiki tools behind stricter policy: `wiki_read_issue`, `wiki_update_issue`, `wiki_write_page`, `wiki_replace_text`, `wiki_rename_page`, and `wiki_delete_page`, with write tools creating drafts or proposed changes unless an explicit safe apply policy is enabled.
- Add a Wiki browser in the knowledge-base workspace for page lists, folders, page reading, source references, link graph navigation, and issue review.
- Integrate Wiki evidence into chat reasoning without replacing raw document chunks for exact quotes, numeric values, code, tables, or citation verification.
- Update architecture, backend RAG pipeline, frontend UI, and runtime documentation to describe Wiki behavior, scope isolation, and safety boundaries.

## Capabilities

### New Capabilities

- `llm-wiki-pages`: LLM-generated Wiki pages, folders, provenance, generation lifecycle, link graph, and issue records.
- `llm-wiki-agent-tools`: Model-callable Wiki search, read, source-drilldown, issue, and maintenance tools with scoped execution and write-safety policy.
- `llm-wiki-browser`: Knowledge-base UI for browsing, reading, graphing, and maintaining Wiki pages and issues.

### Modified Capabilities

None.

## Impact

- Backend schema: add Wiki page, folder, issue, task/status, and optional revision/proposal tables to the existing SQLite metadata store.
- Backend services: add Wiki repository/service/generation layers under the existing `KnowledgeBaseScope`, reuse document repository, chunk repository, prompt catalog, processing spans, and runtime tool registry.
- Agent runtime: add Wiki tools, tool metadata, execution classes, deep-read rules, and prompt guidance; keep tools feature-gated and scoped.
- Retrieval and answer behavior: use Wiki pages as curated navigation and synthesis evidence, while raw document chunks remain authoritative for precise citations and contradiction handling.
- Frontend: extend the knowledge-base workspace with a Wiki tab/browser, page reader, graph view, source panel, issue panel, and draft/proposal review affordances.
- Tests and docs: add unit/integration coverage for Wiki scope isolation, provenance, tool contracts, write safety, UI behavior, and update design documents.
