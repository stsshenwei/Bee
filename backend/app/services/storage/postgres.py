from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator


class PostgresConfigurationError(RuntimeError):
    """Raised when PostgreSQL production storage is not configured."""


class PostgresIntegrityError(RuntimeError):
    """Raised when PostgreSQL rejects a repository write on an integrity constraint."""


@dataclass(frozen=True)
class PostgresSettings:
    database_url: str
    schema: str = "public"
    pool_min_size: int = 1
    pool_max_size: int = 10
    autocommit: bool = False

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "PostgresSettings":
        env = environ if environ is not None else os.environ
        database_url = str(env.get("DATABASE_URL") or "").strip()
        if not database_url:
            raise PostgresConfigurationError("DATABASE_URL is required for PostgreSQL production storage")
        schema = _schema_env(env.get("POSTGRES_SCHEMA"))
        return cls(
            database_url=database_url,
            schema=schema,
            pool_min_size=_int_env(env, "POSTGRES_POOL_MIN_SIZE", 1),
            pool_max_size=_int_env(env, "POSTGRES_POOL_MAX_SIZE", 10),
        )


class PostgresDatabase:
    def __init__(self, settings: PostgresSettings, *, pool: Any | None = None):
        self.settings = settings
        self._pool = pool

    @property
    def pool(self) -> Any:
        if self._pool is None:
            try:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
            except Exception as exc:
                raise RuntimeError("PostgreSQL support requires psycopg[binary,pool].") from exc
            kwargs: dict[str, Any] = {
                "conninfo": self.settings.database_url,
                "min_size": self.settings.pool_min_size,
                "max_size": self.settings.pool_max_size,
                "kwargs": {"row_factory": dict_row, "autocommit": self.settings.autocommit},
            }
            self._pool = ConnectionPool(**kwargs)
        return self._pool

    @contextmanager
    def connection(self) -> Iterator[Any]:
        with self.pool.connection() as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self.connection() as conn:
            with conn.transaction():
                yield conn

    def close(self) -> None:
        close = getattr(self._pool, "close", None)
        if callable(close):
            close()


def _schema_env(raw: object) -> str:
    value = str(raw or "public").strip() or "public"
    for _ in range(2):
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '\"'}:
            value = value[1:-1].strip() or "public"
            continue
        break
    return value


def quote_ident(identifier: str) -> str:
    clean = str(identifier or "").strip()
    if not clean:
        raise ValueError("SQL identifier cannot be empty")
    return '"' + clean.replace('"', '""') + '"'


def _int_env(env: dict[str, str], key: str, default: int) -> int:
    raw = str(env.get(key, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise PostgresConfigurationError(f"{key} must be an integer") from exc
    if value < 1:
        raise PostgresConfigurationError(f"{key} must be >= 1")
    return value
