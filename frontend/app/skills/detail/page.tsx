"use client";

import { useEffect, useState, type KeyboardEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Download, ArrowLeft } from "lucide-react";
import { activateSkill, getSkillWorkspace, getWorkspaceSkills, saveSkillToken, skillDownloadUrl, skillPath, skillRequest, skillToken, type SkillActivation, type SkillPackage, type SkillVersion } from "../../lib/skills-api";
import { SkillUpload } from "../SkillUpload";

const missing = "未提供";
const textValue = (value: unknown, fallback = missing) => typeof value === "string" && value.trim() ? value : fallback;
const detailTabs = ["说明", "原文", "文件", "版本"];

export default function SkillDetail() {
  const router = useRouter();
  const [id, setId] = useState(""); const [detail, setDetail] = useState<SkillPackage | null>(null); const [version, setVersion] = useState<SkillVersion | null>(null);
  const [workspace, setWorkspace] = useState<{ id: string; name: string } | null>(null); const [activation, setActivation] = useState<SkillActivation | null>(null);
  const [error, setError] = useState(""); const [loading, setLoading] = useState(true); const [busy, setBusy] = useState(false); const [upload, setUpload] = useState(false); const [revision, setRevision] = useState(0);
  const [tab, setTab] = useState("说明"); const [path, setPath] = useState("SKILL.md"); const [fileContent, setFileContent] = useState(""); const [fileLoading, setFileLoading] = useState(false);
  const [token, setToken] = useState(""); const [settings, setSettings] = useState(false); const [notice, setNotice] = useState("");
  const [returnTo, setReturnTo] = useState("/skills");
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const nextId = params.get("id") || "";
    const from = params.get("from") || window.sessionStorage.getItem("bee:skills-return") || "";
    setId(nextId);
    setToken(skillToken());
    setReturnTo(from.startsWith("/skills") ? from : "/skills");
    if (!nextId) {
      setLoading(false);
      setError("缺少技能 ID。");
    }
  }, []);
  useEffect(() => {
    if (!id) return; let active = true; setLoading(true); setError("");
    skillRequest<SkillPackage>(skillPath(id), {}, skillToken()).then(data => { if (active) { setDetail(data); setVersion(current => data.versions.find(v => v.version === current?.version) || data.versions.find(v => v.status === "published") || data.versions[0] || null); } }).catch(e => { if (active) setError(e.message); }).finally(() => { if (active) setLoading(false); });
    getSkillWorkspace().then(async ws => { const data = await getWorkspaceSkills(ws.id); if (active) { setWorkspace(ws); setActivation(data.items.find(item => item.skill_id === id) || null); } }).catch(e => { if (active) setNotice(`无法读取工作空间技能：${e.message}`); });
    return () => { active = false; };
  }, [id, revision]);
  useEffect(() => {
    if (tab !== "文件" || !version) return; let active = true; setFileLoading(true); setFileContent("");
    skillRequest<{ content?: string; text?: string; previewable?: boolean }>(`${skillPath(id, version.version)}/files?path=${encodeURIComponent(path)}`, {}, skillToken()).then(data => {
      if (!active) return;
      const text = data.content ?? data.text;
      setFileContent(data.previewable === false || text === undefined ? "此文件不支持文本预览，请下载完整技能包查看。" : text);
    }).catch(e => { if (active) setFileContent(e.message); }).finally(() => { if (active) setFileLoading(false); });
    return () => { active = false; };
  }, [tab, path, version, id]);
  async function changeActivation() {
    if (!workspace || !version) return; setBusy(true); setError("");
    try { const enabled = !(activation?.enabled && activation.version === version.version); await activateSkill(workspace.id, id, version.version, enabled); setNotice(enabled ? `已为 ${workspace.name} 启用 v${version.version}` : "已停用技能"); setRevision(v => v + 1); }
    catch (e) { setError(e instanceof Error ? e.message : "更新失败"); } finally { setBusy(false); }
  }
  async function changeStatus(item: SkillVersion) {
    setBusy(true); setError("");
    try { await skillRequest(skillPath(id, item.version), { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: item.status === "published" ? "yanked" : "published" }) }, skillToken()); setRevision(v => v + 1); }
    catch (e) { setError(e instanceof Error ? e.message : "更新失败"); } finally { setBusy(false); }
  }
  const activeVersion = !!activation?.enabled && activation.version === version?.version;
  const meta = version?.metadata || {};
  const source = textValue(meta.source_url, "");
  const canToggleActivation = !!workspace && !!token && (version?.status === "published" || activeVersion);
  function selectTab(next: string) {
    setTab(next);
    window.requestAnimationFrame(() => document.getElementById(`skill-tab-${next}`)?.focus());
  }
  function handleTabKey(event: KeyboardEvent<HTMLDivElement>) {
    const index = detailTabs.indexOf(tab);
    const last = detailTabs.length - 1;
    const next = event.key === "ArrowRight" ? detailTabs[(index + 1) % detailTabs.length] : event.key === "ArrowLeft" ? detailTabs[(index + last) % detailTabs.length] : event.key === "Home" ? detailTabs[0] : event.key === "End" ? detailTabs[last] : "";
    if (next) { event.preventDefault(); selectTab(next); }
  }
  return <main className="skill-page">
    <button className="skill-back" onClick={() => router.push(returnTo)}><ArrowLeft size={16} />返回技能库</button>
    {error && <p className="skill-error" role="alert">{error}<button onClick={() => setRevision(v => v + 1)}>重试</button></p>}
    {loading ? <p role="status">正在加载技能详情…</p> : detail && version ? <>
      <header className="skill-heading"><div><h1>{detail.name}</h1><p>{textValue(meta.description, detail.description)}</p></div><div className="skill-actions">{version.status === "published" && <a className="skill-primary" href={skillDownloadUrl(id, version.version)} download><Download size={16} />下载技能</a>}</div></header>
      <div className="skill-detail-layout"><section className="skill-reading">
        <div className="skill-tabs" role="tablist" aria-label="技能详情" onKeyDown={handleTabKey}>{detailTabs.map(item => <button key={item} id={`skill-tab-${item}`} role="tab" aria-controls="skill-tab-content" aria-selected={tab === item} tabIndex={tab === item ? 0 : -1} onClick={() => setTab(item)}>{item}</button>)}</div>
        <div role="tabpanel" id="skill-tab-content" aria-labelledby={`skill-tab-${tab}`}>
          {tab === "说明" && <article className="skill-prose"><ReactMarkdown skipHtml remarkPlugins={[remarkGfm]}>{version.markdown.replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, "")}</ReactMarkdown></article>}
          {tab === "原文" && <pre className="skill-source">{version.markdown}</pre>}
          {tab === "文件" && <div className="skill-file-layout"><nav aria-label="技能文件">{version.files.map(file => <button key={file.path} aria-current={file.path === path ? "true" : undefined} onClick={() => setPath(file.path)}>{file.path}<small>{file.size.toLocaleString()} B</small></button>)}</nav><pre className="skill-source">{fileLoading ? "正在加载文件…" : fileContent}</pre></div>}
          {tab === "版本" && <div className="skill-version-list">{detail.versions.map(item => <div key={item.version}><div><strong>v{item.version}</strong><span>{item.status === "published" ? "已发布" : "已下架"}</span></div><div className="skill-actions"><button onClick={() => { setVersion(item); setPath("SKILL.md"); setTab("说明"); }}>查看</button>{detail.can_manage && <button disabled={busy} onClick={() => void changeStatus(item)}>{item.status === "published" ? "下架" : "恢复"}</button>}</div></div>)}</div>}
        </div>
      </section><aside className="skill-info">
        <label>查看版本<select value={version.version} onChange={e => { setVersion(detail.versions.find(v => v.version === e.target.value) || version); setPath("SKILL.md"); }}>{detail.versions.map(v => <option key={v.version} value={v.version}>{v.version}{v.status === "yanked" ? "（已下架）" : ""}</option>)}</select></label>
        <dl><dt>作者</dt><dd>{textValue(meta.author)}</dd><dt>发布方</dt><dd>{detail.owner}</dd><dt>分类</dt><dd>{textValue(meta.category, "未分类")}</dd><dt>许可证</dt><dd>{textValue(meta.license)}</dd><dt>来源</dt><dd>{/^https?:\/\//i.test(source) ? <a href={source} target="_blank" rel="noreferrer">查看来源</a> : missing}</dd><dt>文件</dt><dd>{version.files.length} 个文件</dd></dl>
        <section className="skill-activation"><p className="skill-sandbox-note">脚本技能只会在服务端配置的安全沙箱中执行；未配置沙箱时，聊天仍只读取技能说明。</p><h2>在 Bee 中使用</h2><p>启用后可在聊天输入区选择。聊天读取技能说明，脚本和附属文件供下载使用。</p><p>{workspace?.name || "正在读取工作空间…"}</p>{activation?.enabled && <p>已启用 v{activation.version}{activation.reason ? ` · ${activation.reason}` : ""}</p>}<button className="skill-primary" disabled={busy || !canToggleActivation} onClick={() => void changeActivation()}>{activeVersion ? "停用技能" : activation?.enabled ? `切换到 v${version.version}` : "在工作空间启用"}</button>{!token && <small>需要管理员令牌才能启用或停用。</small>}<Link href="/chat">前往对话</Link></section>
        <button onClick={() => setSettings(v => !v)} aria-expanded={settings}>管理权限</button>
        {settings && <label>发布 / 管理员令牌<input type="password" autoComplete="off" value={token} onChange={e => { setToken(e.target.value); saveSkillToken(e.target.value); }} onBlur={() => setRevision(v => v + 1)} /><small>发布与版本管理需发布方令牌；工作空间启用需管理员令牌。</small></label>}
        {detail.can_manage && <button onClick={() => setUpload(true)}>发布新版本</button>}
      </aside></div>
      {notice && <p className="skill-notice" role="status">{notice}</p>}
    </> : !error && !loading ? <p>未找到技能。<Link href="/skills">返回技能库</Link></p> : null}
    {upload && <SkillUpload id={id} onClose={() => setUpload(false)} onPublished={() => { setUpload(false); setRevision(v => v + 1); }} />}
  </main>;
}
