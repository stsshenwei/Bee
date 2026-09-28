## ADDED Requirements

### Requirement: Skill card catalog
The frontend SHALL provide a skill library navigation item beside plugins, searchable and filterable cards, pagination and an upload button. Cards SHALL show name, description, author, category, version and update time, and open a dedicated detail page while preserving list context on return.

#### Scenario: Browse and return
- **WHEN** a visitor filters skills, opens a card and returns
- **THEN** the previous filters and list position remain available

### Requirement: Skill detail reading and download
The detail page SHALL display metadata, formatted SKILL.md, raw source, a file explorer, versions, a complete ZIP download action and workspace activation status. Markdown MUST disable raw HTML and dangerous URL protocols; non-text files SHALL show metadata and download rather than execute.

#### Scenario: Read and download a skill
- **WHEN** a visitor opens a skill and selects a version
- **THEN** the description, file previews and download all refer to that version

### Requirement: Upload review and management
The frontend SHALL support select-file, validation-preview, metadata-completion and explicit publish steps with progress and actionable errors. It SHALL provide authorized version publication, withdrawal and restoration controls, and distinguish publisher identity from declared author.

#### Scenario: Correct a validation error
- **WHEN** an upload lacks a required skill field or contains an invalid archive
- **THEN** the dialog explains the failure, preserves editable fields and allows another file without creating a card

#### Scenario: Successful publication
- **WHEN** an authorized user publishes a validated skill with complete metadata
- **THEN** the library refreshes and provides access to its new detail page

### Requirement: Accessible responsive states
The library SHALL follow Bee's current tokens and card conventions, support keyboard operation, labeled controls, visible focus, loading/error/empty states and 320px-wide layouts without page-level horizontal overflow. Unknown author/source/license fields MUST be shown as missing rather than fabricated.

#### Scenario: Empty library on mobile
- **WHEN** a visitor opens an empty library on a narrow viewport
- **THEN** the page provides a readable upload entry and all controls remain reachable without overlap
