from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from app.models.kg_models import EntityMention
from app.models.knowledge_base import KnowledgeBaseScope
from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import PostgresSchemaConfig, inspect_postgres_startup_storage, qname
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings


class PostgresKGRepository:
    def __init__(
        self,
        database: PostgresDatabase,
        defaults: DefaultKnowledgeBaseSettings | None = None,
        *,
        schema: str | None = None,
        validate_schema: bool = True,
    ):
        self.database = database
        self.defaults = defaults or DefaultKnowledgeBaseSettings()
        self.schema = schema or database.settings.schema
        if validate_schema:
            inspect_postgres_startup_storage(database, config=PostgresSchemaConfig(schema=self.schema))

    def create_extraction_task(
        self,
        doc_id: str,
        extractor_version: str,
        parent_chunk_count: int = 0,
        metadata: dict[str, Any] | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> dict[str, Any]:
        scope = scope or self.default_scope()
        now = _now()
        task_id = str(uuid.uuid4())
        self._execute(
            f"""
            insert into {self._table('kg_extraction_task')}(
                id, doc_id, workspace_id, knowledge_base_id, status, error_message, extractor_version,
                parent_chunk_count, metadata_json, started_at, finished_at, created_at
            ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, null, null, %s)
            """,
            (
                task_id,
                doc_id,
                scope.workspace_id,
                scope.knowledge_base_id,
                "pending",
                None,
                extractor_version,
                int(parent_chunk_count),
                _dump(metadata or {}),
                now,
            ),
        )
        return self.get_task(task_id) or {}

    def mark_task_started(self, task_id: str) -> None:
        self._update_task(task_id, status="running", started_at=_now())

    def mark_task_completed(self, task_id: str) -> None:
        self._update_task(task_id, status="completed", finished_at=_now(), error_message=None)

    def mark_task_failed(self, task_id: str, error_message: str) -> None:
        self._update_task(task_id, status="failed", finished_at=_now(), error_message=error_message)

    def mark_task_partial_failed(self, task_id: str, error_message: str) -> None:
        self._update_task(task_id, status="partial_failed", finished_at=_now(), error_message=error_message)

    def _update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{name} = %s" for name in fields)
        self._execute(
            f"update {self._table('kg_extraction_task')} set {assignments} where id = %s",
            (*fields.values(), task_id),
        )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._fetch_one(f"select * from {self._table('kg_extraction_task')} where id = %s", (task_id,))
        return self._decode_row(row) if row else None

    def list_extraction_tasks(
        self,
        doc_id: str | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if doc_id:
            clauses.append("doc_id = %s")
            params.append(doc_id)
        if scope is not None:
            placeholders = _placeholders(scope.selected_knowledge_base_ids)
            clauses.extend(["workspace_id = %s", f"knowledge_base_id in ({placeholders})"])
            params.extend([scope.workspace_id, *scope.selected_knowledge_base_ids])
        where = f" where {' and '.join(clauses)}" if clauses else ""
        rows = self._fetch_all(
            f"select * from {self._table('kg_extraction_task')}{where} order by created_at desc",
            tuple(params),
        )
        return [self._decode_row(row) for row in rows]

    def insert_entity_mentions(self, mentions: list[EntityMention]) -> None:
        if not mentions:
            return
        rows = []
        for mention in mentions:
            rows.append(
                (
                    mention.id,
                    str(mention.metadata.get("workspace_id", self.defaults.workspace_id)),
                    str(mention.metadata.get("knowledge_base_id", self.defaults.knowledge_base_id)),
                    mention.entity_id,
                    mention.entity_type,
                    mention.entity_name,
                    mention.doc_id,
                    mention.chunk_id,
                    mention.parent_id,
                    mention.page_start,
                    mention.page_end,
                    mention.mention_text,
                    mention.confidence,
                    _dump(mention.aliases),
                    mention.description,
                    _dump(mention.metadata),
                    mention.created_at,
                )
            )
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    f"""
                    insert into {self._table('entity_mention')}(
                        id, workspace_id, knowledge_base_id, entity_id, entity_type, entity_name, doc_id,
                        chunk_id, parent_id, page_start, page_end, mention_text, confidence,
                        aliases_json, description, metadata_json, created_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s)
                    on conflict(id) do update set
                        entity_id = excluded.entity_id,
                        entity_type = excluded.entity_type,
                        entity_name = excluded.entity_name,
                        doc_id = excluded.doc_id,
                        chunk_id = excluded.chunk_id,
                        parent_id = excluded.parent_id,
                        page_start = excluded.page_start,
                        page_end = excluded.page_end,
                        mention_text = excluded.mention_text,
                        confidence = excluded.confidence,
                        aliases_json = excluded.aliases_json,
                        description = excluded.description,
                        metadata_json = excluded.metadata_json
                    """,
                    rows,
                )

    def list_entity_mentions(
        self,
        doc_id: str | None = None,
        entity_id: str | None = None,
        chunk_id: str | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if doc_id:
            clauses.append("doc_id = %s")
            params.append(doc_id)
        if entity_id:
            clauses.append("entity_id = %s")
            params.append(entity_id)
        if chunk_id:
            clauses.append("chunk_id = %s")
            params.append(chunk_id)
        if scope is not None:
            placeholders = _placeholders(scope.selected_knowledge_base_ids)
            clauses.extend(["workspace_id = %s", f"knowledge_base_id in ({placeholders})"])
            params.extend([scope.workspace_id, *scope.selected_knowledge_base_ids])
        where = f" where {' and '.join(clauses)}" if clauses else ""
        rows = self._fetch_all(
            f"select * from {self._table('entity_mention')}{where} order by created_at, id",
            tuple(params),
        )
        return [self._decode_row(row) for row in rows]

    def upsert_community_summary(
        self,
        community_id: str,
        summary: str,
        entity_ids: list[str],
        source_chunk_ids: list[str],
        confidence: float,
        metadata: dict[str, Any] | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> None:
        scope = scope or self.default_scope()
        now = _now()
        row_id = f"{scope.knowledge_base_id}:{community_id}"
        existing = self._fetch_one(f"select created_at from {self._table('graph_community_summary')} where id = %s", (row_id,))
        created_at = str(_row_get(existing, "created_at")) if existing else now
        self._execute(
            f"""
            insert into {self._table('graph_community_summary')}(
                id, workspace_id, knowledge_base_id, community_id, summary, entity_ids_json,
                source_chunk_ids_json, confidence, metadata_json, created_at, updated_at
            ) values (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s, %s)
            on conflict(workspace_id, knowledge_base_id, community_id) do update set
                summary = excluded.summary,
                entity_ids_json = excluded.entity_ids_json,
                source_chunk_ids_json = excluded.source_chunk_ids_json,
                confidence = excluded.confidence,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                row_id,
                scope.workspace_id,
                scope.knowledge_base_id,
                community_id,
                summary,
                _dump(entity_ids),
                _dump(source_chunk_ids),
                float(confidence),
                _dump(metadata or {}),
                created_at,
                now,
            ),
        )

    def list_community_summaries(self, scope: KnowledgeBaseScope | None = None) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if scope is not None:
            placeholders = _placeholders(scope.selected_knowledge_base_ids)
            where = f" where workspace_id = %s and knowledge_base_id in ({placeholders})"
            params = [scope.workspace_id, *scope.selected_knowledge_base_ids]
        rows = self._fetch_all(
            f"select * from {self._table('graph_community_summary')}{where} order by updated_at desc",
            tuple(params),
        )
        return [self._decode_row(row) for row in rows]

    def default_scope(self) -> KnowledgeBaseScope:
        return KnowledgeBaseScope(
            self.defaults.workspace_id,
            (self.defaults.knowledge_base_id,),
            compatibility_default=True,
        )

    def _fetch_one(self, sql: str, params: tuple[Any, ...]) -> Any | None:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()

    def _fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())

    def _execute(self, sql: str, params: tuple[Any, ...]) -> int:
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return int(getattr(cur, "rowcount", 0) or 0)

    def _table(self, table: str) -> str:
        return qname(self.schema, table)

    def _decode_row(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        for key in ["metadata_json", "aliases_json", "entity_ids_json", "source_chunk_ids_json"]:
            if key in data:
                default = [] if key.endswith("_ids_json") or key == "aliases_json" else {}
                data[key] = _load_json(data[key], default)
        return data


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value or _dump(default))


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    return row.get(key, default) if hasattr(row, "get") else row[key]


def _placeholders(items: Any) -> str:
    count = len(tuple(items))
    if count <= 0:
        raise ValueError("At least one value is required")
    return ", ".join("%s" for _ in range(count))
