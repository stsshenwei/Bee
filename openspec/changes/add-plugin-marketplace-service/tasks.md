## 1. Marketplace Domain Model And Storage

- [x] 1.1 Create the `backend/app/services/marketplace/` module with domain models for owner, token, package, package version, and snapshot records
- [x] 1.2 Add Postgres repositories for owners, tokens, packages, versions, and snapshots following the existing repository patterns in `backend/app/services/plugins/postgres_plugin_repository.py`
- [x] 1.3 Extend `backend/app/services/storage/postgres_schema.py` with marketplace tables (`marketplace_owner`, `marketplace_token`, `marketplace_package`, `marketplace_package_version`, `marketplace_snapshot`) including unique-name and lookup indexes
- [x] 1.4 Define the on-disk storage layout under a configurable `MARKETPLACE_STORAGE_DIR`: per-version bundle paths, staging directory, and snapshot output paths
- [x] 1.5 Add environment configuration (`MARKETPLACE_STORAGE_DIR`, `MARKETPLACE_MAX_UPLOAD_BYTES`, `MARKETPLACE_MARKETPLACE_NAME`, bootstrap admin token) to backend env loading

## 2. Identity And Access Control

- [x] 2.1 Implement token hashing/verification and a bearer-token dependency that resolves the current owner and scopes (`publish`, `admin`)
- [x] 2.2 Implement owner isolation and admin-override authorization helpers for publish, update, and delete operations
- [x] 2.3 Implement visibility enforcement for metadata and downloads (public anonymous access; private requires owner or admin token)
- [x] 2.4 Provision the bootstrap admin token from environment configuration at startup without ever returning it from API responses

## 3. Upload Pipeline And Validation

- [x] 3.1 Implement safe ZIP inspection: entry path normalization, zip-slip detection, symlink and absolute-path rejection, size and entry-count limits
- [x] 3.2 Implement manifest parsing and validation for `.codebuddy-plugin/plugin.json` (required fields, kebab-case name, SemVer version) with structured error reporting
- [x] 3.3 Implement component detection from the bundle tree (skills, commands, agents, hooks, MCP servers, LSP config)
- [x] 3.4 Implement the dry-run validate service that returns manifest and detected components without persisting
- [x] 3.5 Implement publish: stage bundle, compute SHA-256, enforce global-unique name and version immutability (409 on conflicts), persist the version under a per-package publish lock
- [x] 3.6 Implement yank, restore, and purge with confirmation semantics, payload handling, and snapshot rebuild triggers

## 4. Distribution Surfaces

- [x] 4.1 Implement catalog generation producing CodeBuddy-compatible `marketplace.json` (name, owner, `plugins[]` with relative-path sources) from public non-yanked packages
- [x] 4.2 Implement snapshot build: staging assembly of the catalog plus plugin trees extracted from bundles, revision derived from catalog content hash, ZIP creation, and atomic swap with last-good fallback
- [x] 4.3 Add `GET /marketplace/marketplace.json`, `GET /marketplace/snapshot/version`, and `GET /marketplace/snapshot.zip` routes with ETag/Last-Modified headers
- [x] 4.4 Add package listing and detail routes (`GET /marketplace/packages`, `GET /marketplace/packages/{owner}/{name}`) with visibility filtering
- [x] 4.5 Add the per-version download route `GET /marketplace/packages/{owner}/{name}/{version}/download` with private-package authorization and structured not-found behavior
- [x] 4.6 Add write routes: `POST /marketplace/validate`, `POST /marketplace/packages/{owner}/{name}/versions`, version delete/restore under `.../versions/{version}`, `DELETE /marketplace/packages/{owner}/{name}`, and `PATCH /marketplace/packages/{owner}/{name}`

## 5. Schemas And App Wiring

