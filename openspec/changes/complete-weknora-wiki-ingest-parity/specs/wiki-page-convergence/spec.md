## ADDED Requirements

### Requirement: Generated Wiki pages are typed and source-grounded
The system SHALL support summary, entity, concept, index, and log pages. Generated factual pages MUST retain contributing document IDs, exact chunk IDs where available, aliases, generation metadata, and a version.

#### Scenario: Summary page is generated
- **WHEN** a source document completes Map and Reduce
- **THEN** its summary page identifies the source document and supporting chunks and is addressable by a stable canonical slug

#### Scenario: Entity page combines sources
- **WHEN** an entity is supported by more than one document
- **THEN** its page exposes all current source references without duplicating the same document or chunk reference

### Requirement: Page updates converge idempotently
Applying the same logical Wiki update more than once SHALL produce the same visible page content, references, aliases, taxonomy, and log state without duplicate revisions or duplicate entries.

#### Scenario: Task acknowledgement is retried
- **WHEN** a completed Reduce operation is delivered again with the same idempotency key
- **THEN** the repository recognizes the committed result and performs no additional page mutation

### Requirement: Reprocessing retracts stale contributions
The system SHALL compare the previous and current Wiki contributions of a document and retract removed slugs, claims, aliases, and source references while preserving contributions from other documents and manual edits.

#### Scenario: Entity disappears after reprocessing
- **WHEN** a new source revision no longer supports an entity previously contributed only by that document
- **THEN** the entity page removes that contribution and is archived or issue-flagged when no grounded content remains

#### Scenario: Other sources still support the page
- **WHEN** one document retracts a concept that remains supported by another document
- **THEN** the concept page remains published with the remaining content and references

### Requirement: Source deletion cannot create ghost references
Wiki commits MUST revalidate document and chunk existence immediately before publication and SHALL clean or reject references that were deleted during long-running LLM work.

#### Scenario: Source is deleted during Reduce
- **WHEN** a source document is deleted after prompt execution but before the page transaction commits
- **THEN** the system does not publish references or claims attributed only to that deleted source

### Requirement: Taxonomy and system pages converge after batch updates
After affected page reductions complete, the system SHALL update taxonomy placement, a structured index page, and an append-only logical activity log for the knowledge base through one debounced finalization task.

#### Scenario: Batch creates new concepts
- **WHEN** a Wiki batch publishes new concept pages
- **THEN** finalization places them in the current taxonomy and updates index sections and counts once for the batch

#### Scenario: Manually organized page exists
- **WHEN** taxonomy planning encounters a page explicitly moved by a user
- **THEN** finalization preserves the manual placement unless the user requests automatic reclassification

### Requirement: Links are normalized and repaired
Finalization SHALL normalize internal links to canonical slugs, resolve aliases, remove links to unavailable pages, and inject bounded cross-links only when both endpoints exist in the same scoped knowledge base.

#### Scenario: One page generation fails
- **WHEN** another generated page links to the failed page slug
- **THEN** finalization removes or renders the unresolved link as plain text instead of publishing a dead Wiki link

#### Scenario: Alias is renamed
- **WHEN** a page's canonical slug changes and its old slug becomes an alias
- **THEN** incoming links continue resolving and can be rewritten to the canonical slug during finalization

### Requirement: Duplicate pages can be detected and safely merged
The system SHALL identify likely duplicate entity or concept pages using normalized titles, aliases, links, and optional LLM review, and SHALL require a deterministic merge plan before changing canonical slugs.

#### Scenario: Duplicate candidates are confirmed
- **WHEN** two pages are confirmed to describe the same subject
- **THEN** the merge preserves source references, manual content, aliases, backlinks, revision history, and a recoverable redirect from the retired slug

### Requirement: Generated pages become browsable after successful convergence
Grounded pages produced by automatic Wiki ingest SHALL become visible in the Wiki workspace after successful reduction and validation. Manual or agent-authored mutations SHALL continue to use draft or proposal review unless explicitly approved.

#### Scenario: Automatic batch succeeds
- **WHEN** generated pages pass grounding and repository validation
- **THEN** they are published atomically per page and appear in index, search, and navigation views

### Requirement: Wiki pages do not silently enter the raw-document vector index
The system SHALL keep Wiki page storage and search separate from raw document vector records by default. Any future Wiki-page embedding option MUST be explicit, separately identifiable, and reversible.

#### Scenario: Wiki-only finalization completes
- **WHEN** pages are published for a Wiki-only knowledge base
- **THEN** no raw-document vector chunks are created for those pages and Wiki search remains available through scoped page APIs and tools

