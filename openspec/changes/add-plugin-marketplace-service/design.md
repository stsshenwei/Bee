## Context

Bee is a FastAPI + Next.js RAG workspace. Its existing "Plugins" surface (the `plugin-management-tab` change) is a catalog of hard-coded runtime capability toggles (`knowledge`, `wiki`, `web_search`, ...) persisted per workspace and wired into Agent Runtime tool policies. It is not a distribution mechanism: there are no packages, no versions, and no client-facing catalog.

The product goal is a self-hosted plugin marketplace service. AI coding assistants (vibecoding tools such as WorkBuddy/CodeBuddy) consume "marketplace sources" (套件源) that resolve to a `.codebuddy-plugin/marketplace.json` catalog. Publishers need upload, download, and delete operations for plugin bundles.

Relevant facts from the CodeBuddy/WorkBuddy plugin contract (official docs):

- A plugin is a directory with `.codebuddy-plugin/plugin.json` plus optional `skills/`, `commands/`, `agents/`, `hooks/`, `.mcp.json`, `.lsp.json`, and `bin/`.
- A marketplace is a catalog file `.codebuddy-plugin/marketplace.json` with `name`, `owner`, and `plugins[]` entries (`name`, `source`, `description`, optional `version`, `author`, `keywords`, `category`, `strict`, and component path overrides).
- Documented marketplace source types: Directory, GitHub, Git URL, URL(HTTP). Documented plugin entry sources: relative path, GitHub, Git URL.
- URL-type marketplaces are catalog-only: the client fetches `marketplace.json` (and relative-path `plugin.json`) but cannot recursively mirror plugin files over HTTP. Plugins containing skills/commands/hooks must be delivered via a materializable remote source (Git/NPM), or the whole marketplace must be published via Git or ZIP.
- ZIP snapshots participate in a documented remote-source lifecycle: freshness via `versionUrl` → ZIP URL version → ETag/Last-Modified; download into a staging directory, full manifest validation, atomic replace; failure keeps last-good.

