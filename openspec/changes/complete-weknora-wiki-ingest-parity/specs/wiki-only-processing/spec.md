## ADDED Requirements

### Requirement: Indexing strategy controls every processing stage
The system SHALL use the persisted knowledge-base indexing strategy as the source of truth for dense indexing, keyword indexing, graph extraction, and Wiki generation. Knowledge-base type MAY select defaults and presentation, but SHALL NOT independently force a disabled processing stage to run.

#### Scenario: Wiki-only strategy skips retrieval indexes
- **WHEN** a document is uploaded to a knowledge base with Wiki enabled and both dense and keyword indexing disabled
- **THEN** the system persists parsed chunks and starts Wiki processing without calling the embedding or vector-index write path

#### Scenario: Combined Wiki strategy retains retrieval indexes
- **WHEN** a document is uploaded with Wiki and dense or keyword indexing enabled
- **THEN** the system runs the enabled retrieval indexing stages and also schedules Wiki processing

#### Scenario: Wiki is disabled
- **WHEN** a document is uploaded with Wiki generation disabled
- **THEN** the system does not schedule a Wiki ingest task regardless of the knowledge-base display type

### Requirement: Wiki-only uploads retain authoritative source evidence
The system SHALL persist document metadata, parsed parent chunks, table/OCR enrichment, and stable chunk identifiers before scheduling Wiki generation, even when all retrieval indexes are disabled.

#### Scenario: Evidence is available without vectors
- **WHEN** a Wiki-only document completes parsing
- **THEN** its chunks can be loaded by document ID for Wiki generation, source drilldown, and citation verification without a vector-store record

#### Scenario: Parsing fails
- **WHEN** a document cannot produce valid source chunks
- **THEN** the system marks document processing failed and does not enqueue Wiki generation

### Requirement: Wiki creation uses an explicit Wiki-only preset
The knowledge-base creation flow SHALL offer a Wiki-only preset with Wiki enabled and dense, keyword, and graph indexing disabled, while allowing users to explicitly enable combined indexing.

#### Scenario: New Wiki knowledge base
- **WHEN** a user creates a knowledge base using the Wiki preset without changing advanced indexing settings
- **THEN** the persisted strategy has Wiki enabled and dense and keyword indexing disabled

#### Scenario: User enables hybrid behavior
- **WHEN** a user enables dense or keyword indexing before creating the Wiki knowledge base
- **THEN** the explicitly selected strategy is persisted and displayed unchanged

### Requirement: Existing indexing choices are migration-safe
The system MUST preserve existing dense, keyword, and graph choices during migration and SHALL NOT silently convert existing Wiki-type knowledge bases to Wiki-only retrieval behavior.

#### Scenario: Existing Wiki type has vectors enabled
- **WHEN** migration encounters an existing Wiki-type knowledge base with dense or keyword indexing enabled
- **THEN** those settings remain enabled while the migration repairs only inconsistent Wiki enablement metadata

### Requirement: Disabled stages remain observable
The processing timeline SHALL represent disabled dense, keyword, graph, and Wiki stages as skipped with a machine-readable reason instead of reporting them as successful work.

#### Scenario: Embedding is disabled
- **WHEN** both dense and keyword indexing are disabled
- **THEN** the embedding stage records `skipped` with a reason derived from the indexing strategy and records no vector chunk count

### Requirement: Document completion waits for required Wiki work
A document with Wiki generation enabled SHALL remain in a finalizing state until its required Wiki ingest work completes, reaches a terminal dead-letter state, or is explicitly cancelled.

#### Scenario: Wiki task is pending
- **WHEN** parsing succeeds and a Wiki ingest task is queued
- **THEN** the upload API reports the document as finalizing rather than fully completed

#### Scenario: Wiki task drains
- **WHEN** all required Wiki tasks for the document complete successfully
- **THEN** the document transitions to completed and its Wiki pages are available for browsing

