import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";

const base = process.env.APP_BASE || "http://127.0.0.1:3105";
const artifacts = resolve(".artifacts/workspace-typeset");
await mkdir(artifacts, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe" });
const results = [];
try {
  for (const width of [375, 1440]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    await page.route("**/*", (route) => ["GET", "HEAD", "OPTIONS"].includes(route.request().method()) ? route.continue() : route.abort());
    const capture = async (name) => {
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${name} overflow`);
      await page.screenshot({ path: resolve(artifacts, `${name}-${width}.png`), fullPage: name !== "reader-text-200" });
    };
    await page.goto(`${base}/knowledge`);
    await page.locator(".kb-card-open").filter({ hasText: /^wiki$/ }).click();
    await page.locator(".doc-tile-title").first().waitFor();
    const metrics = await page.evaluate(() => Object.fromEntries([".doc-tile-title", ".doc-tile-summary", ".doc-tile-footer"].map((selector) => {
      const style = getComputedStyle(document.querySelector(selector));
      return [selector, { size: style.fontSize, line: style.lineHeight }];
    })));
    assert.equal(metrics[".doc-tile-summary"].size, "14px");
    assert.equal(metrics[".doc-tile-footer"].size, "12px");
    await capture("documents");
    await page.locator(".doc-tile-title").first().click();
    await page.locator(".document-detail-text").waitFor();
    assert.equal(await page.locator(".document-detail-text").evaluate((e) => getComputedStyle(e).fontSize), "16px");
    await capture("document-reader");
    await page.locator(".document-detail-title-row h1").evaluate((e) => { e.textContent = "\u77e5\u8bc6\u6587\u6863-" + "LongFilename".repeat(15) + ".txt"; });
    await page.evaluate(() => { document.documentElement.style.fontSize = "32px"; });
    await capture("reader-text-200");
    await page.evaluate(() => { document.documentElement.style.fontSize = ""; });
    await page.goto(`${base}/knowledge`);
    await page.locator(".kb-card-open").filter({ hasText: /^wiki$/ }).click();
    await page.locator(".kb-detail-tabs button").nth(1).click();
    await page.locator(".wiki-article-head").waitFor();
    await capture("wiki-reader");
    results.push({ width, ...metrics, reader: "16px", textScaling: "passed" });
    await page.close();
  }
  console.log(JSON.stringify(results, null, 2));
} finally { await browser.close(); }
