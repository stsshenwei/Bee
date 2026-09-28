"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Plus, Search, RefreshCw } from "lucide-react";
import { skillRequest, type SkillPackage } from "../lib/skills-api";
import { SkillUpload } from "./SkillUpload";

export default function SkillsPage() {
  const [q, setQ] = useState(""); const [category, setCategory] = useState(""); const [author, setAuthor] = useState("");
  const [items, setItems] = useState<SkillPackage[]>([]); const [cursor, setCursor] = useState(""); const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(true); const [error, setError] = useState(""); const [upload, setUpload] = useState(false); const [revision, setRevision] = useState(0); const [ready, setReady] = useState(false);
  useEffect(() => { const params = new URLSearchParams(window.location.search); setQ(params.get("q") || ""); setCategory(params.get("category") || ""); setAuthor(params.get("author") || ""); setCursor(params.get("cursor") || ""); setReady(true); }, []);
  useEffect(() => {
    if (!ready) return;
    let active = true;
    const timer = setTimeout(() => {
      setLoading(true); setError("");
      const params = new URLSearchParams({ q, category, author, cursor: cursor || "0", limit: "24", sort: "updated" });
      window.history.replaceState(null, "", `/skills?${params}`);
      skillRequest<{ items: SkillPackage[]; next_cursor: string | null }>(`/skills?${params}`).then(data => { if (active) { setItems(data.items); setNext(data.next_cursor); } }).catch(e => { if (active) setError(e.message); }).finally(() => { if (active) setLoading(false); });
    }, 200);
    return () => { active = false; clearTimeout(timer); };
  }, [q, category, author, cursor, revision, ready]);
  useEffect(() => { if (!loading && items.length) { const y = Number(sessionStorage.getItem("bee:skills-scroll") || 0); if (y) { window.scrollTo(0, y); sessionStorage.removeItem("bee:skills-scroll"); } } }, [loading, items]);
  return <main className="skill-page">
    <header className="skill-heading"><div><h1>技能库</h1><p>浏览、分享技能，在对话中使用经过选择的技能说明。</p></div><div className="skill-actions"><button onClick={() => setRevision(v => v + 1)} aria-label="刷新技能库"><RefreshCw size={16} />刷新</button><button className="skill-primary" onClick={() => setUpload(true)}><Plus size={16} />上传技能</button></div></header>
    <div className="skill-filters"><label className="skill-search"><Search size={17} /><input aria-label="搜索技能" placeholder="搜索技能名称或描述" value={q} onChange={e => { setQ(e.target.value); setCursor(""); }} /></label><input aria-label="按分类筛选" placeholder="全部分类" value={category} onChange={e => { setCategory(e.target.value); setCursor(""); }} /><input aria-label="按作者筛选" placeholder="全部作者" value={author} onChange={e => { setAuthor(e.target.value); setCursor(""); }} /></div>
    {error ? <div role="alert" className="skill-error">{error}<button onClick={() => setRevision(v => v + 1)}>重试</button></div> : loading ? <p role="status" className="skill-empty">正在加载技能…</p> : items.length ? <div className="skill-grid">{items.map(item => <Link className="skill-card" key={item.id} href={`/skills/detail?id=${encodeURIComponent(item.id)}&from=${encodeURIComponent(`/skills?${new URLSearchParams({ q, category, author, cursor: cursor || "0", limit: "24", sort: "updated" })}`)}`} onClick={() => { sessionStorage.setItem("bee:skills-scroll", String(window.scrollY)); sessionStorage.setItem("bee:skills-return", window.location.pathname + window.location.search); }}><div className="skill-card-title"><h2>{item.name}</h2><span>v{item.latest_version}</span></div><p>{item.description}</p><div className="skill-tags">{item.category && <span>{item.category}</span>}{(item.tags || []).slice(0, 3).map(tag => <span key={tag}>{tag}</span>)}</div><footer><span>{item.author || item.owner}</span><time>{item.updated_at ? new Date(item.updated_at).toLocaleDateString("zh-CN") : ""}</time></footer></Link>)}</div> : <section className="skill-empty"><h2>{q || category || author ? "没有匹配的技能" : "还没有技能"}</h2><p>{q || category || author ? "试试其他关键词，或清除筛选。" : "上传第一个技能，集中保存说明与相关文件。"}</p><button onClick={() => { if (q || category || author) { setQ(""); setCategory(""); setAuthor(""); } else setUpload(true); }}>{q || category || author ? "清除筛选" : "上传技能"}</button></section>}
    <nav className="skill-pagination" aria-label="技能分页">{cursor && <button onClick={() => setCursor("")}>回到第一页</button>}{next && <button disabled={loading} onClick={() => setCursor(next)}>下一页</button>}</nav>
    {upload && <SkillUpload onClose={() => setUpload(false)} onPublished={() => { setUpload(false); setCursor(""); setRevision(v => v + 1); }} />}
  </main>;
}
