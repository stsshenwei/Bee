import type { KnowledgeCreationWizardSettings, KnowledgeBaseSelectableType, KnowledgeBaseType } from "./types";

export type KnowledgeCreationValidation =
  | { ok: true }
  | { ok: false; section: KnowledgeCreationWizardSettings["activeSection"]; message: string };

const SELECTABLE_KNOWLEDGE_BASE_TYPES: KnowledgeBaseSelectableType[] = ["document", "wiki"];

export function validateKnowledgeCreationSettings(settings: KnowledgeCreationWizardSettings): KnowledgeCreationValidation {
  if (!settings.name.trim()) {
    return { ok: false, section: "basic", message: "请输入知识库名称" };
  }
  if (!selectedKnowledgeBaseTypes(settings).length) {
    return { ok: false, section: "type", message: "当前仅支持 Document 或 Wiki 类型知识库" };
  }
  return { ok: true };
}

export function toKnowledgeBaseCreateInput(settings: KnowledgeCreationWizardSettings) {
  const selectedTypes = selectedKnowledgeBaseTypes(settings);
  const indexingStrategy = indexingStrategyForSelectedTypes(selectedTypes);
  return {
    name: settings.name.trim(),
    description: settings.description.trim(),
    type: primaryKnowledgeBaseType(selectedTypes),
    is_default: settings.isDefault,
    indexing_strategy: {
      ...indexingStrategy,
      wiki_generation_enabled: settings.indexingStrategy.wiki_generation_enabled ?? true,
      wiki_auto_publish_enabled: settings.indexingStrategy.wiki_auto_publish_enabled ?? false,
    },
    provider_config: {
      parser: settings.parser.engine,
      chunk_strategy: settings.chunking.strategy || "auto",
      parent_chunk_size_chars: settings.chunking.parent_chunk_size_chars,
      child_chunk_size_chars: settings.chunking.child_chunk_size_chars,
      child_chunk_overlap_chars: settings.chunking.child_chunk_overlap_chars,
      parent_child_enabled: Boolean(settings.chunking.parent_child_enabled),
      vector_store: "default",
      embedding: "default",
      enrichment: "default",
    },
  };
}

export function applyKnowledgeBaseTypePreset(
  settings: KnowledgeCreationWizardSettings,
  type: KnowledgeBaseType,
): KnowledgeCreationWizardSettings {
  if (!isSelectableKnowledgeBaseType(type)) return settings;
  const selectedTypes = toggleKnowledgeBaseType(selectedKnowledgeBaseTypes(settings), type);
  const indexingStrategy = indexingStrategyForSelectedTypes(selectedTypes);
  return {
    ...settings,
    selectedTypes,
    type: primaryKnowledgeBaseType(selectedTypes),
    indexingStrategy: {
      ...settings.indexingStrategy,
      ...indexingStrategy,
      wiki_generation_enabled: settings.indexingStrategy.wiki_generation_enabled ?? true,
    },
  };
}

export function selectedKnowledgeBaseTypes(settings: KnowledgeCreationWizardSettings): KnowledgeBaseSelectableType[] {
  const source = settings.selectedTypes?.length ? settings.selectedTypes : [settings.type];
  return SELECTABLE_KNOWLEDGE_BASE_TYPES.filter((type) => source.includes(type));
}

function toggleKnowledgeBaseType(
  selectedTypes: KnowledgeBaseSelectableType[],
  type: KnowledgeBaseSelectableType,
): KnowledgeBaseSelectableType[] {
  if (selectedTypes.includes(type)) {
    return selectedTypes.length > 1 ? selectedTypes.filter((item) => item !== type) : selectedTypes;
  }
  const next = [...selectedTypes, type];
  return SELECTABLE_KNOWLEDGE_BASE_TYPES.filter((item) => next.includes(item));
}

function primaryKnowledgeBaseType(selectedTypes: KnowledgeBaseSelectableType[]): KnowledgeBaseSelectableType {
  if (selectedTypes.includes("document")) return "document";
  if (selectedTypes.includes("wiki")) return "wiki";
  return "document";
}

function indexingStrategyForSelectedTypes(selectedTypes: KnowledgeBaseSelectableType[]) {
  const rawDocumentEnabled = selectedTypes.includes("document");
  return {
    dense_enabled: rawDocumentEnabled,
    keyword_enabled: rawDocumentEnabled,
    graph_enabled: false,
    wiki_enabled: selectedTypes.includes("wiki"),
  };
}

function isSelectableKnowledgeBaseType(type: KnowledgeBaseType): type is KnowledgeBaseSelectableType {
  return type === "document" || type === "wiki";
}
