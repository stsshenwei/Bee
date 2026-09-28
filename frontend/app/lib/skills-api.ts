import { API_BASE } from "./api";

export type SkillRef = { skill_id: string; version: string };
export type LoadedSkill = SkillRef & { name: string; sha256: string };
export type SkillFile = { path: string; size: number; previewable?: boolean };
export type SkillVersion = { version: string; status: "published" | "yanked"; markdown: string; files: SkillFile[]; sha256: string; created_at: string; metadata?: Record<string, unknown> };
export type SkillPackage = { id: string; name: string; owner: string; description: string; author: string; category: string; tags: string[]; latest_version: string; updated_at: string; license?: string; source_url?: string; versions: SkillVersion[]; can_manage?: boolean };
export type SkillActivation = SkillRef & { name: string; enabled: boolean; available: boolean; reason?: string };
export type SkillMetadata = { owner: string; version: string; author: string; category: string; tags: string[]; license: string; source_url: string };
export type SkillValidation = { valid: boolean; parsed_metadata: Record<string, unknown>; file_index: SkillFile[]; error?: string };

export function skillToken() { return typeof window === "undefined" ? "" : window.sessionStorage.getItem("bee-skills-token") || window.localStorage.getItem("bee-marketplace-token") || ""; }
export function saveSkillToken(value: string) { window.sessionStorage.setItem("bee-skills-token", value); }
export async function skillRequest<T>(path: string, init: RequestInit = {}, token = ""): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
  const data = await res.json();
  if (!res.ok) {
    const detail = data.detail || data;
    throw new Error(typeof detail === "string" ? detail : detail.message || `请求失败 (${res.status})`);
  }
  return data as T;
}
export const skillPath = (id: string, version?: string) => `/skills/${encodeURIComponent(id)}${version ? `/versions/${encodeURIComponent(version)}` : ""}`;
export const skillDownloadUrl = (id: string, version: string) => `${API_BASE}${skillPath(id, version)}/download`;
export function getSkillWorkspace() { return skillRequest<{ id: string; name: string }>("/workspaces/default"); }
export function getWorkspaceSkills(id: string) { return skillRequest<{ items: SkillActivation[]; reason?: string }>(`/workspaces/${encodeURIComponent(id)}/skills`); }
export function activateSkill(workspace: string, id: string, version: string, enabled: boolean) {
  return skillRequest(`/workspaces/${encodeURIComponent(workspace)}/skills/${encodeURIComponent(id)}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ version, enabled }) }, skillToken());
}
