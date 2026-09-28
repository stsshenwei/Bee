## ADDED Requirements

### Requirement: Cross-Mode Web Search Fallback
The system SHALL attempt `web_search` fallback for every supported chat mode when the mode's internal evidence is insufficient.

#### Scenario: Quick mode falls back to web search
- **WHEN** a `quick` chat request cannot produce sufficient evidence from the selected knowledge-base scope
- **THEN** the system SHALL attempt `web_search` before producing the final answer

#### Scenario: Reasoning mode falls back to web search
- **WHEN** a `reasoning` chat request completes internal knowledge/tool retrieval without sufficient evidence
- **THEN** the system SHALL attempt `web_search` before producing the final answer

#### Scenario: Wiki mode falls back to web search
- **WHEN** a `wiki` chat request cannot produce sufficient evidence from Wiki pages or their scoped source documents
- **THEN** the system SHALL attempt `web_search` before producing the final answer

#### Scenario: RAG plus Wiki mode falls back to web search
- **WHEN** a `rag_wiki` chat request cannot produce sufficient evidence from either raw RAG evidence or Wiki evidence
- **THEN** the system SHALL attempt `web_search` before producing the final answer

### Requirement: Evidence Sufficiency Decision
The system SHALL decide whether fallback is required from a reusable evidence sufficiency decision rather than only checking whether retrieval returned zero records.

#### Scenario: Empty internal evidence triggers fallback
- **WHEN** internal retrieval returns no usable hits or source records
- **THEN** the system SHALL classify internal evidence as insufficient and attempt `web_search`

#### Scenario: Low-confidence internal evidence triggers fallback
- **WHEN** internal retrieval returns records but their confidence, score, citation state, or source availability is below the configured sufficiency threshold
- **THEN** the system SHALL classify internal evidence as insufficient and attempt `web_search`

#### Scenario: Sufficient internal evidence does not trigger fallback
- **WHEN** internal retrieval returns usable evidence that satisfies the configured sufficiency decision
- **THEN** the system SHALL answer from internal evidence and SHALL NOT invoke `web_search` as fallback

### Requirement: Mandatory Web Fallback Disclosure
The system MUST clearly disclose when an answer is based on web-search fallback rather than knowledge-base evidence.

#### Scenario: Web fallback answer begins with required notice
- **WHEN** the final answer is generated from web-search fallback results
- **THEN** the answer text MUST begin with `知识库无答案，以下来自网络搜索`

#### Scenario: Internal answer does not claim web fallback
- **WHEN** the final answer is generated from sufficient internal evidence
- **THEN** the answer text SHALL NOT include the required web fallback notice unless web fallback was actually used

### Requirement: Web Source Metadata
The system SHALL emit and persist web-search fallback sources as source records distinguishable from internal knowledge sources.

#### Scenario: Sources event includes web source records
- **WHEN** web-search fallback returns usable results
- **THEN** the `sources` SSE payload SHALL include source records with `source_type` set to `web`

#### Scenario: Web source records include URL and snippet
- **WHEN** a web result has a title, URL, or snippet from the configured provider
- **THEN** the normalized source record SHALL preserve bounded `source`, `url`, and `snippet` fields

#### Scenario: Old clients remain compatible
- **WHEN** web source records include fields unknown to existing clients
- **THEN** the system SHALL preserve the existing `sources` payload shape as a list of objects and SHALL NOT rename existing stream event keys

### Requirement: Web Fallback Failure Behavior
The system SHALL fail closed when web-search fallback cannot provide usable evidence.

#### Scenario: Web search is unavailable
- **WHEN** internal evidence is insufficient and `web_search` is disabled, unconfigured, or unavailable by server guardrails
- **THEN** the system SHALL return an insufficient-evidence answer that states web search is unavailable and SHALL NOT fabricate an answer

#### Scenario: Web search returns no usable results
- **WHEN** internal evidence is insufficient and `web_search` returns no usable results
- **THEN** the system SHALL return an insufficient-evidence answer that states no usable knowledge-base or web-search evidence was found

#### Scenario: Web search fails
- **WHEN** internal evidence is insufficient and the `web_search` provider times out or raises an error
- **THEN** the system SHALL return an insufficient-evidence answer with safe failure metadata and SHALL NOT expose secrets, raw provider credentials, or private stack traces

### Requirement: Web Results Remain Ephemeral
The system SHALL NOT ingest web-search fallback results into durable knowledge surfaces.

#### Scenario: Web results are not indexed
- **WHEN** web-search fallback is used for a chat answer
- **THEN** the system SHALL NOT write the web results into the knowledge-base corpus, vector store, Wiki pages, knowledge graph, or feedback markdown

#### Scenario: Web results are not stored as memory facts
- **WHEN** memory storage runs after a web-search fallback answer
- **THEN** the system SHALL NOT store web result contents as durable user memory unless a separate explicit memory policy allows it

### Requirement: Fallback Observability And Persistence
The system SHALL record safe metadata about web-search fallback decisions for traceability.

#### Scenario: Fallback metadata is persisted
- **WHEN** web-search fallback is attempted
- **THEN** the assistant message metadata SHALL record that fallback was attempted, whether it succeeded, and the safe trigger reason

#### Scenario: Stream trace reports fallback
- **WHEN** a chat path supports reasoning or agent trace events
- **THEN** the stream SHALL include safe fallback metadata indicating internal evidence was insufficient and web search was attempted

#### Scenario: Replay preserves fallback output
- **WHEN** a client reconnects to a stream after web-search fallback events were emitted
- **THEN** replay SHALL preserve the source, token, final, and terminal event order expected by existing `/chat/stream` clients
