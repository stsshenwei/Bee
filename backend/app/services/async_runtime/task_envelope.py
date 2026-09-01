from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.knowledge_base import KnowledgeBaseScope
from app.services.async_runtime.task_routes import route_for_task_type


@dataclass(frozen=True)
class ProcessingTaskEnvelope:
    task_id: str
    task_type: str
    workspace_id: str
    knowledge_base_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    document_id: str = ""
    upload_batch_id: str = ""
    upload_file_id: str = ""
    idempotency_key: str = ""
    source_revision: str = ""
    payload_schema_version: int = 1
    trace_id: str = ""
    attempt: int = 0
    next_run_at: str = ""
    queue: str = ""

    @classmethod
    def from_task(cls, task: dict[str, Any]) -> "ProcessingTaskEnvelope":
        task_type = str(task.get("task_type") or "")
        return cls(
            task_id=str(task.get("id") or task.get("task_id") or ""),
            task_type=task_type,
            workspace_id=str(task.get("workspace_id") or ""),
            knowledge_base_id=str(task.get("knowledge_base_id") or ""),
            payload=dict(task.get("payload") or {}),
            document_id=str(task.get("document_id") or ""),
            upload_batch_id=str(task.get("upload_batch_id") or ""),
            upload_file_id=str(task.get("upload_file_id") or ""),
            idempotency_key=str(task.get("idempotency_key") or ""),
            source_revision=str(task.get("source_revision") or ""),
            payload_schema_version=max(1, int(task.get("payload_schema_version") or 1)),
            trace_id=str(task.get("trace_id") or ""),
            attempt=int(task.get("attempt") or 0),
            next_run_at=str(task.get("next_run_at") or ""),
            queue=str(task.get("queue_name") or "") or route_for_task_type(task_type),
        )

    @property
    def scope(self) -> KnowledgeBaseScope:
        return KnowledgeBaseScope(self.workspace_id, (self.knowledge_base_id,))

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "workspace_id": self.workspace_id,
            "knowledge_base_id": self.knowledge_base_id,
            "payload": dict(self.payload),
            "document_id": self.document_id,
            "upload_batch_id": self.upload_batch_id,
            "upload_file_id": self.upload_file_id,
            "idempotency_key": self.idempotency_key,
            "source_revision": self.source_revision,
            "payload_schema_version": self.payload_schema_version,
            "trace_id": self.trace_id,
            "attempt": self.attempt,
            "next_run_at": self.next_run_at,
            "queue": self.queue,
        }
