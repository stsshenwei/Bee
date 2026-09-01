import assert from "node:assert/strict";
import test from "node:test";
import {
  archiveKnowledgeBase,
  generateWikiPage,
  listParserEngines,
  retryDocumentProcessing,
  updateUploadBatchSettings,
  uploadChatAttachment,
} from "./api.ts";

function jsonResponse(body, init = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status || 200,
    headers: { "Content-Type": "application/json" },
  });
}

test("lists parser engines including unavailable optional engines", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse({
      items: [
        { name: "builtin", file_types: ["pdf", "docx"], available: true, unavailable_reason: "" },
        { name: "docling", file_types: ["pdf"], available: false, unavailable_reason: "engine dependency is unavailable" },
      ],
    });
  };

  const engines = await listParserEngines();

  assert.equal(calls[0].url, "http://localhost:8000/parser-engines");
  assert.equal(engines[1].name, "docling");
  assert.equal(engines[1].available, false);
  assert.match(engines[1].unavailable_reason, /dependency/);
});

test("deletes a knowledge base through the scoped archive endpoint", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse({ id: "kb/a", status: "archived" });
  };

  const deleted = await archiveKnowledgeBase("kb/a");

  assert.equal(calls[0].url, "http://localhost:8000/knowledge-bases/kb%2Fa");
  assert.equal(calls[0].options.method, "DELETE");
  assert.equal(deleted.status, "archived");
});

test("serializes force-scanned and effective upload processing settings", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse({
      id: "batch-1",
      workspace_id: "default",
      knowledge_base_id: "kb-1",
      status: "staged",
      settings: {},
      aggregate: { total: 0, uploaded: 0, processing: 0, completed: 0, failed: 0, canceled: 0 },
      files: [],
      error_message: "",
      created_at: "",
      updated_at: "",
    });
  };

  await updateUploadBatchSettings("kb-1", "batch-1", {
    parser_engine: "builtin",
    pdf_force_scanned: true,
    pdf_render_dpi: 300,
    pdf_jpeg_quality: 85,
    pdf_max_pages: 12,
    pdf_max_image_edge_px: 1600,
    pdf_render_concurrency: 1,
    chunk_strategy: "auto",
    parent_child_enabled: true,
    parent_chunk_size_chars: 4096,
    child_chunk_size_chars: 384,
    child_chunk_overlap_chars: 76,
    ocr_enabled: false,
    caption_enabled: false,
  });

  assert.match(calls[0].url, /\/knowledge-bases\/kb-1\/upload-batches\/batch-1\/settings$/);
  assert.equal(calls[0].options.method, "PATCH");
  const payload = JSON.parse(calls[0].options.body);
  assert.equal(payload.settings.pdf_force_scanned, true);
  assert.equal(payload.settings.pdf_render_concurrency, 1);
  assert.equal(payload.settings.ocr_enabled, false);
});

test("uploads temporary chat attachment as multipart form data", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse({
      id: "att-1",
      filename: "notes.txt",
      content_type: "text/plain",
      size: 12,
      status: "parsed",
      created_at: "2026-01-01T00:00:00",
      expires_at: "2026-01-01T01:00:00",
    });
  };

  const attachment = await uploadChatAttachment(new File(["hello"], "notes.txt", { type: "text/plain" }));

  assert.equal(calls[0].url, "http://localhost:8000/chat/attachments");
  assert.equal(calls[0].options.method, "POST");
  assert.ok(calls[0].options.body instanceof FormData);
  assert.equal(attachment.id, "att-1");
});

test("retries a dead-letter document processing task", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse({
      id: "doc/1",
      workspace_id: "ws",
      knowledge_base_id: "kb/1",
      name: "manual.md",
      file_type: "md",
      storage_path: "manual.md",
      parse_status: "processing",
      created_at: "",
      updated_at: "",
      metadata_json: {},
      chunks: 1,
      processing_task_status: "retrying",
      processing_task_queue: "core",
      processing_retry_available: false,
    });
  };

  const result = await retryDocumentProcessing("doc/1", "kb/1");

  assert.equal(calls[0].url, "http://localhost:8000/documents/doc%2F1/processing/retry?knowledge_base_id=kb%2F1");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(result.processing_task_status, "retrying");
  assert.equal(result.processing_task_queue, "core");
});

test("generates a Wiki draft from an indexed document", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push({ url: String(url), options });
    return jsonResponse({
      task: {
        id: "task-1",
        workspace_id: "ws",
        knowledge_base_id: "kb-1",
        doc_id: "doc-1",
        status: "completed",
        page_slug: "manual",
        error_message: "",
        config: {},
        attempts: 1,
        created_at: "",
        updated_at: "",
        started_at: "",
        finished_at: "",
      },
      page: null,
      proposal: null,
    });
  };

  const result = await generateWikiPage("kb-1", { doc_id: "doc-1", max_source_chunks: 4 });

  assert.match(calls[0].url, /\/knowledge-bases\/kb-1\/wiki\/generate$/);
  assert.equal(calls[0].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].options.body), { doc_id: "doc-1", max_source_chunks: 4 });
  assert.equal(result.task.status, "completed");
});
