"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  buildAgentTimeline,
  deriveAgentRunSummary,
  formatElapsed,
  statusLabel,
} from "../lib/agent-stream";
import type { AgentRunSummary, AgentTimelineStep, ChatMessage } from "../lib/types";

export function AgentTimeline({ message, streaming }: { message: ChatMessage; streaming: boolean }) {
  const events = message.agentEvents || [];
  const [expanded, setExpanded] = useState(true);
  const wasStreaming = useRef(streaming);
  const steps = useMemo(() => message.agentTimeline || buildAgentTimeline(events), [events, message.agentTimeline]);
  const summary = useMemo(
    () => message.agentSummary || deriveAgentRunSummary(events, steps, Boolean(message.agentCompleted)),
    [events, message.agentCompleted, message.agentSummary, steps],
  );

  useEffect(() => {
    if (wasStreaming.current && !streaming && message.agentCompleted) setExpanded(false);
    wasStreaming.current = streaming;
  }, [message.agentCompleted, streaming]);

  if (!events.length) return null;
  const open = streaming || expanded;
  const visualStatus = streaming || summary.status === "running" ? "running" : summary.status;

  return (
    <section className={`agent-timeline status-${visualStatus}`}>
      <button className="agent-timeline-header" type="button" onClick={() => setExpanded((value) => !value)} aria-expanded={open}>
        <span className="agent-timeline-spark" aria-hidden="true">
          {visualStatus === "running" ? <span className="agent-spinner" /> : summaryIcon(summary)}
        </span>
        <strong>{summaryTitle(summary, streaming)}</strong>
        {visibleStatusLabel(summary.status) ? <small>{visibleStatusLabel(summary.status)}</small> : null}
      </button>

      {open ? (
        <div className="agent-timeline-body">
          <ol className="agent-timeline-list">
            {steps.map((step, index) => (
              <AgentTimelineStepView key={`${step.id}-${step.kind}-${index}`} step={step} last={index === steps.length - 1} />
            ))}
          </ol>
          <p className="agent-timeline-note">可审计执行摘要，不展示隐藏推理链。</p>
        </div>
      ) : null}
    </section>
  );
}

function AgentTimelineStepView({ step, last }: { step: AgentTimelineStep; last: boolean }) {
  const allTitles = uniqueStrings((step.sourceTitles || []).map(sourceTitle).filter(Boolean));
  const titles = allTitles.slice(0, 4);
  const overflow = Math.max(0, allTitles.length - titles.length);
  const detail = step.detail || step.summary;
  const status = visibleStatusLabel(step.status);
  const referenceCount = referenceVisibleCount(step, allTitles);

  return (
    <li className={`agent-timeline-step status-${step.status} ${last ? "last" : ""}`}>
      <span className="agent-step-rail" aria-hidden="true" />
      <span className="agent-step-icon" aria-hidden="true">
        {step.status === "running" ? <span className="agent-spinner" /> : <span className="agent-step-icon-mark">{stepIcon(step)}</span>}
      </span>
      <div className="agent-step-card">
        <div className="agent-step-head">
          <strong>{step.title}</strong>
          {status ? <em>{status}</em> : null}
        </div>
        {detail ? <p>{detail}</p> : null}
        {step.elapsedMs !== undefined ? <small className="agent-step-elapsed">耗时 {formatElapsed(step.elapsedMs)}</small> : null}
        {step.kind === "references" && titles.length ? (
          <details className="agent-step-references">
            <summary>
              <span>{referenceLabel(step, referenceCount)}</span>
              <span className="agent-reference-caret" aria-hidden="true">⌄</span>
            </summary>
            <ul>
              {titles.map((title) => (
                <li key={title}>{title}</li>
              ))}
              {overflow ? <li>另有 {overflow} 个来源</li> : null}
            </ul>
          </details>
        ) : titles.length ? (
          <div className="agent-step-chips">
            {titles.map((title) => (
              <span key={title}>{title}</span>
            ))}
            {overflow ? <span>+{overflow}</span> : null}
          </div>
        ) : null}
      </div>
    </li>
  );
}

function referenceLabel(step: AgentTimelineStep, visibleCount?: number): string {
  const count = visibleCount ?? referenceVisibleCount(step);
  return count ? `引用 ${count} 个来源` : "查看引用来源";
}

function referenceVisibleCount(step: AgentTimelineStep, visibleTitles?: string[]): number {
  const titles = visibleTitles ?? uniqueStrings((step.sourceTitles || []).map(sourceTitle).filter(Boolean));
  return titles.length || step.counts?.citations || step.sourceChunkIds.length;
}

function visibleStatusLabel(status: AgentTimelineStep["status"]): string | null {
  return status === "completed" ? null : statusLabel(status);
}

function sourceTitle(source: string): string {
  const normalized = source.replace(/\\/g, "/").replace(/\/+$/, "");
  const title = normalized.split("/").pop() || "";
  if (isOpaqueInternalId(title)) return "";
  return title;
}

function isOpaqueInternalId(value: string): boolean {
  const normalized = value.trim();
  if (!normalized || normalized.toLowerCase() === "unknown") return true;
  if (/^[a-f0-9]{24,}$/i.test(normalized)) return true;
  if (/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(normalized)) return true;
  return false;
}

function uniqueStrings(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => value.trim()).filter(Boolean)));
}

function summaryTitle(summary: AgentRunSummary, streaming: boolean): string {
  if (streaming || summary.status === "running") {
    return `执行中 ${summary.completedSteps}/${summary.totalSteps} 个步骤，耗时 ${formatElapsed(summary.elapsedMs)}`;
  }
  if (summary.status === "failed") {
    return `执行失败 ${summary.completedSteps}/${summary.totalSteps} 个步骤，耗时 ${formatElapsed(summary.elapsedMs)}`;
  }
  if (summary.status === "partial") {
    return `部分完成 ${summary.completedSteps}/${summary.totalSteps} 个步骤，耗时 ${formatElapsed(summary.elapsedMs)}`;
  }
  if (!summary.toolCalls) {
    return `快速检索 ${summary.completedSteps}/${summary.totalSteps} 个步骤，耗时 ${formatElapsed(summary.elapsedMs)}`;
  }
  if (summary.remedialUsed) {
    return `思考 ${summary.reasoningRounds || 1} 轮 · 调用 ${summary.toolCalls || 0} 次工具 · 含补救检索 · 耗时 ${formatElapsed(summary.elapsedMs)}`;
  }
  return `思考 ${summary.reasoningRounds || 1} 轮 · 调用 ${summary.toolCalls || 0} 次工具 · 耗时 ${formatElapsed(summary.elapsedMs)}`;
}

function summaryIcon(summary: AgentRunSummary): string {
  if (summary.status === "running") return "";
  if (summary.status === "completed" || summary.status === "partial") return "✓";
  if (summary.status === "failed") return "!";
  return "✓";
}

function stepIcon(step: AgentTimelineStep): string {
  if (step.status === "running") return "";
  if (step.status === "failed") return "!";
  if (step.status === "skipped") return "-";
  if (step.kind === "reflection") return "?";
  if (step.kind === "references") return "#";
  if (step.kind === "answer") return "A";
  return "✓";
}
