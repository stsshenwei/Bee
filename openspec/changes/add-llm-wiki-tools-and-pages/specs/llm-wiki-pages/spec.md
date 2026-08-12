## ADDED Requirements

### Requirement: Scoped Wiki Page Storage
The system SHALL store LLM Wiki pages as first-class scoped records owned by exactly one workspace and one knowledge base.

#### Scenario: Create scoped page
- **WHEN** a Wiki page is created for a knowledge base
- **THEN** the system SHALL persist workspace ID, knowledge base ID, slug, title, page type, status, content, summary, version, timestamps, links, source references, and chunk references

#### Scenario: Reject cross-scope page access
- **WHEN** a request scope does not include the page's knowledge base
- **THEN** the system SHALL NOT return or mutate that Wiki page

#### Scenario: Unique slug per knowledge base
- **WHEN** two pages use the same slug in the same active knowledge base
- **THEN** the system SHALL reject the second active page or create a revision/proposal instead of creating a duplicate active slug

### Requirement: Wiki Provenance
The system SHALL bind generated and edited Wiki factual content to original document evidence.

#### Scenario: Generated page carries sources
- **WHEN** the system generates a Wiki page from indexed document chunks
- **THEN** the page SHALL record source document IDs and source chunk IDs used to support the page

#### Scenario: Missing source evidence
- **WHEN** an LLM generation step cannot identify source chunks for a factual claim
- **THEN** the system SHALL either omit the unsupported claim or store the page as draft with a provenance warning

#### Scenario: Source deletion affects Wiki pages
- **WHEN** a source document or chunk referenced by a Wiki page is deleted or reindexed away
- **THEN** the system SHALL mark affected pages stale, remove invalid chunk references, or create a pending issue for review

### Requirement: Wiki Generation Lifecycle
The system SHALL generate Wiki pages through bounded, observable tasks after document indexing succeeds.

#### Scenario: Enqueue generation after indexing
- **WHEN** a document finishes raw parsing, chunking, and indexing in a Wiki-enabled knowledge base
- **THEN** the system SHALL enqueue or run a Wiki generation task without rolling back the successful raw document index if Wiki generation later fails

#### Scenario: Draft by default
- **WHEN** a Wiki generation task creates a new page and auto-publish is not enabled
- **THEN** the system SHALL store the page with `draft` status

#### Scenario: Generation failure
- **WHEN** a Wiki generation task fails
- **THEN** the system SHALL record a sanitized error and keep raw document retrieval available

### Requirement: Wiki Link And Folder Model
The system SHALL support folder navigation and page-link relationships for Wiki pages.

#### Scenario: Parse page links
- **WHEN** a Wiki page is created or updated with `[[slug]]` or `[[slug|label]]` links
- **THEN** the system SHALL refresh outbound links for that page and inbound links for linked pages in the same knowledge base

#### Scenario: Folder tree navigation
- **WHEN** a user or generation task assigns a page to a folder
- **THEN** the system SHALL update folder ID, category path, depth, and sortable Wiki path for that page

#### Scenario: Link graph query
- **WHEN** the frontend requests a Wiki graph overview or ego graph
- **THEN** the system SHALL return bounded nodes, edges, and truncation metadata for pages visible in the selected knowledge-base scope

### Requirement: Wiki Issue Records
The system SHALL record issues against Wiki pages for factual errors, mixed entities, contradictions, outdated content, or other maintenance concerns.

#### Scenario: Flag page issue
- **WHEN** a user, service, or Wiki tool flags a page issue
- **THEN** the system SHALL persist issue type, description, slug, suspected documents or chunks, reporter, status, and timestamps

#### Scenario: List pending issues
- **WHEN** the Wiki browser or an agent tool requests pending issues for a scoped knowledge base
- **THEN** the system SHALL return only issues belonging to that scope

#### Scenario: Resolve issue
- **WHEN** an authorized maintenance flow marks an issue resolved or ignored
- **THEN** the system SHALL update issue status without changing the referenced page content
