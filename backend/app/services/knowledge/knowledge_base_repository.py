from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.models.knowledge_base import (
    EffectiveKnowledgeBaseConfig,
    IndexingStrategy,
    KnowledgeBase,
    KnowledgeBaseAggregate,
    ProviderReferences,
    Workspace,
    utc_now_iso,
)
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings, initialize_metadata_database


class KnowledgeBaseRepository:
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
        conn.execute("begin")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_defaults(self) -> tuple[Workspace, KnowledgeBase]:
        initialize_metadata_database(self.db_path, self.defaults)
        workspace = self.get_workspace(self.defaults.workspace_id)
        knowledge_base = self.get_knowledge_base(self.defaults.knowledge_base_id)
        if workspace is None or knowledge_base is None:
            raise RuntimeError("Default workspace and knowledge base were not created")
        return workspace, knowledge_base

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        with self._connect() as conn:
            row = conn.execute("select * from workspace where id = ?", (workspace_id,)).fetchone()
        return self._decode_workspace(row) if row else None

    def create_knowledge_base(self, knowledge_base: KnowledgeBase, set_as_default: bool = False) -> KnowledgeBase:
        with self._connect() as conn:
            if set_as_default or knowledge_base.is_default:
                conn.execute(
                    "update knowledge_base set is_default = 0 where workspace_id = ?",
                    (knowledge_base.workspace_id,),
                )
            conn.execute(
                """
                insert into knowledge_base(
                    id, workspace_id, name, description, type, is_default, status,
                    indexing_strategy_json, provider_config_json, reset_required, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    knowledge_base.id,
                    knowledge_base.workspace_id,
                    knowledge_base.name,
                    knowledge_base.description,
                    knowledge_base.type,
                    int(set_as_default or knowledge_base.is_default),
                    knowledge_base.status,
                    json.dumps(knowledge_base.indexing_strategy.to_dict(), ensure_ascii=False),
                    json.dumps(knowledge_base.provider_config.to_dict(), ensure_ascii=False),
                    int(knowledge_base.aggregate.reset_required),
                    knowledge_base.created_at,
                    knowledge_base.updated_at,
                ),
            )
        created = self.get_knowledge_base(knowledge_base.id)
        if created is None:
            raise RuntimeError("Knowledge base creation did not persist")
        return created

    def list_knowledge_bases(self, workspace_id: str, include_archived: bool = False) -> list[KnowledgeBase]:
        status_clause = "" if include_archived else " and kb.status = 'active'"
        with self._connect() as conn:
            rows = conn.execute(
                self._knowledge_base_select() + f" where kb.workspace_id = ?{status_clause} group by kb.id order by kb.updated_at desc",
                (workspace_id,),
            ).fetchall()
        return [self._decode_knowledge_base(row) for row in rows]

    def get_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBase | None:
        with self._connect() as conn:
            row = conn.execute(
                self._knowledge_base_select() + " where kb.id = ? group by kb.id",
                (knowledge_base_id,),
            ).fetchone()
        return self._decode_knowledge_base(row) if row else None

    def get_default_knowledge_base(self, workspace_id: str) -> KnowledgeBase | None:
        with self._connect() as conn:
            row = conn.execute(
                self._knowledge_base_select()
                + " where kb.workspace_id = ? and kb.status = 'active' and kb.is_default = 1 group by kb.id",
                (workspace_id,),
            ).fetchone()
        return self._decode_knowledge_base(row) if row else None

    def update_knowledge_base(self, knowledge_base_id: str, changes: dict[str, Any]) -> KnowledgeBase:
        allowed = {"name", "description", "indexing_strategy_json", "provider_config_json", "reset_required"}
        selected = {key: value for key, value in changes.items() if key in allowed}
        if not selected:
            current = self.get_knowledge_base(knowledge_base_id)
            if current is None:
                raise KeyError(knowledge_base_id)
            return current
        selected["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{key} = ?" for key in selected)
        with self._connect() as conn:
            cursor = conn.execute(
                f"update knowledge_base set {assignments} where id = ?",
                (*selected.values(), knowledge_base_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(knowledge_base_id)
        updated = self.get_knowledge_base(knowledge_base_id)
        if updated is None:
            raise KeyError(knowledge_base_id)
        return updated

    def set_knowledge_base_status(self, knowledge_base_id: str, status: str) -> KnowledgeBase:
        if status not in {"active", "archived"}:
            raise ValueError("Unsupported knowledge base status")
        with self._connect() as conn:
            cursor = conn.execute(
                "update knowledge_base set status = ?, updated_at = ? where id = ?",
                (status, utc_now_iso(), knowledge_base_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(knowledge_base_id)
        result = self.get_knowledge_base(knowledge_base_id)
        if result is None:
            raise KeyError(knowledge_base_id)
        return result

    def set_default_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBase:
        current = self.get_knowledge_base(knowledge_base_id)
        if current is None:
            raise KeyError(knowledge_base_id)
        if current.status != "active":
            raise ValueError("Default knowledge base must be active")
        with self._connect() as conn:
            conn.execute(
                "update knowledge_base set is_default = 0, updated_at = ? where workspace_id = ?",
                (utc_now_iso(), current.workspace_id),
            )
            cursor = conn.execute(
                "update knowledge_base set is_default = 1, updated_at = ? where id = ?",
                (utc_now_iso(), knowledge_base_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(knowledge_base_id)
        result = self.get_knowledge_base(knowledge_base_id)
        if result is None:
            raise KeyError(knowledge_base_id)
        return result

    def _knowledge_base_select(self) -> str:
        if not self._table_exists("document"):
            return """
                select kb.*,
                       0 as document_count,
                       0 as indexed_chunk_count,
                       0 as processing_count,
                       0 as failed_count,
                       0 as wiki_page_count,
                       0 as wiki_issue_count
                from knowledge_base kb
            """
        wiki_counts = (
            """
                   (select count(*)
                    from wiki_page wp
                    where wp.workspace_id = kb.workspace_id
                      and wp.knowledge_base_id = kb.id
                      and wp.status != 'archived') as wiki_page_count,
                   (select count(*)
                    from wiki_page_issue wi
                    where wi.workspace_id = kb.workspace_id
                      and wi.knowledge_base_id = kb.id
                      and wi.status = 'open') as wiki_issue_count
            """
            if self._table_exists("wiki_page")
            else """
                   0 as wiki_page_count,
                   0 as wiki_issue_count
            """
        )
        return """
            select kb.*,
                   count(distinct d.id) as document_count,
                   count(distinct case when c.chunk_type in ('child', 'table', 'ocr', 'image_ocr', 'image_caption') then c.id end) as indexed_chunk_count,
                   count(distinct case when d.parse_status in ('uploaded', 'pending', 'parsing', 'processing') then d.id end) as processing_count,
                   count(distinct case when d.parse_status = 'failed' then d.id end) as failed_count,
        """ + wiki_counts + """
            from knowledge_base kb
            left join document d on d.knowledge_base_id = kb.id and d.workspace_id = kb.workspace_id
            left join document_chunk c on c.doc_id = d.id and c.knowledge_base_id = kb.id
        """

    def _table_exists(self, table_name: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "select 1 from sqlite_master where type = 'table' and name = ?", (table_name,)
            ).fetchone()
        return row is not None

    def _decode_workspace(self, row: sqlite3.Row) -> Workspace:
        return Workspace(**{key: row[key] for key in Workspace.__dataclass_fields__})

    def _decode_knowledge_base(self, row: sqlite3.Row) -> KnowledgeBase:
        indexing = IndexingStrategy.from_dict(json.loads(row["indexing_strategy_json"] or "{}"))
        raw_provider = json.loads(row["provider_config_json"] or "{}")
        provider = EffectiveKnowledgeBaseConfig(
            requested=ProviderReferences.from_dict(raw_provider.get("requested")),
            effective=ProviderReferences.from_dict(raw_provider.get("effective")),
            inactive_overrides=tuple(raw_provider.get("inactive_overrides") or ()),
        )
        aggregate = KnowledgeBaseAggregate(
            document_count=int(row["document_count"] or 0),
            indexed_chunk_count=int(row["indexed_chunk_count"] or 0),
            processing_count=int(row["processing_count"] or 0),
            failed_count=int(row["failed_count"] or 0),
            wiki_page_count=int(row["wiki_page_count"] or 0),
            wiki_issue_count=int(row["wiki_issue_count"] or 0),
            reset_required=bool(row["reset_required"]),
        )
        return KnowledgeBase(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            name=str(row["name"]),
            description=str(row["description"]),
            type=str(row["type"]),
            is_default=bool(row["is_default"]),
            status=str(row["status"]),
            indexing_strategy=indexing,
            provider_config=provider,
            aggregate=aggregate,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
