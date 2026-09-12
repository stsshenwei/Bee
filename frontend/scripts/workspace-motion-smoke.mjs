import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";

const base = process.env.APP_BASE || "http://127.0.0.1:3105";
const artifacts = resolve(".artifacts/workspace-motion");
await mkdir(artifacts, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe" });
const results = [];
try {
  for (const width of [1440, 375]) {
    const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: "reduce" });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", (route) => ["GET", "HEAD", "OPTIONS"].includes(route.request().method()) ? route.continue() : route.abort());
    await page.goto(`${base}/knowledge`, { waitUntil: "domcontentloaded" });
    await page.locator(".kb-card-open").first().waitFor();
    await page.locator(".kb-create-button").click();
    assert.equal(await page.locator(".kb-dialog").evaluate((e) => getComputedStyle(e).animationName), "none");
    await page.keyboard.press("Escape");
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await page.locator(".kb-create-button").click();
    assert.equal(await page.locator(".kb-dialog").evaluate((e) => getComputedStyle(e).animationName), "workspace-focus-enter");
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("[inert]").count(), 0);
    await page.locator(".kb-card-open").filter({ hasText: /^wiki$/ }).click();
    await page.locator(".filter-toggle").click();
    assert.equal(await page.locator(".document-advanced-filters").evaluate((e) => getComputedStyle(e).animationName), "workspace-filter-enter");
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.locator(".kb-detail-tabs button").nth(2).click();
    const node = page.locator(".wiki-graph-node circle:not(.node-halo)").first();
    await node.waitFor();
    await page.locator(".wiki-graph-canvas").scrollIntoViewIfNeeded();
    const position = () => node.evaluate((e) => [e.getAttribute("cx"), e.getAttribute("cy")]);
    const still = await position();
    await page.waitForTimeout(350);
    assert.deepEqual(await position(), still, "reduced motion must keep graph stable");
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await page.waitForTimeout(100);
    const moving = await position();
    await page.waitForTimeout(350);
    assert.notDeepEqual(await position(), moving, "normal mode should settle the graph");
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.waitForTimeout(100);
    const stopped = await position();
    await page.waitForTimeout(350);
    assert.deepEqual(await position(), stopped, "live preference change stops motion");
    await page.locator(".wiki-graph-node").first().focus();
    await page.keyboard.press("Enter");
    await page.locator(".wiki-detail-drawer").waitFor();
    assert.ok(await page.locator(".wiki-detail-drawer > header button").evaluate((e) => {
      const r = e.getBoundingClientRect();
      return e.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
    }), "drawer close must not be covered by the page header");
    await page.screenshot({ path: resolve(artifacts, `graph-reduced-${width}.png`), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    assert.deepEqual(errors, []);
    results.push({ width, preferences: "passed", keyboard: "passed", runtimeErrors: errors.length });
    await page.close();
  }
  console.log(JSON.stringify(results, null, 2));
} finally { await browser.close(); }
