## 1. Backend Plugin Domain

- [x] 1.1 Define plugin catalog models for plugin id, display metadata, category, safety labels, mapped runtime tools, supported modes, config schema, and status.
- [x] 1.2 Add plugin setting persistence keyed by workspace id and plugin id, including enabled state, mode bindings, safe config JSON, status metadata, and timestamps.
- [x] 1.3 Add storage initialization/migration coverage for the plugin settings table without breaking existing PostgreSQL startup checks.
- [x] 1.4 Implement a plugin management service that merges static descriptors, environment guardrails, persisted settings, and runtime availability.
- [x] 1.5 Add validation helpers for editable plugin config, supported mode bindings, masked secret fields, allowlisted domains, and read-only database source constraints.

## 2. Backend API And Runtime Wiring

- [x] 2.1 Add schemas for plugin list/detail/update/test/activity responses and requests.
- [x] 2.2 Add `GET /plugins` and `GET /plugins/{plugin_id}` routes with structured not-found errors.
- [x] 2.3 Add `PATCH /plugins/{plugin_id}` with atomic validation and preservation of previous settings on failure.
- [x] 2.4 Add `POST /plugins/{plugin_id}/test` with bounded side-effect-safe test execution and sanitized output.
- [x] 2.5 Add `GET /plugins/activity` with bounded recent activity or empty fallback when trace data is unavailable.
- [x] 2.6 Wire persisted plugin settings into Agent Runtime tool registration and chat runtime policy selection for new chat requests.
- [x] 2.7 Preserve environment variables as deployment defaults and hard guardrails that UI settings cannot bypass.

## 3. Frontend Plugin Workspace

- [x] 3.1 Add plugin types and API client functions in `frontend/app/lib`.
- [x] 3.2 Add a Plugins item to the main sidebar with active state for `/plugins`.
- [x] 3.3 Create the `/plugins` App Router page with catalog metrics, search, filters, loading, empty, and error states.
- [x] 3.4 Implement plugin cards showing category, enabled state, availability, configuration health, safety labels, and last updated time.
- [x] 3.5 Implement plugin detail/config panel with mapped tools, supported modes, permission copy, enable toggle, editable safe config, masked secrets, save/cancel, and validation errors.
- [x] 3.6 Implement plugin test action feedback with success, unavailable, failed, latency, and sanitized summary states.
- [x] 3.7 Add responsive CSS consistent with the existing knowledge workspace without nesting cards or creating layout overlap.

## 4. Tests

- [x] 4.1 Add backend service tests for catalog merging, setting persistence, guardrail enforcement, config validation, and secret masking.
- [x] 4.2 Add backend route tests for list, detail, update, invalid update, test action, activity fallback, and unknown plugin ids.
- [x] 4.3 Add runtime integration tests proving updated plugin settings affect new Agent Runtime tool availability without changing existing defaults unexpectedly.
- [x] 4.4 Add frontend API/state tests for plugin list/detail/update/test handling.
- [x] 4.5 Add frontend UI tests or static checks for sidebar navigation, catalog filters, detail panel states, and responsive layout safeguards.
- [x] 4.6 Run regression tests for chat stream and knowledge-base navigation after adding the Plugins tab.

## 5. Documentation And Validation

- [x] 5.1 Document the Plugins workspace, supported built-in plugins, backend routes, and operational guardrails.
- [x] 5.2 Document how environment defaults interact with persisted UI settings.
- [x] 5.3 Document validation commands for backend plugin APIs and frontend plugin page checks.
- [x] 5.4 Update architecture/development docs to include the plugin management surface and runtime integration boundary.
- [x] 5.5 Run the relevant backend and frontend validation commands and record any known limitations.
