from __future__ import annotations

import json
import re
import sqlite3
from base64 import urlsafe_b64decode, urlsafe_b64encode
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.models.knowledge_base import KnowledgeBaseScope
from app.models.wiki import WikiFolder, WikiGenerationTask, WikiPage, WikiPageIssue, WikiPageProposal, WikiSourceRef, wiki_now_iso
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings, initialize_metadata_database


class WikiVersionConflictError(RuntimeError):
    pass


class WikiRepository:
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

    def create_page(
        self,
        scope: KnowledgeBaseScope,
        *,
        title: str,
        slug: str = "",
        page_type: str = "summary",
        status: str = "draft",
        content_markdown: str = "",
        summary: str = "",
        parent_slug: str = "",
        folder_id: str = "",
        category_path: list[str] | tuple[str, ...] | None = None,
        sort_order: int = 0,
        source_refs: list[dict[str, Any]] | tuple[WikiSourceRef, ...] | None = None,
        chunk_refs: list[str] | tuple[str, ...] | None = None,
        out_links: list[str] | tuple[str, ...] | None = None,
        aliases: list[str] | tuple[str, ...] | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str = "",
        generation_run_id: str = "",
    ) -> WikiPage:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        if idempotency_key:
            committed = self.get_committed_page_update(scope, idempotency_key)
            if committed is not None:
                return committed
        now = wiki_now_iso()
        clean_title = _clean_text(title, 240)
        if not clean_title:
            raise ValueError("Wiki page title cannot be empty")
        clean_slug = normalize_wiki_slug(slug or clean_title)
        source_values = _source_refs(source_refs)
        chunk_values = _string_tuple(chunk_refs)
        category_values = _string_tuple(category_path)
        out_link_values = tuple(normalize_wiki_slug(item) for item in _string_tuple(out_links) if normalize_wiki_slug(item))
        alias_values = _string_tuple(aliases)
        wiki_path = _wiki_path(category_values, clean_slug)
        page_id = uuid4().hex
        with self._connect(immediate=True) as conn:
            self._assert_active_knowledge_base(conn, workspace_id, knowledge_base_id)
            conn.execute(
                """
                insert into wiki_page(
                    id, workspace_id, knowledge_base_id, slug, title, page_type, status,
                    content_markdown, summary, parent_slug, folder_id, category_path_json,
                    wiki_path, depth, sort_order, source_refs_json, chunk_refs_json,
                    in_links_json, out_links_json, aliases_json, metadata_json,
                    version, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, 1, ?, ?)
                """,
                (
                    page_id,
                    workspace_id,
                    knowledge_base_id,
                    clean_slug,
                    clean_title,
                    page_type,
                    status,
                    content_markdown,
                    summary,
                    normalize_wiki_slug(parent_slug) if parent_slug else "",
                    folder_id.strip(),
                    _json_dumps(list(category_values)),
                    wiki_path,
                    len(category_values),
                    int(sort_order),
                    _json_dumps([item.to_dict() for item in source_values]),
                    _json_dumps(list(chunk_values)),
                    _json_dumps(list(out_link_values)),
                    _json_dumps(list(alias_values)),
                    _json_dumps(metadata or {}),
                    now,
                    now,
                ),
            )
            self._replace_source_refs(conn, page_id, workspace_id, knowledge_base_id, source_values, now)
            if idempotency_key:
                conn.execute(
                    """
                    insert into wiki_page_commit(
                        id, workspace_id, knowledge_base_id, idempotency_key, page_slug,
                        page_version, generation_run_id, affected_slug, created_at
                    ) values (?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (f"wiki-commit-{uuid4().hex}", workspace_id, knowledge_base_id, idempotency_key, clean_slug, generation_run_id, clean_slug, now),
                )
        page = self.get_page_by_slug(scope, clean_slug)
        if page is None:
            raise RuntimeError("Wiki page creation did not persist")
        return page

    def update_page(
        self,
        scope: KnowledgeBaseScope,
        slug: str,
        changes: dict[str, Any],
        *,
        expected_version: int | None = None,
        idempotency_key: str = "",
        generation_run_id: str = "",
    ) -> WikiPage:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        clean_slug = normalize_wiki_slug(slug)
        if idempotency_key:
            committed = self.get_committed_page_update(scope, idempotency_key)
            if committed is not None:
                return committed
        current = self.get_page_by_slug(scope, clean_slug)
        if current is None:
            raise KeyError(clean_slug)
        selected: dict[str, Any] = {}
        version_bump = False
        if "slug" in changes and changes["slug"] is not None:
            selected["slug"] = normalize_wiki_slug(str(changes["slug"]))
            version_bump = True
        if "title" in changes and changes["title"] is not None:
            title = _clean_text(str(changes["title"]), 240)
            if not title:
                raise ValueError("Wiki page title cannot be empty")
            selected["title"] = title
            version_bump = True
        for key in ("page_type", "status", "content_markdown", "summary", "folder_id"):
            if key in changes and changes[key] is not None:
                selected[key] = str(changes[key])
                version_bump = version_bump or key in {"page_type", "status", "content_markdown", "summary"}
        if "parent_slug" in changes and changes["parent_slug"] is not None:
            selected["parent_slug"] = normalize_wiki_slug(str(changes["parent_slug"])) if changes["parent_slug"] else ""
            version_bump = True
        if "category_path" in changes and changes["category_path"] is not None:
            category_values = _string_tuple(changes["category_path"])
            selected["category_path_json"] = _json_dumps(list(category_values))
            selected["depth"] = len(category_values)
            selected["wiki_path"] = _wiki_path(category_values, selected.get("slug", current.slug))
        elif "slug" in selected:
            selected["wiki_path"] = _wiki_path(current.category_path, selected["slug"])
        if "sort_order" in changes and changes["sort_order"] is not None:
            selected["sort_order"] = int(changes["sort_order"])
        if "source_refs" in changes and changes["source_refs"] is not None:
            source_values = _source_refs(changes["source_refs"])
            selected["source_refs_json"] = _json_dumps([item.to_dict() for item in source_values])
            version_bump = True
        else:
            source_values = None
        for input_key, column in (
            ("chunk_refs", "chunk_refs_json"),
            ("in_links", "in_links_json"),
            ("out_links", "out_links_json"),
            ("aliases", "aliases_json"),
        ):
            if input_key in changes and changes[input_key] is not None:
                values = _string_tuple(changes[input_key])
                if input_key in {"in_links", "out_links"}:
                    values = tuple(normalize_wiki_slug(item) for item in values if normalize_wiki_slug(item))
                selected[column] = _json_dumps(list(values))
                version_bump = version_bump or input_key in {"chunk_refs", "out_links", "aliases"}
        if "metadata" in changes and changes["metadata"] is not None:
            selected["metadata_json"] = _json_dumps(dict(changes["metadata"] or {}))
        if not selected:
            return current
        selected["updated_at"] = wiki_now_iso()
        if version_bump:
            selected["version"] = current.version + 1
        assignments = ", ".join(f"{key} = ?" for key in selected)
        expected = current.version if expected_version is None else int(expected_version)
        with self._connect(immediate=True) as conn:
            cursor = conn.execute(
                f"""
                update wiki_page
                set {assignments}
                where workspace_id = ? and knowledge_base_id = ? and slug = ? and status != 'archived' and version = ?
                """,
                (*selected.values(), workspace_id, knowledge_base_id, clean_slug, expected),
            )
            if cursor.rowcount == 0:
                raise WikiVersionConflictError(f"Wiki page version changed while updating {clean_slug}")
            if source_values is not None:
                self._replace_source_refs(conn, current.id, workspace_id, knowledge_base_id, source_values, selected["updated_at"])
            if idempotency_key:
                conn.execute(
                    """
                    insert into wiki_page_commit(
                        id, workspace_id, knowledge_base_id, idempotency_key, page_slug,
                        page_version, generation_run_id, affected_slug, created_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"wiki-commit-{uuid4().hex}", workspace_id, knowledge_base_id, idempotency_key,
                        str(selected.get("slug") or clean_slug), int(selected.get("version") or current.version),
                        generation_run_id, clean_slug, selected["updated_at"],
                    ),
                )
        result_slug = str(selected.get("slug") or clean_slug)
        page = self.get_page_by_slug(scope, result_slug, include_archived=str(selected.get("status") or "") == "archived")
        if page is None:
            raise KeyError(result_slug)
        return page

    def get_committed_page_update(self, scope: KnowledgeBaseScope, idempotency_key: str) -> WikiPage | None:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        with self._connect() as conn:
            row = conn.execute(
                """
                select p.* from wiki_page_commit c
                join wiki_page p on p.workspace_id = c.workspace_id
                    and p.knowledge_base_id = c.knowledge_base_id and p.slug = c.page_slug
                where c.workspace_id = ? and c.knowledge_base_id = ? and c.idempotency_key = ?
                """,
                (workspace_id, knowledge_base_id, str(idempotency_key or "").strip()),
            ).fetchone()
        return self._decode_page(row) if row is not None else None

    def archive_page(self, scope: KnowledgeBaseScope, slug: str) -> WikiPage:
        return self.update_page(scope, slug, {"status": "archived"})

    def get_page_by_slug(self, scope: KnowledgeBaseScope, slug: str, *, include_archived: bool = False) -> WikiPage | None:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        clauses = ["workspace_id = ?", "knowledge_base_id = ?", "slug = ?"]
        params: list[Any] = [workspace_id, knowledge_base_id, normalize_wiki_slug(slug)]
        if not include_archived:
            clauses.append("status != 'archived'")
        with self._connect() as conn:
            row = conn.execute(f"select * from wiki_page where {' and '.join(clauses)}", params).fetchone()
        return self._decode_page(row) if row else None

    def get_page_by_id(self, scope: KnowledgeBaseScope, page_id: str) -> WikiPage | None:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        with self._connect() as conn:
            row = conn.execute(
                "select * from wiki_page where workspace_id = ? and knowledge_base_id = ? and id = ?",
                (workspace_id, knowledge_base_id, page_id),
            ).fetchone()
        return self._decode_page(row) if row else None

    def list_pages(
        self,
        scope: KnowledgeBaseScope,
        *,
        q: str = "",
        status: str = "",
        page_type: str = "",
        folder_id: str = "",
        limit: int = 50,
        cursor: str = "",
    ) -> tuple[list[WikiPage], str | None]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        clauses = ["workspace_id = ?", "knowledge_base_id = ?", "status != 'archived'"]
        params: list[Any] = [workspace_id, knowledge_base_id]
        if status:
            clauses[-1] = "status = ?"
            params.append(status)
        if page_type:
            clauses.append("page_type = ?")
            params.append(page_type)
        if folder_id:
            clauses.append("folder_id = ?")
            params.append(folder_id)
        bounded_limit = min(100, max(1, int(limit or 50)))
        cursor_updated_at, cursor_id = _decode_page_cursor(cursor)
        if cursor_updated_at and cursor_id:
            clauses.append("(updated_at < ? or (updated_at = ? and id < ?))")
            params.extend([cursor_updated_at, cursor_updated_at, cursor_id])
        with self._connect() as conn:
            rows = []
            if q.strip():
                rows = self._search_page_rows_fts(
                    conn, workspace_id, knowledge_base_id, q, clauses, params, bounded_limit + 1
                )
                if not rows:
                    like = f"%{q.strip()}%"
                    search_clauses = [*clauses, "(title like ? or slug like ? or summary like ? or content_markdown like ? or aliases_json like ?)"]
                    rows = conn.execute(
                        f"select * from wiki_page where {' and '.join(search_clauses)} order by updated_at desc, id desc limit ?",
                        (*params, like, like, like, like, like, bounded_limit + 1),
                    ).fetchall()
            else:
                rows = conn.execute(
                    f"select * from wiki_page where {' and '.join(clauses)} order by updated_at desc, id desc limit ?",
                    (*params, bounded_limit + 1),
                ).fetchall()
        pages = [self._decode_page(row) for row in rows[:bounded_limit]]
        next_cursor = _encode_page_cursor(pages[-1]) if len(rows) > bounded_limit and pages else None
        return pages, next_cursor

    def _search_page_rows_fts(
        self,
        conn: sqlite3.Connection,
        workspace_id: str,
        knowledge_base_id: str,
        query: str,
        clauses: list[str],
        params: list[Any],
        limit: int,
    ) -> list[sqlite3.Row]:
        terms = re.findall(r"[\w.-]+", query, flags=re.UNICODE)[:8]
        if not terms:
            return []
        match_query = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"*' for term in terms)
        try:
            return conn.execute(
                f"""
                select p.* from wiki_page_fts f join wiki_page p on p.id = f.page_id
                where f.workspace_id = ? and f.knowledge_base_id = ? and wiki_page_fts match ?
                  and {' and '.join(_qualify_page_clause(clause) for clause in clauses)}
                order by bm25(wiki_page_fts), p.updated_at desc, p.id desc limit ?
                """,
                (workspace_id, knowledge_base_id, match_query, *params, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    def pages_by_source_ref(self, scope: KnowledgeBaseScope, *, doc_id: str, chunk_id: str = "") -> list[WikiPage]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        clauses = ["r.workspace_id = ?", "r.knowledge_base_id = ?", "r.doc_id = ?", "p.status != 'archived'"]
        params: list[Any] = [workspace_id, knowledge_base_id, doc_id]
        if chunk_id:
            clauses.append("r.chunk_id = ?")
            params.append(chunk_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select p.*
                from wiki_page_source_ref r
                join wiki_page p on p.id = r.page_id
                where {' and '.join(clauses)}
                order by p.updated_at desc
                """,
                params,
            ).fetchall()
        return [self._decode_page(row) for row in rows]

    def create_folder(self, scope: KnowledgeBaseScope, *, name: str, parent_id: str = "", sort_order: int = 0) -> WikiFolder:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        clean_name = _clean_text(name, 120)
        if not clean_name:
            raise ValueError("Wiki folder name cannot be empty")
        now = wiki_now_iso()
        folder_id = uuid4().hex
        parent = self.get_folder(scope, parent_id) if parent_id else None
        depth = (parent.depth + 1) if parent else 0
        path = f"{parent.path}/{clean_name}" if parent else clean_name
        with self._connect() as conn:
            self._assert_active_knowledge_base(conn, workspace_id, knowledge_base_id)
            conn.execute(
                """
                insert into wiki_folder(id, workspace_id, knowledge_base_id, parent_id, name, path, depth, sort_order, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (folder_id, workspace_id, knowledge_base_id, parent_id.strip(), clean_name, path, depth, int(sort_order), now, now),
            )
        folder = self.get_folder(scope, folder_id)
        if folder is None:
            raise RuntimeError("Wiki folder creation did not persist")
        return folder

    def get_folder(self, scope: KnowledgeBaseScope, folder_id: str) -> WikiFolder | None:
        if not folder_id:
            return None
        workspace_id, knowledge_base_id = self._single_scope(scope)
        with self._connect() as conn:
            row = conn.execute(
                "select * from wiki_folder where workspace_id = ? and knowledge_base_id = ? and id = ?",
                (workspace_id, knowledge_base_id, folder_id),
            ).fetchone()
        return self._decode_folder(row) if row else None

    def list_folders(self, scope: KnowledgeBaseScope, *, parent_id: str = "") -> list[WikiFolder]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        with self._connect() as conn:
            rows = conn.execute(
                """
                select f.*, count(p.id) as page_count
                from wiki_folder f
                left join wiki_page p on p.workspace_id = f.workspace_id
                    and p.knowledge_base_id = f.knowledge_base_id
                    and p.folder_id = f.id
                    and p.status != 'archived'
                where f.workspace_id = ? and f.knowledge_base_id = ? and f.parent_id = ?
                group by f.id
                order by f.sort_order, f.name collate nocase
                """,
                (workspace_id, knowledge_base_id, parent_id.strip()),
            ).fetchall()
        return [self._decode_folder(row) for row in rows]

    def update_folder(self, scope: KnowledgeBaseScope, folder_id: str, *, name: str | None = None, parent_id: str | None = None, sort_order: int | None = None) -> WikiFolder:
        current = self.get_folder(scope, folder_id)
        if current is None:
            raise KeyError(folder_id)
        selected: dict[str, Any] = {}
        clean_name = current.name
        parent = self.get_folder(scope, parent_id or "") if parent_id is not None and parent_id else None
        if parent_id is not None and parent_id and parent is None:
            raise ValueError("Wiki folder parent must exist in the same knowledge base")
        if parent_id == folder_id:
            raise ValueError("Wiki folder cannot be moved into itself")
        ancestor = parent
        while ancestor is not None:
            if ancestor.id == folder_id:
                raise ValueError("Wiki folder move would create a cycle")
            ancestor = self.get_folder(scope, ancestor.parent_id) if ancestor.parent_id else None
        if name is not None:
            clean_name = _clean_text(name, 120)
            if not clean_name:
                raise ValueError("Wiki folder name cannot be empty")
            selected["name"] = clean_name
        if parent_id is not None:
            selected["parent_id"] = parent_id.strip()
            selected["depth"] = (parent.depth + 1) if parent else 0
        if name is not None or parent_id is not None:
            selected["path"] = f"{parent.path}/{clean_name}" if parent else clean_name
        if sort_order is not None:
            selected["sort_order"] = int(sort_order)
        if not selected:
            return current
        selected["updated_at"] = wiki_now_iso()
        assignments = ", ".join(f"{key} = ?" for key in selected)
        workspace_id, knowledge_base_id = self._single_scope(scope)
        with self._connect(immediate=True) as conn:
            cursor = conn.execute(
                f"update wiki_folder set {assignments} where workspace_id = ? and knowledge_base_id = ? and id = ?",
                (*selected.values(), workspace_id, knowledge_base_id, folder_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(folder_id)
            queue = [(folder_id, str(selected.get("path") or current.path), int(selected.get("depth", current.depth)))]
            while queue:
                parent_folder_id, parent_path, parent_depth = queue.pop(0)
                children = conn.execute(
                    "select id, name from wiki_folder where workspace_id = ? and knowledge_base_id = ? and parent_id = ?",
                    (workspace_id, knowledge_base_id, parent_folder_id),
                ).fetchall()
                for child in children:
                    child_path = f"{parent_path}/{child['name']}"
                    child_depth = parent_depth + 1
                    conn.execute(
                        "update wiki_folder set path = ?, depth = ?, updated_at = ? where id = ?",
                        (child_path, child_depth, wiki_now_iso(), str(child["id"])),
                    )
                    queue.append((str(child["id"]), child_path, child_depth))
        folder = self.get_folder(scope, folder_id)
        if folder is None:
            raise KeyError(folder_id)
        return folder

    def delete_empty_folder(self, scope: KnowledgeBaseScope, folder_id: str) -> None:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        with self._connect() as conn:
            child = conn.execute(
                "select 1 from wiki_folder where workspace_id = ? and knowledge_base_id = ? and parent_id = ? limit 1",
                (workspace_id, knowledge_base_id, folder_id),
            ).fetchone()
            page = conn.execute(
                "select 1 from wiki_page where workspace_id = ? and knowledge_base_id = ? and folder_id = ? and status != 'archived' limit 1",
                (workspace_id, knowledge_base_id, folder_id),
            ).fetchone()
            if child or page:
                raise ValueError("Wiki folder is not empty")
            cursor = conn.execute(
                "delete from wiki_folder where workspace_id = ? and knowledge_base_id = ? and id = ?",
                (workspace_id, knowledge_base_id, folder_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(folder_id)

    def create_issue(self, scope: KnowledgeBaseScope, *, slug: str, issue_type: str = "other", description: str = "", suspected_doc_ids: list[str] | None = None, suspected_chunk_ids: list[str] | None = None, reported_by: str = "user") -> WikiPageIssue:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        now = wiki_now_iso()
        issue_id = uuid4().hex
        with self._connect() as conn:
            self._assert_active_knowledge_base(conn, workspace_id, knowledge_base_id)
            conn.execute(
                """
                insert into wiki_page_issue(
                    id, workspace_id, knowledge_base_id, slug, issue_type, description,
                    suspected_doc_ids_json, suspected_chunk_ids_json, status, reported_by, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    issue_id,
                    workspace_id,
                    knowledge_base_id,
                    normalize_wiki_slug(slug),
                    issue_type,
                    _clean_text(description, 4000),
                    _json_dumps(list(_string_tuple(suspected_doc_ids))),
                    _json_dumps(list(_string_tuple(suspected_chunk_ids))),
                    _clean_text(reported_by, 80) or "user",
                    now,
                    now,
                ),
            )
        return self.get_issue(scope, issue_id)

    def get_issue(self, scope: KnowledgeBaseScope, issue_id: str) -> WikiPageIssue:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        with self._connect() as conn:
            row = conn.execute(
                "select * from wiki_page_issue where workspace_id = ? and knowledge_base_id = ? and id = ?",
                (workspace_id, knowledge_base_id, issue_id),
            ).fetchone()
        if row is None:
            raise KeyError(issue_id)
        return self._decode_issue(row)

    def list_issues(self, scope: KnowledgeBaseScope, *, slug: str = "", status: str = "open", issue_type: str = "", limit: int = 50) -> list[WikiPageIssue]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        clauses = ["workspace_id = ?", "knowledge_base_id = ?"]
        params: list[Any] = [workspace_id, knowledge_base_id]
        if slug:
            clauses.append("slug = ?")
            params.append(normalize_wiki_slug(slug))
        if status:
            clauses.append("status = ?")
            params.append(status)
        if issue_type:
            clauses.append("issue_type = ?")
            params.append(issue_type)
        with self._connect() as conn:
            rows = conn.execute(
                f"select * from wiki_page_issue where {' and '.join(clauses)} order by updated_at desc limit ?",
                (*params, min(100, max(1, int(limit or 50)))),
            ).fetchall()
        return [self._decode_issue(row) for row in rows]

    def update_issue_status(self, scope: KnowledgeBaseScope, issue_id: str, status: str) -> WikiPageIssue:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update wiki_page_issue
                set status = ?, updated_at = ?
                where workspace_id = ? and knowledge_base_id = ? and id = ?
                """,
                (status, wiki_now_iso(), workspace_id, knowledge_base_id, issue_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(issue_id)
        return self.get_issue(scope, issue_id)

    def create_proposal(self, scope: KnowledgeBaseScope, *, action: str, slug: str, title: str = "", content_markdown: str = "", payload: dict[str, Any] | None = None, source_refs: list[dict[str, Any]] | None = None, chunk_refs: list[str] | None = None, reason: str = "", created_by: str = "agent") -> WikiPageProposal:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        now = wiki_now_iso()
        proposal_id = uuid4().hex
        refs = _source_refs(source_refs)
        with self._connect() as conn:
            self._assert_active_knowledge_base(conn, workspace_id, knowledge_base_id)
            conn.execute(
                """
                insert into wiki_page_proposal(
                    id, workspace_id, knowledge_base_id, action, slug, status, title, content_markdown,
                    payload_json, source_refs_json, chunk_refs_json, reason, created_by,
                    created_at, updated_at, applied_at, rejected_at
                ) values (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '')
                """,
                (
                    proposal_id,
                    workspace_id,
                    knowledge_base_id,
                    action,
                    normalize_wiki_slug(slug),
                    _clean_text(title, 240),
                    content_markdown,
                    _json_dumps(payload or {}),
                    _json_dumps([item.to_dict() for item in refs]),
                    _json_dumps(list(_string_tuple(chunk_refs))),
                    _clean_text(reason, 1200),
                    _clean_text(created_by, 80) or "agent",
                    now,
                    now,
                ),
            )
        return self.get_proposal(scope, proposal_id)

    def get_proposal(self, scope: KnowledgeBaseScope, proposal_id: str) -> WikiPageProposal:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        with self._connect() as conn:
            row = conn.execute(
                "select * from wiki_page_proposal where workspace_id = ? and knowledge_base_id = ? and id = ?",
                (workspace_id, knowledge_base_id, proposal_id),
            ).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        return self._decode_proposal(row)

    def list_proposals(self, scope: KnowledgeBaseScope, *, slug: str = "", status: str = "pending", limit: int = 50) -> list[WikiPageProposal]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        clauses = ["workspace_id = ?", "knowledge_base_id = ?"]
        params: list[Any] = [workspace_id, knowledge_base_id]
        if slug:
            clauses.append("slug = ?")
            params.append(normalize_wiki_slug(slug))
        if status:
            clauses.append("status = ?")
            params.append(status)
        with self._connect() as conn:
            rows = conn.execute(
                f"select * from wiki_page_proposal where {' and '.join(clauses)} order by updated_at desc limit ?",
                (*params, min(100, max(1, int(limit or 50)))),
            ).fetchall()
        return [self._decode_proposal(row) for row in rows]

    def update_proposal_status(self, scope: KnowledgeBaseScope, proposal_id: str, status: str) -> WikiPageProposal:
        if status not in {"applied", "rejected"}:
            raise ValueError("Unsupported proposal status")
        workspace_id, knowledge_base_id = self._single_scope(scope)
        now = wiki_now_iso()
        timestamp_column = "applied_at" if status == "applied" else "rejected_at"
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                update wiki_page_proposal
                set status = ?, updated_at = ?, {timestamp_column} = ?
                where workspace_id = ? and knowledge_base_id = ? and id = ? and status = 'pending'
                """,
                (status, now, now, workspace_id, knowledge_base_id, proposal_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(proposal_id)
        return self.get_proposal(scope, proposal_id)

    def create_generation_task(self, scope: KnowledgeBaseScope, *, doc_id: str, config: dict[str, Any] | None = None) -> WikiGenerationTask:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        now = wiki_now_iso()
        task_id = f"wiki-gen-{uuid4().hex}"
        with self._connect() as conn:
            self._assert_active_knowledge_base(conn, workspace_id, knowledge_base_id)
            conn.execute(
                """
                insert into wiki_generation_task(
                    id, workspace_id, knowledge_base_id, doc_id, status, page_slug,
                    error_message, config_json, attempts, created_at, updated_at, started_at, finished_at
                ) values (?, ?, ?, ?, 'pending', '', '', ?, 0, ?, ?, '', '')
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
        started_at = now if status == "running" else ""
        finished_at = now if status in {"completed", "failed", "cancelled", "dead_lettered", "skipped"} else ""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update wiki_generation_task
                set status = ?,
                    page_slug = coalesce(nullif(?, ''), page_slug),
                    error_message = ?,
                    attempts = attempts + case when ? = 'running' then 1 else 0 end,
                    updated_at = ?,
                    started_at = case when ? != '' and started_at = '' then ? else started_at end,
                    finished_at = case when ? != '' then ? else finished_at end
                where workspace_id = ? and knowledge_base_id = ? and id = ?
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

    def get_generation_task(self, scope: KnowledgeBaseScope, task_id: str) -> WikiGenerationTask:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        with self._connect() as conn:
            row = conn.execute(
                "select * from wiki_generation_task where workspace_id = ? and knowledge_base_id = ? and id = ?",
                (workspace_id, knowledge_base_id, task_id),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._decode_generation_task(row)

    def list_generation_tasks(self, scope: KnowledgeBaseScope, *, doc_id: str = "", limit: int = 20) -> list[WikiGenerationTask]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        clauses = ["workspace_id = ?", "knowledge_base_id = ?"]
        params: list[Any] = [workspace_id, knowledge_base_id]
        if doc_id:
            clauses.append("doc_id = ?")
            params.append(doc_id.strip())
        with self._connect() as conn:
            rows = conn.execute(
                f"select * from wiki_generation_task where {' and '.join(clauses)} order by updated_at desc limit ?",
                (*params, min(100, max(1, int(limit or 20)))),
            ).fetchall()
        return [self._decode_generation_task(row) for row in rows]

    def enqueue_pending(
        self,
        scope: KnowledgeBaseScope,
        *,
        document_id: str,
        document_revision: str,
        operation: str = "upsert",
        task_id: str = "",
        available_at: str | None = None,
    ) -> dict[str, Any]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        now = wiki_now_iso()
        pending_id = f"wiki-pending-{uuid4().hex}"
        with self._connect(immediate=True) as conn:
            self._assert_active_knowledge_base(conn, workspace_id, knowledge_base_id)
            conn.execute(
                """
                insert into wiki_ingest_pending(
                    id, workspace_id, knowledge_base_id, document_id, document_revision,
                    operation, status, task_id, available_at, last_error, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, 'pending', ?, ?, '', ?, ?)
                on conflict(workspace_id, knowledge_base_id, document_id, document_revision, operation)
                do update set status = 'pending', task_id = excluded.task_id,
                    available_at = excluded.available_at, last_error = '', updated_at = excluded.updated_at
                """,
                (
                    pending_id,
                    workspace_id,
                    knowledge_base_id,
                    document_id,
                    document_revision,
                    operation,
                    task_id,
                    available_at or now,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                select * from wiki_ingest_pending
                where workspace_id = ? and knowledge_base_id = ? and document_id = ?
                  and document_revision = ? and operation = ?
                """,
                (workspace_id, knowledge_base_id, document_id, document_revision, operation),
            ).fetchone()
        return dict(row) if row else {}

    def update_pending_status(
        self,
        scope: KnowledgeBaseScope,
        *,
        document_id: str,
        document_revision: str,
        status: str,
        task_id: str = "",
        last_error: str = "",
    ) -> None:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        with self._connect(immediate=True) as conn:
            conn.execute(
                """
                update wiki_ingest_pending
                set status = ?, task_id = coalesce(nullif(?, ''), task_id), last_error = ?, updated_at = ?
                where workspace_id = ? and knowledge_base_id = ? and document_id = ? and document_revision = ?
                """,
                (
                    status,
                    task_id,
                    _clean_text(last_error, 1000),
                    wiki_now_iso(),
                    workspace_id,
                    knowledge_base_id,
                    document_id,
                    document_revision,
                ),
            )

    def list_pending(
        self,
        scope: KnowledgeBaseScope,
        *,
        statuses: set[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        clauses = ["workspace_id = ?", "knowledge_base_id = ?"]
        params: list[Any] = [workspace_id, knowledge_base_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status in ({placeholders})")
            params.extend(sorted(statuses))
        with self._connect() as conn:
            rows = conn.execute(
                f"select * from wiki_ingest_pending where {' and '.join(clauses)} order by available_at, created_at limit ?",
                (*params, min(500, max(1, int(limit or 100)))),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def replace_document_contributions(
        self,
        scope: KnowledgeBaseScope,
        *,
        document_id: str,
        document_revision: str,
        generation_run_id: str,
        contributions: list[dict[str, Any]],
        idempotency_key: str = "",
    ) -> set[str]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        now = wiki_now_iso()
        commit_key = str(idempotency_key or f"manifest:{generation_run_id}:{document_id}:{document_revision}").strip()
        with self._connect(immediate=True) as conn:
            committed = conn.execute(
                "select affected_slugs_json from wiki_ingest_commit where workspace_id = ? and knowledge_base_id = ? and idempotency_key = ?",
                (workspace_id, knowledge_base_id, commit_key),
            ).fetchone()
            if committed is not None:
                return set(_json_loads(committed["affected_slugs_json"], []))
            old_rows = conn.execute(
                """
                select page_slug from wiki_document_contribution
                where workspace_id = ? and knowledge_base_id = ? and document_id = ? and active = 1
                """,
                (workspace_id, knowledge_base_id, document_id),
            ).fetchall()
            affected = {str(row[0]) for row in old_rows}
            conn.execute(
                """
                update wiki_document_contribution set active = 0, updated_at = ?
                where workspace_id = ? and knowledge_base_id = ? and document_id = ? and active = 1
                """,
                (now, workspace_id, knowledge_base_id, document_id),
            )
            for contribution in contributions:
                slug = normalize_wiki_slug(str(contribution.get("page_slug") or contribution.get("title") or ""))
                page_type = str(contribution.get("page_type") or "concept")
                if not slug:
                    continue
                affected.add(slug)
                chunk_ids = list(_string_tuple(contribution.get("source_chunk_ids") or ()))
                payload = dict(contribution)
                payload["page_slug"] = slug
                content_hash = str(contribution.get("content_hash") or _content_hash(payload))
                conn.execute(
                    """
                    insert into wiki_document_contribution(
                        id, workspace_id, knowledge_base_id, document_id, document_revision,
                        page_slug, page_type, contribution_json, source_chunk_ids_json,
                        generation_run_id, content_hash, active, created_at, updated_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    on conflict(workspace_id, knowledge_base_id, document_id, document_revision, page_slug, page_type)
                    do update set contribution_json = excluded.contribution_json,
                        source_chunk_ids_json = excluded.source_chunk_ids_json,
                        generation_run_id = excluded.generation_run_id,
                        content_hash = excluded.content_hash, active = 1, updated_at = excluded.updated_at
                    """,
                    (
                        f"wiki-contrib-{uuid4().hex}",
                        workspace_id,
                        knowledge_base_id,
                        document_id,
                        document_revision,
                        slug,
                        page_type,
                        _json_dumps(payload),
                        _json_dumps(chunk_ids),
                        generation_run_id,
                        content_hash,
                        now,
                        now,
                    ),
                )
            conn.execute(
                """
                insert into wiki_ingest_commit(
                    id, workspace_id, knowledge_base_id, idempotency_key, document_id,
                    document_revision, generation_run_id, affected_slugs_json, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"wiki-ingest-commit-{uuid4().hex}", workspace_id, knowledge_base_id, commit_key,
                    document_id, document_revision, generation_run_id, _json_dumps(sorted(affected)), now,
                ),
            )
        return affected

    def active_contributions_for_slug(self, scope: KnowledgeBaseScope, slug: str) -> list[dict[str, Any]]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        with self._connect() as conn:
            rows = conn.execute(
                """
                select * from wiki_document_contribution
                where workspace_id = ? and knowledge_base_id = ? and page_slug = ? and active = 1
                order by document_id, document_revision
                """,
                (workspace_id, knowledge_base_id, normalize_wiki_slug(slug)),
            ).fetchall()
        return [_decode_contribution(row) for row in rows]

    def contribution_changes(
        self,
        scope: KnowledgeBaseScope,
        *,
        document_id: str,
        proposed: list[dict[str, Any]],
    ) -> dict[str, list[str]]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        with self._connect() as conn:
            rows = conn.execute(
                """
                select page_slug, content_hash from wiki_document_contribution
                where workspace_id = ? and knowledge_base_id = ? and document_id = ? and active = 1
                """,
                (workspace_id, knowledge_base_id, document_id),
            ).fetchall()
        current = {str(row["page_slug"]): str(row["content_hash"] or "") for row in rows}
        incoming = {
            normalize_wiki_slug(str(item.get("page_slug") or item.get("title") or "")): str(item.get("content_hash") or _content_hash(item))
            for item in proposed
        }
        additions = sorted(incoming.keys() - current.keys())
        retractions = sorted(current.keys() - incoming.keys())
        replacements = sorted(slug for slug in incoming.keys() & current.keys() if incoming[slug] != current[slug])
        return {
            "additions": additions,
            "replacements": replacements,
            "retractions": retractions,
            "affected_slugs": sorted({*additions, *replacements, *retractions}),
        }

    def orphaned_contributions(self, scope: KnowledgeBaseScope, *, limit: int = 100) -> list[dict[str, Any]]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        with self._connect() as conn:
            rows = conn.execute(
                """
                select c.* from wiki_document_contribution c
                left join document d on d.id = c.document_id
                    and d.workspace_id = c.workspace_id and d.knowledge_base_id = c.knowledge_base_id
                where c.workspace_id = ? and c.knowledge_base_id = ? and c.active = 1 and d.id is null
                order by c.updated_at, c.id limit ?
                """,
                (workspace_id, knowledge_base_id, min(500, max(1, int(limit or 100)))),
            ).fetchall()
        return [_decode_contribution(row) for row in rows]

    def append_log(
        self,
        scope: KnowledgeBaseScope,
        *,
        idempotency_key: str,
        event_type: str,
        document_id: str = "",
        page_slugs: list[str] | tuple[str, ...] = (),
        outcome: str = "completed",
        message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        now = wiki_now_iso()
        with self._connect(immediate=True) as conn:
            conn.execute(
                """
                insert or ignore into wiki_log_entry(
                    id, workspace_id, knowledge_base_id, idempotency_key, event_type,
                    document_id, page_slugs_json, outcome, message, metadata_json, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"wiki-log-{uuid4().hex}",
                    workspace_id,
                    knowledge_base_id,
                    idempotency_key,
                    event_type,
                    document_id,
                    _json_dumps(list(_string_tuple(page_slugs))),
                    outcome,
                    _clean_text(message, 2000),
                    _json_dumps(metadata or {}),
                    now,
                ),
            )
            row = conn.execute(
                "select * from wiki_log_entry where workspace_id = ? and knowledge_base_id = ? and idempotency_key = ?",
                (workspace_id, knowledge_base_id, idempotency_key),
            ).fetchone()
        return _decode_log(row) if row else {}

    def list_logs(self, scope: KnowledgeBaseScope, *, limit: int = 50, cursor: int = 0) -> tuple[list[dict[str, Any]], int | None]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        bounded = min(100, max(1, int(limit or 50)))
        offset = max(0, int(cursor or 0))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select * from wiki_log_entry
                where workspace_id = ? and knowledge_base_id = ?
                order by created_at desc, rowid desc limit ? offset ?
                """,
                (workspace_id, knowledge_base_id, bounded + 1, offset),
            ).fetchall()
        items = [_decode_log(row) for row in rows[:bounded]]
        return items, offset + bounded if len(rows) > bounded else None

    def page_type_counts(self, scope: KnowledgeBaseScope) -> dict[str, int]:
        workspace_id, knowledge_base_id = self._single_scope(scope)
        with self._connect() as conn:
            rows = conn.execute(
                """
                select page_type, count(*) from wiki_page
                where workspace_id = ? and knowledge_base_id = ? and status != 'archived'
                group by page_type
                """,
                (workspace_id, knowledge_base_id),
            ).fetchall()
        return {str(row[0]): int(row[1] or 0) for row in rows}

    def _replace_source_refs(self, conn: sqlite3.Connection, page_id: str, workspace_id: str, knowledge_base_id: str, source_refs: tuple[WikiSourceRef, ...], now: str) -> None:
        conn.execute(
            "delete from wiki_page_source_ref where page_id = ? and workspace_id = ? and knowledge_base_id = ?",
            (page_id, workspace_id, knowledge_base_id),
        )
        conn.executemany(
            """
            insert or ignore into wiki_page_source_ref(page_id, workspace_id, knowledge_base_id, doc_id, chunk_id, created_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            [
                (page_id, workspace_id, knowledge_base_id, ref.doc_id, ref.chunk_id, now)
                for ref in source_refs
                if ref.doc_id
            ],
        )

    def _single_scope(self, scope: KnowledgeBaseScope) -> tuple[str, str]:
        return scope.workspace_id, scope.knowledge_base_id

    def _assert_active_knowledge_base(self, conn: sqlite3.Connection, workspace_id: str, knowledge_base_id: str) -> None:
        row = conn.execute(
            "select 1 from knowledge_base where id = ? and workspace_id = ? and status = 'active'",
            (knowledge_base_id, workspace_id),
        ).fetchone()
        if row is None:
            raise ValueError("Knowledge base does not exist, is archived, or belongs to another workspace")

    def _decode_page(self, row: sqlite3.Row) -> WikiPage:
        return WikiPage(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            knowledge_base_id=str(row["knowledge_base_id"]),
            slug=str(row["slug"]),
            title=str(row["title"]),
            page_type=str(row["page_type"]),
            status=str(row["status"]),
            content_markdown=str(row["content_markdown"] or ""),
            summary=str(row["summary"] or ""),
            parent_slug=str(row["parent_slug"] or ""),
            folder_id=str(row["folder_id"] or ""),
            category_path=tuple(_json_loads(row["category_path_json"], [])),
            wiki_path=str(row["wiki_path"] or ""),
            depth=int(row["depth"] or 0),
            sort_order=int(row["sort_order"] or 0),
            source_refs=tuple(WikiSourceRef.from_dict(item) for item in _json_loads(row["source_refs_json"], [])),
            chunk_refs=tuple(str(item) for item in _json_loads(row["chunk_refs_json"], [])),
            in_links=tuple(str(item) for item in _json_loads(row["in_links_json"], [])),
            out_links=tuple(str(item) for item in _json_loads(row["out_links_json"], [])),
            aliases=tuple(str(item) for item in _json_loads(row["aliases_json"], [])),
            metadata=dict(_json_loads(row["metadata_json"], {})),
            version=int(row["version"] or 1),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )

    def _decode_folder(self, row: sqlite3.Row) -> WikiFolder:
        data = dict(row)
        return WikiFolder(
            id=str(data.get("id") or ""),
            workspace_id=str(data.get("workspace_id") or ""),
            knowledge_base_id=str(data.get("knowledge_base_id") or ""),
            parent_id=str(data.get("parent_id") or ""),
            name=str(data.get("name") or ""),
            path=str(data.get("path") or ""),
            depth=int(data.get("depth") or 0),
            sort_order=int(data.get("sort_order") or 0),
            page_count=int(data.get("page_count") or 0),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )

    def _decode_issue(self, row: sqlite3.Row) -> WikiPageIssue:
        return WikiPageIssue(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            knowledge_base_id=str(row["knowledge_base_id"]),
            slug=str(row["slug"]),
            issue_type=str(row["issue_type"]),
            description=str(row["description"] or ""),
            suspected_doc_ids=tuple(str(item) for item in _json_loads(row["suspected_doc_ids_json"], [])),
            suspected_chunk_ids=tuple(str(item) for item in _json_loads(row["suspected_chunk_ids_json"], [])),
            status=str(row["status"]),
            reported_by=str(row["reported_by"] or ""),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )

    def _decode_proposal(self, row: sqlite3.Row) -> WikiPageProposal:
        return WikiPageProposal(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            knowledge_base_id=str(row["knowledge_base_id"]),
            action=str(row["action"]),
            slug=str(row["slug"]),
            status=str(row["status"]),
            title=str(row["title"] or ""),
            content_markdown=str(row["content_markdown"] or ""),
            payload=dict(_json_loads(row["payload_json"], {})),
            source_refs=tuple(WikiSourceRef.from_dict(item) for item in _json_loads(row["source_refs_json"], [])),
            chunk_refs=tuple(str(item) for item in _json_loads(row["chunk_refs_json"], [])),
            reason=str(row["reason"] or ""),
            created_by=str(row["created_by"] or ""),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            applied_at=str(row["applied_at"] or ""),
            rejected_at=str(row["rejected_at"] or ""),
        )

    def _decode_generation_task(self, row: sqlite3.Row) -> WikiGenerationTask:
        return WikiGenerationTask(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            knowledge_base_id=str(row["knowledge_base_id"]),
            doc_id=str(row["doc_id"]),
            status=str(row["status"]),
            page_slug=str(row["page_slug"] or ""),
            error_message=str(row["error_message"] or ""),
            config=dict(_json_loads(row["config_json"], {})),
            attempts=int(row["attempts"] or 0),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            started_at=str(row["started_at"] or ""),
            finished_at=str(row["finished_at"] or ""),
        )


def normalize_wiki_slug(value: str) -> str:
    text = re.sub(r"\s+", "-", str(value or "").strip().lower())
    text = re.sub(r"/+", "/", text)
    parts = []
    for part in text.strip("/").split("/"):
        cleaned = re.sub(r"[^\w.\-]+", "-", part, flags=re.UNICODE).strip(".-")
        if cleaned:
            parts.append(cleaned[:120])
    return "/".join(parts) or uuid4().hex[:12]


def _qualify_page_clause(clause: str) -> str:
    return re.sub(
        r"\b(workspace_id|knowledge_base_id|status|page_type|folder_id|updated_at|id)\b",
        r"p.\1",
        clause,
    )


def _encode_page_cursor(page: WikiPage) -> str:
    raw = json.dumps([page.updated_at, page.id], ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_page_cursor(cursor: str) -> tuple[str, str]:
    clean = str(cursor or "").strip()
    if not clean:
        return "", ""
    try:
        padding = "=" * (-len(clean) % 4)
        value = json.loads(urlsafe_b64decode(clean + padding).decode("utf-8"))
        if isinstance(value, list) and len(value) == 2:
            return str(value[0]), str(value[1])
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    raise ValueError("Invalid Wiki page cursor")


def _wiki_path(category_path: tuple[str, ...], slug: str) -> str:
    return "/".join([*category_path, slug]) if category_path else slug


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    else:
        values = list(value or [])
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _clean_text(str(item), 240)
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return tuple(result)


def _source_refs(value: Any) -> tuple[WikiSourceRef, ...]:
    refs: list[WikiSourceRef] = []
    seen: set[tuple[str, str]] = set()
    for item in list(value or []):
        ref = item if isinstance(item, WikiSourceRef) else WikiSourceRef.from_dict(item if isinstance(item, dict) else {})
        key = (ref.doc_id, ref.chunk_id)
        if ref.doc_id and key not in seen:
            refs.append(ref)
            seen.add(key)
    return tuple(refs)


def _clean_text(value: str, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())[:limit]


def _content_hash(value: Any) -> str:
    import hashlib

    payload = _json_dumps(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decode_contribution(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["contribution"] = dict(_json_loads(data.pop("contribution_json", "{}"), {}))
    data["source_chunk_ids"] = list(_json_loads(data.pop("source_chunk_ids_json", "[]"), []))
    data["active"] = bool(data.get("active"))
    return data


def _decode_log(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["page_slugs"] = list(_json_loads(data.pop("page_slugs_json", "[]"), []))
    data["metadata"] = dict(_json_loads(data.pop("metadata_json", "{}"), {}))
    return data
