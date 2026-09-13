## Context

Bee currently exposes Chat and Knowledge Base workspaces in the Next.js shell. The runtime already has a backend `ToolRegistry` and a set of built-in agent tools, including knowledge search, Wiki tools, web search/fetch, data analysis, database query, and skills. Those tools are mostly controlled through environment variables and are not discoverable or manageable in the UI.

There is also an internal Chat/RAG pipeline plugin concept under `backend/app/services/chat_pipeline/`. That is an implementation extension point for request orchestration, not a user-facing plugin marketplace. The Plugins tab in this change should present operator-visible tool capabilities that can be enabled, configured, tested, and surfaced to chat runtime policies.

## Goals / Non-Goals

**Goals:**

- Add a top-level Plugins page that feels like a peer of Knowledge Base.
- Present available plugins/tools with clear names, descriptions, categories, enabled state, configuration status, and safety labels.
- Persist safe operator settings for plugin enablement and configuration.
- Wire persisted plugin settings into Agent Runtime tool availability without breaking existing environment-variable defaults.
- Provide connection/test feedback for configurable plugins before users rely on them in chat.
- Keep plugin page UI dense, operational, and consistent with the existing workspace shell.

**Non-Goals:**

- Do not implement third-party plugin installation, package download, OAuth marketplaces, or remote plugin discovery in this change.
- Do not expose internal Chat/RAG pipeline stages as user-managed plugins.
- Do not allow arbitrary code execution or broad filesystem access from the UI.
- Do not make unsafe write/database operations available through plugin configuration.
- Do not redesign the chat page or knowledge-base page beyond adding navigation/linkage.

## Decisions

### Decision: Model plugins as user-facing runtime capabilities

Create a plugin catalog layer that maps stable plugin IDs to existing runtime tools and capability groups. Examples:

- `knowledge`: built-in knowledge search and chunk reading tools;
- `wiki`: Wiki search/read/issue tools;
- `web_search`: configured HTTP JSON search provider;
- `web_fetch`: allowlisted web page fetch;
- `data_analysis`: bounded inline JSON analysis;
- `database_query`: read-only allowlisted SQLite data sources;
- `skills`: read-only runtime skills;
- `execute_skill`: visible as unavailable until a secure sandbox exists.

Rationale: users think in terms of plugins/capabilities, while the model runtime thinks in terms of individual tool functions. A catalog layer keeps the UI understandable without renaming low-level tools.

Alternatives considered:

- Show every runtime tool one-to-one. This is precise but noisy and exposes internal details such as `grep_chunks` and `list_knowledge_chunks` without context.
- Build a true third-party plugin framework first. That is larger and would delay basic visibility/configuration for tools that already exist.

### Decision: Persist plugin settings in backend storage

Add a backend plugin settings repository/service with records keyed by `workspace_id` and `plugin_id`. Each record should store enabled state, mode bindings, safe config JSON, status metadata, and timestamps.

Rationale: environment variables are startup configuration, not an interactive UI persistence model. Persisted settings let the plugin page reflect operator changes and allow runtime policy assembly to use a single backend source of truth.

Alternatives considered:

- Write `.env` files from the UI. This is unsafe, deployment-specific, and will not work in containerized production.
- Store settings only in browser local storage. That would not affect backend runtime behavior and would be inconsistent across users.

### Decision: Environment values remain defaults and guardrails

Startup environment variables should seed defaults and hard limits. Persisted plugin settings can enable/disable configured plugins, but they must not bypass required provider configuration, allowed-domain lists, read-only database restrictions, or disabled secure-execution boundaries.

Rationale: operators may deploy with locked-down server settings. The UI should make those limits visible rather than silently overriding them.

Alternatives considered:

- Let UI settings fully override environment configuration. This would be convenient but weakens production safety.
- Keep all configuration env-only and make the UI read-only. This solves discoverability but not management.

### Decision: Add a small plugin API surface

Expose:

```text
GET   /plugins
GET   /plugins/{plugin_id}
PATCH /plugins/{plugin_id}
POST  /plugins/{plugin_id}/test
GET   /plugins/activity
```

`/plugins` returns catalog cards and aggregate counts. `PATCH` updates enabled state, mode bindings, and editable config for one plugin. `test` runs a bounded, side-effect-safe validation. `activity` can initially return recent agent tool-call summary from existing traces where available, with empty fallback when unavailable.

Rationale: this mirrors the frontend management needs while keeping mutations narrow.

Alternatives considered:

- Add plugin settings to `/health`. Health is operational and should not become a configuration API.
- Reuse Agent Runtime internals directly from frontend. The frontend should not need to understand Python registry classes or env parsing rules.

### Decision: Keep the first UI as an operational catalog

The `/plugins` page should use the same app shell and restrained management style as `/knowledge`: header metrics, search/filter controls, catalog cards, detail panel/drawer, configuration form, enable toggle, and test action. It should avoid a marketing-style plugin store until real external marketplace support exists.

Rationale: this product is an internal RAG workspace. Users need scanning, comparison, and repeated configuration rather than promotional plugin cards.

Alternatives considered:

- Copy a consumer app-store layout. It looks familiar but can obscure status, permissions, and runtime mode details.
- Add plugin controls inside the chat composer first. That is useful later, but it lacks room for configuration and testing.

## Risks / Trade-offs

- [Risk] Runtime settings changed through UI may not affect already-running processes. -> Mitigation: centralize plugin setting reads in runtime policy/tool registry construction and document whether changes require a new chat turn or backend restart.
- [Risk] Plugin config could expose secrets in API responses. -> Mitigation: mark secret fields write-only or masked, sanitize responses, and avoid returning raw tokens or credentials.
- [Risk] Database query plugin can become dangerous. -> Mitigation: preserve read-only query validation, explicit allowlisted sources, row limits, and disabled-by-default behavior.
- [Risk] Users may confuse internal pipeline plugins with user-facing plugins. -> Mitigation: name UI concepts "Plugins" or "Tools" and keep internal stage plugins out of the catalog.
- [Risk] Schema changes can trigger storage compatibility concerns. -> Mitigation: add migration/initialization logic for the plugin settings table and targeted tests for existing storage startup.

## Migration Plan

1. Add plugin models, schemas, service, repository, and storage initialization for persisted plugin settings.
2. Build a catalog from static plugin descriptors plus environment/runtime availability plus persisted overrides.
3. Add `/plugins` API routes for list, detail, update, test, and activity.
4. Wire plugin settings into Agent Runtime tool registration and chat mode policy selection for new requests.
5. Add the `/plugins` frontend page, sidebar item, API client functions, and types.
6. Add backend and frontend tests, then update docs.

Rollback is straightforward: hide the sidebar entry and stop reading persisted plugin settings; existing environment-driven runtime tool behavior remains available.

## Open Questions

- Should plugin settings be workspace-scoped only, or should future tenant/user-level overrides be planned now?
- Should chat mode bindings support all modes in the first UI, or only reasoning/wiki/rag_wiki modes where tools are already most visible?
- Should plugin activity be sourced from runtime spans immediately, or shipped as a placeholder until span data is normalized for UI reporting?
