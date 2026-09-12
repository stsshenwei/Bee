import unittest

from app.models.knowledge_base import KnowledgeBaseScope
from app.services.wiki.postgres_wiki_repository import (
    PostgresWikiRepository,
    _PostgresWikiConnection,
    _translate_sql,
)
from app.services.wiki.wiki_repository import _decode_contribution


class _FakeDatabase:
    settings = type("Settings", (), {"schema": "rag"})()


class _Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeCursor:
    def __init__(self, raw):
        self.raw = raw
        self.rowcount = 1

    def execute(self, sql, params=()):
        self.raw.calls.append((sql, tuple(params or ())))

    def executemany(self, sql, rows):
        self.raw.calls.append((sql, tuple(rows)))

    def fetchone(self):
        return None

    def fetchall(self):
        sql = self.raw.calls[-1][0].lower()
        if "from \"rag\".\"wiki_ingest_pending\"" in sql and "for update skip locked" in sql:
            return [
                {
                    "id": "pending-1",
                    "workspace_id": "workspace-1",
                    "knowledge_base_id": "kb-1",
                    "document_id": "doc-1",
                    "document_revision": "r1",
                    "operation": "upsert",
                    "status": "pending",
                    "task_id": "",
                    "available_at": "2026-08-13T00:00:00",
                    "last_error": "",
                    "created_at": "2026-08-13T00:00:00",
                    "updated_at": "2026-08-13T00:00:00",
                }
            ]
        if "where id in" in sql:
            row = {
                "id": "pending-1",
                "workspace_id": "workspace-1",
                "knowledge_base_id": "kb-1",
                "document_id": "doc-1",
                "document_revision": "r1",
                "operation": "upsert",
                "status": "claimed",
                "task_id": "",
                "available_at": "2026-08-13T00:00:00",
                "last_error": "",
                "created_at": "2026-08-13T00:00:00",
                "updated_at": "2026-08-13T00:00:00",
            }
            return [row]
        return []


class _FakeRawConnection:
    def __init__(self):
        self.calls = []

    def cursor(self):
        return _FakeCursor(self)


class _RecordingDatabase(_FakeDatabase):
    def __init__(self):
        self.raw = _FakeRawConnection()

    def transaction(self):
        return _Context(self.raw)


