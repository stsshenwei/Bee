## 1. Schema And Models

- [x] 1.1 Add Wiki feature/config fields to knowledge-base models and frontend types, including `wiki_enabled` or equivalent effective configuration.
- [x] 1.2 Extend SQLite metadata initialization with `wiki_page`, `wiki_folder`, `wiki_page_issue`, and `wiki_page_proposal` or revision tables.
- [x] 1.3 Add indexes and uniqueness constraints for scoped slugs, folder tree lookups, page type/status filters, source refs, and issue status.
- [x] 1.4 Add backend dataclasses or Pydantic models for Wiki pages, folders, issues, graph nodes/edges, generation tasks, and proposals.
- [x] 1.5 Add schema invariant tests for scope fields, unique active slug per KB, default values, and clean rebuild initialization.

## 2. Wiki Repository Layer

- [x] 2.1 Implement `WikiPageRepository` for create, update, get by slug/id, list/filter, archive, source-ref lookup, and cursor-safe page walking.
- [x] 2.2 Implement folder repository operations for create, rename/move, delete-empty, list children, and page counts.
- [x] 2.3 Implement issue repository operations for create, list by page/status, and status update.
- [x] 2.4 Implement proposal/revision repository operations for create, list, get, apply, reject, and audit metadata.
- [x] 2.5 Add repository tests for cross-KB isolation, folder conflicts, source/chunk refs, issue status, and proposal lifecycle.

## 3. Wiki Service Layer

- [x] 3.1 Implement `WikiPageService` with scoped page creation, update, archival, status transitions, and version handling.
- [x] 3.2 Implement wiki-link parsing for `[[slug]]` and `[[slug|label]]`, including inbound/outbound link refresh in the same KB.
- [x] 3.3 Implement folder path normalization, page move, sortable `wiki_path`, and category/depth cache updates.
- [x] 3.4 Implement bounded Wiki index view and page-link graph overview/ego graph responses.
- [x] 3.5 Implement source deletion/reindex handling that marks pages stale, removes invalid refs, or creates issues.
- [x] 3.6 Add service tests for link maintenance, graph bounds, stale source handling, and version/meta update rules.

## 4. Wiki Generation

- [x] 4.1 Add Wiki generation configuration flags and per-KB requested/effective config reporting.
- [x] 4.2 Add prompt templates for summary page generation with strict source/chunk reference output requirements.
- [x] 4.3 Implement generation from indexed parent/table/OCR chunks into draft summary pages.
- [x] 4.4 Add entity/concept candidate extraction and deduplication against existing page titles, slugs, and aliases.
- [x] 4.5 Add bounded task execution with sanitized errors, retry status, and fail-open behavior for raw document indexing.
- [x] 4.6 Add tests for generation success, missing provenance fallback, provider failure, and draft versus auto-publish behavior.

## 5. Wiki API Routes

- [x] 5.1 Add scoped endpoints to list/search Wiki pages, read page detail, create/update page draft, and archive pages.
- [x] 5.2 Add scoped endpoints for folders: list children, create, rename/move, delete empty folder, and move page.
- [x] 5.3 Add scoped endpoints for Wiki graph overview/ego graph.
- [x] 5.4 Add scoped endpoints for issues and proposals, including approval/apply/reject where supported.
- [x] 5.5 Add route tests for scope validation, not-found behavior, archived KB rejection, and payload sanitization.

## 6. Agent Runtime Tools

- [x] 6.1 Add Wiki tool feature flags and runtime policy wiring for read tools and maintenance tools.
- [x] 6.2 Implement `wiki_search` with provider-neutral bounded matching and snippets.
- [x] 6.3 Implement `wiki_read_page` with full Markdown, metadata, links, source refs, chunk refs, and bounded index rendering.
- [x] 6.4 Implement `wiki_read_source_doc` by reusing scoped document/chunk repository boundaries.
- [x] 6.5 Implement `wiki_flag_issue`, `wiki_read_issue`, and `wiki_update_issue`.
- [x] 6.6 Implement `wiki_write_page`, `wiki_replace_text`, `wiki_rename_page`, and `wiki_delete_page` as proposal creators by default.
- [x] 6.7 Update runtime deep-read guard so `wiki_search` requires `wiki_read_page` or raw source drilldown before factual final answers.
- [x] 6.8 Update agent prompt templates with Bee-specific Wiki guidance and no Postgres-specific regex assumptions.
- [x] 6.9 Add runtime tests for tool schemas, argument validation, output truncation, scope isolation, unavailable tools, and proposal semantics.

## 7. Retrieval And Answer Integration

- [x] 7.1 Decide and implement first-phase published Wiki search behavior: SQLite tool-only or optional retrievable Wiki chunks.
- [x] 7.2 Ensure exact-answer prompts prefer raw source chunks over Wiki summaries for quotes, numbers, code, and tables.
- [x] 7.3 Add contradiction handling that flags Wiki issues when Wiki content conflicts with raw chunk evidence.
- [x] 7.4 Add source/chunk refs from Wiki tools to agent trace metadata without exposing private reasoning.
- [x] 7.5 Add tests for Wiki-guided answer, exact-answer source fallback, and contradiction issue creation.

## 8. Frontend Wiki Workspace

- [x] 8.1 Extend `frontend/app/lib/types.ts` and `frontend/app/lib/api.ts` with Wiki page, folder, graph, issue, and proposal APIs.
- [x] 8.2 Add a Wiki tab or section to the knowledge-base detail shell when the KB is Wiki-capable.
- [x] 8.3 Build Wiki page list with search, type/status/folder filters, page status, summaries, and issue indicators.
- [x] 8.4 Build Wiki page reader with Markdown, source documents, chunk refs, backlinks, outbound links, status, and version.
- [x] 8.5 Build source evidence navigation that opens existing document preview or chunk/source context in the selected KB scope.
- [x] 8.6 Build bounded Wiki graph overview and ego expansion UI.
- [x] 8.7 Build issue and proposal review panels with approve/reject/apply actions where backend support exists.
- [x] 8.8 Add responsive styling in `globals.css` using the existing Bee visual system and no second styling framework.

## 9. Observability And Safety

- [x] 9.1 Add processing spans or structured logs for Wiki generation, tool calls, proposals, issue creation, and proposal application.
- [x] 9.2 Sanitize tool outputs, issue descriptions, proposal payloads, source snippets, and error messages before streaming to the UI.
- [x] 9.3 Add safeguards so normal HTTP requests cannot trigger global reset or uncontrolled full-Wiki regeneration.
- [x] 9.4 Add limits for Wiki generation pages per document, chunks per prompt, tool result count, graph nodes, and source drilldown length.

## 10. Documentation And Validation

- [x] 10.1 Update `docs/ARCHITECTURE.md` with the LLM Wiki layer, tables, tools, browser, and relation to raw evidence/KG.
- [x] 10.2 Update `docs/design-docs/backend-rag-pipeline.md` with Wiki generation, tool usage, exact-answer fallback, and issue flow.
- [x] 10.3 Update `docs/design-docs/frontend-chat-ui.md` with Wiki browser and agent timeline implications.
- [x] 10.4 Update `docs/API.md` with Wiki endpoints, request scope, statuses, and proposal/issue payloads.
- [x] 10.5 Run focused backend tests for storage, repositories, services, routes, generation, and runtime tools.
- [x] 10.6 Run focused frontend tests for API helpers, Wiki UI behavior, and agent stream event rendering.
- [x] 10.7 Perform manual smoke validation: create Wiki-capable KB, upload/index document, generate draft page, read/search Wiki, inspect source refs, flag issue, create proposal, and verify chat source fallback.
