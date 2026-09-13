# Plugins

The built-in plugin system exposes operator-managed runtime capabilities for chat: a user-facing catalog over existing Agent Runtime tools. It is not the internal Chat/RAG pipeline plugin system and not a distribution channel.

For the self-hosted plugin marketplace (plugin packages, upload/download/delete, and WorkBuddy/CodeBuddy 套件源 integration), see [MARKETPLACE.md](MARKETPLACE.md). The `/plugins` frontend workspace now hosts the marketplace admin console; the built-in catalog API below remains unchanged and available.

## Built-In Plugins

| Plugin | Runtime tools | Notes |
|---|---|---|
| Knowledge Retrieval | `knowledge_search`, `grep_chunks`, `list_knowledge_chunks`, `get_document_info`, `query_knowledge_graph` | Read-only, scoped to selected knowledge bases. |
| Wiki Tools | `wiki_search`, `wiki_read_page`, `wiki_read_source_doc`, `wiki_flag_issue` | Read-mostly Wiki access; write/maintenance tools stay controlled by runtime policy. |
| Web Search | `web_search` | Uses a configured HTTP JSON endpoint. Tests validate configuration by default and only execute a provider request when explicitly requested. |
| Web Fetch | `web_fetch` | Requires server allowlisted domains. UI settings cannot add domains outside that allowlist. |
| Data Analysis | `data_analysis` | Bounded analysis of inline JSON records. |
| Database Query | `database_query` | Read-only access to server allowlisted SQLite sources. UI settings can select source names, not replace server paths. |
| Runtime Skills | `read_skill` | Read-only skill instruction access. |
| Skill Execution | `execute_skill` | Listed as unavailable until a secure execution sandbox exists. |

## API

- `GET /plugins`: returns catalog records and aggregate counts.
- `GET /plugins/{plugin_id}`: returns a plugin detail record or a structured 404.
- `PATCH /plugins/{plugin_id}`: updates `enabled`, `enabled_modes`, and safe editable `config`.
- `POST /plugins/{plugin_id}/test`: runs a bounded, side-effect-safe validation and returns status, latency, and sanitized details.
- `GET /plugins/activity?limit=20`: returns bounded recent activity or an empty fallback when trace activity is unavailable.

`PATCH` validates the full proposed setting before persistence. Invalid endpoints, unsupported mode bindings, domains outside the server allowlist, and unknown database source names are rejected without replacing the previous setting.

## Environment Defaults And Guardrails

Environment variables remain deployment defaults and hard safety limits. Persisted UI settings can enable, disable, or narrow plugins, but they cannot bypass unavailable runtime features or server allowlists.

Relevant Agent Runtime env values include:

- `AGENT_RUNTIME_ENABLED`
- `AGENT_RUNTIME_ENABLED_TOOLS`
- `AGENT_RUNTIME_QUICK_ENABLED_TOOLS`
- `AGENT_RUNTIME_WIKI_ENABLED_TOOLS`
- `AGENT_RUNTIME_RAG_WIKI_ENABLED_TOOLS`
- `AGENT_RUNTIME_WEB_SEARCH_ENABLED`
- `AGENT_RUNTIME_WEB_SEARCH_URL`
- `AGENT_RUNTIME_WEB_FETCH_ENABLED`
- `AGENT_RUNTIME_WEB_FETCH_ALLOWED_DOMAINS`
- `AGENT_RUNTIME_DATA_ANALYSIS_ENABLED`
- `AGENT_RUNTIME_DATABASE_QUERY_ENABLED`
- `AGENT_RUNTIME_DATABASE_ALLOWED_SOURCES`
- `AGENT_RUNTIME_SKILLS_ENABLED`
- `AGENT_RUNTIME_SKILLS_PATH`

Persisted settings are workspace-scoped in PostgreSQL table `plugin_setting`. Changes are applied when the API refreshes the Agent Runtime registry after a plugin update, and they affect new chat turns. Existing in-flight chat streams keep their current runtime state.

## Validation

Backend:

```powershell
cd backend
python -m pytest tests/test_plugin_management.py tests/test_rag_api_routes.py tests/test_runtime_config.py
```

Frontend:

```powershell
cd frontend
node --test app/lib/api.test.mjs app/lib/responsive-css.test.mjs
npm run build
```
