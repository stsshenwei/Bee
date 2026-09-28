## ADDED Requirements

### Requirement: Standalone skill validation
The system SHALL accept a UTF-8 SKILL.md or a ZIP containing one skill root, parse name and description using safe YAML, and return metadata and a file index before publication. It MUST reject missing required fields, ambiguous skill roots, unsafe paths, symlinks, normalized duplicate paths, and configured size/count limit violations without publishing or executing content.

#### Scenario: Preview standalone upload
- **WHEN** an authorized publisher uploads a valid SKILL.md without plugin.json
- **THEN** validation returns name, description and file information without creating a published skill

#### Scenario: Reject unsafe archive
- **WHEN** a ZIP contains ../ paths, symlinks, duplicate normalized entries or exceeds decompression limits
- **THEN** validation and publication reject it and create no visible version

### Requirement: Authorized immutable publication
The system SHALL reuse publish/admin token identity, keep declared author separate from owner, and publish immutable versions uniquely identified by skill and version. It MUST revalidate submitted bytes on publication, enforce owner authorization, and make metadata and a complete stored bundle visible together.

#### Scenario: Publish and update
- **WHEN** a publisher submits a valid new version of their skill
- **THEN** the version becomes visible with its hash, complete bundle and version-specific metadata while previous versions remain unchanged

#### Scenario: Conflicting or unauthorized publication
- **WHEN** a publisher reuses an existing version or attempts to change another owner's skill
- **THEN** the service returns 409 or 403 respectively and preserves existing content

#### Scenario: Storage failure
- **WHEN** storing the bundle or committing publication fails
- **THEN** no incomplete version appears and temporary objects are cleaned up or eligible for orphan recovery

### Requirement: Catalog detail and complete download
The system SHALL provide paginated search/filter/sort, version-specific details, bounded safe file previews, and complete ZIP downloads for published skills. Single-file uploads SHALL also download as ZIP. Supplemental assets MUST retain relative paths and SKILL.md content MUST remain unchanged.

#### Scenario: Download companion files
- **WHEN** a published skill includes scripts, references and assets and a visitor downloads it
- **THEN** the archive contains the complete skill directory with matching recorded content and no server-side execution occurs

#### Scenario: Browse a version
- **WHEN** a visitor selects a published version and opens its SKILL.md or a text companion
- **THEN** the service returns that version's metadata and bounded text without permitting access outside its bundle

### Requirement: Version withdrawal and restoration
The system SHALL allow only the owner or admin to withdraw and restore versions. Withdrawn versions MUST be unavailable for new downloads and new chat resolution; metadata remains for management. No published version SHALL be overwritten or physically deleted through this first-version UI.

#### Scenario: Withdraw an enabled version
- **WHEN** its owner withdraws a version enabled in a workspace
- **THEN** new chat requests cannot use it, the UI reports it unavailable, and already-started requests retain their resolved snapshots

#### Scenario: Restore a version
- **WHEN** the owner restores a withdrawn version
- **THEN** the same immutable bytes become downloadable and eligible for activation again
