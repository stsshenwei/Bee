"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useRouter } from "next/navigation";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { DocumentViewer } from "../components/DocumentViewer";
import { DeleteIcon, EditIcon, LibraryIcon, MoreIcon, TraceIcon, UploadIcon } from "../components/Icons";
import { WikiWorkspace } from "./WikiWorkspace";
import {
  API_BASE,
  archiveKnowledgeBase,
  cancelUploadBatch,
  confirmUploadBatch,
  createKnowledgeBase,
  createUploadBatch,
  getDocumentProcessingTrace,
  getWikiGraph,
  getWikiPage,
  getUploadBatch,
  generateWikiPage,
  listWikiGenerationTasks,
  listWikiIssues,
  listWikiPages,
  listWikiProposals,
  listKnowledgeBaseDocuments,
  listKnowledgeBases,
  previewDocument,
  readWikiSourceDoc,
  readJson,
  rejectWikiProposal,
  retryDocumentEnrichment,
  retryDocumentProcessing,
  retryUploadBatchFile,
  applyWikiProposal,
  updateWikiIssue,
  updateKnowledgeBase,
  uploadBatchFile,
} from "../lib/api";
import { applyKnowledgeBaseTypePreset, toKnowledgeBaseCreateInput, validateKnowledgeCreationSettings } from "../lib/knowledge-validation";
import { canRetryUploadFile, summarizeProcessingPreview, summarizeUploadFile } from "../lib/processing-ui";
import type {
  DocumentProcessingPreview,
  DocumentProcessingTrace,
  DocumentItem,
  DocumentFilters,
  DocumentViewMode,
  KnowledgeBase,
  KnowledgeBaseCreationSection,
  KnowledgeBaseType,
  KnowledgeCreationWizardSettings,
  UploadBatch,
  UploadBatchSettings,
  UploadFileTaskRecord,
  ProcessingTraceSpan,
  WikiGenerationTask,
  WikiGraph,
  WikiIssue,
  WikiPage,
  WikiProposal,
} from "../lib/types";

type BrowserFile = File & { webkitRelativePath?: string };
type PendingUploadFile = {
  id: string;
  file: File;
  relativePath: string;
  size: number;
};

const SUPPORTED_UPLOAD_EXTENSIONS = new Set([
  ".pdf",
  ".docx",
  ".html",
  ".htm",
  ".xlsx",
  ".xlsm",
  ".xls",
  ".md",
  ".markdown",
  ".txt",
  ".jpg",
  ".jpeg",
  ".png",
  ".gif",
  ".bmp",
  ".tiff",
  ".webp",
]);
const ACCEPTED_INPUT = Array.from(SUPPORTED_UPLOAD_EXTENSIONS).join(",");
function pathSuffix(name: string) {
  const index = name.lastIndexOf(".");
  return index >= 0 ? name.slice(index).toLowerCase() : "";
}
function fileTypeFromPath(name: string) {
  return pathSuffix(name).replace(/^\./, "") || "file";
}
const DEFAULT_DOCUMENT_FILTERS: DocumentFilters = {
  q: "",
  tag: "",
  file_type: "",
  status: "",
  source: "",
  created_from: "",
  created_to: "",
};
const DEFAULT_UPLOAD_SETTINGS: UploadBatchSettings = {
  parser_engine: "builtin",
  pdf_force_scanned: false,
  chunk_strategy: "auto",
  parent_chunk_size_chars: 4096,
  child_chunk_size_chars: 384,
  child_chunk_overlap_chars: 76,
  parent_child_enabled: true,
  dense_enabled: true,
  keyword_enabled: true,
  question_generation_enabled: false,
  graph_enabled: false,
  ocr_enabled: false,
  multimodal_enabled: false,
  audio_enabled: false,
};
const UPLOAD_BATCH_TERMINAL_STATUSES = new Set(["completed", "partial_failed", "failed", "canceled"]);

const DEFAULT_WIZARD: KnowledgeCreationWizardSettings = {
  name: "",
  description: "",
  type: "document",
  isDefault: false,
  activeSection: "basic",
  indexingStrategy: {
    dense_enabled: true,
    keyword_enabled: true,
    graph_enabled: false,
    wiki_enabled: false,
    wiki_generation_enabled: true,
    wiki_auto_publish_enabled: false,
  },
  parser: {
    engine: "builtin",
    readOnly: true,
  },
  chunking: {
    strategy: "auto",
    parent_chunk_size_chars: 4096,
    child_chunk_size_chars: 384,
    child_chunk_overlap_chars: 76,
    parent_child_enabled: true,
  },
  processing: {
    question_generation_enabled: false,
    enrichment_enabled: false,
    ocr_enabled: false,
    multimodal_enabled: false,
    audio_enabled: false,
  },
};

const CREATION_SECTIONS: Array<{ id: KnowledgeBaseCreationSection; label: string; disabled?: boolean }> = [
  { id: "basic", label: "基本信息" },
  { id: "type", label: "知识库类型" },
  { id: "model", label: "模型配置" },
  { id: "vector", label: "向量存储" },
  { id: "parser", label: "解析引擎" },
  { id: "chunking", label: "分段设置" },
  { id: "image_ocr", label: "图片 / OCR" },
  { id: "audio", label: "音频处理", disabled: true },
  { id: "graph", label: "知识图谱" },
  { id: "advanced", label: "高级设置" },
];

