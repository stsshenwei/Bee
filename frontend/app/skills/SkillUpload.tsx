"use client";

import { useRef, useState } from "react";
import { ModalSurface } from "../components/ModalSurface";
import { saveSkillToken, skillPath, skillRequest, skillToken, type SkillMetadata, type SkillValidation } from "../lib/skills-api";

export function SkillUpload({ id, onClose, onPublished }: { id?: string; onClose: () => void; onPublished: () => void }) {
  const [token, setToken] = useState(skillToken);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<SkillValidation | null>(null);
  const [metadata, setMetadata] = useState<SkillMetadata>({ owner: "", version: "1.0.0", author: "", category: "", tags: [], license: "", source_url: "" });
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const sequence = useRef(0);
  async function validate(next: File) {
    const seq = ++sequence.current;
    setFile(next); setPreview(null); setError(""); setBusy("正在校验技能包…");
    const form = new FormData(); form.append("file", next);
    try {
      const data = await skillRequest<SkillValidation>("/skills/validate", { method: "POST", body: form }, token);
      if (seq !== sequence.current) return;
      if (!data.valid) throw new Error(data.error || "技能包校验失败");
      setPreview(data);
      const m = data.parsed_metadata;
      setMetadata(old => ({ ...old, version: String(m.version || old.version), author: String(m.author || old.author), category: String(m.category || old.category), license: String(m.license || old.license), source_url: String(m.source_url || old.source_url), tags: Array.isArray(m.tags) ? m.tags.map(String) : old.tags }));
    } catch (e) { if (seq === sequence.current) setError(e instanceof Error ? e.message : "校验失败"); }
    finally { if (seq === sequence.current) setBusy(""); }
  }
  async function publish() {
    if (!file || !preview) return;
    setBusy("正在发布…"); setError("");
    const form = new FormData(); form.append("file", file); form.append("metadata", JSON.stringify(metadata));
    try { await skillRequest(id ? `${skillPath(id)}/versions` : "/skills", { method: "POST", body: form }, token); saveSkillToken(token); onPublished(); }
    catch (e) { setError(e instanceof Error ? e.message : "发布失败"); }
    finally { setBusy(""); }
  }
  return <div className="skill-modal-backdrop"><ModalSurface className="skill-upload-dialog" role="dialog" aria-modal="true" aria-labelledby="skill-upload-title" onClose={onClose} dismissible={!busy}>
    <header className="skill-heading"><h2 id="skill-upload-title">{id ? "发布新版本" : "上传技能"}</h2><button type="button" onClick={onClose} disabled={!!busy}>关闭</button></header>
    <p>上传 SKILL.md 或包含一个技能的 ZIP。附属脚本和资源会完整保留供下载。</p>
    <label>发布令牌<input type="password" autoComplete="off" value={token} onChange={e => { setToken(e.target.value); saveSkillToken(e.target.value); }} disabled={!!busy} placeholder="与插件市场共用发布令牌" /></label>
    <label>技能文件<input type="file" accept=".md,.zip" disabled={!!busy || !token} onChange={e => { if (e.target.files?.[0]) void validate(e.target.files[0]); }} /></label>
    {!token && <p className="skill-muted">填写发布令牌后可选择文件。</p>}
    {preview && <><section className="skill-upload-preview"><strong>{String(preview.parsed_metadata.name)}</strong><p>{String(preview.parsed_metadata.description)}</p><small>{preview.file_index.length} 个文件 · {file?.name}</small></section>
    <div className="skill-form-grid">
      {([ ["owner", "发布方（留空使用令牌身份）"], ["version", "版本"], ["author", "作者"], ["category", "分类"], ["license", "许可证"], ["source_url", "来源链接"] ] as const).map(([key, label]) => <label key={key}>{label}<input value={metadata[key]} disabled={!!busy} onChange={e => setMetadata(old => ({ ...old, [key]: e.target.value }))} /></label>)}
      <label>标签（逗号分隔）<input value={metadata.tags.join(",")} disabled={!!busy} onChange={e => setMetadata(old => ({ ...old, tags: e.target.value.split(",") }))} /></label>
    </div></>}
    {error && <p role="alert" className="skill-error">{error}</p>}
    <footer className="skill-actions"><span role="status">{busy}</span><button type="button" className="skill-primary" disabled={!preview || !!busy || !token} onClick={() => void publish()}>发布技能</button></footer>
  </ModalSurface></div>;
}
