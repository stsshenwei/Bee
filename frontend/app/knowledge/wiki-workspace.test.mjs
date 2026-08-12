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
  assert.match(workspace, /if \(!activeTasks\.length/);
  assert.match(workspace, /readWikiSourceDoc/);
  assert.match(workspace, /ZoomIn/);
  assert.match(workspace, /Maximize2/);
  assert.match(workspace, /setDrawer\("details"\)/);
  assert.match(workspace, /cleanupWikiIssue/);
  assert.match(workspace, /applyWikiProposal/);
});

test("knowledge catalog exposes guarded knowledge-base deletion", () => {
  assert.match(page, /function deleteKnowledgeBase\(target: KnowledgeBase\)/);
  assert.match(page, /target\.is_default/);
  assert.match(page, /archiveKnowledgeBase\(target\.id\)/);
  assert.match(page, /aria-label="删除知识库"/);
  assert.match(page, />删除知识库<\/button>/);
});
