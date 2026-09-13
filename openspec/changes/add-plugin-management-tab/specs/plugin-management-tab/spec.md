## ADDED Requirements

### Requirement: Top-level plugins workspace navigation
The system SHALL provide a top-level Plugins workspace that is reachable from the main sidebar alongside Chat and Knowledge Base.

#### Scenario: Open plugins workspace
- **WHEN** a user clicks the Plugins navigation item
- **THEN** the application navigates to `/plugins` and marks the Plugins item as the active sidebar destination

#### Scenario: Preserve existing navigation
- **WHEN** the Plugins workspace is added
- **THEN** existing Chat and Knowledge Base navigation behavior remains unchanged

### Requirement: Plugin catalog API
The backend SHALL expose a plugin catalog API that returns user-facing plugin records derived from available runtime capabilities, persisted settings, and environment guardrails.

#### Scenario: List plugins
- **WHEN** the frontend requests `GET /plugins`
- **THEN** the response contains plugin records with stable id, name, description, category, enabled state, availability status, configuration status, supported chat modes, safety labels, and update timestamp

#### Scenario: View plugin detail
- **WHEN** the frontend requests `GET /plugins/{plugin_id}` for a known plugin
- **THEN** the response contains the plugin record, editable configuration schema, masked current configuration, mapped runtime tools, and any status warnings

#### Scenario: Unknown plugin
- **WHEN** the frontend requests or updates an unknown plugin id
- **THEN** the backend returns a structured not-found error without changing runtime settings

### Requirement: Plugin management UI
The Plugins workspace SHALL provide an operational catalog UI for browsing, filtering, inspecting, and configuring plugins.

#### Scenario: Browse plugin cards
- **WHEN** the Plugins page loads successfully
- **THEN** the user sees plugin summary metrics, search/filter controls, and plugin cards showing status, category, enabled state, and configuration health

#### Scenario: Filter plugins
- **WHEN** the user filters by category, enabled state, or configuration status
- **THEN** the catalog shows only matching plugins without losing the current search text

#### Scenario: Inspect plugin detail
- **WHEN** the user opens a plugin card
- **THEN** the page shows plugin details including purpose, permissions, mapped tools, supported modes, current status, and configuration controls

### Requirement: Controlled plugin configuration
The system SHALL allow users to update safe plugin settings while preserving backend guardrails.

#### Scenario: Enable plugin
- **WHEN** a user enables a plugin and saves valid settings
- **THEN** the backend persists the enabled state and includes the plugin's mapped tools in eligible runtime policies for subsequent chat requests

#### Scenario: Disable plugin
- **WHEN** a user disables a plugin
- **THEN** the backend persists the disabled state and excludes the plugin's mapped tools from subsequent chat requests

#### Scenario: Reject unsafe or invalid config
- **WHEN** a user submits invalid configuration, non-allowlisted domains, non-read-only database behavior, or unsupported mode bindings
- **THEN** the backend rejects the update with a structured validation error and preserves the previous plugin settings

#### Scenario: Mask secrets
- **WHEN** plugin configuration contains secret fields
- **THEN** read APIs and the frontend display masked values and never expose raw secrets after they are saved

### Requirement: Plugin connection tests
The system SHALL provide bounded, side-effect-safe plugin test actions for configurable plugins.

#### Scenario: Test configured plugin
- **WHEN** a user runs a plugin test for a configured plugin
- **THEN** the backend executes a bounded validation path and returns success, latency, and a sanitized summary of the result

#### Scenario: Test unavailable plugin
- **WHEN** a user tests a plugin that lacks required server configuration or is disabled by deployment guardrails
- **THEN** the backend returns an unavailable status with a user-safe reason

#### Scenario: Test does not mutate knowledge
- **WHEN** any plugin test runs
- **THEN** it does not create, update, delete, ingest, or reindex knowledge-base content

### Requirement: Runtime integration
The system SHALL apply persisted plugin settings to Agent Runtime tool availability for new chat requests.

#### Scenario: Start chat after plugin setting change
- **WHEN** a plugin setting is updated and a new chat turn starts
- **THEN** Agent Runtime uses the current persisted plugin settings when choosing enabled runtime tools for the requested chat mode

#### Scenario: Built-in knowledge tools remain available
- **WHEN** user-facing plugin management is enabled
- **THEN** required built-in knowledge retrieval tools remain available according to existing runtime policy defaults unless explicitly represented as disabled by safe persisted settings

#### Scenario: Internal pipeline stages stay hidden
- **WHEN** the plugin catalog is listed
- **THEN** internal Chat/RAG pipeline stage plugins are not returned as user-manageable plugins

### Requirement: Plugin activity visibility
The system SHALL expose lightweight plugin activity information without leaking private prompts, secrets, or raw tool payloads.

#### Scenario: View plugin activity
- **WHEN** the Plugins page requests activity data
- **THEN** the response contains bounded recent usage counts or an empty activity set when no compatible trace data is available

#### Scenario: Sanitize activity
- **WHEN** activity data includes tool inputs, outputs, errors, or metadata
- **THEN** the backend returns only sanitized summaries and excludes private reasoning, raw prompts, API keys, authorization values, tokens, and secrets

### Requirement: Documentation and validation
The system SHALL document plugin management behavior, configuration boundaries, and validation commands.

#### Scenario: Operator reads plugin documentation
- **WHEN** an operator opens the project documentation
- **THEN** it describes the Plugins workspace, supported built-in plugins, backend routes, environment guardrails, and secret-masking behavior

#### Scenario: Developer validates plugin management
- **WHEN** a developer runs the documented validation commands
- **THEN** they can verify plugin catalog listing, settings update validation, connection testing, frontend catalog rendering, and unchanged Chat/Knowledge Base navigation
