## Context

The current FastAPI/Next.js application already stores document chunks, processing tasks, Wiki pages, folders, issues, proposals, and generation task records in SQLite. Upload processing nevertheless calls vector upsert unconditionally, runs Wiki generation inline, and creates one deterministic summary draft. The knowledge-base wizard can persist `type=wiki` together with `wiki_enabled=false`, so type and indexing strategy currently disagree.

The WeKnora source demonstrates the desired behavior: chunks are always retained, embedding is conditional on vector/keyword strategy, Wiki generation is queued after parsing, Map work extracts document contributions, Reduce work converges pages by slug, and a later knowledge-base finalizer updates taxonomy, index, log, and links. Its Go/PostgreSQL/Redis implementation is a behavioral reference, not a code dependency; this project must adapt the workflow to Python, FastAPI, SQLite, the existing processing worker, prompt catalog, and React workspace.

## Goals / Non-Goals

**Goals:**

- Make Wiki-only upload avoid vectorization while retaining source chunks.
- Produce grounded summary, entity, concept, index, and log pages through durable LLM work.
- Make retries, reprocessing, deletion, and partial failure converge without duplicate or ghost content.
- Expose the full Wiki hierarchy in processing traces and user-facing task status.
- Deliver a scalable Wiki browser that visibly follows the supplied WeKnora layout and includes its core navigation, reading, graph, status, and maintenance workflows.
- Preserve compatibility for existing knowledge bases and existing raw-document retrieval.

**Non-Goals:**

- Do not copy WeKnora Go/Vue code or introduce its PostgreSQL, Redis, or Asynq dependencies.
- Do not replace source chunks with generated Wiki pages as citation authority.
- Do not automatically vectorize Wiki pages in the raw-document collection.
- Do not add collaborative rich-text editing or external Wiki datasource synchronization.
- Do not enable unrestricted agent mutation of published pages.

## Decisions

### 1. Indexing strategy is the processing source of truth

`IndexingStrategy` controls stage execution. `type=wiki` becomes a product preset and capability label, not an alternate hidden switch. A new Wiki preset persists `wiki_enabled=true`, `dense_enabled=false`, `keyword_enabled=false`, and `graph_enabled=false`; users may then enable combined modes explicitly.

Chunk persistence remains unconditional after successful parsing because Wiki generation and source drilldown require stable evidence. Embedding/vector writes run only when dense or keyword retrieval is enabled. Graph and Wiki post-processing are independently gated. Disabled spans are recorded as skipped.

Alternative: infer all behavior from `type=wiki`. Rejected because document knowledge bases may enable Wiki and Wiki knowledge bases may intentionally combine vector retrieval.

### 2. Reuse the existing task table and worker with typed handlers

Extend the processing worker from one upload handler into a small typed dispatcher supporting at least:

```text
upload_file.process
wiki.ingest
wiki.finalize
```

Each task carries a schema-versioned payload, knowledge-base scope, idempotency key, available-at time, lease owner/expiry, attempt count, source revision, and parent/root trace IDs. Existing lease/retry/dead-letter behavior is reused and generalized. Wiki task claims use compare-and-swap updates; page commits use unique constraints and optimistic versions so correctness does not depend on a process-local lock.

Alternative: run Wiki generation in the upload worker or HTTP request. Rejected because observed jobs can take tens of minutes and must survive restarts, rate limits, and partial failures.

### 3. Separate document Map, slug Reduce, and knowledge-base finalization

`wiki.ingest` debounces pending documents per knowledge base and claims a bounded batch. Each document Map does the following:

1. Load current parent/table/OCR chunks and verify source revision.
2. Reconstruct bounded enriched source text with chunk markers.
3. Run candidate slug extraction.
4. Run document summary and bounded chunk classification in parallel.
5. Validate structured outputs and emit summary/entity/concept contribution records.
6. Compare against the document's previous contribution manifest to produce additions, replacements, and retractions.

Reduce groups contribution records by canonical slug. Distinct slugs run concurrently within global and per-KB limits; the same page commits through version-checked transactions. A page-modify prompt receives current grounded content, additions, and retractions. It cannot remove manual sections unless a repair proposal explicitly targets them.

