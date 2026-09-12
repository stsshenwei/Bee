"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, WheelEvent as ReactWheelEvent } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertTriangle,
  BookOpen,
  Check,
  ChevronDown,
  ChevronRight,
  CircleDot,
  Clock3,
  FileText,
  Folder,
  FolderPlus,
  History,
  Info,
  LayoutList,
  ListTree,
  Maximize2,
  Network,
  Pencil,
  RefreshCw,
  RotateCcw,
  Search,
  Tag,
  X,
  ZoomIn,
  ZoomOut,
  Wrench,
} from "lucide-react";
import {
  applyWikiProposal,
  cancelWikiProcessingTask,
  cleanupWikiIssue,
  createWikiFolder,
  generateWikiPage,
  getWikiGraph,
  getWikiOverview,
  getWikiPage,
  listWikiFolders,
  listWikiGenerationTasks,
  listWikiIssues,
  listWikiLogs,
  listWikiPages,
  listWikiProcessingTasks,
  listWikiProposals,
  moveWikiPage,
  readWikiSourceDoc,
  rejectWikiProposal,
  retryWikiProcessingTask,
  updateWikiFolder,
  updateWikiIssue,
} from "../lib/api";
import type {
  DocumentItem,
  KnowledgeBase,
  WikiFolder,
  WikiGenerationTask,
  WikiGraph,
  WikiIssue,
  WikiLog,
  WikiOverview,
  WikiPage,
  WikiProcessingTask,
  WikiProposal,
} from "../lib/types";

type WikiWorkspaceProps = {
  selected: KnowledgeBase;
  documents: DocumentItem[];
  onOpenDocument: (item: DocumentItem) => void;
  workspaceMode: "reader" | "graph";
};

const ACTIVE_TASK_STATES = new Set(["pending", "queued", "processing", "running", "retrying", "finalizing"]);
const GRAPH_FIT_ZOOM = 0.78;
const GRAPH_MIN_ZOOM = 0.48;
const GRAPH_MAX_ZOOM = 1.6;
const GRAPH_LAYOUT_WIDTH = 1360;
const GRAPH_LAYOUT_HEIGHT = 680;
const REDUNDANT_WIKI_TYPE_HEADINGS = new Set([
  "\u6458\u8981",
  "\u6458\u8981\u9875\u9762",
  "\u5b9e\u4f53",
  "\u5b9e\u4f53\u9875\u9762",
  "\u6982\u5ff5",
  "\u6982\u5ff5\u9875\u9762",
  "\u624b\u5de5\u9875\u9762",
]);

type GraphPosition = { x: number; y: number; r: number };
type GraphVelocity = { x: number; y: number };
type GraphDragState = {
  slug: string;
  pointerId: number;
  offsetX: number;
  offsetY: number;
  startClientX: number;
  startClientY: number;
  moved: boolean;
};

function isActiveWikiTaskStatus(status: string) {
  return ACTIVE_TASK_STATES.has(String(status || "").trim().toLowerCase());
}

