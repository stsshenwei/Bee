from typing import Any

from app.models.document_models import Chunk
from app.models.knowledge_base import KnowledgeBaseScope
from app.services.retrieval.retrieval_models import RetrievedChunk


class MilvusKeywordSearch:
    def __init__(self, vector_store: Any):
        self.vector_store = vector_store

    def index(self, chunks: list[Chunk]) -> None:
        self.vector_store.upsert_chunks(chunks)

    def search(self, query: str, top_k: int, filters: dict[str, Any] | None = None) -> list[RetrievedChunk]:
        scope = (filters or {}).get("scope")
        if scope is None:
            raw_hits = self.vector_store.query_bm25(query, top_k)
        else:
            raw_hits = self.vector_store.query_bm25(query, top_k, scope=scope)
        results: list[RetrievedChunk] = []
        for hit in raw_hits:
            metadata = hit.get("metadata", {})
            chunk_id = str(metadata.get("chunk_id") or metadata.get("child_id") or "")
            doc_id = str(metadata.get("doc_id") or "")
            if filters:
                doc_ids = filters.get("doc_ids")
                if doc_ids and doc_id not in set(doc_ids):
                    continue
            score = float(hit.get("bm25_score", hit.get("keyword_score", 0.0)))
            results.append(
                RetrievedChunk(
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                    parent_id=str(metadata.get("parent_id") or ""),
                    content=str(hit.get("content", "")),
                    chunk_type=str(metadata.get("chunk_type") or "child"),
                    title_path=str(metadata.get("title_path") or ""),
                    page_start=metadata.get("page_start"),
                    page_end=metadata.get("page_end"),
                    score=score,
                    bm25_score=score,
                    metadata=dict(metadata),
                )
            )
        return results[:top_k]

    def delete_by_doc_id(self, doc_id: str) -> None:
        self.vector_store.delete_document(doc_id)


class PostgresKeywordSearch:
    def __init__(self, repository: Any):
        self.repository = repository

    def index(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        doc_ids = {chunk.doc_id for chunk in chunks}
        if len(doc_ids) != 1:
            raise ValueError("PostgresKeywordSearch.index expects chunks from one document")
        self.repository.replace_chunks(next(iter(doc_ids)), chunks)

    def search(self, query: str, top_k: int, filters: dict[str, Any] | None = None) -> list[RetrievedChunk]:
        scope = (filters or {}).get("scope")
        rows = self.repository.search_keyword_chunks(query, top_k=top_k, filters=filters, scope=scope)
        results: list[RetrievedChunk] = []
        for row in rows:
            score = float(row.get("keyword_score", 0.0))
            metadata = dict(row.get("metadata_json", {}))
            metadata.update(
                {
                    "chunk_id": row.get("id", ""),
                    "child_id": row.get("id", ""),
                    "doc_id": row.get("doc_id", ""),
                    "workspace_id": row.get("workspace_id", ""),
                    "knowledge_base_id": row.get("knowledge_base_id", ""),
                    "parent_id": row.get("parent_id") or "",
                    "chunk_type": row.get("chunk_type", ""),
                    "title_path": row.get("title_path", ""),
                    "page_start": row.get("page_start"),
                    "page_end": row.get("page_end"),
                }
            )
            results.append(
                RetrievedChunk(
                    chunk_id=str(row.get("id", "")),
                    doc_id=str(row.get("doc_id", "")),
                    parent_id=str(row.get("parent_id") or ""),
                    content=str(row.get("content", "")),
                    chunk_type=str(row.get("chunk_type") or "child"),
                    title_path=str(row.get("title_path") or ""),
                    page_start=row.get("page_start"),
                    page_end=row.get("page_end"),
                    score=score,
                    bm25_score=score,
                    metadata=metadata,
                )
            )
        return results[:top_k]

    def delete_by_doc_id(self, doc_id: str) -> None:
        self.repository.delete_document(doc_id)


SQLiteFTSKeywordSearch = PostgresKeywordSearch
