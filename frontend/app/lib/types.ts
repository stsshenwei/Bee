export type SourceItem = {
  source: string;
  score: number;
  workspace_id?: string;
  knowledge_base_id?: string;
};

export type ReasoningEvidence = {
  source: string;
  title_path?: string;
  score?: number;
  matched_child_ids?: string[];
  preview?: string;
};

export type ReasoningSummary = {
  question: string;
  normalized_query: string;
  retrieval_queries: string[];
  expanded_terms?: string[];
  term_mappings: string[];
  evidence: ReasoningEvidence[];
  summary?: string;
};

export type AgentEventKind =
  | "agent_trace"
  | "tool_call"
  | "tool_result"
  | "agent_query"
  | "agent_thought"
  | "agent_tool_call"
  | "agent_tool_result"
  | "agent_reflection"
  | "agent_remedial_search"
  | "agent_references"
  | "agent_final_answer"
  | "agent_complete"
  | "agent_error"
  | "evidence_summary"
  | "citation_verification";

export type AgentEventStatus = "running" | "completed" | "partial" | "failed" | "skipped";

export type AgentToolSummary = {
  evidenceItems?: number;
  citations?: number;
  usedChunks?: number;
  resultCount?: number;
  docCount?: number;
  matchedChunks?: number;
  readChunks?: number;
  requestedChunks?: number;
  entities?: number;
  graphPaths?: number;
  confidence?: number;
  valid?: boolean;
  verifiedChunks?: number;
  invalidChunks?: number;
  sufficient?: boolean;
  toolCounts?: Record<string, number>;
};

export type AgentStreamEvent = {
  id: string;
  kind: AgentEventKind;
  timestamp: number;
  sequence: number;
  stage?: string;
  status?: AgentEventStatus;
  summary?: string;
  tool?: string;
  action?: string;
  inputSummary?: string;
  outputSummary?: string;
  sourceChunkIds: string[];
  sourceTitles?: string[];
  required?: boolean;
  limits?: Record<string, unknown>;
  counts?: AgentToolSummary;
  metadata: Record<string, unknown>;
};

export type AgentTimelineStep = {
  id: string;
  kind: "stage" | "tool" | "evidence" | "citation" | "thought" | "reflection" | "references" | "answer" | "complete" | "error";
  title: string;
  status: AgentEventStatus;
  summary?: string;
  detail?: string;
  tool?: string;
  action?: string;
  startedAt: number;
  finishedAt?: number;
  elapsedMs?: number;
  sourceChunkIds: string[];
  sourceTitles?: string[];
  counts?: AgentToolSummary;
};

export type AgentRunSummary = {
  status: AgentEventStatus;
  completedSteps: number;
  totalSteps: number;
  elapsedMs: number;
  evidenceCount?: number;
  citationStatus?: "passed" | "failed" | "unknown";
  failureSummary?: string;
  reasoningRounds?: number;
  toolCalls?: number;
  referencedDocuments?: number;
  remedialUsed?: boolean;
};

export type MemoryRecord = {
  id: string;
  scope: string;
  type: string;
  content: string;
  confidence?: number;
  status?: string;
  created_at?: string | null;
  updated_at?: string | null;
};

export type MemoryUpdate = MemoryRecord & {
  action?: string;
};

export type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  chatMode?: "quick" | "reasoning" | "wiki";
  attachments?: ChatMessageAttachment[];
  sources?: SourceItem[];
  reasoning?: ReasoningSummary;
  agentEvents?: AgentStreamEvent[];
  agentTimeline?: AgentTimelineStep[];
  agentSummary?: AgentRunSummary;
  evidenceSummary?: Record<string, unknown>;
  citationVerification?: Record<string, unknown>;
  agentCompleted?: boolean;
};

export type ChatAttachment = {
  id: string;
  filename: string;
  content_type: string;
  size: number;
  status: string;
  created_at: string;
  expires_at: string;
  parse_error?: string | null;
};

export type ChatMessageAttachment = {
  id: string;
  filename: string;
};

export type FeedbackState = {
  status: "idle" | "liked" | "disliked" | "submitting" | "saved" | "error";
  draft: string;
  savedTitle?: string;
  savedSource?: string;
  error?: string;
};

