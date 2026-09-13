"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  deleteMarketplacePackage,
  downloadMarketplaceVersion,
  getMarketplacePackage,

} from "../../lib/api";
import type { MarketplacePackageDetail, MarketplacePackageVersion } from "../../lib/types";

type ComponentEntry = {
  name: string;
  description?: string;
  path?: string;
  note?: string;
};

const TOKEN_STORAGE_KEY = "bee-marketplace-token";

const UI = {
  back: "\u8fd4\u56de\u63d2\u4ef6\u5e02\u573a",
  source: "\u6765\u6e90",
  market: "\u5e02\u573a",
  noDescription: "\u65e0\u63cf\u8ff0",
  commands: "\u547d\u4ee4",
  commandsHelp: "\u8fd9\u4e9b\u547d\u4ee4\u53ef\u5728\u5ba2\u6237\u7aef\u4e2d\u89e6\u53d1\u5de5\u4f5c\u6d41\u3002",
  skills: "\u6280\u80fd",
  skillsHelp: "Skills \u4f1a\u5728\u4e0e\u5bf9\u8bdd\u76f8\u5173\u65f6\u7531\u667a\u80fd\u4f53\u81ea\u52a8\u8c03\u7528\u3002",
  agents: "Agent",
  agentsHelp: "\u63d2\u4ef6\u5185\u7f6e\u7684\u4e13\u7528 Agent\u3002",
  rules: "Rules",
  rulesHelp: "\u89c4\u5219\u4f1a\u5f71\u54cd\u63d2\u4ef6\u6216\u5ba2\u6237\u7aef\u8fd0\u884c\u65f6\u884c\u4e3a\u3002",
  mcp: "MCP \u670d\u52a1\u5668",
  hooks: "Hooks",
  lsp: "LSP",
  bin: "Bin",
  versions: "\u7248\u672c",
  download: "\u4e0b\u8f7d",
  yank: "\u4e0b\u67b6",
  restore: "\u6062\u590d",
  purge: "\u7269\u7406\u5220\u9664",
  deletePlugin: "\u5220\u9664\u63d2\u4ef6",
  published: "\u5df2\u53d1\u5e03",
  yanked: "\u5df2\u4e0b\u67b6",
  tokenRequired: "\u8bf7\u5148\u5728\u63d2\u4ef6\u5e02\u573a\u9875\u914d\u7f6e\u53d1\u5e03\u4ee4\u724c",
  loadFailed: "\u52a0\u8f7d\u63d2\u4ef6\u8be6\u60c5\u5931\u8d25",
  versionFailed: "\u7248\u672c\u64cd\u4f5c\u5931\u8d25",
  deleteFailed: "\u5220\u9664\u63d2\u4ef6\u5931\u8d25",
};

function arrayComponent(raw: Record<string, unknown> | undefined, key: string): string[] {
  const value = raw?.[key];
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}
function detailComponent(
  raw: Record<string, unknown> | undefined,
  listKey: string,
  detailKey: string,
): ComponentEntry[] {
  const names = arrayComponent(raw, listKey);
  const details = raw?.[detailKey];
  const byName = new Map<string, ComponentEntry>();
  if (Array.isArray(details)) {
    details.forEach((item) => {
      if (!item || typeof item !== "object") return;
      const record = item as Record<string, unknown>;
      const name = String(record.name || "").trim();
      if (!name) return;
      byName.set(name, {
        name,
        description: String(record.description || "").trim(),
        path: String(record.path || "").trim(),
      });
    });
  }
  return names.map((name) => byName.get(name) || { name });
}

function boolComponent(raw: Record<string, unknown> | undefined, key: string): boolean {
  return raw?.[key] === true;
}
function hasComponents(raw: Record<string, unknown> | undefined): boolean {
  if (!raw) return false;
  return ["commands", "skills", "agents", "rules", "bin"].some((key) => arrayComponent(raw, key).length > 0)
    || ["mcp", "hooks", "lsp"].some((key) => boolComponent(raw, key));
}

function formatBytes(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}


