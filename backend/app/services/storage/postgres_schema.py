from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.storage.postgres import PostgresDatabase, quote_ident
from app.services.storage.storage_schema import DefaultKnowledgeBaseSettings, StorageResetRequired


POSTGRES_SCHEMA_VERSION = "20260813_postgres_pgvector_v1"
POSTGRES_REQUIRED_EXTENSIONS = {"vector", "pg_trgm"}
DEFAULT_VECTOR_DIMENSION = 1536
DEFAULT_VECTOR_TYPE = "vector"
DEFAULT_KEYWORD_LANGUAGE = "simple"


@dataclass(frozen=True)
class PostgresSchemaConfig:
    schema: str = "public"
    vector_dimension: int = DEFAULT_VECTOR_DIMENSION
    vector_type: str = DEFAULT_VECTOR_TYPE
    keyword_language: str = DEFAULT_KEYWORD_LANGUAGE
    hnsw_m: int = 16
    hnsw_ef_construction: int = 200

    @property
    def vector_sql_type(self) -> str:
        vector_type = self.vector_type.strip().lower()
        if vector_type not in {"vector", "halfvec"}:
            raise ValueError("vector_type must be 'vector' or 'halfvec'")
        if int(self.vector_dimension) <= 0:
            raise ValueError("vector_dimension must be positive")
        return f"{vector_type}({int(self.vector_dimension)})"


def initialize_postgres_database(
    database: PostgresDatabase,
    *,
    config: PostgresSchemaConfig | None = None,
    defaults: DefaultKnowledgeBaseSettings | None = None,
) -> None:
    config = config or PostgresSchemaConfig(schema=database.settings.schema)
    defaults = defaults or DefaultKnowledgeBaseSettings()
    with database.transaction() as conn:
        _create_schema(conn, config.schema)
        _create_extensions(conn)
        _set_search_path(conn, config.schema)
        _ensure_empty_or_version(conn, config)
        if not _table_exists(conn, config.schema, "storage_schema"):
            _create_final_schema(conn, config, defaults)
        else:
            _ensure_plugin_settings_schema(conn, config)
            _ensure_marketplace_schema(conn, config)
            _validate_required_tables(conn, config)
            _validate_schema_metadata(conn, config)


def inspect_postgres_schema(
    database: PostgresDatabase,
    *,
    config: PostgresSchemaConfig | None = None,
) -> dict[str, object]:
    config = config or PostgresSchemaConfig(schema=database.settings.schema)
    with database.connection() as conn:
        extensions = _installed_extensions(conn)
        tables = _user_tables(conn, config.schema)
        version = None
        metadata: dict[str, Any] = {}
        if "storage_schema" in tables:
            with conn.cursor() as cur:
                cur.execute(
                    f"select version, metadata_json from {qname(config.schema, 'storage_schema')} where component = %s",
                    ("primary",),
                )
                row = cur.fetchone()
            if row:
                version = str(row["version"])
                metadata = dict(row.get("metadata_json") or {})
        return {
            "exists": bool(tables),
            "empty": not tables,
            "version": version,
            "required_extensions": sorted(POSTGRES_REQUIRED_EXTENSIONS),
            "installed_extensions": sorted(extensions),
            "missing_extensions": sorted(POSTGRES_REQUIRED_EXTENSIONS - extensions),
            "vector_dimension": metadata.get("vector_dimension"),
            "vector_type": metadata.get("vector_type"),
            "reset_required": bool(tables)
            and (
                version != POSTGRES_SCHEMA_VERSION
                or bool(POSTGRES_REQUIRED_EXTENSIONS - extensions)
                or int(metadata.get("vector_dimension") or -1) != int(config.vector_dimension)
                or str(metadata.get("vector_type") or "") != config.vector_type
            ),
        }


def inspect_postgres_startup_storage(
    database: PostgresDatabase,
    *,
    config: PostgresSchemaConfig | None = None,
) -> dict[str, object]:
    inspection = inspect_postgres_schema(database, config=config)
    if inspection["empty"]:
        raise StorageResetRequired("PostgreSQL storage is empty; run the clean-rebuild CLI before startup")
    if inspection["missing_extensions"]:
        raise StorageResetRequired(
            f"PostgreSQL storage is missing required extensions {inspection['missing_extensions']}"
        )
    if inspection["reset_required"]:
        raise StorageResetRequired(
            "PostgreSQL storage schema, vector type, or vector dimension does not match this application"
        )
    return inspection


def ensure_postgres_plugin_settings_schema(
    database: PostgresDatabase,
    *,
    config: PostgresSchemaConfig | None = None,
) -> None:
    config = config or PostgresSchemaConfig(schema=database.settings.schema)
    with database.transaction() as conn:
        _set_search_path(conn, config.schema)
        _validate_schema_metadata(conn, config)
        _ensure_plugin_settings_schema(conn, config)


def ensure_postgres_marketplace_schema(
    database: PostgresDatabase,
    *,
    config: PostgresSchemaConfig | None = None,
) -> None:
    config = config or PostgresSchemaConfig(schema=database.settings.schema)
    with database.transaction() as conn:
        _set_search_path(conn, config.schema)
        _validate_schema_metadata(conn, config)
        _ensure_marketplace_schema(conn, config)


def qname(schema: str, table: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(table)}"


