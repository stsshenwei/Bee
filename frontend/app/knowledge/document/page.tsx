"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft, FileText } from "lucide-react";
import { API_BASE, listKnowledgeBaseDocuments, listKnowledgeBases, previewDocument, readJson } from "../../lib/api";
import type { DocumentItem, DocumentProcessingPreview, KnowledgeBase, ProcessingPreviewChunk } from "../../lib/types";

type RouteParams = {
  knowledgeBaseId: string;
  documentId: string;
};

type ContentState = {
  loading: boolean;
  error: string;
  content: string;
  fileUrl: string;
  mode: "text" | "pdf";
};

export default function KnowledgeDocumentDetailPage() {
  const router = useRouter();
  const [routeParams, setRouteParams] = useState<RouteParams>({ knowledgeBaseId: "", documentId: "" });
  const [knowledgeBase, setKnowledgeBase] = useState<KnowledgeBase | null>(null);
  const [documentItem, setDocumentItem] = useState<DocumentItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [content, setContent] = useState<ContentState>({ loading: false, error: "", content: "", fileUrl: "", mode: "text" });
  const [processingPreview, setProcessingPreview] = useState<{ loading: boolean; error: string; data?: DocumentProcessingPreview }>({
    loading: false,
    error: "",
  });
  const [activeTab, setActiveTab] = useState<"preview" | "chunks">("preview");

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setRouteParams({
      knowledgeBaseId: params.get("kb") || "",
      documentId: params.get("doc") || "",
    });
  }, []);

  useEffect(() => {
    if (!routeParams.knowledgeBaseId || !routeParams.documentId) {
      setLoading(false);
      setError("缺少知识库或文档参数。");
      return;
    }
    let canceled = false;
    async function loadDocument() {
      setLoading(true);
      setError("");
      try {
        const [knowledgeBases, documents] = await Promise.all([
          listKnowledgeBases(),
          listKnowledgeBaseDocuments(routeParams.knowledgeBaseId),
        ]);
        if (canceled) return;
        setKnowledgeBase(knowledgeBases.find((item) => item.id === routeParams.knowledgeBaseId) || null);
        const found = documents.find((item) => item.id === routeParams.documentId) || null;
        setDocumentItem(found);
        if (!found) {
          setError("未找到这个文档，可能已被删除或不属于当前知识库。");
          return;
        }
        void loadContent(found);
      } catch (cause) {
        if (!canceled) setError(cause instanceof Error ? cause.message : "文档详情加载失败");
      } finally {
        if (!canceled) setLoading(false);
      }
    }
    void loadDocument();
    return () => {
      canceled = true;
    };
  }, [routeParams.knowledgeBaseId, routeParams.documentId]);

  useEffect(() => {
    if (activeTab !== "chunks" || !documentItem || processingPreview.loading || processingPreview.data || processingPreview.error) return;
    void loadProcessingPreview(documentItem);
  }, [activeTab, documentItem?.id, processingPreview.loading, processingPreview.data, processingPreview.error]);

  async function loadContent(item: DocumentItem) {
    const source = item.source || item.storage_path;
    const query = `source=${encodeURIComponent(source)}&knowledge_base_id=${encodeURIComponent(item.knowledge_base_id)}`;
    const isPdf = isPdfDocument(item);
    setContent({
      loading: !isPdf,
      error: "",
      content: "",
      fileUrl: isPdf ? `${API_BASE}/documents/file?${query}` : "",
      mode: isPdf ? "pdf" : "text",
    });
    if (isPdf) return;
    try {
      const data = await readJson<{ content?: string }>(await fetch(`${API_BASE}/documents/content?${query}`));
      setContent((current) => ({ ...current, loading: false, content: data.content || "" }));
    } catch (cause) {
      setContent((current) => ({
        ...current,
        loading: false,
        error: cause instanceof Error ? cause.message : "文件内容读取失败",
      }));
    }
  }

  async function loadProcessingPreview(item: DocumentItem) {
    const source = item.source || item.storage_path;
    setProcessingPreview({ loading: true, error: "" });
    try {
      const data = await previewDocument(source, item.knowledge_base_id);
      setProcessingPreview({ loading: false, error: "", data });
    } catch (cause) {
      setProcessingPreview({ loading: false, error: cause instanceof Error ? cause.message : "分块预览加载失败" });
    }
  }

  const source = documentItem?.source || documentItem?.storage_path || "";
  const chunks = processingPreview.data?.chunk_previews || [];
  const previewText = content.content || processingPreview.data?.preview || "";
  const summaryText = getDocumentSummary(documentItem);
  const metadataRows = useMemo(() => buildMetadataRows(documentItem, knowledgeBase), [documentItem, knowledgeBase]);
  const chunkSummary = chunks.length ? `${chunks.length} 个可查看分块` : `${documentItem?.chunks || 0} 个索引分块`;

  return (
    <section className="knowledge-document-detail-page">
      <header className="document-detail-hero">
        <button type="button" className="kb-back" onClick={() => router.push(routeParams.knowledgeBaseId ? `/knowledge?kb=${encodeURIComponent(routeParams.knowledgeBaseId)}` : "/knowledge")}>
          <ArrowLeft size={16} /> 返回文档
        </button>
        <div className="document-detail-title-row">
          <span className="document-detail-file-icon"><FileText size={22} /></span>
          <div>
            <h1>{documentItem?.name || "文档"}</h1>
            <p>{knowledgeBase?.name || "知识库"} · {documentItem?.file_type?.toUpperCase() || "FILE"} · {formatBytes(documentItem?.size)}</p>
          </div>
        </div>
      </header>

      {loading ? <div className="notice">正在加载文档详情...</div> : null}
      {error ? <div className="notice error">{error}</div> : null}

      {documentItem ? (
        <div className="document-detail-layout">
          <section className="document-detail-section document-detail-content-section">
            <header className="document-detail-content-head">
              <div>
                <h2>文件内容</h2>
                <p>{chunkSummary}</p>
              </div>
              <div className="document-detail-tabs" role="tablist" aria-label="文件内容视图">
                <button type="button" className={activeTab === "preview" ? "active" : ""} onClick={() => setActiveTab("preview")}>预览</button>
                <button type="button" className={activeTab === "chunks" ? "active" : ""} onClick={() => setActiveTab("chunks")}>分块</button>
              </div>
            </header>
            {activeTab === "preview" ? (
              <DocumentPreviewPanel content={content} text={previewText} />
            ) : (
              <ChunkPreviewPanel loading={processingPreview.loading} error={processingPreview.error} chunks={chunks} />
            )}
          </section>
          <aside className="document-detail-context" aria-label="文档资料">
            <section className="document-detail-section">
              <header><h2>摘要</h2></header>
              <p className="document-detail-summary">{summaryText}</p>
              {documentItem.keywords_json?.length ? <div className="kb-keywords">{documentItem.keywords_json.map((keyword) => <span key={keyword}>{keyword}</span>)}</div> : null}
            </section>
            <section className="document-detail-section">
              <header><h2>基本信息</h2></header>
              <dl className="document-detail-meta">{metadataRows.map((row) => <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>)}</dl>
              {source ? <details className="document-detail-source"><summary>来源路径</summary><p>{source}</p></details> : null}
            </section>
          </aside>
        </div>
      ) : null}
    </section>
  );
}

