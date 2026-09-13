## ADDED Requirements

### Requirement: Marketplace catalog endpoint
The system SHALL serve a CodeBuddy-compatible `marketplace.json` catalog that lists public, non-yanked packages with relative-path plugin entries.

#### Scenario: Catalog lists public packages
- **WHEN** a client requests the marketplace catalog
- **THEN** the response contains the marketplace name, owner metadata, and one entry per public published package with name, relative source path, description, latest version, author, category, and keywords

#### Scenario: Private packages are excluded
- **WHEN** a private package exists
- **THEN** it does not appear in the public catalog

#### Scenario: Yanked versions are excluded
- **WHEN** the latest version of a package is yanked
- **THEN** the catalog entry reflects the newest non-yanked published version, or the package is omitted if none remains

### Requirement: Whole-marketplace snapshot
The system SHALL publish a snapshot ZIP containing the marketplace catalog file and the complete directory tree of every public plugin, using relative-path plugin entries.

#### Scenario: Snapshot contains plugin payloads
- **WHEN** a client downloads the snapshot ZIP
- **THEN** it contains `.codebuddy-plugin/marketplace.json` and the full file tree of each listed plugin, including skills, commands, agents, hooks, and MCP configuration files

#### Scenario: Snapshot matches catalog
- **WHEN** a snapshot has revision `R`
- **THEN** the catalog file inside the snapshot is the same catalog content that produced revision `R`

### Requirement: Snapshot freshness
The system SHALL expose snapshot freshness information through a version endpoint and through ETag and Last-Modified headers on the snapshot download.

#### Scenario: Revision endpoint
- **WHEN** a client requests the snapshot version endpoint
- **THEN** the response contains the current revision identifier and build timestamp

#### Scenario: Revision changes on publish
- **WHEN** a package is published, yanked, or purged
- **THEN** the snapshot revision changes accordingly

#### Scenario: No-change rebuild is a no-op
- **WHEN** a rebuild produces catalog content identical to the current snapshot
- **THEN** the revision and snapshot bytes stay unchanged

### Requirement: Per-package download
The system SHALL serve each published version's bundle ZIP for download while respecting visibility rules.

#### Scenario: Public download
- **WHEN** anyone requests the download URL of a published version of a public package
- **THEN** the response streams the bundle ZIP with an appropriate content type and filename

#### Scenario: Private download requires authorization
- **WHEN** a requester without a token of the owning owner requests a private package download
- **THEN** the response is an authorization error that does not reveal the payload

#### Scenario: Unknown version
- **WHEN** a client requests a package or version that does not exist
- **THEN** the response is a structured not-found error

### Requirement: Atomic snapshot swap
The system SHALL build snapshots in a staging area and swap them atomically so readers never observe a partial snapshot.

#### Scenario: Readers never see partial snapshots
- **WHEN** a snapshot rebuild completes
- **THEN** subsequent downloads resolve entirely to the new snapshot while in-flight downloads of the previous snapshot are allowed to finish