export type DocumentItem = {
  id: string;
  workspace_id: string;
  knowledge_base_id: string;
  name: string;
  file_type: string;
  storage_path: string;
  parse_status: "pending" | "parsing" | "parsed" | "failed" | string;
  created_at: string;
  updated_at: string;
  chunks: number;
  metadata_json: Record<string, unknown>;
  source?: string;
  size?: number;
  summary?: string;
  keywords_json?: string[];
  suggested_questions_json?: string[];
  summary_status?: "none" | "pending" | "processing" | "completed" | "failed" | string;
  summary_error?: string;
  summary_model_ref?: string;
  summary_generated_at?: string | null;
  summary_version?: number;
  summary_available?: boolean;
  processing_task_id?: string;
  processing_task_type?: string;
  processing_task_status?: string;
  processing_task_queue?: string;
  processing_broker_task_id?: string;
  processing_task_attempt?: number;
  processing_task_max_attempts?: number;
  processing_dead_lettered?: boolean;
  processing_last_error?: string;
  processing_dead_letter_reason?: string;
  processing_retry_available?: boolean;
  processing_latest_attempt?: number;
};

export type ProcessingTraceStatus = "pending" | "running" | "done" | "failed" | "skipped" | "cancelled" | string;

export type ProcessingTraceSpan = {
  span_id: string;
  name: string;
  label?: string;
  kind: "root" | "stage" | "subspan" | "generation" | string;
  status: ProcessingTraceStatus;
  started_at?: string;
  ended_at?: string;
  duration_ms?: number;
  input?: Record<string, unknown>;
  output?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  error?: { type?: string; message?: string; traceback?: string } | null;
  children?: ProcessingTraceSpan[];
};

export type DocumentProcessingTrace = {
  document_id: string;
  knowledge_base_id: string;
  parse_status: string;
  summary_status?: string;
  current_attempt: number;
  current_stage: string;
  trace: ProcessingTraceSpan;
  last_error?: { type?: string; message?: string; traceback?: string } | null;
  trace_dir?: string;
  processing_task?: Record<string, unknown>;
};

export type KnowledgeBaseAggregate = {
  document_count: number;
  indexed_chunk_count: number;
  processing_count: number;
  failed_count: number;
  wiki_page_count?: number;
  wiki_issue_count?: number;
  reset_required: boolean;
};

export type KnowledgeBaseType = "document" | "faq" | "wiki" | "future";

export type KnowledgeBaseCreationSection =
  | "basic"
  | "type"
  | "model"
  | "vector"
  | "parser"
  | "chunking"
  | "image_ocr"
  | "audio"
  | "graph"
  | "advanced";

export type EffectiveProviderStatus = {
  requested: Record<string, string | boolean | number | null>;
  effective: Record<string, string | boolean | number | null>;
  inactive_overrides: string[];
  warnings?: string[];
  unavailable_features?: string[];
};

export type KnowledgeCreationWizardSettings = {
  name: string;
  description: string;
  type: KnowledgeBaseType;
  isDefault: boolean;
  activeSection: KnowledgeBaseCreationSection;
  indexingStrategy: {
    dense_enabled: boolean;
    keyword_enabled: boolean;
    graph_enabled: boolean;
    wiki_enabled: boolean;
    wiki_generation_enabled?: boolean;
    wiki_auto_publish_enabled?: boolean;
  };
  parser: {
    engine: string;
    readOnly: boolean;
  };
  chunking: {
    strategy?: "auto" | "heading" | "heuristic" | "recursive";
    parent_chunk_size_chars: number;
    child_chunk_size_chars: number;
    child_chunk_overlap_chars: number;
    parent_child_enabled?: boolean;
  };
  processing: {
    question_generation_enabled: boolean;
    enrichment_enabled: boolean;
    ocr_enabled: boolean;
    multimodal_enabled: boolean;
    audio_enabled: boolean;
  };
  providerStatus?: EffectiveProviderStatus;
};

export type KnowledgeBase = {
  id: string;
  workspace_id: string;
  name: string;
  description: string;
  type: KnowledgeBaseType;
  is_default: boolean;
  status: "active" | "archived";
  indexing_strategy: {
    dense_enabled: boolean;
    keyword_enabled: boolean;
    graph_enabled: boolean;
    wiki_enabled: boolean;
    wiki_generation_enabled?: boolean;
    wiki_auto_publish_enabled?: boolean;
  };
  provider_config: {
    requested: Record<string, string>;
    effective: Record<string, string>;
    inactive_overrides: string[];
  };
  aggregate: KnowledgeBaseAggregate;
  created_at: string;
  updated_at: string;
};

export type DocumentViewMode = "grid" | "list";

