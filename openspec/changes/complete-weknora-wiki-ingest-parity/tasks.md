## 1. Baseline and Compatibility

- [x] 1.1 Add focused regression tests that demonstrate current Wiki-type uploads still enter vector indexing and record the expected failing assertions for Wiki-only behavior.
- [x] 1.2 Add a migration test fixture for existing `type=wiki` knowledge bases with inconsistent `wiki_enabled`, dense, keyword, and graph settings.
- [x] 1.3 Define one backend strategy resolver that returns the effective dense, keyword, graph, and Wiki stage decisions for a scoped knowledge base.
- [x] 1.4 Update backend knowledge-base creation defaults so the Wiki preset persists Wiki enabled with dense, keyword, and graph disabled.
- [x] 1.5 Add a backward-compatible migration that repairs Wiki enablement for existing Wiki-type records without changing their existing dense, keyword, or graph values.
- [x] 1.6 Add API serialization tests proving persisted indexing strategy round-trips without type-based mutation.

## 2. Strategy-Driven Document Processing

- [x] 2.1 Refactor document processing to persist parsed chunks before any optional retrieval or Wiki stage executes.
- [x] 2.2 Gate embedding and vector-store writes on effective dense or keyword indexing and remove unconditional vector upsert behavior.
- [x] 2.3 Gate graph extraction and Wiki enqueueing independently from dense and keyword indexing.
- [x] 2.4 Record disabled embedding, graph, and Wiki stages as skipped spans with machine-readable strategy reasons.
- [x] 2.5 Prevent vector chunk accounting and cleanup code from fabricating vector records for Wiki-only documents.
- [x] 2.6 Add service tests for Wiki-only, vector-only, keyword-only, graph-only, and combined strategies.
- [x] 2.7 Keep Wiki-enabled documents in finalizing state while required Wiki work is pending and transition them on terminal task outcomes.

## 3. Typed Durable Task Runtime

- [x] 3.1 Generalize the processing worker into a typed handler registry while preserving `upload_file.process` behavior.
- [x] 3.2 Extend task payload persistence with schema version, idempotency key, source revision, scope, parent trace context, and available-at scheduling.
- [x] 3.3 Implement handler registration and payload validation for `wiki.ingest` and `wiki.finalize`.
- [x] 3.4 Add compare-and-swap task claiming, lease renewal, retry scheduling, cancellation, and dead-letter transitions for Wiki tasks.
- [x] 3.5 Add per-knowledge-base pending Wiki operations with configurable initial debounce, follow-up delay, and bounded batch claiming.
- [x] 3.6 Ensure a worker restart can reclaim expired Wiki tasks without duplicating acknowledged work.
- [x] 3.7 Add operator-safe retry and cancellation service methods that preserve previous attempt history.
- [x] 3.8 Add worker tests for lease expiry, rate-limit backoff, cancellation, retry exhaustion, dead-letter retry, and duplicate delivery.

## 4. Wiki Contribution Persistence

- [x] 4.1 Add SQLite schema and indexes for document contribution manifests and pending Wiki ingest operations.
- [x] 4.2 Extend Wiki page types and validation to support `index` and `log` system pages.
- [x] 4.3 Add generation run, source revision, idempotency, provenance, version, and protected manual-content metadata required by convergence.
- [x] 4.4 Implement scoped repository CRUD for pending operations and contribution manifests.
- [x] 4.5 Implement optimistic page upsert with canonical slug uniqueness and committed-idempotency lookup.
- [x] 4.6 Add repository queries for additions, replacements, retractions, orphaned contributions, and affected page slugs.
- [x] 4.7 Add migration and repository tests for scope isolation, unique constraints, rollback, and legacy deterministic summary pages.

## 5. Wiki Map Pipeline

- [x] 5.1 Add versioned prompt-catalog entries for candidate extraction, document summary, chunk citation/classification, and conservative combined extraction fallback.
- [x] 5.2 Define structured response models and canonical slug normalization for summary, entity, concept, citation, and classification outputs.
- [x] 5.3 Implement bounded source reconstruction from current parent, table, and OCR chunks with stable chunk markers.
- [x] 5.4 Implement insufficient-source detection that skips LLM work without producing empty pages.
- [x] 5.5 Implement candidate extraction with validation, retry policy, deterministic truncation, and one conservative fallback path.
- [x] 5.6 Run summary generation and bounded chunk classification concurrently after candidate extraction.
- [x] 5.7 Convert validated Map output into typed contribution additions and exact source document/chunk references.
- [x] 5.8 Compare Map output with the prior document manifest to produce additions, replacements, and retractions.
- [x] 5.9 Reject stale Map results when the document revision changed or the source was deleted.
- [x] 5.10 Add Map tests for Chinese technical documents, tables/OCR, invalid JSON, duplicate candidates, limits, sparse text, and stale revisions.

