import assert from "node:assert/strict";
import test from "node:test";
import {
  canRetryUploadFile,
  orderedPhases,
  summarizeDocumentCard,
  summarizeProcessingPreview,
  summarizeUploadFile,
} from "./processing-ui.ts";

const phases = [
  { name: "multimodal", status: "partial_failed", retry_eligible: true, errors: ["ocr failed"] },
  { name: "parse", status: "completed" },
  { name: "index", status: "completed" },
];

test("orders processing phases and exposes valid targeted retry only", () => {
  assert.deepEqual(orderedPhases(phases).map((phase) => phase.name), ["parse", "index", "multimodal"]);
  assert.equal(canRetryUploadFile({ status: "completed", retry_eligible: true, phases }), true);
  assert.equal(canRetryUploadFile({ status: "failed", retry_eligible: false, phases }), false);
  assert.equal(canRetryUploadFile({ status: "failed", retry_eligible: true, phases }), true);
});

test("summarizes partial multimodal failure separately from text indexing", () => {
  const summary = summarizeUploadFile({
    status: "completed",
    chunks: 4,
    phases,
    warnings: [],
    errors: [],
    error_message: "",
  });

  assert.equal(summary.hasPartialMultimodalFailure, true);
  assert.equal(summary.errorCount, 1);
  assert.match(summary.phaseText, /multimodal:partial_failed/);
});

test("does not expose provider errors as document card summaries", () => {
  const summary = summarizeDocumentCard({
    parse_status: "parsed",
    processing_task_status: "completed",
    summary: "",
    summary_status: "failed",
    summary_error: "Connection error.",
  });

  assert.equal(summary, "摘要生成失败，文档仍可预览和检索。");
  assert.notEqual(summary, "Connection error.");
});

test("summarizes processing preview decisions", () => {
  const summary = summarizeProcessingPreview({
    doc_id: "doc-1",
    source: "manual.pdf",
    extension: ".pdf",
    characters: 120,
    parent_chunks: 1,
    child_chunks: 2,
    table_chunks: 0,
    preview: "text",
    parser_diagnostics: { requested_engine: "docling", effective_engine: "builtin", fallback_reason: "missing dependency" },
    document_metadata: { page_count: 3, text_page_count: 2, scanned_page_count: 1 },
    chunk_diagnostics: { selected_tier: "legacy", rejected: [{ tier: "heading", reason: "too many tiny chunks" }] },
    chunk_statistics: { count: 3 },
    chunk_previews: [],
  });

  assert.equal(summary.parserDecision, "docling -> builtin");
  assert.deepEqual(summary.pageCounts, { total: 3, native: 2, scanned: 1 });
  assert.equal(summary.selectedTier, "legacy");
  assert.equal(summary.chunkCount, 3);
  assert.equal(summary.fallbackReason, "missing dependency");
  assert.deepEqual(summary.warnings, ["heading: too many tiny chunks"]);
});

test("keeps preview fallback visible when statistics are only aggregate counts", () => {
  const summary = summarizeProcessingPreview({
    doc_id: "doc-2",
    source: "broken.pdf",
    extension: ".pdf",
    characters: 240,
    parent_chunks: 2,
    child_chunks: 3,
    table_chunks: 1,
    preview: "sample",
    parser_diagnostics: {
      requested_engine: "docling",
      effective_engine: "builtin",
      fallback_reason: "engine dependency is unavailable",
      warnings: ["page_1:layout_text_fallback"],
    },
    document_metadata: { page_count: 2, text_page_count: 1, scanned_page_count: 1 },
    chunk_diagnostics: { selected_tier: "heuristic", rejected: [{ tier: "heading", reason: "not enough headings" }] },
    chunk_statistics: {},
    chunk_previews: [],
  });

  assert.equal(summary.parserDecision, "docling -> builtin");
  assert.equal(summary.chunkCount, 6);
  assert.equal(summary.selectedTier, "heuristic");
  assert.deepEqual(summary.warnings, ["page_1:layout_text_fallback", "heading: not enough headings"]);
});
