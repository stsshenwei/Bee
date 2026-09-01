from __future__ import annotations

import json
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from uuid import uuid4

from app.services.agent.agent_runtime_spans import AgentRuntimeSpan
from app.services.infrastructure.logging_config import sanitize_payload
from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import (
    PostgresSchemaConfig,
    inspect_postgres_startup_storage,
    qname,
)
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings


class PostgresAgentRuntimeSpanRepository:
    def __init__(
        self,
        database: PostgresDatabase | None,
        defaults: DefaultKnowledgeBaseSettings | None = None,
        *,
        schema: str = "public",
        enabled: bool = True,
        validate_schema: bool = True,
    ):
        self.enabled = bool(enabled)
        self.database = database
        self.defaults = defaults or DefaultKnowledgeBaseSettings()
        self.schema = schema
        if self.enabled and self.database is not None and validate_schema:
            inspect_postgres_startup_storage(database, config=PostgresSchemaConfig(schema=self.schema))

    @classmethod
    def disabled(cls) -> "PostgresAgentRuntimeSpanRepository":
        return cls(None, enabled=False)

    def start_span(
        self,
        *,
        run_id: str,
        name: str,
        kind: str,
        parent_span_id: str = "",
        input: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRuntimeSpan | None:
        if not self.enabled or self.database is None:
            return None
        span_id = uuid4().hex
        now = _utc_now()
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    insert into {self._table()}(
                        run_id, span_id, parent_span_id, name, kind, status,
                        input_json, output_json, metadata_json, error_message,
                        started_at, finished_at, duration_ms, created_at, updated_at
                    ) values (%s, %s, %s, %s, %s, 'running', %s::jsonb, '{{}}'::jsonb, %s::jsonb, '', %s, null, 0, %s, %s)
                    """,
                    (
                        run_id,
                        span_id,
                        parent_span_id,
                        name,
                        kind,
                        _json(input or {}),
                        _json(metadata or {}),
                        now,
                        now,
                        now,
                    ),
                )
        return AgentRuntimeSpan(run_id=run_id, span_id=span_id, name=name, kind=kind, started_clock=perf_counter())

    def finish_span(
        self,
        span: AgentRuntimeSpan | None,
        *,
        status: str = "completed",
        output: dict[str, Any] | None = None,
        error_message: str = "",
    ) -> None:
        if not self.enabled or self.database is None or span is None:
            return
        now = _utc_now()
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    update {self._table()}
                    set status = %s, output_json = %s::jsonb, error_message = %s, finished_at = %s,
                        duration_ms = %s, updated_at = %s
                    where span_id = %s
                    """,
                    (
                        status,
                        _json(output or {}),
                        str(error_message or "")[:2000],
                        now,
                        max(0, int((perf_counter() - span.started_clock) * 1000)),
                        now,
                        span.span_id,
                    ),
                )

    def _table(self) -> str:
        return qname(self.schema, "agent_runtime_spans")


def _json(value: dict[str, Any]) -> str:
    return json.dumps(sanitize_payload(value, limit=2000), ensure_ascii=False)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