class PostgresWikiRepositoryTests(unittest.TestCase):
    def test_sql_translation_qualifies_tables_casts_jsonb_and_preserves_idempotent_insert(self):
        sql = _translate_sql(
            """
            insert or ignore into wiki_log_entry(
                id, workspace_id, knowledge_base_id, idempotency_key, page_slugs_json, metadata_json, created_at
            ) values (?, ?, ?, ?, ?, ?, ?)
            """,
            "rag",
        )

        self.assertIn('insert into "rag"."wiki_log_entry"', sql)
        self.assertIn("%s::jsonb", sql)
        self.assertIn("on conflict(workspace_id, knowledge_base_id, idempotency_key) do nothing", sql)

    def test_sql_translation_converts_sqlite_timestamp_and_boolean_literals(self):
        proposal_sql = _translate_sql(
            """
            insert into wiki_page_proposal(
                id, workspace_id, knowledge_base_id, action, slug, payload_json, applied_at, rejected_at
            ) values (?, ?, ?, ?, ?, ?, '', '')
            """,
            "rag",
        )
        contribution_sql = _translate_sql(
            """
            insert into wiki_document_contribution(
                id, workspace_id, knowledge_base_id, document_id, document_revision, page_slug,
                page_type, contribution_json, active, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            "rag",
        )

        self.assertIn("%s::jsonb, null, null", proposal_sql)
        self.assertIn("%s::jsonb, true, %s", contribution_sql)

    def test_sql_translation_preserves_active_assignment_in_upsert(self):
        sql = _translate_sql(
            """
            insert into wiki_document_contribution(
                id, workspace_id, knowledge_base_id, document_id, document_revision, page_slug,
                page_type, contribution_json, source_chunk_ids_json, generation_run_id,
                content_hash, active, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            on conflict(workspace_id, knowledge_base_id, document_id, document_revision, page_slug, page_type)
            do update set contribution_json = excluded.contribution_json,
                source_chunk_ids_json = excluded.source_chunk_ids_json,
                generation_run_id = excluded.generation_run_id,
                content_hash = excluded.content_hash, active = 1, updated_at = excluded.updated_at
            """,
            "rag",
        )

        self.assertIn("active = true", sql)
        self.assertIn("%s::jsonb, %s::jsonb, %s, %s, true, %s, %s", sql)
        self.assertNotIn("excluded.content_hash, true,", sql)

    def test_sql_translation_casts_wiki_alias_like_search_to_text(self):
        sql = _translate_sql(
            """
            select * from wiki_page
            where title like ? or slug like ? or summary like ?
              or content_markdown like ? or aliases_json like ?
            order by updated_at desc
            """,
            "rag",
        )
        lowered = sql.lower()

        self.assertIn("title ilike %s", lowered)
        self.assertIn("slug ilike %s", lowered)
        self.assertIn("summary ilike %s", lowered)
        self.assertIn("content_markdown ilike %s", lowered)
        self.assertIn("aliases_json::text ilike %s", lowered)
        self.assertNotIn("aliases_json like", lowered)

    def test_decodes_postgres_jsonb_contribution_payloads(self):
        decoded = _decode_contribution(
            {
                "id": "contrib-1",
                "page_slug": "gpon",
                "page_type": "concept",
                "contribution_json": {
                    "title": "GPON",
                    "source_refs": [{"doc_id": "doc-1", "chunk_id": "chunk-1"}],
                },
                "source_chunk_ids_json": ["chunk-1"],
                "active": True,
            }
        )

        self.assertEqual("GPON", decoded["contribution"]["title"])
        self.assertEqual([{"doc_id": "doc-1", "chunk_id": "chunk-1"}], decoded["contribution"]["source_refs"])
        self.assertEqual(["chunk-1"], decoded["source_chunk_ids"])
        self.assertTrue(decoded["active"])

    def test_search_uses_postgres_text_search_and_trigram_fallback(self):
        raw = _FakeRawConnection()
        conn = _PostgresWikiConnection(raw, "rag")
        repo = PostgresWikiRepository(_FakeDatabase(), validate_schema=False)

        repo._search_page_rows_fts(
            conn,
            "workspace-1",
            "kb-1",
            "ERR_CODE_42 光猫",
            ["workspace_id = ?", "knowledge_base_id = ?", "status = ?"],
            ["workspace-1", "kb-1", "published"],
            5,
        )

        sql, params = raw.calls[-1]
        self.assertIn('from "rag"."wiki_page" p', sql)
        self.assertIn("websearch_to_tsquery", sql)
        self.assertIn("unnest(%s::text[])", sql)
        self.assertIn("ilike pattern", sql.lower())
        self.assertEqual("ERR_CODE_42 光猫", params[-4])
        self.assertEqual(["%ERR_CODE_42%", "%光猫%"], params[-3])

    def test_claim_pending_batch_uses_row_level_locking(self):
        database = _RecordingDatabase()
        repo = PostgresWikiRepository(database, validate_schema=False)
        scope = KnowledgeBaseScope("workspace-1", ("kb-1",))

        claimed = repo.claim_pending_batch(scope, limit=1, now="2026-08-13T00:00:00")

        claim_sql, claim_params = database.raw.calls[0]
        self.assertEqual("claimed", claimed[0]["status"])
        self.assertIn("for update skip locked", claim_sql.lower())
        self.assertIn('from "rag"."wiki_ingest_pending"', claim_sql)
        self.assertEqual(("workspace-1", "kb-1", "2026-08-13T00:00:00", 1), claim_params)
        self.assertTrue(any("set status = 'claimed'" in sql for sql, _ in database.raw.calls))

    def test_update_generation_task_casts_nullable_timestamp_parameters(self):
        sql = _translate_sql(
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
            "rag",
        )

        self.assertIn('update "rag"."wiki_generation_task"', sql)
        self.assertEqual(5, sql.count("::timestamptz"))
        self.assertIn("%s::text = 'running'", sql)


if __name__ == "__main__":
    unittest.main()