export default function KnowledgePage() {
  const router = useRouter();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [documentFilters, setDocumentFilters] = useState<DocumentFilters>(DEFAULT_DOCUMENT_FILTERS);
  const [documentViewMode, setDocumentViewMode] = useState<DocumentViewMode>("grid");
  const [selectedDocumentIds, setSelectedDocumentIds] = useState<string[]>([]);
  const [bulkStatus, setBulkStatus] = useState("");
  const [loading, setLoading] = useState(true);
  const [documentLoading, setDocumentLoading] = useState(false);
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [settingsTarget, setSettingsTarget] = useState<KnowledgeBase | null>(null);
  const [settingsName, setSettingsName] = useState("");
  const [settingsDescription, setSettingsDescription] = useState("");
  const [settingsIndexingStrategy, setSettingsIndexingStrategy] = useState<KnowledgeBase["indexing_strategy"]>(DEFAULT_WIZARD.indexingStrategy);
  const [wizard, setWizard] = useState<KnowledgeCreationWizardSettings>(DEFAULT_WIZARD);
  const [formError, setFormError] = useState("");
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadStatus, setUploadStatus] = useState("");
  const [uploadMenuOpen, setUploadMenuOpen] = useState(false);
  const [pendingUploadFiles, setPendingUploadFiles] = useState<PendingUploadFile[]>([]);
  const [uploadDialogOpen, setUploadDialogOpen] = useState(false);
  const [uploadSettings, setUploadSettings] = useState<UploadBatchSettings>(DEFAULT_UPLOAD_SETTINGS);
  const [activeBatch, setActiveBatch] = useState<UploadBatch | null>(null);
  const [uploadPlaceholderDocuments, setUploadPlaceholderDocuments] = useState<DocumentItem[]>([]);
  const [processingPreview, setProcessingPreview] = useState<{
    loading: boolean;
    error: string;
    file?: string;
    data?: DocumentProcessingPreview;
  }>({ loading: false, error: "" });
  const [traceDrawer, setTraceDrawer] = useState<{
    open: boolean;
    loading: boolean;
    refreshing: boolean;
    error: string;
    document?: DocumentItem;
    data?: DocumentProcessingTrace;
  }>({ open: false, loading: false, refreshing: false, error: "" });
  const [retryingId, setRetryingId] = useState("");
  const [viewer, setViewer] = useState({
    open: false,
    source: "",
    loading: false,
    error: "",
    mode: "text" as "text" | "pdf",
    content: "",
    fileUrl: "",
  });

  const selected = knowledgeBases.find((item) => item.id === selectedId);
  const displayedDocuments = mergeUploadPlaceholders(documents, uploadPlaceholderDocuments);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setSelectedId(params.get("kb") || "");
    void loadKnowledgeBases();
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setDocuments([]);
      setUploadPlaceholderDocuments([]);
      return;
    }
    void loadDocuments(selectedId, true, documentFilters);
  }, [selectedId, documentFilters]);

  useEffect(() => {
    if (!selectedId) return;
    const hasProcessingDocuments = documents.some((item) => (
      ["pending", "parsing"].includes(item.parse_status || "") ||
      ["pending", "processing"].includes(item.summary_status || "")
    ));
    const hasProcessingBatch = activeBatch ? !UPLOAD_BATCH_TERMINAL_STATUSES.has(activeBatch.status) : false;
    if (!hasProcessingDocuments && !hasProcessingBatch) return;
    const timer = window.setInterval(() => {
      void loadDocuments(selectedId, false, documentFilters);
      void loadKnowledgeBases();
      if (activeBatch) void refreshActiveBatch();
    }, 2500);
    return () => window.clearInterval(timer);
  }, [documents, selectedId, documentFilters, activeBatch]);

  useEffect(() => {
    if (!uploadStatus) return;
    if (uploadDialogOpen) return;
    const hasProcessingDocuments = documents.some((item) => (
      ["pending", "parsing"].includes(item.parse_status || "") ||
      ["pending", "processing"].includes(item.summary_status || "")
    ));
    const batchFinished = activeBatch ? UPLOAD_BATCH_TERMINAL_STATUSES.has(activeBatch.status) : false;
    const knowledgeBaseIdle = selected ? selected.aggregate.processing_count === 0 : false;
    if (batchFinished || (knowledgeBaseIdle && !hasProcessingDocuments)) {
      setUploadStatus("");
    }
  }, [activeBatch?.status, documents, selected?.aggregate.processing_count, uploadDialogOpen, uploadStatus]);

  async function loadKnowledgeBases() {
    setLoading(true);
    setError("");
    try {
      setKnowledgeBases(await listKnowledgeBases());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "知识库加载失败");
    } finally {
      setLoading(false);
    }
  }

  async function loadDocuments(knowledgeBaseId: string, showLoading = true, filters = documentFilters) {
    if (showLoading) setDocumentLoading(true);
    setError("");
    try {
      setDocuments(await listKnowledgeBaseDocuments(knowledgeBaseId, filters));
      setSelectedDocumentIds([]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "文档加载失败");
    } finally {
      if (showLoading) setDocumentLoading(false);
    }
  }

  function openKnowledgeBase(id: string) {
    setSelectedId(id);
    router.replace(`/knowledge?kb=${encodeURIComponent(id)}`);
  }

  function closeKnowledgeBase() {
    setSelectedId("");
    router.replace("/knowledge");
    void loadKnowledgeBases();
  }

  function openSettingsFor(item: KnowledgeBase) {
    setSettingsName(item.name);
    setSettingsDescription(item.description);
    setSettingsIndexingStrategy({ ...item.indexing_strategy });
    setFormError("");
    setSettingsTarget(item);
  }

  function openCreateWizard() {
    setWizard(DEFAULT_WIZARD);
    setFormError("");
    setCreateOpen(true);
  }

  async function submitCreate() {
    const validation = validateKnowledgeCreationSettings(wizard);
    if (!validation.ok) {
      setFormError(validation.message);
      setWizard((current) => ({ ...current, activeSection: validation.section }));
      return;
    }
    setSaving(true);
    setFormError("");
    try {
      const created = await createKnowledgeBase(toKnowledgeBaseCreateInput(wizard));
      setCreateOpen(false);
      await loadKnowledgeBases();
      openKnowledgeBase(created.id);
    } catch (cause) {
      setFormError(cause instanceof Error ? cause.message : "创建失败");
    } finally {
      setSaving(false);
    }
  }

  async function submitSettings() {
    if (!settingsTarget) return;
    setSaving(true);
    setFormError("");
    try {
      await updateKnowledgeBase(settingsTarget.id, {
        name: settingsName,
        description: settingsDescription,
        indexing_strategy: settingsIndexingStrategy,
      });
      setSettingsTarget(null);
      await loadKnowledgeBases();
    } catch (cause) {
      setFormError(cause instanceof Error ? cause.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  async function deleteKnowledgeBase(target: KnowledgeBase) {
    if (target.is_default) {
      setFormError("默认知识库不能删除，请先将其他知识库设为默认。");
      return;
    }
    if (!window.confirm(`删除“${target.name}”？删除后将从列表中移除，不能继续上传或检索；底层数据会暂时保留以便恢复。`)) return;
    setSaving(true);
    setFormError("");
    try {
      await archiveKnowledgeBase(target.id);
      setSettingsTarget(null);
      setKnowledgeBases((current) => current.filter((item) => item.id !== target.id));
      if (selectedId === target.id) closeKnowledgeBase();
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "知识库删除失败";
      if (settingsTarget?.id === target.id) setFormError(message);
      else setError(message);
    } finally {
      setSaving(false);
    }
  }

  function uploadFiles(files: FileList | null) {
    if (!files?.length || !selected) return;
    const allFiles = Array.from(files);
    const supportedFiles = allFiles.filter((sourceFile) => SUPPORTED_UPLOAD_EXTENSIONS.has(pathSuffix(sourceFile.name)));
    const skippedCount = allFiles.length - supportedFiles.length;
    if (!supportedFiles.length) {
      setError("没有可上传的文件。当前支持 PDF、DOCX、TXT/Markdown、HTML、Excel 和常见图片格式。");
      if (fileInputRef.current) fileInputRef.current.value = "";
      if (folderInputRef.current) folderInputRef.current.value = "";
      return;
    }
    const pending = supportedFiles.map((sourceFile, index) => {
      const file = sourceFile as BrowserFile;
      return {
        id: `${file.name}-${file.size}-${file.lastModified}-${index}`,
        file,
        relativePath: file.webkitRelativePath || file.name,
        size: file.size,
      };
    });
    setPendingUploadFiles(pending);
    setUploadSettings(DEFAULT_UPLOAD_SETTINGS);
    setActiveBatch(null);
    setUploadPlaceholderDocuments([]);
    setUploadStatus(skippedCount ? `已选择 ${pending.length} 个可上传文件，已跳过 ${skippedCount} 个暂不支持的文件。` : "");
    setError("");
    setUploadDialogOpen(true);
    setUploadMenuOpen(false);
    if (fileInputRef.current) fileInputRef.current.value = "";
    if (folderInputRef.current) folderInputRef.current.value = "";
  }

  function removePendingUploadFile(id: string) {
    setPendingUploadFiles((current) => current.filter((item) => item.id !== id));
  }

  async function cancelPendingUpload() {
    if (selected && activeBatch && !UPLOAD_BATCH_TERMINAL_STATUSES.has(activeBatch.status)) {
      try {
        const canceled = await cancelUploadBatch(selected.id, activeBatch.id);
        setActiveBatch(canceled);
      } catch {
        // Dialog cancellation should still close locally; backend status can be refreshed later.
      }
    }
    setPendingUploadFiles([]);
    setUploadDialogOpen(false);
    setUploading(false);
  }

  async function confirmPendingUpload() {
    if (!selected || !pendingUploadFiles.length) return;
    setUploading(true);
    setError("");
    setUploadStatus("正在创建上传批次...");
    try {
      let batch = await createUploadBatch(selected.id, uploadSettings);
      setActiveBatch(batch);
      for (let index = 0; index < pendingUploadFiles.length; index += 1) {
        const item = pendingUploadFiles[index];
        setUploadStatus(`正在传输 ${index + 1}/${pendingUploadFiles.length}: ${item.relativePath}`);
        batch = await uploadBatchFile(selected.id, batch.id, item.file, item.relativePath);
        setActiveBatch(batch);
      }
      setUploadStatus("文件已传输，正在确认解析和索引...");
      batch = await confirmUploadBatch(selected.id, batch.id);
      setActiveBatch(batch);
      setUploadStatus(`批次 ${batch.status}: 完成 ${batch.aggregate.completed}/${batch.aggregate.total}，失败 ${batch.aggregate.failed}`);
      await loadDocuments(selected.id, true, documentFilters);
      await loadKnowledgeBases();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "批次上传失败");
    } finally {
      setUploading(false);
    }
  }

  async function submitPendingUploadInBackground() {
    if (!selected || !pendingUploadFiles.length) return;
    setUploading(true);
    setError("");
    setUploadStatus("正在创建上传批次...");
    try {
      let batch = await createUploadBatch(selected.id, uploadSettings);
      setActiveBatch(batch);
      const failedUploads: string[] = [];
      for (let index = 0; index < pendingUploadFiles.length; index += 1) {
        const item = pendingUploadFiles[index];
        setUploadStatus(`正在上传 ${index + 1}/${pendingUploadFiles.length}: ${item.relativePath}`);
        try {
          batch = await uploadBatchFile(selected.id, batch.id, item.file, item.relativePath);
          setActiveBatch(batch);
        } catch (cause) {
          failedUploads.push(`${item.relativePath}: ${cause instanceof Error ? cause.message : "上传失败"}`);
        }
      }
      if (!batch.aggregate.total) {
        throw new Error(failedUploads[0] || "没有文件成功上传");
      }
      setUploadStatus("文件已上传，正在提交后台解析...");
      batch = await confirmUploadBatch(selected.id, batch.id);
      setActiveBatch(batch);
      setUploadPlaceholderDocuments(buildUploadPlaceholderDocuments(batch, selected));
      setUploadStatus(
        failedUploads.length
          ? `已提交 ${batch.aggregate.total} 个文件后台处理，${failedUploads.length} 个文件上传失败。`
          : `已提交后台处理：${batch.aggregate.total} 个文件正在排队解析`,
      );
      setUploadDialogOpen(false);
      setPendingUploadFiles([]);
      await loadDocuments(selected.id, false, documentFilters);
      await loadKnowledgeBases();
      if (failedUploads.length) {
        setError(`部分文件未上传：${failedUploads.slice(0, 3).join("；")}${failedUploads.length > 3 ? "；..." : ""}`);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "批次上传失败");
    } finally {
      setUploading(false);
    }
  }

  async function refreshActiveBatch() {
    if (!selected || !activeBatch) return;
    const batch = await getUploadBatch(selected.id, activeBatch.id);
    setActiveBatch(batch);
    setUploadPlaceholderDocuments(buildUploadPlaceholderDocuments(batch, selected));
  }

  async function retryUploadFileTask(fileId: string) {
    if (!selected || !activeBatch) return;
    setUploading(true);
    try {
      const batch = await retryUploadBatchFile(selected.id, activeBatch.id, fileId);
      setActiveBatch(batch);
      await loadDocuments(selected.id, true, documentFilters);
      await loadKnowledgeBases();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "文件任务重试失败");
    } finally {
      setUploading(false);
    }
  }

  async function previewUploadFileTask(file: UploadFileTaskRecord) {
    if (!selected || !file.storage_path) return;
    setProcessingPreview({ loading: true, error: "", file: file.relative_path || file.original_name });
    try {
      const data = await previewDocument(file.storage_path, selected.id);
      setProcessingPreview({ loading: false, error: "", file: file.relative_path || file.original_name, data });
    } catch (cause) {
      setProcessingPreview({
        loading: false,
        error: cause instanceof Error ? cause.message : "Processing preview failed",
        file: file.relative_path || file.original_name,
      });
    }
  }

  async function openDocument(item: DocumentItem) {
    const source = item.source || item.storage_path;
    const query = `source=${encodeURIComponent(source)}&knowledge_base_id=${encodeURIComponent(item.knowledge_base_id)}`;
    if (source.toLowerCase().endsWith(".pdf")) {
      setViewer({ open: true, source, loading: false, error: "", mode: "pdf", content: "", fileUrl: `${API_BASE}/documents/file?${query}` });
      return;
    }
    setViewer({ open: true, source, loading: true, error: "", mode: "text", content: "", fileUrl: "" });
    try {
      const data = await readJson<{ content?: string }>(await fetch(`${API_BASE}/documents/content?${query}`));
      setViewer((current) => ({ ...current, loading: false, content: data.content || "" }));
    } catch (cause) {
      setViewer((current) => ({ ...current, loading: false, error: cause instanceof Error ? cause.message : "预览失败" }));
    }
  }

  function openDocumentDetail(item: DocumentItem) {
    const knowledgeBaseId = item.knowledge_base_id || selected?.id || "";
    if (!knowledgeBaseId) return;
    router.push(`/knowledge/document?kb=${encodeURIComponent(knowledgeBaseId)}&doc=${encodeURIComponent(item.id)}`);
  }

  async function deleteDocument(item: DocumentItem) {
    if (!selected || !window.confirm(`删除“${item.name}”？源文件和该知识库中的索引都会删除。`)) return;
    await readJson(
      await fetch(`${API_BASE}/rag/documents/${encodeURIComponent(item.id)}?knowledge_base_id=${encodeURIComponent(selected.id)}`, { method: "DELETE" }),
    );
    setDocuments((current) => current.filter((doc) => doc.id !== item.id));
    setSelectedDocumentIds((current) => current.filter((id) => id !== item.id));
    await loadKnowledgeBases();
  }

  async function bulkDeleteDocuments() {
    if (!selected || !selectedDocumentIds.length) return;
    const selectedDocs = documents.filter((item) => selectedDocumentIds.includes(item.id));
    if (!window.confirm(`删除选中的 ${selectedDocs.length} 个文档？所有请求都会携带当前知识库范围。`)) return;
    setBulkStatus("正在执行批量删除...");
    const failed: string[] = [];
    let removed = 0;
    for (const item of selectedDocs) {
      try {
        await readJson(
          await fetch(`${API_BASE}/rag/documents/${encodeURIComponent(item.id)}?knowledge_base_id=${encodeURIComponent(selected.id)}`, { method: "DELETE" }),
        );
        removed += 1;
        setDocuments((current) => current.filter((doc) => doc.id !== item.id));
        setSelectedDocumentIds((current) => current.filter((id) => id !== item.id));
        setBulkStatus(`正在批量删除：已删除 ${removed}/${selectedDocs.length}`);
      } catch {
        failed.push(item.name);
      }
    }
    if (failed.length) {
      await loadDocuments(selected.id, false, documentFilters);
    }
    await loadKnowledgeBases();
    setBulkStatus(failed.length ? `批量删除部分失败：${failed.join(", ")}` : `已删除 ${selectedDocs.length} 个文档`);
  }

  async function retrySummary(item: DocumentItem) {
    if (!selected) return;
    setRetryingId(item.id);
    setError("");
    try {
      await retryDocumentEnrichment(item.id, selected.id);
      await loadDocuments(selected.id, false, documentFilters);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "概要重试失败");
    } finally {
      setRetryingId("");
    }
  }

  async function retryProcessing(item: DocumentItem) {
    if (!selected) return;
    setRetryingId(item.id);
    setError("");
    try {
      await retryDocumentProcessing(item.id, selected.id);
      await loadDocuments(selected.id, false, documentFilters);
      if (traceDrawer.open && traceDrawer.document?.id === item.id) {
        await refreshProcessingTrace();
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "处理任务重试失败");
    } finally {
      setRetryingId("");
    }
  }

  async function openProcessingTrace(item: DocumentItem) {
    if (!selected) return;
    setTraceDrawer({ open: true, loading: true, refreshing: false, error: "", document: item });
    try {
      const data = await getDocumentProcessingTrace(item.id, selected.id);
      setTraceDrawer({ open: true, loading: false, refreshing: false, error: "", document: item, data });
    } catch (cause) {
      setTraceDrawer({
        open: true,
        loading: false,
        refreshing: false,
        error: cause instanceof Error ? cause.message : "处理链路加载失败",
        document: item,
      });
    }
  }

  async function openUploadFileTrace(file: UploadFileTaskRecord) {
    if (!selected || !file.document_id) return;
    const item: DocumentItem = {
      id: file.document_id,
      workspace_id: file.workspace_id,
      knowledge_base_id: file.knowledge_base_id || selected.id,
      name: file.relative_path || file.original_name,
      file_type: fileTypeFromPath(file.relative_path || file.original_name),
      storage_path: file.storage_path,
      parse_status: file.status === "failed" ? "failed" : file.status === "completed" ? "parsed" : "parsing",
      created_at: file.created_at,
      updated_at: file.updated_at,
      chunks: file.chunks,
      metadata_json: {},
      source: file.storage_path,
      processing_task_status: file.status,
      processing_last_error: file.error_message,
    };
    await openProcessingTrace(item);
  }

  async function refreshProcessingTrace() {
    if (!selected || !traceDrawer.document) return;
    const item = traceDrawer.document;
    setTraceDrawer((current) => ({ ...current, refreshing: true }));
    try {
      const data = await getDocumentProcessingTrace(item.id, selected.id);
      setTraceDrawer((current) => ({ ...current, refreshing: false, error: "", data }));
    } catch (cause) {
      setTraceDrawer((current) => ({
        ...current,
        refreshing: false,
        error: cause instanceof Error ? cause.message : "处理链路刷新失败",
      }));
    }
  }

  useEffect(() => {
    const trace = traceDrawer.data?.trace;
    if (!traceDrawer.open || !trace || !["running", "pending"].includes(trace.status)) return;
    const timer = window.setInterval(() => void refreshProcessingTrace(), 2500);
    return () => window.clearInterval(timer);
  }, [traceDrawer.open, traceDrawer.data?.trace?.status, traceDrawer.document?.id, selected?.id]);

  function startChat() {
    if (!selected) return;
    window.localStorage.setItem("bee:selectedKnowledgeBaseIds", JSON.stringify([selected.id]));
    router.push("/chat");
  }

  if (selected) {
    return (
      <KnowledgeBaseDetailShell
        selected={selected}
        documents={displayedDocuments}
        documentLoading={documentLoading}
        error={error}
        uploadStatus={uploadStatus}
        bulkStatus={bulkStatus}
        uploadMenuOpen={uploadMenuOpen}
        uploadDialogOpen={uploadDialogOpen}
        pendingUploadFiles={pendingUploadFiles}
        uploadSettings={uploadSettings}
        activeBatch={activeBatch}
        processingPreview={processingPreview}
        filters={documentFilters}
        viewMode={documentViewMode}
        selectedDocumentIds={selectedDocumentIds}
        uploading={uploading}
        retryingId={retryingId}
        fileInputRef={fileInputRef}
        folderInputRef={folderInputRef}
        onBack={closeKnowledgeBase}
        onStartChat={startChat}
        onOpenSettings={() => {
          openSettingsFor(selected);
        }}
        onFiltersChange={setDocumentFilters}
        onViewModeChange={setDocumentViewMode}
        onRefresh={() => void loadDocuments(selected.id, true, documentFilters)}
        onSelectedDocumentIdsChange={setSelectedDocumentIds}
        onBulkDelete={() => void bulkDeleteDocuments()}
        onUploadMenuOpenChange={setUploadMenuOpen}
        onUpload={uploadFiles}
        onRemovePendingUploadFile={removePendingUploadFile}
        onCancelPendingUpload={() => void cancelPendingUpload()}
        onConfirmPendingUpload={() => void submitPendingUploadInBackground()}
        onUploadSettingsChange={setUploadSettings}
        onOpenDocument={openDocument}
        onOpenDocumentDetail={openDocumentDetail}
        onDeleteDocument={deleteDocument}
        onRetrySummary={retrySummary}
        onRetryProcessing={retryProcessing}
        onOpenTrace={openProcessingTrace}
        settingsDialog={
          settingsTarget ? (
            <KnowledgeBaseSettingsDialog
              selected={settingsTarget}
              name={settingsName}
              description={settingsDescription}
              indexingStrategy={settingsIndexingStrategy}
              error={formError}
              saving={saving}
              onName={setSettingsName}
              onDescription={setSettingsDescription}
              onIndexingStrategy={setSettingsIndexingStrategy}
              onCancel={() => setSettingsTarget(null)}
              onSubmit={() => void submitSettings()}
              onDelete={!settingsTarget.is_default ? () => void deleteKnowledgeBase(settingsTarget) : undefined}
            />
          ) : null
        }
        viewer={
          <DocumentViewer
            open={viewer.open}
            source={viewer.source}
            loading={viewer.loading}
            error={viewer.error}
            mode={viewer.mode}
            content={viewer.content}
            fileUrl={viewer.fileUrl}
            onClose={() => setViewer((current) => ({ ...current, open: false }))}
          />
        }
        traceDrawer={
          <ProcessingTraceDrawer
            state={traceDrawer}
            onRefresh={() => void refreshProcessingTrace()}
            onClose={() => setTraceDrawer({ open: false, loading: false, refreshing: false, error: "" })}
          />
        }
      />
    );
  }

  return (
    <KnowledgeCatalog
      knowledgeBases={knowledgeBases}
      loading={loading}
      error={error}
      createOpen={createOpen}
      wizard={wizard}
      wizardError={formError}
      saving={saving}
      onRefresh={() => void loadKnowledgeBases()}
      onOpenKnowledgeBase={openKnowledgeBase}
      onEditKnowledgeBase={openSettingsFor}
      onDeleteKnowledgeBase={(item) => void deleteKnowledgeBase(item)}
      onOpenCreate={openCreateWizard}
      onCloseCreate={() => setCreateOpen(false)}
      onWizardChange={setWizard}
      onSubmitCreate={() => void submitCreate()}
      settingsDialog={
        settingsTarget ? (
          <KnowledgeBaseSettingsDialog
            selected={settingsTarget}
            name={settingsName}
            description={settingsDescription}
            indexingStrategy={settingsIndexingStrategy}
            error={formError}
            saving={saving}
            onName={setSettingsName}
            onDescription={setSettingsDescription}
            onIndexingStrategy={setSettingsIndexingStrategy}
            onCancel={() => setSettingsTarget(null)}
            onSubmit={() => void submitSettings()}
            onDelete={!settingsTarget.is_default ? () => void deleteKnowledgeBase(settingsTarget) : undefined}
          />
        ) : null
      }
    />
  );
}

