"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { BookOpen, X } from "lucide-react";
import { getSkillWorkspace, getWorkspaceSkills, type SkillActivation } from "../lib/skills-api";

export function SkillPicker({ workspaceId, selected, onChange, disabled }: { workspaceId?: string; selected: SkillActivation[]; onChange: (items: SkillActivation[]) => void; disabled: boolean }) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<SkillActivation[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    let active = true; setLoading(true); setError("");
    (workspaceId ? Promise.resolve({ id: workspaceId }) : getSkillWorkspace()).then(ws => getWorkspaceSkills(ws.id))
      .then(data => { if (active) setItems(data.items.filter(item => item.enabled)); })
      .catch(e => { if (active) setError(e.message); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [open, workspaceId]);
  return <div className="skill-picker" onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOpen(false); }} onKeyDown={e => { if (e.key === "Escape") { e.preventDefault(); setOpen(false); triggerRef.current?.focus(); } }}>
    <button ref={triggerRef} type="button" className="composer-mode-trigger" disabled={disabled} aria-expanded={open} aria-controls="chat-skill-options" onClick={() => setOpen(value => !value)}><BookOpen size={16} />技能{selected.length ? ` (${selected.length})` : ""}</button>
    {open && <div className="skill-picker-menu" id="chat-skill-options"><strong>选择本次对话技能</strong><p>加载技能说明，不执行附属脚本。</p>
      {loading ? <p role="status">正在加载…</p> : error ? <p role="alert">{error}</p> : !items.length ? <p>工作空间尚未启用技能。</p> : items.map(item => <label key={item.skill_id}><input type="checkbox" disabled={!item.available || disabled} checked={selected.some(s => s.skill_id === item.skill_id && s.version === item.version)} onChange={e => onChange(e.target.checked ? [...selected.filter(s => s.skill_id !== item.skill_id), item] : selected.filter(s => s.skill_id !== item.skill_id))} /><span>{item.name}<small>v{item.version}{!item.available ? " · 当前不可用，请检查运行时配置或版本状态" : ""}</small></span></label>)}
      {selected.length > 0 && <div className="skill-selected-list">{selected.map(item => <button type="button" key={item.skill_id} onClick={() => onChange(selected.filter(s => s.skill_id !== item.skill_id))}>{item.name} v{item.version}<X size={13} /><span className="sr-only">移除</span></button>)}</div>}
      <Link href="/skills">管理技能库</Link></div>}
  </div>;
}