## 6. Per-Slug Reduce Pipeline

- [x] 6.1 Add versioned page-merge prompts that accept current grounded content, additions, retractions, aliases, and protected manual sections.
- [x] 6.2 Group batch contributions by page type and canonical slug before Reduce.
- [x] 6.3 Execute distinct slugs with configurable global and per-KB concurrency while serializing conflicting version commits.
- [x] 6.4 Implement creation and merge behavior for summary, entity, and concept pages with source and chunk deduplication.
- [x] 6.5 Revalidate documents and chunks immediately before each page transaction to prevent ghost references.
- [x] 6.6 Preserve the previous published version when LLM output or grounding validation fails.
- [x] 6.7 Commit page version, contribution manifest, task idempotency marker, and affected-slug record atomically.
- [x] 6.8 Publish validated automatic ingest output while keeping manual and agent mutations in draft or proposal workflows.
- [x] 6.9 Archive or issue-flag pages with no remaining grounded generated contribution while preserving protected manual content.
- [x] 6.10 Add Reduce tests for multi-document merges, duplicate delivery, concurrent versions, retractions, partial failure, and source deletion races.

## 7. Knowledge-Base Finalization

- [x] 7.1 Add versioned prompts and response models for taxonomy planning, index introduction, duplicate review, and link repair.
- [x] 7.2 Implement debounced `wiki.finalize` enqueueing that coalesces affected slugs across completed Reduce work.
- [x] 7.3 Plan taxonomy updates while preserving user-managed folder placement and explicit manual classification.
- [x] 7.4 Create or update the structured Index system page with taxonomy sections, bounded summaries, links, and counts.
- [x] 7.5 Append idempotent logical Log entries for generation, reprocessing, retraction, failure, repair, and retry outcomes.
- [x] 7.6 Normalize canonical links and aliases, remove unavailable targets, and inject bounded same-KB cross-links.
- [x] 7.7 Detect duplicate entity/concept candidates and create recoverable merge proposals that preserve provenance and redirects.
- [x] 7.8 Add lint generation for dead links, stale sources, unsupported content, duplicate pages, and taxonomy issues.
- [x] 7.9 Ensure finalization does not create Wiki page records in the raw-document vector collection.
- [x] 7.10 Add finalization tests for idempotency, manual folders, failed target pages, alias redirects, duplicate proposals, and large affected sets.

## 8. Processing Traces and Status APIs

- [x] 8.1 Persist and continue upload trace context across `wiki.ingest` and `wiki.finalize` task boundaries.
- [x] 8.2 Emit `postprocess.wiki` root spans and extract, summary, classify, typed page, and finalization child spans.
- [x] 8.3 Attach attempts, model usage, truncation, affected document/page IDs, skip reasons, and actionable errors to Wiki spans.
- [x] 8.4 Extend document and knowledge-base processing summaries with queued, running, retrying, finalizing, completed, failed, cancelled, and dead-letter Wiki states.
- [x] 8.5 Add trace/status tests for successful, skipped, retried, partially failed, cancelled, and dead-letter flows.

## 9. Bounded Wiki APIs

- [x] 9.1 Add scoped API schemas and endpoints for Index and Log system-view summaries.
- [x] 9.2 Add lazy folder-child and keyset-paginated page-list endpoints with stable ordering.
- [x] 9.3 Add bounded Wiki search over title, slug, aliases, summary, and content using FTS5 when available and a tested compatibility fallback otherwise.
- [x] 9.4 Extend page detail responses with provenance, chunk references, aliases, taxonomy, backlinks, outbound links, version, and generation metadata.
- [x] 9.5 Add bounded overview and ego graph endpoints that never cross knowledge-base scope.
- [x] 9.6 Add task status, attempt history, retry, and cancellation endpoints with existing permission checks.
- [x] 9.7 Add lint issue filters, issue detail, deterministic cleanup, and reviewable repair-proposal endpoints.
- [x] 9.8 Add API tests for pagination continuity, search aliases, graph bounds, scope isolation, authorization, and invalid repair actions.

## 10. Knowledge-Base Settings UI

