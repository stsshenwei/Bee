## MODIFIED Requirements

### Requirement: Plugin management UI
The Plugins workspace SHALL provide a plugin marketplace admin console for browsing, publishing, and managing marketplace packages, and SHALL no longer render the built-in capability catalog console; built-in capability management remains available through the unchanged backend `/plugins` API.

#### Scenario: Browse package cards
- **WHEN** the Plugins page loads successfully
- **THEN** the user sees marketplace package cards showing name, owner, latest version, visibility, and component badges, plus search and filter controls

#### Scenario: Filter packages
- **WHEN** the user filters by owner, visibility, or component type
- **THEN** only matching packages are shown without losing the current search text

#### Scenario: Inspect package detail
- **WHEN** the user opens a package
- **THEN** the page shows its versions with publish state, size, content hash, timestamps, and management actions for upload, yank, restore, purge, and visibility

#### Scenario: Built-in catalog console replaced
- **WHEN** the Plugins workspace renders
- **THEN** the built-in capability catalog console is not shown, while the backend `/plugins` catalog API and Agent Runtime wiring continue to operate unchanged
