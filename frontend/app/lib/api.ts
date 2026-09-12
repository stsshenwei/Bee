import type {
  ChatAttachment,
  ChatSessionSummary,
  DocumentFilters,
  DocumentItem,
  DocumentProcessingTrace,
  DocumentProcessingPreview,
  KnowledgeBase,
  KnowledgeBaseType,
  MessagesLoadResponse,
  ParserEngineInfo,
  ParserEnginesResponse,
  UploadBatch,
  UploadBatchSettings,
  WikiFolder,
  WikiGraph,
  WikiGenerationTask,
  WikiLog,
  WikiOverview,
  WikiProcessingTask,
  WikiIssue,
  WikiPage,
  WikiProposal,
  WikiSourceRef,
} from "./types";

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export async function readJson<T>(res: Response): Promise<T & { detail?: string }> {
  const data = (await res.json()) as T & { detail?: string };
  if (!res.ok) {
    throw new Error(data.detail || `请求失败: ${res.status}`);
  }
  return data;
}

export async function listKnowledgeBases(includeArchived = false): Promise<KnowledgeBase[]> {
  const data = await readJson<{ items?: KnowledgeBase[] }>(
    await fetch(`${API_BASE}/knowledge-bases?include_archived=${includeArchived}`),
  );
  return data.items || [];
}

export async function listParserEngines(): Promise<ParserEngineInfo[]> {
  const data = await readJson<ParserEnginesResponse>(await fetch(`${API_BASE}/parser-engines`));
  return data.items || [];
}

export async function uploadChatAttachment(file: File): Promise<ChatAttachment> {
  const form = new FormData();
  form.append("file", file);
  return readJson<ChatAttachment>(
    await fetch(`${API_BASE}/chat/attachments`, {
      method: "POST",
      body: form,
    }),
  );
}

export async function loadSessionMessages(
  sessionId: string,
  input?: { beforeTime?: string; limit?: number },
): Promise<MessagesLoadResponse> {
  const params = new URLSearchParams();
  if (input?.beforeTime) params.set("before_time", input.beforeTime);
  params.set("limit", String(input?.limit || 20));
  return readJson<MessagesLoadResponse>(
    await fetch(`${API_BASE}/api/v1/messages/${encodeURIComponent(sessionId)}/load?${params.toString()}`),
  );
}

export async function listRecentSessions(limit = 20): Promise<ChatSessionSummary[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  const data = await readJson<{ items?: ChatSessionSummary[] }>(
    await fetch(`${API_BASE}/api/v1/sessions/recent?${params.toString()}`),
  );
  return data.items || [];
}

export async function renameSession(sessionId: string, title: string): Promise<ChatSessionSummary> {
  return readJson<ChatSessionSummary>(
    await fetch(`${API_BASE}/api/v1/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }),
  );
}

export async function deleteSession(sessionId: string): Promise<{ session_id: string; deleted: boolean }> {
  return readJson<{ session_id: string; deleted: boolean }>(
    await fetch(`${API_BASE}/api/v1/sessions/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    }),
  );
}

export async function stopSessionGeneration(sessionId: string, messageId: string): Promise<{ stopped: boolean; status: string }> {
  return readJson<{ stopped: boolean; status: string }>(
    await fetch(`${API_BASE}/api/v1/sessions/${encodeURIComponent(sessionId)}/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_id: messageId }),
    }),
  );
}

export async function previewDocument(source: string, knowledgeBaseId?: string): Promise<DocumentProcessingPreview> {
  return readJson<DocumentProcessingPreview>(await fetch(`${API_BASE}/documents/parse`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source, knowledge_base_id: knowledgeBaseId || null }),
  }));
}

export async function createKnowledgeBase(input: {
  name: string;
  description?: string;
  type?: KnowledgeBaseType;
  is_default?: boolean;
  indexing_strategy?: Record<string, unknown>;
  provider_config?: Record<string, unknown>;
}): Promise<KnowledgeBase> {
  return readJson<KnowledgeBase>(
    await fetch(`${API_BASE}/knowledge-bases`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...input, type: input.type || "document" }),
    }),
  );
}

export async function updateKnowledgeBase(
  knowledgeBaseId: string,
  input: { name?: string; description?: string; is_default?: boolean; indexing_strategy?: Record<string, unknown> },
): Promise<KnowledgeBase> {
  return readJson<KnowledgeBase>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function archiveKnowledgeBase(knowledgeBaseId: string): Promise<KnowledgeBase> {
  return readJson<KnowledgeBase>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}`, { method: "DELETE" }),
  );
}