- [x] 10.1 Present Wiki, dense, keyword, and graph indexing as independent controls backed by one form state.
- [x] 10.2 Add a Wiki-only preset that sets Wiki enabled and dense, keyword, and graph disabled while allowing later explicit changes.
- [x] 10.3 Load and display existing persisted strategies without silently applying new preset defaults.
- [x] 10.4 Explain skipped embedding through processing status rather than implying that Wiki-only upload failed.
- [x] 10.5 Add frontend tests for presets, combined strategies, edit round-trips, and existing-KB compatibility.

## 11. Wiki Workspace UI

- [x] 11.1 Refactor the knowledge-base header into the compact reference hierarchy with breadcrumb, name, description, and first-class Documents/Wiki/Graph navigation.
- [x] 11.2 Replace eager aggregate Wiki loading with separate bounded requests for system views, navigation, page detail, status, graph, and issues.
- [x] 11.3 Build the unframed desktop two-region workspace with a stable 300-340px independently scrolling navigation rail and flexible article reader.
- [x] 11.4 Add the sidebar search field and pinned Index and Log entries with reference-aligned spacing, icons, divider, and active states.
- [x] 11.5 Add page-type tabs with counts plus icon-based tree/list segmented controls and an accessible new-folder action.
- [x] 11.6 Build recursive lazy folder/type navigation with stable expansion, selection, loading, and pagination state.
- [x] 11.7 Implement root/nested folder creation, inline rename, accessible move commands, and desktop drag-and-drop with cyclic and cross-KB move guards.
- [x] 11.8 Implement flat bounded search results and restore the prior group, tree/list mode, expansion, and page selection when search is cleared.
- [x] 11.9 Build the structured Index article with category marker, maintenance introduction, typed headings/counts, canonical links, and one-line descriptions matching the supplied view.
- [x] 11.10 Build the paginated Log view as the second pinned system destination with event type, source, affected pages, outcome, and timestamp.
- [x] 11.11 Render Wiki Markdown with article-scale typography, canonical internal navigation, safe external-link behavior, and stable loading/empty/error geometry.
- [x] 11.12 Add provenance, exact source-chunk drilldown, aliases, taxonomy, backlinks, outbound links, version, and generation metadata through non-crowding secondary panels.
- [x] 11.13 Build the full-workspace graph mode with remote page search, page-type legend/filtering, zoom/fit controls, empty/loading states, and a non-blocking page-detail drawer.
- [x] 11.14 Add queued/running/retrying/finalizing/completed/failed/dead-letter status UI and poll only while non-terminal tasks exist.
- [x] 11.15 Add a global issue-count entry and drawer with lint filters, issue evidence, retry/cancel controls, deterministic cleanup actions, and repair-proposal review.
- [x] 11.16 Implement responsive mobile navigation/reader/detail switching without overlap, clipped actions, or loss of selected-page history.
- [x] 11.17 Add component tests for shell navigation, independent scrolling, system entries, counts, view switching, folder manipulation, search restoration, pagination, status polling, source links, graph drawer, and repair confirmations.

## 12. Validation, Documentation, and Rollout

- [x] 12.1 Add an integration test proving a Wiki-only upload stores chunks, creates no vector records, and publishes summary/entity/concept/index/log pages.
- [x] 12.2 Add an integration test proving combined Wiki and dense indexing produces both raw vector records and Wiki pages.
- [x] 12.3 Add recovery tests covering worker restart, duplicate delivery, provider rate limiting, deletion during Reduce, and reprocessing retractions.
- [x] 12.4 Add a representative multi-document Chinese networking corpus test for taxonomy, aliases, links, provenance, and deterministic convergence.
- [x] 12.5 Run backend unit/integration tests and frontend lint, typecheck, and component tests; resolve all regressions introduced by this change.
- [x] 12.6 Start backend, worker, and frontend locally and complete an end-to-end upload, processing timeline, Wiki browsing, source drilldown, and retry smoke test.
- [x] 12.7 Verify desktop and mobile Wiki layouts with browser screenshots and confirm no overlapping controls, blank views, or unstable navigation state.
- [x] 12.8 Compare the implemented desktop Wiki, Index, page reader, Log, and Graph screenshots against the supplied WeKnora references and resolve material information-architecture or layout differences.
- [x] 12.9 Update `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, backend RAG pipeline, frontend Wiki design, environment variables, migration, and rollback documentation.
- [x] 12.10 Document calibrated batch, debounce, concurrency, timeout, and rate-limit defaults and enable typed Wiki workers only after validation passes.