def _create_extensions(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("create schema if not exists public")
        cur.execute("create extension if not exists vector with schema public")
        cur.execute("create extension if not exists pg_trgm with schema public")


def _create_schema(conn: Any, schema: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"create schema if not exists {quote_ident(schema)}")


def _set_search_path(conn: Any, schema: str) -> None:
    with conn.cursor() as cur:
        if schema == "public":
            cur.execute("set local search_path to public")
        else:
            cur.execute(f"set local search_path to {quote_ident(schema)}, public")


def _ensure_empty_or_version(conn: Any, config: PostgresSchemaConfig) -> None:
    tables = _user_tables(conn, config.schema)
    if not tables:
        return
    if "storage_schema" not in tables:
        raise StorageResetRequired("PostgreSQL storage uses a legacy schema; run the clean-rebuild CLI")
    _validate_schema_metadata(conn, config)


def _validate_schema_metadata(conn: Any, config: PostgresSchemaConfig) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"select version, metadata_json from {qname(config.schema, 'storage_schema')} where component = %s",
            ("primary",),
        )
        row = cur.fetchone()
    version = str(row["version"]) if row else ""
    metadata = dict(row.get("metadata_json") or {}) if row else {}
    if version != POSTGRES_SCHEMA_VERSION:
        raise StorageResetRequired(
            f"PostgreSQL storage schema {version or '<missing>'!r} is incompatible with "
            f"{POSTGRES_SCHEMA_VERSION!r}; run the clean-rebuild CLI"
        )
    if int(metadata.get("vector_dimension") or -1) != int(config.vector_dimension):
        raise StorageResetRequired("PostgreSQL vector dimension does not match configured embedding dimension")
    if str(metadata.get("vector_type") or "") != config.vector_type:
        raise StorageResetRequired("PostgreSQL vector type does not match configured embedding vector type")
    missing = POSTGRES_REQUIRED_EXTENSIONS - _installed_extensions(conn)
    if missing:
        raise StorageResetRequired(f"PostgreSQL storage is missing required extensions {sorted(missing)}")


def _validate_required_tables(conn: Any, config: PostgresSchemaConfig) -> None:
    missing = sorted(_POSTGRES_TABLES - _user_tables(conn, config.schema))
    if missing:
        raise StorageResetRequired(f"PostgreSQL storage is missing final-schema tables {missing}")


