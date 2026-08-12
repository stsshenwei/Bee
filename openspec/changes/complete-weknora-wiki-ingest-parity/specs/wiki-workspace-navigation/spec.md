## ADDED Requirements

### Requirement: Knowledge-base shell matches the reference navigation model
The knowledge-base detail screen SHALL present a compact breadcrumb and description header followed by first-class Documents, Wiki, and Graph navigation. Selecting Wiki SHALL display the Wiki workspace directly beneath this shell without wrapping the primary experience in a decorative card.

#### Scenario: User enters Wiki from documents
- **WHEN** the user selects Wiki from a knowledge-base detail screen
- **THEN** the URL and active navigation update while the knowledge-base identity, breadcrumb, and access context remain visible

#### Scenario: User switches to graph
- **WHEN** the user selects Graph
- **THEN** the graph occupies the available workspace area and preserves the prior Wiki page selection for return navigation

### Requirement: Desktop Wiki layout matches the reference two-region workspace
On desktop, the Wiki workspace SHALL use a persistent 300-340px left navigation rail separated by a quiet divider from a flexible article reader. Each region SHALL scroll independently where needed and SHALL retain stable dimensions while content and status change.

#### Scenario: Index article is long
- **WHEN** the Index content exceeds the viewport height
- **THEN** the reader scrolls without moving the search field, selected navigation entry, or knowledge-base header out of their intended regions

#### Scenario: Processing status updates
- **WHEN** background task counts change
- **THEN** the navigation rail and reader widths do not shift

### Requirement: Wiki workspace provides structured system views
The Wiki workspace SHALL provide dedicated Index and Log views plus recursive folder and page-type navigation within the active knowledge-base scope.

#### Scenario: User opens a populated Wiki
- **WHEN** the Wiki workspace loads
- **THEN** Index is selected by default and displays taxonomy sections, page counts, summaries, and links to generated pages

#### Scenario: User opens Log
- **WHEN** the user selects Log
- **THEN** the workspace displays paginated generation and maintenance events with timestamps, affected pages, source documents, and outcomes

### Requirement: Sidebar controls and hierarchy match the reference workflow
The navigation rail SHALL place Wiki search first, pin Index and Log above a divider, show page-type tabs with counts, provide tree/list icon controls and a new-folder action, and render folders and pages with clear selected, expanded, and loading states.

#### Scenario: Default populated sidebar
- **WHEN** the Wiki contains generated summary, entity, and concept pages
- **THEN** the sidebar displays their available group counts and presents the active group as either a recursive tree or bounded list

#### Scenario: Search mode is active
- **WHEN** the user submits a Wiki search
- **THEN** the hierarchy is replaced by a flat bounded result list with title and summary snippets while the previous tree expansion state is retained

#### Scenario: Search is cleared
- **WHEN** the user clears the query
- **THEN** the prior active page group, view mode, expanded folders, and selected page are restored

### Requirement: Folder organization supports direct manipulation
Authorized users SHALL be able to create root and nested folders, rename folders inline, and move pages or folders through accessible actions and desktop drag-and-drop without losing manual taxonomy ownership.

#### Scenario: User creates a root folder
- **WHEN** an authorized user activates the new-folder icon, enters a valid name, and confirms
- **THEN** the folder appears in the active tree with focused feedback and no full-workspace reload

#### Scenario: User moves a page into a folder
- **WHEN** the user drops a page on a valid folder or selects the equivalent move command
- **THEN** the hierarchy and page taxonomy path update atomically while invalid cyclic or cross-KB moves are rejected

### Requirement: Navigation scales to large Wikis
Folder children, page lists, search results, and logs SHALL use stable cursor or keyset pagination and lazy loading so the workspace does not load the full Wiki graph on initial render.

#### Scenario: Knowledge base has tens of thousands of pages
- **WHEN** the workspace first opens
- **THEN** it requests only system-view summaries and the first bounded navigation page

#### Scenario: User expands a folder
- **WHEN** the user expands an unloaded folder
- **THEN** the workspace fetches that folder's immediate children without resetting existing navigation state

### Requirement: Page reader exposes provenance and relationships
The page reader SHALL render Markdown content, type, status, aliases, taxonomy path, source documents, exact chunk references, backlinks, outbound links, version, and generation timestamp.

