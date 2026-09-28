# Plugins

The built-in plugin system exposes operator-managed runtime capabilities for chat: a user-facing catalog over existing Agent Runtime tools. It is not the internal Chat/RAG pipeline plugin system and not a distribution channel.

For the self-hosted plugin marketplace (plugin packages, upload/download/delete, and WorkBuddy/CodeBuddy 套件源 integration), see [MARKETPLACE.md](MARKETPLACE.md). The `/plugins` frontend workspace now hosts the marketplace admin console; the built-in catalog API below remains unchanged and available.

## Built-In Plugins

| Plugin | Runtime tools | Notes |
|---|---|---|
| Knowledge Retrieval | `knowledge_search`, `grep_chunks`, `list_knowledge_chunks`, `get_document_info`, `query_knowledge_graph` | Read-only, scoped to selected knowledge bases. |
| Wiki Tools | `wiki_search`, `wiki_read_page`, `wiki_read_source_doc`, `wiki_flag_issue` | Read-mostly Wiki access; write/maintenance tools stay controlled by runtime policy. |
| Web Search | `web_search` | Uses either a configured HTTP JSON endpoint or Tavily via `TAVILY_API_KEY`. Can be bound to `quick`, `reasoning`, `wiki`, and `rag_wiki` for insufficient-evidence fallback. Tests validate configuration by default and only execute a provider request when explicitly requested. |
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
- `TAVILY_API_KEY`
- `TAVILY_SEARCH_URL`
- `AGENT_RUNTIME_WEB_FETCH_ENABLED`
- `AGENT_RUNTIME_WEB_FETCH_ALLOWED_DOMAINS`
- `AGENT_RUNTIME_DATA_ANALYSIS_ENABLED`
- `AGENT_RUNTIME_DATABASE_QUERY_ENABLED`
- `AGENT_RUNTIME_DATABASE_ALLOWED_SOURCES`
- `AGENT_RUNTIME_SKILLS_ENABLED`
- `AGENT_RUNTIME_SKILLS_PATH`

Persisted settings are workspace-scoped in PostgreSQL table `plugin_setting`. Changes are applied when the API refreshes the Agent Runtime registry after a plugin update, and they affect new chat turns. Existing in-flight chat streams keep their current runtime state.

## Web Search Fallback

When internal evidence is insufficient, `/chat/stream` can use the configured `web_search` provider as a fallback for every chat mode. Quick chat calls the shared fallback service before source emission. Agent Runtime modes can expose `web_search` through plugin mode bindings and also have a deterministic terminal fallback if the runtime ends without usable internal evidence.

Fallback availability follows server guardrails. `WEB_SEARCH_FALLBACK_ENABLED` and `WEB_SEARCH_FALLBACK_URL` can be set explicitly; otherwise they inherit `AGENT_RUNTIME_WEB_SEARCH_ENABLED` and `AGENT_RUNTIME_WEB_SEARCH_URL`. If no HTTP JSON search URL is configured, `TAVILY_API_KEY` enables Tavily as the provider; `TAVILY_SEARCH_URL` can override the default `https://api.tavily.com/search`. `WEB_SEARCH_FALLBACK_TOP_K`, `WEB_SEARCH_FALLBACK_TIMEOUT_SECONDS`, and `WEB_SEARCH_FALLBACK_MIN_CONFIDENCE` bound result count, timeout, and evidence threshold.

Minimal Tavily-backed configuration:

```env
AGENT_RUNTIME_WEB_SEARCH_ENABLED=true
WEB_SEARCH_FALLBACK_ENABLED=true
TAVILY_API_KEY=tvly-...
```

Successful fallback answers begin with `知识库无答案，以下来自网络搜索` and emit web source records with `source_type: "web"`. If web search is disabled, unconfigured, fails, or returns no usable results, the backend returns a safe insufficient-evidence answer and records sanitized fallback metadata without exposing credentials or provider stack traces.

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