Repo constraints: there is no user/organization model today (only `workspace_id` and the MCP server's bearer-token middleware); PostgreSQL is the application store; the existing built-in plugin API and Agent Runtime wiring must keep working unchanged.

## Goals / Non-Goals

**Goals:**

- A marketplace domain (owner → package → version) with upload, download, and delete.
- CodeBuddy/WorkBuddy-compatible distribution: dynamic catalog plus a whole-marketplace ZIP snapshot.
- Bearer-token identity with author/organization isolation and public/private visibility.
- Repurpose the `/plugins` tab as the marketplace admin console.
- Keep built-in plugin management backend behavior unchanged.

**Non-Goals:**

- No user registration/login UI; tokens are provisioned out of band.
- No Git hosting channel in this change (deferred follow-up; the content tree stays single-sourced so adding it later is additive).
- No in-browser plugin file editing.
- No code execution, sandboxing, or security scanning of plugin contents; no automatic installation into client tools.
- No changes to Agent Runtime tool wiring or the built-in `/plugins/*` API.
- No per-tenant snapshot slices (one public snapshot; private packages are served per-token).

## Decisions

### Decision: Distribute as a CodeBuddy-compatible catalog plus a whole-marketplace ZIP snapshot

`GET /marketplace/marketplace.json` renders the catalog dynamically; `GET /marketplace/snapshot.zip` packages `.codebuddy-plugin/marketplace.json` plus every public plugin directory using relative-path entries; `GET /marketplace/snapshot/version` returns `{revision, built_at}`, and the ZIP carries matching ETag/Last-Modified headers.

Rationale: plugins with skills/scripts cannot be delivered by a URL-type catalog alone; the ZIP snapshot is the documented "publish the whole marketplace" path. Producing both artifacts from one tree lets the suite source (套件源) accept whichever form the client supports.

Alternatives considered:

- Git-hosted marketplace: the most fully documented client path, but it requires git smart-HTTP infrastructure now. Deferred as an additive follow-up because bundles remain the single source of truth. (Follow-up note: since the suite-source ZIP contract remains unverified, the follow-up was implemented inside this change as a pure-Python git mirror — loose objects and refs written during snapshot rebuild, served statically for dumb-HTTP clones at `/marketplace/git`. It adds a publish artifact only: storage model, existing API, and snapshot semantics are unchanged, and mirror failures never fail a publish.)
- Catalog-only with inlined plugins: cannot carry skills/scripts/hooks.
- Custom installer CLI: abandons native `/plugin marketplace add` integration.

### Decision: Global-unique package names equal to the manifest name

A package's `name` is globally unique, kebab-case, and must equal the `name` in its `plugin.json`. Duplicate package names and duplicate version numbers are rejected with 409.

Rationale: CodeBuddy namespaces skills by plugin name and has no scope syntax, so two packages with the same name would collide at install time. Global uniqueness keeps manifest name equal to install name with no rewriting.

Alternatives considered: per-owner names with catalog rewriting (`{owner}-{name}`) — more permissive, but it breaks manifest/install name identity and confuses users.

### Decision: Immutable versions with yank/purge deletion

Published versions never change in place (same version re-upload → 409). Deletion has two modes: `yank` (hidden from catalog/snapshot, metadata and payload retained, reversible) and `purge` (physical removal of metadata and payload, irreversible, requires explicit confirmation).

Rationale: clients cache and keep last-good, so mutable versions would invalidate freshness assumptions. Yank covers "withdraw without breaking installed clients"; purge covers cleanup and compliance.

### Decision: Bearer tokens as identity; owners as the isolation boundary

New tables `marketplace_owner` (handle, kind user|org) and `marketplace_token` (hash, scopes publish|admin, owner_id). Write endpoints require `Authorization: Bearer`; the token maps to an owner. Public packages are anonymously readable and downloadable; private packages require a token of their owner or an admin token.

Rationale: the repo has no account system; tokens mirror the existing MCP auth middleware pattern and are sufficient for a self-hosted registry. Bootstrap/admin tokens are provisioned via environment configuration.

Alternatives considered: a full user/org registration system (out of proportion); reusing `workspace_id` as the isolation boundary (conflates chat workspaces with publishing identity).

### Decision: The uploaded ZIP bundle is the single source of truth; snapshots are derived

Uploads persist one ZIP per version under `MARKETPLACE_STORAGE_DIR` with content hash and size recorded in Postgres. Snapshot/catalog rebuilds extract bundles into a temporary staging area on demand and never keep a long-lived extracted tree.

Rationale: two persistent representations drift; extract-on-demand is cheap at the expected scale and keeps every artifact verifiable against the stored content hash.

### Decision: Atomic snapshot rebuild with last-good fallback

Publish, yank, and purge trigger a rebuild: assemble the tree in a temp directory, validate the generated catalog, zip it, compute the revision (hash of the catalog content), then atomically swap the live snapshot. Failures keep the previous snapshot and leave metadata consistent. Publishes serialize behind a per-package lock; rebuilds producing identical catalog content are no-ops.

Rationale: mirrors the documented client-side refresh behavior (staging → validate → atomic replace; failure keeps last-good) and prevents partial snapshots.

### Decision: Upload validation with a dry-run preview endpoint

`POST /marketplace/validate` accepts a ZIP and returns the parsed manifest and detected components (skills, commands, agents, hooks, MCP servers, LSP config) without persisting. Publishing enforces hard guards: required parseable `.codebuddy-plugin/plugin.json`, manifest name match, SemVer version, size and entry-count limits, path normalization against zip-slip, and rejection of symlinks and absolute-path entries.

Rationale: ZIP is an attack surface; the validate endpoint powers the admin console's preview step and keeps the checks testable independently of persistence.

### Decision: Repurpose the `/plugins` tab; keep the built-in backend

The frontend tab becomes the marketplace admin console. The built-in catalog API (`/plugins`, `/plugins/{id}`, `PATCH`, `/test`, `/activity`) and `PluginManagementService` runtime wiring stay untouched, so Agent Runtime behavior is unaffected by the UI change.

Rationale: the change is cosmetic relative to the runtime; removing the built-in service would force runtime rework and break existing tests for no functional gain.

Alternatives considered: co-locating both consoles in one tab (cluttered); deleting the built-in catalog (breaks a documented management surface).

## Risks / Trade-offs

- [Risk] The exact ZIP-source declaration accepted by the WorkBuddy suite source (套件源) is not publicly documented (`MarketplaceType` enumerates Directory/GitHub/Git/URL). → Mitigation: a P1 gate task validates a real client against `snapshot.zip` plus `versionUrl`; the catalog URL remains a fallback; the deferred Git channel is additive because bundles stay the single source of truth.
- [Risk] Malicious or malformed ZIPs (zip-slip, symlinks, archive bombs). → Mitigation: hard validation (path normalization, symlink and absolute-path rejection, size and entry limits), extraction only into staging, and content-hash verification.
- [Risk] Name conflicts on a flat global namespace (squatting). → Mitigation: owner-scoped publishing rights, explicit 409 conflicts, and an admin scope for arbitration.
- [Risk] Private package leakage through the shared snapshot. → Mitigation: snapshots are built from public packages only; private downloads require owner/admin tokens; rebuild validation asserts snapshot contents.
- [Risk] Snapshot rebuild latency grows with package count. → Mitigation: rebuild on write, not on read; content-hash short-circuit for unchanged catalogs; atomic swap keeps readers on last-good.
- [Risk] Purging a version that installed clients still reference. → Mitigation: yank is the default; purge requires explicit confirmation and is documented as blocking future installs of that version.

## Migration Plan

1. Archive `add-plugin-management-tab` (or ensure its spec reaches main specs) before archiving this change, so the `plugin-management-tab` delta merges cleanly.
2. Add marketplace models, repositories, and table initialization alongside existing Postgres startup checks.
3. Implement token identity, upload validation, and version persistence.
4. Implement catalog/snapshot generation and the distribution routes.
5. Rebuild the `/plugins` frontend tab as the marketplace admin console.
6. Add backend and frontend tests; split `docs/PLUGINS.md` into built-in capability docs plus a new `docs/MARKETPLACE.md`; record the suite-source gate result.

Rollback: the marketplace domain is additive. Hiding the `/plugins` tab change and disabling `/marketplace` routes restores prior behavior; built-in plugin management and runtime wiring are untouched throughout.

## Open Questions

- Should per-owner catalogs (`GET /marketplace/{owner}/marketplace.json`) ship with P1 for team-scoped suite sources, or wait for a demonstrated need?
- What retention policy applies to purged payloads (immediate unlink versus a grace window)?
- Should the freshness endpoint expose per-package revisions in addition to the whole-snapshot revision?
