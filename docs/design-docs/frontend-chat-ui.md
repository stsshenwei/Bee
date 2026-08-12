# Frontend Chat UI

## WeKnora-like Knowledge Management Update

`frontend/app/knowledge/page.tsx` is now organized around focused local components for the knowledge catalog, creation wizard, detail shell, document toolbar, grid/list document views, upload action menu, pending upload dialog, batch monitor, and settings dialog. Styling remains in `frontend/app/globals.css`; no second styling system is introduced.

The create flow is a Bee-branded WeKnora-like wizard:

- left configuration rail: basic information, type, model, vector storage, parser, chunking, image/OCR, audio, graph, and advanced settings
- `Document`, `FAQ`, and `Wiki` knowledge-base metadata types can be submitted
- the wizard exposes a default-knowledge-base toggle; future types, audio, multimodal, and unsupported runtime features remain disabled or unavailable
- supported requested settings are submitted through `POST /knowledge-bases`
- the detail page displays effective provider configuration and inactive overrides returned by the backend
- validation failures keep user input in the wizard

The selected KB document workspace now includes API-backed filters and two view modes:

- filters: search query, tag/keyword, file type, status, source path, start date, and end date
- view modes: compact grid cards and dense list rows
- actions always carry the selected `knowledge_base_id` for preview, delete, enrichment retry, upload task retry, and bulk delete plumbing
- bulk delete is intentionally sequential and scoped; partial failures are shown in the page notice

Wiki-capable KBs now add a `Wiki` tab beside the document workspace. The tab loads scoped Wiki pages, open issues, pending proposals, recent generation tasks, and a bounded page-link graph. Users can:

- filter Wiki pages by query, status, and page type
- read Markdown pages with status, type, version, source refs, inbound links, and outbound links
- open source refs through the existing document preview when the source document is present, while also showing bounded raw chunk context in the Wiki side panel
- trigger draft generation from indexed documents without starting provider work automatically on tab open
- review issues and mark them resolved or ignored
- apply or reject pending proposals created by generation or agent maintenance tools

The chat composer now includes a `Wiki 问答` mode. It sends `chat_mode: "wiki"` to `/chat/stream`; the backend routes this to the Wiki runtime policy, which searches Wiki pages first and drills into raw source chunks for exact facts. The frontend keeps the same SSE parser and agent timeline surface, so Wiki tool calls appear as normal safe timeline events without exposing hidden reasoning.

The upload interaction uses a staged flow:

```text
upload action menu
  -> select documents or folder
  -> pending upload dialog
  -> review relative paths, sizes, remove/cancel
  -> confirm parser/chunk/retrieval settings
  -> create upload batch
  -> upload files into the batch
  -> confirm processing
  -> monitor backend batch/file task state
```

Provider safety boundary: selecting files or opening the pending dialog does not parse, index, embed, enrich, or call external providers. Backend processing starts only after the user confirms the upload batch. Webpage import and online editing entries are shown disabled until backend capabilities exist.

## Goal

Provide a simple single-page workspace where users can:

- ask RAG questions
- see streamed answers
- inspect cited source documents
- browse the indexed dataset
- submit corrected answers back into the corpus

## Owning Files

- `frontend/app/page.tsx`
- `frontend/app/layout.tsx`
- `frontend/app/globals.css`

## Design Shape

The current frontend is intentionally compact: one client page component owns all view state and request logic, while `globals.css` owns all styling. Evidence: `frontend/app/page.tsx:67-504`, `frontend/app/layout.tsx:1-15`.

## UI Regions

### Left Sidebar

- `Bee` brand label
- two primary navigation entries: 对话 and 知识库
- compact new-chat action
- no recent-chat list, account area, search, app switcher, or ChatGPT-style utility controls

### Model tab

- message list
- streamed assistant markdown rendering
- per-message source cards
- per-message feedback controls
- fixed dark composer at the bottom

Evidence: `frontend/app/page.tsx:334-442`.

### Dataset tab

- refresh control
- dataset table with source, size, chunk count, and timestamp
- per-row document open action

Evidence: `frontend/app/page.tsx:443-480`.

### Document viewer

- modal mask
- text mode for parsed non-PDF content
- iframe mode for PDF preview

Evidence: `frontend/app/page.tsx:484-500`.

## Network Contract

- chat requests post `{ message }` to `/chat/stream`
- the SSE stream may send source metadata before token chunks
- the SSE stream may send a `reasoning` payload after sources and before token chunks; the chat UI renders it as a collapsible "深度思考过程" panel with query understanding, retrieval queries, term mappings, and evidence snippets
- feedback requests post `{ question, answer }` to `/feedback/answer`
- document preview mode is chosen by file extension on the client

Evidence: `frontend/app/page.tsx:126-215`, `frontend/app/page.tsx:244-320`.

## State Model

Important local state buckets:

- `messages` for the chat transcript
- `feedbackMap` keyed by assistant message index
- `datasetItems` plus loading/error state
- document viewer state for modal content and mode
- `activeTab`, `input`, and `loading`

Evidence: `frontend/app/page.tsx:68-88`.

## Rendering Choices

