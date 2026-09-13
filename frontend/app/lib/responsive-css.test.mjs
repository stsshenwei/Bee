import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const css = readFileSync(new URL("../globals.css", import.meta.url), "utf8");
const chatPage = readFileSync(new URL("../chat/page.tsx", import.meta.url), "utf8");
const knowledgePage = readFileSync(new URL("../knowledge/page.tsx", import.meta.url), "utf8");
const knowledgeDocumentPage = readFileSync(new URL("../knowledge/document/page.tsx", import.meta.url), "utf8");
const pluginsPage = readFileSync(new URL("../plugins/page.tsx", import.meta.url), "utf8");
const pluginDetailPage = readFileSync(new URL("../plugins/detail/page.tsx", import.meta.url), "utf8");
const sidebar = readFileSync(new URL("../components/Sidebar.tsx", import.meta.url), "utf8");

test("plugins workspace exposes marketplace console and responsive rules", () => {
  assert.match(sidebar, /router\.push\("\/plugins"\)/);
  assert.match(sidebar, /pathname\.startsWith\("\/plugins"\)/);
  assert.match(pluginsPage, /listMarketplacePackages/);
  assert.match(pluginsPage, /validateMarketplaceBundle/);
  assert.match(pluginsPage, /publishMarketplaceVersion/);
  assert.match(pluginDetailPage, /downloadMarketplaceVersion/);
  assert.match(pluginDetailPage, /mkt-back-icon/);
  assert.match(pluginsPage, /plugins-filter/);
  assert.match(pluginsPage, /mkt-publish-panel/);
  assert.match(pluginDetailPage, /mkt-version-list/);
  assert.match(pluginsPage, /mkt-card-grid/);
  assert.match(css, /\.mkt-layout\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\) 340px/);
  assert.match(css, /@media \(max-width: 1180px\) \{[\s\S]*?\.mkt-layout\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(css, /\.mkt-card-grid\s*\{/);
  assert.match(css, /\.mkt-version-list\s*\{/);
});

test("processing preview layout has bounded cards and mobile single-column rules", () => {
  assert.match(css, /\.preview-diagnostics\s*{/);
  assert.match(css, /\.preview-chunk-list\s*{/);
  assert.match(css, /\.preview-chunk-card\s*{/);
  assert.match(css, /overflow-wrap:\s*anywhere/);

  const mobileBlock = css.match(/@media \(max-width: 760px\) \{[\s\S]*?\n\}/)?.[0] || "";
  assert.match(mobileBlock, /preview-diagnostics/);
  assert.match(mobileBlock, /preview-chunk-list/);
  assert.match(mobileBlock, /grid-template-columns:\s*1fr/);
});

test("chat home composer exposes enterprise controls without model selector", () => {
  assert.match(chatPage, /suggestedQuestions/);
  assert.match(chatPage, /composer-mode-select/);
  assert.match(chatPage, /composer-icon-button/);
  assert.match(chatPage, /composer-attachments/);
  assert.doesNotMatch(chatPage, /model selector/i);
  assert.match(css, /\.suggested-question-list/);
  assert.match(css, /\.composer-mode-select/);
  assert.match(css, /\.composer-icon-button/);
  assert.match(css, /\.composer-attachments/);
});

test("running conversation indicator keeps a visible spinner animation", () => {
  const runningDotBlock = css.match(/\.sidebar-running-dot\s*\{[\s\S]*?\n\}/)?.[0] || "";
  assert.match(runningDotBlock, /border-top-color:\s*var\(--color-accent\)/);
  assert.match(runningDotBlock, /animation:\s*sidebar-running-spin\s+760ms\s+linear\s+infinite\s+!important/);

  const reducedMotionBlocks = css.match(/@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?\n\}/g) || [];
  assert.equal(
    reducedMotionBlocks.some((block) => /animation:\s*none\s*!important/.test(block) && /\.sidebar-running-dot\b/.test(block)),
    false,
  );
});

test("document bulk delete action keeps a compact single-line layout", () => {
  assert.match(knowledgePage, /document-bulk-delete/);
  const bulkDeleteBlock = css.match(/\.bee-workspace \.document-toolbar-actions \.document-bulk-delete\s*\{[\s\S]*?\n\}/)?.[0] || "";
  assert.match(bulkDeleteBlock, /width:\s*auto/);
  assert.match(bulkDeleteBlock, /min-width:\s*108px/);
  assert.match(bulkDeleteBlock, /white-space:\s*nowrap/);
  assert.doesNotMatch(bulkDeleteBlock, /color:\s*var\(--color-danger\)/);
  assert.match(css, /\.document-bulk-delete b\s*\{/);
});

test("document metrics sit inside the document panel above search filters", () => {
  const tabsIndex = knowledgePage.indexOf('className="kb-detail-tabs"');
  const panelIndex = knowledgePage.indexOf('id="kb-panel-documents"');
  const metricsIndex = knowledgePage.indexOf("<KnowledgeBaseMetrics selected={selected} />");
  const toolbarIndex = knowledgePage.indexOf("<DocumentToolbar");

  assert.ok(tabsIndex >= 0);
  assert.ok(panelIndex > tabsIndex);
  assert.ok(metricsIndex > panelIndex);
  assert.ok(toolbarIndex > metricsIndex);
  assert.match(css, /#kb-panel-documents > \.kb-metrics\s*\{[\s\S]*?margin:\s*0 0 6px/);
  assert.match(css, /#kb-panel-documents:not\(\[hidden\]\)\) \.kb-detail-page\s*\{[\s\S]*?grid-template-rows:\s*40px 36px minmax\(0, 1fr\)/);
  assert.doesNotMatch(css, /#kb-panel-documents:not\(\[hidden\]\)\) \.kb-detail-page\s*\{[\s\S]*?grid-template-rows:\s*40px 24px 36px minmax\(0, 1fr\)/);
  assert.match(css, /#kb-panel-documents:not\(\[hidden\]\)\) #kb-panel-documents\s*\{[\s\S]*?padding:\s*10px 0 32px/);
  assert.match(css, /#kb-panel-documents:not\(\[hidden\]\)\) \.kb-metrics\s*\{[\s\S]*?height:\s*18px/);
});

test("knowledge document detail page exposes preview and chunk views", () => {
  assert.match(knowledgeDocumentPage, /knowledge-document-detail-page/);
  assert.match(knowledgeDocumentPage, /document-detail-tabs/);
  assert.match(knowledgeDocumentPage, /document-detail-chunks/);
  assert.match(knowledgeDocumentPage, /activeTab/);
  assert.match(css, /\.knowledge-document-detail-page/);
  assert.match(css, /\.document-detail-tabs/);
  assert.match(css, /\.document-detail-chunks/);
});

test("knowledge document detail page keeps a calm readable final layout", () => {
  const finalPassIndex = css.indexOf("Document detail final pass");
  assert.ok(finalPassIndex > 0);
  const finalCss = css.slice(finalPassIndex);

  assert.match(finalCss, /\.bee-workspace \.knowledge-document-detail-page\s*\{[\s\S]*?background:\s*var\(--color-paper-raised\)/);
  assert.match(finalCss, /\.bee-workspace \.document-detail-layout\s*\{[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\) 286px/);
  assert.match(finalCss, /\.bee-workspace \.document-detail-text,\s*\n\.bee-workspace \.document-detail-empty\s*\{[\s\S]*?background:\s*#fbfcfc/);
  assert.match(finalCss, /\.bee-workspace \.document-detail-text,\s*\n\.bee-workspace \.document-detail-empty\s*\{[\s\S]*?font-size:\s*14px/);
  assert.match(finalCss, /\.bee-workspace \.document-detail-summary\s*\{[\s\S]*?font-size:\s*13px/);
  assert.match(finalCss, /\.bee-workspace \.document-detail-context \.document-detail-meta > div\s*\{[\s\S]*?grid-template-columns:\s*70px minmax\(0, 1fr\)/);
});
