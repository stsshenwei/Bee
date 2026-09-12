"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Check, X, PanelLeftClose, PanelLeftOpen, MessageSquare, Plus } from "lucide-react";
import { DeleteIcon, EditIcon, LibraryIcon, MoreIcon, NewChatIcon } from "./Icons";
import { deleteSession, listRecentSessions, renameSession } from "../lib/api";
import type { ChatSessionSummary } from "../lib/types";

export function Sidebar() {
  const pathname = usePathname();
  const router = useRouter();
  const [recentSessions, setRecentSessions] = useState<ChatSessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState("");
  const [openMenuSessionId, setOpenMenuSessionId] = useState("");
  const [renamingSessionId, setRenamingSessionId] = useState("");
  const [renameDraft, setRenameDraft] = useState("");
  const [renameError, setRenameError] = useState("");
  const [renameSaving, setRenameSaving] = useState(false);
  const renamePending = useRef(false);
  const renameRow = useRef<HTMLDivElement>(null);
  const [mobileOpen, setMobileOpen] = useState(false);

  useEffect(() => { setMobileOpen(false); }, [pathname]);

  async function refreshRecentSessions() {
    try {
      setRecentSessions(await listRecentSessions(20));
      setActiveSessionId(window.localStorage.getItem("bee:conversationId") || "");
    } catch {
      setRecentSessions([]);
    }
  }

  useEffect(() => {
    void refreshRecentSessions();
    function handleConversationUpdate() {
      void refreshRecentSessions();
    }
    function handleNewChatEvent() {
      setActiveSessionId("");
    }
    window.addEventListener("bee:conversation-updated", handleConversationUpdate);
    window.addEventListener("bee:new-chat", handleNewChatEvent);
    return () => {
      window.removeEventListener("bee:conversation-updated", handleConversationUpdate);
      window.removeEventListener("bee:new-chat", handleNewChatEvent);
    };
  }, []);

  useEffect(() => {
    if (!recentSessions.some((session) => session.is_running)) return;
    const timer = window.setInterval(() => {
      void refreshRecentSessions();
    }, 1500);
    return () => window.clearInterval(timer);
  }, [recentSessions]);

  useEffect(() => {
    function closeMenu() {
      setOpenMenuSessionId("");
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpenMenuSessionId("");
      }
    }
    window.addEventListener("click", closeMenu);
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("click", closeMenu);
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, []);

  function handleNewChat() {
    setMobileOpen(false);
    window.localStorage.removeItem("bee:conversationId");
    setActiveSessionId("");
    setOpenMenuSessionId("");
    setRenamingSessionId("");
    window.dispatchEvent(new Event("bee:new-chat"));
    router.push("/chat");
  }

  function openConversation(sessionId: string) {
    if (renamingSessionId) return;
    setMobileOpen(false);
    window.localStorage.setItem("bee:conversationId", sessionId);
    setActiveSessionId(sessionId);
    setOpenMenuSessionId("");
    window.dispatchEvent(new CustomEvent("bee:open-conversation", { detail: { sessionId } }));
    router.push("/chat");
  }

  function beginRename(session: ChatSessionSummary) {
    if (renamePending.current) return;
    setOpenMenuSessionId("");
    setRenamingSessionId(session.session_id);
    setRenameDraft(session.title || "新对话");
    setRenameError("");
  }

  function finishRename() {
    const row = renameRow.current;
    setRenamingSessionId("");
    setRenameError("");
    requestAnimationFrame(() => row?.querySelector<HTMLButtonElement>(".sidebar-recent-main")?.focus());
  }

  async function commitRename(session: ChatSessionSummary) {
    if (renamePending.current) return;
    const title = renameDraft.trim();
    if (!title) {
      setRenameError("请输入对话名称。");
      return;
    }
    if (title === session.title) {
      finishRename();
      return;
    }
    renamePending.current = true;
    setRenameSaving(true);
    setRenameError("");
    try {
      await renameSession(session.session_id, title);
      setRecentSessions((current) => current.map((item) => item.session_id === session.session_id ? { ...item, title } : item));
      finishRename();
      window.dispatchEvent(new CustomEvent("bee:conversation-updated", { detail: { sessionId: session.session_id } }));
      void refreshRecentSessions();
    } catch {
      setRenameError("名称未保存，请检查连接后重试。");
    } finally {
      renamePending.current = false;
      setRenameSaving(false);
    }
  }

  async function deleteRecentSession(session: ChatSessionSummary) {
    setOpenMenuSessionId("");
    if (!window.confirm(`删除对话“${session.title || "新对话"}”？`)) return;
    try {
      await deleteSession(session.session_id);
      setRecentSessions((current) => current.filter((item) => item.session_id !== session.session_id));
      if (activeSessionId === session.session_id || window.localStorage.getItem("bee:conversationId") === session.session_id) {
        handleNewChat();
      }
      window.dispatchEvent(new CustomEvent("bee:conversation-updated", { detail: { sessionId: session.session_id } }));
    } catch {
      void refreshRecentSessions();
    }
  }

  return (
    <aside className={`sidebar ${mobileOpen ? "mobile-open" : ""}`}>
      <Link href="/chat" className="sidebar-brand" aria-label="Bee 首页">
        <span className="brand-mark">B</span>
        <span>
          <strong>Bee</strong>
          <small>知识工作空间</small>
        </span>
      </Link>
      <button className="sidebar-mobile-toggle" type="button" aria-label={mobileOpen ? "收起导航" : "展开导航"} aria-expanded={mobileOpen} onClick={() => setMobileOpen((open) => !open)}>
        {mobileOpen ? <PanelLeftClose size={20} /> : <PanelLeftOpen size={20} />}
      </button>

      <button type="button" className="new-thread" onClick={handleNewChat}>
        <Plus size={17} />
        新对话
      </button>

      <p className="sidebar-section-label">工作台</p>
      <nav className="sidebar-nav" aria-label="主导航">
        <button type="button" aria-current={pathname === "/chat" ? "page" : undefined} onClick={() => router.push("/chat")} className={pathname === "/chat" ? "active" : ""}>
          <MessageSquare size={17} />
          <span>对话</span>
        </button>
        <button type="button" aria-current={pathname.startsWith("/knowledge") ? "page" : undefined} onClick={() => router.push("/knowledge")} className={pathname.startsWith("/knowledge") ? "active" : ""}>
          <LibraryIcon />
          <span>知识库</span>
        </button>
      </nav>

      <section className="sidebar-recent-section" aria-label="最近对话">
        <p className="sidebar-section-label">最近对话</p>
        <div className="sidebar-recent-list">
          {recentSessions.map((session) => (
            <div
              key={session.session_id}
              ref={renamingSessionId === session.session_id ? renameRow : undefined}
              className={`sidebar-recent-row ${activeSessionId === session.session_id ? "active" : ""} ${openMenuSessionId === session.session_id ? "menu-open" : ""}`}
              onClick={(event) => event.stopPropagation()}
            >
              {renamingSessionId === session.session_id ? (
                <div className="sidebar-rename-editor" aria-busy={renameSaving}>
                  <input
                    className="sidebar-rename-input"
                    aria-label="对话名称"
                    aria-invalid={Boolean(renameError)}
                    aria-describedby={renameError ? "sidebar-rename-error" : undefined}
                    value={renameDraft}
                    autoFocus
                    readOnly={renameSaving}
                    onChange={(event) => { setRenameDraft(event.target.value); setRenameError(""); }}
                    onKeyDown={(event) => {
                      if (event.nativeEvent.isComposing || event.keyCode === 229) return;
                      if (event.key === "Enter") { event.preventDefault(); void commitRename(session); }
                      if (event.key === "Escape") {
                        event.preventDefault();
                        event.stopPropagation();
                        if (!renamePending.current) finishRename();
                      }
                    }}
                  />
                  <button type="button" disabled={renameSaving} aria-label="保存名称" title="保存名称" onClick={() => void commitRename(session)}><Check size={16} /></button>
                  <button type="button" disabled={renameSaving} aria-label="取消重命名" title="取消重命名" onClick={finishRename}><X size={16} /></button>
                  {renameError ? <p id="sidebar-rename-error" className="feedback-err" role="alert">{renameError}</p> : null}
                </div>
              ) : <button
                type="button"
                className="sidebar-recent-main"
                onClick={() => openConversation(session.session_id)}
                title={session.title || "新对话"}
              >
                <NewChatIcon />
                <span className="sidebar-recent-title">{session.title || "新对话"}</span>
                {session.is_running ? <span className="sidebar-running-dot" aria-label="正在进行中" title="正在进行中" /> : null}
              </button>}
              {renamingSessionId !== session.session_id ? <button
                type="button"
                className="sidebar-recent-more"
                disabled={renameSaving}
                aria-label={`${session.title || "新对话"} 更多操作`}
                title="更多"
                onClick={() => setOpenMenuSessionId((current) => current === session.session_id ? "" : session.session_id)}
              >
                <MoreIcon />
              </button> : null}
              {openMenuSessionId === session.session_id ? (
                <div className="sidebar-session-menu">
                  <button type="button" onClick={() => beginRename(session)}>
                    <EditIcon />
                    <span>重命名</span>
                  </button>
                  <button type="button" className="danger" onClick={() => void deleteRecentSession(session)}>
                    <DeleteIcon />
                    <span>删除</span>
                  </button>
                </div>
              ) : null}
            </div>
          ))}
        </div>
      </section>
    </aside>
  );
}