def _create_final_schema(
    conn: Any,
    config: PostgresSchemaConfig,
    defaults: DefaultKnowledgeBaseSettings,
) -> None:
    schema = quote_ident(config.schema)
    vector_type = config.vector_sql_type
    search_config = _literal(config.keyword_language)
    now = datetime.now().isoformat(timespec="seconds")
    ddl = f"""
    create table {schema}.storage_schema (
        component text primary key,
        version text not null,
        metadata_json jsonb not null default '{{}}'::jsonb,
        initialized_at timestamptz not null
    );

    create table {schema}.workspace (
        id text primary key,
        name text not null,
        description text not null default '',
        status text not null default 'active' check(status in ('active', 'archived')),
        created_at timestamptz not null,
        updated_at timestamptz not null
    );

    create table {schema}.knowledge_base (
        id text primary key,
        workspace_id text not null,
        name text not null,
        description text not null default '',
        type text not null default 'document' check(type in ('document', 'faq', 'wiki')),
        is_default boolean not null default false,
        status text not null default 'active' check(status in ('active', 'archived')),
        indexing_strategy_json jsonb not null default '{{}}'::jsonb,
        provider_config_json jsonb not null default '{{}}'::jsonb,
        reset_required boolean not null default false,
        created_at timestamptz not null,
        updated_at timestamptz not null,
        unique(workspace_id, id),
        unique(workspace_id, name),
        foreign key(workspace_id) references {schema}.workspace(id)
    );

    create table {schema}.document (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        name text not null,
        file_type text not null,
        storage_path text not null,
        parse_status text not null,
        created_at timestamptz not null,
        updated_at timestamptz not null,
        metadata_json jsonb not null default '{{}}'::jsonb,
        summary text not null default '',
        keywords_json jsonb not null default '[]'::jsonb,
        suggested_questions_json jsonb not null default '[]'::jsonb,
        summary_status text not null default 'none',
        summary_error text not null default '',
        summary_model_ref text not null default '',
        summary_generated_at timestamptz,
        summary_version integer not null default 0,
        summary_source_chunk_ids_json jsonb not null default '[]'::jsonb,
        current_enrichment_task_id text,
        unique(workspace_id, knowledge_base_id, id),
        foreign key(workspace_id, knowledge_base_id) references {schema}.knowledge_base(workspace_id, id)
    );

    create table {schema}.document_chunk (
        id text primary key,
        doc_id text not null,
        workspace_id text not null,
        knowledge_base_id text not null,
        parent_id text,
        chunk_type text not null,
        title_path text not null,
        content text not null,
        content_markdown text not null,
        retrieval_text text not null default '',
        search_vector tsvector generated always as (
            to_tsvector({search_config}::regconfig, coalesce(title_path, '') || ' ' || coalesce(retrieval_text, '') || ' ' || coalesce(content_markdown, '') || ' ' || coalesce(content, ''))
        ) stored,
        page_start integer,
        page_end integer,
        token_count integer not null,
        metadata_json jsonb not null default '{{}}'::jsonb,
        created_at timestamptz not null,
        unique(workspace_id, knowledge_base_id, id),
        foreign key(workspace_id, knowledge_base_id, doc_id)
            references {schema}.document(workspace_id, knowledge_base_id, id) on delete cascade
    );

    create table {schema}.document_chunk_embedding (
        chunk_id text primary key,
        doc_id text not null,
        workspace_id text not null,
        knowledge_base_id text not null,
        parent_id text,
        chunk_type text not null,
        title_path text not null default '',
        page_start integer,
        page_end integer,
        embedding {vector_type} not null,
        retrieval_text text not null default '',
        metadata_json jsonb not null default '{{}}'::jsonb,
        created_at timestamptz not null default now(),
        updated_at timestamptz not null default now(),
        foreign key(workspace_id, knowledge_base_id, chunk_id)
            references {schema}.document_chunk(workspace_id, knowledge_base_id, id) on delete cascade
    );

    create table {schema}.parse_task (
        id text primary key,
        doc_id text not null,
        workspace_id text not null,
        knowledge_base_id text not null,
        status text not null,
        error_message text not null default '',
        started_at timestamptz,
        finished_at timestamptz,
        created_at timestamptz not null,
        foreign key(workspace_id, knowledge_base_id, doc_id)
            references {schema}.document(workspace_id, knowledge_base_id, id) on delete cascade
    );

    create table {schema}.document_image_resource (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        doc_id text not null,
        storage_key text not null,
        storage_provider text not null default 'local',
        source_type text not null,
        page_number integer,
        mime_type text not null,
        width integer,
        height integer,
        metadata_json jsonb not null default '{{}}'::jsonb,
        created_at timestamptz not null,
        unique(workspace_id, knowledge_base_id, id),
        unique(workspace_id, knowledge_base_id, storage_key),
        foreign key(workspace_id, knowledge_base_id, doc_id)
            references {schema}.document(workspace_id, knowledge_base_id, id) on delete cascade
    );

    create table {schema}.document_image_operation (
        id text primary key,
        image_id text not null,
        workspace_id text not null,
        knowledge_base_id text not null,
        doc_id text not null,
        operation_type text not null check(operation_type in ('ocr', 'caption')),
        status text not null check(status in ('pending', 'processing', 'completed', 'failed', 'canceled')),
        provider_ref text not null default '',
        result_chunk_id text,
        error_message text not null default '',
        attempt integer not null default 0,
        created_at timestamptz not null,
        updated_at timestamptz not null,
        unique(workspace_id, knowledge_base_id, image_id, operation_type),
        foreign key(workspace_id, knowledge_base_id, image_id)
            references {schema}.document_image_resource(workspace_id, knowledge_base_id, id) on delete cascade,
        foreign key(workspace_id, knowledge_base_id, doc_id)
            references {schema}.document(workspace_id, knowledge_base_id, id) on delete cascade
    );

    create table {schema}.document_enrichment_task (
        id text primary key,
        doc_id text not null,
        workspace_id text not null,
        knowledge_base_id text not null,
        version integer not null,
        status text not null,
        provider_ref text not null default '',
        error_message text not null default '',
        source_chunk_ids_json jsonb not null default '[]'::jsonb,
        started_at timestamptz,
        finished_at timestamptz,
        created_at timestamptz not null,
        unique(workspace_id, knowledge_base_id, doc_id, version),
        foreign key(workspace_id, knowledge_base_id, doc_id)
            references {schema}.document(workspace_id, knowledge_base_id, id) on delete cascade
    );

    create table {schema}.knowledge_processing_spans (
        id bigserial primary key,
        knowledge_id text not null,
        attempt integer not null,
        span_id text not null unique,
        parent_span_id text,
        name text not null,
        kind text not null,
        status text not null,
        input_json jsonb not null default '{{}}'::jsonb,
        output_json jsonb not null default '{{}}'::jsonb,
        metadata_json jsonb not null default '{{}}'::jsonb,
        error_code text not null default '',
        error_message text not null default '',
        error_detail text not null default '',
        started_at timestamptz,
        finished_at timestamptz,
        duration_ms integer not null default 0,
        created_at timestamptz not null,
        updated_at timestamptz not null,
        unique nulls not distinct (knowledge_id, attempt, parent_span_id, name, kind)
    );

    create table {schema}.document_processing_task (
        id text primary key,
        task_type text not null,
        workspace_id text not null,
        knowledge_base_id text not null,
        document_id text not null default '',
        upload_batch_id text not null default '',
        upload_file_id text not null default '',
        status text not null check(status in ('pending', 'retrying', 'processing', 'completed', 'failed', 'canceled', 'dead_lettered')),
        payload_json jsonb not null default '{{}}'::jsonb,
        payload_schema_version integer not null default 1,
        idempotency_key text not null default '',
        source_revision text not null default '',
        parent_trace_id text not null default '',
        attempt integer not null default 0,
        max_attempts integer not null default 3,
        next_run_at timestamptz not null,
        lease_owner text not null default '',
        lease_expires_at timestamptz,
        last_error_code text not null default '',
        last_error_message text not null default '',
        trace_id text not null default '',
        created_at timestamptz not null,
        updated_at timestamptz not null,
        started_at timestamptz,
        finished_at timestamptz,
        unique(workspace_id, knowledge_base_id, task_type, document_id, upload_batch_id, upload_file_id)
    );

    create table {schema}.document_processing_dead_letter (
        id text primary key,
        task_id text not null,
        task_type text not null,
        workspace_id text not null,
        knowledge_base_id text not null,
        document_id text not null default '',
        upload_batch_id text not null default '',
        upload_file_id text not null default '',
        payload_json jsonb not null default '{{}}'::jsonb,
        error_code text not null default '',
        error_message text not null default '',
        attempt integer not null default 0,
        trace_id text not null default '',
        created_at timestamptz not null
    );

    create table {schema}.document_processing_task_attempt (
        id text primary key,
        task_id text not null references {schema}.document_processing_task(id) on delete cascade,
        attempt integer not null,
        worker_id text not null default '',
        status text not null,
        error_code text not null default '',
        error_message text not null default '',
        started_at timestamptz not null,
        finished_at timestamptz,
        created_at timestamptz not null
    );

    create table {schema}.kg_extraction_task (
        id text primary key,
        doc_id text not null,
        workspace_id text not null,
        knowledge_base_id text not null,
        status text not null,
        error_message text,
        extractor_version text not null,
        parent_chunk_count integer not null default 0,
        metadata_json jsonb not null default '{{}}'::jsonb,
        started_at timestamptz,
        finished_at timestamptz,
        created_at timestamptz not null,
        foreign key(workspace_id, knowledge_base_id, doc_id)
            references {schema}.document(workspace_id, knowledge_base_id, id) on delete cascade
    );

    create table {schema}.entity_mention (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        entity_id text not null,
        entity_type text not null,
        entity_name text not null,
        doc_id text not null,
        chunk_id text not null,
        parent_id text,
        page_start integer,
        page_end integer,
        mention_text text not null,
        confidence double precision not null,
        aliases_json jsonb not null default '[]'::jsonb,
        description text not null default '',
        metadata_json jsonb not null default '{{}}'::jsonb,
        created_at timestamptz not null,
        foreign key(workspace_id, knowledge_base_id, doc_id)
            references {schema}.document(workspace_id, knowledge_base_id, id) on delete cascade,
        foreign key(workspace_id, knowledge_base_id, chunk_id)
            references {schema}.document_chunk(workspace_id, knowledge_base_id, id) on delete cascade
    );

    create table {schema}.entity_embedding (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        entity_id text not null,
        entity_type text not null,
        entity_name text not null,
        description text not null default '',
        aliases_json jsonb not null default '[]'::jsonb,
        embedding {vector_type} not null,
        metadata_json jsonb not null default '{{}}'::jsonb,
        created_at timestamptz not null default now(),
        updated_at timestamptz not null default now(),
        unique(workspace_id, knowledge_base_id, entity_id)
    );

    create table {schema}.graph_community_summary (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        community_id text not null,
        summary text not null,
        entity_ids_json jsonb not null default '[]'::jsonb,
        source_chunk_ids_json jsonb not null default '[]'::jsonb,
        confidence double precision not null,
        metadata_json jsonb not null default '{{}}'::jsonb,
        created_at timestamptz not null,
        updated_at timestamptz not null,
        unique(workspace_id, knowledge_base_id, community_id),
        foreign key(workspace_id, knowledge_base_id) references {schema}.knowledge_base(workspace_id, id) on delete cascade
    );

    create table {schema}.query_log (
        id text primary key,
        workspace_id text not null,
        knowledge_base_ids_json jsonb not null,
        question text not null,
        status text not null,
        query_type text not null default '',
        tool_calls_json jsonb not null default '[]'::jsonb,
        citation_chunk_ids_json jsonb not null default '[]'::jsonb,
        response_metadata_json jsonb not null default '{{}}'::jsonb,
        error_message text not null default '',
        created_at timestamptz not null,
        finished_at timestamptz
    );

    create table {schema}.answer_feedback (
        id text primary key,
        query_log_id text references {schema}.query_log(id) on delete set null,
        workspace_id text not null,
        knowledge_base_id text not null,
        rating text not null default '',
        correction text not null default '',
        source_chunk_ids_json jsonb not null default '[]'::jsonb,
        metadata_json jsonb not null default '{{}}'::jsonb,
        created_at timestamptz not null,
        foreign key(workspace_id, knowledge_base_id) references {schema}.knowledge_base(workspace_id, id)
    );

    create table {schema}.knowledge_upload_batch (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        status text not null check(status in ('draft', 'uploading', 'ready_to_process', 'processing', 'completed', 'partial_failed', 'failed', 'canceled')),
        settings_json jsonb not null default '{{}}'::jsonb,
        error_message text not null default '',
        created_at timestamptz not null,
        updated_at timestamptz not null,
        confirmed_at timestamptz,
        completed_at timestamptz,
        unique(workspace_id, knowledge_base_id, id),
        foreign key(workspace_id, knowledge_base_id) references {schema}.knowledge_base(workspace_id, id) on delete cascade
    );

    create table {schema}.knowledge_upload_file (
        id text primary key,
        batch_id text not null,
        workspace_id text not null,
        knowledge_base_id text not null,
        original_name text not null,
        relative_path text not null,
        storage_path text not null default '',
        size bigint not null default 0,
        status text not null check(status in ('pending', 'uploaded', 'parsing', 'indexed', 'enrichment_pending', 'completed', 'failed', 'canceled')),
        document_id text,
        chunks integer not null default 0,
        error_message text not null default '',
        phases_json jsonb not null default '[]'::jsonb,
        warnings_json jsonb not null default '[]'::jsonb,
        errors_json jsonb not null default '[]'::jsonb,
        retry_eligible boolean not null default false,
        created_at timestamptz not null,
        updated_at timestamptz not null,
        foreign key(workspace_id, knowledge_base_id, batch_id) references {schema}.knowledge_upload_batch(workspace_id, knowledge_base_id, id) on delete cascade,
        foreign key(workspace_id, knowledge_base_id, document_id) references {schema}.document(workspace_id, knowledge_base_id, id) on delete set null
    );
    """
    ddl += _wiki_ddl(config)
    ddl += _memory_eval_agent_ddl(config)
    ddl += _marketplace_ddl(config)
    ddl += _postgres_indexes(config)
    with conn.cursor() as cur:
        cur.execute(ddl)
        cur.execute(
            f"insert into {qname(config.schema, 'storage_schema')}(component, version, metadata_json, initialized_at) values (%s, %s, %s::jsonb, %s)",
            (
                "primary",
                POSTGRES_SCHEMA_VERSION,
                _jsonb_literal(
                    {
                        "vector_dimension": int(config.vector_dimension),
                        "vector_type": config.vector_type,
                        "keyword_language": config.keyword_language,
                    }
                ),
                now,
            ),
        )
        cur.execute(
            f"insert into {qname(config.schema, 'workspace')}(id, name, description, status, created_at, updated_at) values (%s, %s, '', 'active', %s, %s)",
            (defaults.workspace_id, defaults.workspace_name, now, now),
        )
        cur.execute(
            f"""
            insert into {qname(config.schema, 'knowledge_base')}(
                id, workspace_id, name, description, type, is_default, status,
                indexing_strategy_json, provider_config_json, reset_required, created_at, updated_at
            ) values (%s, %s, %s, '', 'document', true, 'active', %s::jsonb, %s::jsonb, false, %s, %s)
            """,
            (
                defaults.knowledge_base_id,
                defaults.workspace_id,
                defaults.knowledge_base_name,
                _jsonb_literal({"dense_enabled": True, "keyword_enabled": True, "graph_enabled": False, "wiki_enabled": False}),
                _jsonb_literal({"requested": {}, "effective": {}, "inactive_overrides": []}),
                now,
                now,
            ),
        )


