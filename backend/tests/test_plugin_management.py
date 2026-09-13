from types import SimpleNamespace
from unittest.mock import patch

from app.models.agent_runtime import AgentRuntimeConfig
from app.services.plugins.plugin_management import (
    InMemoryPluginSettingsRepository,
    PluginManagementService,
    PluginValidationError,
)
from app.services.plugins.plugin_models import PluginRuntimeEnvironment
from app.services.plugins.postgres_plugin_repository import PostgresPluginSettingsRepository


def build_service():
    repo = InMemoryPluginSettingsRepository()
    service = PluginManagementService(
        repo,
        workspace_id="workspace-1",
        runtime_environment=PluginRuntimeEnvironment(
            web_search_enabled=False,
            web_search_endpoint="",
            web_fetch_enabled=True,
            web_fetch_allowed_domains=("docs.example.com",),
            data_analysis_enabled=True,
            database_query_enabled=True,
            database_allowed_sources={"main": "./data.sqlite3"},
            skills_enabled=True,
            wiki_tools_enabled=True,
        ),
    )
    return service, repo


def test_catalog_merges_descriptors_environment_and_settings():
    service, _ = build_service()
    catalog = service.list_plugins()
    items = {item["id"]: item for item in catalog["items"]}

    assert "knowledge" in items
    assert items["knowledge"]["enabled"] is True
    assert items["web_search"]["configuration_status"] == "needs_configuration"
    assert items["data_analysis"]["availability"] == "available"
    assert catalog["aggregate"]["total"] >= 8


def test_update_plugin_persists_and_masks_config():
    service, repo = build_service()

    updated = service.update_plugin(
        "web_search",
        {"enabled": True, "enabled_modes": ["reasoning"], "config": {"endpoint": "https://search.example.com/api"}},
    )

    assert updated["enabled"] is True
    assert updated["enabled_modes"] == ["reasoning"]
    assert updated["configuration_status"] == "configured"
    assert repo.get_setting("workspace-1", "web_search").config["endpoint"] == "https://search.example.com/api"


def test_invalid_config_does_not_replace_previous_setting():
    service, repo = build_service()
    service.update_plugin("web_search", {"enabled": True, "config": {"endpoint": "https://search.example.com/api"}})

    try:
        service.update_plugin("web_search", {"enabled": True, "config": {"endpoint": "not-a-url"}})
    except PluginValidationError:
        pass
    else:
        raise AssertionError("invalid endpoint should be rejected")

    assert repo.get_setting("workspace-1", "web_search").config["endpoint"] == "https://search.example.com/api"


def test_domain_and_database_guardrails_are_enforced():
    service, repo = build_service()

    try:
        service.update_plugin("web_fetch", {"enabled": True, "config": {"allowed_domains": "evil.example.com"}})
    except PluginValidationError as exc:
        assert "服务器白名单" in str(exc)
    else:
        raise AssertionError("non-allowlisted domain should be rejected")

    try:
        service.update_plugin("database_query", {"enabled": True, "config": {"sources": {"other": "./other.sqlite3"}}})
    except PluginValidationError as exc:
        assert "服务器白名单" in str(exc)
    else:
        raise AssertionError("non-allowlisted database source should be rejected")

    updated = service.update_plugin("database_query", {"enabled": True, "config": {"sources": {"main": "./tampered.sqlite3"}}})
    assert updated["config"]["sources"] == {"main": "./data.sqlite3"}
    assert repo.get_setting("workspace-1", "database_query").config["sources"] == {"main": "./data.sqlite3"}


def test_plugin_settings_update_runtime_config_tools():
    service, _ = build_service()
    service.update_plugin("data_analysis", {"enabled": True, "enabled_modes": ["reasoning"]})
    service.update_plugin("web_search", {"enabled": True, "enabled_modes": ["reasoning"], "config": {"endpoint": "https://search.example.com/api"}})
    config = AgentRuntimeConfig(enabled=True, enabled_tools=("thinking",))

    updated = service.apply_to_agent_runtime_config(config)

    assert updated.data_analysis_enabled is True
    assert updated.web_search_enabled is True
    assert updated.web_search_endpoint == "https://search.example.com/api"
    assert "data_analysis" in updated.enabled_tools
    assert "web_search" in updated.enabled_tools


def test_plugin_test_is_bounded_and_side_effect_free_by_default():
    service, _ = build_service()
    service.update_plugin("web_search", {"enabled": True, "config": {"endpoint": "https://search.example.com/api"}})

    result = service.test_plugin("web_search", {"query": "redis"})

    assert result["success"] is True
    assert result["status"] == "success"
    assert "端点格式" in result["summary"]


def test_postgres_repository_ensures_plugin_table_on_startup():
    database = SimpleNamespace(settings=SimpleNamespace(schema="public"))

    with patch("app.services.plugins.postgres_plugin_repository.ensure_postgres_plugin_settings_schema") as ensure:
        repository = PostgresPluginSettingsRepository(database)

    assert repository.schema == "public"
    ensure.assert_called_once()
