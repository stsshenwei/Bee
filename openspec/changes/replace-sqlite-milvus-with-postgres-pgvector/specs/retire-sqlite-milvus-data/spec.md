## ADDED Requirements

### Requirement: SQLite and Milvus production data are retired
The system SHALL treat existing SQLite metadata/evaluation databases, SQLite FTS5 indexes, and Milvus collections as retired production data after the PostgreSQL cutover.

#### Scenario: Old SQLite files exist
- **WHEN** old SQLite metadata, evaluation, WAL, or SHM files exist in the workspace after PostgreSQL cutover
- **THEN** normal backend startup SHALL NOT open them as production stores and SHALL NOT merge their rows into PostgreSQL implicitly

#### Scenario: Old Milvus collections exist
- **WHEN** old Milvus `rag_chunk_vectors` or `kg_entity_vectors` collections exist after PostgreSQL cutover
- **THEN** normal backend startup SHALL NOT query or write those collections for production retrieval or KG entity similarity

### Requirement: Clean rebuild is the only supported cutover path
The system SHALL require the protected clean-rebuild flow to initialize PostgreSQL production storage and retire SQLite/Milvus production artifacts.

#### Scenario: Dry-run cutover plan
- **WHEN** an operator runs clean-rebuild in dry-run mode for the PostgreSQL cutover
- **THEN** the command SHALL list the PostgreSQL initialization target, retired SQLite artifacts, retired Milvus collections if configured, generated state/report artifacts, managed source cleanup choices, and any optional backup targets without modifying data

#### Scenario: Confirmed cutover
- **WHEN** writers are stopped and the operator provides the exact confirmation phrase for the PostgreSQL cutover
- **THEN** clean-rebuild SHALL initialize PostgreSQL final schema, record a reset manifest, and only then report the system ready for re-ingest

### Requirement: No implicit production data migration
The system SHALL NOT automatically migrate existing SQLite rows or Milvus vector records into PostgreSQL during startup, ingest, retrieval, or clean-rebuild.

#### Scenario: Startup with old data and empty PostgreSQL
- **WHEN** PostgreSQL has no initialized production schema but old SQLite/Milvus data is present
- **THEN** startup SHALL require clean-rebuild and SHALL NOT infer or import production rows from old stores

#### Scenario: Re-ingest after cutover
- **WHEN** PostgreSQL clean-rebuild has completed
- **THEN** operators SHALL rebuild retrievable knowledge by re-ingesting trusted source files, uploads, feedback files, or explicit future import artifacts rather than by implicit SQLite/Milvus migration

### Requirement: Backups are explicit and external to normal startup
The system SHALL make backup or export of retired SQLite/Milvus production data an explicit operator action outside normal application startup.

#### Scenario: Optional backup requested
- **WHEN** clean-rebuild is run with an approved backup destination
- **THEN** the command SHALL include eligible retired files and metadata in the backup plan and SHALL refuse backup paths that overlap destructive targets

#### Scenario: No backup requested
- **WHEN** clean-rebuild is run without a backup destination
- **THEN** the command SHALL still require dry-run review and exact confirmation before destructive retirement actions

### Requirement: Rollback uses complete backups only
The system SHALL support rollback only by restoring a complete pre-cutover SQLite/Milvus application backup set with old application code or by restoring a PostgreSQL backup taken after cutover.

#### Scenario: Mixed rollback attempt
- **WHEN** an operator attempts to point new PostgreSQL-backed application code at old SQLite/Milvus production data or old application code at the new PostgreSQL schema
- **THEN** the system SHALL fail closed or report unsupported storage configuration rather than running with mixed storage generations
