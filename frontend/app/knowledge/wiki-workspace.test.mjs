import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const workspace = readFileSync(new URL("./WikiWorkspace.tsx", import.meta.url), "utf8");
const page = readFileSync(new URL("./page.tsx", import.meta.url), "utf8");
const css = readFileSync(new URL("../globals.css", import.meta.url), "utf8");

test("Wiki workspace shell keeps reference navigation and independent scrolling", () => {
  assert.match(page, /Documents|文档/);
  assert.match(page, /Wiki/);
  assert.match(page, /Graph|图谱/);
  assert.match(workspace, />索引</);
  assert.match(workspace, />日志</);
  assert.match(css, /\.wiki-navigation[\s\S]*overflow-y:\s*auto/);
  assert.match(css, /\.wiki-reader-v2[\s\S]*overflow-y:\s*auto/);
});

test("Wiki navigation preserves bounded search, counts, views, folders, and log pagination", () => {
  assert.match(workspace, /listWikiPages\(selected\.id, \{ q: query, status: "published", limit: 100 \}\)/);
  assert.match(workspace, /page_counts\.summary/);
  assert.match(workspace, /navigationMode === "list" \|\| query\.trim\(\)/);
  assert.match(workspace, /function FolderTree/);
  assert.match(workspace, /updateWikiFolder/);
  assert.match(workspace, /moveWikiPage/);
  assert.match(workspace, /loadMoreLogs/);
});

test("Wiki status, source, graph drawer, and repair flows remain wired", () => {
  assert.match(workspace, /ACTIVE_TASK_STATES/);
  assert.match(workspace, /activeGenerationTasks/);
  assert.match(workspace, /activeWikiTaskCount/);
  assert.match(workspace, /readWikiSourceDoc/);
  assert.match(workspace, /ZoomIn/);
  assert.match(workspace, /Maximize2/);
  assert.match(workspace, /setDrawer\("details"\)/);
  assert.match(workspace, /cleanupWikiIssue/);
  assert.match(workspace, /applyWikiProposal/);
  assert.match(css, /\.wiki-graph-info-tag \.wiki-graph-filters[\s\S]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/);
});

test("Wiki article rendering strips duplicated type headings from markdown", () => {
  assert.match(workspace, /REDUNDANT_WIKI_TYPE_HEADINGS/);
  assert.match(workspace, /stripRedundantWikiTypeHeading\(page\.content_markdown\)/);
  assert.match(workspace, /REDUNDANT_WIKI_TYPE_HEADINGS\.has\(firstContent\)/);
});

test("knowledge catalog exposes guarded knowledge-base deletion", () => {
  assert.match(page, /function deleteKnowledgeBase\(target: KnowledgeBase\)/);
  assert.match(page, /target\.is_default/);
  assert.match(page, /archiveKnowledgeBase\(target\.id\)/);
  assert.match(page, /aria-label="删除知识库"/);
  assert.match(page, />删除知识库<\/button>/);
});

test("knowledge catalog cards show type badges and readable create hover", () => {
  assert.match(page, /kb-card-type-badge/);
  assert.match(page, /item\.is_default \? `默认 · \$\{typeLabel\}` : typeLabel/);
  assert.match(page, /kb-catalog-controls/);
  assert.match(page, /catalog-overview-actions/);
  assert.match(page, /workspace-icon-button kb-refresh-button/);
  assert.match(page, /className="catalog-overview"[\s\S]*className="catalog-overview-actions"[\s\S]*aria-label="搜索知识库"/);
  assert.doesNotMatch(page, />全部知识库</);
  assert.match(page, /knowledgeBaseCardTypeLabel\(item\)/);
  assert.match(css, /\.kb-card \.kb-card-head \.kb-card-title-copy > \.kb-card-type-badge/);
  assert.match(css, /\.kb-catalog-page \.catalog-overview-actions/);
  assert.match(css, /\.kb-catalog-page \.catalog-overview-actions \.kb-refresh-button/);
  assert.match(css, /grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\)/);
  assert.match(css, /\.kb-catalog-page \.kb-create-button:hover/);
  assert.match(css, /color:\s*var\(--color-accent-ink\)/);
});
