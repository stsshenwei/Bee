## Why

Bee today only exposes a catalog of built-in runtime capability toggles (the `plugin-management-tab` surface). The team needs a real plugin management service: a self-hosted plugin marketplace that AI coding assistants (vibecoding tools such as WorkBuddy/CodeBuddy) can connect to as a marketplace source to discover and install plugins, and that gives publishers first-class upload, download, and delete operations for plugin packages.

## What Changes

- Add a marketplace domain with owners (authors/organizations), packages, and immutable SemVer versions stored in PostgreSQL, with bundle payloads (ZIP) persisted on disk.
- Add ZIP bundle upload with security validation: required `.codebuddy-plugin/plugin.json`, zip-slip and symlink protection, size/entry limits, and a dry-run `validate` endpoint that previews the manifest before publishing.
- Enforce global-unique package names where the package name must equal the plugin manifest `name` (kebab-case), so installed plugin skill namespaces never collide; duplicate version publishes are rejected (versions are immutable).
- Add deletion semantics: `yank` (removed from catalog/snapshot, recoverable) and `purge` (physical removal that triggers an atomic snapshot rebuild preserving last-good on failure).
- Add CodeBuddy-compatible distribution: a dynamically generated `marketplace.json` catalog, a whole-marketplace `snapshot.zip` (catalog plus all public plugin directories with relative-path entries), a `version` endpoint for freshness checks (versionUrl → ZIP version → ETag/Last-Modified), and per-package ZIP downloads.
- Add bearer-token identity (token = author identity, scopes `publish`/`admin`) and public/private visibility; private packages are excluded from the public snapshot and require a token to download.
- Repurpose the `/plugins` frontend tab from the built-in capability catalog console into the marketplace admin console (browse, upload with validate preview, version management, yank/purge, visibility). **BREAKING** for the tab's UI behavior only.
- Keep the built-in plugin management backend unchanged: `PluginManagementService`, `/plugins/*` API routes, and Agent Runtime wiring keep their current behavior.
- Distribution note: CodeBuddy documents that URL-type marketplaces cannot recursively mirror plugin files, so plugins with skills/commands/hooks must be delivered via a materializable remote source. P1 ships the ZIP snapshot channel plus the catalog URL; a Git mirror channel is a deferred follow-up that reuses the same content tree.

## Capabilities

### New Capabilities

- `plugin-marketplace-registry`: Package registration and storage — owner/package/version model, ZIP upload and security validation, global-unique naming, immutable versions, yank/purge deletion, publish locking, and snapshot rebuild triggers.
- `plugin-marketplace-distribution`: Client-facing distribution contract — dynamic `marketplace.json` catalog, whole-marketplace `snapshot.zip`, freshness endpoints/headers, per-package download, atomic rebuild with last-good fallback.
- `plugin-marketplace-identity`: Identity and visibility — bearer-token authentication mapped to owners, scopes, public/private packages, and access rules for metadata and downloads.
- `plugin-marketplace-admin-ui`: The `/plugins` workspace as a marketplace admin console — browsing with component badges, upload with validate preview, version management, deletion, and visibility settings.

### Modified Capabilities

- `plugin-management-tab`: The Plugins workspace UI requirement changes from the built-in capability catalog console to the plugin marketplace admin console. The backend catalog API, controlled configuration, plugin tests, runtime integration, and activity requirements remain unchanged.

## Impact

- Backend: new `backend/app/services/marketplace/` (models, Postgres repositories, service, upload validation), new `/marketplace/*` routes in `backend/app/main.py`, request/response models in `backend/app/schemas.py`, table initialization in `backend/app/services/storage/postgres_schema.py`, and a new on-disk storage area for bundles and snapshots.
- Frontend: rebuild of `frontend/app/plugins/page.tsx`, new API client functions and types in `frontend/app/lib/`, and styles in `frontend/app/globals.css`.
- Docs and tests: `docs/PLUGINS.md` split into built-in capability docs plus a new `docs/MARKETPLACE.md`, backend route/service tests, frontend API/state tests, and updated validation commands.
- Unchanged: Agent Runtime tool registration, chat pipeline, RAG retrieval, MCP server, and the existing `/plugins/*` built-in catalog API.
- Known risk carried by design: the exact ZIP-source field format accepted by the WorkBuddy suite source (套件源) is not publicly documented, so P1 includes a gate validation task against a real client; the catalog URL remains a working fallback, and the deferred Git channel would be additive because bundles remain the single source of truth.