export function WikiWorkspace({ selected, documents, onOpenDocument, workspaceMode }: WikiWorkspaceProps) {
  const [query, setQuery] = useState("");
  const [pageGroup, setPageGroup] = useState<"knowledge" | "summary">("knowledge");
  const [navigationMode, setNavigationMode] = useState<"tree" | "list">("tree");
  const [pages, setPages] = useState<WikiPage[]>([]);
  const [folders, setFolders] = useState<WikiFolder[]>([]);
  const [activeFolder, setActiveFolder] = useState("");
  const [expandedFolders, setExpandedFolders] = useState<string[]>([]);
  const [loadedFolderParents, setLoadedFolderParents] = useState<string[]>([""]);
  const [expandedGroups, setExpandedGroups] = useState<string[]>([]);
  const [selectedSlug, setSelectedSlug] = useState("index");
  const [page, setPage] = useState<WikiPage | null>(null);
  const [overview, setOverview] = useState<WikiOverview | null>(null);
  const [logs, setLogs] = useState<WikiLog[]>([]);
  const [logCursor, setLogCursor] = useState<number | null>(null);
  const [issues, setIssues] = useState<WikiIssue[]>([]);
  const [proposals, setProposals] = useState<WikiProposal[]>([]);
  const [graph, setGraph] = useState<WikiGraph | null>(null);
  const [generationTasks, setGenerationTasks] = useState<WikiGenerationTask[]>([]);
  const [processingTasks, setProcessingTasks] = useState<WikiProcessingTask[]>([]);
  const [graphTypes, setGraphTypes] = useState(["summary", "entity", "concept", "manual"]);
  const [graphQuery, setGraphQuery] = useState("");
  const [graphSearchResults, setGraphSearchResults] = useState<WikiPage[]>([]);
  const [graphZoom, setGraphZoom] = useState(GRAPH_FIT_ZOOM);
  const [graphPan, setGraphPan] = useState({
    x: (GRAPH_LAYOUT_WIDTH / 2) * (1 - GRAPH_FIT_ZOOM),
    y: (GRAPH_LAYOUT_HEIGHT / 2) * (1 - GRAPH_FIT_ZOOM),
  });
  const [hoveredGraphSlug, setHoveredGraphSlug] = useState("");
  const [dynamicGraphPositions, setDynamicGraphPositions] = useState<Map<string, GraphPosition>>(new Map());
  const [graphSettleNonce, setGraphSettleNonce] = useState(0);
  const graphCanvasRef = useRef<HTMLDivElement>(null);
  const [graphMotionAllowed, setGraphMotionAllowed] = useState(false);

  useEffect(() => {
    if (workspaceMode !== "graph") {
      setGraphMotionAllowed(false);
      return;
    }
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setGraphMotionAllowed(!document.hidden && !preference.matches);
    update();
    preference.addEventListener("change", update);
    document.addEventListener("visibilitychange", update);
    return () => {
      preference.removeEventListener("change", update);
      document.removeEventListener("visibilitychange", update);
    };
  }, [workspaceMode]);
  const graphVelocityRef = useRef<Map<string, GraphVelocity>>(new Map());
  const graphPinnedRef = useRef<Set<string>>(new Set());
  const graphDragRef = useRef<GraphDragState | null>(null);
  const graphSuppressClickRef = useRef("");
  const graphRootRef = useRef<SVGGElement | null>(null);
  const [drawer, setDrawer] = useState<"" | "details" | "issues" | "source">("");
  const [sourcePanel, setSourcePanel] = useState<{ title: string; chunks: Array<Record<string, unknown>>; error: string }>({ title: "", chunks: [], error: "" });
  const [loading, setLoading] = useState(false);
  const [actionStatus, setActionStatus] = useState("");
  const [folderFormOpen, setFolderFormOpen] = useState(false);
  const [folderName, setFolderName] = useState("");
  const [renamingFolderId, setRenamingFolderId] = useState("");
  const [folderRename, setFolderRename] = useState("");

  const visiblePages = useMemo(() => pages.filter((item) => {
    if (["index", "log"].includes(item.page_type)) return false;
    if (activeFolder && item.folder_id !== activeFolder) return false;
    return pageGroup === "summary" ? item.page_type === "summary" : item.page_type !== "summary";
  }), [activeFolder, pageGroup, pages]);

  const groupedPages = useMemo(() => {
    const groups = new Map<string, WikiPage[]>();
    for (const item of visiblePages) {
      const key = item.category_path?.[0] || wikiTypeLabel(item.page_type);
      groups.set(key, [...(groups.get(key) || []), item]);
    }
    return Array.from(groups.entries()).sort(([left], [right]) => left.localeCompare(right, "zh-CN"));
  }, [visiblePages]);

  const activeTasks = processingTasks.filter((task) => isActiveWikiTaskStatus(task.status));
  const activeGenerationTasks = generationTasks.filter((task) => isActiveWikiTaskStatus(task.status));
  const activeWikiTaskCount = activeGenerationTasks.length || activeTasks.length;

  useEffect(() => {
    const groupNames = groupedPages.map(([group]) => group);
    setExpandedGroups((current) => {
      const valid = current.filter((group) => groupNames.includes(group));
      if (valid.length) return valid;
      return groupNames;
    });
  }, [groupedPages]);

  async function selectDestination(slug: string, clearStatus = true) {
    const cleanSlug = normalizeWikiSlug(slug);
    syncNavigationForSlug(cleanSlug);
    setSelectedSlug(cleanSlug);
    if (clearStatus) setActionStatus("");
    try {
      if (cleanSlug === "log") {
        const result = await listWikiLogs(selected.id, { limit: 30 });
        setLogs(result.items);
        setLogCursor(result.next_cursor ?? null);
        setPage(null);
      } else {
        setPage(await getWikiPage(selected.id, cleanSlug));
      }
    } catch (cause) {
      setActionStatus(errorMessage(cause, "Wiki 页面加载失败"));
    }
  }

  function syncNavigationForSlug(slug: string) {
    if (slug === "index" || slug === "log") {
      setActiveFolder("");
      return;
    }
    const target = pages.find((item) => item.slug === slug);
    if (!target) return;
    setPageGroup(target.page_type === "summary" ? "summary" : "knowledge");
    setActiveFolder(target.folder_id || "");
    const group = target.category_path?.[0] || wikiTypeLabel(target.page_type);
    setExpandedGroups((current) => current.includes(group) ? current : [...current, group]);
  }

  function internalWikiSlugFromHref(href: string | undefined) {
    const value = String(href || "").trim();
    if (!value || value.startsWith("#")) return "";
    if (value.startsWith("wiki:")) return normalizeWikiSlug(decodeWikiURIComponent(value.slice(5)));
    const pagePrefix = `/knowledge-bases/${selected.id}/wiki/pages/`;
    let candidate = value;
    if (/^[a-z][a-z0-9+.-]*:/i.test(value)) {
      try {
        const url = new URL(value, window.location.href);
        if (url.origin !== window.location.origin) return "";
        candidate = `${url.pathname}${url.hash}`;
      } catch {
        return "";
      }
    }
    if (candidate.startsWith(pagePrefix)) candidate = candidate.slice(pagePrefix.length);
    candidate = candidate.replace(/^[./]+/, "").split(/[?#]/)[0];
    const slug = normalizeWikiSlug(decodeWikiURIComponent(candidate));
    return pages.some((item) => item.slug === slug) || slug === "index" || slug === "log" ? slug : "";
  }

  async function loadNavigation(nextSlug = selectedSlug) {
    setLoading(true);
    try {
      const [nextOverview, pageResult, folderResult, nextGeneration, nextProcessing] = await Promise.all([
        getWikiOverview(selected.id),
        listWikiPages(selected.id, { q: query, status: "published", limit: 100 }),
        listWikiFolders(selected.id),
        listWikiGenerationTasks(selected.id, { limit: 20 }),
        listWikiProcessingTasks(selected.id),
      ]);
      setOverview(nextOverview);
      setPages(pageResult.items);
      setFolders(folderResult);
      setLoadedFolderParents([""]);
      setGenerationTasks(nextGeneration);
      setProcessingTasks(nextProcessing);
      if (workspaceMode === "reader") await selectDestination(nextSlug || "index", false);
    } catch (cause) {
      setActionStatus(errorMessage(cause, "Wiki 工作区加载失败"));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    const timer = window.setTimeout(() => void loadNavigation("index"), 180);
    return () => window.clearTimeout(timer);
  }, [selected.id, query]);

  useEffect(() => {
    if (workspaceMode !== "graph") return;
    setLoading(true);
    getWikiGraph(selected.id, { limit: 160 })
      .then(setGraph)
      .catch((cause) => setActionStatus(errorMessage(cause, "图谱加载失败")))
      .finally(() => setLoading(false));
  }, [selected.id, workspaceMode]);

  useEffect(() => {
    if (workspaceMode !== "graph" || !graphQuery.trim()) {
      setGraphSearchResults([]);
      return;
    }
    const timer = window.setTimeout(() => {
      void listWikiPages(selected.id, { q: graphQuery, status: "published", limit: 8 })
        .then((result) => setGraphSearchResults(result.items))
        .catch(() => setGraphSearchResults([]));
    }, 180);
    return () => window.clearTimeout(timer);
  }, [graphQuery, selected.id, workspaceMode]);

  useEffect(() => {
    if (!activeWikiTaskCount && !(overview?.active_task_count || 0)) return;
    const timer = window.setInterval(() => {
      void Promise.all([
        getWikiOverview(selected.id),
        listWikiProcessingTasks(selected.id),
        listWikiGenerationTasks(selected.id, { limit: 20 }),
      ]).then(([nextOverview, nextProcessing, nextGeneration]) => {
        setOverview(nextOverview);
        setProcessingTasks(nextProcessing);
        setGenerationTasks(nextGeneration);
      });
    }, 2500);
    return () => window.clearInterval(timer);
  }, [selected.id, activeWikiTaskCount, overview?.active_task_count]);

  async function loadMoreLogs() {
    if (logCursor === null) return;
    const result = await listWikiLogs(selected.id, { limit: 30, cursor: logCursor });
    setLogs((current) => [...current, ...result.items]);
    setLogCursor(result.next_cursor ?? null);
  }

  async function openSource(ref: { doc_id?: string; chunk_id?: string; title?: string }) {
    const document = documents.find((item) => item.id === ref.doc_id);
    if (document) onOpenDocument(document);
    setDrawer("source");
    try {
      const result = await readWikiSourceDoc(selected.id, {
        doc_id: ref.doc_id || "",
        chunk_ids: ref.chunk_id ? [ref.chunk_id] : [],
        limit: 8,
      });
      setSourcePanel({
        title: ref.title || document?.name || ref.doc_id || "来源证据",
        chunks: Array.isArray(result.chunks) ? result.chunks as Array<Record<string, unknown>> : [],
        error: "",
      });
    } catch (cause) {
      setSourcePanel({ title: ref.title || ref.doc_id || "来源证据", chunks: [], error: errorMessage(cause, "来源读取失败") });
    }
  }

  async function generateFromDocument(docId: string) {
    setActionStatus("正在加入 Wiki 生成队列...");
    const result = await generateWikiPage(selected.id, { doc_id: docId });
    setActionStatus(`Wiki 任务：${result.task.status}`);
    await loadNavigation(selectedSlug);
  }

  async function openIssueDrawer() {
    setDrawer("issues");
    const [nextIssues, nextProposals] = await Promise.all([
      listWikiIssues(selected.id, { status: "open", limit: 100 }),
      listWikiProposals(selected.id, { status: "pending", limit: 50 }),
    ]);
    setIssues(nextIssues);
    setProposals(nextProposals);
  }

  async function updateIssueStatus(issueId: string, status: string) {
    await updateWikiIssue(selected.id, issueId, status);
    await openIssueDrawer();
    setOverview(await getWikiOverview(selected.id));
  }

  async function cleanupIssue(issueId: string) {
    const result = await cleanupWikiIssue(selected.id, issueId);
    setActionStatus(result.cleaned ? "问题已确定性清理" : "已创建待审核修复提案");
    await openIssueDrawer();
  }

  async function applyProposal(proposalId: string) {
    const result = await applyWikiProposal(selected.id, proposalId);
    await openIssueDrawer();
    await loadNavigation(result.page?.slug || selectedSlug);
  }

  async function rejectProposal(proposalId: string) {
    await rejectWikiProposal(selected.id, proposalId);
    await openIssueDrawer();
  }

  async function createFolder() {
    const name = folderName.trim();
    if (!name) return;
    await createWikiFolder(selected.id, { name, parent_id: activeFolder });
    setFolderName("");
    setFolderFormOpen(false);
    await refreshFolderLevel(activeFolder);
  }

  async function refreshFolderLevel(parentId: string) {
    const children = await listWikiFolders(selected.id, parentId);
    setFolders((current) => [...current.filter((folder) => folder.parent_id !== parentId), ...children]);
    setLoadedFolderParents((current) => current.includes(parentId) ? current : [...current, parentId]);
  }

  async function toggleFolder(folder: WikiFolder) {
    if (!loadedFolderParents.includes(folder.id)) await refreshFolderLevel(folder.id);
    setExpandedFolders((current) => current.includes(folder.id) ? current.filter((id) => id !== folder.id) : [...current, folder.id]);
  }

  async function renameFolder(folder: WikiFolder) {
    const name = folderRename.trim();
    if (!name) return;
    await updateWikiFolder(selected.id, folder.id, { name });
    setRenamingFolderId("");
    setFolderRename("");
    await refreshFolderLevel(folder.parent_id);
  }

  async function dropOnFolder(folderId: string, encoded: string) {
    const [kind, knowledgeBaseId, value] = encoded.split(":", 3);
    if (knowledgeBaseId !== selected.id || !value) {
      setActionStatus("不能跨知识库移动 Wiki 内容");
      return;
    }
    try {
      if (kind === "page") await moveWikiPage(selected.id, value, { folder_id: folderId });
      if (kind === "folder") await updateWikiFolder(selected.id, value, { parent_id: folderId });
      await loadNavigation(selectedSlug);
      await refreshFolderLevel(folderId);
    } catch (cause) {
      setActionStatus(errorMessage(cause, "移动失败"));
    }
  }

  async function retryTask(taskId: string) {
    await retryWikiProcessingTask(selected.id, taskId);
    setProcessingTasks(await listWikiProcessingTasks(selected.id));
  }

  async function cancelTask(taskId: string) {
    await cancelWikiProcessingTask(selected.id, taskId);
    setProcessingTasks(await listWikiProcessingTasks(selected.id));
  }

  const markdownComponents: Components = {
    a({ href, children }) {
      const slug = internalWikiSlugFromHref(href);
      if (slug) {
        return <button type="button" className="wiki-inline-link" onClick={() => void selectDestination(slug)}>{children}</button>;
      }
      return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
    },
  };

  const allGraphNodes = useMemo(() => (graph?.nodes || []) as Array<{ slug?: string; title?: string; page_type?: string }>, [graph]);
  const graphNodes = useMemo(() => allGraphNodes.filter((node) => {
    const matchesType = graphTypes.includes(String(node.page_type || ""));
    const needle = graphQuery.trim().toLocaleLowerCase("zh-CN");
    return matchesType && (!needle || `${node.title || ""} ${node.slug || ""}`.toLocaleLowerCase("zh-CN").includes(needle));
  }), [allGraphNodes, graphQuery, graphTypes]);
  const graphNodeSet = useMemo(() => new Set(graphNodes.map((node) => node.slug)), [graphNodes]);
  const graphEdges = useMemo(() => ((graph?.edges || []) as Array<{ source?: string; target?: string }>).filter((edge) => graphNodeSet.has(edge.source) && graphNodeSet.has(edge.target)), [graph, graphNodeSet]);
  const graphLayout = useMemo(() => buildGraphLayout(graphNodes, graphEdges), [graphNodes, graphEdges]);
  const graphPositions = dynamicGraphPositions.size ? dynamicGraphPositions : graphLayout.positions;
  const graphNeighbors = useMemo(() => {
    const neighbors = new Map<string, Set<string>>();
    for (const node of graphNodes) {
      const slug = String(node.slug || "");
      if (slug) neighbors.set(slug, new Set());
    }
    for (const edge of graphEdges) {
      const source = String(edge.source || "");
      const target = String(edge.target || "");
      if (!source || !target) continue;
      if (!neighbors.has(source)) neighbors.set(source, new Set());
      if (!neighbors.has(target)) neighbors.set(target, new Set());
      neighbors.get(source)!.add(target);
      neighbors.get(target)!.add(source);
    }
    return neighbors;
  }, [graphEdges, graphNodes]);
  const focusedGraphSlug = hoveredGraphSlug || (workspaceMode === "graph" && graphNeighbors.has(selectedSlug) ? selectedSlug : "");
  const focusedGraphNeighbors = focusedGraphSlug ? graphNeighbors.get(focusedGraphSlug) || new Set<string>() : null;
  const graphTransform = `translate(${graphPan.x} ${graphPan.y}) scale(${graphZoom})`;

  function fitGraphCanvas() {
    setGraphZoom(GRAPH_FIT_ZOOM);
    setGraphPan({
      x: (graphLayout.width / 2) * (1 - GRAPH_FIT_ZOOM),
      y: (graphLayout.height / 2) * (1 - GRAPH_FIT_ZOOM),
    });
  }

  useEffect(() => {
    const nextPositions = copyGraphPositions(graphLayout.positions);
    const nextVelocity = new Map<string, GraphVelocity>();
    for (const slug of nextPositions.keys()) {
      nextVelocity.set(slug, { x: 0, y: 0 });
    }
    graphVelocityRef.current = nextVelocity;
    graphPinnedRef.current = new Set(Array.from(graphPinnedRef.current).filter((slug) => nextPositions.has(slug)));
    graphDragRef.current = null;
    graphSuppressClickRef.current = "";
    setDynamicGraphPositions(nextPositions);
    setGraphZoom(GRAPH_FIT_ZOOM);
    setGraphPan({
      x: (graphLayout.width / 2) * (1 - GRAPH_FIT_ZOOM),
      y: (graphLayout.height / 2) * (1 - GRAPH_FIT_ZOOM),
    });
    setGraphSettleNonce((value) => value + 1);
  }, [graphLayout]);

  useEffect(() => {
    if (workspaceMode !== "graph" || !graphMotionAllowed || graphNodes.length === 0) return;

    let frameId = 0;
    let alpha = 1;
    const nodeTypes = new Map(graphNodes.map((node) => [String(node.slug || ""), String(node.page_type || "concept")]));
    const links = graphEdges
      .map((edge) => ({ source: String(edge.source || ""), target: String(edge.target || "") }))
      .filter((edge) => edge.source && edge.target);

    const tick = () => {
      alpha *= graphDragRef.current ? 0.996 : 0.982;
      const forceAlpha = graphDragRef.current ? Math.max(alpha, 0.36) : alpha;

      setDynamicGraphPositions((current) => {
        if (!current.size) return current;
        const next = copyGraphPositions(current);
        const velocity = graphVelocityRef.current;
        for (const slug of next.keys()) {
          if (!velocity.has(slug)) velocity.set(slug, { x: 0, y: 0 });
        }

        const entries = Array.from(next.entries()).sort((left, right) => left[1].x - right[1].x);
        const maxRepulsionDistance = 320;
        const maxRepulsionDistanceSq = maxRepulsionDistance * maxRepulsionDistance;
        for (let leftIndex = 0; leftIndex < entries.length; leftIndex += 1) {
          const [leftSlug, left] = entries[leftIndex];
          for (let rightIndex = leftIndex + 1; rightIndex < entries.length; rightIndex += 1) {
            const [rightSlug, right] = entries[rightIndex];
            const dx = right.x - left.x;
            if (dx > maxRepulsionDistance) break;
            const dy = right.y - left.y;
            if (Math.abs(dy) > maxRepulsionDistance) continue;
            const distanceSq = dx * dx + dy * dy;
            if (distanceSq > maxRepulsionDistanceSq) continue;
            const distance = Math.sqrt(Math.max(1, distanceSq));
            const force = Math.min(14, 26000 / Math.max(900, distanceSq)) * forceAlpha;
            const forceX = (dx / distance) * force;
            const forceY = (dy / distance) * force;
            const leftVelocity = velocity.get(leftSlug)!;
            const rightVelocity = velocity.get(rightSlug)!;
            if (!graphPinnedRef.current.has(leftSlug)) {
              leftVelocity.x -= forceX;
              leftVelocity.y -= forceY;
            }
            if (!graphPinnedRef.current.has(rightSlug)) {
              rightVelocity.x += forceX;
              rightVelocity.y += forceY;
            }
          }
        }

        for (const link of links) {
          const source = next.get(link.source);
          const target = next.get(link.target);
          if (!source || !target) continue;
          const dx = target.x - source.x;
          const dy = target.y - source.y;
          const distance = Math.max(1, Math.sqrt(dx * dx + dy * dy));
          const force = (distance - 150) * 0.012 * forceAlpha;
          const forceX = (dx / distance) * force;
          const forceY = (dy / distance) * force;
          const sourceVelocity = velocity.get(link.source)!;
          const targetVelocity = velocity.get(link.target)!;
          if (!graphPinnedRef.current.has(link.source)) {
            sourceVelocity.x += forceX;
            sourceVelocity.y += forceY;
          }
          if (!graphPinnedRef.current.has(link.target)) {
            targetVelocity.x -= forceX;
            targetVelocity.y -= forceY;
          }
        }

        for (const [slug, position] of next) {
          if (graphPinnedRef.current.has(slug)) continue;
          const anchor = graphAnchorForType(nodeTypes.get(slug) || "concept", graphLayout.width, graphLayout.height);
          const currentVelocity = velocity.get(slug)!;
          currentVelocity.x += (anchor.x - position.x) * 0.006 * forceAlpha;
          currentVelocity.y += (anchor.y - position.y) * 0.006 * forceAlpha;
          currentVelocity.x *= 0.68;
          currentVelocity.y *= 0.68;
          const speed = Math.sqrt(currentVelocity.x * currentVelocity.x + currentVelocity.y * currentVelocity.y);
          if (speed > 16) {
            currentVelocity.x = (currentVelocity.x / speed) * 16;
            currentVelocity.y = (currentVelocity.y / speed) * 16;
          }
          position.x = clamp(position.x + currentVelocity.x, 70, graphLayout.width - 70);
          position.y = clamp(position.y + currentVelocity.y, 82, graphLayout.height - 70);
        }

        return next;
      });

      if (alpha > 0.018 || graphDragRef.current) {
        frameId = requestAnimationFrame(tick);
      }
    };

    frameId = requestAnimationFrame(tick);
    return () => {
      if (frameId) cancelAnimationFrame(frameId);
    };
  }, [graphEdges, graphLayout.height, graphLayout.width, graphNodes, graphSettleNonce, workspaceMode, graphMotionAllowed]);

  function graphPointFromPointer(event: ReactPointerEvent<SVGElement>) {
    const target = event.currentTarget;
    const svg = target instanceof SVGSVGElement ? target : target.ownerSVGElement;
    const matrix = graphRootRef.current?.getScreenCTM()?.inverse() || svg?.getScreenCTM()?.inverse();
    if (!svg || !matrix) return null;
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const transformed = point.matrixTransform(matrix);
    return { x: transformed.x, y: transformed.y };
  }

  function zoomGraphWithWheel(event: ReactWheelEvent<SVGSVGElement>) {
    event.preventDefault();
    const matrix = event.currentTarget.getScreenCTM()?.inverse();
    if (!matrix) return;
    const point = event.currentTarget.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const cursor = point.matrixTransform(matrix);
    const nextZoom = clamp(graphZoom * (event.deltaY < 0 ? 1.12 : 0.88), GRAPH_MIN_ZOOM, GRAPH_MAX_ZOOM);
    if (nextZoom === graphZoom) return;
    const graphX = (cursor.x - graphPan.x) / graphZoom;
    const graphY = (cursor.y - graphPan.y) / graphZoom;
    setGraphZoom(nextZoom);
    setGraphPan({
      x: cursor.x - graphX * nextZoom,
      y: cursor.y - graphY * nextZoom,
    });
    setGraphSettleNonce((value) => value + 1);
  }

  function beginGraphNodeDrag(event: ReactPointerEvent<SVGGElement>, slug: string) {
    if (event.button !== 0) return;
    const point = graphPointFromPointer(event);
    const position = graphPositions.get(slug);
    if (!point || !position) return;
    event.stopPropagation();
    graphPinnedRef.current.add(slug);
    graphVelocityRef.current.set(slug, { x: 0, y: 0 });
    graphDragRef.current = {
      slug,
      pointerId: event.pointerId,
      offsetX: point.x - position.x,
      offsetY: point.y - position.y,
      startClientX: event.clientX,
      startClientY: event.clientY,
      moved: false,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveGraphNodeDrag(event: ReactPointerEvent<SVGSVGElement>) {
    const drag = graphDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const point = graphPointFromPointer(event);
    if (!point) return;
    const moved = Math.abs(event.clientX - drag.startClientX) > 4 || Math.abs(event.clientY - drag.startClientY) > 4;
    graphDragRef.current = { ...drag, moved: drag.moved || moved };
    setDynamicGraphPositions((current) => {
      const next = current.size ? copyGraphPositions(current) : copyGraphPositions(graphLayout.positions);
      const previous = next.get(drag.slug);
      if (!previous) return current;
      next.set(drag.slug, {
        ...previous,
        x: clamp(point.x - drag.offsetX, 70, graphLayout.width - 70),
        y: clamp(point.y - drag.offsetY, 82, graphLayout.height - 70),
      });
      return next;
    });
    graphVelocityRef.current.set(drag.slug, { x: 0, y: 0 });
  }

  function endGraphNodeDrag(event: ReactPointerEvent<SVGSVGElement>) {
    const drag = graphDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (drag.moved) graphSuppressClickRef.current = drag.slug;
    graphDragRef.current = null;
    setGraphSettleNonce((value) => value + 1);
  }

  function handleGraphNodeClick(slug: string) {
    if (graphSuppressClickRef.current === slug) {
      graphSuppressClickRef.current = "";
      return;
    }
    void openGraphNode(slug);
  }

  async function openGraphNode(slug: string) {
    await selectDestination(slug);
    setDrawer("details");
  }

  return (
    <section className="wiki-workspace wiki-workspace-v2">
      {actionStatus ? <div className="wiki-status-toast">{actionStatus}<button type="button" title="关闭" onClick={() => setActionStatus("")}><X size={15} /></button></div> : null}
      <div className={`wiki-layout-v2 ${workspaceMode === "graph" ? "graph-mode" : ""}`}>
        <aside className="wiki-navigation">
          <label className="wiki-search"><Search size={17} /><input value={query} placeholder="搜索 Wiki 页面..." onChange={(event) => setQuery(event.target.value)} /></label>
          <nav className="wiki-system-nav" aria-label="Wiki 系统页面">
            <button type="button" className={selectedSlug === "index" && workspaceMode === "reader" ? "active" : ""} onClick={() => void selectDestination("index")}><BookOpen size={17} /><span>索引</span></button>
            <button type="button" className={selectedSlug === "log" && workspaceMode === "reader" ? "active" : ""} onClick={() => void selectDestination("log")}><History size={17} /><span>日志</span></button>
          </nav>
          <div className="wiki-nav-divider" />
          <div className="wiki-nav-tools">
            <div className="wiki-page-tabs" role="tablist">
              <button type="button" className={pageGroup === "knowledge" ? "active" : ""} onClick={() => setPageGroup("knowledge")}>知识 <span>{(overview?.page_counts.entity || 0) + (overview?.page_counts.concept || 0) + (overview?.page_counts.manual || 0)}</span></button>
              <button type="button" className={pageGroup === "summary" ? "active" : ""} onClick={() => setPageGroup("summary")}>摘要 <span>{overview?.page_counts.summary || 0}</span></button>
            </div>
            <div className="wiki-nav-actions">
              <div className="wiki-mode-switch" role="group" aria-label="导航显示方式">
                <button type="button" title="树形视图" className={navigationMode === "tree" ? "active" : ""} onClick={() => setNavigationMode("tree")}><ListTree size={17} /></button>
                <button type="button" title="列表视图" className={navigationMode === "list" ? "active" : ""} onClick={() => setNavigationMode("list")}><LayoutList size={17} /></button>
              </div>
              <button type="button" title="新建文件夹" onClick={() => setFolderFormOpen((value) => !value)}><FolderPlus size={17} /></button>
            </div>
          </div>
          {folderFormOpen ? <div className="wiki-folder-form"><input value={folderName} autoFocus placeholder="文件夹名称" onChange={(event) => setFolderName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void createFolder(); }} /><button type="button" onClick={() => void createFolder()}>创建</button></div> : null}
          {folders.length ? <div className="wiki-folder-filter"><button type="button" className={!activeFolder ? "active" : ""} onClick={() => setActiveFolder("")}><Folder size={15} />全部页面</button><FolderTree folders={folders} parentId="" activeFolder={activeFolder} expanded={expandedFolders} renamingId={renamingFolderId} renameValue={folderRename} knowledgeBaseId={selected.id} onToggle={toggleFolder} onSelect={setActiveFolder} onBeginRename={(folder) => { setRenamingFolderId(folder.id); setFolderRename(folder.name); }} onRenameValue={setFolderRename} onCommitRename={renameFolder} onDrop={dropOnFolder} /></div> : null}
          <div className={`wiki-page-tree ${navigationMode}`}>
            {loading && !pages.length ? <div className="wiki-nav-loading">正在加载...</div> : null}
            {!visiblePages.length && !loading ? <div className="wiki-nav-empty">暂无页面</div> : null}
            {navigationMode === "list" || query.trim() ? visiblePages.map((item) => <NavigationRow key={item.id} page={item} active={selectedSlug === item.slug} knowledgeBaseId={selected.id} onSelect={selectDestination} />) : groupedPages.map(([group, items]) => {
              const expanded = expandedGroups.includes(group);
              return <div className="wiki-tree-group" key={group}><button type="button" className="wiki-tree-heading" onClick={() => setExpandedGroups((current) => current.includes(group) ? current.filter((value) => value !== group) : [...current, group])}>{expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}<strong>{group}</strong><span>{items.length}</span></button>{expanded ? items.map((item) => <NavigationRow key={item.id} page={item} active={selectedSlug === item.slug} knowledgeBaseId={selected.id} onSelect={selectDestination} />) : null}</div>;
            })}
          </div>
        </aside>

        <main className="wiki-reader-v2">
          {workspaceMode === "graph" ? (
            <section className="wiki-graph-workspace">
              <div className="wiki-graph-toolbar"><div className="wiki-graph-search-wrap"><label className="wiki-search"><Search size={16} /><input value={graphQuery} placeholder="查找图谱页面..." onChange={(event) => setGraphQuery(event.target.value)} /></label>{graphQuery && graphSearchResults.length ? <div className="wiki-graph-search-results">{graphSearchResults.map((item) => <button type="button" key={item.id} onClick={() => { setGraphQuery(item.title); setGraphSearchResults([]); void openGraphNode(item.slug); }}><span>{item.title}</span><small>{wikiTypeLabel(item.page_type)}</small></button>)}</div> : null}</div><div className="wiki-graph-controls"><button type="button" title="缩小" onClick={() => setGraphZoom((value) => Math.max(GRAPH_MIN_ZOOM, value - 0.2))}><ZoomOut size={17} /></button><button type="button" title="适配画布" onClick={fitGraphCanvas}><Maximize2 size={17} /></button><button type="button" title="放大" onClick={() => setGraphZoom((value) => Math.min(GRAPH_MAX_ZOOM, value + 0.2))}><ZoomIn size={17} /></button></div></div>
              <aside className="wiki-graph-info-tag">
                <header>
                  <div>
                    <h2>知识图谱</h2>
                    <p><span>{graphNodes.length} 页面</span><span>{graphEdges.length} 连接</span></p>
                  </div>
                  <button type="button" title="刷新图谱" onClick={() => void getWikiGraph(selected.id, { limit: 160 }).then(setGraph)}><RefreshCw size={17} /></button>
                </header>
                <div className="wiki-graph-filters">{["summary", "entity", "concept", "manual"].map((type) => <label key={type}><input type="checkbox" checked={graphTypes.includes(type)} onChange={() => setGraphTypes((current) => current.includes(type) ? current.filter((value) => value !== type) : [...current, type])} /><span className={`wiki-legend-dot ${type}`} />{wikiTypeLabel(type)}</label>)}</div>
              </aside>
              <div className="wiki-graph-canvas" ref={graphCanvasRef}>
                {graphNodes.length ? (
                  <svg
                    viewBox={`0 0 ${graphLayout.width} ${graphLayout.height}`}
                    role="img"
                    aria-label="Wiki 页面关系图"
                    onWheel={zoomGraphWithWheel}
                    onMouseLeave={() => setHoveredGraphSlug("")}
                    onPointerMove={moveGraphNodeDrag}
                    onPointerUp={endGraphNodeDrag}
                    onPointerCancel={endGraphNodeDrag}
                  >
                    <defs>
                      <marker id="wiki-graph-arrow" viewBox="0 0 12 8" markerWidth="10" markerHeight="10" refX="10" refY="4" orient="auto" markerUnits="strokeWidth">
                        <path d="M0,0 L12,4 L0,8 L3,4 Z" />
                      </marker>
                      <marker id="wiki-graph-arrow-active" viewBox="0 0 12 8" markerWidth="11" markerHeight="11" refX="10" refY="4" orient="auto" markerUnits="strokeWidth">
                        <path d="M0,0 L12,4 L0,8 L3,4 Z" />
                      </marker>
                    </defs>
                    <g ref={graphRootRef} transform={graphTransform}>
                      {graphEdges.map((edge, index) => {
                        const sourceSlug = String(edge.source || "");
                        const targetSlug = String(edge.target || "");
                        const source = graphPositions.get(sourceSlug);
                        const target = graphPositions.get(targetSlug);
                        if (!source || !target) return null;
                        const isRelated = focusedGraphSlug && (sourceSlug === focusedGraphSlug || targetSlug === focusedGraphSlug);
                        const dx = target.x - source.x;
                        const dy = target.y - source.y;
                        const distance = Math.max(1, Math.sqrt(dx * dx + dy * dy));
                        const sourcePad = source.r + 3;
                        const targetPad = target.r + 10;
                        const startX = source.x + (dx / distance) * sourcePad;
                        const startY = source.y + (dy / distance) * sourcePad;
                        const endX = target.x - (dx / distance) * targetPad;
                        const endY = target.y - (dy / distance) * targetPad;
                        const curve = Math.min(70, Math.max(-70, (dx + dy) * 0.08));
                        const cx = (startX + endX) / 2 - dy * 0.08 + curve;
                        const cy = (startY + endY) / 2 + dx * 0.08 - curve;
                        return <path key={`${sourceSlug}-${targetSlug}-${index}`} className={`wiki-graph-edge ${isRelated ? "related" : focusedGraphSlug ? "dimmed" : ""}`} markerEnd={`url(#${isRelated ? "wiki-graph-arrow-active" : "wiki-graph-arrow"})`} d={`M${startX} ${startY} Q${cx} ${cy} ${endX} ${endY}`} />;
                      })}
                      {graphNodes.map((node) => {
                        const slug = String(node.slug || "");
                        const position = graphPositions.get(slug);
                        const title = String(node.title || slug);
                        if (!position) return null;
                        const isFocused = focusedGraphSlug === slug;
                        const isRelated = Boolean(focusedGraphNeighbors?.has(slug));
                        const isDimmed = Boolean(focusedGraphSlug && !isFocused && !isRelated);
                        const radius = position.r + (isFocused ? 4 : isRelated ? 2 : 0);
                        return (
                          <g
                            key={slug}
                            className={`wiki-graph-node ${node.page_type} ${selectedSlug === slug ? "active" : ""} ${isFocused ? "hovered" : ""} ${isRelated ? "related" : ""} ${isDimmed ? "dimmed" : ""}`}
                            role="button"
                            tabIndex={0}
                            onMouseEnter={() => setHoveredGraphSlug(slug)}
                            onFocus={() => setHoveredGraphSlug(slug)}
                            onBlur={() => setHoveredGraphSlug("")}
                            onPointerDown={(event) => beginGraphNodeDrag(event, slug)}
                            onClick={() => handleGraphNodeClick(slug)}
                            onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") void openGraphNode(slug); }}
                          >
                            <circle className="node-halo" cx={position.x} cy={position.y} r={radius + 5} />
                            <circle cx={position.x} cy={position.y} r={radius} />
                            <text x={position.x} y={position.y + radius + 15} textAnchor="middle">{title.length > 18 ? `${title.slice(0, 17)}…` : title}</text>
                          </g>
                        );
                      })}
                    </g>
                  </svg>
                ) : (
                  <div className="wiki-graph-empty"><Network size={38} /><p>{graphQuery ? "没有匹配的 Wiki 页面。" : "生成 Wiki 页面后，关系会显示在这里。"}</p></div>
                )}
              </div>
            </section>
          ) : selectedSlug === "log" ? (
            <LogReader logs={logs} nextCursor={logCursor} onLoadMore={loadMoreLogs} />
          ) : page ? (
            <WikiArticle page={page} markdownComponents={markdownComponents} onOpenDetails={() => setDrawer("details")} />
          ) : (
            <div className="wiki-reader-empty"><BookOpen size={34} /><p>选择一个 Wiki 页面</p>{documents[0] ? <button type="button" onClick={() => void generateFromDocument(documents[0].id)}>从首个文档生成</button> : null}</div>
          )}
        </main>

        {drawer && drawer !== "issues" ? (
          <aside className={`wiki-detail-drawer ${workspaceMode === "graph" && drawer === "details" ? "wiki-graph-detail-drawer" : ""}`}>
            <header>
              <strong>{drawer === "source" ? "来源证据" : workspaceMode === "graph" && page ? page.title : "页面详情"}</strong>
              <button type="button" title="关闭" onClick={() => setDrawer("")}><X size={17} /></button>
            </header>
            {drawer === "source" ? (
              <SourcePanel panel={sourcePanel} />
            ) : page ? (
              workspaceMode === "graph" ? (
                <GraphPageDetails page={page} markdownComponents={markdownComponents} neighborCount={graphNeighbors.get(page.slug)?.size || 0} onSelect={selectDestination} />
              ) : (
                <PageDetails page={page} onOpenSource={openSource} onSelect={selectDestination} />
              )
            ) : null}
          </aside>
        ) : null}
      </div>
    </section>
  );
}

function WikiArticle({ page, markdownComponents, onOpenDetails }: { page: WikiPage; markdownComponents: Components; onOpenDetails: () => void }) {
  const markdown = wikiLinkMarkdown(stripRedundantWikiTypeHeading(page.content_markdown));
  return (
    <article className="wiki-article">
      <header className="wiki-article-head">
        <div>
          <h2>{page.title}</h2>
          <div className="wiki-article-meta">
            <span className={`wiki-article-kind ${page.page_type}`}>{wikiTypeLabel(page.page_type)}</span>
            {page.category_path.length ? <span className="wiki-article-path">{page.category_path.join(" / ")}</span> : null}
            <span>v{page.version}</span>
            {page.updated_at ? <span>{formatWikiTime(page.updated_at)}</span> : null}
          </div>
          {page.aliases.length ? (
            <div className="wiki-article-aliases">
              <span>别名</span>
              {page.aliases.slice(0, 12).map((alias) => <b key={alias}>{alias}</b>)}
            </div>
          ) : null}
        </div>
        <button type="button" title="页面详情" onClick={onOpenDetails}><Info size={18} /></button>
      </header>
      <div className="wiki-markdown-v2">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{markdown}</ReactMarkdown>
      </div>
    </article>
  );
}

function GraphPageDetails({ page, markdownComponents, neighborCount, onSelect }: { page: WikiPage; markdownComponents: Components; neighborCount: number; onSelect: (slug: string) => Promise<void> }) {
  const neighbors = [...page.out_links, ...page.in_links].filter((value, index, all) => value && all.indexOf(value) === index);
  const markdown = wikiLinkMarkdown(stripRedundantWikiTypeHeading(page.content_markdown));
  return (
    <article className="wiki-graph-page-details">
      <div className="wiki-article-meta">
        <span className={`wiki-article-kind ${page.page_type}`}>{wikiTypeLabel(page.page_type)}</span>
        <span>v{page.version}</span>
        {page.updated_at ? <span>{formatWikiTime(page.updated_at)}</span> : null}
      </div>
      <p className="wiki-graph-neighbor-note">全部 {neighborCount || neighbors.length} 个邻居已在图中，点击下方关系可切换到对应页面。</p>
      {page.aliases.length ? (
        <div className="wiki-article-aliases">
          <span>别名</span>
          {page.aliases.slice(0, 10).map((alias) => <b key={alias}>{alias}</b>)}
        </div>
      ) : null}
      <div className="wiki-markdown-v2 wiki-graph-markdown">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{markdown}</ReactMarkdown>
      </div>
      {neighbors.length ? (
        <section className="wiki-graph-related-pages">
          <h3><CircleDot size={15} />页面关系</h3>
          <div>{neighbors.slice(0, 18).map((slug) => <button type="button" key={slug} onClick={() => void onSelect(slug)}>{slug}</button>)}</div>
        </section>
      ) : null}
    </article>
  );
}

function FolderTree({ folders, parentId, activeFolder, expanded, renamingId, renameValue, knowledgeBaseId, onToggle, onSelect, onBeginRename, onRenameValue, onCommitRename, onDrop }: {
  folders: WikiFolder[];
  parentId: string;
  activeFolder: string;
  expanded: string[];
  renamingId: string;
  renameValue: string;
  knowledgeBaseId: string;
  onToggle: (folder: WikiFolder) => Promise<void>;
  onSelect: (folderId: string) => void;
  onBeginRename: (folder: WikiFolder) => void;
  onRenameValue: (value: string) => void;
  onCommitRename: (folder: WikiFolder) => Promise<void>;
  onDrop: (folderId: string, value: string) => Promise<void>;
}) {
  return <>{folders.filter((folder) => folder.parent_id === parentId).map((folder) => {
    const isExpanded = expanded.includes(folder.id);
    const hasLoadedChildren = folders.some((candidate) => candidate.parent_id === folder.id);
    return <div className="wiki-folder-branch" key={folder.id} style={{ "--folder-depth": folder.depth } as React.CSSProperties}>
      <div className={`wiki-folder-row ${activeFolder === folder.id ? "active" : ""}`} draggable onDragStart={(event) => event.dataTransfer.setData("text/plain", `folder:${knowledgeBaseId}:${folder.id}`)} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); void onDrop(folder.id, event.dataTransfer.getData("text/plain")); }}>
        <button type="button" className="wiki-folder-expand" title={isExpanded ? "收起文件夹" : "展开文件夹"} onClick={() => void onToggle(folder)}>{isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</button>
        {renamingId === folder.id ? <input autoFocus value={renameValue} aria-label="文件夹名称" onChange={(event) => onRenameValue(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void onCommitRename(folder); }} /> : <button type="button" className="wiki-folder-name" onClick={() => onSelect(folder.id)}><Folder size={15} /><span>{folder.name}</span><small>{folder.page_count}</small></button>}
        {renamingId === folder.id ? <button type="button" title="确认重命名" onClick={() => void onCommitRename(folder)}><Check size={14} /></button> : <button type="button" title="重命名文件夹" onClick={() => onBeginRename(folder)}><Pencil size={13} /></button>}
      </div>
      {isExpanded && hasLoadedChildren ? <FolderTree folders={folders} parentId={folder.id} activeFolder={activeFolder} expanded={expanded} renamingId={renamingId} renameValue={renameValue} knowledgeBaseId={knowledgeBaseId} onToggle={onToggle} onSelect={onSelect} onBeginRename={onBeginRename} onRenameValue={onRenameValue} onCommitRename={onCommitRename} onDrop={onDrop} /> : null}
    </div>;
  })}</>;
}

function NavigationRow({ page, active, knowledgeBaseId, onSelect }: { page: WikiPage; active: boolean; knowledgeBaseId: string; onSelect: (slug: string) => Promise<void> }) {
  return <button type="button" draggable onDragStart={(event) => event.dataTransfer.setData("text/plain", `page:${knowledgeBaseId}:${page.slug}`)} className={`wiki-nav-page ${active ? "active" : ""}`} onClick={() => void onSelect(page.slug)}><FileText size={15} /><span>{page.title}</span>{page.status !== "published" ? <i>{page.status}</i> : null}</button>;
}

function LogReader({ logs, nextCursor, onLoadMore }: { logs: WikiLog[]; nextCursor: number | null; onLoadMore: () => Promise<void> }) {
  return <article className="wiki-article wiki-log-reader"><header className="wiki-article-head"><div><h2>日志</h2><span className="wiki-article-kind">生成记录</span></div></header><p className="wiki-log-intro">记录 Wiki 自动生成、重处理、修复与最终化结果。</p><div className="wiki-log-list">{logs.map((item) => <section key={item.id}><History size={16} /><div><header><strong>{item.message || item.event_type}</strong><time>{formatWikiTime(item.created_at)}</time></header><p>{item.event_type} · {item.outcome}{item.document_id ? ` · ${item.document_id}` : ""}</p>{item.page_slugs.length ? <div>{item.page_slugs.map((slug) => <code key={slug}>{slug}</code>)}</div> : null}</div></section>)}</div>{nextCursor !== null ? <button type="button" className="wiki-load-more" onClick={() => void onLoadMore()}>加载更多</button> : null}</article>;
}

function IssuePanel({ issues, onUpdate, onCleanup }: { issues: WikiIssue[]; onUpdate: (id: string, status: string) => Promise<void>; onCleanup: (id: string) => Promise<void> }) {
  const [filter, setFilter] = useState("");
  const visible = filter ? issues.filter((issue) => issue.issue_type === filter) : issues;
  return <section className="wiki-review-panel"><div className="wiki-panel-title"><strong>待处理问题</strong><span>{visible.length}</span></div><select aria-label="问题类型" value={filter} onChange={(event) => setFilter(event.target.value)}><option value="">全部类型</option>{Array.from(new Set(issues.map((issue) => issue.issue_type))).map((type) => <option value={type} key={type}>{type}</option>)}</select>{visible.map((issue) => <article key={issue.id}><b>{issue.slug}</b><small>{issue.issue_type} · {issue.reported_by}</small><p>{issue.description}</p><div className="wiki-review-actions"><button type="button" title="清理或创建修复提案" onClick={() => void onCleanup(issue.id)}><Wrench size={14} /></button><button type="button" onClick={() => void onUpdate(issue.id, "resolved")}>解决</button><button type="button" onClick={() => void onUpdate(issue.id, "wontfix")}>忽略</button></div></article>)}</section>;
}

function ProposalPanel({ proposals, onApply, onReject }: { proposals: WikiProposal[]; onApply: (id: string) => Promise<void>; onReject: (id: string) => Promise<void> }) {
  return <section className="wiki-review-panel"><div className="wiki-panel-title"><strong>修复提案</strong><span>{proposals.length}</span></div>{proposals.map((proposal) => <article key={proposal.id}><b>{proposal.title || proposal.slug}</b><small>{proposal.action} · {proposal.created_by}</small><p>{proposal.reason || JSON.stringify(proposal.payload).slice(0, 180)}</p><div className="wiki-review-actions"><button type="button" onClick={() => void onApply(proposal.id)}>应用</button><button type="button" onClick={() => void onReject(proposal.id)}>拒绝</button></div></article>)}</section>;
}

function SourcePanel({ panel }: { panel: { title: string; chunks: Array<Record<string, unknown>>; error: string } }) {
  return <section className="wiki-review-panel wiki-source-panel"><div className="wiki-panel-title"><strong>{panel.title || "来源"}</strong><span>{panel.chunks.length}</span></div>{panel.error ? <p className="metric-error">{panel.error}</p> : null}{panel.chunks.map((chunk, index) => <article key={String(chunk.chunk_id || index)}><small>{String(chunk.chunk_type || "")} · {String(chunk.chunk_id || "").slice(0, 12)}</small><p>{String(chunk.content || "").slice(0, 900)}</p></article>)}</section>;
}

function PageDetails({ page, onOpenSource, onSelect }: { page: WikiPage; onOpenSource: (ref: WikiPage["source_refs"][number]) => Promise<void>; onSelect: (slug: string) => Promise<void> }) {
  return <div className="wiki-page-details"><dl><div><dt>状态</dt><dd>{page.status}</dd></div><div><dt>类型</dt><dd>{wikiTypeLabel(page.page_type)}</dd></div><div><dt>版本</dt><dd>v{page.version}</dd></div><div><dt>更新时间</dt><dd>{formatWikiTime(page.updated_at)}</dd></div></dl>{page.aliases.length ? <section><h3><Tag size={15} />别名</h3><div className="wiki-detail-tags">{page.aliases.map((alias) => <span key={alias}>{alias}</span>)}</div></section> : null}<section><h3><FileText size={15} />来源片段</h3>{page.source_refs.length ? page.source_refs.map((ref, index) => <button type="button" key={`${ref.doc_id}-${ref.chunk_id || index}`} onClick={() => void onOpenSource(ref)}>{ref.title || ref.doc_id}<small>{ref.chunk_id || "整篇文档"}</small></button>) : <p>无来源引用</p>}</section>{page.in_links.length || page.out_links.length ? <section><h3><CircleDot size={15} />页面关系</h3>{[...page.out_links, ...page.in_links].filter((value, index, all) => all.indexOf(value) === index).map((slug) => <button type="button" key={slug} onClick={() => void onSelect(slug)}>{slug}</button>)}</section> : null}</div>;
}

function wikiLinkMarkdown(markdown: string): string {
  return (markdown || "").replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (_match, slug: string, label?: string) => `[${label || slug}](./${encodeURIComponent(String(slug || "").trim())})`);
}

