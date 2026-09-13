"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  API_BASE,
  listMarketplacePackages,
  publishMarketplaceVersion,
  validateMarketplaceBundle,
} from "../lib/api";
import type { MarketplacePackage, MarketplaceValidationResult } from "../lib/types";

const TOKEN_STORAGE_KEY = "bee-marketplace-token";
const OWNER_STORAGE_KEY = "bee-marketplace-owner";

const UI = {
  title: "\u63d2\u4ef6\u5e02\u573a",
  subtitle: "\u4e3a\u667a\u80fd\u7f16\u7801\u5de5\u5177\u63d0\u4f9b\u63d2\u4ef6\u4e0a\u4f20\u3001\u76ee\u5f55\u53d1\u5e03\u4e0e\u5ba2\u6237\u7aef\u5206\u53d1",
  refresh: "\u5237\u65b0",
  copyMarketAddress: "\u590d\u5236\u5e02\u573a\u5730\u5740",
  search: "\u641c\u7d22\u63d2\u4ef6\u3001\u53d1\u5e03\u65b9\u3001\u5206\u7c7b",
  allOwners: "\u5168\u90e8\u53d1\u5e03\u65b9",
  allCategories: "\u5168\u90e8\u5206\u7c7b",
  noDescription: "\u65e0\u63cf\u8ff0",
  none: "\u6682\u65e0\u80fd\u529b",
  publish: "\u53d1\u5e03",
  uploadZip: "\u4e0a\u4f20\u63d2\u4ef6 ZIP",
  uploadButton: "\u4e0a\u4f20\u63d2\u4ef6",
  replaceZip: "\u91cd\u65b0\u9009\u62e9",
  token: "\u53d1\u5e03\u4ee4\u724c",
  chooseZipHint: "\u652f\u6301 CodeBuddy / Codex / Qoder / Qder / \u901a\u7528 plugin.json \u6e05\u5355",
  validating: "\u6821\u9a8c\u4e2d...",
  manifestPreview: "\u6e05\u5355\u9884\u89c8",
  advanced: "\u9ad8\u7ea7\u8bbe\u7f6e",
  advancedClose: "\u6536\u8d77\u8bbe\u7f6e",
  versionOverride: "\u7248\u672c\u8986\u76d6",
  accessSettings: "\u53d1\u5e03\u6743\u9650\u8bbe\u7f6e",
  publisher: "\u53d1\u5e03\u65b9",
  publisherRequired: "\u8bf7\u5148\u8bbe\u7f6e\u53d1\u5e03\u65b9",
  fileUnit: "\u4e2a\u6587\u4ef6",
  configured: "\u5df2\u914d\u7f6e",
  notConfigured: "\u672a\u914d\u7f6e",
  publishToMarket: "\u53d1\u5e03\u5230\u5e02\u573a",
  publishing: "\u53d1\u5e03\u4e2d...",
  emptyTitle: "\u6682\u65e0\u63d2\u4ef6\u5305",
  emptyText: "\u4e0a\u4f20\u7b26\u5408\u63d2\u4ef6\u7ed3\u6784\u7684 ZIP \u540e\uff0c\u4f1a\u81ea\u52a8\u751f\u6210\u5e02\u573a\u76ee\u5f55\u548c\u5ba2\u6237\u7aef\u6e90\u3002",
};

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

function componentBadgesFromComponents(raw: Record<string, unknown> | undefined): Array<{ key: string; label: string }> {
  const source = raw || {};
  const badges: Array<{ key: string; label: string }> = [];
  const count = (key: string) => (Array.isArray(source[key]) ? (source[key] as unknown[]).length : 0);
  if (count("commands")) badges.push({ key: "commands", label: `\u547d\u4ee4 ${count("commands")}` });
  if (count("skills")) badges.push({ key: "skills", label: `\u6280\u80fd ${count("skills")}` });
  if (count("agents")) badges.push({ key: "agents", label: `Agent ${count("agents")}` });
  if (count("rules")) badges.push({ key: "rules", label: `Rules ${count("rules")}` });
  if (source.mcp === true) badges.push({ key: "mcp", label: "MCP" });
  if (source.hooks === true) badges.push({ key: "hooks", label: "Hooks" });
  if (source.lsp === true) badges.push({ key: "lsp", label: "LSP" });
  if (count("bin")) badges.push({ key: "bin", label: `Bin ${count("bin")}` });
  return badges;
}

