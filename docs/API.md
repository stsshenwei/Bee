# API

## 知识库生命周期

- `GET /workspaces/default`：读取稳定默认工作空间。
- `GET /knowledge-bases?workspace_id=...&include_archived=false`：列出知识库及聚合状态。
- `POST /knowledge-bases`：创建知识库；支持 `name`、`description`、`type`（`document`、`faq`、`wiki`）、`is_default`、`indexing_strategy` 和 `provider_config`。
- `GET /knowledge-bases/{knowledge_base_id}`：读取详情、requested/effective Provider 配置和聚合状态。
- `PATCH /knowledge-bases/{knowledge_base_id}`：更新名称、描述、`is_default`、索引策略或 Provider 请求值。
- `DELETE /knowledge-bases/{knowledge_base_id}`：逻辑归档。默认 KB 不可归档。
- `POST /knowledge-bases/{knowledge_base_id}/restore`：恢复归档状态。

`is_default=true` 表示该工作空间的默认知识库；每个 workspace 同时只能有一个默认 KB，新的默认 KB 会自动替换旧默认 KB。归档 KB 默认不出现在列表中，不能上传或检索，但不会物理删除内容；默认 KB 不可归档。

## 文档范围

以下接口接受单个 `knowledge_base_id`；省略时解析为当前 `is_default=true` 的 KB（兼容初始 `DEFAULT_KNOWLEDGE_BASE_ID`），不表示全部知识库：

- `POST /documents/upload`：multipart form 字段。
- `POST /documents/parse`：JSON request body 字段。
- `GET /documents`、`GET /documents/content`、`GET /documents/file`：query 参数。
- `POST /rag/documents/upload`：multipart form 字段。
- `POST /rag/documents/{doc_id}/ingest`、`DELETE /rag/documents/{doc_id}`：query 参数。
- `POST /documents/{doc_id}/enrichment/retry`：query 参数。

文档、parent/child/table/OCR chunk、FTS 和向量记录必须与请求 scope 同域。跨库 document/chunk 身份会被拒绝。

## 查询与聊天范围

`POST /rag/query` 与 `POST /chat/stream` 支持：

```json
{
  "question": "问题文本",
  "knowledge_base_id": "kb-a",
  "knowledge_base_ids": ["kb-a", "kb-b"]
}
```

客户端通常二选一；`knowledge_base_ids` 用于多库 fan-out。服务验证所有 KB 处于 active 且属于同一 workspace，检索在排序前过滤 scope，并在 citation、父块和图谱 source chunk 回查时再次校验。未传任一字段时使用默认 KB，并在 debug metadata 中记录 `compatibility_default=true`。

`/rag/query` 返回 `answer`、`citations`、`graph_paths`、`used_entities`、`used_chunks`、`confidence` 和 `debug_info`。启用 Agent workflow 时还返回 `agent_trace`、`tool_calls` 和 `evidence_summary`。`/chat/stream` 保留原 SSE framing，并可在答案 token 前发送同类可审计事件。

## 反馈与审计

`POST /feedback` 必须落到一个明确活动 KB。多库回答需要额外提供单个 `knowledge_base_id` 作为修正目标，否则请求被拒绝。查询和反馈分别写入范围化 `query_log` 与 `answer_feedback`，记录实际 KB scope、工具和引用 chunk，但审计记录不作为知识证据。

## LLM Wiki

Wiki routes are scoped under one active KB with persisted `indexing_strategy.wiki_enabled=true`. The `wiki` type applies that strategy as a creation preset.

- `GET /knowledge-bases/{kb}/wiki/pages`: list/search pages with `q`, `status`, `page_type`, `folder_id`, `limit`, and `cursor`.
- `POST /knowledge-bases/{kb}/wiki/pages`: create a draft or published page with Markdown, source refs, chunk refs, aliases, and folder/category metadata.
- `GET|PATCH|DELETE /knowledge-bases/{kb}/wiki/pages/{slug}`: read, update, or archive one page. `[[slug]]` and `[[slug|label]]` links update inbound/outbound link caches.
- `GET|POST /knowledge-bases/{kb}/wiki/folders`, `PATCH|DELETE /knowledge-bases/{kb}/wiki/folders/{folder_id}`: manage the folder tree; delete only succeeds for empty folders.
- `GET /knowledge-bases/{kb}/wiki/graph`: bounded page-link graph with optional `center_slug`.
- `POST /knowledge-bases/{kb}/wiki/source-doc`: read raw source chunks by `doc_id` and/or `chunk_ids`.
- `GET /knowledge-bases/{kb}/wiki/generation-tasks`: list recent bounded Wiki generation tasks, optionally filtered by `doc_id`.
- `POST /knowledge-bases/{kb}/wiki/generate`: enqueue immediate durable Map/Reduce generation for one parsed document. Body: `{ "doc_id": "..." }`.
- `GET /knowledge-bases/{kb}/wiki/overview`: bounded page-type counts, Index/Log summaries, issue count, and generation states.
- `GET /knowledge-bases/{kb}/wiki/logs`: paginated logical generation and maintenance log.
- `GET /knowledge-bases/{kb}/wiki/processing-tasks`: typed `wiki.ingest` and `wiki.finalize` task status.
- `POST /knowledge-bases/{kb}/wiki/processing-tasks/{task_id}/retry|cancel`: retry a dead-letter task or cancel active Wiki work without deleting attempt history.
- `GET|POST /knowledge-bases/{kb}/wiki/issues`, `GET|PATCH /knowledge-bases/{kb}/wiki/issues/{issue_id}`: create, list, read, and update Wiki quality issues.
- `GET|POST /knowledge-bases/{kb}/wiki/proposals`, `GET /knowledge-bases/{kb}/wiki/proposals/{proposal_id}`, `POST /apply`, `POST /reject`: review and apply/reject proposed writes.

When Wiki generation is enabled, document processing persists raw chunks and enqueues durable Wiki Map/Reduce work. Valid automatic ingest publishes grounded summary/entity/concept pages and maintains Index/Log. Manual and agent writes remain draft/proposal workflows.

Agent write tools create `wiki_page_proposal` rows by default. Applying a proposal is a separate API operation so automated maintenance stays reviewable.

## reset_required

破坏性 clean-rebuild 没有 HTTP API。旧 SQLite schema、maintenance marker 或不兼容 Milvus collection 会使启动或证据访问失败关闭；运维人员必须停服务后运行 `python -m app.scripts.rebuild_knowledge_storage`。正常 HTTP 请求不能绕过、确认或触发全局清空。