function KnowledgeCatalog({
  knowledgeBases,
  loading,
  error,
  createOpen,
  wizard,
  wizardError,
  saving,
  onRefresh,
  onOpenKnowledgeBase,
  onEditKnowledgeBase,
  onDeleteKnowledgeBase,
  onOpenCreate,
  onCloseCreate,
  onWizardChange,
  onSubmitCreate,
  settingsDialog,
}: {
  knowledgeBases: KnowledgeBase[];
  loading: boolean;
  error: string;
  createOpen: boolean;
  wizard: KnowledgeCreationWizardSettings;
  wizardError: string;
  saving: boolean;
  onRefresh: () => void;
  onOpenKnowledgeBase: (id: string) => void;
  onEditKnowledgeBase: (item: KnowledgeBase) => void;
  onDeleteKnowledgeBase: (item: KnowledgeBase) => void;
  onOpenCreate: () => void;
  onCloseCreate: () => void;
  onWizardChange: (settings: KnowledgeCreationWizardSettings) => void;
  onSubmitCreate: () => void;
  settingsDialog: ReactNode;
}) {
  return (
    <section className="knowledge-page kb-catalog-page">
      <header className="knowledge-header kb-catalog-header">
        <div>
          <h1>知识库</h1>
          <p>管理工作空间中的文档知识、检索范围与处理状态。</p>
        </div>
      </header>
      <div className="kb-catalog-layout">
        <div className="kb-catalog-content">
          <div className="kb-toolbar">
            <div className="kb-toolbar-title">
              <strong>全部知识库</strong>
              <span>{knowledgeBases.length} 个知识库</span>
            </div>
            <div className="kb-toolbar-actions">
              <button type="button" onClick={onRefresh}>刷新</button>
              <button className="kb-create-button" type="button" onClick={onOpenCreate}>＋ 创建知识库</button>
            </div>
          </div>
          {error ? <div className="notice error">{error}</div> : null}
          {loading ? <div className="notice">正在加载知识库...</div> : null}
          {!loading && !knowledgeBases.length ? (
            <div className="kb-empty">
              <LibraryIcon />
              <h2>暂无知识库</h2>
              <p>创建一个 Document 知识库开始整理资料。</p>
            </div>
          ) : null}
          <div className="kb-card-grid">
            {knowledgeBases.map((item) => (
              <KnowledgeBaseCard
                key={item.id}
                item={item}
                onOpen={() => onOpenKnowledgeBase(item.id)}
                onEdit={(event) => {
                  event.stopPropagation();
                  onEditKnowledgeBase(item);
                }}
                onDelete={(event) => {
                  event.stopPropagation();
                  onDeleteKnowledgeBase(item);
                }}
              />
            ))}
          </div>
        </div>
      </div>
      {createOpen ? (
        <KnowledgeBaseCreateWizard
          settings={wizard}
          error={wizardError}
          saving={saving}
          onChange={onWizardChange}
          onCancel={onCloseCreate}
          onSubmit={onSubmitCreate}
        />
      ) : null}
      {settingsDialog}
    </section>
  );
}

function KnowledgeBaseCard({
  item,
  onOpen,
  onEdit,
  onDelete,
}: {
  item: KnowledgeBase;
  onOpen: () => void;
  onEdit: (event: React.MouseEvent<HTMLButtonElement>) => void;
  onDelete: (event: React.MouseEvent<HTMLButtonElement>) => void;
}) {
  const typeLabel = knowledgeBaseTypeLabel(item.type);
  return (
    <article className="kb-card" onClick={onOpen}>
      <div className="kb-card-head">
        <div>
          <h2>{item.name}</h2>
          <span>{typeLabel}{item.is_default ? " · 默认" : ""}</span>
        </div>
        <div className="kb-card-actions">
          <button type="button" aria-label="编辑知识库" title="编辑知识库" onClick={onEdit}>
            <EditIcon />
          </button>
          {!item.is_default ? (
            <button type="button" className="danger-icon" aria-label="删除知识库" title="删除知识库" onClick={onDelete}>
              <DeleteIcon />
            </button>
          ) : null}
        </div>
      </div>
      {item.description ? <p>{item.description}</p> : null}
      <div className="kb-card-stats">
        <span>文档 {item.aggregate.document_count}</span>
        <span>分块 {item.aggregate.indexed_chunk_count}</span>
        {item.aggregate.processing_count ? <span className="metric-warning">处理中 {item.aggregate.processing_count}</span> : null}
        {item.aggregate.failed_count ? <span className="metric-error">失败 {item.aggregate.failed_count}</span> : null}
        {item.provider_config?.inactive_overrides?.length ? <span className="metric-warning">有未生效配置</span> : null}
      </div>
    </article>
  );
}

function knowledgeBaseTypeLabel(type: KnowledgeBaseType): string {
  const labels: Record<KnowledgeBaseType, string> = {
    document: "Document",
    faq: "FAQ",
    wiki: "Wiki",
    future: "更多类型",
  };
  return labels[type] || type;
}

function KnowledgeBaseCreateWizard({
  settings,
  error,
  saving,
  onChange,
  onCancel,
  onSubmit,
}: {
  settings: KnowledgeCreationWizardSettings;
  error: string;
  saving: boolean;
  onChange: (settings: KnowledgeCreationWizardSettings) => void;
  onCancel: () => void;
  onSubmit: () => void;
}) {
  const setSection = (activeSection: KnowledgeBaseCreationSection) => onChange({ ...settings, activeSection });
  return (
    <div className="dialog-mask" role="presentation" onClick={onCancel}>
      <section className="kb-dialog kb-create-wizard" role="dialog" aria-modal="true" aria-label="创建知识库" onClick={(event) => event.stopPropagation()}>
        <header>
          <div>
            <h2>创建知识库</h2>
            <p>配置一个 Bee Document 知识库。不可用能力会明确显示为未启用。</p>
          </div>
          <button type="button" onClick={onCancel} aria-label="关闭">
            ×
          </button>
        </header>
        <div className="kb-wizard-body">
          <nav className="kb-wizard-rail" aria-label="创建配置">
            {CREATION_SECTIONS.map((section) => (
              <button
                key={section.id}
                type="button"
                className={settings.activeSection === section.id ? "active" : ""}
                disabled={section.disabled}
                onClick={() => setSection(section.id)}
              >
                <span>{section.label}</span>
                {section.disabled ? <em>未开放</em> : null}
              </button>
            ))}
          </nav>
          <div className="kb-wizard-panel">{renderWizardPanel(settings, onChange)}</div>
        </div>
        {error ? <p className="feedback-err">{error}</p> : null}
        <div className="kb-effective-config">
          <strong>当前有效配置</strong>
          <span>Document 类型 / 默认解析器 / 默认向量存储 / Dense + Keyword 检索</span>
          <span>图谱、OCR、多模态、音频等仅在后端可用时才会生效。</span>
        </div>
        <div className="kb-dialog-actions">
          <span />
          <button type="button" onClick={onCancel}>
            取消
          </button>
          <button type="button" className="primary-action" disabled={saving} onClick={onSubmit}>
            {saving ? "创建中..." : "创建并进入知识库"}
          </button>
        </div>
      </section>
    </div>
  );
}

function renderWizardPanel(settings: KnowledgeCreationWizardSettings, onChange: (settings: KnowledgeCreationWizardSettings) => void) {
  if (settings.activeSection === "basic") {
    return (
      <div className="kb-wizard-section">
        <h3>基本信息</h3>
        <label>
          <span>名称</span>
          <input autoFocus value={settings.name} maxLength={80} onChange={(event) => onChange({ ...settings, name: event.target.value })} />
        </label>
        <label>
          <span>描述</span>
          <textarea value={settings.description} maxLength={300} onChange={(event) => onChange({ ...settings, description: event.target.value })} />
        </label>
        <label className="kb-check-row">
          <input type="checkbox" checked={settings.isDefault} onChange={(event) => onChange({ ...settings, isDefault: event.target.checked })} />
          <span>设为默认知识库</span>
        </label>
      </div>
    );
  }
  if (settings.activeSection === "type") {
    return (
      <div className="kb-wizard-section">
        <h3>知识库类型</h3>
        <div className="kb-type-grid">
          <button type="button" className={settings.type === "document" ? "selected" : ""} onClick={() => onChange(applyKnowledgeBaseTypePreset(settings, "document"))}>
            <strong>Document</strong>
            <span>上传 PDF、Word、Markdown、表格等文档。</span>
          </button>
          {(["faq", "wiki"] as const).map((type) => (
            <button key={type} type="button" className={settings.type === type ? "selected" : ""} onClick={() => onChange(applyKnowledgeBaseTypePreset(settings, type))}>
              <strong>{type === "faq" ? "FAQ" : "Wiki"}</strong>
              <span>{type === "faq" ? "适合问答条目和标准回复。" : "适合结构化主题和说明页面。"}</span>
            </button>
          ))}
          <button type="button" disabled>
            <strong>更多类型</strong>
            <span>暂未开放</span>
          </button>
        </div>
        <div className="kb-indexing-controls" aria-label="索引策略">
          {([
            ["wiki_enabled", "Wiki"],
            ["dense_enabled", "Dense"],
            ["keyword_enabled", "Keyword"],
            ["graph_enabled", "Graph"],
          ] as const).map(([key, label]) => (
            <label className="kb-check-row" key={key}>
              <input type="checkbox" checked={Boolean(settings.indexingStrategy[key])} onChange={(event) => onChange({ ...settings, indexingStrategy: { ...settings.indexingStrategy, [key]: event.target.checked } })} />
              <span>{label}</span>
            </label>
          ))}
        </div>
      </div>
    );
  }
  if (settings.activeSection === "chunking") {
    return (
      <div className="kb-wizard-section">
        <h3>分段设置</h3>
        <label>
          <span>切片策略</span>
          <select value={settings.chunking.strategy || "auto"} onChange={(event) => onChange({ ...settings, chunking: { ...settings.chunking, strategy: event.target.value as "auto" | "heading" | "heuristic" | "recursive" } })}><option value="auto">auto (heading → heuristic → recursive)</option><option value="heading">heading</option><option value="heuristic">heuristic</option><option value="recursive">recursive</option></select>
        </label>
        <label>
          <span>子块大小（字符）</span>
          <input type="number" min={64} max={2048} value={settings.chunking.child_chunk_size_chars} onChange={(event) => onChange({ ...settings, chunking: { ...settings.chunking, child_chunk_size_chars: Number(event.target.value) || 384 } })} />
        </label>
        <label>
          <span>父块大小（字符）</span>
          <input type="number" min={512} max={8192} value={settings.chunking.parent_chunk_size_chars} onChange={(event) => onChange({ ...settings, chunking: { ...settings.chunking, parent_chunk_size_chars: Number(event.target.value) || 4096 } })} />
        </label>
        <label>
          <span>子块重叠（字符）</span>
          <input type="number" min={0} max={1024} value={settings.chunking.child_chunk_overlap_chars} onChange={(event) => onChange({ ...settings, chunking: { ...settings.chunking, child_chunk_overlap_chars: Number(event.target.value) || 0 } })} />
        </label>
        <label className="kb-check-row">
          <input type="checkbox" checked={Boolean(settings.chunking.parent_child_enabled)} onChange={(event) => onChange({ ...settings, chunking: { ...settings.chunking, parent_child_enabled: event.target.checked } })} />
          <span>使用父子分段结构</span>
        </label>
      </div>
    );
  }
  if (settings.activeSection === "vector") {
    return (
      <div className="kb-wizard-section">
        <h3>检索与向量</h3>
        <label className="kb-check-row">
          <input type="checkbox" checked={settings.indexingStrategy.dense_enabled} onChange={(event) => onChange({ ...settings, indexingStrategy: { ...settings.indexingStrategy, dense_enabled: event.target.checked } })} />
          <span>Dense retrieval</span>
        </label>
        <label className="kb-check-row">
          <input type="checkbox" checked={settings.indexingStrategy.keyword_enabled} onChange={(event) => onChange({ ...settings, indexingStrategy: { ...settings.indexingStrategy, keyword_enabled: event.target.checked } })} />
          <span>Keyword retrieval</span>
        </label>
        <label>
          <span>Vector store</span>
          <input value="Default runtime vector store" readOnly />
        </label>
      </div>
    );
  }
  if (settings.activeSection === "graph") {
    return (
      <div className="kb-wizard-section">
        <h3>知识图谱</h3>
        <label className="kb-check-row">
          <input type="checkbox" checked={settings.indexingStrategy.graph_enabled} onChange={(event) => onChange({ ...settings, indexingStrategy: { ...settings.indexingStrategy, graph_enabled: event.target.checked } })} />
          <span>请求启用图谱抽取（仅当后端已配置时生效）</span>
        </label>
        <p className="kb-muted">若运行时未配置 KG provider，该请求会保留为 requested，但不会伪装成 effective。</p>
      </div>
    );
  }
  if (settings.activeSection === "parser") {
    return (
      <div className="kb-wizard-section">
        <h3>解析引擎</h3>
        <label>
          <span>Parser engine</span>
          <input value={settings.parser.engine} readOnly={settings.parser.readOnly} onChange={(event) => onChange({ ...settings, parser: { ...settings.parser, engine: event.target.value } })} />
        </label>
        <p className="kb-muted">当前仅暴露默认解析器。Docling / fallback parser 的真实选择由后端运行时决定。</p>
      </div>
    );
  }
  if (settings.activeSection === "image_ocr") {
    return (
      <UnavailableSection title="图片 / OCR" text="OCR 和多模态解析依赖后端运行时能力；当前创建请求不会把这些能力提交为已生效选项。" />
    );
  }
  if (settings.activeSection === "audio") {
    return <UnavailableSection title="音频处理" text="音频知识库尚未实现。" />;
  }
  if (settings.activeSection === "model") {
    return <UnavailableSection title="模型配置" text="创建阶段展示运行时 provider 状态；具体模型由后端环境变量控制。" />;
  }
  return <UnavailableSection title="高级设置" text="高级覆盖项会在后续能力具备后逐步开放。" />;
}

