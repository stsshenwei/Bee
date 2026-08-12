import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const apiBase = process.env.API_BASE || "http://127.0.0.1:8000";
const appBase = process.env.APP_BASE || "http://127.0.0.1:3000";
const chromePath = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const artifactDir = resolve(process.env.ARTIFACT_DIR || ".artifacts");

await mkdir(artifactDir, { recursive: true });

async function request(path, init) {
  const response = await fetch(`${apiBase}${path}`, init);
  const body = response.status === 204 ? null : await response.json();
  if (!response.ok) throw new Error(body?.detail || `${response.status} ${path}`);
  return body;
}

const knowledgeBase = await request("/knowledge-bases", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    name: `Wiki Visual Smoke ${Date.now()}`,
    description: "网络产品资料 Wiki",
    type: "wiki",
  }),
});

const pages = [
  {
    slug: "index",
    title: "索引",
    page_type: "index",
    summary: "自动维护的分类目录",
    content_markdown: "# 索引\n\n> 分类目录\n\n## 维基索引\n\n本页为维基索引，将随页面增加自动更新。\n\n## 摘要 (2)\n\n- [[dh-p204-summary|PON全光产品/ONU/DH-P204 - Summary]] - 支持 GPON、PoE 与 Wi-Fi 6 的三层管理 ONU。\n- [[dh-p224-summary|PON全光产品/ONU/DH-P224 - Summary]] - 提供无网管光口与以太网供电能力。\n\n## 实体 (1)\n\n- [[dh-p204|DH-P204]] - 三层管理型 ONU 设备。\n\n## 概念 (1)\n\n- [[gpon|GPON]] - 千兆无源光网络接入技术。",
  },
  { slug: "dh-p204-summary", title: "PON全光产品/ONU/DH-P204 - Summary", page_type: "summary", summary: "支持 GPON、PoE 与 Wi-Fi 6 的三层管理 ONU。", content_markdown: "# DH-P204 摘要\n\n## 概览\n\nDH-P204 是支持 GPON/EPON 自适应与 Wi-Fi 6 的三层管理 ONU。\n\n## 主要能力\n\n- 4 个 PoE 以太网端口\n- GPON/EPON 自适应\n- 本地与远程管理" },
  { slug: "dh-p224-summary", title: "PON全光产品/ONU/DH-P224 - Summary", page_type: "summary", summary: "提供无网管光口与以太网供电能力。", content_markdown: "# DH-P224 摘要\n\nDH-P224 面向园区接入场景。" },
  { slug: "dh-p204", title: "DH-P204", page_type: "entity", summary: "三层管理型 ONU 设备。", content_markdown: "# DH-P204\n\nDH-P204 使用 [[gpon|GPON]] 接入，并提供 PoE 与 Wi-Fi 6。" },
  { slug: "gpon", title: "GPON", page_type: "concept", summary: "千兆无源光网络接入技术。", content_markdown: "# GPON\n\nGPON 是面向光接入网络的无源光网络技术，可连接 [[dh-p204|DH-P204]]。" },
];

try {
  for (const page of pages) {
    await request(`/knowledge-bases/${knowledgeBase.id}/wiki/pages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...page, status: "published" }),
    });
  }

  const browser = await chromium.launch({ headless: true, executablePath: chromePath });
  try {
    const desktop = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
    await desktop.goto(`${appBase}/knowledge?kb=${knowledgeBase.id}`, { waitUntil: "networkidle" });
    await desktop.getByRole("button", { name: "Wiki", exact: true }).click();
    await desktop.getByRole("button", { name: "索引", exact: true }).waitFor();
    await desktop.locator(".wiki-article-head h2", { hasText: "索引" }).waitFor();
    await desktop.screenshot({ path: resolve(artifactDir, "wiki-desktop.png"), fullPage: true });
    const desktopLayout = await desktop.evaluate(() => {
      const navigation = document.querySelector(".wiki-navigation")?.getBoundingClientRect();
      const reader = document.querySelector(".wiki-reader-v2")?.getBoundingClientRect();
      return {
        navigation: navigation && { left: navigation.left, right: navigation.right, width: navigation.width },
        reader: reader && { left: reader.left, right: reader.right, width: reader.width },
        overlap: Boolean(navigation && reader && navigation.right > reader.left + 1),
        horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth,
      };
    });

    await desktop.getByRole("button", { name: "Graph", exact: true }).click();
    await desktop.locator(".wiki-graph-canvas").waitFor();
    await desktop.waitForTimeout(400);
    await desktop.screenshot({ path: resolve(artifactDir, "wiki-graph.png"), fullPage: true });
    const graphPixels = await desktop.locator(".wiki-graph-node").count();

    const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 1 });
    await mobile.goto(`${appBase}/knowledge?kb=${knowledgeBase.id}`, { waitUntil: "networkidle" });
    await mobile.getByRole("button", { name: "Wiki", exact: true }).click();
    await mobile.getByRole("button", { name: "索引", exact: true }).waitFor();
    await mobile.locator(".wiki-article-head h2", { hasText: "索引" }).waitFor();
    await mobile.screenshot({ path: resolve(artifactDir, "wiki-mobile.png"), fullPage: true });
    const mobileLayout = await mobile.evaluate(() => ({
      horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth,
      navigationHeight: document.querySelector(".wiki-navigation")?.getBoundingClientRect().height,
      readerWidth: document.querySelector(".wiki-reader-v2")?.getBoundingClientRect().width,
    }));

    console.log(JSON.stringify({ desktopLayout, graphNodes: graphPixels, mobileLayout }, null, 2));
  } finally {
    await browser.close();
  }
} finally {
  await request(`/knowledge-bases/${knowledgeBase.id}`, { method: "DELETE" });
}
