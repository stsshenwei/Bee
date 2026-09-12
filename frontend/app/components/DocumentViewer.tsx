"use client";

import { FileText, X } from "lucide-react";
import { ModalSurface } from "./ModalSurface";

type DocumentViewerProps = {
  open: boolean;
  source: string;
  loading: boolean;
  error: string;
  mode: "text" | "pdf";
  content: string;
  fileUrl: string;
  onClose: () => void;
};

export function DocumentViewer({ open, source, loading, error, mode, content, fileUrl, onClose }: DocumentViewerProps) {
  if (!open) return null;

  return (
    <section className="doc-viewer-mask" onClick={onClose}>
      <ModalSurface className="doc-viewer" aria-label="文档预览" onClose={onClose} onClick={(event) => event.stopPropagation()}>
        <header className="doc-viewer-header">
          <div className="doc-viewer-title" title={source}><FileText size={18} /><span>{source.split(/[\\/]/).pop() || "文档内容"}</span></div>
          <button type="button" className="doc-close" title="关闭预览" aria-label="关闭预览" onClick={onClose}>
            <X size={18} />
          </button>
        </header>
        <div className="doc-viewer-body">
          {loading ? "加载中..." : null}
          {!loading && error ? `加载失败: ${error}` : null}
          {!loading && !error && mode === "text" ? content : null}
          {!loading && !error && mode === "pdf" ? <iframe title={source || "pdf"} className="doc-pdf-frame" src={fileUrl} /> : null}
        </div>
      </ModalSurface>
    </section>
  );
}