function UnavailableSection({ title, text }: { title: string; text: string }) {
  return (
    <div className="kb-wizard-section">
      <h3>{title}</h3>
      <div className="kb-unavailable">
        <strong>当前不可用</strong>
        <p>{text}</p>
      </div>
    </div>
  );
}

function KnowledgeBaseDetailShell({
  selected,
  documents,
  documentLoading,
  error,
  uploadStatus,
  bulkStatus,
  uploadMenuOpen,
  uploadDialogOpen,
  pendingUploadFiles,
  uploadSettings,
  activeBatch,
  processingPreview,
  filters,
  viewMode,
  selectedDocumentIds,
  uploading,
  retryingId,
  fileInputRef,
  folderInputRef,
  onBack,
  onStartChat,
  onOpenSettings,
  onFiltersChange,
  onViewModeChange,
  onRefresh,
  onSelectedDocumentIdsChange,
  onBulkDelete,
  onUploadMenuOpenChange,
  onUpload,
  onRemovePendingUploadFile,
  onCancelPendingUpload,
  onConfirmPendingUpload,
  onUploadSettingsChange,
  onOpenDocument,
  onOpenDocumentDetail,
  onDeleteDocument,
  onRetrySummary,
  onRetryProcessing,
  onOpenTrace,
  settingsDialog,
  viewer,
  traceDrawer,
}: {
  selected: KnowledgeBase;
  documents: DocumentItem[];
  documentLoading: boolean;
  error: string;
  uploadStatus: string;
  bulkStatus: string;
  uploadMenuOpen: boolean;
  uploadDialogOpen: boolean;
  pendingUploadFiles: PendingUploadFile[];
  uploadSettings: UploadBatchSettings;
  activeBatch: UploadBatch | null;
  processingPreview: { loading: boolean; error: string; file?: string; data?: DocumentProcessingPreview };
  filters: DocumentFilters;
  viewMode: DocumentViewMode;
  selectedDocumentIds: string[];
  uploading: boolean;
  retryingId: string;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  folderInputRef: React.RefObject<HTMLInputElement | null>;
  onBack: () => void;
  onStartChat: () => void;
  onOpenSettings: () => void;
  onFiltersChange: (filters: DocumentFilters) => void;
  onViewModeChange: (mode: DocumentViewMode) => void;
  onRefresh: () => void;
  onSelectedDocumentIdsChange: (ids: string[]) => void;
  onBulkDelete: () => void;
  onUploadMenuOpenChange: (open: boolean) => void;
  onUpload: (files: FileList | null) => void;
  onRemovePendingUploadFile: (id: string) => void;
  onCancelPendingUpload: () => void;
  onConfirmPendingUpload: () => void;
  onUploadSettingsChange: (settings: UploadBatchSettings) => void;
  onOpenDocument: (item: DocumentItem) => void;
  onOpenDocumentDetail: (item: DocumentItem) => void;
  onDeleteDocument: (item: DocumentItem) => void;
  onRetrySummary: (item: DocumentItem) => void;
  onRetryProcessing: (item: DocumentItem) => void;
  onOpenTrace: (item: DocumentItem) => void;
  settingsDialog: ReactNode;
  viewer: ReactNode;
  traceDrawer: ReactNode;
}) {
  const wikiCapable = selected.type === "wiki" || Boolean(selected.indexing_strategy?.wiki_enabled);
  const [activeTab, setActiveTab] = useState<"documents" | "wiki" | "graph">("documents");

  useEffect(() => {
    if (!wikiCapable && activeTab !== "documents") setActiveTab("documents");
  }, [wikiCapable, activeTab]);

  return (
    <section className="knowledge-page kb-detail-page">
      <header className="knowledge-header kb-detail-header">
        <div>
          <div className="kb-detail-breadcrumb" aria-label="当前位置">
            <button className="kb-back" type="button" onClick={onBack}>知识库</button>
            <span>›</span>
            <span>{selected.name}</span>
            <span>›</span>
            <strong>文档</strong>
          </div>
          <h1>{selected.name}</h1>
          <p>{selected.description || "上传文档后自动解析并构建可浏览、可追溯的知识空间。"}</p>
        </div>
        <div className="knowledge-actions">
          <button type="button" onClick={onStartChat}>开始聊天</button>
          <button type="button" onClick={onOpenSettings}>设置</button>
          <UploadActionMenu
            open={uploadMenuOpen}
            uploading={uploading}
            fileInputRef={fileInputRef}
            folderInputRef={folderInputRef}
            onOpenChange={onUploadMenuOpenChange}
            onUpload={onUpload}
          />
        </div>
      </header>
      {activeTab === "documents" ? <KnowledgeBaseMetrics selected={selected} /> : null}
      <div className="kb-detail-tabs" role="tablist" aria-label="Knowledge base workspace">
        <button type="button" className={activeTab === "documents" ? "active" : ""} onClick={() => setActiveTab("documents")}>Documents</button>
        {wikiCapable ? <button type="button" className={activeTab === "wiki" ? "active" : ""} onClick={() => setActiveTab("wiki")}>Wiki</button> : null}
        {wikiCapable ? <button type="button" className={activeTab === "graph" ? "active" : ""} onClick={() => setActiveTab("graph")}>Graph</button> : null}
      </div>
      <div hidden={activeTab !== "documents"}>
      <DocumentToolbar
        filters={filters}
        viewMode={viewMode}
        selectedCount={selectedDocumentIds.length}
        disabled={selected.status === "archived"}
        onFiltersChange={onFiltersChange}
        onViewModeChange={onViewModeChange}
        onRefresh={onRefresh}
        onBulkDelete={onBulkDelete}
      />
      {uploadStatus && !uploadDialogOpen ? <div className="notice">{uploadStatus}</div> : null}
      {bulkStatus ? <div className={bulkStatus.includes("失败") ? "notice error" : "notice"}>{bulkStatus}</div> : null}
      {error ? <div className="notice error">{error}</div> : null}
      {documentLoading ? <div className="notice">正在加载文档...</div> : null}
      {!documentLoading && !documents.length ? (
        <div className="kb-empty">
          <LibraryIcon />
          <h2>还没有文档</h2>
          <p>上传文件后即可在该知识库中检索。</p>
        </div>
      ) : null}
      {documents.length ? (
        <DocumentCollection
          documents={documents}
          viewMode={viewMode}
          selectedDocumentIds={selectedDocumentIds}
          retryingId={retryingId}
          onSelectedDocumentIdsChange={onSelectedDocumentIdsChange}
          onOpenDocument={onOpenDocument}
          onOpenDocumentDetail={onOpenDocumentDetail}
          onDeleteDocument={onDeleteDocument}
          onRetrySummary={onRetrySummary}
          onRetryProcessing={onRetryProcessing}
          onOpenTrace={onOpenTrace}
        />
      ) : null}
      {uploadDialogOpen ? (
        <PendingUploadDialog
          files={pendingUploadFiles}
          settings={uploadSettings}
          batch={activeBatch}
          uploading={uploading}
          uploadStatus={uploadStatus}
          onSettingsChange={onUploadSettingsChange}
          onRemove={onRemovePendingUploadFile}
          onCancel={onCancelPendingUpload}
          onConfirm={onConfirmPendingUpload}
        />
      ) : null}
      <ProcessingPreviewPanel preview={processingPreview} />
      </div>
      {activeTab !== "documents" && wikiCapable ? (
        <WikiWorkspace selected={selected} documents={documents} onOpenDocument={onOpenDocument} workspaceMode={activeTab === "graph" ? "graph" : "reader"} />
      ) : null}
      {settingsDialog}
      {viewer}
      {traceDrawer}
    </section>
  );
}

function LegacyWikiWorkspace({
  selected,
  documents,
  onOpenDocument,
}: {
  selected: KnowledgeBase;
  documents: DocumentItem[];
  onOpenDocument: (item: DocumentItem) => void;
}) {
  const [filters, setFilters] = useState({ q: "", status: "", page_type: "", folder_id: "" });
  const [pages, setPages] = useState<WikiPage[]>([]);
  const [selectedSlug, setSelectedSlug] = useState("");
  const [page, setPage] = useState<WikiPage | null>(null);
  const [issues, setIssues] = useState<WikiIssue[]>([]);
  const [proposals, setProposals] = useState<WikiProposal[]>([]);
  const [graph, setGraph] = useState<WikiGraph | null>(null);
  const [tasks, setTasks] = useState<WikiGenerationTask[]>([]);
  const [sourcePanel, setSourcePanel] = useState<{ title: string; chunks: Array<Record<string, unknown>>; error: string }>({ title: "", chunks: [], error: "" });
  const [loading, setLoading] = useState(false);
  const [actionStatus, setActionStatus] = useState("");
  const openIssueCountBySlug = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const issue of issues) counts[issue.slug] = (counts[issue.slug] || 0) + 1;
    return counts;
  }, [issues]);

  async function loadWiki(nextSlug = selectedSlug) {
    setLoading(true);
    setActionStatus("");
    try {
      const [pageResult, issueResult, proposalResult, graphResult, taskResult] = await Promise.all([
        listWikiPages(selected.id, { ...filters, limit: 80 }),
        listWikiIssues(selected.id, { status: "open", limit: 50 }),
        listWikiProposals(selected.id, { status: "pending", limit: 50 }),
        getWikiGraph(selected.id, { center_slug: nextSlug || undefined, limit: 80 }),
        listWikiGenerationTasks(selected.id, { limit: 20 }),
      ]);
      setPages(pageResult.items);
      setIssues(issueResult);
      setProposals(proposalResult);
      setGraph(graphResult);
      setTasks(taskResult);
      const slug = nextSlug || pageResult.items[0]?.slug || "";
      setSelectedSlug(slug);
      if (slug) {
        setPage(await getWikiPage(selected.id, slug));
      } else {
        setPage(null);
      }
    } catch (cause) {
      setActionStatus(cause instanceof Error ? cause.message : "Wiki workspace failed to load.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadWiki("");
  }, [selected.id, filters.q, filters.status, filters.page_type, filters.folder_id]);

  async function selectPage(slug: string) {
    setSelectedSlug(slug);
    try {
      setPage(await getWikiPage(selected.id, slug));
      setGraph(await getWikiGraph(selected.id, { center_slug: slug, limit: 80 }));
    } catch (cause) {
      setActionStatus(cause instanceof Error ? cause.message : "Wiki page failed to load.");
    }
  }

  async function openSource(ref: { doc_id?: string; chunk_id?: string; title?: string }) {
    const doc = documents.find((item) => item.id === ref.doc_id);
    if (doc) onOpenDocument(doc);
    try {
      const data = await readWikiSourceDoc(selected.id, {
        doc_id: ref.doc_id || "",
        chunk_ids: ref.chunk_id ? [ref.chunk_id] : [],
        limit: 8,
      });
      setSourcePanel({
        title: ref.title || doc?.name || ref.doc_id || "Source evidence",
        chunks: Array.isArray(data.chunks) ? data.chunks as Array<Record<string, unknown>> : [],
        error: "",
      });
    } catch (cause) {
      setSourcePanel({ title: ref.title || ref.doc_id || "Source evidence", chunks: [], error: cause instanceof Error ? cause.message : "Source evidence failed to load." });
    }
  }

  async function generateFromDocument(docId: string) {
    setActionStatus("Generating Wiki draft...");
    const result = await generateWikiPage(selected.id, { doc_id: docId });
    setActionStatus(result.page ? `Draft generated: ${result.page.title}` : result.proposal ? `Proposal created: ${result.proposal.title || result.proposal.slug}` : result.task.status);
    await loadWiki(result.page?.slug || result.proposal?.slug || selectedSlug);
  }

  async function updateIssueStatus(issueId: string, status: string) {
    await updateWikiIssue(selected.id, issueId, status);
    await loadWiki(selectedSlug);
  }

  async function applyProposal(proposalId: string) {
    const result = await applyWikiProposal(selected.id, proposalId);
    await loadWiki(result.page?.slug || selectedSlug);
  }

  async function rejectProposal(proposalId: string) {
    await rejectWikiProposal(selected.id, proposalId);
    await loadWiki(selectedSlug);
  }

  const markdownComponents: Components = {
    a({ href, children }) {
      const value = String(href || "");
      if (value.startsWith("wiki:")) {
        const slug = decodeURIComponent(value.slice(5));
        return <button type="button" className="wiki-inline-link" onClick={() => void selectPage(slug)}>{children}</button>;
      }
      return <a href={href}>{children}</a>;
    },
  };

  return (
    <section className="wiki-workspace">
      <div className="wiki-toolbar">
        <input value={filters.q} placeholder="Search Wiki" onChange={(event) => setFilters({ ...filters, q: event.target.value })} />
        <select value={filters.status} onChange={(event) => setFilters({ ...filters, status: event.target.value })}>
          <option value="">All status</option>
          <option value="draft">Draft</option>
          <option value="published">Published</option>
          <option value="stale">Stale</option>
        </select>
        <select value={filters.page_type} onChange={(event) => setFilters({ ...filters, page_type: event.target.value })}>
          <option value="">All types</option>
          <option value="summary">Summary</option>
          <option value="entity">Entity</option>
          <option value="concept">Concept</option>
          <option value="synthesis">Synthesis</option>
          <option value="manual">Manual</option>
        </select>
        <button type="button" onClick={() => void loadWiki(selectedSlug)}>Refresh</button>
      </div>
      {actionStatus ? <div className={actionStatus.includes("failed") || actionStatus.includes("Error") ? "notice error" : "notice"}>{actionStatus}</div> : null}
      <div className="wiki-layout">
        <aside className="wiki-page-list">
          <div className="wiki-panel-title">
            <strong>Pages</strong>
            {loading ? <span>Loading</span> : <span>{pages.length}</span>}
          </div>
          {!pages.length ? (
            <div className="wiki-empty">
              <p>No Wiki pages yet.</p>
              {documents[0] ? <button type="button" onClick={() => void generateFromDocument(documents[0].id)}>Generate draft</button> : null}
            </div>
          ) : pages.map((item) => (
            <button key={item.id} type="button" className={`wiki-page-row ${item.slug === selectedSlug ? "active" : ""}`} onClick={() => void selectPage(item.slug)}>
              <span>{item.title}</span>
              <small>{item.page_type} · {item.status} · v{item.version}</small>
              {item.summary ? <em>{item.summary}</em> : null}
              {openIssueCountBySlug[item.slug] ? <b>{openIssueCountBySlug[item.slug]} issue</b> : null}
            </button>
          ))}
          {documents.length ? (
            <div className="wiki-generation-box">
              <strong>Draft from source</strong>
              {documents.slice(0, 5).map((doc) => (
                <button key={doc.id} type="button" onClick={() => void generateFromDocument(doc.id)}>{doc.name}</button>
              ))}
              {tasks.slice(0, 3).map((task) => <small key={task.id}>{task.status}: {task.page_slug || task.doc_id}</small>)}
            </div>
          ) : null}
        </aside>
        <main className="wiki-reader">
          {page ? (
            <>
              <div className="wiki-reader-head">
                <div>
                  <h2>{page.title}</h2>
                  <p>{page.summary}</p>
                </div>
                <div className="wiki-badges">
                  <span>{page.status}</span>
                  <span>{page.page_type}</span>
                  <span>v{page.version}</span>
                </div>
              </div>
              <div className="wiki-markdown">
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{wikiLinkMarkdown(page.content_markdown)}</ReactMarkdown>
              </div>
              <div className="wiki-evidence-grid">
                <section>
                  <h3>Source refs</h3>
                  {(page.source_refs || []).length ? page.source_refs.map((ref, index) => (
                    <button key={`${ref.doc_id}-${ref.chunk_id || index}`} type="button" onClick={() => void openSource(ref)}>
                      {ref.title || ref.doc_id}{ref.chunk_id ? ` · ${ref.chunk_id.slice(0, 8)}` : ""}
                    </button>
                  )) : <p>No source refs.</p>}
                </section>
                <section>
                  <h3>Links</h3>
                  <div className="wiki-link-cloud">
                    {[...page.out_links.map((slug) => ({ slug, type: "out" })), ...page.in_links.map((slug) => ({ slug, type: "in" }))].map((link) => (
                      <button key={`${link.type}-${link.slug}`} type="button" onClick={() => void selectPage(link.slug)}>{link.type}: {link.slug}</button>
                    ))}
                  </div>
                </section>
              </div>
            </>
          ) : (
            <div className="wiki-empty"><p>Select or generate a Wiki page.</p></div>
          )}
        </main>
        <aside className="wiki-review">
          <WikiGraphPanel graph={graph} onSelect={(slug) => void selectPage(slug)} />
          <WikiIssuePanel issues={issues} onUpdate={updateIssueStatus} />
          <WikiProposalPanel proposals={proposals} onApply={applyProposal} onReject={rejectProposal} />
          <WikiSourcePanel panel={sourcePanel} />
        </aside>
      </div>
    </section>
  );
}