export async function listKnowledgeBaseDocuments(
  knowledgeBaseId: string,
  filters?: Partial<DocumentFilters>,
): Promise<DocumentItem[]> {
  const params = new URLSearchParams({ knowledge_base_id: knowledgeBaseId });
  if (filters) {
    for (const [key, value] of Object.entries(filters)) {
      if (value) params.set(key, value);
    }
  }
  const data = await readJson<{ items?: DocumentItem[] }>(
    await fetch(`${API_BASE}/documents?${params.toString()}`),
  );
  return data.items || [];
}

export async function retryDocumentEnrichment(documentId: string, knowledgeBaseId: string): Promise<DocumentItem> {
  return readJson<DocumentItem>(
    await fetch(
      `${API_BASE}/documents/${encodeURIComponent(documentId)}/enrichment/retry?knowledge_base_id=${encodeURIComponent(knowledgeBaseId)}`,
      { method: "POST" },
    ),
  );
}

export async function retryDocumentProcessing(documentId: string, knowledgeBaseId: string): Promise<DocumentItem> {
  return readJson<DocumentItem>(
    await fetch(
      `${API_BASE}/documents/${encodeURIComponent(documentId)}/processing/retry?knowledge_base_id=${encodeURIComponent(knowledgeBaseId)}`,
      { method: "POST" },
    ),
  );
}

export async function getDocumentProcessingTrace(documentId: string, knowledgeBaseId: string): Promise<DocumentProcessingTrace> {
  return readJson<DocumentProcessingTrace>(
    await fetch(
      `${API_BASE}/documents/${encodeURIComponent(documentId)}/processing-trace?knowledge_base_id=${encodeURIComponent(knowledgeBaseId)}`,
    ),
  );
}

export async function createUploadBatch(knowledgeBaseId: string, settings: UploadBatchSettings): Promise<UploadBatch> {
  return readJson<UploadBatch>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/upload-batches`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings }),
    }),
  );
}

export async function uploadBatchFile(
  knowledgeBaseId: string,
  batchId: string,
  file: File,
  relativePath: string,
): Promise<UploadBatch> {
  const form = new FormData();
  form.append("file", file);
  form.append("relative_path", relativePath);
  return readJson<UploadBatch>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/upload-batches/${encodeURIComponent(batchId)}/files`, {
      method: "POST",
      body: form,
    }),
  );
}

export async function confirmUploadBatch(knowledgeBaseId: string, batchId: string): Promise<UploadBatch> {
  return readJson<UploadBatch>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/upload-batches/${encodeURIComponent(batchId)}/confirm`, {
      method: "POST",
    }),
  );
}

export async function getUploadBatch(knowledgeBaseId: string, batchId: string): Promise<UploadBatch> {
  return readJson<UploadBatch>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/upload-batches/${encodeURIComponent(batchId)}`),
  );
}

export async function updateUploadBatchSettings(
  knowledgeBaseId: string,
  batchId: string,
  settings: UploadBatchSettings,
): Promise<UploadBatch> {
  return readJson<UploadBatch>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/upload-batches/${encodeURIComponent(batchId)}/settings`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings }),
    }),
  );
}

export async function cancelUploadBatch(knowledgeBaseId: string, batchId: string): Promise<UploadBatch> {
  return readJson<UploadBatch>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/upload-batches/${encodeURIComponent(batchId)}/cancel`, {
      method: "POST",
    }),
  );
}

