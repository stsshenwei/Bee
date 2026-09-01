from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from app.models.document_models import Chunk
from app.models.knowledge_base import KnowledgeBaseScope
from app.models.processing_config import PROCESSING_VERSION
from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import (
    PostgresSchemaConfig,
    inspect_postgres_startup_storage,
    qname,
)
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings


class PostgresDocumentRepository:
    KEYWORD_CHUNK_TYPES = {"child", "table", "ocr", "image_ocr", "image_caption"}

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

    def upsert_document(
        self,
        id: str,
        name: str,
        file_type: str,
        storage_path: str,
        parse_status: str,
        metadata_json: dict[str, Any] | None = None,
        workspace_id: str | None = None,
        knowledge_base_id: str | None = None,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        metadata_json = metadata_json or {}
        workspace_id = (workspace_id or self.defaults.workspace_id).strip()
        knowledge_base_id = (knowledge_base_id or self.defaults.knowledge_base_id).strip()
        with self.database.transaction() as conn:
            self._assert_active_knowledge_base(conn, workspace_id, knowledge_base_id)
            existing = self._fetch_one_in_conn(
                conn,
                f"select workspace_id, knowledge_base_id, created_at from {self._table('document')} where id = %s",
                (id,),
            )
            if existing is not None and (
                str(_row_get(existing, "workspace_id")),
                str(_row_get(existing, "knowledge_base_id")),
            ) != (workspace_id, knowledge_base_id):
                raise ValueError("Document ownership is immutable")
            created_at = str(_row_get(existing, "created_at")) if existing else now
            self._execute_in_conn(
                conn,
                f"""
                insert into {self._table('document')}
                (id, workspace_id, knowledge_base_id, name, file_type, storage_path, parse_status,
                 created_at, updated_at, metadata_json)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                on conflict(id) do update set
                    name = excluded.name,
                    file_type = excluded.file_type,
                    storage_path = excluded.storage_path,
                    parse_status = excluded.parse_status,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    id,
                    workspace_id,
                    knowledge_base_id,
                    name,
                    file_type,
                    storage_path,
                    parse_status,
                    created_at,
                    now,
                    _jsonb_param(metadata_json),
                ),
            )

    def replace_chunks(
        self,
        doc_id: str,
        chunks: list[Chunk],
        scope: KnowledgeBaseScope | None = None,
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.database.transaction() as conn:
            workspace_id, knowledge_base_id = self._document_owner(conn, doc_id, scope)
            self._validate_chunk_ownership(doc_id, chunks, workspace_id, knowledge_base_id)
            self._execute_in_conn(conn, f"delete from {self._table('document_chunk')} where doc_id = %s", (doc_id,))
            self._insert_chunks(conn, chunks, workspace_id, knowledge_base_id, now)

    def upsert_chunks(
        self,
        doc_id: str,
        chunks: list[Chunk],
        scope: KnowledgeBaseScope | None = None,
    ) -> None:
        if not chunks:
            return
        now = datetime.now().isoformat(timespec="seconds")
        with self.database.transaction() as conn:
            workspace_id, knowledge_base_id = self._document_owner(conn, doc_id, scope)
            self._validate_chunk_ownership(doc_id, chunks, workspace_id, knowledge_base_id)
            chunk_ids = [chunk.id for chunk in chunks]
            placeholders = _placeholders(chunk_ids)
            self._execute_in_conn(
                conn,
                f"delete from {self._table('document_chunk')} where doc_id = %s and id in ({placeholders})",
                (doc_id, *chunk_ids),
            )
            self._insert_chunks(conn, chunks, workspace_id, knowledge_base_id, now)

    def reset(self, scope: KnowledgeBaseScope | None = None) -> None:
        scope = scope or self.default_scope()
        placeholders = _placeholders(scope.selected_knowledge_base_ids)
        params = (scope.workspace_id, *scope.selected_knowledge_base_ids)
        with self.database.transaction() as conn:
            self._execute_in_conn(
                conn,
                f"delete from {self._table('document')} where workspace_id = %s and knowledge_base_id in ({placeholders})",
                params,
            )

    def delete_document(self, doc_id: str, scope: KnowledgeBaseScope | None = None) -> None:
        scope = scope or self.default_scope()
        placeholders = _placeholders(scope.selected_knowledge_base_ids)
        params = (doc_id, scope.workspace_id, *scope.selected_knowledge_base_ids)
        with self.database.transaction() as conn:
            exists = self._fetch_one_in_conn(
                conn,
                f"select 1 from {self._table('document')} where id = %s and workspace_id = %s and knowledge_base_id in ({placeholders})",
                params,
            )
            if exists is None:
                raise KeyError(doc_id)
            self._execute_in_conn(
                conn,
                f"""
                update {self._table('knowledge_upload_file')}
                set document_id = null
                where document_id = %s and workspace_id = %s and knowledge_base_id in ({placeholders})
                """,
                params,
            )
            self._execute_in_conn(conn, f"delete from {self._table('document')} where id = %s", (doc_id,))

    def count_chunks(
        self,
        chunk_types: set[str] | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> int:
        scope = scope or self.default_scope()
        clauses, params = self._scope_clauses(scope)
        if chunk_types:
            clauses.append(f"chunk_type in ({_placeholders(chunk_types)})")
            params.extend(sorted(chunk_types))
        row = self._fetch_one(
            f"select count(*) as count from {self._table('document_chunk')} where {' and '.join(clauses)}",
            tuple(params),
        )
        return int(_row_get(row, "count", 0) or 0)

    def list_documents(self, scope: KnowledgeBaseScope | None = None) -> list[dict[str, Any]]:
        scope = scope or self.default_scope()
        placeholders = _placeholders(scope.selected_knowledge_base_ids)
        rows = self._fetch_all(
            f"""
            select d.*, count(c.id) as chunks
            from {self._table('document')} d
            left join {self._table('document_chunk')} c on c.doc_id = d.id
            where d.workspace_id = %s and d.knowledge_base_id in ({placeholders})
            group by d.id
            order by d.updated_at desc
            """,
            (scope.workspace_id, *scope.selected_knowledge_base_ids),
        )
        return [self._decode_row(row) for row in rows]

    def list_chunks(
        self,
        doc_id: str | None = None,
        chunk_types: set[str] | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> list[dict[str, Any]]:
        scope = scope or self.default_scope()
        clauses, params = self._scope_clauses(scope)
        if doc_id:
            clauses.append("doc_id = %s")
            params.append(doc_id)
        elif scope.document_ids:
            clauses.append(f"doc_id in ({_placeholders(scope.document_ids)})")
            params.extend(scope.document_ids)
        if chunk_types:
            clauses.append(f"chunk_type in ({_placeholders(chunk_types)})")
            params.extend(sorted(chunk_types))
        rows = self._fetch_all(
            f"select * from {self._table('document_chunk')} where {' and '.join(clauses)} order by created_at, id",
            tuple(params),
        )
        return [self._decode_row(row) for row in rows]

    def count_chunks_for_documents(
        self,
        doc_ids: list[str] | tuple[str, ...],
        *,
        chunk_types: set[str] | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> dict[str, int]:
        scope = scope or self.default_scope()
        selected_doc_ids = tuple(dict.fromkeys(str(item).strip() for item in doc_ids if str(item).strip()))
        if not selected_doc_ids:
            return {}
        clauses, params = self._scope_clauses(scope)
        clauses.append(f"doc_id in ({_placeholders(selected_doc_ids)})")
        params.extend(selected_doc_ids)
        if chunk_types:
            clauses.append(f"chunk_type in ({_placeholders(chunk_types)})")
            params.extend(sorted(chunk_types))
        rows = self._fetch_all(
            f"""
            select doc_id, count(*) as chunk_count
            from {self._table('document_chunk')}
            where {' and '.join(clauses)}
            group by doc_id
            """,
            tuple(params),
        )
        counts = {doc_id: 0 for doc_id in selected_doc_ids}
        counts.update({str(_row_get(row, "doc_id")): int(_row_get(row, "chunk_count", 0) or 0) for row in rows})
        return counts

    def list_chunks_for_documents(
        self,
        doc_ids: list[str] | tuple[str, ...],
        *,
        chunk_types: set[str] | None = None,
        scope: KnowledgeBaseScope | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        scope = scope or self.default_scope()
        selected_doc_ids = tuple(dict.fromkeys(str(item).strip() for item in doc_ids if str(item).strip()))
        if not selected_doc_ids:
            return []
        clauses, params = self._scope_clauses(scope)
        clauses.append(f"doc_id in ({_placeholders(selected_doc_ids)})")
        params.extend(selected_doc_ids)
        if chunk_types:
            clauses.append(f"chunk_type in ({_placeholders(chunk_types)})")
            params.extend(sorted(chunk_types))
        sql = f"select * from {self._table('document_chunk')} where {' and '.join(clauses)} order by doc_id, created_at, id"
        if limit is not None:
            sql = f"{sql} limit %s"
            params.append(max(0, int(limit)))
        rows = self._fetch_all(sql, tuple(params))
        return [self._decode_row(row) for row in rows]

    def get_chunk(self, chunk_id: str, scope: KnowledgeBaseScope | None = None) -> dict[str, Any] | None:
        scope = scope or self.default_scope()
        clauses, params = self._scope_clauses(scope)
        clauses.insert(0, "id = %s")
        params.insert(0, chunk_id)
        if scope.document_ids:
            clauses.append(f"doc_id in ({_placeholders(scope.document_ids)})")
            params.extend(scope.document_ids)
        row = self._fetch_one(
            f"select * from {self._table('document_chunk')} where {' and '.join(clauses)}",
            tuple(params),
        )
        return self._decode_row(row) if row else None

    def get_document(self, doc_id: str, scope: KnowledgeBaseScope | None = None) -> dict[str, Any] | None:
        scope = scope or self.default_scope()
        if scope.document_ids and doc_id not in scope.document_ids:
            return None
        placeholders = _placeholders(scope.selected_knowledge_base_ids)
        row = self._fetch_one(
            f"select * from {self._table('document')} where id = %s and workspace_id = %s and knowledge_base_id in ({placeholders})",
            (doc_id, scope.workspace_id, *scope.selected_knowledge_base_ids),
        )
        return self._decode_row(row) if row else None

    def get_document_by_path(
        self,
        storage_path: str,
        scope: KnowledgeBaseScope | None = None,
    ) -> dict[str, Any] | None:
        scope = scope or self.default_scope()
        placeholders = _placeholders(scope.selected_knowledge_base_ids)
        row = self._fetch_one(
            f"select * from {self._table('document')} where storage_path = %s and workspace_id = %s and knowledge_base_id in ({placeholders})",
            (storage_path, scope.workspace_id, *scope.selected_knowledge_base_ids),
        )
        return self._decode_row(row) if row else None

    def rebuild_keyword_index(self) -> None:
        return None

    def search_keyword_chunks(
        self,
        query: str,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
        scope: KnowledgeBaseScope | None = None,
    ) -> list[dict[str, Any]]:
        terms = self._keyword_terms(query)
        if not terms:
            return []
        scope = scope or self.default_scope()
        clauses, params = self._scope_clauses(scope, alias="c")
        clauses.append(f"c.chunk_type in ({_placeholders(self.KEYWORD_CHUNK_TYPES)})")
        params.extend(sorted(self.KEYWORD_CHUNK_TYPES))
        doc_ids = tuple(dict.fromkeys((filters or {}).get("doc_ids") or scope.document_ids))
        if doc_ids:
            clauses.append(f"c.doc_id in ({_placeholders(doc_ids)})")
            params.extend(doc_ids)
        ts_query = " | ".join(term.replace("'", "''") for term in terms)
        like_terms = [f"%{term}%" for term in terms]
        where = " and ".join(clauses)
        rows = self._fetch_all(
            f"""
            select c.*,
                   greatest(
                       ts_rank_cd(c.search_vector, websearch_to_tsquery('simple', %s)),
                       max(similarity(c.title_path, term.value)),
                       max(similarity(c.content_markdown, term.value)),
                       max(case when c.content_markdown ilike term.pattern then 0.35 else 0 end)
                   ) as keyword_score
            from {self._table('document_chunk')} c
            cross join unnest(%s::text[], %s::text[]) as term(value, pattern)
            where {where}
              and (
                  c.search_vector @@ websearch_to_tsquery('simple', %s)
                  or c.title_path ilike term.pattern
                  or c.content_markdown ilike term.pattern
                  or c.content ilike term.pattern
              )
            group by c.id
            order by keyword_score desc, c.created_at, c.id
            limit %s
            """,
            (ts_query, terms, like_terms, *params, ts_query, int(top_k)),
        )
        return [self._decode_row(row) for row in rows]

    def update_enrichment(
        self,
        doc_id: str,
        scope: KnowledgeBaseScope,
        *,
        status: str,
        summary: str | None = None,
        keywords: list[str] | None = None,
        suggested_questions: list[str] | None = None,
        error: str | None = None,
        model_ref: str | None = None,
        generated_at: str | None = None,
        source_chunk_ids: list[str] | None = None,
        increment_version: bool = False,
        current_task_id: str | None = None,
        summary_version: int | None = None,
    ) -> dict[str, Any]:
        if self.get_document(doc_id, scope) is None:
            raise KeyError(doc_id)
        assignments = ["summary_status = %s", "updated_at = %s"]
        values: list[Any] = [status, datetime.now().isoformat(timespec="seconds")]
        optional = {
            "summary": summary,
            "keywords_json": _jsonb_param(keywords) if keywords is not None else None,
            "suggested_questions_json": _jsonb_param(suggested_questions) if suggested_questions is not None else None,
            "summary_error": error,
            "summary_model_ref": model_ref,
            "summary_generated_at": generated_at,
            "summary_source_chunk_ids_json": _jsonb_param(source_chunk_ids) if source_chunk_ids is not None else None,
            "current_enrichment_task_id": current_task_id,
        }
        for column, value in optional.items():
            if value is not None:
                cast = "::jsonb" if column.endswith("_json") else ""
                assignments.append(f"{column} = %s{cast}")
                values.append(value)
        if increment_version:
            assignments.append("summary_version = summary_version + 1")
        elif summary_version is not None:
            assignments.append("summary_version = %s")
            values.append(int(summary_version))
        values.extend([doc_id, scope.workspace_id, scope.knowledge_base_id])
        self._execute(
            f"update {self._table('document')} set {', '.join(assignments)} where id = %s and workspace_id = %s and knowledge_base_id = %s",
            tuple(values),
        )
        result = self.get_document(doc_id, scope)
        if result is None:
            raise KeyError(doc_id)
        return result

    def create_enrichment_task(
        self,
        doc_id: str,
        scope: KnowledgeBaseScope,
        *,
        provider_ref: str,
        source_chunk_ids: list[str],
    ) -> dict[str, Any]:
        import uuid

        if self.get_document(doc_id, scope) is None:
            raise KeyError(doc_id)
        now = datetime.now().isoformat(timespec="seconds")
        task_id = f"enrichment-{uuid.uuid4().hex}"
        with self.database.transaction() as conn:
            row = self._fetch_one_in_conn(
                conn,
                f"""
                select coalesce(max(version), 0) + 1 as version
                from {self._table('document_enrichment_task')}
                where doc_id = %s and workspace_id = %s and knowledge_base_id = %s
                """,
                (doc_id, scope.workspace_id, scope.knowledge_base_id),
            )
            version = int(_row_get(row, "version", 1) or 1)
            self._execute_in_conn(
                conn,
                f"""
                insert into {self._table('document_enrichment_task')}(
                    id, doc_id, workspace_id, knowledge_base_id, version, status, provider_ref,
                    error_message, source_chunk_ids_json, started_at, finished_at, created_at
                ) values (%s, %s, %s, %s, %s, 'pending', %s, '', %s::jsonb, null, null, %s)
                """,
                (task_id, doc_id, scope.workspace_id, scope.knowledge_base_id, version, provider_ref, _jsonb_param(source_chunk_ids), now),
            )
            self._execute_in_conn(
                conn,
                f"""
                update {self._table('document')}
                set current_enrichment_task_id = %s, summary_status = 'pending', summary_error = '',
                    summary_version = %s, updated_at = %s
                where id = %s and workspace_id = %s and knowledge_base_id = %s
                """,
                (task_id, version, now, doc_id, scope.workspace_id, scope.knowledge_base_id),
            )
        return self.get_enrichment_task(task_id, scope)

    def update_enrichment_task(
        self,
        task_id: str,
        scope: KnowledgeBaseScope,
        *,
        status: str,
        error_message: str = "",
    ) -> dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        started_at = now if status == "processing" else None
        finished_at = now if status in {"completed", "failed", "skipped"} else None
        rowcount = self._execute(
            f"""
            update {self._table('document_enrichment_task')}
            set status = %s, error_message = %s,
                started_at = coalesce(started_at, %s), finished_at = coalesce(%s, finished_at)
            where id = %s and workspace_id = %s and knowledge_base_id = %s
            """,
            (status, error_message, started_at, finished_at, task_id, scope.workspace_id, scope.knowledge_base_id),
        )
        if rowcount == 0:
            raise KeyError(task_id)
        return self.get_enrichment_task(task_id, scope)

    def get_enrichment_task(self, task_id: str, scope: KnowledgeBaseScope) -> dict[str, Any]:
        row = self._fetch_one(
            f"""
            select * from {self._table('document_enrichment_task')}
            where id = %s and workspace_id = %s and knowledge_base_id = %s
            """,
            (task_id, scope.workspace_id, scope.knowledge_base_id),
        )
        if row is None:
            raise KeyError(task_id)
        return _decode_enrichment_task(row)

    def list_enrichment_tasks(self, doc_id: str, scope: KnowledgeBaseScope) -> list[dict[str, Any]]:
        rows = self._fetch_all(
            f"""
            select * from {self._table('document_enrichment_task')}
            where doc_id = %s and workspace_id = %s and knowledge_base_id = %s
            order by version
            """,
            (doc_id, scope.workspace_id, scope.knowledge_base_id),
        )
        return [_decode_enrichment_task(row) for row in rows]

    def default_scope(self) -> KnowledgeBaseScope:
        return KnowledgeBaseScope(
            workspace_id=self.defaults.workspace_id,
            selected_knowledge_base_ids=(self.defaults.knowledge_base_id,),
            compatibility_default=True,
        )

    def _insert_chunks(
        self,
        conn: Any,
        chunks: list[Chunk],
        workspace_id: str,
        knowledge_base_id: str,
        now: str,
    ) -> None:
        rows = []
        for chunk in chunks:
            metadata = self._normalized_chunk_metadata(chunk)
            rows.append(
                (
                    chunk.id,
                    chunk.doc_id,
                    workspace_id,
                    knowledge_base_id,
                    chunk.parent_id,
                    chunk.chunk_type,
                    chunk.title_path,
                    chunk.content,
                    chunk.content_markdown,
                    chunk.page_start,
                    chunk.page_end,
                    chunk.token_count,
                    _jsonb_param(metadata),
                    now,
                )
            )
        if not rows:
            return
        with conn.cursor() as cur:
            cur.executemany(
                f"""
                insert into {self._table('document_chunk')}
                (id, doc_id, workspace_id, knowledge_base_id, parent_id, chunk_type, title_path, content, content_markdown,
                 page_start, page_end, token_count, metadata_json, created_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                rows,
            )

    def _document_owner(
        self,
        conn: Any,
        doc_id: str,
        scope: KnowledgeBaseScope | None,
    ) -> tuple[str, str]:
        row = self._fetch_one_in_conn(
            conn,
            f"select workspace_id, knowledge_base_id from {self._table('document')} where id = %s",
            (doc_id,),
        )
        if row is None:
            raise ValueError(f"Cannot write chunks for missing document {doc_id!r}")
        workspace_id = str(_row_get(row, "workspace_id"))
        knowledge_base_id = str(_row_get(row, "knowledge_base_id"))
        if scope is not None and not scope.contains(workspace_id, knowledge_base_id):
            raise ValueError("Document is outside the active knowledge base scope")
        return workspace_id, knowledge_base_id

    def _validate_chunk_ownership(
        self,
        doc_id: str,
        chunks: list[Chunk],
        workspace_id: str,
        knowledge_base_id: str,
    ) -> None:
        for chunk in chunks:
            if chunk.doc_id != doc_id:
                raise ValueError(f"Chunk {chunk.id!r} belongs to a different document")
            chunk_workspace = str(chunk.metadata.get("workspace_id", workspace_id))
            chunk_knowledge_base = str(chunk.metadata.get("knowledge_base_id", knowledge_base_id))
            if chunk_workspace != workspace_id or chunk_knowledge_base != knowledge_base_id:
                raise ValueError(f"Chunk {chunk.id!r} ownership does not match document")

    def _normalized_chunk_metadata(self, chunk: Chunk) -> dict[str, Any]:
        metadata = dict(chunk.metadata)
        metadata.setdefault("processing_version", PROCESSING_VERSION)
        metadata.setdefault("size_unit", "chars")
        metadata.setdefault("strategy", self._default_chunk_strategy(chunk))
        if chunk.chunk_type in {"image_ocr", "image_caption", "ocr"}:
            metadata.setdefault("generated_evidence", chunk.chunk_type in {"image_ocr", "image_caption"})
        return metadata

    def _default_chunk_strategy(self, chunk: Chunk) -> str:
        if chunk.chunk_type in {"image_ocr", "image_caption"}:
            return chunk.chunk_type
        if chunk.chunk_type == "ocr":
            return "ocr"
        if chunk.chunk_type == "table":
            return "table"
        return "legacy"

    def _keyword_terms(self, query: str) -> list[str]:
        tokens = re.findall(r"[\w.\-:/]+", query, flags=re.UNICODE)
        normalized = []
        for token in tokens:
            token = token.strip("-_:/.")
            if len(token) >= 2:
                normalized.append(token)
        return list(dict.fromkeys(normalized))

    def _scope_clauses(self, scope: KnowledgeBaseScope, *, alias: str = "") -> tuple[list[str], list[Any]]:
        prefix = f"{alias}." if alias else ""
        placeholders = _placeholders(scope.selected_knowledge_base_ids)
        return [f"{prefix}workspace_id = %s", f"{prefix}knowledge_base_id in ({placeholders})"], [
            scope.workspace_id,
            *scope.selected_knowledge_base_ids,
        ]

    def _fetch_one(self, sql: str, params: tuple[Any, ...]) -> Any | None:
        with self.database.connection() as conn:
            return self._fetch_one_in_conn(conn, sql, params)

    def _fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())

    def _execute(self, sql: str, params: tuple[Any, ...]) -> int:
        with self.database.transaction() as conn:
            return self._execute_in_conn(conn, sql, params)

    def _fetch_one_in_conn(self, conn: Any, sql: str, params: tuple[Any, ...]) -> Any | None:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _execute_in_conn(self, conn: Any, sql: str, params: tuple[Any, ...]) -> int:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return int(getattr(cur, "rowcount", 0) or 0)

    def _assert_active_knowledge_base(
        self,
        conn: Any,
        workspace_id: str,
        knowledge_base_id: str,
    ) -> None:
        row = self._fetch_one_in_conn(
            conn,
            f"""
            select 1 from {self._table('knowledge_base')}
            where id = %s and workspace_id = %s and status = 'active'
            """,
            (knowledge_base_id, workspace_id),
        )
        if row is None:
            raise ValueError("Knowledge base does not exist, is archived, or belongs to another workspace")

    def _decode_row(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        if "metadata_json" in data:
            data["metadata_json"] = _load_json(data["metadata_json"], {})
        for field_name in ("keywords_json", "suggested_questions_json", "summary_source_chunk_ids_json"):
            if field_name in data:
                data[field_name] = _load_json(data[field_name], [])
        _normalize_timestamp_fields(data, ("created_at", "updated_at", "summary_generated_at"))
        return data

    def _table(self, table: str) -> str:
        return qname(self.schema, table)


def _decode_enrichment_task(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["source_chunk_ids"] = _load_json(data.pop("source_chunk_ids_json", None), [])
    _normalize_timestamp_fields(data, ("created_at", "started_at", "finished_at"))
    return data


def _jsonb_param(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _load_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value or json.dumps(default))
    return value


def _normalize_timestamp_fields(data: dict[str, Any], fields: tuple[str, ...]) -> None:
    for field in fields:
        value = data.get(field)
        if isinstance(value, datetime):
            data[field] = value.isoformat(timespec="seconds")


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


def _placeholders(items: Any) -> str:
    count = len(tuple(items))
    if count <= 0:
        raise ValueError("At least one value is required for SQL placeholders")
    return ", ".join("%s" for _ in range(count))