function WikiGraphPanel({ graph, onSelect }: { graph: WikiGraph | null; onSelect: (slug: string) => void }) {
  const nodes = (graph?.nodes || []) as Array<{ slug?: string; title?: string; page_type?: string; status?: string }>;
  const edges = graph?.edges || [];
  return (
    <section className="wiki-review-panel">
      <div className="wiki-panel-title"><strong>Graph</strong><span>{nodes.length} / {edges.length}</span></div>
      <div className="wiki-graph-list">
        {nodes.slice(0, 16).map((node) => (
          <button key={node.slug || node.title} type="button" onClick={() => node.slug && onSelect(node.slug)}>
            <span>{node.title || node.slug}</span>
            <small>{node.page_type} · {node.status}</small>
          </button>
        ))}
      </div>
    </section>
  );
}

function WikiIssuePanel({ issues, onUpdate }: { issues: WikiIssue[]; onUpdate: (id: string, status: string) => Promise<void> }) {
  return (
    <section className="wiki-review-panel">
      <div className="wiki-panel-title"><strong>Issues</strong><span>{issues.length}</span></div>
      {issues.slice(0, 8).map((issue) => (
        <article key={issue.id}>
          <b>{issue.slug}</b>
          <small>{issue.issue_type} · {issue.reported_by}</small>
          <p>{issue.description}</p>
          <div className="wiki-review-actions">
            <button type="button" onClick={() => void onUpdate(issue.id, "resolved")}>Resolve</button>
            <button type="button" onClick={() => void onUpdate(issue.id, "wontfix")}>Ignore</button>
          </div>
        </article>
      ))}
    </section>
  );
}

function WikiProposalPanel({ proposals, onApply, onReject }: { proposals: WikiProposal[]; onApply: (id: string) => Promise<void>; onReject: (id: string) => Promise<void> }) {
  return (
    <section className="wiki-review-panel">
      <div className="wiki-panel-title"><strong>Proposals</strong><span>{proposals.length}</span></div>
      {proposals.slice(0, 8).map((proposal) => (
        <article key={proposal.id}>
          <b>{proposal.title || proposal.slug}</b>
          <small>{proposal.action} · {proposal.created_by}</small>
          <p>{proposal.reason || JSON.stringify(proposal.payload).slice(0, 180)}</p>
          <div className="wiki-review-actions">
            <button type="button" onClick={() => void onApply(proposal.id)}>Apply</button>
            <button type="button" onClick={() => void onReject(proposal.id)}>Reject</button>
          </div>
        </article>
      ))}
    </section>
  );
}

function WikiSourcePanel({ panel }: { panel: { title: string; chunks: Array<Record<string, unknown>>; error: string } }) {
  if (!panel.title && !panel.error) return null;
  return (
    <section className="wiki-review-panel wiki-source-panel">
      <div className="wiki-panel-title"><strong>Source</strong><span>{panel.chunks.length}</span></div>
      {panel.error ? <p className="metric-error">{panel.error}</p> : null}
      {panel.title ? <b>{panel.title}</b> : null}
      {panel.chunks.map((chunk, index) => (
        <article key={String(chunk.chunk_id || index)}>
          <small>{String(chunk.chunk_type || "")} · {String(chunk.chunk_id || "").slice(0, 12)}</small>
          <p>{String(chunk.content || "").slice(0, 900)}</p>
        </article>
      ))}
    </section>
  );
}

function wikiLinkMarkdown(markdown: string): string {
  return (markdown || "").replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (_match, slug: string, label?: string) => {
    const cleanSlug = encodeURIComponent(String(slug || "").trim());
    return `[${label || slug}](wiki:${cleanSlug})`;
  });
}

function KnowledgeBaseMetrics({ selected }: { selected: KnowledgeBase }) {
  return (
    <div className="kb-metrics" aria-label="知识库状态">
      <span><b>{selected.aggregate.document_count}</b> 文档</span>
      <span><b>{selected.aggregate.indexed_chunk_count}</b> 分块</span>
      <span><b>{selected.aggregate.processing_count}</b> 处理中</span>
      <span className={selected.aggregate.failed_count ? "metric-error" : ""}><b>{selected.aggregate.failed_count}</b> 失败</span>
      {selected.aggregate.reset_required ? <span className="metric-warning">存储需要清空重建</span> : null}
      {selected.provider_config?.inactive_overrides?.length ? <span className="metric-warning">部分 provider 覆盖未生效</span> : null}
    </div>
  );
}

function ProviderStatusPanel({ selected }: { selected: KnowledgeBase }) {
  const effective = selected.provider_config?.effective || {};
  const inactive = selected.provider_config?.inactive_overrides || [];
  return (
    <div className="kb-provider-panel" aria-label="有效 Provider 配置">
      <span><b>Parser</b> {effective.parser || "default"}</span>
      <span><b>Embedding</b> {effective.embedding || "default"}</span>
      <span><b>Vector store</b> {effective.vector_store || "default"}</span>
      <span><b>Enrichment</b> {effective.enrichment || "default"}</span>
      {inactive.length ? <span className="metric-warning">未生效覆盖：{inactive.join(", ")}</span> : <span>所有支持项已按运行时生效</span>}
    </div>
  );
}

function UploadActionMenu({
  open,
  uploading,
  fileInputRef,
  folderInputRef,
  onOpenChange,
  onUpload,
}: {
  open: boolean;
  uploading: boolean;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  folderInputRef: React.RefObject<HTMLInputElement | null>;
  onOpenChange: (open: boolean) => void;
  onUpload: (files: FileList | null) => void;
}) {
  return (
    <div className="upload-menu-wrap">
      <button className="upload-button" type="button" onClick={() => onOpenChange(!open)}>
        <UploadIcon /><span>{uploading ? "处理中" : "上传"}</span>
      </button>
      {open ? (
        <div className="upload-action-menu">
          <label>
            <input ref={fileInputRef} type="file" multiple accept={ACCEPTED_INPUT} onChange={(event) => onUpload(event.target.files)} />
            <span>上传文档</span>
            <small>进入待确认队列</small>
          </label>
          <label>
            <input ref={folderInputRef} type="file" multiple accept={ACCEPTED_INPUT} onChange={(event) => onUpload(event.target.files)} {...({ webkitdirectory: "", directory: "" } as Record<string, string>)} />
            <span>上传文件夹</span>
            <small>保留相对路径</small>
          </label>
        </div>
      ) : null}
    </div>
  );
}