def _wiki_ddl(config: PostgresSchemaConfig) -> str:
    schema = quote_ident(config.schema)
    search_config = _literal(config.keyword_language)
    return f"""
    create table {schema}.wiki_folder (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        parent_id text not null default '',
        name text not null,
        path text not null,
        depth integer not null default 0,
        sort_order integer not null default 0,
        created_at timestamptz not null,
        updated_at timestamptz not null,
        unique(workspace_id, knowledge_base_id, path),
        foreign key(workspace_id, knowledge_base_id) references {schema}.knowledge_base(workspace_id, id) on delete cascade
    );

    create table {schema}.wiki_page (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        slug text not null,
        title text not null,
        page_type text not null default 'summary' check(page_type in ('summary', 'entity', 'concept', 'synthesis', 'manual', 'index', 'log')),
        status text not null default 'draft' check(status in ('draft', 'published', 'stale', 'archived')),
        content_markdown text not null default '',
        summary text not null default '',
        parent_slug text not null default '',
        folder_id text not null default '',
        category_path_json jsonb not null default '[]'::jsonb,
        wiki_path text not null default '',
        depth integer not null default 0,
        sort_order integer not null default 0,
        source_refs_json jsonb not null default '[]'::jsonb,
        chunk_refs_json jsonb not null default '[]'::jsonb,
        in_links_json jsonb not null default '[]'::jsonb,
        out_links_json jsonb not null default '[]'::jsonb,
        aliases_json jsonb not null default '[]'::jsonb,
        metadata_json jsonb not null default '{{}}'::jsonb,
        search_vector tsvector generated always as (
            to_tsvector({search_config}::regconfig, coalesce(title, '') || ' ' || coalesce(slug, '') || ' ' || coalesce(summary, '') || ' ' || coalesce(content_markdown, ''))
        ) stored,
        version integer not null default 1,
        created_at timestamptz not null,
        updated_at timestamptz not null,
        unique(workspace_id, knowledge_base_id, id),
        foreign key(workspace_id, knowledge_base_id) references {schema}.knowledge_base(workspace_id, id) on delete cascade
    );

    create table {schema}.wiki_page_source_ref (
        page_id text not null,
        workspace_id text not null,
        knowledge_base_id text not null,
        doc_id text not null,
        chunk_id text not null default '',
        created_at timestamptz not null,
        primary key(page_id, doc_id, chunk_id),
        foreign key(workspace_id, knowledge_base_id, page_id) references {schema}.wiki_page(workspace_id, knowledge_base_id, id) on delete cascade
    );

    create table {schema}.wiki_page_issue (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        slug text not null,
        issue_type text not null default 'other' check(issue_type in ('incorrect', 'outdated', 'missing_source', 'conflict', 'dead_link', 'duplicate_page', 'ungrounded_content', 'taxonomy', 'other')),
        description text not null default '',
        suspected_doc_ids_json jsonb not null default '[]'::jsonb,
        suspected_chunk_ids_json jsonb not null default '[]'::jsonb,
        status text not null default 'open' check(status in ('open', 'resolved', 'wontfix')),
        reported_by text not null default 'user',
        created_at timestamptz not null,
        updated_at timestamptz not null,
        foreign key(workspace_id, knowledge_base_id) references {schema}.knowledge_base(workspace_id, id) on delete cascade
    );

    create table {schema}.wiki_page_proposal (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        action text not null check(action in ('write_page', 'replace_text', 'rename_page', 'delete_page', 'merge_pages')),
        slug text not null,
        status text not null default 'pending' check(status in ('pending', 'applied', 'rejected')),
        title text not null default '',
        content_markdown text not null default '',
        payload_json jsonb not null default '{{}}'::jsonb,
        source_refs_json jsonb not null default '[]'::jsonb,
        chunk_refs_json jsonb not null default '[]'::jsonb,
        reason text not null default '',
        created_by text not null default 'agent',
        created_at timestamptz not null,
        updated_at timestamptz not null,
        applied_at timestamptz,
        rejected_at timestamptz,
        foreign key(workspace_id, knowledge_base_id) references {schema}.knowledge_base(workspace_id, id) on delete cascade
    );

    create table {schema}.wiki_generation_task (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        doc_id text not null,
        status text not null default 'pending' check(status in ('pending', 'queued', 'running', 'retrying', 'finalizing', 'completed', 'failed', 'cancelled', 'dead_lettered', 'skipped')),
        page_slug text not null default '',
        error_message text not null default '',
        config_json jsonb not null default '{{}}'::jsonb,
        attempts integer not null default 0,
        created_at timestamptz not null,
        updated_at timestamptz not null,
        started_at timestamptz,
        finished_at timestamptz,
        foreign key(workspace_id, knowledge_base_id) references {schema}.knowledge_base(workspace_id, id) on delete cascade
    );

    create table {schema}.wiki_document_contribution (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        document_id text not null,
        document_revision text not null,
        page_slug text not null,
        page_type text not null,
        contribution_json jsonb not null default '{{}}'::jsonb,
        source_chunk_ids_json jsonb not null default '[]'::jsonb,
        generation_run_id text not null,
        content_hash text not null,
        active boolean not null default true,
        created_at timestamptz not null,
        updated_at timestamptz not null,
        unique(workspace_id, knowledge_base_id, document_id, document_revision, page_slug, page_type),
        foreign key(workspace_id, knowledge_base_id) references {schema}.knowledge_base(workspace_id, id) on delete cascade
    );

    create table {schema}.wiki_ingest_pending (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        document_id text not null,
        document_revision text not null,
        operation text not null default 'upsert',
        status text not null default 'pending',
        task_id text not null default '',
        available_at timestamptz not null,
        last_error text not null default '',
        created_at timestamptz not null,
        updated_at timestamptz not null,
        unique(workspace_id, knowledge_base_id, document_id, document_revision, operation),
        foreign key(workspace_id, knowledge_base_id) references {schema}.knowledge_base(workspace_id, id) on delete cascade
    );

    create table {schema}.wiki_log_entry (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        idempotency_key text not null,
        event_type text not null,
        document_id text not null default '',
        page_slugs_json jsonb not null default '[]'::jsonb,
        outcome text not null default 'completed',
        message text not null default '',
        metadata_json jsonb not null default '{{}}'::jsonb,
        created_at timestamptz not null,
        unique(workspace_id, knowledge_base_id, idempotency_key),
        foreign key(workspace_id, knowledge_base_id) references {schema}.knowledge_base(workspace_id, id) on delete cascade
    );

    create table {schema}.wiki_page_commit (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        idempotency_key text not null,
        page_slug text not null,
        page_version integer not null,
        generation_run_id text not null default '',
        affected_slug text not null default '',
        created_at timestamptz not null,
        unique(workspace_id, knowledge_base_id, idempotency_key),
        foreign key(workspace_id, knowledge_base_id) references {schema}.knowledge_base(workspace_id, id) on delete cascade
    );

    create table {schema}.wiki_ingest_commit (
        id text primary key,
        workspace_id text not null,
        knowledge_base_id text not null,
        idempotency_key text not null,
        document_id text not null,
        document_revision text not null,
        generation_run_id text not null,
        affected_slugs_json jsonb not null default '[]'::jsonb,
        created_at timestamptz not null,
        unique(workspace_id, knowledge_base_id, idempotency_key),
        foreign key(workspace_id, knowledge_base_id) references {schema}.knowledge_base(workspace_id, id) on delete cascade
    );
    """