- assistant content is rendered through `react-markdown` with `remark-gfm`
- user messages remain plain text
- source cards show source path and rounded relevance
- visual design is driven through CSS custom properties in `globals.css`

Evidence: `frontend/app/page.tsx:354-372`, `frontend/app/globals.css:1-398`.

## Constraints For Future Changes

- Preserve SSE compatibility unless backend and frontend change together.
- If the page is split into smaller components, keep request behavior centralized or introduce a clear data layer.
- Keep the dataset and feedback flows visible; they are part of the product loop, not just debugging tools.
- If document preview grows more complex, separate PDF and text viewers rather than overloading one handler.
## Horse Shell Update

The frontend is now organized as a two-column app frame:

- Left sidebar: `Bee` brand, 对话 navigation, 知识库 navigation, and 新建会话 action.
- Right workspace: either the existing streaming chat workflow or the knowledge-base document manager.
- The visual system keeps the user's requested `Bee` identity while following a Cognitive Node-style knowledge assistant shell: pale left rail, compact product brand block, top chat toolbar with search, centered conversation lane, assistant rows with a small bot marker and blue vertical accent, right-aligned user bubbles, and a compact bottom composer.
- Knowledge-base view supports upload for PDF, DOCX, HTML, Excel, and Markdown extensions.
- Upload calls `POST /documents/upload`; the backend stores, parses, chunks, persists, and indexes synchronously in this version.
- Knowledge-base upload now has a task workspace with separate file and folder selection controls. File uploads can include one or many files; folder uploads use browser folder selection and submit each supported file with its folder-relative path.
- Upload progress is frontend-managed per file: rows move through queued, uploading, parsing, parsed, or failed states, and the page displays total completed count, success count, failure count, current file, chunk count, and per-file errors.
- A failed file does not stop the upload task. The frontend continues with remaining files and refreshes the document list when every row is parsed or failed.
- Document rows show file name, type, parse status, chunk count, update time, and a preview action.
- Source preview still uses `/documents/file` for PDFs and `/documents/content` for text-renderable formats.

The chat SSE contract is backwards compatible: `/chat/stream` sends source data first, may send a `reasoning` summary event next, then token events and `[DONE]` at completion.

## Conversation And Memory UI Update

The chat UI now tracks backend conversation and memory state:

- The client stores the latest streamed `conversation_id` and sends it with subsequent `/chat/stream` requests.
- The sidebar new-chat action dispatches a local event that clears the current transcript, feedback state, active conversation ID, input, and memory notice without deleting saved long-term memories.
- Chat requests include `memory_enabled` and `temporary` flags. The temporary toggle disables long-term memory recall and extraction for that request.
- The SSE parser handles optional `conversation_id` and `memory_updated` events while preserving existing `sources`, `reasoning`, `token`, and `[DONE]` handling.
- When `memory_updated` arrives, the UI shows a non-blocking memory notice and refreshes the memory list.
- The top toolbar exposes a memory panel. The panel calls `GET /memories`, renders active memories, and calls `DELETE /memories/{memory_id}` for deletion.

Memory UI state lives in `frontend/app/chat/page.tsx` alongside the existing local chat state: `conversationId`, `temporaryChat`, `memoryNotice`, `memoryPanelOpen`, `memories`, `memoryLoading`, and `memoryError`.

## Agent Stream Timeline Update

When the backend streams agentic retrieval events, the chat UI now renders a WeKnora-style execution timeline for assistant messages.

- `/chat/stream` may emit `agent_trace`, `tool_call`, `tool_observation`, `evidence_summary`, and `citation_verification` events while preserving existing `sources`, `reasoning`, `token`, `conversation_id`, and `memory_updated` behavior.
- `frontend/app/lib/agent-stream.ts` normalizes those SSE payloads into safe `AgentStreamEvent` objects, derives `AgentTimelineStep` rows, pairs tool calls with tool observations, and produces an `AgentRunSummary`.
- `frontend/app/components/AgentTimeline.tsx` renders the timeline below sources and above the legacy reasoning panel, so source buttons, document preview, memory notices, and feedback controls remain visible.
- The timeline is expanded while an answer is streaming and can be collapsed after completion.
- Tool and stage names are shown with user-facing Chinese labels, such as `理解问题`, `规划检索`, `原文知识库检索`, `关键词检索`, and `图谱检索`.
- The timeline displays auditable execution summaries only. It scrubs private fields such as `chain_of_thought`, `scratchpad`, `private_reasoning`, `raw_prompt`, and `memory_context`, and it must not be treated as hidden model chain-of-thought.

No backend event metadata was added for this UI change. Ordering and elapsed time are derived in the frontend from event arrival sequence and timestamps.

The chat UI now also understands Agent domain events emitted by the runtime:

```text
agent_query -> agent_thought -> agent_tool_call / agent_tool_result
-> agent_reflection -> agent_remedial_search -> agent_references
-> agent_final_answer -> agent_complete
```

