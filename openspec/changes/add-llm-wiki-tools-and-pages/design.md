## Context

Bee currently has scoped knowledge bases, document parsing/chunking, Milvus/FTS retrieval, optional KG, document enrichment, and a Weknora-style agent runtime. The `wiki` knowledge-base type is already accepted by the backend and frontend, but the content pipeline still treats it like document evidence.

The WeKnora reference provides a useful pattern: Wiki is a durable page layer with `wiki_pages`, folders, issues, source references, chunk references, page links, read/search tools, source-drilldown, and maintenance tools. Bee should adapt that shape to the existing FastAPI, SQLite, Milvus, and Next.js architecture instead of copying Go/Vue implementation details.

Constraints:

- Preserve `KnowledgeBaseScope` on every Wiki read, write, generation, issue, and tool call.
- Keep raw document chunks as the authoritative evidence for exact quotes, numbers, code, tables, and citation verification.
- Do not make normal HTTP requests trigger global destructive rebuilds.
- Do not expose high-risk write tools as direct model mutations by default.
- Keep frontend styling in the existing Next.js/CSS system.

## Goals / Non-Goals

**Goals:**

- Add first-class LLM Wiki pages and folders to scoped knowledge bases.
- Generate source-bound Wiki pages from indexed documents.
- Expose safe Wiki read/search/source/issue tools to reasoning mode.
- Add controlled maintenance tools that create drafts or proposals before mutating published pages.
- Add a Wiki browser in the knowledge-base workspace.
- Keep Wiki evidence auditable through source document IDs and source chunk IDs.

**Non-Goals:**

- Do not implement external datasource sync such as Feishu/Yuque/Notion.
- Do not implement full multi-user collaborative editing.
- Do not replace the raw RAG pipeline with Wiki-only retrieval.
- Do not directly copy WeKnora Go repositories, Vue components, branding, or Postgres-specific regex assumptions.
- Do not expose destructive Wiki deletion or rename as unapproved model actions.

## Decisions

### 1. Store Wiki as scoped SQLite business data

Add SQLite tables under the existing metadata database:

```text
wiki_page(
  id, workspace_id, knowledge_base_id,
  slug, title, page_type, status,
  content_markdown, summary,
  parent_slug, folder_id, category_path_json, wiki_path, depth, sort_order,
  source_refs_json, chunk_refs_json,
  in_links_json, out_links_json, aliases_json, metadata_json,
  version, created_at, updated_at
)

wiki_folder(
  id, workspace_id, knowledge_base_id,
  parent_id, name, path, depth, sort_order,
  created_at, updated_at
)

wiki_page_issue(
  id, workspace_id, knowledge_base_id,
  slug, issue_type, description,
  suspected_doc_ids_json, suspected_chunk_ids_json,
  status, reported_by, created_at, updated_at
)

wiki_page_revision or wiki_page_proposal(
  id, page_id, workspace_id, knowledge_base_id,
  action, proposed_payload_json, source_refs_json, chunk_refs_json,
  status, created_by, created_at, applied_at
)
```

Rationale: SQLite is already the business-data source of truth for documents, chunks, tasks, KG metadata, feedback, and query logs. Wiki pages are user-visible business data, not a derived Milvus-only index.

Alternative considered: store Wiki pages as generated Markdown files under `backend/data`. Rejected because page status, links, issues, revisions, and scope queries need transactional metadata and strong KB isolation.

### 2. Treat Wiki pages as curated evidence, not primary truth

Every generated or edited page must carry:

- `source_refs`: document IDs and titles that contributed to the page.
- `chunk_refs`: exact `document_chunk.id` values for factual claims when available.
- `page_type`: `summary`, `entity`, `concept`, `synthesis`, `comparison`, `index`, or `log`.
- `status`: `draft`, `published`, or `archived`.

Wiki search and read tools may guide answers, but exact-answer tasks must still read source chunks through `wiki_read_source_doc`, `list_knowledge_chunks`, or `get_document_info`.

Alternative considered: index Wiki pages as equal peers to source chunks and cite them directly. Rejected because generated content can drift or over-summarize. Published Wiki can be searchable, but citations must stay traceable to original chunks for high-precision facts.

### 3. Add a staged Wiki generation lifecycle

Generation runs after a document is indexed and should reuse existing processing/task infrastructure where practical:

```text
indexed document
  -> enqueue wiki generation task
  -> collect parent/table/OCR chunks
  -> generate document summary page
  -> extract candidate entity/concept pages
  -> deduplicate against existing pages
  -> create/update draft or published pages
  -> refresh links, index view, stats, and issues
```

