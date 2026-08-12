## Why

Wiki knowledge bases currently retain document chunks but still run dense and keyword indexing, then produce only a deterministic summary draft inside the upload task. This does not match the intended WeKnora-style Wiki workflow: Wiki-only uploads should skip embeddings while durable LLM post-processing builds source-grounded summary, entity, concept, index, and log pages with observable progress.

## What Changes

- Make indexing strategy, rather than knowledge-base type alone, the source of truth for chunk persistence, embedding, keyword indexing, graph extraction, and Wiki generation.
- Add a Wiki-only preset that stores parsed chunks as evidence while disabling dense and keyword indexing and enabling Wiki generation.
- Move Wiki generation out of synchronous upload processing into durable, retryable, rate-limit-aware `wiki.ingest` and `wiki.finalize` tasks.
- Replace deterministic summary-only generation with a bounded LLM Map/Reduce pipeline for candidate extraction, document summaries, chunk classification, and per-slug page reduction.
- Generate and converge summary, entity, concept, index, and log pages with source document/chunk references, aliases, taxonomy placement, stale-source retraction, deduplication, dead-link cleanup, and cross-links.
- Expose hierarchical `postprocess.wiki`, `postprocess.wiki.extract`, `postprocess.wiki.summary`, `postprocess.wiki.classify`, and `postprocess.wiki.page[...]` spans and keep document processing in a finalizing state until required Wiki work drains.
- Rebuild the Wiki workspace around the WeKnora WikiBrowser information architecture shown in the reference: knowledge-base breadcrumb and `Documents / Wiki / Graph` tabs, a persistent left navigation rail with search and pinned Index/Log entries, typed page groups with counts, tree/list modes and folder actions, and a spacious Markdown reader for structured index and page content.
- Add the companion Wiki interactions needed for that interface: lazy directory expansion, create/rename/move folders, full-width graph mode, generation status, source/backlink navigation, global issue drawer, lint results, and safe repair actions.
- Preserve raw chunks as the authoritative evidence layer; Wiki pages remain database/tool-backed by default and are not silently added to the document vector index.

## Capabilities

### New Capabilities

- `wiki-only-processing`: Strategy-driven upload behavior that persists evidence chunks while selectively skipping vector, keyword, graph, or Wiki stages.
- `wiki-ingest-orchestration`: Durable, observable, bounded LLM Map/Reduce processing for turning uploaded documents into grounded Wiki page updates.
- `wiki-page-convergence`: Idempotent page reduction, taxonomy, index/log maintenance, source retraction, deduplication, linking, and publication convergence.
- `wiki-workspace-navigation`: A WeKnora-style WikiBrowser experience matching the reference layout and workflows, including the knowledge tabs, persistent navigation rail, Index/Log views, typed trees and counts, article reader, graph mode, provenance, status, lint, and repair workflows.

### Modified Capabilities

None. No main specs are currently registered under `openspec/specs/`; this change introduces explicit contracts for behavior that the earlier unarchived Wiki change did not fully deliver.

## Impact

- Backend processing and persistence: knowledge-base indexing strategy resolution, `RAGService`, processing worker/task repository, Wiki repository/service, processing spans, and SQLite migrations.
- LLM integration: Wiki prompt catalog, structured response validation, bounded concurrency, token/input limits, retry and fallback policy.
- APIs: Wiki task status, paginated hierarchy/index/log data, page provenance/backlinks, lint issues, repair actions, and consistent indexing-strategy payloads.
- Frontend: knowledge-base creation defaults and a substantial WikiBrowser-style workspace redesign in `frontend/app/knowledge/page.tsx`, with supporting API types, components where needed, and global styles responsive to desktop and mobile layouts.
- Operations: worker enablement, Wiki batch/concurrency/debounce/time-out settings, resumability, dead-letter visibility, and migration handling for existing Wiki knowledge bases.
- Documentation and tests: architecture, RAG pipeline, frontend Wiki design, strategy compatibility, failure recovery, source-grounding, and end-to-end upload validation.
