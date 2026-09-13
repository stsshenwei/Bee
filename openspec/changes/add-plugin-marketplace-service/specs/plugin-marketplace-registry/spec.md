## ADDED Requirements

### Requirement: Plugin package upload
The system SHALL allow an authenticated owner to publish a plugin version by uploading a ZIP bundle containing `.codebuddy-plugin/plugin.json`, and SHALL store the bundle payload, its content hash, its size, and its parsed manifest as an immutable version.

#### Scenario: Successful publish
- **WHEN** an owner uploads a valid plugin ZIP with a new version number
- **THEN** the system persists the version as `published`, returns the version record, and the package's latest version is updated

#### Scenario: Missing plugin manifest
- **WHEN** an uploaded ZIP does not contain a parseable `.codebuddy-plugin/plugin.json`
- **THEN** the system rejects the upload with a structured validation error and persists nothing

#### Scenario: Duplicate version is rejected
- **WHEN** an owner uploads a version number that already exists for the package
- **THEN** the system rejects the upload with a conflict error and the existing version remains unchanged

#### Scenario: Upload size limit
- **WHEN** an uploaded ZIP exceeds the configured maximum bundle size
- **THEN** the system rejects the upload with a structured error before persisting anything

### Requirement: Upload security validation
The system SHALL validate uploaded ZIP archives against path traversal and unsafe entries before persisting them.

#### Scenario: Path traversal rejected
- **WHEN** an archive entry resolves outside the extraction root
- **THEN** the upload is rejected with a structured validation error

#### Scenario: Unsafe entries rejected
- **WHEN** an archive contains symbolic links or absolute-path entries
- **THEN** the upload is rejected with a structured validation error

### Requirement: Global-unique plugin naming
The system SHALL require package names to be globally unique, kebab-case, and equal to the `name` field in the plugin manifest.

#### Scenario: Duplicate package name
- **WHEN** an owner publishes a package whose name already exists under a different owner
- **THEN** the system rejects the publish with a conflict error

#### Scenario: Manifest name mismatch
- **WHEN** the manifest `name` does not match the package name in the publish request
- **THEN** the system rejects the publish with a structured validation error

### Requirement: Dry-run validation
The system SHALL provide a validate endpoint that parses an uploaded ZIP and returns the manifest and detected components without persisting anything.

#### Scenario: Valid bundle preview
- **WHEN** a valid plugin ZIP is submitted to the validate endpoint
- **THEN** the response contains the parsed manifest fields and detected components such as skills, commands, agents, hooks, MCP servers, and LSP configuration

#### Scenario: Invalid bundle preview
- **WHEN** an invalid plugin ZIP is submitted to the validate endpoint
- **THEN** the response contains structured validation errors and no data is persisted

### Requirement: Version deletion semantics
The system SHALL support yank and purge deletion for published versions, where yank hides a version but keeps it recoverable, and purge physically removes it.

#### Scenario: Yank a version
- **WHEN** an authorized owner yanks a version
- **THEN** the version disappears from the catalog and snapshot, its metadata and payload are retained, and the yank is reversible

#### Scenario: Restore a yanked version
- **WHEN** an authorized owner restores a yanked version
- **THEN** the version becomes listed again with its original payload

#### Scenario: Purge a version
- **WHEN** an authorized owner purges a version with explicit confirmation
- **THEN** the version metadata and payload are physically removed and the marketplace snapshot is rebuilt

### Requirement: Publish concurrency control
The system SHALL serialize publishes for the same package and SHALL keep serving the previous marketplace snapshot when a rebuild fails.

#### Scenario: Concurrent uploads to one package
- **WHEN** two uploads for the same package are processed at the same time
- **THEN** they are serialized so both resulting versions are consistent and neither corrupts the other

#### Scenario: Snapshot rebuild failure
- **WHEN** a snapshot rebuild fails after a publish or deletion
- **THEN** the previously published snapshot continues to be served and stored metadata stays consistent with the published versions