These events are additive to the old SSE shape. The parser still handles `sources`, `reasoning`, `token`, `final`, `conversation_id`, `memory_updated`, `error`, and `[DONE]`. Domain events are normalized in `frontend/app/lib/agent-stream.ts`; tool calls and results pair by `call_id`; `agent_references` becomes a references timeline step; `agent_reflection` shows public validity/gap/correction-query summaries; `agent_remedial_search` appears as a distinct follow-up search caused by an evidence gap. The final collapsed summary can indicate that remedial retrieval was used.

## Intelligent Reasoning Presentation Update

The chat UI now separates quick-answer presentation from intelligent-reasoning presentation:

- Raw RAG / quick-answer streams show a compact retrieval summary such as `检索中...`, `检索完成 · 引用了 N 篇文档`, `证据不足`, or `引用校验失败`.
- Agentic chat streams with agent events show a WeKnora-style timeline with public steps such as `已完成问题理解`, `检索知识库：[query]`, `找到 N 个结果`, `引用了 N 篇文档`, `思考`, and `完成`.
- Tool class names are projected into user-facing actions: `RawRAGTool` -> `检索知识库`, `KeywordSearchTool` -> `关键词检索`, and `GraphRetrieverTool` -> `查询图谱证据`.
- The `思考` step is a public evidence-organization summary, not hidden chain-of-thought.
- Markdown code blocks in assistant answers render with a language label and copy-code button while inline code remains compact.

The implementation lives in `frontend/app/lib/agent-stream.ts`, `frontend/app/components/AgentTimeline.tsx`, and `frontend/app/chat/page.tsx`. Private fields such as `chain_of_thought`, `scratchpad`, `private_reasoning`, `raw_prompt`, and `memory_context` are scrubbed before display.

## Search And Read Timeline

- Agent execution is rendered before assistant answer Markdown, so observable retrieval work appears before the final answer.
- Raw and keyword retrieval results emit bounded `DocumentChunkReaderTool` presentation events for documents whose chunk content was actually loaded.
- Tool calls carry stable `call_id` and round metadata, allowing repeated search/read actions to pair correctly in the timeline.
- While streaming, the timeline stays expanded. When the answer completes, it automatically collapses to `思考 N 轮 · 调用 N 次工具 · 耗时 Ns` and remains manually expandable.
- Agent mode hides the duplicate legacy reasoning panel and compact Raw RAG search summary. Hidden chain-of-thought is never displayed.

## Quick Answer Timeline

Quick-answer streams can now emit the same safe timeline surface without implying that quick mode used the reasoning runtime. The frontend maps quick RAG stages to user-facing labels:

```text
UnderstandQuestion -> 理解问题
RetrieveKnowledgeBase -> 检索知识库
ReadEvidence -> 引用文档
SynthesizeAnswer -> 思考
Complete -> 完成
```

When no actual `tool_call` events are present, the collapsed summary uses quick-search wording instead of `调用 0 次工具`. Reasoning-mode streams still use paired tool call/result rows, evidence summary, citation verification, and the existing expanded timeline behavior. The `思考` label remains a public evidence-organization summary; the UI continues to scrub private fields such as `chain_of_thought`, `scratchpad`, `private_reasoning`, `raw_prompt`, and `memory_context`.

## Unified Runtime Timeline

When the backend routes quick chat through the unified runtime, the frontend receives the same domain event contract used by reasoning mode. Quick streams may contain only:

```text
agent_query -> agent_references -> agent_final_answer -> agent_complete
```

Reasoning streams may additionally include thought, tool, result, reflection, and remedial-search events. `frontend/app/lib/agent-stream.ts` normalizes both shapes into the same `AgentStreamEvent` and `AgentTimelineStep` models. If a quick stream has no tool calls, the collapsed summary keeps the quick-search wording and does not synthesize fake tool steps. Legacy `sources`, `token`, `final`, `error`, `memory_updated`, and `[DONE]` events remain supported.

## Knowledge Base Catalog And Chat Scope

知识管理首页采用 Bee 品牌下的 WeKnora 式信息架构：主导航、全部/收藏/最近/当前工作空间筛选、紧凑工具栏和响应式两列 KB 卡片。卡片进入由 `?kb=<id>` 标识的详情工作区，上传、列表、预览、删除、概要状态和重试请求都携带该 KB ID。

聊天输入栏提供知识库多选菜单。从 KB 详情进入时预选该库；没有显式选择时显示“默认知识库”，不显示“全部知识库”。选中 ID 随 `/chat/stream` 发送，SSE 事件顺序保持兼容。多库回答保存反馈前必须缩小到一个目标 KB。

目录和详情页将 `aggregate.reset_required` 显示为“存储需要清空重建”，但前端不提供全局清空按钮；破坏性升级只允许运维 CLI。普通知识库可从卡片删除图标或设置弹窗执行“删除知识库”，该操作调用逻辑归档接口并立即移出活动列表；默认知识库不显示删除入口，确认文案明确底层数据仍暂时保留以便恢复。

桌面 `1440x900` 与移动 `390x844` 的目录、长名称详情、上传进度和多 KB 聊天范围截图保存在 `openspec/changes/add-multi-knowledge-base-domain/verification/`。验收要求无横向溢出、无非预期元素重叠，并在知识库菜单展开时隐藏其后的空状态文案。
