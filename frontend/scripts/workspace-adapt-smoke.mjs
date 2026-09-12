import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";

const base = process.env.APP_BASE || "http://127.0.0.1:3105";
const artifacts = resolve(".artifacts/workspace-adapt");
await mkdir(artifacts, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe" });
const results = [];
const failures = [];
try {
  for (const [width, height, touch] of [[320, 740, true], [375, 844, true], [768, 1024, true], [1024, 768, true], [844, 390, true], [1440, 900, false]]) {
    const page = await browser.newPage({ viewport: { width, height }, hasTouch: touch });
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.route("**/*", (route) => ["GET", "HEAD", "OPTIONS"].includes(route.request().method()) ? route.continue() : route.abort());
    async function capture(name, targets = []) {
      await page.screenshot({ path: resolve(artifacts, `${name}-${width}x${height}.png`), fullPage: true });
      const geometry = await page.evaluate(({ targets }) => ({
        overflow: document.documentElement.scrollWidth > innerWidth,
        targets: targets.flatMap((s) => Array.from(document.querySelectorAll(s)).filter((e) => e.getClientRects().length).map((e) => {
          const r = e.getBoundingClientRect();
          return { selector: s, width: r.width, height: r.height };
        })),
      }), { targets });
      if (geometry.overflow) failures.push(`${width} ${name}: overflow`);
      if (touch) for (const r of geometry.targets) if (r.width < 43.9 || r.height < 43.9) failures.push(`${width} ${name}: small ${r.selector} ${r.width}x${r.height}`);
      results.push({ name, width, height, ...geometry });
    }
    await page.goto(`${base}/knowledge`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.locator(".kb-card-open").first().waitFor();
    await capture("catalog", [".kb-card-actions button", ".kb-create-button"]);
    await page.locator(".kb-create-button").click();
    await capture("create", [".kb-dialog button"]);
    await page.keyboard.press("Escape");
    await page.locator(".kb-card-open").filter({ hasText: /^wiki$/ }).click();
    await page.locator(".doc-tile-card").first().waitFor();
    await capture("documents", [".doc-tile-menu-trigger", ".workspace-segmented button", ".doc-select", ".knowledge-actions button"]);
    await page.locator(".filter-toggle").click();
    await capture("filters");
    await page.locator(".kb-detail-tabs button").nth(1).click();
    await page.locator(".wiki-navigation").waitFor();
    await capture("wiki");
    await page.locator(".kb-detail-tabs button").nth(2).click();
    await page.locator(".wiki-graph-node").first().waitFor();
    await capture("graph", [".wiki-graph-controls button"]);
    await page.goto(`${base}/chat`, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.locator(".chat-composer").waitFor();
    await capture("chat", [".composer-send", ".composer-icon-button", ".kb-scope-trigger", ".composer-mode-trigger"]);
    if (width <= 768) {
      await page.setViewportSize({ width, height: 480 });
      await page.locator(".chat-composer textarea").focus();
      const bounds = await page.locator(".chat-composer").boundingBox();
      if (bounds.y + bounds.height > 480) failures.push(`${width}: composer outside shortened viewport`);
      if (bounds.width < width - 48) failures.push(`${width}: composer compressed by sidebar column`);
      await capture("chat-short");
    }
    failures.push(...errors.map((e) => `${width}: ${e}`));
    await page.close();
  }
  console.log(JSON.stringify({ captures: results.length, failures }, null, 2));
  assert.deepEqual(failures, []);
} finally { await browser.close(); }
