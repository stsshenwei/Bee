from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol


RESET_CONFIRMATION = "RESET_ALL_APPLICATION_DATA"


@dataclass(frozen=True)
class ResetPlanItem:
    provider: str
    target: str
    action: str
    exists: bool


@dataclass
class ProviderResetResult:
    provider: str
    status: str
    details: dict[str, object] = field(default_factory=dict)
    error: str = ""


class KnowledgeStorageResetProvider(Protocol):
    name: str

    def plan(self) -> list[ResetPlanItem]: ...

    def backup(self, backup_root: Path) -> dict[str, object]: ...

    def reset(self) -> dict[str, object]: ...

    def initialize(self) -> dict[str, object]: ...


class SQLiteStorageResetProvider:
    def __init__(self, name: str, db_path: Path | str, initializer: Callable[[], None] | None = None):
        self.name = name
        self.db_path = Path(db_path).resolve()
        self.initializer = initializer

    def _targets(self) -> list[Path]:
        return [self.db_path, Path(f"{self.db_path}-wal"), Path(f"{self.db_path}-shm")]

    def plan(self) -> list[ResetPlanItem]:
        return [ResetPlanItem(self.name, str(path), "delete", path.exists()) for path in self._targets()]

    def backup(self, backup_root: Path) -> dict[str, object]:
        copied = []
        destination = backup_root / self.name
        destination.mkdir(parents=True, exist_ok=True)
        for path in self._targets():
            if path.exists() and path.is_file():
                target = destination / path.name
                shutil.copy2(path, target)
                copied.append(str(target))
        return {"copied": copied}

    def reset(self) -> dict[str, object]:
        removed = []
        for path in self._targets():
            if path.exists():
                path.unlink()
                removed.append(str(path))
        return {"removed": removed}

    def initialize(self) -> dict[str, object]:
        if self.initializer is not None:
            self.initializer()
        return {"initialized": self.initializer is not None, "path": str(self.db_path)}


class PostgresStorageResetProvider:
    name = "postgres"

    def __init__(self, database, *, config=None, defaults=None, target_label: str = ""):
        from app.services.storage.postgres_schema import PostgresSchemaConfig

        self.database = database
        self.config = config or PostgresSchemaConfig(schema=database.settings.schema)
        self.defaults = defaults
        self.target_label = target_label or f"schema={self.config.schema}"

    def plan(self) -> list[ResetPlanItem]:
        return [ResetPlanItem(self.name, self.target_label, "drop schema and initialize final PostgreSQL schema", True)]

    def backup(self, backup_root: Path) -> dict[str, object]:
        return {"supported": False, "reason": "Use pg_dump or a PostgreSQL-native backup before clean-rebuild when required"}

    def reset(self) -> dict[str, object]:
        from app.services.storage.postgres import quote_ident

        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(f"drop schema if exists {quote_ident(self.config.schema)} cascade")
        return {"dropped_schema": self.config.schema}

    def initialize(self) -> dict[str, object]:
        from app.services.storage.postgres_schema import POSTGRES_SCHEMA_VERSION, initialize_postgres_database

        initialize_postgres_database(self.database, config=self.config, defaults=self.defaults)
        return {
            "initialized": True,
            "schema": self.config.schema,
            "version": POSTGRES_SCHEMA_VERSION,
            "vector_dimension": self.config.vector_dimension,
            "vector_type": self.config.vector_type,
        }


class RetiredMilvusArtifactsProvider:
    name = "retired-milvus"

    def __init__(self, rag_collection: str, entity_collection: str):
        self.rag_collection = rag_collection
        self.entity_collection = entity_collection

    def plan(self) -> list[ResetPlanItem]:
        return [
            ResetPlanItem(self.name, self.rag_collection, "retired collection ignored by PostgreSQL startup", True),
            ResetPlanItem(self.name, self.entity_collection, "retired collection ignored by PostgreSQL startup", True),
        ]

    def backup(self, backup_root: Path) -> dict[str, object]:
        return {"supported": False, "reason": "Use a Milvus-native backup/export outside normal startup when required"}

    def reset(self) -> dict[str, object]:
        return {"retired": [self.rag_collection, self.entity_collection], "dropped": []}

    def initialize(self) -> dict[str, object]:
        return {"initialized": False, "reason": "Milvus is retired for PostgreSQL storage"}


