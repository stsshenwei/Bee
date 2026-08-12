## ADDED Requirements

### Requirement: Wiki Workspace Tab
The frontend SHALL provide a Wiki workspace for knowledge bases that are Wiki typed or Wiki-enabled.

#### Scenario: Show Wiki tab
- **WHEN** a user opens a knowledge base with type `wiki` or Wiki indexing enabled
- **THEN** the detail workspace SHALL show a Wiki tab or section alongside documents and settings

#### Scenario: Hide unavailable Wiki workspace
- **WHEN** a knowledge base is not Wiki-capable
- **THEN** the frontend SHALL hide the Wiki workspace or show a clear unavailable state without implying pages exist

#### Scenario: Preserve existing document workflows
- **WHEN** the Wiki workspace is added
- **THEN** existing document upload, document list, preview, retry, and chat-scope workflows SHALL remain available

### Requirement: Wiki Page Browser
The Wiki workspace SHALL let users browse and search Wiki pages.

#### Scenario: List pages
- **WHEN** a user opens the Wiki workspace
- **THEN** the frontend SHALL request scoped Wiki pages and render titles, summaries, page types, statuses, folders, updated time, and issue indicators

#### Scenario: Filter pages
- **WHEN** a user filters by query, page type, status, or folder
- **THEN** the frontend SHALL send scoped filter parameters and render only matching pages

#### Scenario: Empty state
- **WHEN** a Wiki-enabled knowledge base has no pages
- **THEN** the frontend SHALL show an empty state with available generation or upload actions, without starting provider work automatically

### Requirement: Wiki Page Reader
The Wiki workspace SHALL render selected Wiki pages with provenance and navigation.

#### Scenario: Read page
- **WHEN** a user selects a Wiki page
- **THEN** the frontend SHALL render Markdown content, summary, page status, version, page type, aliases, source documents, chunk references, inbound links, and outbound links

#### Scenario: Open source evidence
- **WHEN** a user selects a source document or chunk reference from a Wiki page
- **THEN** the frontend SHALL open the existing document preview or chunk/source evidence view within the selected knowledge-base scope

#### Scenario: Navigate Wiki link
- **WHEN** a user clicks an internal Wiki link
- **THEN** the frontend SHALL navigate to the linked page in the same knowledge base or show a not-found state

### Requirement: Wiki Graph View
The Wiki workspace SHALL provide a bounded page-link graph view.

#### Scenario: Open graph overview
- **WHEN** a user opens the Wiki graph view
- **THEN** the frontend SHALL request a bounded overview graph and render returned nodes, edges, page types, and truncation metadata

#### Scenario: Expand page neighborhood
- **WHEN** a user selects a page node
- **THEN** the frontend SHALL request an ego graph around that page and update the graph without loading the entire Wiki graph

### Requirement: Wiki Issue And Proposal Review
The Wiki workspace SHALL expose issue and proposal review surfaces without silently applying risky changes.

#### Scenario: Review issues
- **WHEN** a user opens the Wiki issue panel
- **THEN** the frontend SHALL list scoped pending issues with type, description, target page, suspected sources, reporter, and status

#### Scenario: Review proposed edit
- **WHEN** a Wiki maintenance tool creates a proposal
- **THEN** the frontend SHALL show the proposed action, target page, source references, and diff or payload for human review

#### Scenario: Apply approved proposal
- **WHEN** a user approves a proposal through an authorized UI path
- **THEN** the frontend SHALL call a scoped backend endpoint that applies the proposal and refreshes page content, links, issues, and graph metadata
