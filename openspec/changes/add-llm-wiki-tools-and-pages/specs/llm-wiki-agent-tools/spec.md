## ADDED Requirements

### Requirement: Conditional Wiki Tool Registration
The agent runtime SHALL expose Wiki tools only when the request has a Wiki-capable knowledge-base scope and Wiki tool feature flags are enabled.

#### Scenario: Read tools enabled
- **WHEN** reasoning mode starts with an active Wiki-enabled knowledge base and read Wiki tools are enabled
- **THEN** the runtime SHALL expose `wiki_search`, `wiki_read_page`, `wiki_read_source_doc`, and `wiki_flag_issue`

#### Scenario: Wiki tools disabled
- **WHEN** Wiki tools are disabled or no selected knowledge base supports Wiki
- **THEN** the runtime SHALL omit Wiki tools or return clear unavailable observations without failing the request

#### Scenario: Tool scope isolation
- **WHEN** a Wiki tool receives a knowledge-base ID outside the resolved request scope
- **THEN** the tool SHALL reject the call or ignore the out-of-scope target and SHALL NOT leak page names or content

### Requirement: Wiki Search Tool
The system SHALL provide a `wiki_search` tool that finds candidate Wiki pages without exposing unbounded page content.

#### Scenario: Search returns bounded summaries
- **WHEN** the model calls `wiki_search` with one or more queries
- **THEN** the tool SHALL return bounded matching page slugs, titles, page types, summaries, aliases, snippets, and knowledge-base IDs

#### Scenario: Provider-neutral matching
- **WHEN** the model uses simple text or alternation terms in `wiki_search`
- **THEN** the tool SHALL execute provider-neutral matching compatible with the active storage backend and SHALL NOT require Postgres-specific regex behavior

#### Scenario: Search is not enough for final answer
- **WHEN** `wiki_search` returns one or more candidate pages
- **THEN** the runtime SHALL require `wiki_read_page` or raw source deep reading before accepting a factual final answer based on those pages

### Requirement: Wiki Page Read Tool
The system SHALL provide a `wiki_read_page` tool that reads full scoped Wiki page content and navigation metadata.

#### Scenario: Read page by slug
- **WHEN** the model calls `wiki_read_page` with one or more slugs
- **THEN** the tool SHALL return full Markdown content, summary, page type, status, source references, chunk references, inbound links, outbound links, aliases, and knowledge-base ID

#### Scenario: Read index page
- **WHEN** the model reads the special `index` slug
- **THEN** the tool SHALL return a bounded overview of Wiki structure rather than unbounded content for every page in the knowledge base

#### Scenario: Missing page
- **WHEN** the model requests a slug that does not exist in scope
- **THEN** the tool SHALL return a recoverable not-found observation

### Requirement: Wiki Source Drilldown Tool
The system SHALL provide a `wiki_read_source_doc` tool that reads original source chunks referenced by a Wiki page.

#### Scenario: Read source chunks by document
- **WHEN** the model calls `wiki_read_source_doc` with a source document ID from a Wiki page
- **THEN** the tool SHALL return bounded source chunk content from that scoped document

#### Scenario: Search within source document
- **WHEN** the model provides a query to `wiki_read_source_doc`
- **THEN** the tool SHALL return bounded matching chunks and nearby context from the original document

#### Scenario: Exact-answer fallback
- **WHEN** the user asks for exact quotes, numbers, code, table values, or other precise facts
- **THEN** the runtime SHALL prefer `wiki_read_source_doc`, `list_knowledge_chunks`, or `get_document_info` evidence over Wiki summary text

### Requirement: Wiki Issue Tooling
The system SHALL provide Wiki issue tools for reporting and reviewing page problems.

#### Scenario: Flag issue
- **WHEN** the model detects that a Wiki page contradicts source chunks or appears to mix entities
- **THEN** it SHALL be able to call `wiki_flag_issue` to record a pending issue

#### Scenario: Read issue
- **WHEN** maintenance tools are enabled and the model calls `wiki_read_issue`
- **THEN** the tool SHALL return issue details only for issues in the resolved scope

#### Scenario: Update issue status
- **WHEN** maintenance tools are enabled and the model calls `wiki_update_issue`
- **THEN** the tool SHALL update only the issue status and SHALL NOT modify Wiki page content

### Requirement: Safe Wiki Maintenance Tools
The system SHALL keep Wiki write, replace, rename, and delete operations behind maintenance policy and proposal semantics by default.

#### Scenario: Write creates proposal
- **WHEN** the model calls `wiki_write_page` while direct Wiki mutation is disabled
- **THEN** the tool SHALL create a draft page or page proposal instead of overwriting a published page

#### Scenario: Replace creates patch proposal
- **WHEN** the model calls `wiki_replace_text` while direct Wiki mutation is disabled
- **THEN** the tool SHALL verify the target text exists and create a proposed patch with source references

#### Scenario: Rename or delete requires approval
- **WHEN** the model calls `wiki_rename_page` or `wiki_delete_page`
- **THEN** the tool SHALL create a pending proposal or return an approval-required observation unless direct mutation is explicitly enabled

#### Scenario: Source references required for factual edits
- **WHEN** a Wiki maintenance tool proposes new factual content
- **THEN** the proposal SHALL include source document or chunk references that justify the change