def _memory_eval_agent_ddl(config: PostgresSchemaConfig) -> str:
    schema = quote_ident(config.schema)
    return f"""
    create table {schema}.conversation (
        id text primary key,
        tenant_id text not null default '',
        user_id text not null default '',
        title text not null default '',
        summary text not null default '',
        agent_config jsonb not null default '{{}}'::jsonb,
        created_at timestamptz not null,
        updated_at timestamptz not null,
        deleted_at timestamptz
    );
    create table {schema}.conversation_message (
        id text primary key,
        conversation_id text not null references {schema}.conversation(id) on delete cascade,
        request_id text not null default '',
        role text not null,
        content text not null,
        metadata_json jsonb not null default '{{}}'::jsonb,
        is_completed boolean not null default true,
        created_at timestamptz not null,
        updated_at timestamptz not null,
        deleted_at timestamptz
    );
    create table {schema}.memory (
        id text primary key,
        scope text not null,
        type text not null,
        normalized_key text not null,
        memory_key text not null default '',
        content text not null,
        confidence double precision not null default 1.0,
        status text not null default 'active',
        source_conversation_id text,
        source_message_id text,
        metadata_json jsonb not null default '{{}}'::jsonb,
        created_at timestamptz not null,
        updated_at timestamptz not null,
        unique(scope, normalized_key, status)
    );
    create table {schema}.eval_run (
        id text primary key,
        dataset_id text not null,
        dataset_version text not null,
        dataset_path text not null,
        status text not null,
        started_at timestamptz not null,
        finished_at timestamptz,
        created_at timestamptz not null,
        updated_at timestamptz not null,
        config_snapshot jsonb not null,
        aggregate_scores jsonb not null,
        report_paths jsonb not null,
        error_message text not null,
        knowledge_base_ids_json jsonb not null default '[]'::jsonb
    );
    create table {schema}.eval_result (
        id text primary key,
        run_id text not null references {schema}.eval_run(id) on delete cascade,
        case_id text not null,
        status text not null,
        question text not null,
        query_type text not null,
        tags jsonb not null,
        case_snapshot jsonb not null,
        answer text not null,
        response_snapshot jsonb not null,
        evidence_snapshot jsonb not null,
        metric_scores jsonb not null,
        latency_ms double precision not null,
        error_message text not null,
        created_at timestamptz not null,
        knowledge_base_ids_json jsonb not null default '[]'::jsonb
    );
    create table {schema}.agent_runtime_spans (
        id bigserial primary key,
        run_id text not null,
        span_id text not null unique,
        parent_span_id text,
        name text not null,
        kind text not null,
        status text not null,
        input_json jsonb not null default '{{}}'::jsonb,
        output_json jsonb not null default '{{}}'::jsonb,
        metadata_json jsonb not null default '{{}}'::jsonb,
        error_code text not null default '',
        error_message text not null default '',
        started_at timestamptz,
        finished_at timestamptz,
        duration_ms integer not null default 0,
        created_at timestamptz not null,
        updated_at timestamptz not null
    );

    create table {schema}.plugin_setting (
        workspace_id text not null,
        plugin_id text not null,
        enabled boolean not null default false,
        mode_bindings_json jsonb not null default '[]'::jsonb,
        config_json jsonb not null default '{{}}'::jsonb,
        status_metadata_json jsonb not null default '{{}}'::jsonb,
        created_at timestamptz not null,
        updated_at timestamptz not null,
        primary key(workspace_id, plugin_id),
        foreign key(workspace_id) references {schema}.workspace(id)
    );
    """


