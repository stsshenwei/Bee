import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";

const base = process.env.APP_BASE || "http://127.0.0.1:3103";
const artifacts = resolve(".artifacts/workspace-hardening");
await mkdir(artifacts, { recursive: true });
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe" });
const results = [];
try {
  for (const width of [1440, 375]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    const errors = [];
    const writes = [];
    let release;
    let gate = Promise.resolve();
    page.on("pageerror", (error) => errors.push(error.message));
    // Mutations are fulfilled locally; this test never writes to the real backend.
    await page.route("**/*", async (route) => {
      const request = route.request();
      if (request.url().includes("/api/v1/sessions/recent")) {
        return route.fulfill({ json: { items: [{ session_id: "hardening-test", title: "A".repeat(120), is_running: false }] } });
      }
      if (!["GET", "HEAD", "OPTIONS"].includes(request.method())) {
        writes.push({ url: request.url(), data: request.postData() });
        await gate;
        return route.fulfill({ status: 503, json: { detail: "Test service unavailable" } });
      }
      return route.continue();
    });
    const pauseWrites = () => { gate = new Promise((resolve) => { release = resolve; }); };
    const resumeWrites = () => { release?.(); gate = Promise.resolve(); };
    const waitForWrites = async (count) => {
      for (let i = 0; i < 100 && writes.length < count; i++) await page.waitForTimeout(20);
      assert.equal(writes.length, count);
    };
    async function trapped(selector) {
      await page.locator(selector).waitFor();
      assert.ok(await page.evaluate((s) => Boolean(document.activeElement.closest(s)), selector), "initial focus");
      for (const key of ["Shift+Tab", ...Array(24).fill("Tab")]) {
        await page.keyboard.press(key);
        assert.ok(await page.evaluate((s) => Boolean(document.activeElement.closest(s)), selector), `${selector}: ${key}`);
      }
    }
    await page.goto(`${base}/knowledge`);
    await page.locator(".kb-card-open").first().waitFor();
    const create = page.locator(".kb-create-button");
    await create.click();
    await trapped(".kb-dialog");
    assert.ok(await page.locator(".sidebar").evaluate((e) => Boolean(e.closest("[inert]"))));
    await page.keyboard.press("Escape");
    await page.locator(".kb-dialog").waitFor({ state: "hidden" });
    assert.ok(await create.evaluate((e) => e === document.activeElement), "restore opener");
    assert.equal(await page.locator("[inert]").count(), 0);

    await create.click();
    const name = page.locator(".kb-wizard-panel input").first();
    const draft = "\u77e5\u8bc6\u5e93 \ud83d\udcda \u0645\u0631\u062d\u0628\u0627 " + "LongName".repeat(6);
    await name.fill(draft);
    pauseWrites();
    await page.locator(".kb-dialog .primary-action").evaluate((button) => { for (let i = 0; i < 10; i++) button.click(); });
    await waitForWrites(1);
    await page.keyboard.press("Escape");
    assert.ok(await page.locator(".kb-dialog").isVisible(), "pending creation remains visible");
    resumeWrites();
    await page.locator(".kb-dialog [role=alert]").waitFor();
    assert.equal(await name.inputValue(), draft);
    await page.screenshot({ path: resolve(artifacts, `create-error-${width}.png`), fullPage: true });
    await page.keyboard.press("Escape");

    if (width < 768) await page.locator(".sidebar-mobile-toggle").click();
    await page.locator(".sidebar-recent-more").first().click();
    await page.locator(".sidebar-session-menu button").first().click();
    const input = page.locator(".sidebar-rename-input");
    assert.equal(await input.evaluate((e) => e.closest("button")), null);
    await input.fill("\u4e2d\u6587 \ud83d\udcda \u0645\u0631\u062d\u0628\u0627");
    await input.dispatchEvent("keydown", { key: "Enter", isComposing: true });
    assert.equal(writes.length, 1, "IME must not save");
    await page.keyboard.press("Escape");
    await input.waitFor({ state: "hidden" });
    assert.equal(writes.length, 1, "cancel must not save");
    await page.waitForFunction(() => document.activeElement?.matches(".sidebar-recent-main"));
    await page.locator(".sidebar-recent-more").first().click();
    await page.locator(".sidebar-session-menu button").first().click();
    await input.fill("  ");
    await page.keyboard.press("Enter");
    await page.locator("#sidebar-rename-error").waitFor();
    assert.equal(writes.length, 1, "empty name must not save");
    await input.fill(draft);
    pauseWrites();
    await input.evaluate((e) => { for (let i = 0; i < 10; i++) e.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    await waitForWrites(2);
    resumeWrites();
    await page.locator("#sidebar-rename-error").waitFor();
    assert.equal(await input.inputValue(), draft);
    await page.screenshot({ path: resolve(artifacts, `rename-error-${width}.png`), fullPage: true });
    await page.keyboard.press("Escape");

    await page.locator(".kb-card-open").filter({ hasText: /^wiki$/ }).click();
    await page.locator(".doc-tile-card").first().waitFor();
    const tabs = page.locator(".kb-detail-tabs [role=tab]");
    await tabs.first().focus();
    for (const [key, index] of [["ArrowRight", 1], ["End", 2], ["Home", 0], ["ArrowLeft", 2], ["ArrowRight", 0]]) {
      await page.keyboard.press(key);
      assert.equal(await tabs.nth(index).getAttribute("aria-selected"), "true");
      assert.ok(await tabs.nth(index).evaluate((e) => e === document.activeElement));
      assert.equal(await page.locator('.kb-detail-tabs [tabindex="0"]').count(), 1);
      assert.ok(await tabs.nth(index).evaluate((e) => Boolean(document.getElementById(e.getAttribute("aria-controls")))));
    }
    await page.locator(".knowledge-actions .workspace-icon-button").click();
    await trapped(".kb-dialog");
    await page.keyboard.press("Escape");
    await page.locator(".doc-runtime-badge").first().click();
    await trapped(".trace-drawer");
    await page.keyboard.press("Escape");
    await page.locator(".doc-tile-menu-trigger").first().click();
    await page.locator(".doc-tile-menu [role=menuitem]").first().click();
    await trapped(".doc-viewer");
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => document.activeElement?.matches(".doc-tile-menu-trigger"));
    await page.locator(".upload-button").click();
    await page.locator('.upload-action-menu input[type="file"]').first().setInputFiles({ name: "hardening-test.txt", mimeType: "text/plain", buffer: Buffer.from("Local staged file; never uploaded.") });
    await trapped(".pending-upload-dialog");
    await page.keyboard.press("Escape");
    await page.locator(".pending-upload-dialog").waitFor({ state: "hidden" });
    assert.equal(writes.length, 2, "staging a file must not upload it");
    assert.equal(await page.locator("[inert]").count(), 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: resolve(artifacts, `documents-${width}.png`), fullPage: true });
    assert.deepEqual(errors, []);
    results.push({ width, keyboard: "passed", errors: "passed", realMutations: 0, interceptedMutations: writes.length });
    await page.close();
  }
  console.log(JSON.stringify(results, null, 2));
} finally {
  await browser.close();
}
