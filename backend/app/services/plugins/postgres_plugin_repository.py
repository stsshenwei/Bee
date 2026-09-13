from __future__ import annotations

import json
from typing import Any

from app.services.plugins.plugin_models import PluginSetting
from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import PostgresSchemaConfig, ensure_postgres_plugin_settings_schema, qname


class PostgresPluginSettingsRepository:
    def __init__(self, database: PostgresDatabase, *, schema: str | None = None):
        self.database = database
        self.schema = schema or database.settings.schema
        ensure_postgres_plugin_settings_schema(database, config=PostgresSchemaConfig(schema=self.schema))

    def list_settings(self, workspace_id: str) -> list[PluginSetting]:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select workspace_id, plugin_id, enabled, mode_bindings_json, config_json,
                           status_metadata_json, created_at, updated_at
                    from {qname(self.schema, 'plugin_setting')}
                    where workspace_id = %s
                    order by plugin_id
                    """,
                    (workspace_id,),
                )
                rows = cur.fetchall()
        return [_row_to_setting(row) for row in rows]

    def get_setting(self, workspace_id: str, plugin_id: str) -> PluginSetting | None:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    select workspace_id, plugin_id, enabled, mode_bindings_json, config_json,
                           status_metadata_json, created_at, updated_at
                    from {qname(self.schema, 'plugin_setting')}
                    where workspace_id = %s and plugin_id = %s
                    """,
                    (workspace_id, plugin_id),
                )
                row = cur.fetchone()
        return _row_to_setting(row) if row else None

    def upsert_setting(
        self,
        workspace_id: str,
        plugin_id: str,
        *,
        enabled: bool,
        mode_bindings: tuple[str, ...],
        config: dict[str, Any],
        status_metadata: dict[str, Any] | None = None,
    ) -> PluginSetting:
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    insert into {qname(self.schema, 'plugin_setting')}(
                        workspace_id, plugin_id, enabled, mode_bindings_json, config_json,
                        status_metadata_json, created_at, updated_at
                    )
                    values (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, now(), now())
                    on conflict(workspace_id, plugin_id) do update set
                        enabled = excluded.enabled,
                        mode_bindings_json = excluded.mode_bindings_json,
                        config_json = excluded.config_json,
                        status_metadata_json = excluded.status_metadata_json,
                        updated_at = now()
                    returning workspace_id, plugin_id, enabled, mode_bindings_json, config_json,
                              status_metadata_json, created_at, updated_at
                    """,
                    (
                        workspace_id,
                        plugin_id,
                        bool(enabled),
                        json.dumps(list(mode_bindings), ensure_ascii=False),
                        json.dumps(config, ensure_ascii=False),
                        json.dumps(status_metadata or {}, ensure_ascii=False),
                    ),
                )
                row = cur.fetchone()
        return _row_to_setting(row)


def _row_to_setting(row: dict[str, Any]) -> PluginSetting:
    return PluginSetting(
        workspace_id=str(row["workspace_id"]),
        plugin_id=str(row["plugin_id"]),
        enabled=bool(row["enabled"]),
        mode_bindings=tuple(_load_json(row.get("mode_bindings_json"), [])),
        config=dict(_load_json(row.get("config_json"), {})),
        status_metadata=dict(_load_json(row.get("status_metadata_json"), {})),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
    )


def _load_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default
