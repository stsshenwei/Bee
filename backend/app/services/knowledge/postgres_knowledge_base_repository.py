from __future__ import annotations

import json
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
from app.services.storage.postgres import PostgresDatabase, PostgresIntegrityError
from app.services.storage.postgres_schema import (
    PostgresSchemaConfig,
    inspect_postgres_startup_storage,
    qname,
)
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings


class PostgresKnowledgeBaseRepository:
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
            inspect_postgres_startup_storage(
                database,
                config=PostgresSchemaConfig(schema=self.schema),
            )

    def ensure_defaults(self) -> tuple[Workspace, KnowledgeBase]:
        workspace = self.get_workspace(self.defaults.workspace_id)
        knowledge_base = self.get_knowledge_base(self.defaults.knowledge_base_id)
        if workspace is None or knowledge_base is None:
            raise RuntimeError("Default workspace and knowledge base were not created")
        return workspace, knowledge_base

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        row = self._fetch_one(
            f"select * from {self._table('workspace')} where id = %s",
            (workspace_id,),
        )
        return self._decode_workspace(row) if row else None

    def create_knowledge_base(self, knowledge_base: KnowledgeBase, set_as_default: bool = False) -> KnowledgeBase:
        try:
            with self.database.transaction() as conn:
                with conn.cursor() as cur:
                    if set_as_default or knowledge_base.is_default:
                        cur.execute(
                            f"update {self._table('knowledge_base')} set is_default = false where workspace_id = %s",
                            (knowledge_base.workspace_id,),
                        )
                    cur.execute(
                        f"""
                        insert into {self._table('knowledge_base')}(
                            id, workspace_id, name, description, type, is_default, status,
                            indexing_strategy_json, provider_config_json, reset_required, created_at, updated_at
                        ) values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                        """,
                        (
                            knowledge_base.id,
                            knowledge_base.workspace_id,
                            knowledge_base.name,
                            knowledge_base.description,
                            knowledge_base.type,
                            bool(set_as_default or knowledge_base.is_default),
                            knowledge_base.status,
                            _jsonb_param(knowledge_base.indexing_strategy.to_dict()),
                            _jsonb_param(knowledge_base.provider_config.to_dict()),
                            bool(knowledge_base.aggregate.reset_required),
                            knowledge_base.created_at,
                            knowledge_base.updated_at,
                        ),
                    )
        except Exception as exc:
            _raise_integrity_if_needed(exc)
            raise
        created = self.get_knowledge_base(knowledge_base.id)
        if created is None:
            raise RuntimeError("Knowledge base creation did not persist")
        return created

    def list_knowledge_bases(self, workspace_id: str, include_archived: bool = False) -> list[KnowledgeBase]:
        status_clause = "" if include_archived else " and kb.status = 'active'"
        rows = self._fetch_all(
            self._knowledge_base_select()
            + f" where kb.workspace_id = %s{status_clause} order by kb.updated_at desc",
            (workspace_id,),
        )
        return [self._decode_knowledge_base(row) for row in rows]

    def get_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBase | None:
        row = self._fetch_one(
            self._knowledge_base_select() + " where kb.id = %s",
            (knowledge_base_id,),
        )
        return self._decode_knowledge_base(row) if row else None

    def get_default_knowledge_base(self, workspace_id: str) -> KnowledgeBase | None:
        row = self._fetch_one(
            self._knowledge_base_select()
            + " where kb.workspace_id = %s and kb.status = 'active' and kb.is_default is true",
            (workspace_id,),
        )
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
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in selected.items():
            if key in {"indexing_strategy_json", "provider_config_json"}:
                assignments.append(f"{key} = %s::jsonb")
                params.append(_jsonb_param(value))
            elif key == "reset_required":
                assignments.append(f"{key} = %s")
                params.append(bool(value))
            else:
                assignments.append(f"{key} = %s")
                params.append(value)
        params.append(knowledge_base_id)
        try:
            rowcount = self._execute(
                f"update {self._table('knowledge_base')} set {', '.join(assignments)} where id = %s",
                tuple(params),
            )
        except Exception as exc:
            _raise_integrity_if_needed(exc)
            raise
        if rowcount == 0:
            raise KeyError(knowledge_base_id)
        updated = self.get_knowledge_base(knowledge_base_id)
        if updated is None:
            raise KeyError(knowledge_base_id)
        return updated

    def set_knowledge_base_status(self, knowledge_base_id: str, status: str) -> KnowledgeBase:
        if status not in {"active", "archived"}:
            raise ValueError("Unsupported knowledge base status")
        rowcount = self._execute(
            f"update {self._table('knowledge_base')} set status = %s, updated_at = %s where id = %s",
            (status, utc_now_iso(), knowledge_base_id),
        )
        if rowcount == 0:
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
        now = utc_now_iso()
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"update {self._table('knowledge_base')} set is_default = false, updated_at = %s where workspace_id = %s",
                    (now, current.workspace_id),
                )
                cur.execute(
                    f"update {self._table('knowledge_base')} set is_default = true, updated_at = %s where id = %s",
                    (now, knowledge_base_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(knowledge_base_id)
        result = self.get_knowledge_base(knowledge_base_id)
        if result is None:
            raise KeyError(knowledge_base_id)
        return result

    def _knowledge_base_select(self) -> str:
        return f"""
            select kb.*,
                   (select count(*) from {self._table('document')} d
                    where d.workspace_id = kb.workspace_id and d.knowledge_base_id = kb.id) as document_count,
                   (select count(*) from {self._table('document_chunk')} c
                    where c.workspace_id = kb.workspace_id and c.knowledge_base_id = kb.id
                      and c.chunk_type in ('child', 'table', 'ocr', 'image_ocr', 'image_caption')) as indexed_chunk_count,
                   (select count(*) from {self._table('document')} d
                    where d.workspace_id = kb.workspace_id and d.knowledge_base_id = kb.id
                      and d.parse_status in ('uploaded', 'pending', 'parsing', 'processing')) as processing_count,
                   (select count(*) from {self._table('document')} d
                    where d.workspace_id = kb.workspace_id and d.knowledge_base_id = kb.id
                      and d.parse_status = 'failed') as failed_count,
                   (select count(*) from {self._table('wiki_page')} wp
                    where wp.workspace_id = kb.workspace_id and wp.knowledge_base_id = kb.id
                      and wp.status != 'archived') as wiki_page_count,
                   (select count(*) from {self._table('wiki_page_issue')} wi
                    where wi.workspace_id = kb.workspace_id and wi.knowledge_base_id = kb.id
                      and wi.status = 'open') as wiki_issue_count
            from {self._table('knowledge_base')} kb
        """

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

    def _decode_workspace(self, row: Any) -> Workspace:
        return Workspace(**{key: _row_get(row, key) for key in Workspace.__dataclass_fields__})

    def _decode_knowledge_base(self, row: Any) -> KnowledgeBase:
        indexing = IndexingStrategy.from_dict(_load_json(_row_get(row, "indexing_strategy_json"), {}))
        raw_provider = _load_json(_row_get(row, "provider_config_json"), {})
        provider = EffectiveKnowledgeBaseConfig(
            requested=ProviderReferences.from_dict(raw_provider.get("requested")),
            effective=ProviderReferences.from_dict(raw_provider.get("effective")),
            inactive_overrides=tuple(raw_provider.get("inactive_overrides") or ()),
        )
        aggregate = KnowledgeBaseAggregate(
            document_count=int(_row_get(row, "document_count", 0) or 0),
            indexed_chunk_count=int(_row_get(row, "indexed_chunk_count", 0) or 0),
            processing_count=int(_row_get(row, "processing_count", 0) or 0),
            failed_count=int(_row_get(row, "failed_count", 0) or 0),
            wiki_page_count=int(_row_get(row, "wiki_page_count", 0) or 0),
            wiki_issue_count=int(_row_get(row, "wiki_issue_count", 0) or 0),
            reset_required=_bool(_row_get(row, "reset_required", False)),
        )
        return KnowledgeBase(
            id=str(_row_get(row, "id")),
            workspace_id=str(_row_get(row, "workspace_id")),
            name=str(_row_get(row, "name")),
            description=str(_row_get(row, "description")),
            type=str(_row_get(row, "type")),
            is_default=_bool(_row_get(row, "is_default", False)),
            status=str(_row_get(row, "status")),
            indexing_strategy=indexing,
            provider_config=provider,
            aggregate=aggregate,
            created_at=str(_row_get(row, "created_at")),
            updated_at=str(_row_get(row, "updated_at")),
        )


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


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "on"}
    return bool(value)


def _raise_integrity_if_needed(exc: Exception) -> None:
    try:
        from psycopg.errors import IntegrityError as PsycopgIntegrityError
    except Exception:
        PsycopgIntegrityError = ()  # type: ignore[assignment]
    if isinstance(exc, PsycopgIntegrityError) or exc.__class__.__name__.endswith("IntegrityError"):
        raise PostgresIntegrityError(str(exc)) from exc
