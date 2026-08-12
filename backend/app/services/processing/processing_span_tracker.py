from __future__ import annotations

import json
import sqlite3
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings, initialize_metadata_database

SPAN_ROOT = "root"
SPAN_STAGE = "stage"
SPAN_SUBSPAN = "subspan"
SPAN_GENERATION = "generation"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_CANCELLED = "cancelled"

STAGE_DOCREADER = "docreader"
STAGE_CHUNKING = "chunking"
STAGE_EMBEDDING = "embedding"
STAGE_MULTIMODAL = "multimodal"
STAGE_POSTPROCESS = "postprocess"

CANONICAL_STAGES = (
    STAGE_DOCREADER,
    STAGE_CHUNKING,
    STAGE_EMBEDDING,
    STAGE_MULTIMODAL,
    STAGE_POSTPROCESS,
)

STAGE_DEPENDENCIES = {
    STAGE_DOCREADER: (),
    STAGE_CHUNKING: (STAGE_DOCREADER,),
    STAGE_EMBEDDING: (STAGE_CHUNKING,),
    STAGE_MULTIMODAL: (STAGE_CHUNKING,),
    STAGE_POSTPROCESS: (STAGE_EMBEDDING, STAGE_MULTIMODAL),
}


@dataclass(frozen=True)
class ProcessingSpan:
    knowledge_id: str
    attempt: int
    span_id: str
    name: str
    kind: str
    parent_span_id: str | None = None
    started_clock: float = 0.0