The first implementation can be conservative:

- Generate summary pages first.
- Add entity/concept extraction next.
- Add synthesis/comparison only when requested by a tool or user action.
- Default to `draft` unless `WIKI_AUTO_PUBLISH_ENABLED=true`.

Alternative considered: generate a full KB Wiki in one long request. Rejected because large corpora need resumable tasks, bounded LLM calls, retries, and partial failure visibility.

### 4. Adapt WeKnora tools into Bee runtime policy

Tool set:

```text
Read-first:
  wiki_search
  wiki_read_page
  wiki_read_source_doc
  wiki_flag_issue

Maintenance:
  wiki_read_issue
  wiki_update_issue
  wiki_write_page
  wiki_replace_text
  wiki_rename_page
  wiki_delete_page
```

Read-first tools are safe to register when the request scope contains an active Wiki-capable KB and `AGENT_RUNTIME_WIKI_TOOLS_ENABLED=true`.

Maintenance tools require `AGENT_RUNTIME_WIKI_MAINTENANCE_TOOLS_ENABLED=true`. By default, `wiki_write_page`, `wiki_replace_text`, `wiki_rename_page`, and `wiki_delete_page` create `wiki_page_proposal` rows instead of mutating published pages. A later approval action applies the proposal.

Alternative considered: follow WeKnora and let maintenance tools directly mutate pages. Rejected for Bee's current product state because the chat agent has no robust human-in-the-loop approval UI for destructive knowledge edits yet.

### 5. Keep search provider-neutral

WeKnora's `wiki_search` prompt encourages Postgres POSIX regex. Bee uses SQLite, so the tool contract should support provider-neutral query strings and bounded matching:

- Exact/substring matching over title, slug, summary, aliases, and content.
- Optional simple alternation syntax such as `term1|term2`.
- Optional FTS5 indexing later.
- Bounded snippets and result counts.

The model-facing prompt must not mention Postgres operators or unsupported regex behavior.

### 6. Add Wiki UI as a tab inside the KB detail workspace

The knowledge-base detail shell gains a Wiki tab when the KB type is `wiki` or wiki indexing is enabled. The tab should include:

- Left folder/type navigation.
- Page list with search, filters, status, and page type.
- Page reader with Markdown, summary, source documents, chunk references, backlinks, outbound links, and version/status.
- Graph view for page-link overview and ego expansion.
- Issue panel and proposal/draft review panel.

This reuses the existing app frame and CSS rather than adding a second styling system.

## Risks / Trade-offs

- Generated Wiki may hallucinate or merge unrelated entities -> require source/chunk refs, draft review, issue flagging, and raw chunk drilldown.
- Wiki generation can be expensive -> feature flags, per-KB config, task limits, batch sizes, and fail-open document indexing.
- SQLite search may be weaker than Postgres regex/trigram -> start with bounded provider-neutral matching and add FTS indexes when needed.
- Agent write tools can damage knowledge -> default write tools to proposals, not direct published-page mutation.
- Wiki pages can become stale when source docs are deleted or reprocessed -> maintain source/chunk refs and mark affected pages stale or issue-flagged on document deletion/reindex.
- UI can become crowded -> add a dedicated Wiki tab instead of mixing pages into the document list.

## Migration Plan

1. Add schema initialization for Wiki tables and default Wiki config fields.
2. Add repository and model tests for scope isolation, unique slugs, folders, issues, and revisions.
3. Add service-level page, folder, issue, link, and proposal operations.
4. Add generation tasks behind disabled-by-default flags.
5. Add read-first runtime tools and prompt guidance.
6. Add maintenance tools as proposal creators.
7. Add Wiki API routes and frontend browser.
8. Add docs and focused validation.

Rollback:

- Disable `WIKI_ENABLED` and `AGENT_RUNTIME_WIKI_TOOLS_ENABLED`.
- Hide the Wiki tab while leaving tables inert.
- Existing document ingest, chat, retrieval, and KG paths continue without Wiki.

## Open Questions

- Should `type=wiki` imply Wiki generation by default, or should document KBs also support `wiki_enabled=true`?
- Should published Wiki pages be indexed into Milvus as retrievable chunks in the first implementation, or only searched through SQLite tools?
- Should approvals be admin-only now, or should this wait for `add-auth-tenant-kb-permissions`?
- Should source document deletion archive affected Wiki pages automatically or mark them as stale for review?