class ManagedFilesResetProvider:
    def __init__(
        self,
        managed_root: Path | str,
        relative_targets: list[str],
        *,
        enabled: bool,
        name: str = "managed-files",
    ):
        self.name = name
        self.managed_root = Path(managed_root).resolve()
        self.enabled = enabled
        self.targets = [self._safe_target(item) for item in relative_targets]

    def _safe_target(self, relative: str) -> Path:
        candidate = (self.managed_root / relative).resolve()
        if candidate == self.managed_root or self.managed_root not in candidate.parents:
            raise ValueError(f"Managed reset target escapes root: {relative!r}")
        return candidate

    def plan(self) -> list[ResetPlanItem]:
        action = "delete" if self.enabled else "preserve (enable --delete-managed-sources to delete)"
        return [ResetPlanItem(self.name, str(path), action, path.exists()) for path in self.targets]

    def backup(self, backup_root: Path) -> dict[str, object]:
        if not self.enabled:
            return {"copied": []}
        copied = []
        destination = backup_root / self.name
        for path in self.targets:
            if not path.exists():
                continue
            target = destination / path.name
            if path.is_dir():
                shutil.copytree(path, target, dirs_exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            copied.append(str(target))
        return {"copied": copied}

    def reset(self) -> dict[str, object]:
        if not self.enabled:
            return {"removed": [], "preserved": [str(path) for path in self.targets if path.exists()]}
        removed = []
        for path in self.targets:
            if path.is_dir():
                shutil.rmtree(path)
                removed.append(str(path))
            elif path.exists():
                path.unlink()
                removed.append(str(path))
        return {"removed": removed}

    def initialize(self) -> dict[str, object]:
        return {"initialized": False}


class MilvusCollectionsResetProvider:
    name = "milvus"

    def __init__(
        self,
        uri: str,
        token: str,
        rag_collection: str,
        entity_collection: str,
        embedding_dim: int,
        bm25_enabled: bool,
    ):
        self.uri = uri
        self.token = token
        self.rag_collection = rag_collection
        self.entity_collection = entity_collection
        self.embedding_dim = embedding_dim
        self.bm25_enabled = bm25_enabled

    def _connect(self):
        from pymilvus import connections, utility

        kwargs = {"alias": "default", "uri": self.uri}
        if self.token:
            kwargs["token"] = self.token
        connections.connect(**kwargs)
        return utility

    def plan(self) -> list[ResetPlanItem]:
        utility = self._connect()
        collections = list(utility.list_collections())
        return [ResetPlanItem(self.name, collection, "drop", True) for collection in collections] or [
            ResetPlanItem(self.name, "all collections", "already empty", False)
        ]

    def backup(self, backup_root: Path) -> dict[str, object]:
        return {"supported": False, "reason": "Use a Milvus-native backup before clean-rebuild when required"}

    def reset(self) -> dict[str, object]:
        utility = self._connect()
        dropped = []
        for collection in list(utility.list_collections()):
            utility.drop_collection(collection)
            dropped.append(collection)
        return {"dropped": dropped}

    def initialize(self) -> dict[str, object]:
        from app.services.kg.entity_vector_store import _create_or_load_entity_collection
        from app.services.retrieval.vector_store import _create_or_load_collection

        rag = _create_or_load_collection(
            self.uri,
            self.token,
            self.rag_collection,
            self.embedding_dim,
            bm25_enabled=self.bm25_enabled,
        )
        entity = _create_or_load_entity_collection(
            self.uri,
            self.token,
            self.entity_collection,
            self.embedding_dim,
        )
        for collection in (rag, entity):
            release = getattr(collection, "release", None)
            if callable(release):
                release()
        return {"created": [self.rag_collection, self.entity_collection]}


class Neo4jStorageResetProvider:
    name = "neo4j"

    def __init__(self, uri: str, user: str, password: str, driver_factory=None):
        self.uri = uri
        self.user = user
        self.password = password
        self.driver_factory = driver_factory

    def _driver(self):
        if self.driver_factory is not None:
            return self.driver_factory(self.uri, auth=(self.user, self.password))
        from neo4j import GraphDatabase

        return GraphDatabase.driver(self.uri, auth=(self.user, self.password))

    def plan(self) -> list[ResetPlanItem]:
        return [ResetPlanItem(self.name, self.uri, "delete all graph nodes and recreate constraints", True)]

    def backup(self, backup_root: Path) -> dict[str, object]:
        return {"supported": False, "reason": "Use neo4j-admin backup when graph backup is required"}

    def reset(self) -> dict[str, object]:
        driver = self._driver()
        try:
            with driver.session() as session:
                result = session.run(
                    "MATCH (n) WITH collect(n) AS nodes "
                    "FOREACH (n IN nodes | DETACH DELETE n) RETURN size(nodes) AS removed"
                ).single()
                return {"removed_nodes": int(result["removed"] if result else 0)}
        finally:
            driver.close()

    def initialize(self) -> dict[str, object]:
        driver = self._driver()
        try:
            with driver.session() as session:
                session.run("CREATE CONSTRAINT bee_entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE")
            return {"constraints": ["bee_entity_id"]}
        finally:
            driver.close()


class KnowledgeStorageResetCoordinator:
    def __init__(
        self,
        providers: list[KnowledgeStorageResetProvider],
        state_dir: Path | str,
        runtime_lock: Path | str,
    ):
        self.providers = providers
        self.state_dir = Path(state_dir).resolve()
        self.runtime_lock = Path(runtime_lock).resolve()
        self.manifest_path = self.state_dir / "reset-manifest.json"
        self.maintenance_path = self.state_dir / "maintenance.json"

    def plan(self) -> list[ResetPlanItem]:
        return [item for provider in self.providers for item in provider.plan()]

    def execute(self, *, confirmation: str, backup_dir: Path | None = None) -> dict[str, object]:
        if confirmation != RESET_CONFIRMATION:
            raise ValueError(f"Confirmation must exactly equal {RESET_CONFIRMATION}")
        if _active_pid(self.runtime_lock):
            raise RuntimeError("The API or a worker is still active; stop all writers before clean-rebuild")
        if backup_dir is not None:
            backup_dir = backup_dir.resolve()
            for item in self.plan():
                target = Path(item.target)
                if target.exists() and (backup_dir == target or target in backup_dir.parents):
                    raise ValueError("Backup directory must not be inside a reset target")

        self.state_dir.mkdir(parents=True, exist_ok=True)
        started_at = _now()
        manifest: dict[str, object] = {
            "status": "running",
            "started_at": started_at,
            "finished_at": None,
            "confirmation": "accepted",
            "plan": [asdict(item) for item in self.plan()],
            "results": [],
        }
        self._write_state(self.maintenance_path, {"status": "maintenance", "started_at": started_at})
        self._write_state(self.manifest_path, manifest)
        results: list[dict[str, object]] = []
        try:
            if backup_dir is not None:
                backup_dir.mkdir(parents=True, exist_ok=True)
                for provider in self.providers:
                    details = provider.backup(backup_dir)
                    results.append(asdict(ProviderResetResult(provider.name, "backed_up", details)))
            for provider in self.providers:
                details = provider.reset()
                results.append(asdict(ProviderResetResult(provider.name, "reset", details)))
            for provider in self.providers:
                details = provider.initialize()
                results.append(asdict(ProviderResetResult(provider.name, "initialized", details)))
            manifest.update({"status": "completed", "finished_at": _now(), "results": results})
            self._write_state(self.manifest_path, manifest)
            self.maintenance_path.unlink(missing_ok=True)
            return manifest
        except Exception as exc:
            results.append(asdict(ProviderResetResult("coordinator", "failed", error=str(exc))))
            manifest.update({"status": "failed", "finished_at": _now(), "results": results})
            self._write_state(self.manifest_path, manifest)
            self._write_state(
                self.maintenance_path,
                {"status": "maintenance", "started_at": started_at, "error": str(exc)},
            )
            raise

    @staticmethod
    def _write_state(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def write_runtime_lock(path: Path | str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"pid": os.getpid(), "started_at": _now()}), encoding="utf-8")


def clear_runtime_lock(path: Path | str) -> None:
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except Exception:
        data = {}
    if int(data.get("pid", 0) or 0) in {0, os.getpid()}:
        target.unlink(missing_ok=True)


def _active_pid(lock_path: Path) -> bool:
    if not lock_path.exists():
        return False
    try:
        pid = int(json.loads(lock_path.read_text(encoding="utf-8")).get("pid", 0) or 0)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        lock_path.unlink(missing_ok=True)
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
