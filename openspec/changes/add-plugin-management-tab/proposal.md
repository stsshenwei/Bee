## Why

Operators can configure agent/runtime tools today through environment variables, but the product has no visible place to inspect which plugins are available, enabled, misconfigured, or safe to use in chat. A ChatGPT-like plugin tab gives users a first-class management surface alongside Knowledge Base, making tool capabilities discoverable and governable before agents call them.

## What Changes

- Add a top-level Plugins workspace tab/page next to Chat and Knowledge Base.
- Expose a backend plugin catalog API that summarizes available runtime tools/plugins, status, categories, required configuration, and enabled chat modes.
- Support controlled plugin enable/disable and editable safe configuration for configurable plugins such as web search, web fetch, data analysis, database query, and runtime skills.
- Add plugin detail, search/filter, status badges, and configuration/test affordances in the frontend.
- Keep the existing internal chat/RAG pipeline plugin architecture separate from the user-facing plugin catalog.
- Preserve existing chat, knowledge-base, and agent runtime behavior unless an operator changes plugin settings.

## Capabilities

### New Capabilities

- `plugin-management-tab`: Provides a user-facing Plugins workspace with catalog, status, configuration, testing, and runtime-tool enablement behavior.

### Modified Capabilities

None.

## Impact

- Frontend: add `/plugins` App Router page, sidebar navigation item, plugin catalog/detail/config UI, and API client types/functions.
- Backend: add plugin catalog/config service, API schemas, FastAPI routes, and wiring from persisted plugin settings into Agent Runtime tool registration/policies.
- Persistence/configuration: store plugin enablement and safe configuration in PostgreSQL or another existing application settings boundary rather than relying only on environment variables.
- Docs/tests: add API/development docs, backend service/route tests, frontend API/state tests, and UI smoke coverage for the new tab.
