from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Iterator

from app.models.knowledge_base import KnowledgeBaseScope
from app.models.wiki import WikiGenerationTask, wiki_now_iso
from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import (
    PostgresSchemaConfig,
    inspect_postgres_startup_storage,
    qname,
)
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings
from app.services.wiki.wiki_repository import (
    WikiRepository,
    _clean_text,
    _json_dumps,
    _qualify_page_clause,
    normalize_wiki_slug,
)


_WIKI_JSON_COLUMNS = {
    "category_path_json",
    "source_refs_json",
    "chunk_refs_json",
    "in_links_json",
    "out_links_json",
    "aliases_json",
    "metadata_json",
    "suspected_doc_ids_json",
    "suspected_chunk_ids_json",
    "payload_json",
    "config_json",
    "contribution_json",
    "source_chunk_ids_json",
    "page_slugs_json",
    "affected_slugs_json",
}

_TABLE_NAMES = (
    "knowledge_base",
    "document",
    "wiki_folder",
    "wiki_page_source_ref",
    "wiki_page_issue",
    "wiki_page_proposal",
    "wiki_generation_task",
    "wiki_document_contribution",
    "wiki_ingest_pending",
    "wiki_log_entry",
    "wiki_page_commit",
    "wiki_ingest_commit",
    "wiki_page",
)