def _marketplace_ddl(config: PostgresSchemaConfig) -> str:
    return _marketplace_table_ddl(config.schema)


def _marketplace_table_ddl(schema: str) -> str:
    schema = quote_ident(schema)
    return f"""
    create table {schema}.marketplace_owner (
        handle text primary key,
        kind text not null default 'user' check (kind in ('user', 'org')),
        display_name text not null default '',
        created_at timestamptz not null,
        updated_at timestamptz not null
    );

    create table {schema}.marketplace_token (
        id text primary key,
        token_hash text not null unique,
        owner_handle text not null references {schema}.marketplace_owner(handle) on delete cascade,
        scopes_json jsonb not null default '[]'::jsonb,
        created_at timestamptz not null,
        last_used_at timestamptz,
        revoked_at timestamptz
    );

    create table {schema}.marketplace_package (
        name text primary key,
        owner_handle text not null references {schema}.marketplace_owner(handle),
        description text not null default '',
        category text not null default '',
        keywords_json jsonb not null default '[]'::jsonb,
        visibility text not null default 'public' check (visibility in ('public', 'private')),
        latest_version text not null default '',
        created_at timestamptz not null,
        updated_at timestamptz not null
    );

    create table {schema}.marketplace_package_version (
        id text primary key,
        package_name text not null references {schema}.marketplace_package(name) on delete cascade,
        version text not null,
        manifest_json jsonb not null default '{{}}'::jsonb,
        components_json jsonb not null default '{{}}'::jsonb,
        content_hash text not null,
        size_bytes bigint not null default 0,
        storage_key text not null,
        status text not null default 'published' check (status in ('published', 'yanked')),
        published_by_owner_handle text not null default '',
        published_at timestamptz not null,
        unique(package_name, version)
    );

    create table {schema}.marketplace_snapshot (
        id text primary key,
        revision text not null,
        catalog_json jsonb not null default '{{}}'::jsonb,
        package_count integer not null default 0,
        built_at timestamptz not null
    );

    create index idx_marketplace_package_owner on {schema}.marketplace_package(owner_handle, updated_at);
    create index idx_marketplace_package_visibility on {schema}.marketplace_package(visibility, name);
    create index idx_marketplace_version_package_status on {schema}.marketplace_package_version(package_name, status, published_at);
    create index idx_marketplace_token_owner on {schema}.marketplace_token(owner_handle);
    """