function stripRedundantWikiTypeHeading(markdown: string): string {
  const lines = String(markdown || "").split(/\r?\n/);
  const firstContentIndex = lines.findIndex((line) => line.trim());
  if (firstContentIndex < 0) return "";
  const firstContent = lines[firstContentIndex]
    .trim()
    .replace(/^#{1,6}\s*/, "")
    .replace(/^[-*]\s*/, "")
    .replace(/^\*\*(.+)\*\*$/, "$1")
    .replace(/^__(.+)__$/, "$1")
    .trim();
  if (!REDUNDANT_WIKI_TYPE_HEADINGS.has(firstContent)) return markdown;
  return lines.slice(firstContentIndex + 1).join("\n").replace(/^\s*\n+/, "");
}

function normalizeWikiSlug(value: string): string {
  const parts = String(value || "")
    .trim()
    .toLocaleLowerCase("zh-CN")
    .replace(/\s+/g, "-")
    .replace(/\/+/g, "/")
    .split("/")
    .map((part) => part.replace(/[^\p{L}\p{N}_.-]+/gu, "-").replace(/^[.-]+|[.-]+$/g, "").slice(0, 120))
    .filter(Boolean);
  return parts.join("/") || "index";
}

function decodeWikiURIComponent(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function buildGraphLayout(
  nodes: Array<{ slug?: string; title?: string; page_type?: string }>,
  edges: Array<{ source?: string; target?: string }>,
) {
  const width = GRAPH_LAYOUT_WIDTH;
  const height = GRAPH_LAYOUT_HEIGHT;
  const degree = new Map<string, number>();
  for (const edge of edges) {
    if (edge.source) degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
    if (edge.target) degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
  }
  const anchors: Record<string, { x: number; y: number }> = {
    summary: { x: width * 0.5, y: height * 0.5 },
    entity: { x: width * 0.38, y: height * 0.48 },
    concept: { x: width * 0.62, y: height * 0.5 },
    manual: { x: width * 0.5, y: height * 0.28 },
  };
  const positions = new Map<string, { x: number; y: number; r: number }>();
  const velocity = new Map<string, { x: number; y: number }>();
  nodes.forEach((node, index) => {
    const slug = String(node.slug || index);
    const type = String(node.page_type || "concept");
    const anchor = anchors[type] || anchors.concept;
    const seed = hashString(slug);
    const angle = ((seed % 360) / 180) * Math.PI;
    const ring = 80 + (index % 7) * 34 + (seed % 47);
    const r = Math.min(30, 15 + (degree.get(slug) || 0) * 1.6 + (type === "summary" ? 4 : 0));
    positions.set(slug, {
      x: anchor.x + Math.cos(angle) * ring,
      y: anchor.y + Math.sin(angle) * ring,
      r,
    });
    velocity.set(slug, { x: 0, y: 0 });
  });

  const links = edges
    .map((edge) => ({ source: String(edge.source || ""), target: String(edge.target || "") }))
    .filter((edge) => positions.has(edge.source) && positions.has(edge.target));
  for (let step = 0; step < 170; step += 1) {
    const list = Array.from(positions.entries());
    for (let i = 0; i < list.length; i += 1) {
      for (let j = i + 1; j < list.length; j += 1) {
        const [leftSlug, left] = list[i];
        const [rightSlug, right] = list[j];
        let dx = right.x - left.x;
        let dy = right.y - left.y;
        const distanceSq = Math.max(100, dx * dx + dy * dy);
        const distance = Math.sqrt(distanceSq);
        dx /= distance;
        dy /= distance;
        const force = Math.min(18, 22000 / distanceSq);
        const leftVelocity = velocity.get(leftSlug)!;
        const rightVelocity = velocity.get(rightSlug)!;
        leftVelocity.x -= dx * force;
        leftVelocity.y -= dy * force;
        rightVelocity.x += dx * force;
        rightVelocity.y += dy * force;
      }
    }
    for (const link of links) {
      const source = positions.get(link.source)!;
      const target = positions.get(link.target)!;
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const distance = Math.max(1, Math.sqrt(dx * dx + dy * dy));
      const force = (distance - 170) * 0.012;
      const sx = (dx / distance) * force;
      const sy = (dy / distance) * force;
      const sourceVelocity = velocity.get(link.source)!;
      const targetVelocity = velocity.get(link.target)!;
      sourceVelocity.x += sx;
      sourceVelocity.y += sy;
      targetVelocity.x -= sx;
      targetVelocity.y -= sy;
    }
    for (const node of nodes) {
      const slug = String(node.slug || "");
      const position = positions.get(slug);
      const currentVelocity = velocity.get(slug);
      if (!position || !currentVelocity) continue;
      const anchor = anchors[String(node.page_type || "concept")] || anchors.concept;
      currentVelocity.x += (anchor.x - position.x) * 0.008;
      currentVelocity.y += (anchor.y - position.y) * 0.008;
      position.x = clamp(position.x + currentVelocity.x, 70, width - 70);
      position.y = clamp(position.y + currentVelocity.y, 82, height - 70);
      currentVelocity.x *= 0.72;
      currentVelocity.y *= 0.72;
    }
  }
  return { width, height, positions };
}

function copyGraphPositions(positions: Map<string, GraphPosition>) {
  const next = new Map<string, GraphPosition>();
  for (const [slug, position] of positions) {
    next.set(slug, { ...position });
  }
  return next;
}

function graphAnchorForType(type: string, width: number, height: number) {
  const anchors: Record<string, { x: number; y: number }> = {
    summary: { x: width * 0.5, y: height * 0.5 },
    entity: { x: width * 0.38, y: height * 0.48 },
    concept: { x: width * 0.62, y: height * 0.5 },
    manual: { x: width * 0.5, y: height * 0.28 },
  };
  return anchors[type] || anchors.concept;
}

function hashString(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) >>> 0;
  }
  return hash;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function wikiTypeLabel(type: string) {
  return ({ summary: "摘要", entity: "实体", concept: "概念", manual: "手工页面", synthesis: "综合", index: "分类目录", log: "日志" } as Record<string, string>)[type] || type;
}

function formatWikiTime(value: string) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function errorMessage(cause: unknown, fallback: string) {
  return cause instanceof Error ? cause.message : fallback;
}
