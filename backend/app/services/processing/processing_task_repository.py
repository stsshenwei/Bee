from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.models.knowledge_base import KnowledgeBaseScope
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings, initialize_metadata_database


TASK_PENDING = "pending"
TASK_RETRYING = "retrying"
TASK_PROCESSING = "processing"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_CANCELED = "canceled"
TASK_DEAD_LETTERED = "dead_lettered"

RUNNABLE_STATUSES = {TASK_PENDING, TASK_RETRYING}
TERMINAL_STATUSES = {TASK_COMPLETED, TASK_FAILED, TASK_CANCELED, TASK_DEAD_LETTERED}


class ProcessingTaskRepository:
    def __init__(self, db_path: Path | str, defaults: DefaultKnowledgeBaseSettings | None = None):
        self.db_path = Path(db_path)
        self.defaults = defaults or DefaultKnowledgeBaseSettings()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        initialize_metadata_database(self.db_path, self.defaults)

    @contextmanager
    def _connect(self, *, immediate: bool = False):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        conn.execute("begin immediate" if immediate else "begin")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_task(
        self,
        task_type: str,
        scope: KnowledgeBaseScope,
        *,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        document_id: str = "",
        upload_batch_id: str = "",
        upload_file_id: str = "",
        max_attempts: int = 3,
        run_after: datetime | str | None = None,
        trace_id: str = "",
        payload_schema_version: int = 1,
        idempotency_key: str = "",
        source_revision: str = "",
        parent_trace_id: str = "",
    ) -> dict[str, Any]:
        task_type = _required_text(task_type, "task_type")
        payload = payload or {}
        task_id = task_id or deterministic_task_id(
            task_type,
            scope,
            document_id=document_id,
            upload_batch_id=upload_batch_id,
            upload_file_id=upload_file_id,
            payload=payload,
        )
        clean_idempotency_key = _optional_text(idempotency_key)
        now = _now()
        next_run_at = _as_timestamp(run_after) if run_after else now
        with self._connect(immediate=True) as conn:
            existing = None
            if clean_idempotency_key:
                existing = conn.execute(
                    """
                    select id from document_processing_task
                    where workspace_id = ? and knowledge_base_id = ? and task_type = ? and idempotency_key = ?
                    order by created_at, id limit 1
                    """,
                    (scope.workspace_id, scope.knowledge_base_id, task_type, clean_idempotency_key),
                ).fetchone()
            if existing is not None:
                task_id = str(existing["id"])
            else:
                conn.execute(
                """
                insert or ignore into document_processing_task(
                    id, task_type, workspace_id, knowledge_base_id, document_id, upload_batch_id,
                    upload_file_id, status, payload_json, attempt, max_attempts, next_run_at,
                    lease_owner, lease_expires_at, last_error_code, last_error_message,
                    trace_id, created_at, updated_at, started_at, finished_at,
                    payload_schema_version, idempotency_key, source_revision, parent_trace_id
                ) values (?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?, ?, '', null, '', '', ?, ?, ?, null, null, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    task_type,
                    scope.workspace_id,
                    scope.knowledge_base_id,
                    _optional_text(document_id),
                    _optional_text(upload_batch_id),
                    _optional_text(upload_file_id),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    max(1, int(max_attempts)),
                    next_run_at,
                    _optional_text(trace_id),
                    now,
                    now,
                    max(1, int(payload_schema_version or 1)),
                    _optional_text(idempotency_key),
                    _optional_text(source_revision),
                    _optional_text(parent_trace_id),
                ),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("select * from document_processing_task where id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return _decode_task(row)

    def list_tasks(
        self,
        scope: KnowledgeBaseScope | None = None,
        *,
        statuses: set[str] | None = None,
        document_id: str | None = None,
        upload_batch_id: str | None = None,
        task_types: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope is not None:
            clauses.extend(["workspace_id = ?", "knowledge_base_id = ?"])
            params.extend([scope.workspace_id, scope.knowledge_base_id])
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status in ({placeholders})")
            params.extend(sorted(statuses))
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        if upload_batch_id is not None:
            clauses.append("upload_batch_id = ?")
            params.append(upload_batch_id)
        if task_types:
            placeholders = ",".join("?" for _ in task_types)
            clauses.append(f"task_type in ({placeholders})")
            params.extend(sorted(task_types))
        where = f"where {' and '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"select * from document_processing_task {where} order by created_at, id",
                params,
            ).fetchall()
        return [_decode_task(row) for row in rows]

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        task_types: set[str] | None = None,
        now: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        worker_id = _required_text(worker_id, "worker_id")
        current = _as_timestamp(now) if now else _now()
        clauses = [
            "((status in ('pending', 'retrying') and next_run_at <= ?) "
            "or (status = 'processing' and lease_expires_at is not null and lease_expires_at <= ?))"
        ]
        params: list[Any] = [current, current]
        if task_types:
            placeholders = ",".join("?" for _ in task_types)
            clauses.append(f"task_type in ({placeholders})")
            params.extend(sorted(task_types))
        with self._connect(immediate=True) as conn:
            row = conn.execute(
                f"""
                select * from document_processing_task
                where {' and '.join(clauses)}
                order by next_run_at, created_at, id
                limit 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None
            lease_expires_at = _add_seconds(current, lease_seconds)
            conn.execute(
                """
                update document_processing_task
                set status = 'processing',
                    attempt = attempt + 1,
                    lease_owner = ?,
                    lease_expires_at = ?,
                    started_at = coalesce(started_at, ?),
                    updated_at = ?
                where id = ?
                """,
                (worker_id, lease_expires_at, current, current, row["id"]),
            )
            claimed = conn.execute("select * from document_processing_task where id = ?", (row["id"],)).fetchone()
            if claimed is not None:
                conn.execute(
                    """
                    insert into document_processing_task_attempt(
                        id, task_id, attempt, worker_id, status, started_at, created_at
                    ) values (?, ?, ?, ?, 'processing', ?, ?)
                    """,
                    (f"attempt-{uuid4().hex}", row["id"], int(claimed["attempt"] or 0), worker_id, current, current),
                )
        return _decode_task(claimed) if claimed else None

    def heartbeat(self, task_id: str, worker_id: str, *, lease_seconds: int = 60) -> dict[str, Any]:
        now = _now()
        lease_expires_at = _add_seconds(now, lease_seconds)
        with self._connect(immediate=True) as conn:
            cursor = conn.execute(
                """
                update document_processing_task
                set lease_expires_at = ?, updated_at = ?
                where id = ? and status = 'processing' and lease_owner = ?
                """,
                (lease_expires_at, now, task_id, worker_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(task_id)
        return self.get_task(task_id)

    def complete(self, task_id: str, worker_id: str | None = None) -> dict[str, Any]:
        return self._finish(task_id, TASK_COMPLETED, worker_id=worker_id)

    def fail(self, task_id: str, *, error_code: str = "", error_message: str = "", worker_id: str | None = None) -> dict[str, Any]:
        return self._finish(task_id, TASK_FAILED, error_code=error_code, error_message=error_message, worker_id=worker_id)

    def cancel_task(self, task_id: str, *, reason: str = "cancelled") -> dict[str, Any]:
        return self._finish(task_id, TASK_CANCELED, error_code="CANCELED", error_message=reason)

    def cancel_for_document(self, scope: KnowledgeBaseScope, document_id: str, *, reason: str = "cancelled") -> int:
        return self._cancel_where(scope, "document_id = ?", [document_id], reason)

    def cancel_for_upload_file(self, scope: KnowledgeBaseScope, upload_file_id: str, *, reason: str = "cancelled") -> int:
        return self._cancel_where(scope, "upload_file_id = ?", [upload_file_id], reason)

    def cancel_for_upload_batch(self, scope: KnowledgeBaseScope, upload_batch_id: str, *, reason: str = "cancelled") -> int:
        return self._cancel_where(scope, "upload_batch_id = ?", [upload_batch_id], reason)

    def retry(
        self,
        task_id: str,
        *,
        error_code: str = "",
        error_message: str = "",
        delay_seconds: int = 30,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self._connect(immediate=True) as conn:
            row = conn.execute("select * from document_processing_task where id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["status"] in TERMINAL_STATUSES:
                return _decode_task(row)
            if worker_id is not None and row["lease_owner"] and row["lease_owner"] != worker_id:
                raise KeyError(task_id)
            next_run_at = _add_seconds(now, delay_seconds)
            conn.execute(
                """
                update document_processing_task
                set status = 'retrying',
                    next_run_at = ?,
                    lease_owner = '',
                    lease_expires_at = null,
                    last_error_code = ?,
                    last_error_message = ?,
                    updated_at = ?
                where id = ?
                """,
                (next_run_at, _optional_text(error_code), _sanitize_error(error_message), now, task_id),
            )
            self._finish_attempt(conn, task_id, TASK_RETRYING, error_code, error_message, now)
        return self.get_task(task_id)

    def dead_letter(
        self,
        task_id: str,
        *,
        error_code: str = "",
        error_message: str = "",
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self._connect(immediate=True) as conn:
            row = conn.execute("select * from document_processing_task where id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if worker_id is not None and row["lease_owner"] and row["lease_owner"] != worker_id:
                raise KeyError(task_id)
            letter_id = f"dead-{uuid4().hex}"
            conn.execute(
                """
                insert into document_processing_dead_letter(
                    id, task_id, task_type, workspace_id, knowledge_base_id, document_id,
                    upload_batch_id, upload_file_id, payload_json, error_code, error_message,
                    attempt, trace_id, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    letter_id,
                    row["id"],
                    row["task_type"],
                    row["workspace_id"],
                    row["knowledge_base_id"],
                    row["document_id"],
                    row["upload_batch_id"],
                    row["upload_file_id"],
                    row["payload_json"],
                    _optional_text(error_code),
                    _sanitize_error(error_message),
                    int(row["attempt"] or 0),
                    row["trace_id"],
                    now,
                ),
            )
            conn.execute(
                """
                update document_processing_task
                set status = 'dead_lettered',
                    lease_owner = '',
                    lease_expires_at = null,
                    last_error_code = ?,
                    last_error_message = ?,
                    finished_at = coalesce(finished_at, ?),
                    updated_at = ?
                where id = ?
                """,
                (_optional_text(error_code), _sanitize_error(error_message), now, now, task_id),
            )
            self._finish_attempt(conn, task_id, TASK_DEAD_LETTERED, error_code, error_message, now)
        return self.get_task(task_id)

    def list_dead_letters(self, scope: KnowledgeBaseScope | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope is not None:
            clauses.extend(["workspace_id = ?", "knowledge_base_id = ?"])
            params.extend([scope.workspace_id, scope.knowledge_base_id])
        where = f"where {' and '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"select * from document_processing_dead_letter {where} order by created_at, id",
                params,
            ).fetchall()
        return [_decode_dead_letter(row) for row in rows]

    def retry_dead_letter(self, task_id: str, *, delay_seconds: int = 0) -> dict[str, Any]:
        now = _now()
        with self._connect(immediate=True) as conn:
            row = conn.execute("select * from document_processing_task where id = ?", (task_id,)).fetchone()
            if row is None or str(row["status"]) != TASK_DEAD_LETTERED:
                raise KeyError(task_id)
            conn.execute(
                """
                update document_processing_task
                set status = 'retrying', attempt = 0, next_run_at = ?, lease_owner = '',
                    lease_expires_at = null, last_error_code = '', last_error_message = '',
                    started_at = null, finished_at = null, updated_at = ?
                where id = ? and status = 'dead_lettered'
                """,
                (_add_seconds(now, delay_seconds), now, task_id),
                )
        return self.get_task(task_id)

    def find_by_idempotency(
        self,
        scope: KnowledgeBaseScope,
        task_type: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        clean_key = _optional_text(idempotency_key)
        if not clean_key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                select * from document_processing_task
                where workspace_id = ? and knowledge_base_id = ? and task_type = ? and idempotency_key = ?
                order by created_at, id limit 1
                """,
                (scope.workspace_id, scope.knowledge_base_id, _required_text(task_type, "task_type"), clean_key),
            ).fetchone()
        return _decode_task(row) if row is not None else None

    def merge_runnable_task_payload(
        self,
        scope: KnowledgeBaseScope,
        task_type: str,
        payload: dict[str, Any],
        *,
        run_after: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        now = _now()
        next_run_at = _as_timestamp(run_after) if run_after else now
        with self._connect(immediate=True) as conn:
            row = conn.execute(
                """
                select * from document_processing_task
                where workspace_id = ? and knowledge_base_id = ? and task_type = ?
                  and status in ('pending', 'retrying')
                order by created_at, id limit 1
                """,
                (scope.workspace_id, scope.knowledge_base_id, _required_text(task_type, "task_type")),
            ).fetchone()
            if row is None:
                return None
            merged = json.loads(str(row["payload_json"] or "{}"))
            for key in {"generation_run_ids", "generation_task_ids", "document_ids", "affected_slugs"}:
                values = [*list(merged.get(key) or []), *list(payload.get(key) or [])]
                if key == "generation_run_ids" and payload.get("generation_run_id"):
                    values.append(payload["generation_run_id"])
                merged[key] = list(dict.fromkeys(str(value) for value in values if str(value)))
            conn.execute(
                "update document_processing_task set payload_json = ?, next_run_at = ?, updated_at = ? where id = ?",
                (json.dumps(merged, ensure_ascii=False, sort_keys=True), next_run_at, now, str(row["id"])),
            )
            updated = conn.execute("select * from document_processing_task where id = ?", (str(row["id"]),)).fetchone()
        return _decode_task(updated) if updated is not None else None

    def list_attempts(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "select * from document_processing_task_attempt where task_id = ? order by rowid",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _finish(
        self,
        task_id: str,
        status: str,
        *,
        error_code: str = "",
        error_message: str = "",
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self._connect(immediate=True) as conn:
            clauses = ["id = ?"]
            params: list[Any] = [
                status,
                _optional_text(error_code),
                _sanitize_error(error_message),
                now,
                now,
                task_id,
            ]
            if worker_id is not None:
                clauses.append("(lease_owner = ? or lease_owner = '')")
                params.append(worker_id)
            cursor = conn.execute(
                f"""
                update document_processing_task
                set status = ?,
                    lease_owner = '',
                    lease_expires_at = null,
                    last_error_code = ?,
                    last_error_message = ?,
                    finished_at = coalesce(finished_at, ?),
                    updated_at = ?
                where {' and '.join(clauses)}
                """,
                params,
            )
            if cursor.rowcount == 0:
                raise KeyError(task_id)
            self._finish_attempt(conn, task_id, status, error_code, error_message, now)
        return self.get_task(task_id)

    def _finish_attempt(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        status: str,
        error_code: str,
        error_message: str,
        finished_at: str,
    ) -> None:
        conn.execute(
            """
            update document_processing_task_attempt
            set status = ?, error_code = ?, error_message = ?, finished_at = ?
            where id = (
                select id from document_processing_task_attempt
                where task_id = ? and finished_at is null order by created_at desc, id desc limit 1
            )
            """,
            (status, _optional_text(error_code), _sanitize_error(error_message), finished_at, task_id),
        )

    def _cancel_where(self, scope: KnowledgeBaseScope, condition: str, condition_params: list[Any], reason: str) -> int:
        now = _now()
        with self._connect(immediate=True) as conn:
            cursor = conn.execute(
                f"""
                update document_processing_task
                set status = 'canceled',
                    lease_owner = '',
                    lease_expires_at = null,
                    last_error_code = 'CANCELED',
                    last_error_message = ?,
                    finished_at = coalesce(finished_at, ?),
                    updated_at = ?
                where workspace_id = ? and knowledge_base_id = ?
                  and status not in ('completed', 'failed', 'canceled', 'dead_lettered')
                  and {condition}
                """,
                [
                    _sanitize_error(reason),
                    now,
                    now,
                    scope.workspace_id,
                    scope.knowledge_base_id,
                    *condition_params,
                ],
            )
        return int(cursor.rowcount or 0)


def deterministic_task_id(
    task_type: str,
    scope: KnowledgeBaseScope,
    *,
    document_id: str = "",
    upload_batch_id: str = "",
    upload_file_id: str = "",
    payload: dict[str, Any] | None = None,
) -> str:
    canonical = json.dumps(
        {
            "task_type": task_type,
            "workspace_id": scope.workspace_id,
            "knowledge_base_id": scope.knowledge_base_id,
            "document_id": document_id,
            "upload_batch_id": upload_batch_id,
            "upload_file_id": upload_file_id,
            "payload": payload or {},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "processing-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _decode_task(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["payload"] = _json_object(data.pop("payload_json", "{}"))
    return data


def _decode_dead_letter(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["payload"] = _json_object(data.pop("payload_json", "{}"))
    return data


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _as_timestamp(value: datetime | str | None) -> str:
    if value is None:
        return _now()
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _add_seconds(timestamp: str, seconds: int) -> str:
    base = datetime.fromisoformat(timestamp)
    return (base + timedelta(seconds=max(0, int(seconds)))).isoformat(timespec="seconds")


def _required_text(value: str, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} cannot be empty")
    return text


def _optional_text(value: str | None) -> str:
    return str(value or "").strip()


def _sanitize_error(message: str) -> str:
    return " ".join(str(message or "").split())[:1000]
