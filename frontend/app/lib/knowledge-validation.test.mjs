import assert from "node:assert/strict";
import test from "node:test";
import {
  applyKnowledgeBaseTypePreset,
  selectedKnowledgeBaseTypes,
  toKnowledgeBaseCreateInput,
  validateKnowledgeCreationSettings,
} from "./knowledge-validation.ts";

const baseSettings = {
  name: " 产品资料 ",
  description: " 文档知识库 ",
  type: "document",
  selectedTypes: ["document"],
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
    engine: "default",
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

test("blocks empty knowledge-base names without losing wizard section context", () => {
  const result = validateKnowledgeCreationSettings({ ...baseSettings, name: "   " });

  assert.deepEqual(result, { ok: false, section: "basic", message: "请输入知识库名称" });
});

test("allows creation-selectable knowledge-base types and blocks removed placeholders", () => {
  const wiki = validateKnowledgeCreationSettings({ ...baseSettings, type: "wiki", selectedTypes: ["wiki"], activeSection: "type" });
  const faq = validateKnowledgeCreationSettings({ ...baseSettings, type: "faq", selectedTypes: [], activeSection: "type" });
  const future = validateKnowledgeCreationSettings({ ...baseSettings, type: "future", selectedTypes: [], activeSection: "type" });

  assert.deepEqual(wiki, { ok: true });
  assert.deepEqual(faq, { ok: false, section: "type", message: "当前仅支持 Document 或 Wiki 类型知识库" });
  assert.deepEqual(future, { ok: false, section: "type", message: "当前仅支持 Document 或 Wiki 类型知识库" });
});

test("derives document and wiki indexes from multi-selected types", () => {
  const payload = toKnowledgeBaseCreateInput({
    ...baseSettings,
    selectedTypes: ["document", "wiki"],
    isDefault: true,
    indexingStrategy: {
      dense_enabled: false,
      keyword_enabled: false,
      graph_enabled: true,
      wiki_enabled: false,
      wiki_generation_enabled: true,
      wiki_auto_publish_enabled: false,
    },
    parser: { engine: "default", readOnly: true },
  });

  assert.equal(payload.name, "产品资料");
  assert.equal(payload.description, "文档知识库");
  assert.equal(payload.type, "document");
  assert.equal(payload.is_default, true);
  assert.deepEqual(payload.indexing_strategy, {
    dense_enabled: true,
    keyword_enabled: true,
    graph_enabled: false,
    wiki_enabled: true,
    wiki_generation_enabled: true,
    wiki_auto_publish_enabled: false,
  });
  assert.equal(payload.provider_config.parser, "default");
});

test("toggles type cards while keeping at least one selected type", () => {
  const combined = applyKnowledgeBaseTypePreset(baseSettings, "wiki");
  assert.deepEqual(selectedKnowledgeBaseTypes(combined), ["document", "wiki"]);
  assert.deepEqual(combined.indexingStrategy, {
    dense_enabled: true,
    keyword_enabled: true,
    graph_enabled: false,
    wiki_enabled: true,
    wiki_generation_enabled: true,
    wiki_auto_publish_enabled: false,
  });

  const wikiOnly = applyKnowledgeBaseTypePreset(combined, "document");
  assert.deepEqual(selectedKnowledgeBaseTypes(wikiOnly), ["wiki"]);
  assert.equal(wikiOnly.type, "wiki");
  assert.equal(toKnowledgeBaseCreateInput(wikiOnly).indexing_strategy.dense_enabled, false);
  assert.equal(toKnowledgeBaseCreateInput(wikiOnly).indexing_strategy.wiki_enabled, true);

  assert.deepEqual(selectedKnowledgeBaseTypes(applyKnowledgeBaseTypePreset(wikiOnly, "wiki")), ["wiki"]);
});
