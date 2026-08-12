from __future__ import annotations

import json
import logging
import os
import re
import traceback
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.services.processing.processing_span_tracker import (
    SPAN_SUBSPAN,
    ProcessingSpan,
    ProcessingSpanTracker,
    STATUS_DONE,
)
from app.services.infrastructure.observability import ObservationHandle, get_observability_sink, use_observability_trace

logger = logging.getLogger(__name__)


TRACE_SCHEMA_VERSION = "20260718_processing_trace_v1"
TRACE_STAGE_MAP = {
    "load": "docreader",
    "chunk_strategy": "chunking",
    "index": "embedding",
    "multimodal": "multimodal",
    "postprocess": "postprocess",
}


class ProcessingTraceRecorder:
    def __init__(
        self,
        root_dir: Path | str,
        *,
        enabled: bool = True,
        langfuse_enabled: bool = False,
        langfuse_host: str | None = None,
        langfuse_public_key: str | None = None,
        langfuse_secret_key: str | None = None,
        span_tracker: ProcessingSpanTracker | None = None,
    ):
        self.root_dir = Path(root_dir)
        self.enabled = enabled
        self.langfuse_enabled = langfuse_enabled
        self.langfuse_host = langfuse_host
        self.langfuse_public_key = langfuse_public_key
        self.langfuse_secret_key = langfuse_secret_key
        self.span_tracker = span_tracker or ProcessingSpanTracker.disabled()

    @classmethod
    def from_env(cls, root_dir: Path | str, *, span_tracker: ProcessingSpanTracker | None = None) -> "ProcessingTraceRecorder":
        return cls(
            root_dir,
            enabled=_env_bool("PROCESSING_TRACE_ENABLED", True),
            span_tracker=span_tracker,
        )

    def start(
        self,
        *,
        name: str,
        doc_id: str,
        file_name: str,
        source: str,
        scope: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> "ProcessingTrace":
        if not self.enabled:
            return ProcessingTrace.disabled()
        trace_id = uuid4().hex
        created = _utc_now()
        trace_dir = self.root_dir / created[:10].replace("-", "") / f"{_safe_name(file_name)}-{trace_id[:12]}"
        trace = ProcessingTrace(
            recorder=self,
            trace_id=trace_id,
            trace_dir=trace_dir,
            name=name,
            doc_id=doc_id,
            file_name=file_name,
            source=source,
            scope=scope,
            metadata=metadata or {},
            started_at=created,
        )
        trace.start()
        return trace

    def flush_langfuse(self) -> None:
        get_observability_sink().flush()


class ProcessingTrace:
    def __init__(
        self,
        *,
        recorder: ProcessingTraceRecorder | None,
        trace_id: str,
        trace_dir: Path,
        name: str,
        doc_id: str,
        file_name: str,
        source: str,
        scope: dict[str, Any],
        metadata: dict[str, Any],
        started_at: str,
        enabled: bool = True,
    ):
        self.recorder = recorder
        self.trace_id = trace_id
        self.trace_dir = trace_dir
        self.name = name
        self.doc_id = doc_id
        self.file_name = file_name
        self.source = source
        self.scope = scope
        self.metadata = metadata
        self.started_at = started_at
        self.ended_at = ""
        self.status = "running"
        self.error: dict[str, Any] | None = None
        self.spans: list[dict[str, Any]] = []
        self.enabled = enabled
        self._start_clock = perf_counter()
        self._langfuse_trace: ObservationHandle | None = None
        self._db_root: ProcessingSpan | None = None
        self._db_attempt = 0
        self._db_stage_by_file_span: dict[str, ProcessingSpan] = {}
        self._skipped_db_span_ids: set[str] = set()

    @classmethod
    def disabled(cls) -> "ProcessingTrace":
        return cls(
            recorder=None,
            trace_id="",
            trace_dir=Path(),
            name="",
            doc_id="",
            file_name="",
            source="",
            scope={},
            metadata={},
            started_at="",
            enabled=False,
        )

    def start(self) -> None:
        if not self.enabled:
            return
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        if self.recorder:
            self._langfuse_trace = get_observability_sink().start_span(
                name="document.processing",
                input={"source": self.source, "file_name": self.file_name},
                metadata={
                    "processing_trace_id": self.trace_id,
                    "doc_id": self.doc_id,
                    "scope": self.scope,
                    "trace_dir": str(self.trace_dir),
                    **self.metadata,
                },
            )
        if self.recorder is not None and self.recorder.span_tracker.enabled:
            self._db_root, self._db_attempt = self.recorder.span_tracker.open_attempt(
                knowledge_id=self.doc_id,
                input={"source": self.source, "file_name": self.file_name},
                metadata={
                    "trace_id": self.trace_id,
                    "trace_dir": str(self.trace_dir),
                    "scope": self.scope,
                    **self.metadata,
                },
            )
        self.flush()

    def reassign_doc_id(self, doc_id: str) -> None:
        if not self.enabled or not doc_id or doc_id == self.doc_id:
            return
        old_doc_id = self.doc_id
        self.doc_id = doc_id
        if self.recorder is not None and self._db_attempt:
            self.recorder.span_tracker.reassign_knowledge_id(old_doc_id, doc_id, self._db_attempt)
            if self._db_root is not None:
                self._db_root = ProcessingSpan(
                    doc_id,
                    self._db_root.attempt,
                    self._db_root.span_id,
                    self._db_root.name,
                    self._db_root.kind,
                    self._db_root.parent_span_id,
                    self._db_root.started_clock,
                )

    @contextmanager
    def span(self, name: str, *, input: dict[str, Any] | None = None):
        span_id = uuid4().hex
        started = _utc_now()
        clock = perf_counter()
        span = {
            "span_id": span_id,
            "name": name,
            "status": "running",
            "started_at": started,
            "ended_at": "",
            "duration_ms": 0,
            "input": _jsonable(input or {}),
            "output": {},
            "error": None,
        }
        langfuse_span = self._start_langfuse_span(name, span["input"])
        db_span = self._start_db_stage(name, span["input"])
        if db_span is not None:
            span["_db_span_id"] = db_span.span_id
            self._db_stage_by_file_span[span_id] = db_span
        if self.enabled:
            self.spans.append(span)
            self.flush()
        try:
            yield span
        except Exception as exc:
            span["status"] = "failed"
            span["error"] = _error_payload(exc)
            span["ended_at"] = _utc_now()
            span["duration_ms"] = int((perf_counter() - clock) * 1000)
            if self.recorder is not None:
                self.recorder.span_tracker.fail_span(db_span, exc)
            self._finish_langfuse_span(langfuse_span, output=span["output"], error=exc)
            if self.enabled:
                self.flush()
            raise
        else:
            skipped = span.get("status") == "skipped"
            span["status"] = "skipped" if skipped else "completed"
            span["ended_at"] = _utc_now()
            span["duration_ms"] = int((perf_counter() - clock) * 1000)
            if self.recorder is not None:
                if skipped:
                    self.recorder.span_tracker.skip_span(db_span, span["output"])
                else:
                    self.recorder.span_tracker.end_span(db_span, span["output"])
            self._finish_langfuse_span(langfuse_span, output=span["output"], error=None)
            if self.enabled:
                self.flush()

    def record_output(self, span: dict[str, Any], output: dict[str, Any]) -> None:
        if not self.enabled:
            return
        span["output"] = _jsonable(output)
        if self.recorder is not None:
            self.recorder.span_tracker.update_output(
                self._db_stage_by_file_span.get(str(span.get("span_id") or "")),
                span["output"],
            )
        self.flush()

    def mark_skipped(self, span: dict[str, Any], *, reason: str, output: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        span["status"] = "skipped"
        span["output"] = _jsonable({"skip_reason": reason, **(output or {})})
        if self.recorder is not None:
            self.recorder.span_tracker.update_output(
                self._db_stage_by_file_span.get(str(span.get("span_id") or "")),
                span["output"],
            )
        self.flush()

    def mark_db_subspan_skipped(
        self,
        span: ProcessingSpan | None,
        *,
        reason: str,
        output: dict[str, Any] | None = None,
    ) -> None:
        if span is None or self.recorder is None:
            return
        payload = {"skip_reason": reason, **(output or {})}
        self._skipped_db_span_ids.add(span.span_id)
        self.recorder.span_tracker.skip_span(span, payload)

    @contextmanager
    def db_subspan(
        self,
        parent_file_span: dict[str, Any],
        name: str,
        *,
        kind: str = SPAN_SUBSPAN,
        input: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        db_span = None
        if self.recorder is not None:
            parent_span = self._db_stage_by_file_span.get(str(parent_file_span.get("span_id") or ""))
            db_span = self.recorder.span_tracker.begin_subspan(
                parent_span,
                name,
                kind=kind,
                input=input or {},
                metadata=metadata or {},
            )
        try:
            yield db_span
        except Exception as exc:
            if self.recorder is not None:
                self.recorder.span_tracker.fail_span(db_span, exc)
            raise
        else:
            if self.recorder is not None:
                if db_span is None or db_span.span_id not in self._skipped_db_span_ids:
                    self.recorder.span_tracker.end_span(db_span)
                elif db_span is not None:
                    self._skipped_db_span_ids.discard(db_span.span_id)

    def write_text(self, filename: str, text: str) -> str:
        if not self.enabled:
            return ""
        path = self.trace_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text or "", encoding="utf-8")
        return path.relative_to(self.trace_dir).as_posix()

    def write_jsonl(self, filename: str, rows: list[dict[str, Any]]) -> str:
        if not self.enabled:
            return ""
        path = self.trace_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(_jsonable(row), ensure_ascii=False, sort_keys=True) + "\n")
        if filename == "chunks.jsonl":
            (self.trace_dir / "chunks_preview.md").write_text(
                _render_chunks_preview(rows),
                encoding="utf-8",
            )
        return path.relative_to(self.trace_dir).as_posix()

    def finish(self, *, status: str = "completed", error: BaseException | None = None) -> None:
        if not self.enabled:
            return
        self.status = "failed" if error else status
        self.ended_at = _utc_now()
        if error is not None:
            self.error = _error_payload(error)
            self.write_text("error.txt", self.error["traceback"])
        self.flush()
        if self.recorder is not None:
            self.recorder.span_tracker.finalize_attempt(
                self._db_root,
                status="failed" if error else STATUS_DONE,
                output={"trace_dir": str(self.trace_dir), "trace_id": self.trace_id},
                error=error,
            )
        if self._langfuse_trace is not None:
            self._langfuse_trace.finish(
                output={"trace_dir": str(self.trace_dir), "processing_trace_id": self.trace_id, "status": self.status},
                metadata={"doc_id": self.doc_id, "scope": self.scope},
                error=error,
            )
        if self.recorder is not None:
            self.recorder.flush_langfuse()

    def _start_db_stage(self, name: str, input: dict[str, Any]) -> ProcessingSpan | None:
        if self.recorder is None or not self._db_attempt:
            return None
        stage_name = TRACE_STAGE_MAP.get(name)
        if not stage_name:
            return None
        return self.recorder.span_tracker.begin_stage(self.doc_id, self._db_attempt, stage_name, input)

    def flush(self) -> None:
        if not self.enabled:
            return
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "trace_id": self.trace_id,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": int((perf_counter() - self._start_clock) * 1000),
            "doc_id": self.doc_id,
            "file_name": self.file_name,
            "source": self.source,
            "scope": self.scope,
            "metadata": _jsonable(self.metadata),
            "trace_dir": str(self.trace_dir),
            "spans": self.spans,
            "error": self.error,
        }
        (self.trace_dir / "trace.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (self.trace_dir / "report.md").write_text(_render_trace_report(payload), encoding="utf-8")

    def _start_langfuse_span(self, name: str, span_input: dict[str, Any]) -> Any | None:
        if self._langfuse_trace is None:
            return None
        try:
            return get_observability_sink().start_span(
                name=f"document.{TRACE_STAGE_MAP.get(name, name)}",
                input=span_input,
                metadata={"processing_trace_id": self.trace_id, "doc_id": self.doc_id, "stage": name},
            )
        except Exception as exc:
            logger.warning("Langfuse span start failed: %s", exc)
        return None

    def _finish_langfuse_span(self, langfuse_span: Any | None, *, output: dict[str, Any], error: BaseException | None) -> None:
        if langfuse_span is None:
            return
        try:
            if isinstance(langfuse_span, ObservationHandle):
                langfuse_span.finish(output=_jsonable(output), error=error)
        except Exception as exc:
            logger.warning("Langfuse span finish failed: %s", exc)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_name(value: str) -> str:
    stem = Path(value).stem or "document"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")
    return (cleaned or "document")[:80]


def _error_payload(exc: BaseException) -> dict[str, Any]:
    return {
        "type": exc.__class__.__name__,
        "message": str(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }


def _render_trace_report(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata") or {}
    effective = metadata.get("effective_processing") or {}
    requested = metadata.get("requested_processing") or {}
    spans = list(payload.get("spans") or [])
    lines = [
        f"# 文档处理报告：{payload.get('file_name') or '-'}",
        "",
        "## 概览",
        f"- 状态：{_status_label(str(payload.get('status') or ''))}",
        f"- 文件：{payload.get('file_name') or '-'}",
        f"- 来源路径：{payload.get('source') or '-'}",
        f"- 文档 ID：{payload.get('doc_id') or '-'}",
        f"- Trace ID：{payload.get('trace_id') or '-'}",
        f"- 开始时间：{payload.get('started_at') or '-'}",
        f"- 结束时间：{payload.get('ended_at') or '-'}",
        f"- 总耗时：{_duration(payload.get('duration_ms'))}",
        f"- 文件大小：{_bytes(metadata.get('file_size'))}",
        f"- 文件类型：{metadata.get('extension') or '-'}",
        "",
        "## 阶段时间线",
        "| 阶段 | 状态 | 耗时 | 关键结果 |",
        "|---|---:|---:|---|",
    ]
    for span in spans:
        lines.append(
            "| "
            + " | ".join(
                [
                    _span_label(str(span.get("name") or "")),
                    _status_label(str(span.get("status") or "")),
                    _duration(span.get("duration_ms")),
                    _span_summary(span),
                ]
            )
            + " |"
        )
    if not spans:
        lines.append("| 尚未开始 | 等待中 | - | 后台任务尚未写入阶段信息 |")

    lines.extend(
        [
            "",
            "## 关键配置",
            f"- 解析引擎：{effective.get('parser_engine') or requested.get('parser_engine') or '-'}",
            f"- 切片策略：{effective.get('chunk_strategy') or requested.get('chunk_strategy') or '-'}",
            f"- 父块大小：{effective.get('parent_chunk_size_chars') or '-'} 字符",
            f"- 子块大小：{effective.get('child_chunk_size_chars') or '-'} 字符",
            f"- 子块重叠：{effective.get('child_chunk_overlap_chars') or '-'} 字符",
            f"- Dense 检索：{_yes_no(effective.get('dense_enabled'))}",
            f"- Keyword 检索：{_yes_no(effective.get('keyword_enabled'))}",
            f"- OCR：{_yes_no(effective.get('ocr_enabled'))}",
            f"- 多模态：{_yes_no(effective.get('multimodal_enabled') or effective.get('caption_enabled'))}",
            "",
            "## 产物说明",
            "- `report.md`：当前这份面向人工排查的总览报告。",
            "- `parsed.md`：解析器抽取后的正文，适合检查原文是否读对、是否乱码。",
            "- `chunks_preview.md`：前若干个切片的可读预览，适合快速确认切片质量。",
            "- `chunks.jsonl`：完整切片明细，适合程序读取或深度排查。",
            "- `trace.json`：完整机器 trace，供前端 trace 抽屉和自动化分析使用。",
        ]
    )
    error = payload.get("error")
    if error:
        lines.extend(
            [
                "",
                "## 错误",
                f"- 类型：{error.get('type') or '-'}",
                f"- 信息：{error.get('message') or '-'}",
                "",
                "```text",
                str(error.get("traceback") or "").strip(),
                "```",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _render_chunks_preview(rows: list[dict[str, Any]], limit: int = 30) -> str:
    counts: dict[str, int] = {}
    strategies: dict[str, int] = {}
    for row in rows:
        counts[str(row.get("chunk_type") or "unknown")] = counts.get(str(row.get("chunk_type") or "unknown"), 0) + 1
        strategies[str(row.get("strategy") or (row.get("metadata") or {}).get("strategy") or "unknown")] = (
            strategies.get(str(row.get("strategy") or (row.get("metadata") or {}).get("strategy") or "unknown"), 0) + 1
        )
    lines = [
        "# 切片预览",
        "",
        "## 汇总",
        f"- 切片总数：{len(rows)}",
        f"- 类型分布：{_dict_summary(counts)}",
        f"- 策略分布：{_dict_summary(strategies)}",
        "",
        "## 预览",
    ]
    for index, row in enumerate(rows[:limit], start=1):
        metadata = row.get("metadata") or {}
        content = str(row.get("content_markdown") or row.get("content") or "").strip()
        lines.extend(
            [
                "",
                f"### {index}. {row.get('chunk_type') or 'chunk'} / {row.get('id') or '-'}",
                f"- 字符数：{row.get('characters') or len(content)}",
                f"- 估算 token：{row.get('approx_tokens') or '-'}",
                f"- 策略：{row.get('strategy') or metadata.get('strategy') or '-'}",
                f"- 父块：{row.get('parent_id') or '-'}",
                f"- 标题路径：{row.get('title_path') or metadata.get('context_header') or '-'}",
                f"- 页码：{_page_range(row)}",
                "",
                "```text",
                _trim(content, 1200),
                "```",
            ]
        )
    if len(rows) > limit:
        lines.extend(["", f"> 仅展示前 {limit} 个切片，完整内容请查看 `chunks.jsonl`。"])
    return "\n".join(lines).rstrip() + "\n"


def _span_label(name: str) -> str:
    labels = {
        "load": "文档加载 / 解析",
        "chunk_strategy": "切片策略",
        "index": "索引写入",
        "postprocess": "后处理",
        "multimodal": "多模态处理",
    }
    return labels.get(name, name or "-")


def _span_summary(span: dict[str, Any]) -> str:
    output = span.get("output") or {}
    error = span.get("error")
    if error:
        return _escape_table(str(error.get("message") or "失败"))
    name = str(span.get("name") or "")
    if name == "load":
        parts = [
            f"字符 {output.get('characters', '-')}",
            f"元素 {output.get('elements', '-')}",
            f"图片 {output.get('images', '-')}",
        ]
        diagnostics = output.get("parser_diagnostics") or {}
        warnings = diagnostics.get("warnings") or []
        if warnings:
            parts.append("；".join(map(str, warnings)))
        return _escape_table("，".join(parts))
    if name == "chunk_strategy":
        lengths = output.get("lengths") or {}
        return _escape_table(
            f"切片 {output.get('chunk_count', '-')}，类型 {_dict_summary(output.get('by_type') or {})}，"
            f"长度 {lengths.get('minimum', '-')}/{lengths.get('average', '-')}/{lengths.get('maximum', '-')}"
        )
    if name == "index":
        return _escape_table(f"SQLite {output.get('sqlite_chunks', '-')}，向量 {output.get('vector_chunks', '-')}")
    if name == "postprocess":
        return _escape_table(f"知识图谱 {_yes_no(output.get('kg_attempted'))}，摘要队列 {_yes_no(output.get('enrichment_queued'))}")
    if name == "multimodal":
        return _escape_table(f"图片资源 {output.get('image_resources', '-')}，操作 {output.get('image_operations', '-')}")
    return _escape_table(json.dumps(output, ensure_ascii=False)[:160] if output else "-")


def _status_label(status: str) -> str:
    labels = {
        "running": "进行中",
        "completed": "完成",
        "failed": "失败",
        "skipped": "跳过",
        "pending": "等待中",
    }
    return labels.get(status, status or "-")


def _duration(value: Any) -> str:
    try:
        ms = int(value or 0)
    except (TypeError, ValueError):
        return "-"
    if ms <= 0:
        return "-"
    if ms < 1000:
        return f"{ms} ms"
    return f"{ms / 1000:.2f} s"


def _bytes(value: Any) -> str:
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        return "-"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def _yes_no(value: Any) -> str:
    return "开启" if bool(value) else "关闭"


def _dict_summary(value: dict[str, Any]) -> str:
    if not value:
        return "-"
    return "，".join(f"{key} {item}" for key, item in value.items())


def _page_range(row: dict[str, Any]) -> str:
    start = row.get("page_start")
    end = row.get("page_end")
    if start and end and start != end:
        return f"{start}-{end}"
    return str(start or end or "-")


def _trim(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit].rstrip() + "\n..."


def _escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
