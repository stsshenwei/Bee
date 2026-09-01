## ADDED Requirements

### Requirement: PostgreSQL is the active production store
The system SHALL use PostgreSQL as the active production store for knowledge-base metadata, documents, chunks, processing tasks, processing spans, Wiki state, audit rows, memory rows, KG metadata, image metadata, upload batches, and evaluation rows.

#### Scenario: Startup uses PostgreSQL
- **WHEN** the backend starts with `DATABASE_URL` configured and the PostgreSQL schema version matches the application
- **THEN** all production repositories SHALL connect to PostgreSQL and no production repository SHALL open SQLite metadata or evaluation databases

#### Scenario: Missing PostgreSQL configuration
- **WHEN** the backend starts without a usable `DATABASE_URL`
- **THEN** startup SHALL fail closed with an actionable configuration error before ingest, retrieval, Wiki, processing, memory, or evaluation routes are served

### Requirement: PostgreSQL schema initializes required extensions and final tables
The system SHALL initialize PostgreSQL only from an empty or explicitly reset database and SHALL create the required extensions, schema version row, tables, constraints, and indexes for the final production schema.

#### Scenario: Empty database initialization
- **WHEN** the protected clean-rebuild flow initializes an empty PostgreSQL database
- **THEN** it SHALL create `vector` and `pg_trgm` extensions, write the expected storage schema version, create all production tables, and create required scope, foreign-key, text-search, trigram, and vector indexes

#### Scenario: Incompatible schema version
- **WHEN** PostgreSQL contains production tables with a missing or incompatible storage schema version
- **THEN** normal startup SHALL report reset-required or fail closed and SHALL NOT auto-alter, auto-drop, or partially repair production tables

### Requirement: Scope ownership is enforced in PostgreSQL
The system SHALL preserve workspace and knowledge-base ownership constraints for all authoritative rows and derived retrieval rows in PostgreSQL.

#### Scenario: Cross-KB chunk lookup
- **WHEN** a caller requests a chunk by ID using a scope that does not include the chunk's workspace and knowledge base
- **THEN** PostgreSQL repository methods SHALL return no chunk and citation verification SHALL treat the reference as unusable

#### Scenario: Multi-KB retrieval scope
- **WHEN** a query specifies multiple active knowledge base IDs in one workspace
- **THEN** PostgreSQL dense, keyword, hydration, parent recall, Wiki source drilldown, and citation verification queries SHALL restrict rows to that workspace and those knowledge bases before returning evidence

### Requirement: Repository contracts remain stable for service layers
The system SHALL preserve the service-facing repository method contracts used by route handlers, `RAGService`, processing workers, Wiki services, memory services, KG services, and evaluation services while replacing their persistence implementation with PostgreSQL.

#### Scenario: Existing service flow
- **WHEN** document upload, parsing, chunk persistence, Wiki ingest, feedback write-back, memory update, or evaluation replay calls an existing repository method
- **THEN** the method SHALL provide equivalent domain results using PostgreSQL without requiring route handlers to know SQL dialect details

### Requirement: PostgreSQL task claiming is durable and concurrent-safe
The system SHALL use PostgreSQL transactions and row-level locking for durable task claiming, retry scheduling, cancellation, dead-lettering, and span updates.

#### Scenario: Competing workers claim tasks
- **WHEN** two processing workers poll runnable tasks concurrently
- **THEN** each runnable task SHALL be claimed by at most one worker and the other worker SHALL skip locked or already-claimed rows

#### Scenario: Failed task retry
- **WHEN** a PostgreSQL-backed task fails with remaining attempts
- **THEN** the task SHALL persist the attempt count, last error, next run time, and lease release in a transaction
