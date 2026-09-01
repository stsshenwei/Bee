from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from app.services.storage.storage_reset import (
    KnowledgeStorageResetCoordinator,
    ManagedFilesResetProvider,
    Neo4jStorageResetProvider,
    PostgresStorageResetProvider,
    RESET_CONFIRMATION,
    RetiredMilvusArtifactsProvider,
    SQLiteStorageResetProvider,
)
from app.services.storage.postgres import PostgresDatabase, PostgresSettings
from app.services.storage.postgres_schema import PostgresSchemaConfig
from app.services.storage.storage_schema import (
    DefaultKnowledgeBaseSettings,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Destroy ALL legacy Bee application data and create the final empty schema")
    parser.add_argument("--execute", action="store_true", help="Execute the displayed reset plan")
    parser.add_argument("--environment", default="", help="Explicit target environment name; required with --execute")
    parser.add_argument("--confirm", default="", help=f"Must exactly equal {RESET_CONFIRMATION}:<environment>")
    parser.add_argument("--delete-managed-sources", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--backup-dir", type=Path, help="Optional backup directory for retired files and managed files")
    parser.add_argument("--skip-milvus", action="store_true", help="Skip listing retired Milvus collections")
    parser.add_argument("--include-neo4j", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-neo4j", action="store_true", help="Skip Neo4j only for isolated offline schema tests")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    vector_dir = Path(_env("VECTOR_STORE_DIR", "CHROMA_DIR", default="./vector_db")).resolve()
    data_dir = Path(_env("RAG_DATA_DIR", default="./data")).resolve()
    metadata_path = Path(_env("METADATA_DB_PATH", default=str(vector_dir / "rag_metadata.sqlite3"))).resolve()
    kg_metadata_path = Path(_env("KG_METADATA_DB_PATH", default=str(metadata_path))).resolve()
    eval_path = Path(_env("EVAL_DB_PATH", default=str(vector_dir / "rag_eval.sqlite3"))).resolve()
    memory_path = Path(_env("MEMORY_DB_PATH", default=str(vector_dir / "rag_memory.sqlite3"))).resolve()
    report_dir = Path(_env("EVAL_REPORT_DIR", default=str(vector_dir / "eval_reports"))).resolve()
    state_dir = Path(_env("STORAGE_RESET_STATE_DIR", default=str(vector_dir / "reset-state"))).resolve()
    runtime_lock = Path(_env("STORAGE_RUNTIME_LOCK", default=str(vector_dir / "runtime.lock"))).resolve()
    postgres_settings = PostgresSettings.from_env()
    postgres_database = PostgresDatabase(postgres_settings)
    postgres_config = PostgresSchemaConfig(
        schema=postgres_settings.schema,
        vector_dimension=int(_env("EMBEDDING_DIM", default="1536")),
        vector_type=_env("PGVECTOR_TYPE", "POSTGRES_VECTOR_TYPE", default="vector"),
    )
    defaults = DefaultKnowledgeBaseSettings(
        workspace_id=_env("DEFAULT_WORKSPACE_ID", default="default-workspace"),
        workspace_name=_env("DEFAULT_WORKSPACE_NAME", default="默认工作空间"),
        knowledge_base_id=_env("DEFAULT_KNOWLEDGE_BASE_ID", default="default-knowledge-base"),
        knowledge_base_name=_env("DEFAULT_KNOWLEDGE_BASE_NAME", default="默认知识库"),
    )

    providers = [
        PostgresStorageResetProvider(
            postgres_database,
            config=postgres_config,
            defaults=defaults,
            target_label=f"{_safe_database_label(postgres_settings.database_url)}#{postgres_settings.schema}",
        ),
        SQLiteStorageResetProvider(
            "retired-metadata-sqlite",
            metadata_path,
        ),
        SQLiteStorageResetProvider("retired-evaluation-sqlite", eval_path),
        SQLiteStorageResetProvider("retired-memory-sqlite", memory_path),
        ManagedFilesResetProvider(
            data_dir,
            ["uploads", "feedback"],
            enabled=True,
            name="managed-sources",
        ),
        ManagedFilesResetProvider(
            report_dir.parent,
            [report_dir.name],
            enabled=True,
            name="evaluation-reports",
        ),
        ManagedFilesResetProvider(
            vector_dir,
            ["ingest_state.json", "ingest-state"],
            enabled=True,
            name="generated-ingest-state",
        ),
    ]
    media_dir = Path(_env("MEDIA_STORAGE_DIR", default=str(vector_dir / "media"))).resolve()
    if media_dir.parent == vector_dir:
        providers.append(ManagedFilesResetProvider(vector_dir, [media_dir.name], enabled=True, name="legacy-media"))
    if kg_metadata_path != metadata_path:
        providers.append(
            SQLiteStorageResetProvider("retired-kg-metadata-sqlite", kg_metadata_path)
        )
    if not args.skip_milvus:
        providers.append(
            RetiredMilvusArtifactsProvider(
                rag_collection=_env("MILVUS_COLLECTION", default="rag_chunk_vectors"),
                entity_collection=_env("KG_ENTITY_COLLECTION", default="kg_entity_vectors"),
            )
        )
    if not args.skip_neo4j:
        providers.append(
            Neo4jStorageResetProvider(
                _env("NEO4J_URI", default="bolt://localhost:7687"),
                _env("NEO4J_USER", default="neo4j"),
                _env("NEO4J_PASSWORD", default="password"),
            )
        )

    coordinator = KnowledgeStorageResetCoordinator(providers, state_dir, runtime_lock)
    plan = [item.__dict__ for item in coordinator.plan()]
    print(json.dumps({"mode": "execute" if args.execute else "dry-run", "plan": plan}, ensure_ascii=False, indent=2))
    if not args.execute:
        return 0
    if not args.environment.strip():
        raise ValueError("--environment is required with --execute")
    expected = f"{RESET_CONFIRMATION}:{args.environment.strip()}"
    if args.confirm != expected:
        raise ValueError(f"Confirmation must exactly equal {expected}")
    manifest = coordinator.execute(confirmation=RESET_CONFIRMATION, backup_dir=args.backup_dir)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def _env(*names: str, default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return default


def _safe_database_label(database_url: str) -> str:
    if "@" not in database_url or "://" not in database_url:
        return database_url
    scheme, rest = database_url.split("://", 1)
    credentials, host = rest.split("@", 1)
    if ":" not in credentials:
        return database_url
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


if __name__ == "__main__":
    raise SystemExit(main())