export default function PluginsPage() {
  const router = useRouter();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [packages, setPackages] = useState<MarketplacePackage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [token, setToken] = useState("");
  const [search, setSearch] = useState("");
  const [ownerFilter, setOwnerFilter] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [publishOwner, setPublishOwner] = useState("");
  const [publishVersion, setPublishVersion] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [validation, setValidation] = useState<MarketplaceValidationResult | null>(null);
  const [validating, setValidating] = useState(false);
  const [publishing, setPublishing] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const storedToken = window.localStorage.getItem(TOKEN_STORAGE_KEY);
    const storedOwner = window.localStorage.getItem(OWNER_STORAGE_KEY);
    if (storedToken) setToken(storedToken);
    setPublishOwner(storedOwner || "bee");
  }, []);

  const saveToken = useCallback((next: string) => {
    setToken(next);
    if (typeof window !== "undefined") {
      if (next) window.localStorage.setItem(TOKEN_STORAGE_KEY, next);
      else window.localStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  }, []);

  const saveOwner = useCallback((next: string) => {
    setPublishOwner(next);
    if (typeof window !== "undefined") {
      if (next) window.localStorage.setItem(OWNER_STORAGE_KEY, next);
      else window.localStorage.removeItem(OWNER_STORAGE_KEY);
    }
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await listMarketplacePackages({ token: token || null });
      setPackages((data.items || []).filter((pkg) => pkg.visibility !== "private"));
    } catch (err) {
      setError(err instanceof Error ? err.message : "\u52a0\u8f7d\u63d2\u4ef6\u5e02\u573a\u5931\u8d25");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const filtered = useMemo(() => {
    return packages.filter((pkg) => {
      if (ownerFilter && pkg.owner !== ownerFilter) return false;
      if (categoryFilter && pkg.category !== categoryFilter) return false;
      if (search) {
        const needle = search.toLowerCase();
        const haystack = `${pkg.name} ${pkg.description} ${pkg.owner} ${pkg.category}`.toLowerCase();
        if (!haystack.includes(needle)) return false;
      }
      return true;
    });
  }, [packages, ownerFilter, categoryFilter, search]);

  const owners = useMemo(() => Array.from(new Set(packages.map((pkg) => pkg.owner))).sort(), [packages]);
  const categories = useMemo(() => Array.from(new Set(packages.map((pkg) => pkg.category).filter(Boolean))).sort(), [packages]);
  const marketplaceUrl = `${API_BASE}/marketplace/marketplace.json`;

  const validateFile = async (nextFile: File) => {
    setFile(nextFile);
    setValidation(null);
    setValidating(true);
    setMessage("");
    try {
      const result = await validateMarketplaceBundle(nextFile);
      setValidation(result);
      setMessage(result.valid ? "\u63d2\u4ef6\u5305\u6821\u9a8c\u901a\u8fc7" : `\u6821\u9a8c\u5931\u8d25\uff1a${result.error}`);
    } catch (err) {
      setValidation(null);
      setMessage(err instanceof Error ? err.message : "\u6821\u9a8c\u8bf7\u6c42\u5931\u8d25");
    } finally {
      setValidating(false);
    }
  };

  const handlePublish = async () => {
    if (!file) {
      setMessage("\u8bf7\u5148\u4e0a\u4f20\u63d2\u4ef6 ZIP");
      return;
    }
    if (!validation?.valid) {
      setMessage("\u63d2\u4ef6 ZIP \u6821\u9a8c\u901a\u8fc7\u540e\u624d\u80fd\u53d1\u5e03");
      return;
    }
    if (!publishOwner) {
      setMessage(UI.publisherRequired);
      return;
    }
    if (!token) {
      setMessage("\u8bf7\u5148\u5728\u53d1\u5e03\u6743\u9650\u8bbe\u7f6e\u4e2d\u586b\u5199\u53d1\u5e03\u4ee4\u724c");
      return;
    }
    const packageName = validation.name;
    if (!packageName) {
      setMessage("plugin.json \u7f3a\u5c11 name");
      return;
    }
    setPublishing(true);
    setMessage("");
    try {
      const record = await publishMarketplaceVersion(publishOwner, packageName, file, {
        version: publishVersion || undefined,
        visibility: "public",
        token,
      });
      setMessage(`\u5df2\u53d1\u5e03\uff1a${packageName}@${record.version}`);
      setFile(null);
      setValidation(null);
      setPublishVersion("");
      if (fileInputRef.current) fileInputRef.current.value = "";
      await refresh();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "\u53d1\u5e03\u5931\u8d25");
    } finally {
      setPublishing(false);
    }
  };

  const copySource = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setMessage("");
    } catch {
      setMessage(value);
    }
  };

  const openPlugin = (pkg: MarketplacePackage) => {
    router.push(`/plugins/detail?owner=${encodeURIComponent(pkg.owner)}&name=${encodeURIComponent(pkg.name)}`);
  };

  return (
    <main className="plugins-page mkt-console">
      <header className="mkt-topbar">
        <div>
          <h1>{UI.title}</h1>
          <p>{UI.subtitle}</p>
        </div>
        <div className="mkt-topbar-actions">
          <button type="button" className="plugins-refresh" onClick={() => void copySource(marketplaceUrl)}>{UI.copyMarketAddress}</button>
          <button type="button" className="plugins-refresh" onClick={() => void refresh()}>{UI.refresh}</button>
        </div>
      </header>

      {error ? <div className="plugins-error">{error}</div> : null}
      {message ? <div className="plugins-message">{message}</div> : null}

      <section className="plugins-layout mkt-layout">
        <section className="plugins-catalog mkt-main" aria-label="marketplace packages">
          <div className="plugins-filter mkt-filterbar">
            <input value={search} placeholder={UI.search} onChange={(event) => setSearch(event.target.value)} />
            <select value={ownerFilter} onChange={(event) => setOwnerFilter(event.target.value)}>
              <option value="">{UI.allOwners}</option>
              {owners.map((owner) => <option key={owner} value={owner}>{owner}</option>)}
            </select>
            <select value={categoryFilter} onChange={(event) => setCategoryFilter(event.target.value)}>
              <option value="">{UI.allCategories}</option>
              {categories.map((category) => <option key={category} value={category}>{category}</option>)}
            </select>
          </div>

          {loading ? (
            <div className="plugins-loading">Loading...</div>
          ) : filtered.length === 0 ? (
            <div className="plugins-empty mkt-empty"><h2>{UI.emptyTitle}</h2><p>{UI.emptyText}</p></div>
          ) : (
            <div className="mkt-card-grid" aria-label="packages">
              {filtered.map((pkg) => {
                const badges = componentBadgesFromComponents(pkg.components);
                const versionLabel = pkg.latest_version ? `v${pkg.latest_version}` : "\u672a\u53d1\u5e03\u7248\u672c";
                return (
                  <button key={`${pkg.owner}/${pkg.name}`} type="button" className="mkt-card" onClick={() => openPlugin(pkg)}>
                    <strong>{pkg.name}</strong>
                    <span className="mkt-card-meta-row"><span>@{pkg.owner}</span>{pkg.category ? <span>{pkg.category}</span> : null}<span>{versionLabel}</span></span>
                    <small>{pkg.description || UI.noDescription}</small>
                    <span className="mkt-card-capabilities">
                      {badges.length ? badges.map((badge) => <em key={badge.key} className={`mkt-badge mkt-badge-${badge.key}`}>{badge.label}</em>) : <em className="mkt-muted">{UI.none}</em>}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </section>

        <aside className="mkt-publish-panel" aria-label="publish plugin">
          <div className="mkt-panel-head"><span>{UI.publish}</span><h2>{UI.uploadZip}</h2></div>
          <div className="mkt-publish-state">
            <span>{UI.accessSettings}</span>
            <b>{token && publishOwner ? UI.configured : UI.notConfigured}</b>
            <button type="button" onClick={() => setSettingsOpen((open) => !open)}>{settingsOpen ? UI.advancedClose : UI.accessSettings}</button>
          </div>
          {settingsOpen ? (
            <div className="mkt-settings-block">
              <label className="mkt-field"><span>{UI.token}</span><input type="password" value={token} placeholder="publish/admin token" onChange={(event) => saveToken(event.target.value)} /></label>
              <label className="mkt-field"><span>{UI.publisher}</span><input value={publishOwner} placeholder="bee" onChange={(event) => saveOwner(event.target.value)} /></label>
            </div>
          ) : null}
          <input ref={fileInputRef} type="file" accept=".zip" className="mkt-file-input" onChange={(event) => { const nextFile = event.target.files?.[0] ?? null; if (nextFile) void validateFile(nextFile); }} />
          <button type="button" className="mkt-secondary-wide" disabled={validating} onClick={() => fileInputRef.current?.click()}>{validating ? UI.validating : file ? UI.replaceZip : UI.uploadButton}</button>
          <p className="mkt-upload-note">{file ? `${file.name} · ${formatBytes(file.size)}` : UI.chooseZipHint}</p>
          {validation ? validation.valid ? (
            <div className="mkt-preview">
              <span>{UI.manifestPreview}</span>
              <strong>{validation.name}@{publishVersion || validation.version || "-"}</strong>
              <p>{validation.description || UI.noDescription}</p>
              <div className="mkt-badges">{componentBadgesFromComponents(validation.components).map((badge) => <span key={badge.key} className={`mkt-badge mkt-badge-${badge.key}`}>{badge.label}</span>)}</div>
              <small>{validation.file_count} {UI.fileUnit} &middot; {formatBytes(validation.total_uncompressed_bytes)}</small>
            </div>
          ) : <div className="mkt-preview mkt-preview-error">{validation.error}</div> : null}
          <button type="button" className="mkt-advanced-toggle" onClick={() => setAdvancedOpen((open) => !open)}>{advancedOpen ? UI.advancedClose : UI.advanced}</button>
          {advancedOpen ? <label className="mkt-field"><span>{UI.versionOverride}</span><input value={publishVersion} placeholder="manifest.version" onChange={(event) => setPublishVersion(event.target.value)} /></label> : null}
          <button type="button" className="mkt-primary-wide" disabled={!file || !validation?.valid || publishing} onClick={() => void handlePublish()}>{publishing ? UI.publishing : UI.publishToMarket}</button>
        </aside>
      </section>
    </main>
  );
}