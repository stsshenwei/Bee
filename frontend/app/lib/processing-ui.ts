import type { DocumentItem, DocumentProcessingPreview, UploadFileTaskRecord, UploadProcessingPhase } from "./types";

const PHASE_ORDER = ["parse", "chunk", "index", "multimodal", "postprocess"];
const ACTIVE_DOCUMENT_TASK_STATUSES = new Set(["queued", "pending", "scheduled", "processing", "retrying"]);

export function isDocumentProcessingActive(
  item: Pick<DocumentItem, "parse_status" | "processing_task_status">,
): boolean {
  const taskStatus = String(item.processing_task_status || "").toLowerCase();
  const parseStatus = String(item.parse_status || "").toLowerCase();
  return ACTIVE_DOCUMENT_TASK_STATUSES.has(taskStatus) || ["pending", "uploaded", "parsing", "processing"].includes(parseStatus);
}

export function summarizeDocumentCard(
  item: Pick<DocumentItem, "parse_status" | "processing_task_status" | "summary" | "summary_status" | "summary_error">,
): string {
  if (isDocumentProcessingActive(item)) {
    return "文档处理中，摘要和后处理结果将在任务完成后更新。";
  }
  const summary = item.summary?.trim();
  if (summary) return summary;
  const status = String(item.summary_status || "none").toLowerCase();
  if (status === "pending" || status === "processing") {
    return "摘要生成中，完成后会自动显示在卡片中。";
  }
  if (status === "failed") {
    return "摘要生成失败，文档仍可预览和检索。";
  }
  return "未生成摘要。";
}

export function orderedPhases(phases: UploadProcessingPhase[] = []): UploadProcessingPhase[] {
  return [...phases].sort((left, right) => {
    const leftIndex = PHASE_ORDER.indexOf(left.name);
    const rightIndex = PHASE_ORDER.indexOf(right.name);
    return (leftIndex === -1 ? 99 : leftIndex) - (rightIndex === -1 ? 99 : rightIndex);
  });
}

export function canRetryUploadFile(file: Pick<UploadFileTaskRecord, "retry_eligible" | "phases" | "status">): boolean {
  return Boolean(
    file.retry_eligible &&
      orderedPhases(file.phases).some((phase) => phase.retry_eligible || phase.status === "failed" || phase.status === "partial_failed"),
  );
}

export function summarizeUploadFile(file: Pick<UploadFileTaskRecord, "status" | "chunks" | "phases" | "warnings" | "errors" | "error_message">) {
  const phases = orderedPhases(file.phases);
  const multimodal = phases.find((phase) => phase.name === "multimodal");
  return {
    text: `${file.status} · ${file.chunks} chunks`,
    phaseText: phases.map((phase) => `${phase.name}:${phase.status}`).join(" / "),
    hasPartialMultimodalFailure: multimodal?.status === "partial_failed",
    warningCount: (file.warnings || []).length + phases.reduce((total, phase) => total + (phase.warnings || []).length, 0),
    errorCount: (file.errors || []).length + (file.error_message ? 1 : 0) + phases.reduce((total, phase) => total + (phase.errors || []).length, 0),
  };
}

export function summarizeProcessingPreview(preview: DocumentProcessingPreview) {
  const parser = preview.parser_diagnostics || {};
  const chunk = preview.chunk_diagnostics || {};
  const metadata = preview.document_metadata || {};
  return {
    parserDecision: `${parser.requested_engine || "builtin"} -> ${parser.effective_engine || "builtin"}`,
    parserName: parser.parser_name || "",
    fallbackReason: parser.fallback_reason || "",
    pageCounts: {
      total: Number(metadata.page_count || 0),
      native: Number(metadata.text_page_count || 0),
      scanned: Number(metadata.scanned_page_count || 0),
    },
    selectedTier: chunk.selected_tier || "",
    rejectedTiers: chunk.rejected || [],
    warnings: [...(parser.warnings || []), ...((chunk.rejected || []).map((item) => `${item.tier}: ${item.reason}`))],
    chunkCount: preview.chunk_statistics.count || preview.parent_chunks + preview.child_chunks + preview.table_chunks,
  };
}
