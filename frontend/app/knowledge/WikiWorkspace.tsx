"use client";

import { useEffect, useMemo, useState } from "react";
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
  const [graphZoom, setGraphZoom] = useState(1);
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

  const activeTasks = processingTasks.filter((task) => ACTIVE_TASK_STATES.has(task.status));

  async function selectDestination(slug: string, clearStatus = true) {
    setSelectedSlug(slug);
    if (clearStatus) setActionStatus("");
    try {
      if (slug === "log") {
        const result = await listWikiLogs(selected.id, { limit: 30 });
        setLogs(result.items);
        setLogCursor(result.next_cursor ?? null);
        setPage(null);
      } else {
        setPage(await getWikiPage(selected.id, slug));
      }
    } catch (cause) {
      setActionStatus(errorMessage(cause, "Wiki 页面加载失败"));
    }
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
    if (!activeTasks.length && !(overview?.active_task_count || 0)) return;
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
  }, [selected.id, activeTasks.length, overview?.active_task_count]);

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
      const value = String(href || "");
      if (value.startsWith("wiki:")) {
        return <button type="button" className="wiki-inline-link" onClick={() => void selectDestination(decodeURIComponent(value.slice(5)))}>{children}</button>;
      }
      return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
    },
  };

  const allGraphNodes = (graph?.nodes || []) as Array<{ slug?: string; title?: string; page_type?: string }>;
  const graphNodes = allGraphNodes.filter((node) => {
    const matchesType = graphTypes.includes(String(node.page_type || ""));
    const needle = graphQuery.trim().toLocaleLowerCase("zh-CN");
    return matchesType && (!needle || `${node.title || ""} ${node.slug || ""}`.toLocaleLowerCase("zh-CN").includes(needle));
  });
  const graphNodeSet = new Set(graphNodes.map((node) => node.slug));
  const graphEdges = ((graph?.edges || []) as Array<{ source?: string; target?: string }>).filter((edge) => graphNodeSet.has(edge.source) && graphNodeSet.has(edge.target));
  const graphPositions = new Map(graphNodes.map((node, index) => {
    const angle = (Math.PI * 2 * index) / Math.max(1, graphNodes.length) - Math.PI / 2;
    const radius = Math.min(310, 130 + graphNodes.length * 5);
    return [node.slug, { x: 430 + Math.cos(angle) * radius, y: 300 + Math.sin(angle) * radius }] as const;
  }));

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
              const expanded = expandedGroups.includes(group) || !expandedGroups.length;
              return <div className="wiki-tree-group" key={group}><button type="button" className="wiki-tree-heading" onClick={() => setExpandedGroups((current) => current.includes(group) ? current.filter((value) => value !== group) : [...current, group])}>{expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}<strong>{group}</strong><span>{items.length}</span></button>{expanded ? items.map((item) => <NavigationRow key={item.id} page={item} active={selectedSlug === item.slug} knowledgeBaseId={selected.id} onSelect={selectDestination} />) : null}</div>;
            })}
          </div>
          <button type="button" className="wiki-issues-entry" onClick={() => void openIssueDrawer()}><AlertTriangle size={16} /><span>问题与修复</span>{overview?.open_issue_count ? <b>{overview.open_issue_count}</b> : null}</button>
        </aside>

        <main className="wiki-reader-v2">
          {workspaceMode === "graph" ? (
            <section className="wiki-graph-workspace">
              <header><div><h2>知识图谱</h2><p>{graphNodes.length} 个页面，{graphEdges.length} 条连接</p></div><button type="button" title="刷新图谱" onClick={() => void getWikiGraph(selected.id, { limit: 160 }).then(setGraph)}><RefreshCw size={17} /></button></header>
              <div className="wiki-graph-toolbar"><div className="wiki-graph-search-wrap"><label className="wiki-search"><Search size={16} /><input value={graphQuery} placeholder="查找图谱页面..." onChange={(event) => setGraphQuery(event.target.value)} /></label>{graphQuery && graphSearchResults.length ? <div className="wiki-graph-search-results">{graphSearchResults.map((item) => <button type="button" key={item.id} onClick={() => { setGraphQuery(item.title); setGraphSearchResults([]); void openGraphNode(item.slug); }}><span>{item.title}</span><small>{wikiTypeLabel(item.page_type)}</small></button>)}</div> : null}</div><div className="wiki-graph-controls"><button type="button" title="缩小" onClick={() => setGraphZoom((value) => Math.max(0.6, value - 0.2))}><ZoomOut size={17} /></button><button type="button" title="适配画布" onClick={() => setGraphZoom(1)}><Maximize2 size={17} /></button><button type="button" title="放大" onClick={() => setGraphZoom((value) => Math.min(1.8, value + 0.2))}><ZoomIn size={17} /></button></div></div>
              <div className="wiki-graph-filters">{["summary", "entity", "concept", "manual"].map((type) => <label key={type}><input type="checkbox" checked={graphTypes.includes(type)} onChange={() => setGraphTypes((current) => current.includes(type) ? current.filter((value) => value !== type) : [...current, type])} /><span className={`wiki-legend-dot ${type}`} />{wikiTypeLabel(type)}</label>)}</div>
              <div className="wiki-graph-canvas">{graphNodes.length ? <svg viewBox="0 0 860 600" role="img" aria-label="Wiki 页面关系图"><g transform={`translate(${430 * (1 - graphZoom)} ${300 * (1 - graphZoom)}) scale(${graphZoom})`}>{graphEdges.map((edge, index) => { const source = graphPositions.get(edge.source); const target = graphPositions.get(edge.target); return source && target ? <line key={`${edge.source}-${edge.target}-${index}`} x1={source.x} y1={source.y} x2={target.x} y2={target.y} /> : null; })}{graphNodes.map((node) => { const position = graphPositions.get(node.slug); return position ? <g key={node.slug} className={`wiki-graph-node ${node.page_type}`} role="button" tabIndex={0} onClick={() => void openGraphNode(String(node.slug))} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") void openGraphNode(String(node.slug)); }}><circle cx={position.x} cy={position.y} r="26" /><text x={position.x} y={position.y + 43} textAnchor="middle">{String(node.title || node.slug).slice(0, 18)}</text></g> : null; })}</g></svg> : <div className="wiki-graph-empty"><Network size={38} /><p>{graphQuery ? "没有匹配的 Wiki 页面。" : "生成 Wiki 页面后，关系会显示在这里。"}</p></div>}</div>
            </section>
          ) : selectedSlug === "log" ? (
            <LogReader logs={logs} nextCursor={logCursor} onLoadMore={loadMoreLogs} />
          ) : page ? (
            <article className="wiki-article"><header className="wiki-article-head"><div><h2>{page.title}</h2><span className="wiki-article-kind">{wikiTypeLabel(page.page_type)}</span></div><button type="button" title="页面详情" onClick={() => setDrawer("details")}><Info size={18} /></button></header><div className="wiki-markdown-v2"><ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{wikiLinkMarkdown(page.content_markdown)}</ReactMarkdown></div></article>
          ) : (
            <div className="wiki-reader-empty"><BookOpen size={34} /><p>选择一个 Wiki 页面</p>{documents[0] ? <button type="button" onClick={() => void generateFromDocument(documents[0].id)}>从首个文档生成</button> : null}</div>
          )}
        </main>

        {drawer ? <aside className="wiki-detail-drawer"><header><strong>{drawer === "issues" ? "问题与修复" : drawer === "source" ? "来源证据" : "页面详情"}</strong><button type="button" title="关闭" onClick={() => setDrawer("")}><X size={17} /></button></header>{drawer === "issues" ? <><IssuePanel issues={issues} onUpdate={updateIssueStatus} onCleanup={cleanupIssue} /><ProposalPanel proposals={proposals} onApply={applyProposal} onReject={rejectProposal} />{processingTasks.filter((task) => ["retrying", "dead_lettered", "processing", "pending"].includes(task.status)).map((task) => <div className="wiki-task-row" key={task.id}><span><b>{task.task_type}</b><small>{task.status} · {task.attempt}/{task.max_attempts}</small></span>{task.status === "dead_lettered" ? <button type="button" title="重试" onClick={() => void retryTask(task.id)}><RotateCcw size={15} /></button> : !["completed", "canceled"].includes(task.status) ? <button type="button" title="取消" onClick={() => void cancelTask(task.id)}><X size={15} /></button> : null}</div>)}</> : drawer === "source" ? <SourcePanel panel={sourcePanel} /> : page ? <PageDetails page={page} onOpenSource={openSource} onSelect={selectDestination} /> : null}</aside> : null}
      </div>
      {activeTasks.length || generationTasks.some((task) => ACTIVE_TASK_STATES.has(task.status)) ? <div className="wiki-task-strip"><Clock3 size={15} /><span>{activeTasks.length || overview?.active_task_count} 个 Wiki 任务正在处理</span><span>向量化已按知识库策略跳过，不影响 Wiki 生成</span></div> : null}
    </section>
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
  return (markdown || "").replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (_match, slug: string, label?: string) => `[${label || slug}](wiki:${encodeURIComponent(String(slug || "").trim())})`);
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