export type DocumentFilters = {
  q: string;
  tag: string;
  file_type: string;
  status: string;
  source: string;
  created_from: string;
  created_to: string;
};

export type DocumentUploadResult = {
  doc_id: string;
  source: string;
  filename: string;
  size: number;
  parse_status: string;
  chunks: number;
  error?: string | null;
};

export type UploadTaskStatus = "queued" | "uploading" | "parsing" | "parsed" | "failed";

export type UploadBatchStatus =
  | "draft"
  | "uploading"
  | "ready_to_process"
  | "processing"
  | "completed"
  | "partial_failed"
  | "failed"
  | "canceled";

export type UploadFileTaskStatus =
  | "pending"
  | "uploaded"
  | "parsing"
  | "indexed"
  | "enrichment_pending"
  | "completed"
  | "failed"
  | "canceled";

export type UploadBatchSettings = {
  parser_engine?: string;
  pdf_force_scanned?: boolean;
  pdf_render_dpi?: number;
  pdf_jpeg_quality?: number;
  pdf_max_pages?: number;
  pdf_max_image_edge_px?: number;
  pdf_render_concurrency?: number;
  chunk_strategy?: "auto" | "heading" | "heuristic" | "recursive";
  size_unit?: "chars" | string;
  parent_chunk_size_chars?: number;
  child_chunk_size_chars?: number;
  child_chunk_overlap_chars?: number;
  parent_child_enabled?: boolean;
  max_protected_span_chars?: number;
  embedding_token_limit?: number;
  dense_enabled?: boolean;
  keyword_enabled?: boolean;
  question_generation_enabled?: boolean;
  graph_enabled?: boolean;
  ocr_enabled?: boolean;
  ocr_provider?: string;
  ocr_min_confidence?: number;
  caption_enabled?: boolean;
  caption_provider?: string;
  multimodal_enabled?: boolean;
  audio_enabled?: boolean;
  processing_version?: string;
  inactive_overrides?: string[];
  warnings?: string[];
  requested?: Record<string, unknown>;
  effective?: Record<string, unknown>;
};

export type ParserEngineInfo = {
  name: string;
  file_types: string[];
  available: boolean;
  unavailable_reason: string;
};

export type ParserEnginesResponse = {
  items?: ParserEngineInfo[];
  default?: string;
};

export type ProcessingPreviewParserDiagnostics = {
  requested_engine?: string;
  effective_engine?: string;
  parser_name?: string;
  fallback_reason?: string;
  parse_duration_ms?: number;
  warnings?: string[];
};

export type ProcessingPreviewChunkDiagnostics = {
  selected_tier?: string;
  tier_chain?: string[];
  rejected?: Array<{ tier?: string; reason?: string }>;
  profile?: Record<string, unknown>;
};

export type ProcessingPreviewChunkStats = {
  count?: number;
  minimum?: number;
  maximum?: number;
  average?: number;
  stddev?: number;
  tiny_count?: number;
  oversize_count?: number;
};

export type ProcessingPreviewChunk = {
  id?: string;
  type?: string;
  characters?: number;
  approx_tokens?: number;
  parent_id?: string | null;
  title_path?: string;
  page_start?: number | null;
  page_end?: number | null;
  strategy?: string;
  preview?: string;
};

export type DocumentProcessingPreview = {
  doc_id: string;
  source: string;
  extension: string;
  characters: number;
  parent_chunks: number;
  child_chunks: number;
  table_chunks: number;
  preview: string;
  parser_diagnostics: ProcessingPreviewParserDiagnostics;
  document_metadata: Record<string, unknown>;
  chunk_diagnostics: ProcessingPreviewChunkDiagnostics;
  chunk_statistics: ProcessingPreviewChunkStats;
  chunk_previews: ProcessingPreviewChunk[];
};

export type UploadProcessingPhaseName = "parse" | "chunk" | "index" | "multimodal" | "postprocess" | string;

export type UploadProcessingPhaseStatus =
  | "pending"
  | "processing"
  | "completed"
  | "failed"
  | "partial_failed"
  | "skipped"
  | string;

export type UploadProcessingPhase = {
  name: UploadProcessingPhaseName;
  status: UploadProcessingPhaseStatus;
  started_at?: string | null;
  finished_at?: string | null;
  warnings?: string[];
  errors?: string[];
  retry_eligible?: boolean;
};

export type UploadBatchAggregate = {
  total: number;
  uploaded: number;
  processing: number;
  completed: number;
  failed: number;
  canceled: number;
};