#### Scenario: User inspects a generated claim
- **WHEN** the user selects a source or chunk reference
- **THEN** the workspace opens the corresponding document preview at the available source context without leaving the active knowledge base

#### Scenario: User follows an internal link
- **WHEN** the user selects a canonical or aliased Wiki link
- **THEN** the target page opens and navigation history remains usable

### Requirement: Index reader mirrors the generated Wiki overview
The Index reader SHALL use an article title, category marker, maintenance introduction, and semantic sections for summaries, entities, and concepts. Each entry SHALL contain a canonical page link and a bounded one-line description so the generated Wiki can be scanned like the supplied reference.

#### Scenario: Index has several page types
- **WHEN** the Index view contains summary, entity, and concept pages
- **THEN** each type appears under a distinct heading with counts and readable linked entries rather than raw cards or an undifferentiated page list

### Requirement: Graph view follows the WikiBrowser full-workspace model
The Graph view SHALL use an unframed full-workspace canvas with page search, type legend and filters, fit/zoom controls, visible loading and empty states, and a non-blocking page-detail drawer.

#### Scenario: User selects a graph node
- **WHEN** a graph node is selected
- **THEN** a detail drawer shows the page type, version, content preview, and neighbor actions without replacing or dimming the graph canvas

### Requirement: Global issues use a secondary drawer
Pending Wiki issues SHALL be visible as a compact status entry and SHALL open in a secondary drawer so issue review does not displace the selected Index or page article.

#### Scenario: Pending issues exist
- **WHEN** the knowledge base reports pending lint or convergence issues
- **THEN** the sidebar and graph mode expose the issue count and selecting it opens the scoped issue drawer

### Requirement: Wiki processing status is visible and refreshes efficiently
The workspace SHALL display queued, running, retrying, finalizing, completed, failed, and dead-letter Wiki states and poll only while non-terminal work exists.

#### Scenario: Generation is running
- **WHEN** the current knowledge base has non-terminal Wiki tasks
- **THEN** the UI refreshes status and affected views at a bounded interval without resetting the selected page

#### Scenario: All work is terminal
- **WHEN** no non-terminal Wiki task remains
- **THEN** automatic status polling stops

### Requirement: Search and graph navigation are scoped
Users SHALL be able to search Wiki titles, slugs, aliases, summaries, and content and inspect overview or ego link graphs, with every result restricted to the active workspace and knowledge base.

#### Scenario: Search matches an alias
- **WHEN** a query matches a page alias
- **THEN** the canonical page is returned with a bounded contextual snippet

#### Scenario: User opens an ego graph
- **WHEN** the user requests graph context for a page
- **THEN** the API returns only bounded incoming and outgoing neighbors from the same knowledge base

### Requirement: Lint issues and repairs are reviewable
The workspace SHALL expose dead-link, stale-source, duplicate-page, ungrounded-content, and taxonomy issues with filters, evidence, status, and safe repair actions.

#### Scenario: Automated repair changes published content
- **WHEN** a repair would rename, merge, delete, or materially rewrite a published page
- **THEN** the system creates a reviewable proposal unless the action is an explicitly approved deterministic cleanup

#### Scenario: User retries dead-letter work
- **WHEN** an authorized user retries a dead-letter Wiki task
- **THEN** the UI shows the new attempt while retaining the previous failure history

### Requirement: Knowledge-base settings communicate Wiki-only behavior
The creation and settings UI SHALL show Wiki, dense, keyword, and graph indexing as independent controls, provide a Wiki-only preset, and accurately reflect persisted values after save.

#### Scenario: User selects Wiki-only preset
- **WHEN** the preset is selected
- **THEN** the UI enables Wiki and disables dense, keyword, and graph controls while still allowing explicit subsequent changes

### Requirement: Workspace state remains usable across responsive layouts
The Wiki workspace SHALL prevent navigation, page content, status controls, and provenance panels from overlapping, and SHALL preserve access to each region on supported desktop and mobile viewports.

#### Scenario: Narrow viewport
- **WHEN** the Wiki workspace is viewed on a mobile-width viewport
- **THEN** navigation and detail panels become switchable views or drawers without clipping page titles, status, or primary actions
