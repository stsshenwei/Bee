import assert from "node:assert/strict";
import test from "node:test";
import { applyKnowledgeBaseTypePreset, toKnowledgeBaseCreateInput, validateKnowledgeCreationSettings } from "./knowledge-validation.ts";

const baseSettings = {
  name: " 产品资料 ",
  description: " 文档知识库 ",
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

test("allows supported metadata knowledge-base types and blocks placeholders", () => {
  const faq = validateKnowledgeCreationSettings({ ...baseSettings, type: "faq", activeSection: "type" });
  const future = validateKnowledgeCreationSettings({ ...baseSettings, type: "future", activeSection: "type" });

  assert.deepEqual(faq, { ok: true });
  assert.deepEqual(future, { ok: false, section: "type", message: "当前仅支持 Document、FAQ 或 Wiki 类型知识库" });
});

test("builds supported create payload while preserving requested settings", () => {
  const payload = toKnowledgeBaseCreateInput({
    ...baseSettings,
    type: "wiki",
    isDefault: true,
    indexingStrategy: {
      dense_enabled: true,
      keyword_enabled: false,
      graph_enabled: true,
      wiki_enabled: true,
      wiki_generation_enabled: true,
      wiki_auto_publish_enabled: false,
    },
    parser: { engine: "default", readOnly: true },
  });

  assert.equal(payload.name, "产品资料");
  assert.equal(payload.description, "文档知识库");
  assert.equal(payload.type, "wiki");
  assert.equal(payload.is_default, true);
  assert.deepEqual(payload.indexing_strategy, {
    dense_enabled: true,
    keyword_enabled: false,
    graph_enabled: true,
    wiki_enabled: true,
    wiki_generation_enabled: true,
    wiki_auto_publish_enabled: false,
  });
  assert.equal(payload.provider_config.parser, "default");
});

test("applies Wiki-only preset without preventing later explicit combinations", () => {
  const wiki = applyKnowledgeBaseTypePreset(baseSettings, "wiki");
  assert.deepEqual(wiki.indexingStrategy, {
    dense_enabled: false,
    keyword_enabled: false,
    graph_enabled: false,
    wiki_enabled: true,
    wiki_generation_enabled: true,
    wiki_auto_publish_enabled: false,
  });

  const combined = {
    ...wiki,
    indexingStrategy: { ...wiki.indexingStrategy, dense_enabled: true },
  };
  assert.equal(toKnowledgeBaseCreateInput(combined).indexing_strategy.dense_enabled, true);
  assert.equal(toKnowledgeBaseCreateInput(combined).indexing_strategy.wiki_enabled, true);
});