export type UploadFileTaskRecord = {
  id: string;
  batch_id: string;
  workspace_id: string;
  knowledge_base_id: string;
  original_name: string;
  relative_path: string;
  storage_path: string;
  size: number;
  status: UploadFileTaskStatus;
  document_id?: string | null;
  chunks: number;
  error_message: string;
  phases: UploadProcessingPhase[];
  warnings: string[];
  errors: string[];
  retry_eligible: boolean;
  created_at: string;
  updated_at: string;
};

export type UploadBatch = {
  id: string;
  workspace_id: string;
  knowledge_base_id: string;
  status: UploadBatchStatus;
  settings: UploadBatchSettings;
  effective_settings?: UploadBatchSettings;
  aggregate: UploadBatchAggregate;
  files: UploadFileTaskRecord[];
  error_message: string;
  created_at: string;
  updated_at: string;
  confirmed_at?: string | null;
  completed_at?: string | null;
};

export type UploadFileTask = {
  id: string;
  file: File;
  relativePath: string;
  size: number;
  status: UploadTaskStatus;
  progress: number;
  source?: string;
  chunks?: number;
  error?: string;
};

export type WikiSourceRef = {
  doc_id: string;
  chunk_id?: string;
  title?: string;
};

export type WikiPage = {
  id: string;
  workspace_id: string;
  knowledge_base_id: string;
  slug: string;
  title: string;
  page_type: "summary" | "entity" | "concept" | "synthesis" | "manual" | string;
  status: "draft" | "published" | "stale" | "archived" | string;
  content_markdown: string;
  summary: string;
  parent_slug: string;
  folder_id: string;
  category_path: string[];
  wiki_path: string;
  depth: number;
  sort_order: number;
  source_refs: WikiSourceRef[];
  chunk_refs: string[];
  in_links: string[];
  out_links: string[];
  aliases: string[];
  metadata: Record<string, unknown>;
  version: number;
  created_at: string;
  updated_at: string;
};

export type WikiFolder = {
  id: string;
  workspace_id: string;
  knowledge_base_id: string;
  parent_id: string;
  name: string;
  path: string;
  depth: number;
  sort_order: number;
  page_count: number;
  created_at: string;
  updated_at: string;
};

export type WikiIssue = {
  id: string;
  workspace_id: string;
  knowledge_base_id: string;
  slug: string;
  issue_type: "incorrect" | "outdated" | "missing_source" | "conflict" | "other" | string;
  description: string;
  suspected_doc_ids: string[];
  suspected_chunk_ids: string[];
  status: "open" | "resolved" | "wontfix" | string;
  reported_by: string;
  created_at: string;
  updated_at: string;
};

export type WikiProposal = {
  id: string;
  workspace_id: string;
  knowledge_base_id: string;
  action: "write_page" | "replace_text" | "rename_page" | "delete_page" | string;
  slug: string;
  status: "pending" | "applied" | "rejected" | string;
  title: string;
  content_markdown: string;
  payload: Record<string, unknown>;
  source_refs: WikiSourceRef[];
  chunk_refs: string[];
  reason: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  applied_at: string;
  rejected_at: string;
};

export type WikiGraph = {
  nodes: Array<Record<string, unknown>>;
  edges: Array<Record<string, unknown>>;
  meta: Record<string, unknown>;
};

export type WikiGenerationTask = {
  id: string;
  workspace_id: string;
  knowledge_base_id: string;
  doc_id: string;
  status: "pending" | "running" | "completed" | "failed" | "skipped" | string;
  page_slug: string;
  error_message: string;
  config: Record<string, unknown>;
  attempts: number;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  attempt_history?: Array<{
    id: string;
    attempt: number;
    worker_id: string;
    status: string;
    error_code: string;
    error_message: string;
    started_at: string;
    finished_at?: string | null;
  }>;
};

export type WikiOverview = {
  page_counts: Record<string, number>;
  system_pages: WikiPage[];
  open_issue_count: number;
  active_task_count: number;
  task_states: Record<string, number>;
};

export type WikiLog = {
  id: string;
  event_type: string;
  document_id: string;
  page_slugs: string[];
  outcome: string;
  message: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type WikiProcessingTask = {
  id: string;
  task_type: "wiki.ingest" | "wiki.finalize" | string;
  workspace_id: string;
  knowledge_base_id: string;
  document_id: string;
  status: string;
  attempt: number;
  max_attempts: number;
  next_run_at: string;
  last_error_code: string;
  last_error_message: string;
  trace_id: string;
  source_revision: string;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
};