export async function retryUploadBatchFile(knowledgeBaseId: string, batchId: string, fileId: string): Promise<UploadBatch> {
  return readJson<UploadBatch>(
    await fetch(
      `${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/upload-batches/${encodeURIComponent(batchId)}/files/${encodeURIComponent(fileId)}/retry`,
      { method: "POST" },
    ),
  );
}

export async function listWikiPages(
  knowledgeBaseId: string,
  filters?: { q?: string; status?: string; page_type?: string; folder_id?: string; limit?: number; cursor?: string },
): Promise<{ items: WikiPage[]; next_cursor?: string | null }> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters || {})) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return readJson<{ items: WikiPage[]; next_cursor?: string | null }>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/pages${suffix}`),
  );
}

export async function getWikiPage(knowledgeBaseId: string, slug: string): Promise<WikiPage> {
  return readJson<WikiPage>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/pages/${encodeURIComponent(slug)}`),
  );
}

export async function createWikiPage(
  knowledgeBaseId: string,
  input: Partial<WikiPage> & { title: string; source_refs?: WikiSourceRef[] },
): Promise<WikiPage> {
  return readJson<WikiPage>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/pages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function updateWikiPage(knowledgeBaseId: string, slug: string, input: Partial<WikiPage>): Promise<WikiPage> {
  return readJson<WikiPage>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/pages/${encodeURIComponent(slug)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function archiveWikiPage(knowledgeBaseId: string, slug: string): Promise<WikiPage> {
  return readJson<WikiPage>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/pages/${encodeURIComponent(slug)}`, {
      method: "DELETE",
    }),
  );
}

export async function moveWikiPage(
  knowledgeBaseId: string,
  slug: string,
  input: { folder_id?: string; category_path?: string[] },
): Promise<WikiPage> {
  return readJson<WikiPage>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/pages/${encodeURIComponent(slug)}/move`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function listWikiFolders(knowledgeBaseId: string, parentId = ""): Promise<WikiFolder[]> {
  const params = new URLSearchParams();
  if (parentId) params.set("parent_id", parentId);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const data = await readJson<{ items?: WikiFolder[] }>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/folders${suffix}`),
  );
  return data.items || [];
}

export async function createWikiFolder(
  knowledgeBaseId: string,
  input: { name: string; parent_id?: string; sort_order?: number },
): Promise<WikiFolder> {
  return readJson<WikiFolder>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/folders`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function updateWikiFolder(
  knowledgeBaseId: string,
  folderId: string,
  input: { name?: string; parent_id?: string; sort_order?: number },
): Promise<WikiFolder> {
  return readJson<WikiFolder>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/folders/${encodeURIComponent(folderId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function deleteWikiFolder(knowledgeBaseId: string, folderId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/folders/${encodeURIComponent(folderId)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const data = (await res.json()) as { detail?: string };
    throw new Error(data.detail || `璇锋眰澶辫触: ${res.status}`);
  }
}

export async function getWikiGraph(knowledgeBaseId: string, input?: { center_slug?: string; limit?: number }): Promise<WikiGraph> {
  const params = new URLSearchParams();
  if (input?.center_slug) params.set("center_slug", input.center_slug);
  if (input?.limit) params.set("limit", String(input.limit));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return readJson<WikiGraph>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/graph${suffix}`),
  );
}

export async function readWikiSourceDoc(
  knowledgeBaseId: string,
  input: { doc_id?: string; chunk_ids?: string[]; limit?: number },
): Promise<Record<string, unknown>> {
  return readJson<Record<string, unknown>>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/source-doc`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function listWikiGenerationTasks(
  knowledgeBaseId: string,
  filters?: { doc_id?: string; limit?: number },
): Promise<WikiGenerationTask[]> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters || {})) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const data = await readJson<{ items?: WikiGenerationTask[] }>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/generation-tasks${suffix}`),
  );
  return data.items || [];
}

export async function getWikiOverview(knowledgeBaseId: string): Promise<WikiOverview> {
  return readJson<WikiOverview>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/overview`),
  );
}

export async function listWikiLogs(
  knowledgeBaseId: string,
  input?: { limit?: number; cursor?: number },
): Promise<{ items: WikiLog[]; next_cursor?: number | null }> {
  const params = new URLSearchParams();
  if (input?.limit) params.set("limit", String(input.limit));
  if (input?.cursor) params.set("cursor", String(input.cursor));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return readJson<{ items: WikiLog[]; next_cursor?: number | null }>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/logs${suffix}`),
  );
}

export async function listWikiProcessingTasks(knowledgeBaseId: string, status = ""): Promise<WikiProcessingTask[]> {
  const suffix = status ? `?status=${encodeURIComponent(status)}` : "";
  const data = await readJson<{ items?: WikiProcessingTask[] }>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/processing-tasks${suffix}`),
  );
  return data.items || [];
}

export async function retryWikiProcessingTask(knowledgeBaseId: string, taskId: string): Promise<WikiProcessingTask> {
  return readJson<WikiProcessingTask>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/processing-tasks/${encodeURIComponent(taskId)}/retry`, { method: "POST" }),
  );
}

export async function cancelWikiProcessingTask(knowledgeBaseId: string, taskId: string): Promise<WikiProcessingTask> {
  return readJson<WikiProcessingTask>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/processing-tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" }),
  );
}

export async function generateWikiPage(
  knowledgeBaseId: string,
  input: { doc_id: string; auto_publish?: boolean; max_source_chunks?: number },
): Promise<{ task: WikiGenerationTask; page?: WikiPage | null; proposal?: WikiProposal | null }> {
  return readJson<{ task: WikiGenerationTask; page?: WikiPage | null; proposal?: WikiProposal | null }>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function listWikiIssues(
  knowledgeBaseId: string,
  filters?: { slug?: string; status?: string; issue_type?: string; limit?: number },
): Promise<WikiIssue[]> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters || {})) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const data = await readJson<{ items?: WikiIssue[] }>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/issues${suffix}`),
  );
  return data.items || [];
}

