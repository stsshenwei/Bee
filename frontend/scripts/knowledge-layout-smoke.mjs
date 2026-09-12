import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";

const base = process.env.APP_BASE || "http://127.0.0.1:3105";
const artifacts = resolve(".artifacts/knowledge-layout");
await mkdir(artifacts, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe" });
const results = [];
try {
  for (const width of [375, 1440, 1880]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", (route) => {
      if (new URL(route.request().url()).pathname === "/documents/parse") return route.fulfill({ json: { chunk_previews: [{ id: "layout-fixture", preview: "Layout test fixture. No parsing or indexing was performed.", characters: 61 }] } });
      return ["GET", "HEAD", "OPTIONS"].includes(route.request().method()) ? route.continue() : route.abort();
    });
    async function capture(name) {
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${width} ${name} overflow`);
      await page.screenshot({ path: resolve(artifacts, `${name}-${width}.png`), fullPage: name !== "reader" });
    }
    await page.goto(`${base}/knowledge`);
    await page.locator(".kb-card-open").first().waitFor();
    const card = await page.locator(".kb-card").first().boundingBox();
    assert.ok(card.height < 180);
    assert.equal(await page.locator(".kb-card-open svg").count(), 0);
    if (width > 900) assert.ok(card.width <= 320);
    await capture("catalog");
    await page.locator(".kb-card-open").filter({ hasText: /^wiki$/ }).click();
    await page.locator(".doc-tile-title").first().waitFor();
    await page.waitForURL(/knowledge\?kb=/);
    const detail = page.url();
    assert.equal(await page.locator(".doc-tile-card > .doc-select").count(), 0);
    await capture("documents");
    await page.locator(".doc-tile-title").first().click();
    await page.locator(".document-detail-text").waitFor();
    const content = await page.locator(".document-detail-content-section").boundingBox();
    const context = await page.locator(".document-detail-context").boundingBox();
    if (width > 900) assert.ok(content.x + content.width <= context.x + 1 && content.width > context.width * 2);
    assert.equal(await page.locator(".document-detail-text").evaluate((e) => getComputedStyle(e).maxHeight), "none");
    await capture("reader");
    await page.getByRole("button", { name: "\u5206\u5757", exact: true }).click();
    await page.locator(".document-detail-chunk").first().waitFor();
    await capture("chunks");
    await page.goto(detail);
    await page.locator(".kb-detail-tabs button").nth(1).click();
    await page.locator(".wiki-article-head").waitFor();
    const article = await page.locator(".wiki-reader-v2 .wiki-article").boundingBox();
    const reader = await page.locator(".wiki-reader-v2").boundingBox();
    assert.ok(article.width > reader.width * 0.85);
    await capture("wiki");
    assert.deepEqual(errors, []);
    results.push({ width, card, contentWidth: content.width, wikiWidth: article.width });
    await page.close();
  }
  console.log(JSON.stringify(results, null, 2));
} finally { await browser.close(); }
