import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { chromium } from "playwright";

const base = process.env.APP_BASE || "http://localhost:3100";
const api = process.env.SKILL_TEST_API || "http://localhost:8301";
const out = new URL("../.artifacts/skills/", import.meta.url);
await mkdir(out, { recursive: true });
const browser = await chromium.launch({ headless: true, channel: "chrome" });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on("pageerror", e => errors.push(e.message));
await page.route("http://localhost:8000/**", async route => {
  const url = new URL(route.request().url());
  if (url.pathname.startsWith("/api/v1/") || url.pathname === "/knowledge-bases" || url.pathname === "/memories") return route.fulfill({ json: { items: [] } });
  await route.continue({ url: `${api}${url.pathname}${url.search}` });
});
const measurements = [];
async function capture(name) {
  await page.screenshot({ path: new URL(`${name}.png`, out).pathname.replace(/^\/(\w:)/, "$1"), fullPage: true });
  const layout = await page.evaluate(() => ({ width: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
  measurements.push({ name, ...layout });
  assert.ok(layout.scrollWidth <= layout.width, `${name}: horizontal overflow`);
}
try {
  await page.goto(`${base}/skills`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "上传技能", exact: true }).first().click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("发布令牌").fill("ui-test-token");
  const markdown = "---\nname: release-review\ndescription: 发布前检查变更范围、验证证据与回滚步骤。\nauthor: Bee 测试团队\ncategory: 开发协作\ntags: [代码审查, 发布]\n---\n# 发布检查\n\n此技能用于验收测试。\n\n## 使用方法\n\n1. 阅读变更范围。\n2. 核对测试结果。\n3. 列出回滚步骤。\n\n```text\nreview → verify → release\n```\n";
  await dialog.getByLabel("技能文件").setInputFiles({ name: "SKILL.md", mimeType: "text/markdown", buffer: Buffer.from(markdown) });
  await dialog.getByText("release-review", { exact: true }).waitFor();
  await capture("upload-desktop");
  await dialog.getByRole("button", { name: "发布技能", exact: true }).click();
  await page.locator(".skill-card").first().waitFor();
  await capture("catalog-desktop");
  await page.locator(".skill-card").first().click();
  await page.getByRole("heading", { name: "发布检查", exact: true }).waitFor();
  await capture("detail-desktop");
  const id = new URL(page.url()).searchParams.get("id");
  const download = await page.request.get(`${api}/skills/${id}/versions/1.0.0/download`);
  assert.equal(download.status(), 200);
  assert.equal((await download.body()).subarray(0, 2).toString(), "PK");
  await page.getByRole("button", { name: "在工作空间启用", exact: true }).click();
  await page.getByRole("button", { name: "停用技能", exact: true }).waitFor();
  await page.getByRole("tab", { name: "文件", exact: true }).click();
  await page.getByRole("button", { name: /SKILL.md/ }).waitFor();
  await page.getByRole("tab", { name: "原文", exact: true }).click();
  assert.ok((await page.locator(".skill-source").textContent()).includes("name: release-review"));
  await page.getByRole("tab", { name: "说明", exact: true }).click();
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 }); await capture(`detail-${width}`);
    await page.goto(`${base}/skills`, { waitUntil: "networkidle" }); await page.locator(".skill-card").first().waitFor(); await capture(`catalog-${width}`);
    await page.getByRole("button", { name: "上传技能", exact: true }).first().click();
    const mobileDialog = page.getByRole("dialog");
    await mobileDialog.getByLabel("发布令牌").fill("ui-test-token");
    await mobileDialog.getByLabel("技能文件").setInputFiles({ name: "SKILL.md", mimeType: "text/markdown", buffer: Buffer.from(markdown) });
    await mobileDialog.getByText("release-review", { exact: true }).waitFor();
    await capture(`upload-${width}`);
    await page.keyboard.press("Escape"); assert.equal(await page.getByRole("dialog").count(), 0);
    await page.goto(`${base}/skills/detail?id=${id}`, { waitUntil: "networkidle" }); await page.getByRole("heading", { name: "发布检查", exact: true }).waitFor();
  }
  await page.goto(`${base}/chat`, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "技能", exact: true }).click();
  await page.getByRole("checkbox", { name: /release-review/ }).check();
  await capture("chat-skills-320");
  assert.equal(errors.length, 0, errors.join("\n"));
  await writeFile(new URL("results.json", out), JSON.stringify({ measurements, errors, checks: ["upload", "catalog", "detail", "download", "activation", "files", "source", "mobile", "escape", "chat-selection"] }, null, 2));
  console.log("Skill UI smoke passed", measurements.length, "screenshots");
} catch (error) {
  console.error(await page.locator("body").innerText());
  await page.screenshot({ path: new URL("failure.png", out).pathname.replace(/^\/(\w:)/, "$1"), fullPage: true });
  throw error;
} finally { await browser.close(); }