export async function createWikiIssue(
  knowledgeBaseId: string,
  input: { slug: string; issue_type?: string; description?: string; suspected_doc_ids?: string[]; suspected_chunk_ids?: string[] },
): Promise<WikiIssue> {
  return readJson<WikiIssue>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/issues`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function updateWikiIssue(knowledgeBaseId: string, issueId: string, status: string): Promise<WikiIssue> {
  return readJson<WikiIssue>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/issues/${encodeURIComponent(issueId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    }),
  );
}

export async function cleanupWikiIssue(knowledgeBaseId: string, issueId: string): Promise<{ issue: WikiIssue; proposal?: WikiProposal | null; cleaned: boolean }> {
  return readJson<{ issue: WikiIssue; proposal?: WikiProposal | null; cleaned: boolean }>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/issues/${encodeURIComponent(issueId)}/cleanup`, { method: "POST" }),
  );
}

export async function listWikiProposals(
  knowledgeBaseId: string,
  filters?: { slug?: string; status?: string; limit?: number },
): Promise<WikiProposal[]> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters || {})) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const data = await readJson<{ items?: WikiProposal[] }>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/proposals${suffix}`),
  );
  return data.items || [];
}

export async function createWikiProposal(
  knowledgeBaseId: string,
  input: Partial<WikiProposal> & { action: string; slug: string },
): Promise<WikiProposal> {
  return readJson<WikiProposal>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/proposals`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function applyWikiProposal(
  knowledgeBaseId: string,
  proposalId: string,
): Promise<{ proposal: WikiProposal; page?: WikiPage | null }> {
  return readJson<{ proposal: WikiProposal; page?: WikiPage | null }>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/proposals/${encodeURIComponent(proposalId)}/apply`, {
      method: "POST",
    }),
  );
}

export async function rejectWikiProposal(knowledgeBaseId: string, proposalId: string): Promise<WikiProposal> {
  return readJson<WikiProposal>(
    await fetch(`${API_BASE}/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/wiki/proposals/${encodeURIComponent(proposalId)}/reject`, {
      method: "POST",
    }),
  );
}