function PendingUploadDialog({
  files,
  settings,
  batch,
  uploading,
  uploadStatus,
  onSettingsChange,
  onRemove,
  onCancel,
  onConfirm,
}: {
  files: PendingUploadFile[];
  settings: UploadBatchSettings;
  batch: UploadBatch | null;
  uploading: boolean;
  uploadStatus: string;
  onSettingsChange: (settings: UploadBatchSettings) => void;
  onRemove: (id: string) => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const terminal = batch ? ["completed", "partial_failed", "failed", "canceled"].includes(batch.status) : false;
  return (
    <div className="dialog-mask" role="presentation" onClick={uploading ? undefined : onCancel}>
      <section className="kb-dialog pending-upload-dialog" role="dialog" aria-modal="true" aria-label="确认上传" onClick={(event) => event.stopPropagation()}>
        <header>
          <div>
            <h2>确认上传与处理</h2>
            <p>已选择 {files.length} 个文件。确认前不会解析、索引、嵌入或调用外部 provider。</p>
          </div>
          <button type="button" onClick={onCancel} aria-label="关闭" disabled={uploading}>×</button>
        </header>
        {uploadStatus ? (
          <div className="pending-upload-progress">
            <span className={uploading ? "upload-task-dot running" : "upload-task-dot neutral"} aria-hidden="true" />
            <span>{uploadStatus}</span>
          </div>
        ) : null}
        <div className="pending-upload-layout">
          <div>
            <h3>待上传文件</h3>
            <div className="pending-file-list">
              {files.map((item) => (
                <div className="pending-file-row" key={item.id}>
                  <div>
                    <strong>{item.file.name}</strong>
                    <span>{item.relativePath}</span>
                    <small>{formatBytes(item.size)}</small>
                  </div>
                  <button type="button" disabled={uploading} onClick={() => onRemove(item.id)}>移除</button>
                </div>
              ))}
              {!files.length ? <p className="kb-muted">没有待上传文件。</p> : null}
            </div>
          </div>
          <div className="upload-config-panel">
            <h3>处理配置</h3>
            <label><span>解析引擎</span><input value={settings.parser_engine || "builtin"} readOnly /></label>
            <label><span>切片策略</span><select value={settings.chunk_strategy || "auto"} onChange={(event) => onSettingsChange({ ...settings, chunk_strategy: event.target.value as "auto" | "heading" | "heuristic" | "recursive" })}><option value="auto">auto (heading → heuristic → recursive)</option><option value="heading">heading</option><option value="heuristic">heuristic</option><option value="recursive">recursive</option></select></label>
            <label><span>父块大小（字符）</span><input type="number" value={settings.parent_chunk_size_chars || 4096} onChange={(event) => onSettingsChange({ ...settings, parent_chunk_size_chars: Number(event.target.value) || 4096 })} /></label>
            <label><span>子块大小（字符）</span><input type="number" value={settings.child_chunk_size_chars || 384} onChange={(event) => onSettingsChange({ ...settings, child_chunk_size_chars: Number(event.target.value) || 384 })} /></label>
            <label><span>子块重叠（字符）</span><input type="number" value={settings.child_chunk_overlap_chars || 76} onChange={(event) => onSettingsChange({ ...settings, child_chunk_overlap_chars: Number(event.target.value) || 0 })} /></label>
            <label className="kb-check-row"><input type="checkbox" checked={Boolean(settings.pdf_force_scanned)} onChange={(event) => onSettingsChange({ ...settings, pdf_force_scanned: event.target.checked })} /><span>PDF 强制扫描模式</span></label>
            <label className="kb-check-row"><input type="checkbox" checked={Boolean(settings.dense_enabled)} onChange={(event) => onSettingsChange({ ...settings, dense_enabled: event.target.checked })} /><span>Dense 检索</span></label>
            <label className="kb-check-row"><input type="checkbox" checked={Boolean(settings.keyword_enabled)} onChange={(event) => onSettingsChange({ ...settings, keyword_enabled: event.target.checked })} /><span>Keyword 检索</span></label>
            <label className="kb-check-row"><input type="checkbox" checked={Boolean(settings.graph_enabled)} onChange={(event) => onSettingsChange({ ...settings, graph_enabled: event.target.checked })} /><span>请求图谱抽取（按运行时生效）</span></label>
            <div className="kb-unavailable"><strong>暂不可用</strong><p>问题生成、OCR、多模态和音频处理会显示为不可用，不会作为 effective 设置提交。</p></div>
          </div>
        </div>
        <div className="kb-effective-config">
          <strong>Provider safety boundary</strong>
          <span>只有点击“确认上传并处理”后，后端才会开始解析、索引和可能的 provider 调用。</span>
        </div>
        <div className="kb-dialog-actions">
          <span />
          <button type="button" disabled={uploading} onClick={onCancel}>取消</button>
          <button type="button" className="primary-action" disabled={uploading || !files.length || terminal} onClick={onConfirm}>
            {uploading ? "上传中..." : "确认上传并处理"}
          </button>
        </div>
      </section>
    </div>
  );
}

function UploadBatchMonitor({
  batch,
  uploading,
  onRefresh,
  onRetryFile,
  onPreviewFile,
  onOpenTrace,
}: {
  batch: UploadBatch;
  uploading: boolean;
  onRefresh: () => void;
  onRetryFile: (fileId: string) => void;
  onPreviewFile: (file: UploadFileTaskRecord) => void;
  onOpenTrace: (file: UploadFileTaskRecord) => void;
}) {
  return (
    <div className="upload-batch-monitor">
      <div className="upload-batch-head">
        <strong>处理队列：{uploadBatchStatusLabel(batch.status)}</strong>
        <span>已完成 {batch.aggregate.completed}/{batch.aggregate.total}，失败 {batch.aggregate.failed}，取消 {batch.aggregate.canceled}</span>
        <button type="button" disabled={uploading} onClick={onRefresh}>刷新</button>
      </div>
      <div className="upload-task-list as-cards">
        {batch.files.map((rawFile) => {
          const file = canRetryUploadFile(rawFile)
            ? { ...rawFile, status: "failed" as const }
            : { ...rawFile, status: rawFile.status === "failed" ? "completed" as const : rawFile.status };
          const summary = summarizeUploadFile(file);
          return (
          <article className={`upload-task-card ${uploadTaskTone(file.status)}`} key={file.id}>
            <div className="upload-task-card-main">
              <span className={`upload-task-dot ${uploadTaskTone(file.status)}`} aria-hidden="true" />
              <div>
                <strong>{file.relative_path || file.original_name}</strong>
                <p>{uploadFileStatusLabel(file.status)} · {summary.phaseText || `${file.chunks || 0} 个分块`}</p>
              </div>
            </div>
            {summary.hasPartialMultimodalFailure ? <span className="metric-warning">多模态部分失败</span> : null}
            {file.error_message ? <span className="metric-error">{file.error_message}</span> : null}
            <div className="upload-task-card-actions">
              {file.document_id ? <button type="button" onClick={() => onOpenTrace(file)}>处理链路</button> : null}
              {file.storage_path ? <button type="button" disabled={uploading} onClick={() => onPreviewFile(file)}>预览</button> : null}
              {file.status === "failed" ? <button type="button" disabled={uploading} onClick={() => onRetryFile(file.id)}>重试</button> : null}
            </div>
          </article>
          );
        })}
      </div>
    </div>
  );
}

function uploadBatchStatusLabel(status: string) {
  const labels: Record<string, string> = {
    draft: "待上传",
    uploading: "上传中",
    ready_to_process: "等待处理",
    processing: "处理中",
    completed: "已完成",
    partial_failed: "部分失败",
    failed: "失败",
    canceled: "已取消",
  };
  return labels[status] || status;
}

function uploadFileStatusLabel(status: string) {
  const labels: Record<string, string> = {
    pending: "等待上传",
    uploaded: "等待处理",
    parsing: "文档解析中",
    indexed: "写入索引中",
    enrichment_pending: "等待后处理",
    completed: "已完成",
    failed: "处理失败",
    canceled: "已取消",
  };
  return labels[status] || status;
}

function uploadTaskTone(status: string) {
  if (status === "failed") return "failed";
  if (status === "completed" || status === "indexed") return "done";
  if (["parsing", "processing", "uploaded", "enrichment_pending", "pending"].includes(status)) return "running";
  return "neutral";
}

function ProcessingPreviewPanel({
  preview,
}: {
  preview: { loading: boolean; error: string; file?: string; data?: DocumentProcessingPreview };
}) {
  if (!preview.loading && !preview.error && !preview.data) return null;
  const summary = preview.data ? summarizeProcessingPreview(preview.data) : null;
  return (
    <div className="upload-batch-monitor">
      <div className="upload-batch-head">
        <strong>Processing preview{preview.file ? `: ${preview.file}` : ""}</strong>
        {preview.loading ? <span>loading</span> : null}
        {preview.error ? <span className="metric-error">{preview.error}</span> : null}
      </div>
      {summary && preview.data ? (
        <div className="preview-diagnostics">
          <span>Parser {summary.parserDecision}</span>
          <span>Pages {summary.pageCounts.total} / native {summary.pageCounts.native} / scanned {summary.pageCounts.scanned}</span>
          <span>Tier {summary.selectedTier || "n/a"}</span>
          <span>Chunks {summary.chunkCount}</span>
          {summary.fallbackReason ? <span className="metric-warning">{summary.fallbackReason}</span> : null}
          {summary.warnings.slice(0, 3).map((warning) => <span className="metric-warning" key={warning}>{warning}</span>)}
          <div className="preview-chunk-list">
            {preview.data.chunk_previews.slice(0, 6).map((chunk, index) => (
              <article className="preview-chunk-card" key={chunk.id || index}>
                <strong>{chunk.type || "chunk"} · {chunk.characters || 0} chars</strong>
                <small>{chunk.title_path || ""} {chunk.page_start ? `p.${chunk.page_start}` : ""}</small>
                <p>{chunk.preview || ""}</p>
              </article>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function DocumentToolbar({
  filters,
  viewMode,
  selectedCount,
  disabled,
  onFiltersChange,
  onViewModeChange,
  onRefresh,
  onBulkDelete,
}: {
  filters: DocumentFilters;
  viewMode: DocumentViewMode;
  selectedCount: number;
  disabled: boolean;
  onFiltersChange: (filters: DocumentFilters) => void;
  onViewModeChange: (mode: DocumentViewMode) => void;
  onRefresh: () => void;
  onBulkDelete: () => void;
}) {
  const update = (patch: Partial<DocumentFilters>) => onFiltersChange({ ...filters, ...patch });
  return (
    <div className="document-toolbar" aria-label="文档筛选工具栏">
      <div className="document-toolbar-main">
        <input value={filters.q} placeholder="搜索名称、路径、概要或关键词" onChange={(event) => update({ q: event.target.value })} />
        <input value={filters.tag} placeholder="标签 / 关键词" onChange={(event) => update({ tag: event.target.value })} />
        <select value={filters.file_type} onChange={(event) => update({ file_type: event.target.value })}>
          <option value="">全部类型</option>
          {["pdf", "docx", "doc", "md", "txt", "html", "csv", "json", "xlsx", "xls"].map((type) => (
            <option key={type} value={type}>{type.toUpperCase()}</option>
          ))}
        </select>
        <select value={filters.status} onChange={(event) => update({ status: event.target.value })}>
          <option value="">全部状态</option>
          <option value="parsed">可检索</option>
          <option value="parsing">解析中</option>
          <option value="failed">失败</option>
          <option value="completed">概要完成</option>
          <option value="processing">概要处理中</option>
        </select>
        <input value={filters.source} placeholder="来源路径" onChange={(event) => update({ source: event.target.value })} />
        <input type="date" value={filters.created_from} onChange={(event) => update({ created_from: event.target.value })} />
        <input type="date" value={filters.created_to} onChange={(event) => update({ created_to: event.target.value })} />
      </div>
      <div className="document-toolbar-actions">
        <button type="button" onClick={() => onFiltersChange(DEFAULT_DOCUMENT_FILTERS)}>清除</button>
        <button type="button" onClick={onRefresh}>刷新</button>
        <button type="button" className={viewMode === "grid" ? "active" : ""} onClick={() => onViewModeChange("grid")}>卡片</button>
        <button type="button" className={viewMode === "list" ? "active" : ""} onClick={() => onViewModeChange("list")}>列表</button>
        <button type="button" disabled={!selectedCount || disabled} onClick={onBulkDelete}>删除选中 {selectedCount || ""}</button>
        {disabled ? <span className="metric-warning">归档知识库只读</span> : null}
      </div>
    </div>
  );
}

function DocumentCollection({
  documents,
  viewMode,
  selectedDocumentIds,
  retryingId,
  onSelectedDocumentIdsChange,
  onOpenDocument,
  onOpenDocumentDetail,
  onDeleteDocument,
  onRetrySummary,
  onRetryProcessing,
  onOpenTrace,
}: {
  documents: DocumentItem[];
  viewMode: DocumentViewMode;
  selectedDocumentIds: string[];
  retryingId: string;
  onSelectedDocumentIdsChange: (ids: string[]) => void;
  onOpenDocument: (item: DocumentItem) => void;
  onOpenDocumentDetail: (item: DocumentItem) => void;
  onDeleteDocument: (item: DocumentItem) => void;
  onRetrySummary: (item: DocumentItem) => void;
  onRetryProcessing: (item: DocumentItem) => void;
  onOpenTrace: (item: DocumentItem) => void;
}) {
  const toggle = (id: string, checked: boolean) => {
    onSelectedDocumentIdsChange(checked ? [...selectedDocumentIds, id] : selectedDocumentIds.filter((item) => item !== id));
  };
  const visibleDocumentIds = documents.map((item) => item.id);
  const selectedVisibleCount = visibleDocumentIds.filter((id) => selectedDocumentIds.includes(id)).length;
  const allVisibleSelected = visibleDocumentIds.length > 0 && selectedVisibleCount === visibleDocumentIds.length;
  const selectAllRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = selectedVisibleCount > 0 && !allVisibleSelected;
    }
  }, [allVisibleSelected, selectedVisibleCount]);
  const toggleVisibleDocuments = (checked: boolean) => {
    if (checked) {
      onSelectedDocumentIdsChange(Array.from(new Set([...selectedDocumentIds, ...visibleDocumentIds])));
      return;
    }
    onSelectedDocumentIdsChange(selectedDocumentIds.filter((id) => !visibleDocumentIds.includes(id)));
  };
  if (viewMode === "grid") {
    return (
      <div className="kb-document-grid">
        {documents.map((item) => (
          <DocumentTileCard
            key={item.id}
            item={item}
            selected={selectedDocumentIds.includes(item.id)}
            retryingId={retryingId}
            onSelected={(checked) => toggle(item.id, checked)}
            onOpenDocument={onOpenDocument}
            onOpenDocumentDetail={onOpenDocumentDetail}
            onDeleteDocument={onDeleteDocument}
            onRetrySummary={onRetrySummary}
            onRetryProcessing={onRetryProcessing}
            onOpenTrace={onOpenTrace}
          />
        ))}
      </div>
    );
  }
  return (
    <div className="kb-document-list" role="table" aria-label="文档列表">
      <div className="kb-document-table-head" role="row">
        <label className="doc-select-all">
          <input
            ref={selectAllRef}
            type="checkbox"
            checked={allVisibleSelected}
            onChange={(event) => toggleVisibleDocuments(event.target.checked)}
          />
          <span className="sr-only">选择全部文档</span>
        </label>
        <span>名称</span>
        <span>状态</span>
        <span>分块</span>
        <span>类型</span>
        <span>来源</span>
        <span>更新时间</span>
        <span>操作</span>
      </div>
      {documents.map((item) => {
        const runtimeStatus = documentRuntimeStatus(item);
        return (
        <article className="kb-document-row compact" key={item.id} role="row">
          <label className="doc-select"><input type="checkbox" disabled={isUploadPlaceholderDocument(item)} checked={selectedDocumentIds.includes(item.id)} onChange={(event) => toggle(item.id, event.target.checked)} /><span>选中</span></label>
          <div className="kb-document-main">
            <div className="kb-document-title-line">
              <h2>{item.name}</h2>
              <TraceStatusButton item={item} onOpenTrace={onOpenTrace} />
              <TraceSummaryStatus item={item} onOpenTrace={onOpenTrace} />
              <span className={`status-pill ${item.parse_status}`}>{item.parse_status === "parsed" ? "可检索" : item.parse_status}</span>
              <SummaryStatus status={item.summary_status || "none"} />
            </div>
            <p className="doc-path">{item.source || item.storage_path}</p>
            {item.summary ? (
              <button type="button" className="kb-document-summary as-detail" onClick={() => onOpenDocumentDetail(item)}>
                {item.summary}
              </button>
            ) : null}
            {item.keywords_json?.length ? (
              <div className="kb-keywords">{item.keywords_json.map((keyword) => <span key={keyword}>{keyword}</span>)}</div>
            ) : null}
            {item.summary_status === "failed" ? <p className="summary-error">概要生成失败，文档仍可正常检索。</p> : null}
          </div>
          <button
            type="button"
            className={`doc-runtime-badge inline ${runtimeStatus.tone}`}
            title={runtimeStatus.title}
            disabled={isUploadPlaceholderDocument(item)}
            onClick={() => onOpenTrace(item)}
          >
            {runtimeStatus.tone === "running" ? <span className="runtime-spinner" aria-hidden="true" /> : null}
            {runtimeStatus.label}
          </button>
          <span>{item.chunks}</span>
          <span title={item.file_type || "-"}>{item.file_type || "-"}</span>
          <span className="doc-path" title={item.source || item.storage_path}>{item.source || item.storage_path}</span>
          <span>{formatDate(item.updated_at)}</span>
          <div className="row-actions">
            {!isUploadPlaceholderDocument(item) && item.summary_status === "failed" ? (
              <button type="button" onClick={() => onRetrySummary(item)} disabled={retryingId === item.id}>
                {retryingId === item.id ? "重试中" : "重试概要"}
              </button>
            ) : null}
            {!isUploadPlaceholderDocument(item) && item.processing_retry_available ? (
              <button type="button" onClick={() => onRetryProcessing(item)} disabled={retryingId === item.id}>
                {retryingId === item.id ? "重试中" : "重试处理"}
              </button>
            ) : null}
            {isUploadPlaceholderDocument(item) ? <span className="kb-muted">等待入库</span> : null}
            {!isUploadPlaceholderDocument(item) ? <button type="button" onClick={() => onOpenDocumentDetail(item)}>详情</button> : null}
            {!isUploadPlaceholderDocument(item) ? <button type="button" onClick={() => onOpenDocument(item)}>预览</button> : null}
            {!isUploadPlaceholderDocument(item) ? <button type="button" className="danger-action" onClick={() => onDeleteDocument(item)}>删除</button> : null}
          </div>
        </article>
        );
      })}
    </div>
  );
}

function getDocumentTileSummary(item: DocumentItem): string {
  const summary = item.summary?.trim();
  if (summary) return summary;
  const status = (item.summary_status || "none").toLowerCase();
  if (status === "pending" || status === "processing") {
    return "摘要生成中，完成后会自动显示在卡片中。";
  }
  if (status === "failed") {
    return "摘要生成失败，文档仍可预览和检索。";
  }
  return "未生成摘要。";
}

function isUploadPlaceholderDocument(item: DocumentItem) {
  return Boolean(item.metadata_json?.optimistic_upload);
}

function buildUploadPlaceholderDocuments(batch: UploadBatch, knowledgeBase: KnowledgeBase): DocumentItem[] {
  const now = new Date().toISOString();
  return batch.files.map((file) => {
    const name = file.relative_path || file.original_name;
    const status = (file.status || "uploaded").toLowerCase();
    const failed = status === "failed";
    const done = status === "completed" || status === "indexed";
    const waiting = status === "uploaded" || status === "pending";
    return {
      id: file.document_id || `upload-placeholder:${batch.id}:${file.id}`,
      workspace_id: file.workspace_id || knowledgeBase.workspace_id,
      knowledge_base_id: file.knowledge_base_id || knowledgeBase.id,
      name,
      file_type: fileTypeFromPath(name),
      storage_path: file.storage_path || name,
      source: file.storage_path || name,
      parse_status: failed ? "failed" : done ? "parsed" : waiting ? "pending" : "parsing",
      created_at: file.created_at || now,
      updated_at: file.updated_at || now,
      chunks: file.chunks || 0,
      metadata_json: {
        optimistic_upload: true,
        upload_batch_id: batch.id,
        upload_file_id: file.id,
      },
      size: file.size,
      summary: "",
      summary_status: done ? "completed" : failed ? "failed" : "pending",
      processing_task_status: failed ? "failed" : done ? "completed" : waiting ? "queued" : "processing",
      processing_last_error: file.error_message || "",
    };
  });
}

function mergeUploadPlaceholders(documents: DocumentItem[], placeholders: DocumentItem[]) {
  if (!placeholders.length) return documents;
  const realKeys = new Set(
    documents.flatMap((item) => [item.storage_path, item.source, item.name].filter(Boolean) as string[]),
  );
  return [
    ...placeholders.filter((item) => !realKeys.has(item.storage_path) && !realKeys.has(item.source || "") && !realKeys.has(item.name)),
    ...documents,
  ];
}

function documentRuntimeStatus(item: DocumentItem): { label: string; tone: "neutral" | "running" | "failed" | "done"; title: string } {
  const taskStatus = (item.processing_task_status || "").toLowerCase();
  const parseStatus = (item.parse_status || "").toLowerCase();
  const summaryStatus = (item.summary_status || "none").toLowerCase();
  const queue = item.processing_task_queue ? ` · 队列 ${item.processing_task_queue}` : "";
  const attempt = item.processing_task_attempt || 0;
  const maxAttempts = item.processing_task_max_attempts || 0;
  const retrySuffix = attempt && maxAttempts ? ` · ${attempt}/${maxAttempts}` : "";
  if (item.processing_dead_lettered || taskStatus === "dead_lettered") {
    return { label: `处理失败${retrySuffix}`, tone: "failed", title: `${item.processing_last_error || "任务已进入死信队列"}${queue}` };
  }
  if (taskStatus === "retrying") {
    return { label: `等待重试${retrySuffix}`, tone: "failed", title: `${item.processing_last_error || "处理任务将在稍后重试"}${queue}` };
  }
  if (taskStatus === "queued" || taskStatus === "pending" || taskStatus === "scheduled") {
    return { label: "等待处理", tone: "running", title: `任务已入队，等待 worker 处理${queue}` };
  }
  if (taskStatus === "processing" || parseStatus === "parsing") {
    return { label: `处理中${retrySuffix}`, tone: "running", title: `正在解析、切片或索引文档${queue}` };
  }
  if (parseStatus === "failed") {
    return { label: "解析失败", tone: "failed", title: item.processing_last_error || "文档解析失败" };
  }
  if (summaryStatus === "processing" || summaryStatus === "pending") {
    return { label: "生成概要中", tone: "running", title: "后处理正在生成概要信息" };
  }
  if (summaryStatus === "failed") {
    return { label: "概要失败", tone: "failed", title: item.summary_error || item.processing_last_error || "概要生成失败" };
  }
  if (item.summary_available || summaryStatus === "completed" || parseStatus === "parsed") {
    return { label: "可检索", tone: "done", title: "文档已完成处理，可以用于检索" };
  }
  if (parseStatus === "pending" || parseStatus === "uploaded" || parseStatus === "processing") {
    return { label: "等待处理", tone: "running", title: "文档已进入处理队列，等待后台解析" };
  }
  return { label: "待处理", tone: "neutral", title: "文档尚未完成处理" };
}

function documentRuntimeTaskLine(task?: Record<string, unknown>): string {
  if (!task) return "";
  const status = String(task.processing_task_status || "");
  const taskType = String(task.processing_task_type || "");
  const queue = String(task.processing_task_queue || "");
  const brokerTaskId = String(task.processing_broker_task_id || "");
  const attempt = Number(task.processing_task_attempt || 0);
  const maxAttempts = Number(task.processing_task_max_attempts || 0);
  const latestAttempt = Number(task.processing_latest_attempt || 0);
  const deadLettered = Boolean(task.processing_dead_lettered);
  const parts = [];
  if (taskType) parts.push(taskType);
  if (status) parts.push(`任务 ${status}`);
  if (queue) parts.push(`队列 ${queue}`);
  if (brokerTaskId) parts.push(`broker ${brokerTaskId}`);
  if (attempt || maxAttempts) parts.push(`重试 ${attempt}/${maxAttempts || "-"}`);
  if (latestAttempt) parts.push(`Trace attempt ${latestAttempt}`);
  if (deadLettered) parts.push("死信");
  return parts.join(" · ");
}

function DocumentTileCard({
  item,
  selected,
  retryingId,
  onSelected,
  onOpenDocument,
  onOpenDocumentDetail,
  onDeleteDocument,
  onRetrySummary,
  onRetryProcessing,
  onOpenTrace,
}: {
  item: DocumentItem;
  selected: boolean;
  retryingId: string;
  onSelected: (checked: boolean) => void;
  onOpenDocument: (item: DocumentItem) => void;
  onOpenDocumentDetail: (item: DocumentItem) => void;
  onDeleteDocument: (item: DocumentItem) => void;
  onRetrySummary: (item: DocumentItem) => void;
  onRetryProcessing: (item: DocumentItem) => void;
  onOpenTrace: (item: DocumentItem) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const summary = getDocumentTileSummary(item);
  const runtimeStatus = documentRuntimeStatus(item);
  const optimistic = isUploadPlaceholderDocument(item);
  return (
    <article className="kb-document-card doc-tile-card">
      <label className="doc-select">
        <input type="checkbox" disabled={optimistic} checked={selected} onChange={(event) => onSelected(event.target.checked)} />
        <span>选择</span>
      </label>
      <div className="doc-tile-head">
        <button type="button" className="doc-tile-title" title={item.name} disabled={optimistic} onClick={() => onOpenDocumentDetail(item)}>
          {item.name}
        </button>
        {!optimistic ? <div className="doc-tile-menu-wrap">
          <button
            type="button"
            className="doc-tile-menu-trigger"
            aria-label="文档操作"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
          >
            <MoreIcon />
          </button>
          {menuOpen ? (
            <div className="doc-tile-menu" role="menu">
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  onOpenDocument(item);
                }}
              >
                <LibraryIcon />
                <span>预览文档</span>
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false);
                  onOpenTrace(item);
                }}
              >
                <TraceIcon />
                <span>查看 Trace</span>
              </button>
              {item.summary_status === "failed" ? (
                <button
                  type="button"
                  role="menuitem"
                  disabled={retryingId === item.id}
                  onClick={() => {
                    setMenuOpen(false);
                    onRetrySummary(item);
                  }}
                >
                  <EditIcon />
                  <span>{retryingId === item.id ? "重试中" : "重试摘要"}</span>
                </button>
              ) : null}
              {item.processing_retry_available ? (
                <button
                  type="button"
                  role="menuitem"
                  disabled={retryingId === item.id}
                  onClick={() => {
                    setMenuOpen(false);
                    onRetryProcessing(item);
                  }}
                >
                  <TraceIcon />
                  <span>{retryingId === item.id ? "重试中" : "重试处理"}</span>
                </button>
              ) : null}
              <button
                type="button"
                role="menuitem"
                className="danger"
                onClick={() => {
                  setMenuOpen(false);
                  onDeleteDocument(item);
                }}
              >
                <DeleteIcon />
                <span>删除文档</span>
              </button>
            </div>
          ) : null}
        </div> : null}
      </div>
      <button type="button" className="doc-tile-summary as-detail" disabled={optimistic} onClick={() => onOpenDocumentDetail(item)}>
        {summary}
      </button>
      <button
        type="button"
        className={`doc-runtime-badge ${runtimeStatus.tone}`}
        title={runtimeStatus.title}
        disabled={optimistic}
        onClick={() => onOpenTrace(item)}
      >
        {runtimeStatus.tone === "running" ? <span className="runtime-spinner" aria-hidden="true" /> : null}
        {runtimeStatus.label}
      </button>
      <div className="doc-tile-footer">
        <span>{formatCardDate(item.updated_at)}</span>
        <span>{item.file_type?.toUpperCase() || "FILE"}</span>
      </div>
    </article>
  );
}

function DocumentCard({
  item,
  selected,
  retryingId,
  onSelected,
  onOpenDocument,
  onDeleteDocument,
  onRetrySummary,
  onRetryProcessing,
  onOpenTrace,
}: {
  item: DocumentItem;
  selected: boolean;
  retryingId: string;
  onSelected: (checked: boolean) => void;
  onOpenDocument: (item: DocumentItem) => void;
  onDeleteDocument: (item: DocumentItem) => void;
  onRetrySummary: (item: DocumentItem) => void;
  onRetryProcessing: (item: DocumentItem) => void;
  onOpenTrace: (item: DocumentItem) => void;
}) {
  const runtimeStatus = documentRuntimeStatus(item);
  return (
    <article className="kb-document-card">
      <label className="doc-select"><input type="checkbox" checked={selected} onChange={(event) => onSelected(event.target.checked)} /><span>选择</span></label>
      <div className="kb-document-title-line">
        <h2>{item.name}</h2>
        <TraceStatusButton item={item} onOpenTrace={onOpenTrace} />
        <TraceSummaryStatus item={item} onOpenTrace={onOpenTrace} />
        <span className={`status-pill ${item.parse_status}`}>{item.parse_status === "parsed" ? "可检索" : item.parse_status}</span>
        <button
          type="button"
          className={`doc-runtime-badge inline ${runtimeStatus.tone}`}
          title={runtimeStatus.title}
          onClick={() => onOpenTrace(item)}
        >
          {runtimeStatus.tone === "running" ? <span className="runtime-spinner" aria-hidden="true" /> : null}
          {runtimeStatus.label}
        </button>
      </div>
      <p className="doc-path">{item.source || item.storage_path}</p>
      <p className="kb-document-summary">{item.summary || "暂无概要。可以打开文档预览原始内容，或等待后端概要生成任务完成。"}</p>
      <div className="kb-card-stats">
        <span>{item.file_type?.toUpperCase() || "FILE"}</span>
        <span>{item.chunks} chunks</span>
        <span>{formatDate(item.updated_at)}</span>
        <SummaryStatus status={item.summary_status || "none"} />
      </div>
      <div className="row-actions">
        {item.summary_status === "failed" ? (
          <button type="button" onClick={() => onRetrySummary(item)} disabled={retryingId === item.id}>
            {retryingId === item.id ? "重试中" : "重试概要"}
          </button>
        ) : null}
        {item.processing_retry_available ? (
          <button type="button" onClick={() => onRetryProcessing(item)} disabled={retryingId === item.id}>
            {retryingId === item.id ? "重试中" : "重试处理"}
          </button>
        ) : null}
        <button type="button" onClick={() => onOpenDocument(item)}>查看</button>
        <button type="button" className="danger-action" onClick={() => onDeleteDocument(item)}>删除</button>
      </div>
    </article>
  );
}

function TraceStatusButton({ item, onOpenTrace }: { item: DocumentItem; onOpenTrace: (item: DocumentItem) => void }) {
  return (
    <button type="button" className={`trace-status-button ${item.parse_status || "pending"}`} onClick={() => onOpenTrace(item)}>
      {documentParseStatusLabel(item.parse_status)}
    </button>
  );
}

function TraceSummaryStatus({ item, onOpenTrace }: { item: DocumentItem; onOpenTrace: (item: DocumentItem) => void }) {
  const status = item.summary_status || "none";
  if (status === "processing" || status === "pending") {
    return <button type="button" className="summary-status processing as-button" onClick={() => onOpenTrace(item)}>生成摘要中</button>;
  }
  if (status === "failed") {
    return <button type="button" className="summary-status failed as-button" onClick={() => onOpenTrace(item)}>摘要失败</button>;
  }
  if (status === "completed") return <span className="summary-status completed">摘要已生成</span>;
  return null;
}

function documentParseStatusLabel(status: string) {
  if (status === "parsed") return "可检索";
  if (status === "parsing") return "解析中";
  if (status === "pending") return "等待解析";
  if (status === "failed") return "解析失败";
  return status || "等待解析";
}

function SummaryStatus({ status }: { status: string }) {
  if (status === "processing" || status === "pending") return <span className="summary-status processing">生成概要中</span>;
  if (status === "completed") return <span className="summary-status completed">概要已生成</span>;
  if (status === "failed") return <span className="summary-status failed">概要失败</span>;
  return null;
}

function ProcessingTraceDrawer({
  state,
  onRefresh,
  onClose,
}: {
  state: {
    open: boolean;
    loading: boolean;
    refreshing: boolean;
    error: string;
    document?: DocumentItem;
    data?: DocumentProcessingTrace;
  };
  onRefresh: () => void;
  onClose: () => void;
}) {
  const [selectedSpanId, setSelectedSpanId] = useState("");
  const [detailTab, setDetailTab] = useState<"overview" | "input" | "output" | "metadata" | "raw">("overview");
  const root = state.data?.trace;
  useEffect(() => {
    if (state.open && root) {
      setSelectedSpanId(root.span_id || root.name);
      setDetailTab("overview");
    }
  }, [state.open, root?.span_id, root?.name]);
  if (!state.open) return null;
  const stages = root?.children || [];
  const allSpans = root ? flattenTraceSpans(root) : [];
  const selectedSpan = allSpans.find((span) => (span.span_id || span.name) === selectedSpanId) || root;
  const rootDuration = root?.duration_ms || stages.reduce((total, item) => total + (item.duration_ms || 0), 0);
  const taskLine = documentRuntimeTaskLine(state.data?.processing_task);
  const taskError = String(state.data?.processing_task?.processing_last_error || "");
  return (
    <div className="trace-drawer-layer" role="presentation">
      <button type="button" className="trace-drawer-scrim" aria-label="关闭处理链路" onClick={onClose} />
      <aside className="trace-drawer" role="dialog" aria-modal="true" aria-label="文档处理链路">
        <header className="trace-drawer-header">
          <div>
            <p className="trace-eyebrow">文档处理链路</p>
            <h2>{state.document?.name || "文档"}</h2>
            <span>{state.data?.current_stage ? `当前阶段：${traceStageLabel(state.data.current_stage)}` : "当前阶段：-"}</span>
            {taskLine ? <small className="trace-task-line">{taskLine}</small> : null}
          </div>
          <div className="trace-header-actions">
            {root?.status === "running" ? <span className="trace-live">LIVE</span> : null}
            <button type="button" onClick={onRefresh} disabled={state.loading || state.refreshing}>
              {state.refreshing ? "刷新中" : "刷新"}
            </button>
            <button type="button" onClick={onClose}>关闭</button>
          </div>
        </header>
        {state.error ? <div className="trace-alert">{state.error}</div> : null}
        {state.loading ? (
          <div className="trace-loading">正在加载处理链路...</div>
        ) : root ? (
          <>
            <div className="trace-root-summary">
              <span className={`trace-dot ${root.status}`} />
              <strong>{traceStatusLabel(root.status)}</strong>
              <span>总耗时 {formatDuration(rootDuration)}</span>
              {state.data?.trace_dir ? <small title={state.data.trace_dir}>本地 trace 已记录</small> : null}
            </div>
            {taskError ? <div className="trace-alert compact">{taskError}</div> : null}
            <div className="trace-workspace">
              <div className="trace-stage-list">
                <TraceStageRow
                  stage={root}
                  totalDuration={Math.max(rootDuration, 1)}
                  selectedSpanId={selectedSpanId}
                  onSelect={(span) => {
                    setSelectedSpanId(span.span_id || span.name);
                    setDetailTab("overview");
                  }}
                />
              </div>
              <TraceSpanInspector span={selectedSpan || root} activeTab={detailTab} onTabChange={setDetailTab} />
            </div>
            {root.error ? <TraceErrorBlock error={root.error} /> : null}
          </>
        ) : (
          <div className="trace-loading">暂无处理链路</div>
        )}
      </aside>
    </div>
  );
}

function TraceStageRow({
  stage,
  totalDuration,
  selectedSpanId,
  onSelect,
}: {
  stage: ProcessingTraceSpan;
  totalDuration: number;
  selectedSpanId: string;
  onSelect: (span: ProcessingTraceSpan) => void;
}) {
  const percent = Math.max(4, Math.min(100, Math.round(((stage.duration_ms || 0) / totalDuration) * 100)));
  const selected = selectedSpanId === (stage.span_id || stage.name);
  return (
    <article className={`trace-stage-row ${stage.status} ${selected ? "selected" : ""}`}>
      <button type="button" className="trace-node-button" onClick={() => onSelect(stage)}>
      <div className="trace-stage-main">
        <span className={`trace-dot ${stage.status}`} />
        <div>
          <h3>{stage.label || traceStageLabel(stage.name)}</h3>
          <p>{traceStatusLabel(stage.status)} · {formatDuration(stage.duration_ms || 0)}</p>
        </div>
      </div>
      <div className="trace-stage-meter" aria-hidden="true">
        <span style={{ width: `${stage.status === "pending" || stage.status === "skipped" ? 0 : percent}%` }} />
      </div>
      </button>
      {stage.children?.length ? (
        <div className="trace-child-list">
          {stage.children.map((child) => (
            <TraceChildSpan key={child.span_id || child.name} span={child} depth={0} selectedSpanId={selectedSpanId} onSelect={onSelect} />
          ))}
        </div>
      ) : null}
    </article>
  );
}

function TraceChildSpan({
  span,
  depth,
  selectedSpanId,
  onSelect,
}: {
  span: ProcessingTraceSpan;
  depth: number;
  selectedSpanId: string;
  onSelect: (span: ProcessingTraceSpan) => void;
}) {
  const selected = selectedSpanId === (span.span_id || span.name);
  return (
    <div className={`trace-child-span ${span.status} ${selected ? "selected" : ""}`} style={{ marginLeft: depth ? 14 : 0 }}>
      <button type="button" className="trace-node-button child" onClick={() => onSelect(span)}>
      <div className="trace-child-main">
        <span className={`trace-dot ${span.status}`} />
        <div>
          <h4>{span.label || traceStageLabel(span.name)}</h4>
          <p>{span.kind} · {traceStatusLabel(span.status)} · {formatDuration(span.duration_ms || 0)}</p>
        </div>
      </div>
      </button>
      {span.children?.length ? (
        <div className="trace-child-list nested">
          {span.children.map((child) => (
            <TraceChildSpan key={child.span_id || child.name} span={child} depth={depth + 1} selectedSpanId={selectedSpanId} onSelect={onSelect} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function TraceSpanInspector({
  span,
  activeTab,
  onTabChange,
}: {
  span?: ProcessingTraceSpan;
  activeTab: "overview" | "input" | "output" | "metadata" | "raw";
  onTabChange: (tab: "overview" | "input" | "output" | "metadata" | "raw") => void;
}) {
  if (!span) return <aside className="trace-inspector empty">选择左侧节点查看详情</aside>;
  const metadata = span.metadata || {};
  const tabs: Array<{ id: typeof activeTab; label: string }> = [
    { id: "overview", label: "概览" },
    { id: "input", label: "输入" },
    { id: "output", label: "输出" },
    { id: "metadata", label: "元数据" },
    { id: "raw", label: "原始 JSON" },
  ];
  return (
    <aside className="trace-inspector">
      <header>
        <div>
          <span className={`trace-dot ${span.status}`} />
          <strong>{span.label || traceStageLabel(span.name)}</strong>
        </div>
        <small>{span.kind.toUpperCase()} · {traceStatusLabel(span.status)}</small>
      </header>
      <div className="trace-inspector-tabs" role="tablist">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            className={activeTab === tab.id ? "active" : ""}
            onClick={() => onTabChange(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {activeTab === "overview" ? (
        <div className="trace-inspector-panel">
          <TraceOverview span={span} />
          {span.error ? <TraceErrorBlock error={span.error} compact /> : null}
        </div>
      ) : null}
      {activeTab === "input" ? <TraceRecordPanel title="输入参数" record={span.input || {}} empty="该节点没有记录输入参数。" /> : null}
      {activeTab === "output" ? <TraceRecordPanel title="输出结果" record={span.output || {}} empty="该节点没有记录输出结果。" /> : null}
      {activeTab === "metadata" ? <TraceRecordPanel title="运行元数据" record={metadata} empty="该节点没有额外元数据。" /> : null}
      {activeTab === "raw" ? (
        <pre className="trace-raw-json">{JSON.stringify(span, null, 2)}</pre>
      ) : null}
    </aside>
  );
}

function TraceOverview({ span }: { span: ProcessingTraceSpan }) {
  return (
    <dl className="trace-overview">
      <div><dt>阶段</dt><dd>{span.label || traceStageLabel(span.name)}</dd></div>
      <div><dt>类型</dt><dd>{span.kind}</dd></div>
      <div><dt>状态</dt><dd>{traceStatusLabel(span.status)}</dd></div>
      <div><dt>耗时</dt><dd>{formatDuration(span.duration_ms || 0)}</dd></div>
      <div><dt>开始</dt><dd>{formatTraceTime(span.started_at)}</dd></div>
      <div><dt>结束</dt><dd>{formatTraceTime(span.ended_at)}</dd></div>
    </dl>
  );
}

function TraceRecordPanel({ title, record, empty }: { title: string; record: Record<string, unknown>; empty: string }) {
  const entries = Object.entries(record);
  if (!entries.length) return <div className="trace-empty-panel">{empty}</div>;
  return (
    <div className="trace-inspector-panel">
      <h3>{title}</h3>
      <TraceKeyValues record={record} />
    </div>
  );
}

function flattenTraceSpans(root: ProcessingTraceSpan): ProcessingTraceSpan[] {
  const result: ProcessingTraceSpan[] = [];
  const visit = (span: ProcessingTraceSpan) => {
    result.push(span);
    for (const child of span.children || []) visit(child);
  };
  visit(root);
  return result;
}

function TraceKeyValues({ record }: { record: Record<string, unknown> }) {
  const rows = Object.entries(record);
  if (!rows.length) return null;
  return (
    <dl className="trace-kv">
      {rows.map(([key, value]) => (
        <div key={key}>
          <dt>{traceFieldLabel(key)}</dt>
          <dd>{traceValuePreview(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function traceFieldLabel(key: string) {
  const labels: Record<string, string> = {
    parser: "解析器",
    parser_engine: "解析引擎",
    chunk_strategy: "分块策略",
    parent_chunks: "父块数量",
    child_chunks: "子块数量",
    table_chunks: "表格块数量",
    chunk_count: "分块数量",
    indexed_chunks: "写入索引数量",
    page_count: "页数",
    pages: "页数",
    source: "来源",
    storage_path: "存储路径",
    file_type: "文件类型",
    summary_status: "摘要状态",
    status: "状态",
    trace_id: "Trace ID",
    attempt: "尝试次数",
    duration_ms: "耗时",
    error: "错误",
  };
  return labels[key] || key.replaceAll("_", " ");
}

function TraceErrorBlock({
  error,
  compact = false,
}: {
  error: { type?: string; message?: string; traceback?: string } | null;
  compact?: boolean;
}) {
  if (!error) return null;
  return (
    <div className={compact ? "trace-error compact" : "trace-error"}>
      <strong>{error.type || "Error"}</strong>
      <p>{error.message || "处理失败"}</p>
      {error.traceback ? <pre>{error.traceback}</pre> : null}
    </div>
  );
}

function traceStageLabel(name: string) {
  const labels: Record<string, string> = {
    knowledge_processing: "知识处理",
    document_processing: "文档处理",
    docreader: "文档解析",
    chunking: "分块",
    embedding: "向量化",
    multimodal: "多模态识别",
    postprocess: "后处理",
    "postprocess.question": "问题生成",
    summary: "摘要生成",
    enrichment: "知识增强",
  };
  return labels[name] || name;
}

function traceStatusLabel(status: string) {
  const labels: Record<string, string> = {
    pending: "等待中",
    running: "进行中",
    done: "已完成",
    failed: "失败",
    skipped: "跳过",
    cancelled: "已取消",
  };
  return labels[status] || status;
}

function formatTraceTime(value?: string) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function traceValuePreview(value: unknown) {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return `${value.length} 项`;
  if (typeof value === "object") return JSON.stringify(value).slice(0, 120);
  return String(value);
}

function formatDuration(value: number) {
  if (!value) return "-";
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

function KnowledgeBaseSettingsDialog({
  selected,
  name,
  description,
  indexingStrategy,
  error,
  saving,
  onName,
  onDescription,
  onIndexingStrategy,
  onCancel,
  onSubmit,
  onDelete,
}: {
  selected: KnowledgeBase;
  name: string;
  description: string;
  indexingStrategy: KnowledgeBase["indexing_strategy"];
  error: string;
  saving: boolean;
  onName: (value: string) => void;
  onDescription: (value: string) => void;
  onIndexingStrategy: (value: KnowledgeBase["indexing_strategy"]) => void;
  onCancel: () => void;
  onSubmit: () => void;
  onDelete?: () => void;
}) {
  return (
    <div className="dialog-mask" role="presentation" onClick={onCancel}>
      <section className="kb-dialog" role="dialog" aria-modal="true" aria-label="知识库设置" onClick={(event) => event.stopPropagation()}>
        <header>
          <h2>知识库设置</h2>
          <button type="button" onClick={onCancel} aria-label="关闭">×</button>
        </header>
        <label><span>名称</span><input autoFocus value={name} maxLength={80} onChange={(event) => onName(event.target.value)} /></label>
        <label><span>描述</span><textarea value={description} maxLength={300} onChange={(event) => onDescription(event.target.value)} /></label>
        <fieldset className="kb-settings-indexing">
          <legend>索引策略</legend>
          {([
            ["wiki_enabled", "Wiki"],
            ["dense_enabled", "Dense"],
            ["keyword_enabled", "Keyword"],
            ["graph_enabled", "Graph"],
          ] as const).map(([key, label]) => (
            <label className="kb-check-row" key={key}>
              <input type="checkbox" checked={Boolean(indexingStrategy[key])} onChange={(event) => onIndexingStrategy({ ...indexingStrategy, [key]: event.target.checked })} />
              <span>{label}</span>
            </label>
          ))}
          {indexingStrategy.wiki_enabled && !indexingStrategy.dense_enabled && !indexingStrategy.keyword_enabled ? <p>Wiki-only：文档会保留解析分块并生成 Wiki，不执行向量化。</p> : null}
        </fieldset>
        {error ? <p className="feedback-err">{error}</p> : null}
        <div className="kb-dialog-actions">
          {onDelete && !selected.is_default ? <button type="button" className="danger-action" disabled={saving} onClick={onDelete}>删除知识库</button> : null}
          <span />
          <button type="button" onClick={onCancel}>取消</button>
          <button type="button" className="primary-action" disabled={saving} onClick={onSubmit}>{saving ? "保存中..." : "保存"}</button>
        </div>
      </section>
    </div>
  );
}

function formatDate(value: string) {
  if (!value) return "-";
  return value.slice(0, 10);
}

function formatCardDate(value: string) {
  if (!value) return "-";
  const normalized = value.replace("T", " ");
  const year = normalized.slice(2, 4);
  const rest = normalized.slice(5, 16);
  return year && rest ? `${year}-${rest}` : formatDate(value);
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
