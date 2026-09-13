## ADDED Requirements

### Requirement: Bearer token authentication
The system SHALL authenticate marketplace write operations with a bearer token that maps to an owner, and SHALL support `publish` and `admin` scopes.

#### Scenario: Missing token
- **WHEN** a write operation is requested without an Authorization header
- **THEN** the response is an authentication error and no mutation occurs

#### Scenario: Invalid or revoked token
- **WHEN** a write operation uses an unknown or revoked token
- **THEN** the response is an authentication error and no mutation occurs

#### Scenario: Token identity attribution
- **WHEN** a valid publish-scoped token is used
- **THEN** the operation is attributed to the token's owner and recorded with that attribution

### Requirement: Owner isolation
The system SHALL restrict publish, update, and delete operations to tokens of the owning owner, with an admin scope override for arbitration.

#### Scenario: Foreign owner rejected
- **WHEN** a token of owner A attempts to modify or delete a package owned by owner B
- **THEN** the response is a forbidden error and nothing changes

#### Scenario: Admin override
- **WHEN** an admin-scoped token modifies or deletes any package
- **THEN** the operation is allowed and recorded as an admin action

### Requirement: Package visibility
The system SHALL support public and private package visibility, where private packages are excluded from public catalog and snapshot surfaces and require owner or admin authorization for metadata and download.

#### Scenario: Private package metadata
- **WHEN** an anonymous client requests metadata for a private package
- **THEN** the response is a not-found or authorization error that does not reveal package contents

#### Scenario: Owner accesses private package
- **WHEN** a token of the owning owner requests private package metadata or download
- **THEN** the request succeeds

#### Scenario: Visibility change updates surfaces
- **WHEN** an authorized owner changes a package's visibility
- **THEN** the catalog and snapshot are rebuilt to include or exclude the package accordingly
