"use client";

import { ChangeEvent, FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { AgentTimeline } from "../components/AgentTimeline";
import { DocumentViewer } from "../components/DocumentViewer";
import { LibraryIcon, SendIcon, ThumbsDownIcon, ThumbsUpIcon, UploadIcon } from "../components/Icons";
import { API_BASE, listKnowledgeBaseDocuments, listKnowledgeBases, uploadChatAttachment } from "../lib/api";
import { buildAgentTimeline, deriveAgentRunSummary, deriveSearchSummary, normalizeAgentPayload } from "../lib/agent-stream";
import type { AgentStreamEvent, ChatAttachment, ChatMessage, FeedbackState, KnowledgeBase, MemoryRecord, MemoryUpdate, ReasoningSummary, SourceItem } from "../lib/types";

type ChatMode = "quick" | "reasoning" | "wiki";

export default function ChatPage() {
  const endRef = useRef<HTMLDivElement | null>(null);
  const agentEventSequenceRef = useRef(0);
  const attachmentInputRef = useRef<HTMLInputElement | null>(null);

  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [feedbackMap, setFeedbackMap] = useState<Record<number, FeedbackState>>({});
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [chatMode, setChatMode] = useState<ChatMode>("quick");
  const [pendingAttachments, setPendingAttachments] = useState<ChatAttachment[]>([]);
  const [attachmentUploading, setAttachmentUploading] = useState(false);
  const [attachmentError, setAttachmentError] = useState("");
  const [memoryNotice, setMemoryNotice] = useState("");
  const [memoryPanelOpen, setMemoryPanelOpen] = useState(false);
  const [memories, setMemories] = useState<MemoryRecord[]>([]);
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [memoryError, setMemoryError] = useState("");
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [selectedKnowledgeBaseIds, setSelectedKnowledgeBaseIds] = useState<string[]>([]);
  const [suggestedQuestions, setSuggestedQuestions] = useState<string[]>([]);
  const [modeMenuOpen, setModeMenuOpen] = useState(false);
  const [knowledgeMenuOpen, setKnowledgeMenuOpen] = useState(false);

  const [docViewerOpen, setDocViewerOpen] = useState(false);
  const [docLoading, setDocLoading] = useState(false);
  const [docSource, setDocSource] = useState("");
  const [docContent, setDocContent] = useState("");
  const [docError, setDocError] = useState("");
  const [docMode, setDocMode] = useState<"text" | "pdf">("text");
  const [docFileUrl, setDocFileUrl] = useState("");

  const canSend = useMemo(() => input.trim().length > 0 && !loading && !attachmentUploading, [input, loading, attachmentUploading]);
  const knowledgeScopeLabel = useMemo(() => {
    if (!selectedKnowledgeBaseIds.length) return "默认知识库";
    const names = selectedKnowledgeBaseIds
      .map((id) => knowledgeBases.find((item) => item.id === id)?.name)
      .filter(Boolean) as string[];
    if (names.length <= 2) return names.join("、") || "默认知识库";
    return `${names[0]} 等 ${names.length} 个知识库`;
  }, [knowledgeBases, selectedKnowledgeBaseIds]);
  const activeSuggestionKnowledgeBaseIds = useMemo(() => {
    if (selectedKnowledgeBaseIds.length) return selectedKnowledgeBaseIds;
    return knowledgeBases.some((item) => item.id === "default-knowledge-base") ? ["default-knowledge-base"] : [];
  }, [knowledgeBases, selectedKnowledgeBaseIds]);

  function toggleKnowledgeBase(id: string) {
    setSelectedKnowledgeBaseIds((current) => current.includes(id) ? current.filter((item) => item !== id) : [...current, id]);
  }

  useEffect(() => {
    const saved = JSON.parse(window.localStorage.getItem("bee:selectedKnowledgeBaseIds") || "[]") as string[];
    const prefill = window.localStorage.getItem("bee:prefillQuestion") || "";
    setSelectedKnowledgeBaseIds(saved);
    if (prefill) {
      setInput(prefill);
      window.localStorage.removeItem("bee:prefillQuestion");
    }
    void listKnowledgeBases().then((items) => {
      setKnowledgeBases(items);
      setSelectedKnowledgeBaseIds((current) => current.filter((id) => items.some((item) => item.id === id)));
    }).catch(() => setKnowledgeBases([]));
  }, []);

  useEffect(() => {
    window.localStorage.setItem("bee:selectedKnowledgeBaseIds", JSON.stringify(selectedKnowledgeBaseIds));
  }, [selectedKnowledgeBaseIds]);

  useEffect(() => {
    let canceled = false;
    async function loadSuggestedQuestions() {
      if (!activeSuggestionKnowledgeBaseIds.length) {
        setSuggestedQuestions([]);
        return;
      }
      try {
        const batches = await Promise.all(
          activeSuggestionKnowledgeBaseIds.map((id) => listKnowledgeBaseDocuments(id)),
        );
        if (canceled) return;
        const unique: string[] = [];
        for (const document of batches.flat()) {
          for (const question of document.suggested_questions_json || []) {
            const cleanQuestion = question.trim();
            if (cleanQuestion && !unique.includes(cleanQuestion)) {
              unique.push(cleanQuestion);
            }
            if (unique.length >= 6) break;
          }
          if (unique.length >= 6) break;
        }
        setSuggestedQuestions(unique);
      } catch {
        if (!canceled) setSuggestedQuestions([]);
      }
    }
    void loadSuggestedQuestions();
    return () => {
      canceled = true;
    };
  }, [activeSuggestionKnowledgeBaseIds]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, loading]);

  useEffect(() => {
    function handleNewChat() {
      setMessages([]);
      setFeedbackMap({});
      setConversationId(null);
      setMemoryNotice("");
      setInput("");
      setPendingAttachments([]);
      setAttachmentError("");
      setChatMode("quick");
    }
    window.addEventListener("bee:new-chat", handleNewChat);
    return () => window.removeEventListener("bee:new-chat", handleNewChat);
  }, []);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const question = input.trim();
    if (!question) return;
    const submittedMode = chatMode;
    const submittedAttachments = pendingAttachments.map((item) => ({ id: item.id, filename: item.filename }));
    const submittedAttachmentIds = submittedAttachments.map((item) => item.id);

    setInput("");
    setLoading(true);
    setAttachmentError("");
    agentEventSequenceRef.current = 0;
    setMessages((prev) => [
      ...prev,
      { role: "user", content: question, chatMode: submittedMode, attachments: submittedAttachments },
      { role: "assistant", content: "", chatMode: submittedMode, sources: [], agentEvents: [], agentCompleted: false },
    ]);

    try {
      const res = await fetch(`${API_BASE}/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: question,
          conversation_id: conversationId || undefined,
          memory_enabled: true,
          temporary: false,
          chat_mode: submittedMode,
          knowledge_base_ids: selectedKnowledgeBaseIds.length ? selectedKnowledgeBaseIds : undefined,
          attachment_ids: submittedAttachmentIds.length ? submittedAttachmentIds : undefined,
        }),
      });
      if (!res.ok || !res.body) throw new Error(`请求失败: ${res.status}`);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split("\n\n");
        buffer = events.pop() || "";
        for (const item of events) {
          const line = item.split("\n").find((entry) => entry.startsWith("data:"));
          if (!line) continue;
          const payload = line.replace(/^data:\s*/, "");
          if (payload === "[DONE]") continue;
          const data = JSON.parse(payload) as {
            token?: string;
            error?: string;
            sources?: SourceItem[];
            reasoning?: ReasoningSummary;
            agent_trace?: Record<string, unknown>;
            tool_call?: Record<string, unknown>;
            tool_observation?: Record<string, unknown>;
            agent_query?: Record<string, unknown>;
            agent_thought?: Record<string, unknown>;
            agent_tool_call?: Record<string, unknown>;
            agent_tool_result?: Record<string, unknown>;
            agent_reflection?: Record<string, unknown>;
            agent_remedial_search?: Record<string, unknown>;
            agent_references?: Record<string, unknown>;
            agent_final_answer?: Record<string, unknown>;
            agent_complete?: Record<string, unknown>;
            agent_error?: Record<string, unknown>;
            evidence_summary?: Record<string, unknown>;
            citation_verification?: Record<string, unknown>;
            conversation_id?: string;
            memory_updated?: MemoryUpdate[];
          };
          if (data.error) {
            updateLastAssistant(`后端错误: ${data.error}`);
          } else if (data.conversation_id) {
            setConversationId(data.conversation_id);
          } else if (data.sources) {
            setMessages((prev) => {
              const next = [...prev];
              next[next.length - 1] = { ...next[next.length - 1], sources: data.sources };
              return next;
            });
          } else if (data.reasoning) {
            setMessages((prev) => {
              const next = [...prev];
              next[next.length - 1] = { ...next[next.length - 1], reasoning: data.reasoning };
              return next;
            });
          } else if (data.agent_trace) {
            appendAgentEvent(normalizeAgentPayload("agent_trace", data.agent_trace, nextAgentEventSequence()));
          } else if (data.agent_query) {
            appendAgentEvent(normalizeAgentPayload("agent_query", data.agent_query, nextAgentEventSequence()));
          } else if (data.agent_thought) {
            appendAgentEvent(normalizeAgentPayload("agent_thought", data.agent_thought, nextAgentEventSequence()));
          } else if (data.agent_tool_call) {
            appendAgentEvent(normalizeAgentPayload("agent_tool_call", data.agent_tool_call, nextAgentEventSequence()));
          } else if (data.agent_tool_result) {
            appendAgentEvent(normalizeAgentPayload("agent_tool_result", data.agent_tool_result, nextAgentEventSequence()));
          } else if (data.agent_reflection) {
            appendAgentEvent(normalizeAgentPayload("agent_reflection", data.agent_reflection, nextAgentEventSequence()));
          } else if (data.agent_remedial_search) {
            appendAgentEvent(normalizeAgentPayload("agent_remedial_search", data.agent_remedial_search, nextAgentEventSequence()));
          } else if (data.agent_references) {
            appendAgentEvent(normalizeAgentPayload("agent_references", data.agent_references, nextAgentEventSequence()));
          } else if (data.agent_final_answer) {
            appendAgentEvent(normalizeAgentPayload("agent_final_answer", data.agent_final_answer, nextAgentEventSequence()));
          } else if (data.agent_complete) {
            appendAgentEvent(normalizeAgentPayload("agent_complete", data.agent_complete, nextAgentEventSequence()));
          } else if (data.agent_error) {
            appendAgentEvent(normalizeAgentPayload("agent_error", data.agent_error, nextAgentEventSequence()));
          } else if (data.tool_call) {
            appendAgentEvent(normalizeAgentPayload("tool_call", data.tool_call, nextAgentEventSequence()));
          } else if (data.tool_observation) {
            appendAgentEvent(normalizeAgentPayload("tool_observation", data.tool_observation, nextAgentEventSequence()));
          } else if (data.evidence_summary) {
            setMessages((prev) => {
              const next = [...prev];
              next[next.length - 1] = { ...next[next.length - 1], evidenceSummary: data.evidence_summary };
              return next;
            });
            appendAgentEvent(normalizeAgentPayload("evidence_summary", data.evidence_summary, nextAgentEventSequence()));
          } else if (data.citation_verification) {
            setMessages((prev) => {
              const next = [...prev];
              next[next.length - 1] = { ...next[next.length - 1], citationVerification: data.citation_verification };
              return next;
            });
            appendAgentEvent(normalizeAgentPayload("citation_verification", data.citation_verification, nextAgentEventSequence()));
          } else if (data.token) {
            setMessages((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              next[next.length - 1] = { ...last, content: `${last.content}${data.token}` };
              return next;
            });
          } else if (data.memory_updated?.length) {
            const first = data.memory_updated[0];
            setMemoryNotice(`已记住：${first.content}`);
            void loadMemories();
          }
        }
      }
      markLastAssistantCompleted();
    } catch (err) {
      updateLastAssistant(`请求异常: ${err instanceof Error ? err.message : "unknown error"}`);
    } finally {
      if (submittedAttachmentIds.length) {
        setPendingAttachments((prev) => prev.filter((item) => !submittedAttachmentIds.includes(item.id)));
      }
      setLoading(false);
    }
  }

  function nextAgentEventSequence(): number {
    agentEventSequenceRef.current += 1;
    return agentEventSequenceRef.current;
  }

  function appendAgentEvent(event: AgentStreamEvent) {
    setMessages((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      const agentEvents = [...(last.agentEvents || []), event];
      const agentTimeline = buildAgentTimeline(agentEvents);
      next[next.length - 1] = {
        ...last,
        agentEvents,
        agentTimeline,
        agentSummary: deriveAgentRunSummary(agentEvents, agentTimeline, Boolean(last.agentCompleted)),
      };
      return next;
    });
  }

  function markLastAssistantCompleted() {
    setMessages((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (!last || last.role !== "assistant") return next;
      const agentEvents = last.agentEvents || [];
      const agentTimeline = last.agentTimeline || buildAgentTimeline(agentEvents);
      next[next.length - 1] = {
        ...last,
        agentCompleted: true,
        agentTimeline,
        agentSummary: agentEvents.length ? deriveAgentRunSummary(agentEvents, agentTimeline, true) : last.agentSummary,
      };
      return next;
    });
  }

  async function handleAttachmentChange(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (!files.length) return;
    setAttachmentUploading(true);
    setAttachmentError("");
    try {
      const uploaded: ChatAttachment[] = [];
      for (const file of files.slice(0, 5)) {
        uploaded.push(await uploadChatAttachment(file));
      }
      setPendingAttachments((prev) => [...prev, ...uploaded].slice(-5));
    } catch (err) {
      setAttachmentError(err instanceof Error ? err.message : "附件上传失败");
    } finally {
      setAttachmentUploading(false);
    }
  }

  function removePendingAttachment(id: string) {
    setPendingAttachments((prev) => prev.filter((item) => item.id !== id));
  }

  function applySuggestedQuestion(question: string) {
    setInput(question);
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    if (canSend) {
      event.currentTarget.form?.requestSubmit();
    }
  }

  function updateLastAssistant(content: string) {
    setMessages((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      next[next.length - 1] = { ...last, role: "assistant", content };
      return next;
    });
  }

  function getFeedback(index: number): FeedbackState {
    return feedbackMap[index] || { status: "idle", draft: "" };
  }

  function findQuestionForAssistant(index: number): string {
    for (let i = index - 1; i >= 0; i -= 1) {
      if (messages[i]?.role === "user") return messages[i].content;
    }
    return "";
  }

  async function submitCorrection(index: number) {
    const fb = getFeedback(index);
    const answer = fb.draft.trim();
    const question = findQuestionForAssistant(index).trim();
    if (!question || !answer) {
      setFeedbackMap((prev) => ({ ...prev, [index]: { ...fb, status: "error", error: "请先填写纠正答案。" } }));
      return;
    }
    if (selectedKnowledgeBaseIds.length > 1) {
      setFeedbackMap((prev) => ({
        ...prev,
        [index]: { ...fb, status: "error", error: "多知识库回答需要先将范围缩小到一个目标知识库再保存修正。" },
      }));
      return;
    }
    setFeedbackMap((prev) => ({ ...prev, [index]: { ...fb, status: "submitting", error: undefined } }));
    try {
      const res = await fetch(`${API_BASE}/feedback/answer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          answer,
          knowledge_base_id: selectedKnowledgeBaseIds.length === 1 ? selectedKnowledgeBaseIds[0] : undefined,
          knowledge_base_ids: selectedKnowledgeBaseIds.length ? selectedKnowledgeBaseIds : undefined,
        }),
      });
      const data = (await res.json()) as { title?: string; source?: string; detail?: string };
      if (!res.ok) throw new Error(data.detail || `请求失败: ${res.status}`);
      setFeedbackMap((prev) => ({
        ...prev,
        [index]: { ...fb, status: "saved", savedTitle: data.title || "", savedSource: data.source || "" },
      }));
    } catch (err) {
      setFeedbackMap((prev) => ({ ...prev, [index]: { ...fb, status: "error", error: err instanceof Error ? err.message : "unknown error" } }));
    }
  }

  async function loadMemories() {
    setMemoryLoading(true);
    setMemoryError("");
    try {
      const res = await fetch(`${API_BASE}/memories`);
      const data = (await res.json()) as { items?: MemoryRecord[]; detail?: string };
      if (!res.ok) throw new Error(data.detail || `加载记忆失败: ${res.status}`);
      setMemories(data.items || []);
    } catch (err) {
      setMemoryError(err instanceof Error ? err.message : "unknown error");
    } finally {
      setMemoryLoading(false);
    }
  }

  async function openMemoryPanel() {
    setMemoryPanelOpen(true);
    await loadMemories();
  }

  async function deleteMemory(id: string) {
    try {
      const res = await fetch(`${API_BASE}/memories/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`删除记忆失败: ${res.status}`);
      setMemories((prev) => prev.filter((item) => item.id !== id));
    } catch (err) {
      setMemoryError(err instanceof Error ? err.message : "unknown error");
    }
  }

  async function handleOpenDoc(source: string) {
    const cleanSource = source.split(" 路径")[0];
    setDocViewerOpen(true);
    setDocLoading(true);
    setDocSource(cleanSource);
    setDocContent("");
    setDocError("");
    setDocFileUrl("");

    if (cleanSource.toLowerCase().endsWith(".pdf")) {
      setDocMode("pdf");
      setDocFileUrl(`${API_BASE}/documents/file?source=${encodeURIComponent(cleanSource)}`);
      setDocLoading(false);
      return;
    }

    setDocMode("text");
    try {
      const res = await fetch(`${API_BASE}/documents/content?source=${encodeURIComponent(cleanSource)}`);
      const data = (await res.json()) as { content?: string; detail?: string };
      if (!res.ok) throw new Error(data.detail || `请求失败: ${res.status}`);
      setDocContent(data.content || "");
    } catch (err) {
      setDocError(err instanceof Error ? err.message : "unknown error");
    } finally {
      setDocLoading(false);
    }
  }

  return (
    <section className={`chat-page ${messages.length === 0 ? "empty-state" : ""}`}>
      <div className="chat-thread">
        {memoryNotice ? <div className="memory-notice">{memoryNotice}</div> : null}
        {messages.length === 0 ? (
          <div className="empty-chat">
            <h1>Hi，我是 Bee，让你的知识触手可及</h1>
            {suggestedQuestions.length ? (
              <>
                <p>你可以这样问我</p>
                <div className="suggested-question-list">
                  {suggestedQuestions.map((question) => (
                    <button key={question} type="button" onClick={() => applySuggestedQuestion(question)}>
                      {question}
                    </button>
                  ))}
                </div>
              </>
            ) : null}
          </div>
        ) : null}

        {messages.map((message, index) => {
          const fb = getFeedback(index);
          const isStreamingAssistant = loading && index === messages.length - 1 && !message.agentCompleted;
          const canFeedback = message.role === "assistant" && message.content.trim().length > 0 && Boolean(message.agentCompleted) && !isStreamingAssistant;
          return (
            <article key={index} className={`message-row ${message.role}`}>
              <div className="message-content">
                {message.agentEvents?.length ? (
                  <AgentTimeline message={message} streaming={loading && index === messages.length - 1 && !message.agentCompleted} />
                ) : null}

                <div className="message-bubble">
                  {message.role === "assistant" ? (
                    <AssistantMarkdown content={cleanAssistantContent(message.content) || (loading && index === messages.length - 1 ? "正在检索知识库..." : "")} />
                  ) : (
                    message.content
                  )}
                </div>

                {message.role === "user" && (message.chatMode || message.attachments?.length) ? (
                  <div className="message-meta-row">
                    {message.chatMode ? <span>{chatModeLabel(message.chatMode)}</span> : null}
                    {message.attachments?.map((attachment) => (
                      <span key={attachment.id}>附件：{attachment.filename}</span>
                    ))}
                  </div>
                ) : null}

                {message.role === "assistant" && !message.agentEvents?.length ? (
                  <SearchSummary message={message} streaming={loading && index === messages.length - 1 && !message.agentCompleted} />
                ) : null}

                {message.reasoning && !message.agentEvents?.length ? <ReasoningPanel reasoning={message.reasoning} /> : null}

                {canFeedback ? (
                  <section className={`feedback-panel ${["disliked", "submitting", "saved", "error"].includes(fb.status) ? "is-open" : ""}`}>
                    <div className="feedback-top">
                      <span className="sr-only">回答反馈</span>
                      <div className="feedback-actions">
                        <button
                          type="button"
                          className={fb.status === "liked" ? "active" : ""}
                          onClick={() => setFeedbackMap((prev) => ({ ...prev, [index]: { ...fb, status: "liked" } }))}
                          aria-label="回答有帮助"
                          aria-pressed={fb.status === "liked"}
                          title="有帮助"
                        >
                          <ThumbsUpIcon className="feedback-icon" />
                        </button>
                        <button
                          type="button"
                          className={["disliked", "submitting", "saved", "error"].includes(fb.status) ? "active" : ""}
                          onClick={() => setFeedbackMap((prev) => ({ ...prev, [index]: { ...fb, status: "disliked" } }))}
                          aria-label="回答需要修正"
                          aria-pressed={["disliked", "submitting", "saved", "error"].includes(fb.status)}
                          title="需要修正"
                        >
                          <ThumbsDownIcon className="feedback-icon" />
                        </button>
                      </div>
                    </div>
                    {["disliked", "submitting", "saved", "error"].includes(fb.status) ? (
                      <div className="feedback-form">
                        <textarea
                          value={fb.draft}
                          onChange={(event) => setFeedbackMap((prev) => ({ ...prev, [index]: { ...fb, draft: event.target.value, status: fb.status === "error" ? "disliked" : fb.status } }))}
                          disabled={fb.status === "submitting" || fb.status === "saved"}
                          placeholder="写下更准确的答案，Bee 会把它保存到反馈知识库。"
                        />
                        <div className="feedback-submit-line">
                          <button type="button" onClick={() => submitCorrection(index)} disabled={fb.status === "submitting" || fb.status === "saved"}>
                            {fb.status === "submitting" ? "保存中..." : "保存修正"}
                          </button>
                        </div>
                        {fb.status === "saved" ? <p className="feedback-ok">已保存为 {fb.savedTitle}（{fb.savedSource}）。</p> : null}
                        {fb.status === "error" && fb.error ? <p className="feedback-err">提交失败: {fb.error}</p> : null}
                      </div>
                    ) : null}
                  </section>
                ) : null}
              </div>
            </article>
          );
        })}
        <div ref={endRef} />
      </div>

      <form className="chat-composer" onSubmit={handleSubmit}>
        <textarea
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={handleComposerKeyDown}
          placeholder="直接向知识库提问"
          rows={1}
        />
        {pendingAttachments.length || attachmentError ? (
          <div className="composer-attachments">
            {pendingAttachments.map((attachment) => (
              <span key={attachment.id}>
                {attachment.filename}
                <button type="button" onClick={() => removePendingAttachment(attachment.id)} aria-label={`移除 ${attachment.filename}`}>
                  ×
                </button>
              </span>
            ))}
            {attachmentError ? <strong>{attachmentError}</strong> : null}
          </div>
        ) : null}
        <div className="composer-toolbar">
          <div
            className={`composer-mode-select ${modeMenuOpen ? "open" : ""}`}
            onBlur={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setModeMenuOpen(false);
            }}
          >
            <span className="sr-only">回答模式</span>
            <button
              type="button"
              className="composer-mode-trigger"
              aria-haspopup="menu"
              aria-expanded={modeMenuOpen}
              aria-label="回答模式"
              onClick={() => setModeMenuOpen((open) => !open)}
            >
              {chatModeLabel(chatMode)}
            </button>
            {modeMenuOpen ? (
              <div className="composer-mode-menu" role="menu">
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={chatMode === "quick"}
                  className={chatMode === "quick" ? "active" : ""}
                  onClick={() => {
                    setChatMode("quick");
                    setModeMenuOpen(false);
                  }}
                >
                  快速问答
                </button>
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={chatMode === "reasoning"}
                  className={chatMode === "reasoning" ? "active" : ""}
                  onClick={() => {
                    setChatMode("reasoning");
                    setModeMenuOpen(false);
                  }}
                >
                  智能推理
                </button>
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={chatMode === "wiki"}
                  className={chatMode === "wiki" ? "active" : ""}
                  onClick={() => {
                    setChatMode("wiki");
                    setModeMenuOpen(false);
                  }}
                >
                  Wiki 问答
                </button>
              </div>
            ) : null}
          </div>
          <button
            type="button"
            className="composer-icon-button"
            onClick={() => attachmentInputRef.current?.click()}
            disabled={attachmentUploading}
            aria-label={attachmentUploading ? "上传中" : "上传文档"}
            title={attachmentUploading ? "上传中" : "上传文档"}
          >
            <UploadIcon className="composer-icon" />
            <span className="composer-tooltip">{attachmentUploading ? "上传中" : "上传文档"}</span>
          </button>
          <input ref={attachmentInputRef} className="chat-attachment-input" type="file" multiple onChange={handleAttachmentChange} />
          <div
            className={`kb-scope-selector ${knowledgeMenuOpen ? "open" : ""}`}
            onBlur={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setKnowledgeMenuOpen(false);
            }}
          >
            <button
              type="button"
              className="kb-scope-trigger"
              aria-haspopup="menu"
              aria-expanded={knowledgeMenuOpen}
              aria-label={`选择知识库，当前：${knowledgeScopeLabel}`}
              title={`知识库：${knowledgeScopeLabel}`}
              onClick={() => setKnowledgeMenuOpen((open) => !open)}
            >
              <LibraryIcon className="composer-icon" />
              <span className="sr-only">知识库：{knowledgeScopeLabel}</span>
              <span className="composer-tooltip">知识库：{knowledgeScopeLabel}</span>
            </button>
            {knowledgeMenuOpen ? (
              <div className="kb-scope-menu" role="menu">
                <button
                  type="button"
                  className={!selectedKnowledgeBaseIds.length ? "active" : ""}
                  onClick={() => {
                    setSelectedKnowledgeBaseIds([]);
                    setKnowledgeMenuOpen(false);
                  }}
                >
                  默认知识库
                </button>
                {knowledgeBases.map((item) => (
                  <label key={item.id}>
                    <input type="checkbox" checked={selectedKnowledgeBaseIds.includes(item.id)} onChange={() => toggleKnowledgeBase(item.id)} />
                    <span>{item.name}</span>
                  </label>
                ))}
              </div>
            ) : null}
          </div>
          <button type="submit" className="composer-send" disabled={!canSend} aria-label="发送">
            <SendIcon />
          </button>
        </div>
      </form>

      {memoryPanelOpen ? (
        <div className="memory-panel-mask" role="presentation" onClick={() => setMemoryPanelOpen(false)}>
          <section className="memory-panel" role="dialog" aria-modal="true" aria-label="记忆" onClick={(event) => event.stopPropagation()}>
            <header>
              <h2>记忆</h2>
              <button type="button" onClick={() => setMemoryPanelOpen(false)}>
                关闭
              </button>
            </header>
            {memoryLoading ? <p className="memory-muted">加载中...</p> : null}
            {memoryError ? <p className="feedback-err">加载失败: {memoryError}</p> : null}
            {!memoryLoading && !memories.length ? <p className="memory-muted">暂无记忆</p> : null}
            <ul className="memory-list">
              {memories.map((memory) => (
                <li key={memory.id}>
                  <div>
                    <strong>{memory.type}</strong>
                    <p>{memory.content}</p>
                  </div>
                  <button type="button" onClick={() => deleteMemory(memory.id)}>
                    删除
                  </button>
                </li>
              ))}
            </ul>
          </section>
        </div>
      ) : null}

      <DocumentViewer
        open={docViewerOpen}
        source={docSource}
        loading={docLoading}
        error={docError}
        mode={docMode}
        content={docContent}
        fileUrl={docFileUrl}
        onClose={() => setDocViewerOpen(false)}
      />
    </section>
  );
}

function SearchSummary({ message, streaming }: { message: ChatMessage; streaming: boolean }) {
  const summary = deriveSearchSummary(message, streaming);
  if (summary.status === "empty" && !streaming) return null;
  return (
    <div className={`search-summary status-${summary.status}`}>
      <span className="search-summary-icon" aria-hidden="true">{summaryIcon(summary.status)}</span>
      <span>{summary.label}</span>
    </div>
  );
}

function summaryIcon(status: ReturnType<typeof deriveSearchSummary>["status"]): string {
  if (status === "searching") return "⌕";
  if (status === "citation_failed") return "!";
  if (status === "insufficient") return "?";
  return "✓";
}

function chatModeLabel(mode: ChatMode): string {
  if (mode === "reasoning") return "智能推理";
  if (mode === "wiki") return "Wiki 问答";
  return "快速问答";
}

const markdownComponents: Components = {
  table({ children }) {
    return (
      <div className="markdown-table-wrap">
        <table>{children}</table>
      </div>
    );
  },
  code({ className, children, ...props }) {
    const code = String(children).replace(/\n$/, "");
    const language = /language-(\w+)/.exec(className || "")?.[1];
    if (!language) {
      return <code className={className} {...props}>{children}</code>;
    }
    return <CodeBlock code={code} language={language} />;
  },
};

function AssistantMarkdown({ content }: { content: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{content}</ReactMarkdown>
    </div>
  );
}

function cleanAssistantContent(content: string): string {
  return content
    .replace(/(?:^|\n)如果需要(?:更|进一步)?详细(?:的)?(?:信息|建议|说明)[^。！？\n]*[。！？]?/g, "")
    .replace(/(?:^|\n)如需(?:更多|进一步|更详细)[^。！？\n]*[。！？]?/g, "")
    .replace(/(?:^|\n)请提供更多(?:具体)?信息[。！？]?/g, "")
    .trim();
}

function CodeBlock({ code, language }: { code: string; language?: string }) {
  const [copied, setCopied] = useState(false);
  const label = language ? languageLabel(language) : "Text";

  async function copyCode() {
    try {
      await navigator.clipboard?.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="code-block">
      <div className="code-block-header">
        <span>{label}</span>
        <button type="button" onClick={copyCode}>{copied ? "已复制" : "复制代码"}</button>
      </div>
      <pre>
        <code className={language ? `language-${language}` : undefined}>{code}</code>
      </pre>
    </div>
  );
}

function languageLabel(language: string): string {
  const labels: Record<string, string> = {
    bash: "Bash",
    sh: "Shell",
    shell: "Shell",
    powershell: "PowerShell",
    ps1: "PowerShell",
    yaml: "YAML",
    yml: "YAML",
    json: "JSON",
    python: "Python",
    py: "Python",
    text: "Text",
    txt: "Text",
  };
  return labels[language.toLowerCase()] || language.toUpperCase();
}

function ReasoningPanel({ reasoning }: { reasoning: ReasoningSummary }) {
  const hasMappings = reasoning.term_mappings?.length > 0;
  const hasQueries = reasoning.retrieval_queries?.length > 0;
  const hasEvidence = reasoning.evidence?.length > 0;

  return (
    <details className="reasoning-panel">
      <summary>
        <span>检索推理详情</span>
        <small>可审计 RAG 摘要</small>
      </summary>
      <div className="reasoning-body">
        <div className="reasoning-step">
          <strong>问题理解</strong>
          <p>{reasoning.normalized_query && reasoning.normalized_query !== reasoning.question ? `将问题理解为：${reasoning.normalized_query}` : `按原问题检索：${reasoning.question}`}</p>
        </div>

        {hasMappings ? (
          <div className="reasoning-step">
            <strong>术语归一</strong>
            <div className="reasoning-tags">
              {reasoning.term_mappings.map((item) => (
                <span key={item}>{item}</span>
              ))}
            </div>
          </div>
        ) : null}

        {hasQueries ? (
          <div className="reasoning-step">
            <strong>实际检索词</strong>
            <div className="reasoning-tags">
              {reasoning.retrieval_queries.map((item) => (
                <span key={item}>{item}</span>
              ))}
            </div>
          </div>
        ) : null}

        {hasEvidence ? (
          <div className="reasoning-step">
            <strong>命中依据</strong>
            <ul className="reasoning-evidence">
              {reasoning.evidence.map((item, index) => (
                <li key={`${item.source}-${index}`}>
                  <div>
                    <b>{item.source}</b>
                    {typeof item.score === "number" ? <em>相关度 {item.score.toFixed(3)}</em> : null}
                  </div>
                  {item.title_path ? <p>{item.title_path}</p> : null}
                  {item.preview ? <p>{item.preview}</p> : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {reasoning.summary ? <p className="reasoning-note">{reasoning.summary}</p> : null}
      </div>
    </details>
  );
}