- [x] 5.1 Add request/response schemas to `backend/app/schemas.py` for marketplace list, detail, upload, validate, delete, visibility, and snapshot-version responses
- [x] 5.2 Register marketplace routes and service construction in `backend/app/main.py` without altering existing plugin or Agent Runtime wiring
- [x] 5.3 Ensure startup storage initialization creates marketplace tables without breaking existing schema startup tests

## 6. Frontend Marketplace Admin Console

- [x] 6.1 Add marketplace API client functions and types in `frontend/app/lib/api.ts` and `frontend/app/lib/types.ts`, including token handling
- [x] 6.2 Rebuild `frontend/app/plugins/page.tsx` as the marketplace admin console with package list, search/filter, loading/empty/error states, and refresh
- [x] 6.3 Implement package cards showing owner, latest version, visibility, size, and component badges
- [x] 6.4 Implement the upload flow: file selection, validate preview of manifest and components, token prompt when unconfigured, publish with success/error feedback
- [x] 6.5 Implement version management UI: version list with publish state, yank/restore, purge with explicit confirmation, and visibility toggle
- [x] 6.6 Add styles in `frontend/app/globals.css` consistent with the existing workspace shell and responsive without card nesting or overlap
- [x] 6.7 Preserve the built-in plugin API client functions and types so the unchanged backend `/plugins` API remains reachable

## 7. Tests

- [x] 7.1 Add backend unit tests for ZIP validation (zip-slip, symlink, absolute paths, limits), manifest parsing, and component detection
- [x] 7.2 Add backend service tests for publish, version immutability conflicts, global-unique naming, yank/restore/purge, and per-package locking
- [x] 7.3 Add backend tests for catalog generation (public filtering, yanked exclusion), snapshot build, revision stability, atomic swap, and freshness headers
- [x] 7.4 Add backend route tests for authentication (missing, invalid, foreign-owner tokens), visibility enforcement, downloads, and structured errors
- [x] 7.5 Add frontend API/state tests for marketplace client functions, the validate-then-publish flow, and version management interactions
- [x] 7.6 Add frontend static checks for the rebuilt `/plugins` page and responsive layout safeguards
- [x] 7.7 Run regression for existing plugin-management, chat, and storage schema tests to prove built-in behavior is unchanged

## 8. Docs And Gate Validation

- [x] 8.1 Add `docs/MARKETPLACE.md` documenting the data model, API surface, snapshot and freshness contract, token provisioning, and client configuration values to paste into the WorkBuddy suite source (套件源)
- [x] 8.2 Update `docs/PLUGINS.md` to separate the built-in capability catalog (unchanged backend) from the marketplace, and update `docs/ARCHITECTURE.md` and `docs/DEVELOPMENT.md` with the new domain and validation commands
- [ ] 8.3 Gate validation: configure a real WorkBuddy/CodeBuddy client against the `snapshot.zip` URL (plus `versionUrl` where supported) and record the result; if unsupported, document the catalog fallback and confirm the Git-channel follow-up
- [x] 8.4 Run the documented backend and frontend validation commands and record known limitations

## 9. Git Mirror Fallback Channel (follow-up, additive)

- [x] 9.1 Add `marketplace_git.py`: pure-Python bare-repo mirror builder (loose blob/tree/commit objects, atomic ref updates, `info/refs` regeneration, traversal-safe file resolution)
- [x] 9.2 Add `git_mirror_enabled` setting with `MARKETPLACE_GIT_MIRROR_ENABLED` and `git_dir` storage layout
- [x] 9.3 Hook mirror updates into successful snapshot rebuilds only (objects → refs → info/refs), logging failures without failing publishes
- [x] 9.4 Add `GET /marketplace/git/{file}` static serving with media types and traversal rejection
- [x] 9.5 Add mirror tests: object formats, tree sorting, revision tags, last-good refs on failed rebuild, disable flag, and route serving
- [x] 9.6 Document the git channel (URL, dumb-HTTP behavior, failure semantics) in `docs/MARKETPLACE.md`