def _ensure_marketplace_schema(conn: Any, config: PostgresSchemaConfig) -> None:
    missing = [table for table in _MARKETPLACE_TABLES if not _table_exists(conn, config.schema, table)]
    if not missing:
        return
    with conn.cursor() as cur:
        cur.execute(_marketplace_table_ddl(config.schema))


def _postgres_indexes(config: PostgresSchemaConfig) -> str:
    schema = quote_ident(config.schema)
    vector_ops = "vector_cosine_ops" if config.vector_type == "vector" else "halfvec_cosine_ops"
    return f"""
    create index idx_knowledge_base_workspace_status on {schema}.knowledge_base(workspace_id, status, updated_at);
    create unique index idx_knowledge_base_workspace_default on {schema}.knowledge_base(workspace_id) where is_default;
    create index idx_document_kb_updated on {schema}.document(workspace_id, knowledge_base_id, updated_at);
    create index idx_document_kb_status on {schema}.document(workspace_id, knowledge_base_id, parse_status);
    create index idx_document_chunk_kb_doc on {schema}.document_chunk(workspace_id, knowledge_base_id, doc_id, chunk_type);
    create index idx_document_chunk_search on {schema}.document_chunk using gin(search_vector);
    create index idx_document_chunk_trgm on {schema}.document_chunk using gin((title_path || ' ' || content_markdown || ' ' || content) gin_trgm_ops);
    create index idx_document_chunk_embedding_scope on {schema}.document_chunk_embedding(workspace_id, knowledge_base_id, doc_id, chunk_type);
    create index idx_document_chunk_embedding_hnsw on {schema}.document_chunk_embedding using hnsw (embedding {vector_ops}) with (m = {int(config.hnsw_m)}, ef_construction = {int(config.hnsw_ef_construction)});
    create index idx_entity_embedding_scope on {schema}.entity_embedding(workspace_id, knowledge_base_id, entity_id);
    create index idx_entity_embedding_hnsw on {schema}.entity_embedding using hnsw (embedding {vector_ops}) with (m = {int(config.hnsw_m)}, ef_construction = {int(config.hnsw_ef_construction)});
    create index idx_parse_task_kb_status on {schema}.parse_task(workspace_id, knowledge_base_id, status);
    create index idx_image_resource_kb_doc on {schema}.document_image_resource(workspace_id, knowledge_base_id, doc_id, page_number);
    create index idx_image_operation_kb_status on {schema}.document_image_operation(workspace_id, knowledge_base_id, status, updated_at);
    create index idx_enrichment_task_kb_doc on {schema}.document_enrichment_task(workspace_id, knowledge_base_id, doc_id, version);
    create index idx_spans_knowledge_attempt on {schema}.knowledge_processing_spans(knowledge_id, attempt);
    create index idx_spans_parent on {schema}.knowledge_processing_spans(parent_span_id);
    create index idx_processing_task_runnable on {schema}.document_processing_task(status, next_run_at, lease_expires_at);
    create index idx_processing_task_scope_doc on {schema}.document_processing_task(workspace_id, knowledge_base_id, document_id, status);
    create index idx_processing_task_upload on {schema}.document_processing_task(workspace_id, knowledge_base_id, upload_batch_id, upload_file_id);
    create index idx_processing_task_attempt_task on {schema}.document_processing_task_attempt(task_id, created_at, id);
    create index idx_processing_dead_letter_scope on {schema}.document_processing_dead_letter(workspace_id, knowledge_base_id, created_at);
    create index idx_kg_task_kb_doc on {schema}.kg_extraction_task(workspace_id, knowledge_base_id, doc_id);
    create index idx_entity_mention_kb_entity on {schema}.entity_mention(workspace_id, knowledge_base_id, entity_id);
    create index idx_graph_summary_kb on {schema}.graph_community_summary(workspace_id, knowledge_base_id, updated_at);
    create index idx_query_log_scope_created on {schema}.query_log(workspace_id, created_at);
    create index idx_answer_feedback_kb_created on {schema}.answer_feedback(workspace_id, knowledge_base_id, created_at);
    create index idx_upload_batch_kb_status on {schema}.knowledge_upload_batch(workspace_id, knowledge_base_id, status, updated_at);
    create index idx_upload_file_batch_status on {schema}.knowledge_upload_file(workspace_id, knowledge_base_id, batch_id, status);
    create unique index idx_wiki_page_active_slug on {schema}.wiki_page(workspace_id, knowledge_base_id, slug) where status != 'archived';
    create index idx_wiki_page_kb_status_type on {schema}.wiki_page(workspace_id, knowledge_base_id, status, page_type, updated_at);
    create index idx_wiki_page_search on {schema}.wiki_page using gin(search_vector);
    create index idx_wiki_page_trgm on {schema}.wiki_page using gin((title || ' ' || slug || ' ' || summary || ' ' || content_markdown) gin_trgm_ops);
    create index idx_wiki_page_folder_sort on {schema}.wiki_page(workspace_id, knowledge_base_id, folder_id, sort_order, title);
    create index idx_wiki_page_path on {schema}.wiki_page(workspace_id, knowledge_base_id, wiki_path);
    create index idx_wiki_folder_tree on {schema}.wiki_folder(workspace_id, knowledge_base_id, parent_id, sort_order, name);
    create index idx_wiki_source_ref_doc on {schema}.wiki_page_source_ref(workspace_id, knowledge_base_id, doc_id, chunk_id);
    create index idx_wiki_issue_status on {schema}.wiki_page_issue(workspace_id, knowledge_base_id, status, updated_at);
    create index idx_wiki_issue_slug on {schema}.wiki_page_issue(workspace_id, knowledge_base_id, slug, status);
    create index idx_wiki_proposal_status on {schema}.wiki_page_proposal(workspace_id, knowledge_base_id, status, updated_at);
    create index idx_wiki_proposal_slug on {schema}.wiki_page_proposal(workspace_id, knowledge_base_id, slug, status);
    create index idx_wiki_generation_task_doc on {schema}.wiki_generation_task(workspace_id, knowledge_base_id, doc_id, updated_at);
    create index idx_wiki_generation_task_status on {schema}.wiki_generation_task(workspace_id, knowledge_base_id, status, updated_at);
    create index idx_wiki_contribution_doc on {schema}.wiki_document_contribution(workspace_id, knowledge_base_id, document_id, active, updated_at);
    create index idx_wiki_contribution_slug on {schema}.wiki_document_contribution(workspace_id, knowledge_base_id, page_slug, active, updated_at);
    create index idx_wiki_ingest_pending_runnable on {schema}.wiki_ingest_pending(status, available_at, workspace_id, knowledge_base_id);
    create index idx_wiki_log_scope_created on {schema}.wiki_log_entry(workspace_id, knowledge_base_id, created_at, id);
    create index idx_wiki_page_commit_slug on {schema}.wiki_page_commit(workspace_id, knowledge_base_id, page_slug, page_version);
    create index idx_wiki_ingest_commit_document on {schema}.wiki_ingest_commit(workspace_id, knowledge_base_id, document_id, document_revision);
    create index idx_conversation_owner on {schema}.conversation(tenant_id, user_id, updated_at);
    create index idx_conversation_message_conversation on {schema}.conversation_message(conversation_id, created_at);
    create index idx_conversation_message_request on {schema}.conversation_message(conversation_id, request_id, role);
    create index idx_memory_status_updated on {schema}.memory(status, updated_at);
    create index idx_memory_scope_key on {schema}.memory(scope, normalized_key, status);
    create index idx_eval_run_status_updated on {schema}.eval_run(status, updated_at);
    create index idx_eval_result_run_case on {schema}.eval_result(run_id, case_id);
    create index idx_agent_runtime_spans_run on {schema}.agent_runtime_spans(run_id);
    create index idx_agent_runtime_spans_parent on {schema}.agent_runtime_spans(parent_span_id);
    create index idx_plugin_setting_workspace_updated on {schema}.plugin_setting(workspace_id, updated_at);
    """


