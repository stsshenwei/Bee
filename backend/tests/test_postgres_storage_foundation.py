import unittest

from app.services.storage.postgres import PostgresConfigurationError, PostgresSettings, quote_ident
from app.services.storage.postgres_schema import (
    POSTGRES_SCHEMA_VERSION,
    PostgresSchemaConfig,
    inspect_postgres_startup_storage,
    qname,
)
from app.services.storage.storage_schema import StorageResetRequired


class _FakeDatabase:
    def __init__(self, inspection):
        self.inspection = inspection

    def connection(self):
        raise AssertionError("connection should not be used when inspect_postgres_schema is patched")


class PostgresStorageFoundationTests(unittest.TestCase):
    def test_settings_require_database_url_and_parse_pool_settings(self):
        with self.assertRaises(PostgresConfigurationError):
            PostgresSettings.from_env({})

        settings = PostgresSettings.from_env(
            {
                "DATABASE_URL": "postgresql://user:pass@localhost:5432/rag",
                "POSTGRES_SCHEMA": "rag_app",
                "POSTGRES_POOL_MIN_SIZE": "2",
                "POSTGRES_POOL_MAX_SIZE": "9",
            }
        )

        self.assertEqual("postgresql://user:pass@localhost:5432/rag", settings.database_url)
        self.assertEqual("rag_app", settings.schema)
        self.assertEqual(2, settings.pool_min_size)
        self.assertEqual(9, settings.pool_max_size)

    def test_quote_ident_escapes_and_rejects_empty_identifiers(self):
        self.assertEqual('"rag_app"', quote_ident("rag_app"))
        self.assertEqual('"bad""name"', quote_ident('bad"name'))
        self.assertEqual('"rag_app"."storage_schema"', qname("rag_app", "storage_schema"))
        with self.assertRaises(ValueError):
            quote_ident("")

    def test_postgres_schema_config_validates_vector_shape(self):
        config = PostgresSchemaConfig(schema="rag", vector_dimension=1536, vector_type="vector")
        self.assertEqual("vector(1536)", config.vector_sql_type)

        half = PostgresSchemaConfig(schema="rag", vector_dimension=3072, vector_type="halfvec")
        self.assertEqual("halfvec(3072)", half.vector_sql_type)

        with self.assertRaises(ValueError):
            PostgresSchemaConfig(vector_dimension=0).vector_sql_type
        with self.assertRaises(ValueError):
            PostgresSchemaConfig(vector_type="json").vector_sql_type

    def test_schema_version_marks_postgres_cutover_generation(self):
        self.assertEqual("20260813_postgres_pgvector_v1", POSTGRES_SCHEMA_VERSION)

    def test_startup_inspection_fails_closed_for_empty_or_mismatched_storage(self):
        import app.services.storage.postgres_schema as postgres_schema

        original = postgres_schema.inspect_postgres_schema
        try:
            postgres_schema.inspect_postgres_schema = lambda database, config=None: database.inspection
            ready = {
                "empty": False,
                "missing_extensions": [],
                "reset_required": False,
                "version": POSTGRES_SCHEMA_VERSION,
            }
            self.assertEqual(ready, inspect_postgres_startup_storage(_FakeDatabase(ready)))

            for inspection in (
                {"empty": True, "missing_extensions": [], "reset_required": False},
                {"empty": False, "missing_extensions": ["vector"], "reset_required": False},
                {"empty": False, "missing_extensions": [], "reset_required": True},
            ):
                with self.assertRaises(StorageResetRequired):
                    inspect_postgres_startup_storage(_FakeDatabase(inspection))
        finally:
            postgres_schema.inspect_postgres_schema = original


if __name__ == "__main__":
    unittest.main()
