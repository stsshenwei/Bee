## ADDED Requirements

### Requirement: Marketplace console navigation
The Plugins workspace SHALL present the plugin marketplace admin console at `/plugins`.

#### Scenario: Open marketplace console
- **WHEN** a user opens the Plugins workspace
- **THEN** the page shows the marketplace package list with loading, empty, and error states

#### Scenario: Refresh packages
- **WHEN** the user triggers a refresh
- **THEN** the console reloads package data and preserves the current search and filter context

### Requirement: Package browsing
The console SHALL let users browse and filter published packages with component and status information.

#### Scenario: Browse package cards
- **WHEN** the package list loads
- **THEN** each card shows name, owner, latest version, visibility, size, and detected component badges such as skills, commands, agents, hooks, and MCP

#### Scenario: Filter packages
- **WHEN** the user filters by owner, visibility, or component type
- **THEN** only matching packages are shown without losing the current search text

### Requirement: Upload flow with validation preview
The console SHALL let users upload a plugin ZIP through a validate-then-publish flow with clear feedback.

#### Scenario: Preview before publish
- **WHEN** the user selects a ZIP file
- **THEN** the console shows the parsed manifest and detected components before publishing

#### Scenario: Publish feedback
- **WHEN** the user confirms publishing
- **THEN** the console reports success with the new version, or a structured error message without discarding the selected file

#### Scenario: Publish requires credentials
- **WHEN** no publish token is configured for the session
- **THEN** the console prompts for the token before enabling upload actions

### Requirement: Version and delete management
The console SHALL let authorized users manage a package's versions, including yank, restore, and purge with confirmation.

#### Scenario: Yank and restore
- **WHEN** an authorized user yanks a version and then restores it
- **THEN** the version list reflects both state changes

#### Scenario: Purge confirmation
- **WHEN** an authorized user purges a version
- **THEN** the console requires explicit confirmation before issuing the request

### Requirement: Visibility settings
The console SHALL let authorized users change package visibility and reflect the change in the package list.

#### Scenario: Toggle visibility
- **WHEN** an authorized user toggles a package between public and private
- **THEN** the package list and detail views show the new visibility after saving