class PostgresWikiRepository(WikiRepository):
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

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator["_PostgresWikiConnection"]:  # noqa: ARG002
        with self.database.transaction() as conn:
            yield _PostgresWikiConnection(conn, self.schema)

    def _search_page_rows_fts(
        self,
        conn: "_PostgresWikiConnection",
        workspace_id: str,
        knowledge_base_id: str,
        query: str,
        clauses: list[str],
        params: list[Any],
        limit: int,
    ) -> list[Any]:
        terms = re.findall(r"[\w.-]+", query, flags=re.UNICODE)[:8]
        if not terms:
            return []
        ts_query = " ".join(terms)
        patterns = [f"%{term}%" for term in terms]
        scoped_clauses = " and ".join(_qualify_page_clause(clause) for clause in clauses)
        rows = conn.execute(
            f"""
            select p.*
            from wiki_page p
            where p.workspace_id = ? and p.knowledge_base_id = ?
              and {scoped_clauses}
              and (
                p.search_vector @@ websearch_to_tsquery('simple', ?)
                or exists (
                    select 1 from unnest(?::text[]) as pattern
                    where concat_ws(' ', p.title, p.slug, p.summary, p.content_markdown, p.aliases_json::text) ilike pattern
                )
              )
            order by ts_rank_cd(p.search_vector, websearch_to_tsquery('simple', ?)) desc,
                     p.updated_at desc, p.id desc
            limit ?
            """,
            (workspace_id, knowledge_base_id, *params, ts_query, patterns, ts_query, limit),
        ).fetchall()
        return rows

    def create_generation_task(
        self,
        scope: KnowledgeBaseScope,
        *,
        doc_id: str,
        config: dict[str, Any] | None = None,
    ) -> WikiGenerationTask:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        now = wiki_now_iso()
        from uuid import uuid4

        task_id = f"wiki-gen-{uuid4().hex}"
        with self._connect() as conn:
            self._assert_active_knowledge_base(conn, workspace_id, knowledge_base_id)
            conn.execute(
                """
                insert into wiki_generation_task(
                    id, workspace_id, knowledge_base_id, doc_id, status, page_slug,
                    error_message, config_json, attempts, created_at, updated_at, started_at, finished_at
                ) values (?, ?, ?, ?, 'pending', '', '', ?, 0, ?, ?, null, null)
                """,
                (task_id, workspace_id, knowledge_base_id, doc_id.strip(), _json_dumps(config or {}), now, now),
            )
        return self.get_generation_task(scope, task_id)

    def update_generation_task(
        self,
        scope: KnowledgeBaseScope,
        task_id: str,
        *,
        status: str,
        page_slug: str = "",
        error_message: str = "",
    ) -> WikiGenerationTask:
        if status not in {
            "pending", "queued", "running", "retrying", "finalizing", "completed",
            "failed", "cancelled", "dead_lettered", "skipped",
        }:
            raise ValueError("Unsupported Wiki generation task status")
        workspace_id, knowledge_base_id = self._single_scope(scope)
        now = wiki_now_iso()
        started_at = now if status == "running" else None
        finished_at = now if status in {"completed", "failed", "cancelled", "dead_lettered", "skipped"} else None
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update wiki_generation_task
                set status = ?::text,
                    page_slug = coalesce(nullif(?::text, ''), page_slug),
                    error_message = ?::text,
                    attempts = attempts + case when ?::text = 'running' then 1 else 0 end,
                    updated_at = ?::timestamptz,
                    started_at = case when ?::timestamptz is not null and started_at is null then ?::timestamptz else started_at end,
                    finished_at = case when ?::timestamptz is not null then ?::timestamptz else finished_at end
                where workspace_id = ?::text and knowledge_base_id = ?::text and id = ?::text
                """,
                (
                    status,
                    normalize_wiki_slug(page_slug) if page_slug else "",
                    _clean_text(error_message, 800),
                    status,
                    now,
                    started_at,
                    started_at,
                    finished_at,
                    finished_at,
                    workspace_id,
                    knowledge_base_id,
                    task_id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(task_id)
        return self.get_generation_task(scope, task_id)

    def claim_pending_batch(
        self,
        scope: KnowledgeBaseScope,
        *,
        limit: int = 5,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        claimed_at = str(now or wiki_now_iso())
        bounded = min(100, max(1, int(limit or 5)))
        with self._connect(immediate=True) as conn:
            rows = conn.execute(
                """
                select * from wiki_ingest_pending
                where workspace_id = ? and knowledge_base_id = ?
                  and status in ('pending', 'retrying') and available_at <= ?
                order by available_at, created_at, id
                limit ?
                for update skip locked
                """,
                (workspace_id, knowledge_base_id, claimed_at, bounded),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"update wiki_ingest_pending set status = 'claimed', updated_at = ? where id in ({placeholders}) and status in ('pending', 'retrying')",
                    (claimed_at, *ids),
                )
                rows = conn.execute(
                    f"select * from wiki_ingest_pending where id in ({placeholders}) order by available_at, created_at, id",
                    ids,
                ).fetchall()
        return [dict(row) for row in rows]


class _PostgresWikiConnection:
    def __init__(self, conn: Any, schema: str):
        self.conn = conn
        self.schema = schema

    def execute(self, sql: str, params: Any = ()) -> "_PostgresWikiCursor":
        translated = _translate_sql(sql, self.schema)
        cur = self.conn.cursor()
        cur.execute(translated, tuple(params or ()))
        return _PostgresWikiCursor(cur)

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> "_PostgresWikiCursor":
        translated = _translate_sql(sql, self.schema)
        cur = self.conn.cursor()
        cur.executemany(translated, rows)
        return _PostgresWikiCursor(cur)


class _PostgresWikiCursor:
    def __init__(self, cursor: Any):
        self.cursor = cursor

    @property
    def rowcount(self) -> int:
        return int(getattr(self.cursor, "rowcount", 0) or 0)

    def fetchone(self) -> Any | None:
        row = self.cursor.fetchone()
        return _compat_row(row) if row is not None else None

    def fetchall(self) -> list[Any]:
        return [_compat_row(row) for row in self.cursor.fetchall()]


class _CompatRow(dict):
    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _compat_row(row: Any) -> Any:
    if isinstance(row, dict) and not isinstance(row, _CompatRow):
        return _CompatRow(row)
    return row


def _translate_sql(sql: str, schema: str) -> str:
    translated = sql.replace("?", "%s")
    translated = re.sub(r"\binsert\s+or\s+ignore\s+into\b", "insert into", translated, flags=re.IGNORECASE)
    translated = _qualify_tables(translated, schema)
    translated = _cast_json_assignments(translated)
    translated = _cast_insert_json_placeholders(translated)
    translated = translated.replace(" f.name collate nocase", " lower(f.name)")
    translated = translated.replace("rowid desc", "id desc")
    translated = re.sub(r"\bactive\s*=\s*1\b", "active = true", translated)
    translated = re.sub(r"\bactive\s*=\s*0\b", "active = false", translated)
    if re.search(r"\binsert\s+into\s+.+\bwiki_log_entry\b", translated, flags=re.IGNORECASE):
        translated = f"{translated.rstrip()} on conflict(workspace_id, knowledge_base_id, idempotency_key) do nothing"
    if re.search(r"\binsert\s+into\s+.+\bwiki_page_source_ref\b", translated, flags=re.IGNORECASE):
        translated = f"{translated.rstrip()} on conflict do nothing"
    return translated


def _qualify_tables(sql: str, schema: str) -> str:
    translated = sql
    for table in _TABLE_NAMES:
        qualified = qname(schema, table)
        translated = re.sub(
            rf"(?<![\w\".]){re.escape(table)}(?![\w\"])",
            qualified,
            translated,
        )
    return translated


def _cast_json_assignments(sql: str) -> str:
    column_pattern = "|".join(re.escape(column) for column in sorted(_WIKI_JSON_COLUMNS, key=len, reverse=True))
    return re.sub(rf"\b({column_pattern})\s*=\s*%s(?!::jsonb)", r"\1 = %s::jsonb", sql)


def _cast_insert_json_placeholders(sql: str) -> str:
    match = re.search(
        r"(insert\s+into\s+[^\(]+\()(?P<columns>.+?)(\)\s*values\s*\()(?P<values>.+?)(\))(?P<tail>\s*(?:on\s+conflict.*)?)\s*$",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return sql
    columns = [column.strip().strip('"') for column in _split_csv(match.group("columns"))]
    values = _split_csv(match.group("values"))
    if len(columns) != len(values):
        return sql
    for index, column in enumerate(columns):
        if column in _WIKI_JSON_COLUMNS and values[index].strip() == "%s":
            values[index] = "%s::jsonb"
        elif column == "active" and values[index].strip() in {"1", "0"}:
            values[index] = "true" if values[index].strip() == "1" else "false"
        elif column in {"applied_at", "rejected_at", "started_at", "finished_at"} and values[index].strip() == "''":
            values[index] = "null"
    return "".join(
        [
            match.group(1),
            match.group("columns"),
            match.group(3),
            ", ".join(values),
            match.group(5),
            match.group("tail"),
        ]
    )


def _split_csv(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    depth = 0
    for char in value:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
            continue
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    parts.append("".join(current).strip())
    return parts