After all available slug reductions, one debounced `wiki.finalize` task updates taxonomy, index, log, dead links, aliases, and cross-links. This avoids rebuilding KB-global material for every page.

Alternative: one prompt generates a complete Wiki from a full knowledge base. Rejected because it is unbounded, non-resumable, expensive, and unable to attribute failures per document or page.

### 4. Use structured prompts with conservative fallbacks

Extend the existing prompt catalog with versioned templates for candidate extraction, summary, chunk classification/citation, page merge, taxonomy planning, index intro, deduplication, and link repair. Responses use explicit schemas and normalized identifiers. Invalid extraction may use a legacy combined extractor once; invalid page output never replaces a valid published page.

Configuration includes maximum source characters, candidates and pages per document, classification batch size, Map/Reduce concurrency, per-KB inflight count, debounce windows, timeout, retry budget, and rate-limit backoff. Defaults remain conservative for SQLite and a single worker process.

### 5. Persist contribution manifests for deterministic retraction

Add or extend records for:

```text
wiki_document_contribution(
  knowledge_base_id, document_id, document_revision,
  page_slug, page_type, contribution_json,
  source_chunk_ids_json, generation_run_id, content_hash,
  active, created_at, updated_at
)

wiki_ingest_pending(
  knowledge_base_id, document_id, document_revision,
  operation, state, task_id, available_at, last_error,
  created_at, updated_at
)
```

`wiki_page` gains page types for `index` and `log` if absent, generation metadata, provenance/version fields, and explicit manual-content markers. A contribution manifest, rather than diffing generated Markdown, is the authority for reprocessing and deletion retractions.

Alternative: derive old contributions from page text and source JSON. Rejected because merged prose cannot reliably identify which claims belong to one source.

### 6. Publish automatic grounded output, review human-impacting repairs

Successful automatic ingest publishes each validated generated page version so users see Wiki results after upload. Existing published content remains visible until its replacement transaction succeeds. Manual edits are preserved as protected sections or revisions. Agent-authored edits and material repairs such as merge, rename, delete, or broad rewrite create proposals by default.

This intentionally differs from the previous deterministic draft-only behavior and matches the requested upload-to-Wiki experience.

### 7. Keep raw and Wiki retrieval layers distinct

Wiki page search uses scoped SQLite queries and, where available, a dedicated FTS index over title, slug, aliases, summary, and content. Raw chunk vector/keyword records remain separately identifiable and authoritative for exact citations. No Wiki page is inserted into the raw document vector collection unless a future explicit strategy adds a separate page index.

### 8. Model post-processing as one trace tree

The upload trace owns a post-processing root. Durable Wiki tasks continue that trace through persisted trace context and create:

```text
postprocess.wiki
  postprocess.wiki.extract
  postprocess.wiki.summary
  postprocess.wiki.classify
  postprocess.wiki.page[summary:<slug>]
  postprocess.wiki.page[entity:<slug>]
  postprocess.wiki.page[concept:<slug>]
  postprocess.wiki.page[index:<slug>]
```

Task and span state are related but separate: task leases support execution recovery, while spans explain timing and failures. Document status remains `finalizing` while required Wiki pending rows are non-terminal.

### 9. Add bounded APIs tailored to the Wiki workspace

Avoid an initial endpoint that returns every page, folder, issue, task, and graph edge. Add scoped endpoints for system-view summaries, folder children, keyset-paginated pages/logs, search, page detail/provenance, backlinks, bounded graph neighborhoods, task status, lint issues, and repair proposals.

The React workspace keeps stable selected-page and expansion state while polling only non-terminal task status. Desktop uses persistent navigation and reader regions; narrow layouts expose the same regions as switchable views or drawers. Existing styling and icon libraries remain in use.

### 10. Reproduce the WeKnora WikiBrowser information architecture

The supplied WikiBrowser is the interaction and visual-layout reference. The implementation SHALL adapt it to the existing Next.js shell and design tokens instead of copying Vue code or WeKnora branding.

The knowledge-base header remains a compact full-width band containing the breadcrumb, knowledge-base name, description, and primary `Documents / Wiki / Graph` navigation. Selecting Wiki opens an unframed two-region workspace below that header:

```text
+-----------------------+---------------------------------------------+
| Search Wiki pages     | Page title                                  |
| Index                 | Type/status metadata                         |
| Log                   |                                             |
|-----------------------| Markdown article / structured Index          |
| Knowledge  N Summary N|                                             |
| tree/list  new folder |                                             |
| folders and pages     |                                             |
+-----------------------+---------------------------------------------+
```

On desktop the navigation rail has a stable 300-340px width, its own scrolling region, and a quiet right divider rather than a floating card. Index and Log remain pinned above the page hierarchy. The hierarchy exposes page-type tabs with counts, tree/list segmented icon controls, folder creation, inline rename, drag-and-drop move, lazy child loading, and clear selected/expanded states. Search temporarily replaces the hierarchy with a bounded flat result list and returns to the preserved tree state when cleared.

The reader uses normal article-scale typography rather than dashboard-card headings. The Index view renders semantic sections such as summaries, entities, and concepts with canonical links and short descriptions, matching the reference screenshot. Page views expose provenance and relationships without crowding the primary article; secondary details use drawers or collapsible regions. Graph is a full-width workspace mode with search, type legend/filtering, zoom controls, an optional page-detail drawer, and no decorative card around the canvas.

Loading, empty, error, finalizing, and partially generated states retain stable workspace geometry. On narrow viewports, navigation, reader, and secondary details become switchable panels while the current page and back navigation are preserved.

Alternative: retain the current all-in-one Wiki list with review panels. Rejected because it cannot express the requested index-first browsing model, scales poorly, and does not resemble the supplied Wiki experience.

## Risks / Trade-offs

- [LLM output can introduce unsupported claims] -> Require chunk-grounded contribution records, structured validation, source drilldown, and issue reporting; never replace a valid page on invalid output.
- [SQLite write contention under Reduce concurrency] -> Use short transactions, optimistic versions, unique constraints, low default concurrency, jittered retries, and no LLM calls inside transactions.
- [Long jobs can leave documents finalizing] -> Add leases, heartbeats, retry/dead-letter state, cancellation, operator retry, and explicit terminal failure reporting.
- [Reprocessing can erase manual work] -> Separate generated contributions from protected manual revisions and retract only source-owned contributions.
- [Taxonomy and link finalization can become expensive] -> Debounce per KB, operate on affected slugs where possible, and paginate global scans.
- [Automatic publishing changes prior draft semantics] -> Publish only validated ingest output; retain proposal review for manual/agent edits and provide version history for rollback.
- [Existing Wiki KB settings are inconsistent] -> Preserve dense/keyword/graph values, repair Wiki enablement explicitly, and display the resulting strategy before reprocessing.

## Migration Plan

1. Add backward-compatible schema fields/tables, indexes, page types, and repository methods; keep new task handlers disabled.
2. Backfill `wiki_enabled=true` for existing `type=wiki` records without changing their dense, keyword, or graph flags. Record migration results for operator review.
3. Change new-Wiki defaults in backend and frontend to the Wiki-only preset, then enforce strategy-based stage gating and skipped-span reporting.
4. Add typed worker dispatch, pending/contribution persistence, Map/Reduce prompts, trace continuation, and finalization behind a feature flag.
5. Enable generation for a test KB, regenerate deterministic summary pages into versioned grounded pages, and compare source references and task telemetry.
6. Add bounded Wiki APIs and migrate the workspace from eager aggregate loading to lazy system, folder, page, status, and issue requests.
7. Enable the worker and Wiki ingest by default after backend, recovery, and end-to-end UI tests pass.

Rollback disables new Wiki task enqueueing and typed handlers while preserving pages, contributions, and task history. Existing raw document retrieval remains available for knowledge bases whose dense or keyword settings were already enabled. Schema additions remain inert and do not require destructive rollback.

## Open Questions

- Production defaults for batch size, Map/Reduce concurrency, and debounce duration require calibration against the configured LLM provider and representative Chinese technical documents.
- Dedicated Wiki FTS5 creation can ship in this change if the deployed SQLite build supports it; otherwise bounded indexed substring search remains the compatibility fallback.