class ProcessingSpanRepository:
    def __init__(self, db_path: Path | str, defaults: DefaultKnowledgeBaseSettings | None = None):
        self.db_path = Path(db_path)
        self.defaults = defaults or DefaultKnowledgeBaseSettings()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        initialize_metadata_database(self.db_path, self.defaults)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        try:
            yield conn
        finally:
            conn.close()

    def next_attempt(self, knowledge_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "select coalesce(max(attempt), 0) + 1 from knowledge_processing_spans where knowledge_id = ?",
                (knowledge_id,),
            ).fetchone()
        return int(row[0] if row else 1)

    def latest_attempt(self, knowledge_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "select coalesce(max(attempt), 0) from knowledge_processing_spans where knowledge_id = ?",
                (knowledge_id,),
            ).fetchone()
        return int(row[0] if row else 0)

    def insert_span(
        self,
        *,
        knowledge_id: str,
        attempt: int,
        span_id: str,
        parent_span_id: str | None,
        name: str,
        kind: str,
        status: str,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_ms: int = 0,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("begin")
            conn.execute(
                """
                insert into knowledge_processing_spans
                (knowledge_id, attempt, span_id, parent_span_id, name, kind, status,
                 input_json, output_json, metadata_json, started_at, finished_at, duration_ms, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(knowledge_id, attempt, parent_span_id, name, kind) do update set
                    status = excluded.status,
                    input_json = excluded.input_json,
                    output_json = excluded.output_json,
                    metadata_json = excluded.metadata_json,
                    error_code = '',
                    error_message = '',
                    error_detail = '',
                    started_at = coalesce(excluded.started_at, knowledge_processing_spans.started_at),
                    finished_at = excluded.finished_at,
                    duration_ms = excluded.duration_ms,
                    updated_at = excluded.updated_at
                """,
                (
                    knowledge_id,
                    attempt,
                    span_id,
                    parent_span_id,
                    name,
                    kind,
                    status,
                    _json(input or {}),
                    _json(output or {}),
                    _json(metadata or {}),
                    started_at,
                    finished_at,
                    int(duration_ms or 0),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                select * from knowledge_processing_spans
                where knowledge_id = ? and attempt = ? and parent_span_id is ? and name = ? and kind = ?
                """,
                (knowledge_id, attempt, parent_span_id, name, kind),
            ).fetchone()
            conn.commit()
        return _decode_row(row)

    def update_span(
        self,
        span_id: str,
        *,
        status: str | None = None,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        error_code: str = "",
        error_message: str = "",
        error_detail: str = "",
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        assignments = ["updated_at = ?"]
        params: list[Any] = [_utc_now()]
        if status is not None:
            assignments.append("status = ?")
            params.append(status)
        if input is not None:
            assignments.append("input_json = ?")
            params.append(_json(input))
        if output is not None:
            assignments.append("output_json = ?")
            params.append(_json(output))
        if metadata is not None:
            assignments.append("metadata_json = ?")
            params.append(_json(metadata))
        if error_code:
            assignments.append("error_code = ?")
            params.append(error_code)
        if error_message:
            assignments.append("error_message = ?")
            params.append(error_message)
        if error_detail:
            assignments.append("error_detail = ?")
            params.append(error_detail[:8192])
        if started_at is not None:
            assignments.append("started_at = coalesce(started_at, ?)")
            params.append(started_at)
        if finished_at is not None:
            assignments.append("finished_at = ?")
            params.append(finished_at)
        if duration_ms is not None:
            assignments.append("duration_ms = ?")
            params.append(max(0, int(duration_ms)))
        params.append(span_id)
        with self._connect() as conn:
            conn.execute("begin")
            conn.execute(f"update knowledge_processing_spans set {', '.join(assignments)} where span_id = ?", params)
            conn.commit()

    def get_stage(self, knowledge_id: str, attempt: int, name: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select * from knowledge_processing_spans
                where knowledge_id = ? and attempt = ? and name = ? and kind = ?
                order by id desc limit 1
                """,
                (knowledge_id, attempt, name, SPAN_STAGE),
            ).fetchone()
        return _decode_row(row) if row else None

    def get_span_by_name(
        self,
        knowledge_id: str,
        attempt: int,
        name: str,
        *,
        parent_span_id: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["knowledge_id = ?", "attempt = ?", "name = ?"]
        params: list[Any] = [knowledge_id, attempt, name]
        if parent_span_id is None:
            clauses.append("parent_span_id is null")
        else:
            clauses.append("parent_span_id = ?")
            params.append(parent_span_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                select * from knowledge_processing_spans
                where {' and '.join(clauses)}
                order by id desc limit 1
                """,
                params,
            ).fetchone()
        return _decode_row(row) if row else None

    def supersede_open_span_by_name(
        self,
        knowledge_id: str,
        attempt: int,
        name: str,
        *,
        parent_span_id: str | None,
        kind: str,
        reason: str = "retry re-entry",
    ) -> None:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("begin")
            conn.execute(
                """
                update knowledge_processing_spans
                set status = ?, error_code = 'SUPERSEDED', error_message = ?,
                    finished_at = coalesce(finished_at, ?), updated_at = ?
                where knowledge_id = ? and attempt = ? and parent_span_id = ? and name = ? and kind = ?
                  and status in (?, ?)
                """,
                (
                    STATUS_CANCELLED,
                    reason,
                    now,
                    now,
                    knowledge_id,
                    attempt,
                    parent_span_id,
                    name,
                    kind,
                    STATUS_PENDING,
                    STATUS_RUNNING,
                ),
            )
            conn.commit()

    def heartbeat_span(self, span_id: str) -> None:
        with self._connect() as conn:
            conn.execute("begin")
            conn.execute(
                "update knowledge_processing_spans set updated_at = ? where span_id = ?",
                (_utc_now(), span_id),
            )
            conn.commit()

    def list_attempt(self, knowledge_id: str, attempt: int | None = None) -> tuple[int, list[dict[str, Any]]]:
        with self._connect() as conn:
            if attempt is None:
                row = conn.execute(
                    "select max(attempt) from knowledge_processing_spans where knowledge_id = ?",
                    (knowledge_id,),
                ).fetchone()
                attempt = int(row[0] or 0) if row else 0
            if attempt <= 0:
                return 0, []
            rows = conn.execute(
                """
                select * from knowledge_processing_spans
                where knowledge_id = ? and attempt = ?
                order by case kind when 'root' then 0 when 'stage' then 1 when 'subspan' then 2 else 3 end, id
                """,
                (knowledge_id, attempt),
            ).fetchall()
        return attempt, [_decode_row(row) for row in rows]

    def reassign_knowledge_id(self, old_knowledge_id: str, new_knowledge_id: str, attempt: int) -> None:
        if old_knowledge_id == new_knowledge_id:
            return
        with self._connect() as conn:
            conn.execute("begin")
            conn.execute(
                "update knowledge_processing_spans set knowledge_id = ?, updated_at = ? where knowledge_id = ? and attempt = ?",
                (new_knowledge_id, _utc_now(), old_knowledge_id, attempt),
            )
            conn.commit()

    def cancel_dependents(self, knowledge_id: str, attempt: int, failed_stage: str, reason: str) -> None:
        dependents = _dependent_stages(failed_stage)
        if not dependents:
            return
        now = _utc_now()
        placeholders = ",".join("?" for _ in dependents)
        with self._connect() as conn:
            conn.execute("begin")
            conn.execute(
                f"""
                update knowledge_processing_spans
                set status = ?, error_message = ?, finished_at = coalesce(finished_at, ?), updated_at = ?
                where knowledge_id = ? and attempt = ? and kind = ? and name in ({placeholders})
                  and status in (?, ?)
                """,
                (
                    STATUS_CANCELLED,
                    reason,
                    now,
                    now,
                    knowledge_id,
                    attempt,
                    SPAN_STAGE,
                    *dependents,
                    STATUS_PENDING,
                    STATUS_RUNNING,
                ),
            )
            conn.commit()

    def cancel_descendants(self, knowledge_id: str, attempt: int, parent_span_id: str, reason: str) -> int:
        now = _utc_now()
        with self._connect() as conn:
            child_rows = conn.execute(
                """
                select span_id from knowledge_processing_spans
                where knowledge_id = ? and attempt = ? and parent_span_id = ?
                """,
                (knowledge_id, attempt, parent_span_id),
            ).fetchall()
            pending = [str(row["span_id"]) for row in child_rows]
            descendants: list[str] = []
            while pending:
                current = pending.pop()
                descendants.append(current)
                rows = conn.execute(
                    """
                    select span_id from knowledge_processing_spans
                    where knowledge_id = ? and attempt = ? and parent_span_id = ?
                    """,
                    (knowledge_id, attempt, current),
                ).fetchall()
                pending.extend(str(row["span_id"]) for row in rows)
            if not descendants:
                return 0
            placeholders = ",".join("?" for _ in descendants)
            conn.execute("begin")
            cursor = conn.execute(
                f"""
                update knowledge_processing_spans
                set status = ?, error_code = 'CANCELLED', error_message = ?,
                    finished_at = coalesce(finished_at, ?), updated_at = ?
                where knowledge_id = ? and attempt = ? and span_id in ({placeholders})
                  and status in (?, ?)
                """,
                (
                    STATUS_CANCELLED,
                    reason,
                    now,
                    now,
                    knowledge_id,
                    attempt,
                    *descendants,
                    STATUS_PENDING,
                    STATUS_RUNNING,
                ),
            )
            conn.commit()
        return int(cursor.rowcount or 0)

    def cancel_all_open_spans(self, knowledge_id: str, attempt: int, reason: str) -> int:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("begin")
            cursor = conn.execute(
                """
                update knowledge_processing_spans
                set status = ?, error_code = 'CANCELLED', error_message = ?,
                    finished_at = coalesce(finished_at, ?), updated_at = ?
                where knowledge_id = ? and attempt = ? and status in (?, ?)
                """,
                (
                    STATUS_CANCELLED,
                    reason,
                    now,
                    now,
                    knowledge_id,
                    attempt,
                    STATUS_PENDING,
                    STATUS_RUNNING,
                ),
            )
            conn.commit()
        return int(cursor.rowcount or 0)


class ProcessingSpanTracker:
    def __init__(self, repository: ProcessingSpanRepository | None):
        self.repository = repository

    @classmethod
    def disabled(cls) -> "ProcessingSpanTracker":
        return cls(None)

    @property
    def enabled(self) -> bool:
        return self.repository is not None

    def latest_attempt(self, knowledge_id: str) -> int:
        if self.repository is None:
            return 0
        return self.repository.latest_attempt(knowledge_id)

    def open_attempt(
        self,
        *,
        knowledge_id: str,
        input: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> tuple[ProcessingSpan | None, int]:
        if self.repository is None:
            return None, 0
        attempt = self.repository.next_attempt(knowledge_id)
        root = self.repository.insert_span(
            knowledge_id=knowledge_id,
            attempt=attempt,
            span_id=uuid4().hex,
            parent_span_id=None,
            name="knowledge_processing",
            kind=SPAN_ROOT,
            status=STATUS_RUNNING,
            input=input,
            metadata=metadata or {},
            started_at=_utc_now(),
        )
        root_span = ProcessingSpan(knowledge_id, attempt, str(root["span_id"]), "knowledge_processing", SPAN_ROOT, started_clock=perf_counter())
        for stage in CANONICAL_STAGES:
            self.repository.insert_span(
                knowledge_id=knowledge_id,
                attempt=attempt,
                span_id=uuid4().hex,
                parent_span_id=root_span.span_id,
                name=stage,
                kind=SPAN_STAGE,
                status=STATUS_PENDING,
                input={},
                metadata={"dependencies": list(STAGE_DEPENDENCIES[stage])},
            )
        return root_span, attempt

    def begin_stage(self, knowledge_id: str, attempt: int, name: str, input: dict[str, Any] | None = None) -> ProcessingSpan | None:
        if self.repository is None or not attempt:
            return None
        row = self.repository.get_stage(knowledge_id, attempt, name)
        if row is None:
            return None
        started = _utc_now()
        self.repository.update_span(
            str(row["span_id"]),
            status=STATUS_RUNNING,
            input=input or {},
            started_at=started,
            finished_at="",
            duration_ms=0,
        )
        return ProcessingSpan(
            knowledge_id=knowledge_id,
            attempt=attempt,
            span_id=str(row["span_id"]),
            parent_span_id=row.get("parent_span_id"),
            name=name,
            kind=SPAN_STAGE,
            started_clock=perf_counter(),
        )

    def lookup_stage(self, knowledge_id: str, attempt: int, name: str) -> dict[str, Any] | None:
        if self.repository is None:
            return None
        return self.repository.get_stage(knowledge_id, attempt, name)

    def lookup_span_by_name(
        self,
        knowledge_id: str,
        attempt: int,
        name: str,
        *,
        parent_span_id: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any] | None:
        if self.repository is None:
            return None
        return self.repository.get_span_by_name(
            knowledge_id,
            attempt,
            name,
            parent_span_id=parent_span_id,
            kind=kind,
        )

    def begin_subspan(
        self,
        parent: ProcessingSpan | None,
        name: str,
        *,
        kind: str = SPAN_SUBSPAN,
        input: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ProcessingSpan | None:
        if self.repository is None or parent is None:
            return None
        self.repository.supersede_open_span_by_name(
            parent.knowledge_id,
            parent.attempt,
            name,
            parent_span_id=parent.span_id,
            kind=kind,
        )
        row = self.repository.insert_span(
            knowledge_id=parent.knowledge_id,
            attempt=parent.attempt,
            span_id=uuid4().hex,
            parent_span_id=parent.span_id,
            name=name,
            kind=kind,
            status=STATUS_RUNNING,
            input=input or {},
            metadata=metadata or {},
            started_at=_utc_now(),
            finished_at="",
            duration_ms=0,
        )
        return ProcessingSpan(
            knowledge_id=parent.knowledge_id,
            attempt=parent.attempt,
            span_id=str(row["span_id"]),
            parent_span_id=parent.span_id,
            name=name,
            kind=kind,
            started_clock=perf_counter(),
        )

    def begin_generation(
        self,
        parent: ProcessingSpan | None,
        name: str,
        *,
        input: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ProcessingSpan | None:
        return self.begin_subspan(parent, name, kind=SPAN_GENERATION, input=input, metadata=metadata)

    def heartbeat(self, span: ProcessingSpan | None) -> None:
        if self.repository is None or span is None:
            return
        self.repository.heartbeat_span(span.span_id)

    def update_output(self, span: ProcessingSpan | None, output: dict[str, Any]) -> None:
        if self.repository is None or span is None:
            return
        self.repository.update_span(span.span_id, output=output)

    def end_span(self, span: ProcessingSpan | None, output: dict[str, Any] | None = None) -> None:
        if self.repository is None or span is None:
            return
        self.repository.update_span(
            span.span_id,
            status=STATUS_DONE,
            output=output if output is not None else None,
            finished_at=_utc_now(),
            duration_ms=_elapsed_ms(span.started_clock),
        )

    def skip_span(self, span: ProcessingSpan | None, output: dict[str, Any] | None = None) -> None:
        if self.repository is None or span is None:
            return
        self.repository.update_span(
            span.span_id,
            status=STATUS_SKIPPED,
            output=output if output is not None else None,
            finished_at=_utc_now(),
            duration_ms=_elapsed_ms(span.started_clock),
        )

    def fail_span(self, span: ProcessingSpan | None, exc: BaseException) -> None:
        if self.repository is None or span is None:
            return
        error = _error_payload(exc)
        self.repository.update_span(
            span.span_id,
            status=STATUS_FAILED,
            error_code=error["type"],
            error_message=error["message"],
            error_detail=error["traceback"],
            finished_at=_utc_now(),
            duration_ms=_elapsed_ms(span.started_clock),
        )
        if span.kind == SPAN_STAGE:
            self.repository.cancel_dependents(
                span.knowledge_id,
                span.attempt,
                span.name,
                f"upstream {span.name} failed ({error['type']})",
            )

    def finalize_attempt(
        self,
        root: ProcessingSpan | None,
        *,
        status: str,
        output: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        if self.repository is None or root is None:
            return
        if error is not None:
            err = _error_payload(error)
            self.repository.update_span(
                root.span_id,
                status=STATUS_FAILED,
                output=output,
                error_code=err["type"],
                error_message=err["message"],
                error_detail=err["traceback"],
                finished_at=_utc_now(),
                duration_ms=_elapsed_ms(root.started_clock),
            )
            return
        self.repository.update_span(
            root.span_id,
            status=status,
            output=output,
            finished_at=_utc_now(),
            duration_ms=_elapsed_ms(root.started_clock),
        )

    def abort_attempt(
        self,
        root: ProcessingSpan | None,
        *,
        reason: str = "aborted",
        error: BaseException | None = None,
    ) -> None:
        if self.repository is None or root is None:
            return
        self.repository.cancel_all_open_spans(root.knowledge_id, root.attempt, reason)
        if error is None:
            self.repository.update_span(
                root.span_id,
                status=STATUS_CANCELLED,
                error_code="CANCELLED",
                error_message=reason,
                finished_at=_utc_now(),
                duration_ms=_elapsed_ms(root.started_clock),
            )
            return
        err = _error_payload(error)
        self.repository.update_span(
            root.span_id,
            status=STATUS_FAILED,
            error_code=err["type"],
            error_message=err["message"],
            error_detail=err["traceback"],
            finished_at=_utc_now(),
            duration_ms=_elapsed_ms(root.started_clock),
        )

    def cancel_descendants(self, span: ProcessingSpan | None, reason: str = "cancelled") -> int:
        if self.repository is None or span is None:
            return 0
        return self.repository.cancel_descendants(span.knowledge_id, span.attempt, span.span_id, reason)

    def cancel_all_open_spans(self, knowledge_id: str, attempt: int | None = None, reason: str = "cancelled") -> int:
        if self.repository is None:
            return 0
        if attempt is None:
            attempt = self.repository.latest_attempt(knowledge_id)
        if not attempt:
            return 0
        return self.repository.cancel_all_open_spans(knowledge_id, attempt, reason)

    def reassign_knowledge_id(self, old_knowledge_id: str, new_knowledge_id: str, attempt: int) -> None:
        if self.repository is not None:
            self.repository.reassign_knowledge_id(old_knowledge_id, new_knowledge_id, attempt)

    def latest_tree(self, knowledge_id: str) -> dict[str, Any] | None:
        if self.repository is None:
            return None
        attempt, rows = self.repository.list_attempt(knowledge_id)
        if not rows:
            return None
        nodes = [_span_response(row) for row in rows]
        by_id = {node["span_id"]: node for node in nodes}
        root = next((node for node in nodes if node["kind"] == SPAN_ROOT), nodes[0])
        for node in nodes:
            parent_id = node.get("parent_span_id")
            if parent_id and parent_id in by_id:
                by_id[parent_id].setdefault("children", []).append(node)
        root["children"] = _canonical_stage_order(root.get("children", []))
        return {"attempt": attempt, "root": root}


def _span_response(row: dict[str, Any]) -> dict[str, Any]:
    error = None
    if row.get("error_code") or row.get("error_message") or row.get("error_detail"):
        error = {
            "type": row.get("error_code") or "ProcessingError",
            "message": row.get("error_message") or "",
            "traceback": row.get("error_detail") or "",
        }
    return {
        "span_id": row["span_id"],
        "parent_span_id": row.get("parent_span_id") or "",
        "name": row["name"],
        "label": _label(str(row["name"])),
        "kind": row["kind"],
        "status": row["status"],
        "started_at": row.get("started_at") or "",
        "ended_at": row.get("finished_at") or "",
        "duration_ms": int(row.get("duration_ms") or 0),
        "input": _sanitize_payload(row.get("input_json") or {}),
        "output": _sanitize_payload(row.get("output_json") or {}),
        "metadata": _sanitize_payload(row.get("metadata_json") or {}),
        "error": error,
        "children": [],
    }


def _decode_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    data = dict(row)
    for key in ("input_json", "output_json", "metadata_json"):
        data[key] = json.loads(data.get(key) or "{}")
    return data


def _json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _sanitize_payload(value: Any, *, max_string_chars: int = 2000) -> Any:
    secret_markers = ("secret", "token", "api_key", "apikey", "password", "authorization", "cookie")
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if any(marker in text_key.lower() for marker in secret_markers):
                sanitized[text_key] = "[redacted]"
            else:
                sanitized[text_key] = _sanitize_payload(item, max_string_chars=max_string_chars)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_payload(item, max_string_chars=max_string_chars) for item in value[:100]]
    if isinstance(value, str):
        if len(value) > max_string_chars:
            return value[:max_string_chars] + "...[truncated]"
        return value
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _elapsed_ms(started_clock: float) -> int:
    if not started_clock:
        return 0
    return int((perf_counter() - started_clock) * 1000)


def _error_payload(exc: BaseException) -> dict[str, str]:
    return {
        "type": exc.__class__.__name__,
        "message": str(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }


def _dependent_stages(stage_name: str) -> list[str]:
    result: list[str] = []
    pending = [stage_name]
    while pending:
        current = pending.pop()
        for stage, deps in STAGE_DEPENDENCIES.items():
            if current in deps and stage not in result:
                result.append(stage)
                pending.append(stage)
    return result


def _canonical_stage_order(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = {name: index for index, name in enumerate(CANONICAL_STAGES)}
    return sorted(nodes, key=lambda node: (order.get(str(node.get("name")), 99), str(node.get("name"))))


def _label(name: str) -> str:
    return {
        "knowledge_processing": "知识处理",
        STAGE_DOCREADER: "文档解析",
        STAGE_CHUNKING: "分块",
        STAGE_EMBEDDING: "向量化",
        STAGE_MULTIMODAL: "多模态识别",
        STAGE_POSTPROCESS: "后处理",
    }.get(name, name)
