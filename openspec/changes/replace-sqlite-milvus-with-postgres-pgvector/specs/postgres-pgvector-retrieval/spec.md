## ADDED Requirements

### Requirement: Chunk vectors are stored and searched with pgvector
The system SHALL store indexable child, table, OCR, image OCR, and image caption chunk embeddings in PostgreSQL using pgvector-compatible columns and SHALL search them with workspace, knowledge-base, and optional document filters before ranking.

#### Scenario: Ingest writes pgvector rows
- **WHEN** a document is ingested for a knowledge base whose effective indexing strategy enables dense retrieval
- **THEN** PostgreSQL SHALL persist embeddings for each indexable chunk with chunk ID, document ID, parent ID, chunk type, workspace ID, knowledge base ID, title path, page metadata, and vector-search text metadata

#### Scenario: Dense search is scoped before ranking
- **WHEN** dense retrieval runs for a scoped query
- **THEN** PostgreSQL SHALL filter candidate vector rows by workspace, knowledge base, and selected document IDs before returning top-k vector results

### Requirement: Embedding dimension is explicit and validated
The system SHALL record and validate the configured embedding dimension and PostgreSQL vector type before ingesting or querying vectors.

#### Scenario: Dimension matches schema
- **WHEN** the configured embedding provider returns vectors matching the PostgreSQL vector column type and dimension
- **THEN** ingest and dense retrieval SHALL proceed normally

#### Scenario: Dimension mismatch
- **WHEN** the configured embedding provider returns vectors incompatible with the PostgreSQL vector column type or dimension
- **THEN** the system SHALL fail closed before writing vectors and SHALL NOT truncate, pad, or coerce embeddings silently

### Requirement: Keyword retrieval uses PostgreSQL text and trigram search
The system SHALL replace SQLite FTS5 and Milvus BM25 production keyword retrieval with PostgreSQL-backed keyword retrieval using text search plus trigram or exact-term matching for technical and Chinese corpus text.

#### Scenario: Exact technical term recall
- **WHEN** a query contains a model number, version string, command-line flag, configuration key, error code, or API identifier present in a chunk
- **THEN** PostgreSQL keyword retrieval SHALL be able to return that chunk even if dense retrieval does not rank it highly

#### Scenario: Chinese fragment recall
- **WHEN** a Chinese query fragment appears in an indexed chunk but PostgreSQL tokenization does not isolate it as a full-text lexeme
- **THEN** trigram or exact substring fallback SHALL be considered by keyword retrieval before returning the final keyword candidate list

### Requirement: Hybrid retrieval preserves existing evidence semantics
The system SHALL preserve hybrid retrieval semantics: query understanding, dense fan-out, keyword fan-out, RRF fusion, chunk ID deduplication, optional reranking, parent/table/OCR context assembly, and source metadata extraction.

#### Scenario: Dense and keyword hit same chunk
- **WHEN** PostgreSQL dense retrieval and PostgreSQL keyword retrieval return the same chunk
- **THEN** the fused candidate list SHALL contain one candidate for that chunk and SHALL preserve vector score, keyword score, hybrid score, matched retrieval query metadata, and reranker score when present

#### Scenario: Parent recall from PostgreSQL
- **WHEN** a child, table, OCR, image OCR, or image caption chunk is selected as evidence
- **THEN** parent recall and final context assembly SHALL load authoritative content and metadata from PostgreSQL document chunk rows

### Requirement: KG entity vectors use PostgreSQL pgvector
The system SHALL replace Milvus-backed entity vector similarity with PostgreSQL pgvector-backed entity vector storage and search.

#### Scenario: Entity similarity search is scoped
- **WHEN** KG entity resolution searches for similar entities within a knowledge-base scope
- **THEN** PostgreSQL entity vector search SHALL only return entities belonging to that workspace and selected knowledge bases

### Requirement: Milvus is not required for retrieval
The system SHALL not require a Milvus server or `pymilvus` package for production ingest, dense retrieval, keyword retrieval, entity vector search, or clean startup.

#### Scenario: Milvus unavailable
- **WHEN** PostgreSQL is healthy and Milvus is absent or unreachable
- **THEN** ingest, retrieval, Wiki, KG metadata, and entity vector search SHALL remain available according to PostgreSQL-backed configuration
