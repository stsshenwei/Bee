from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.models.document_models import Chunk
from app.models.knowledge_base import KnowledgeBaseScope
from app.services.retrieval.embedding_provider import EmbeddingProvider
from app.services.retrieval.vector_store import _default_scope, _scope_from_chunks
from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import PostgresSchemaConfig, inspect_postgres_startup_storage, qname


class PostgresVectorDimensionError(RuntimeError):
    pass


class PostgresVectorStore:
    INDEXABLE_CHUNK_TYPES = {"child", "table", "ocr", "image_ocr", "image_caption"}

    def __init__(
        self,
        database: PostgresDatabase,
        embedding_dim: int,
        embedding_provider: EmbeddingProvider,
        state_dir: str | Path = "./vector_db",
        *,
        schema: str | None = None,
        vector_type: str = "vector",
        validate_schema: bool = True,
    ):
        self.database = database
        self.embedding_dim = int(embedding_dim)
        self.embedding_provider = embedding_provider
        self.persist_dir = Path(state_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.schema = schema or database.settings.schema
        self.vector_type = vector_type.strip().lower() or "vector"
        self.reset_required = False
        if validate_schema:
            inspect_postgres_startup_storage(
                database,
                config=PostgresSchemaConfig(
                    schema=self.schema,
                    vector_dimension=self.embedding_dim,
                    vector_type=self.vector_type,
                ),
            )

    @property
    def items(self) -> list[dict[str, Any]]:
        return []

    def reset_collection(self) -> None:
        self._execute(f"delete from {self._table('document_chunk_embedding')}", ())

    def delete_document(self, doc_id: str, scope: KnowledgeBaseScope | None = None) -> None:
        scope = scope or _default_scope()
        clauses, params = self._scope_clauses(scope)
        clauses.insert(0, "doc_id = %s")
        params.insert(0, doc_id)
        self._execute(
            f"delete from {self._table('document_chunk_embedding')} where {' and '.join(clauses)}",
            tuple(params),
        )

    def replace_document_chunks(
        self,
        doc_id: str,
        chunks: list[Chunk],
        scope: KnowledgeBaseScope | None = None,
    ) -> None:
        scope = scope or _scope_from_chunks(chunks)
        self.delete_document(doc_id, scope=scope)
        self.upsert_chunks(chunks)

    def upsert_chunks(self, chunks: list[Chunk]) -> None:
        indexable = [chunk for chunk in chunks if chunk.chunk_type in self.INDEXABLE_CHUNK_TYPES]
        if not indexable:
            return
        embeddings = self.embedding_provider.embed_batch([chunk.embedding_text for chunk in indexable])
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for chunk, embedding in zip(indexable, embeddings):
            self._validate_embedding(embedding)
            metadata = dict(chunk.metadata or {})
            rows.append(
                (
                    chunk.id,
                    chunk.doc_id,
                    str(metadata.get("workspace_id", "default-workspace")),
                    str(metadata.get("knowledge_base_id", "default-knowledge-base")),
                    chunk.parent_id or "",
                    chunk.chunk_type,
                    chunk.title_path,
                    int(chunk.page_start or 0),
                    int(chunk.page_end or 0),
                    _vector_literal(embedding),
                    chunk.embedding_text,
                    _jsonb_param(
                        {
                            "strategy": chunk.strategy,
                            "processing_version": chunk.processing_version,
                            "size_unit": chunk.size_unit,
                            "image_id": chunk.image_id,
                            "storage_key": chunk.storage_key,
                            "source_type": str(metadata.get("source_type", "")),
                        }
                    ),
                    now,
                    now,
                )
            )
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    f"""
                    insert into {self._table('document_chunk_embedding')}(
                        chunk_id, doc_id, workspace_id, knowledge_base_id, parent_id, chunk_type,
                        title_path, page_start, page_end, embedding, retrieval_text, metadata_json,
                        created_at, updated_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::{self.vector_type}, %s, %s::jsonb, %s, %s)
                    on conflict(chunk_id) do update set
                        doc_id = excluded.doc_id,
                        workspace_id = excluded.workspace_id,
                        knowledge_base_id = excluded.knowledge_base_id,
                        parent_id = excluded.parent_id,
                        chunk_type = excluded.chunk_type,
                        title_path = excluded.title_path,
                        page_start = excluded.page_start,
                        page_end = excluded.page_end,
                        embedding = excluded.embedding,
                        retrieval_text = excluded.retrieval_text,
                        metadata_json = excluded.metadata_json,
                        updated_at = excluded.updated_at
                    """,
                    rows,
                )

    def upsert(self, ids: list[str], docs: list[str], metadatas: list[dict[str, Any]]) -> None:
        raise RuntimeError("Use upsert_chunks with structured Chunk objects")

    def replace_all(self, ids: list[str], docs: list[str], metadatas: list[dict[str, Any]]) -> None:
        raise RuntimeError("Use structured ingest with DocumentChunker and upsert_chunks")

    def query(
        self,
        question: str,
        top_k: int,
        scope: KnowledgeBaseScope | None = None,
    ) -> list[dict[str, Any]]:
        scope = scope or _default_scope()
        if self.count() == 0:
            return []
        query_embedding = self.embedding_provider.embed_text(question)
        self._validate_embedding(query_embedding)
        clauses, params = self._scope_clauses(scope, alias="e")
        if scope.document_ids:
            clauses.append(f"e.doc_id in ({_placeholders(scope.document_ids)})")
            params.extend(scope.document_ids)
        vector = _vector_literal(query_embedding)
        rows = self._fetch_all(
            f"""
            select e.*, e.embedding <=> %s::{self.vector_type} as distance
            from {self._table('document_chunk_embedding')} e
            where {' and '.join(clauses)}
            order by e.embedding <=> %s::{self.vector_type}, e.updated_at desc, e.chunk_id
            limit %s
            """,
            (vector, *params, vector, int(top_k)),
        )
        return [self._decode_hit(row) for row in rows]

    def query_dense(
        self,
        question: str,
        top_k: int,
        scope: KnowledgeBaseScope | None = None,
    ) -> list[dict[str, Any]]:
        return self.query(question, top_k, scope=scope)

    def search_dense(
        self,
        question: str,
        top_k: int,
        scope: KnowledgeBaseScope | None = None,
    ) -> list[dict[str, Any]]:
        return self.query_dense(question, top_k, scope=scope)

    def query_bm25(
        self,
        question: str,
        top_k: int,
        scope: KnowledgeBaseScope | None = None,
    ) -> list[dict[str, Any]]:
        return []

    def search_bm25(
        self,
        question: str,
        top_k: int,
        scope: KnowledgeBaseScope | None = None,
    ) -> list[dict[str, Any]]:
        return self.query_bm25(question, top_k, scope=scope)

    def delete_knowledge_base(self, scope: KnowledgeBaseScope) -> None:
        clauses, params = self._scope_clauses(scope)
        self._execute(
            f"delete from {self._table('document_chunk_embedding')} where {' and '.join(clauses)}",
            tuple(params),
        )

    def count(self) -> int:
        row = self._fetch_one(f"select count(*) as count from {self._table('document_chunk_embedding')}", ())
        return int(_row_get(row, "count", 0) or 0)

    def _decode_hit(self, row: Any) -> dict[str, Any]:
        metadata_json = _load_json(_row_get(row, "metadata_json"), {})
        chunk_id = str(_row_get(row, "chunk_id") or "")
        distance = float(_row_get(row, "distance", 0.0) or 0.0)
        metadata = {
            "child_id": chunk_id,
            "chunk_id": chunk_id,
            "doc_id": str(_row_get(row, "doc_id") or ""),
            "workspace_id": str(_row_get(row, "workspace_id") or ""),
            "knowledge_base_id": str(_row_get(row, "knowledge_base_id") or ""),
            "parent_id": str(_row_get(row, "parent_id") or ""),
            "chunk_type": str(_row_get(row, "chunk_type") or ""),
            "strategy": str(metadata_json.get("strategy") or ""),
            "processing_version": str(metadata_json.get("processing_version") or ""),
            "size_unit": str(metadata_json.get("size_unit") or ""),
            "image_id": str(metadata_json.get("image_id") or ""),
            "storage_key": str(metadata_json.get("storage_key") or ""),
            "source_type": str(metadata_json.get("source_type") or ""),
            "title_path": str(_row_get(row, "title_path") or ""),
            "page_start": _row_get(row, "page_start"),
            "page_end": _row_get(row, "page_end"),
        }
        return {
            "content": "",
            "metadata": metadata,
            "distance": distance,
            "vector_score": max(0.0, 1.0 - distance),
        }

    def _validate_embedding(self, embedding: list[float]) -> None:
        if len(embedding) != self.embedding_dim:
            raise PostgresVectorDimensionError(
                f"Embedding dimension {len(embedding)} does not match PostgreSQL vector dimension {self.embedding_dim}"
            )

    def _scope_clauses(self, scope: KnowledgeBaseScope, *, alias: str = "") -> tuple[list[str], list[Any]]:
        prefix = f"{alias}." if alias else ""
        placeholders = _placeholders(scope.selected_knowledge_base_ids)
        return [f"{prefix}workspace_id = %s", f"{prefix}knowledge_base_id in ({placeholders})"], [
            scope.workspace_id,
            *scope.selected_knowledge_base_ids,
        ]

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


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in values) + "]"


def _jsonb_param(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value or json.dumps(default))


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    return row.get(key, default) if hasattr(row, "get") else row[key]


def _placeholders(items: Any) -> str:
    count = len(tuple(items))
    if count <= 0:
        raise ValueError("At least one value is required")
    return ", ".join("%s" for _ in range(count))