function DocumentPreviewPanel({ content, text }: { content: ContentState; text: string }) {
  if (content.mode === "pdf" && content.fileUrl) {
    return <iframe className="document-detail-pdf" title="文档预览" src={content.fileUrl} />;
  }
  if (content.loading) return <div className="document-detail-empty">正在读取文件内容...</div>;
  if (content.error && !text) return <div className="document-detail-empty error">{content.error}</div>;
  return <pre className="document-detail-text">{text || "暂无可预览文本。"}</pre>;
}

function ChunkPreviewPanel({
  loading,
  error,
  chunks,
}: {
  loading: boolean;
  error: string;
  chunks: ProcessingPreviewChunk[];
}) {
  if (loading) return <div className="document-detail-empty">正在生成分块预览...</div>;
  if (error) return <div className="document-detail-empty error">{error}</div>;
  if (!chunks.length) return <div className="document-detail-empty">暂无分块信息。</div>;
  return (
    <div className="document-detail-chunks">
      {chunks.map((chunk, index) => (
        <article className="document-detail-chunk" key={chunk.id || index}>
          <header>
            <strong>{chunk.title_path || `分块 ${index + 1}`}</strong>
            <span>{chunk.type || "chunk"} · {chunk.characters || 0} 字符</span>
          </header>
          <p>{chunk.preview || "暂无预览内容。"}</p>
          <footer>
            {chunk.page_start ? <span>页码 {chunk.page_start}{chunk.page_end && chunk.page_end !== chunk.page_start ? `-${chunk.page_end}` : ""}</span> : null}
            {chunk.approx_tokens ? <span>{chunk.approx_tokens} tokens</span> : null}
            {chunk.strategy ? <span>{chunk.strategy}</span> : null}
          </footer>
        </article>
      ))}
    </div>
  );
}

function buildMetadataRows(item: DocumentItem | null, knowledgeBase: KnowledgeBase | null) {
  if (!item) return [];
  return [
    { label: "上传时间", value: formatDateTime(item.created_at) },
    { label: "更新时间", value: formatDateTime(item.updated_at) },
    { label: "类型", value: item.file_type?.toUpperCase() || "FILE" },
    { label: "知识库", value: knowledgeBase?.name || item.knowledge_base_id },
    { label: "解析状态", value: documentParseStatusLabel(item.parse_status) },
    { label: "摘要状态", value: summaryStatusLabel(item.summary_status || "none") },
    { label: "索引分块", value: `${item.chunks || 0}` },
    { label: "文件大小", value: formatBytes(item.size) },
  ];
}

function getDocumentSummary(item: DocumentItem | null) {
  if (!item) return "";
  if (item.summary?.trim()) return item.summary.trim();
  const status = (item.summary_status || "none").toLowerCase();
  if (status === "pending" || status === "processing") return "摘要正在生成，完成后会自动显示在文档详情中。";
  if (status === "failed") return item.summary_error || "摘要生成失败，文档仍可预览和检索。";
  return "暂无摘要。";
}

function isPdfDocument(item: DocumentItem) {
  const source = `${item.source || item.storage_path || item.name}`.toLowerCase();
  return source.endsWith(".pdf") || item.file_type?.toLowerCase() === "pdf";
}

function documentParseStatusLabel(status: string) {
  if (status === "parsed") return "可检索";
  if (status === "parsing") return "解析中";
  if (status === "pending") return "等待解析";
  if (status === "failed") return "解析失败";
  return status || "等待解析";
}

function summaryStatusLabel(status: string) {
  if (status === "completed") return "摘要已生成";
  if (status === "processing" || status === "pending") return "生成中";
  if (status === "failed") return "生成失败";
  return "未生成";
}

function formatDateTime(value?: string) {
  if (!value) return "-";
  return value.replace("T", " ").slice(0, 19);
}

function formatBytes(value?: number) {
  if (!value) return "-";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