def _ensure_plugin_settings_schema(conn: Any, config: PostgresSchemaConfig) -> None:
    if _table_exists(conn, config.schema, "plugin_setting"):
        return
    schema = quote_ident(config.schema)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            create table {schema}.plugin_setting (
                workspace_id text not null,
                plugin_id text not null,
                enabled boolean not null default false,
                mode_bindings_json jsonb not null default '[]'::jsonb,
                config_json jsonb not null default '{{}}'::jsonb,
                status_metadata_json jsonb not null default '{{}}'::jsonb,
                created_at timestamptz not null,
                updated_at timestamptz not null,
                primary key(workspace_id, plugin_id),
                foreign key(workspace_id) references {schema}.workspace(id)
            )
            """
        )
        cur.execute(
            f"create index idx_plugin_setting_workspace_updated on {schema}.plugin_setting(workspace_id, updated_at)"
        )


def _installed_extensions(conn: Any) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("select extname from pg_extension")
        return {str(row["extname"]) for row in cur.fetchall()}


def _user_tables(conn: Any, schema: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select table_name
            from information_schema.tables
            where table_schema = %s and table_type = 'BASE TABLE'
            """,
            (schema,),
        )
        return {str(row["table_name"]) for row in cur.fetchall()}


def _table_exists(conn: Any, schema: str, table: str) -> bool:
    return table in _user_tables(conn, schema)


def _jsonb_literal(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


_POSTGRES_TABLES = {
    "storage_schema",
    "workspace",
    "knowledge_base",
    "document",
    "document_chunk",
    "document_chunk_embedding",
    "parse_task",
    "document_image_resource",
    "document_image_operation",
    "document_enrichment_task",
    "knowledge_processing_spans",
    "document_processing_task",
    "document_processing_dead_letter",
    "document_processing_task_attempt",
    "kg_extraction_task",
    "entity_mention",
    "entity_embedding",
    "graph_community_summary",
    "query_log",
    "answer_feedback",
    "knowledge_upload_batch",
    "knowledge_upload_file",
    "wiki_folder",
    "wiki_page",
    "wiki_page_source_ref",
    "wiki_page_issue",
    "wiki_page_proposal",
    "wiki_generation_task",
    "wiki_document_contribution",
    "wiki_ingest_pending",
    "wiki_log_entry",
    "wiki_page_commit",
    "wiki_ingest_commit",
    "conversation",
    "conversation_message",
    "memory",
    "eval_run",
    "eval_result",
    "agent_runtime_spans",
    "plugin_setting",
    "marketplace_owner",
    "marketplace_token",
    "marketplace_package",
    "marketplace_package_version",
    "marketplace_snapshot",
}

_MARKETPLACE_TABLES = (
    "marketplace_owner",
    "marketplace_token",
    "marketplace_package",
    "marketplace_package_version",
    "marketplace_snapshot",
)
