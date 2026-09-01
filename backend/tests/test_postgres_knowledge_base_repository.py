import json
import unittest

from app.models.knowledge_base import KnowledgeBase, ProviderReferences, utc_now_iso
from app.services.knowledge.knowledge_base_service import KnowledgeBaseService, KnowledgeBaseValidationError
from app.services.knowledge.postgres_knowledge_base_repository import PostgresKnowledgeBaseRepository
from app.services.storage.postgres import PostgresIntegrityError
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.connection.executed.append((sql, params))
        sql_lower = " ".join(sql.lower().split())
        if self.connection.fail_on_insert and sql_lower.startswith("insert into"):
            raise self.connection.fail_on_insert
        if "from \"rag\".\"workspace\"" in sql_lower:
            self.connection.last_rows = [self.connection.workspace_row]
            self.rowcount = 1
            return
        if "from \"rag\".\"knowledge_base\"" in sql_lower and "where kb.id = %s" in sql_lower:
            key = params[0]
            self.connection.last_rows = [self.connection.knowledge_bases[key]] if key in self.connection.knowledge_bases else []
            self.rowcount = len(self.connection.last_rows)
            return
        if "from \"rag\".\"knowledge_base\"" in sql_lower and "where kb.workspace_id = %s" in sql_lower:
            rows = [
                row
                for row in self.connection.knowledge_bases.values()
                if row["workspace_id"] == params[0] and ("kb.status = 'active'" not in sql_lower or row["status"] == "active")
            ]
            self.connection.last_rows = sorted(rows, key=lambda row: row["updated_at"], reverse=True)
            self.rowcount = len(rows)
            return
        if "from \"rag\".\"knowledge_base\"" in sql_lower and "kb.is_default is true" in sql_lower:
            rows = [
                row
                for row in self.connection.knowledge_bases.values()
                if row["workspace_id"] == params[0] and row["status"] == "active" and row["is_default"]
            ]
            self.connection.last_rows = rows[:1]
            self.rowcount = len(self.connection.last_rows)
            return
        if sql_lower.startswith("update \"rag\".\"knowledge_base\""):
            self.rowcount = self.connection.apply_update(sql_lower, params)
            self.connection.last_rows = []
            return
        if sql_lower.startswith("insert into \"rag\".\"knowledge_base\""):
            row = _kb_row_from_insert(params)
            self.connection.knowledge_bases[row["id"]] = row
            self.rowcount = 1
            self.connection.last_rows = []
            return
        self.connection.last_rows = []
        self.rowcount = 0

    def fetchone(self):
        return self.connection.last_rows[0] if self.connection.last_rows else None

    def fetchall(self):
        return list(self.connection.last_rows)


class FakeConnection:
    def __init__(self):
        self.executed = []
        self.last_rows = []
        self.fail_on_insert = None
        now = utc_now_iso()
        self.workspace_row = {
            "id": "default-workspace",
            "name": "Default Workspace",
            "description": "",
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        self.knowledge_bases = {
            "default-knowledge-base": {
                "id": "default-knowledge-base",
                "workspace_id": "default-workspace",
                "name": "Default KB",
                "description": "",
                "type": "document",
                "is_default": True,
                "status": "active",
                "indexing_strategy_json": {"dense_enabled": True, "keyword_enabled": True},
                "provider_config_json": {"requested": {}, "effective": {"embedding": "openai"}, "inactive_overrides": []},
                "reset_required": False,
                "created_at": now,
                "updated_at": now,
                "document_count": 2,
                "indexed_chunk_count": 5,
                "processing_count": 1,
                "failed_count": 0,
                "wiki_page_count": 3,
                "wiki_issue_count": 1,
            }
        }

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def apply_update(self, sql, params):
        if "where id = %s" not in sql:
            for row in self.knowledge_bases.values():
                if row["workspace_id"] == params[-1]:
                    row["is_default"] = False
            return 1
        knowledge_base_id = params[-1]
        if knowledge_base_id not in self.knowledge_bases:
            return 0
        row = self.knowledge_bases[knowledge_base_id]
        if "status = %s" in sql:
            row["status"] = params[0]
        if "is_default = true" in sql:
            row["is_default"] = True
        if "name = %s" in sql:
            row["name"] = params[0]
        if "provider_config_json = %s::jsonb" in sql:
            row["provider_config_json"] = json.loads(params[0])
        row["updated_at"] = params[-2] if len(params) > 1 else row["updated_at"]
        return 1


class FakeDatabase:
    def __init__(self, connection):
        self.connection_obj = connection
        self.settings = type("Settings", (), {"schema": "rag"})()

    def connection(self):
        return self.connection_obj

    def transaction(self):
        return self.connection_obj


class PostgresKnowledgeBaseRepositoryTests(unittest.TestCase):
    def test_repository_preserves_existing_method_contracts(self):
        connection = FakeConnection()
        repository = PostgresKnowledgeBaseRepository(
            FakeDatabase(connection),
            defaults=DefaultKnowledgeBaseSettings(
                workspace_id="default-workspace",
                workspace_name="Default Workspace",
                knowledge_base_id="default-knowledge-base",
                knowledge_base_name="Default KB",
            ),
            validate_schema=False,
        )

        workspace, default_kb = repository.ensure_defaults()
        listed = repository.list_knowledge_bases("default-workspace")

        self.assertEqual("default-workspace", workspace.id)
        self.assertEqual("default-knowledge-base", default_kb.id)
        self.assertEqual("openai", default_kb.provider_config.effective.embedding)
        self.assertEqual(5, default_kb.aggregate.indexed_chunk_count)
        self.assertEqual([default_kb.id], [item.id for item in listed])

        now = utc_now_iso()
        created = repository.create_knowledge_base(
            KnowledgeBase(
                id="kb-2",
                workspace_id="default-workspace",
                name="PostgreSQL KB",
                created_at=now,
                updated_at=now,
            )
        )
        self.assertEqual("kb-2", created.id)
        self.assertTrue(any("%s::jsonb" in sql for sql, _ in connection.executed))

    def test_service_translates_postgres_integrity_to_validation_error(self):
        connection = FakeConnection()
        connection.fail_on_insert = PostgresIntegrityError("duplicate key")
        repository = PostgresKnowledgeBaseRepository(FakeDatabase(connection), validate_schema=False)
        service = KnowledgeBaseService(repository, default_providers=ProviderReferences(embedding="openai"))

        with self.assertRaises(KnowledgeBaseValidationError):
            service.create("Default KB")


def _kb_row_from_insert(params):
    return {
        "id": params[0],
        "workspace_id": params[1],
        "name": params[2],
        "description": params[3],
        "type": params[4],
        "is_default": bool(params[5]),
        "status": params[6],
        "indexing_strategy_json": json.loads(params[7]),
        "provider_config_json": json.loads(params[8]),
        "reset_required": bool(params[9]),
        "created_at": params[10],
        "updated_at": params[11],
        "document_count": 0,
        "indexed_chunk_count": 0,
        "processing_count": 0,
        "failed_count": 0,
        "wiki_page_count": 0,
        "wiki_issue_count": 0,
    }


if __name__ == "__main__":
    unittest.main()