export default function PluginDetailPage() {
  const router = useRouter();
  const [owner, setOwner] = useState("");
  const [name, setName] = useState("");
  const [token, setToken] = useState("");
  const [detail, setDetail] = useState<MarketplacePackageDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    setOwner(params.get("owner") || "");
    setName(params.get("name") || "");
    setToken(window.localStorage.getItem(TOKEN_STORAGE_KEY) || "");
  }, []);

  const loadDetail = useCallback(async () => {
    if (!owner || !name) return;
    setLoading(true);
    setError("");
    try {
      const data = await getMarketplacePackage(owner, name, token || null);
      setDetail(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : UI.loadFailed);
    } finally {
      setLoading(false);
    }
  }, [owner, name, token]);

  useEffect(() => {
    void loadDetail();
  }, [loadDetail]);

  const latest = detail?.versions?.[0];
  const components = hasComponents(detail?.components) ? detail?.components || {} : latest?.components || {};
  const sections = useMemo<Array<{ key: string; title: string; help: string; items: ComponentEntry[] }>>(() => [
    { key: "skills", title: UI.skills, help: UI.skillsHelp, items: detailComponent(components, "skills", "skill_details") },
    { key: "commands", title: UI.commands, help: UI.commandsHelp, items: arrayComponent(components, "commands").map((item) => ({ name: item })) },
    { key: "agents", title: UI.agents, help: UI.agentsHelp, items: arrayComponent(components, "agents").map((item) => ({ name: item })) },
    { key: "rules", title: UI.rules, help: UI.rulesHelp, items: arrayComponent(components, "rules").map((item) => ({ name: item })) },
    { key: "bin", title: UI.bin, help: "", items: arrayComponent(components, "bin").map((item) => ({ name: item })) },
  ].filter((section) => section.items.length > 0), [components]);

  const featureCards = useMemo(() => [
    boolComponent(components, "mcp") ? { key: "mcp", title: UI.mcp, name: `${detail?.name || name}-mcp`, note: ".mcp.json / manifest mcpServers" } : null,
    boolComponent(components, "hooks") ? { key: "hooks", title: UI.hooks, name: "hooks", note: "hooks/ or manifest hooks" } : null,
    boolComponent(components, "lsp") ? { key: "lsp", title: UI.lsp, name: "lsp", note: ".lsp.json / manifest lsp" } : null,
  ].filter(Boolean) as Array<{ key: string; title: string; name: string; note: string }>, [components, detail?.name, name]);

  const downloadVersion = async (version: MarketplacePackageVersion) => {
    if (!detail) return;
    try {
      await downloadMarketplaceVersion(detail.owner, detail.name, version.version, token || null);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : UI.versionFailed);
    }
  };

  const removePlugin = async () => {
    if (!detail) return;
    if (!token) {
      setMessage(UI.tokenRequired);
      return;
    }
    if (!window.confirm(`Delete ${detail.owner}/${detail.name} and all versions?`)) return;
    try {
      await deleteMarketplacePackage(detail.owner, detail.name, token);
      router.push("/plugins");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : UI.deleteFailed);
    }
  };

  return (
    <main className="plugins-page mkt-console mkt-detail-page">
      <header className="mkt-detail-hero">
        <div className="mkt-detail-headline">
          <div className="mkt-detail-title-row">
            <button type="button" className="mkt-back-icon" aria-label={UI.back} title={UI.back} onClick={() => router.push("/plugins")}>&larr;</button>
            {detail ? <h1>{detail.name}</h1> : null}
          </div>
          {detail ? (
            <>
              <p>{detail.description || UI.noDescription}</p>
              <div className="mkt-badges">
                {detail.category ? <em className="mkt-badge">{detail.category}</em> : null}
                {detail.latest_version ? <em className="mkt-badge">v{detail.latest_version}</em> : null}
                {(detail.keywords || []).map((keyword) => <em key={keyword} className="mkt-badge">{keyword}</em>)}
              </div>
            </>
          ) : null}
        </div>
      </header>

      {error ? <div className="plugins-error">{error}</div> : null}
      {message ? <div className="plugins-message">{message}</div> : null}
      {loading ? <div className="plugins-loading">Loading...</div> : null}

      {detail ? (
        <section className="mkt-detail-layout">
          <div className="mkt-detail-main">
            {sections.map((section) => (
              <section key={section.key} className="mkt-component-section">
                <header><h2>{section.title}</h2>{section.help ? <p>{section.help}</p> : null}</header>
                <div className="mkt-component-list">
                  {section.items.map((item) => (
                    <article key={item.name} className="mkt-component-item">
                      <div>
                        <strong>{item.name}</strong>
                      </div>
                      {item.description ? <p>{item.description}</p> : null}
                    </article>
                  ))}
                </div>
              </section>
            ))}
            {featureCards.map((feature) => (
              <section key={feature.key} className="mkt-component-section">
                <header><h2>{feature.title}</h2></header>
                <article className="mkt-component-item"><div><strong>{feature.name}</strong><small>{feature.note}</small></div></article>
              </section>
            ))}
          </div>

          <aside className="mkt-detail-side">
            <section className="mkt-detail-card">
              <h2>{UI.versions}</h2>
              {latest ? <p>{formatBytes(latest.size_bytes)} &middot; {latest.content_hash.slice(0, 12)}</p> : null}
              <ul className="mkt-version-list">
                {(detail.versions || []).map((version) => (
                  <li key={version.version} className={version.status === "yanked" ? "yanked" : ""}>
                    <div className="mkt-version-main"><strong>v{version.version}</strong><span className={`mkt-badge mkt-badge-status-${version.status}`}>{version.status === "yanked" ? UI.yanked : UI.published}</span><small>{formatBytes(version.size_bytes)} &middot; {version.published_at || "-"}</small></div>
                  </li>
                ))}
              </ul>
              <div className="mkt-version-footer">
                {latest ? <button type="button" onClick={() => void downloadVersion(latest)}>{UI.download}</button> : null}
                <button type="button" className="mkt-danger" onClick={() => void removePlugin()}>{UI.deletePlugin}</button>
              </div>
            </section>
          </aside>
        </section>
      ) : null}
    </main>
  );
}


