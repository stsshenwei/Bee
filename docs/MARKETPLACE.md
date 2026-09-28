# Plugin Marketplace

Bee ships a self-hosted plugin marketplace service. AI coding assistants (vibecoding tools such as WorkBuddy/CodeBuddy) can connect to it as a marketplace source (套件源) to discover and install plugins, and publishers manage plugins through upload, download, and delete operations.

It is a distribution registry, separate from the built-in runtime capability catalog documented in [PLUGINS.md](PLUGINS.md).

## Concepts

- **Owner**: a publisher identity (`user` or `org`), keyed by a kebab-case handle.
- **Package**: a plugin with a globally-unique `name` that must equal the `name` in its manifest, plus `visibility` (`public`/`private`), `description`, `category`, and `keywords`.
- **Version**: an immutable SemVer release of a package. Re-publishing the same version is rejected with 409.
- **Bundle**: the uploaded ZIP — a plugin directory containing `.codebuddy-plugin/plugin.json` plus optional `skills/`, `commands/`, `agents/`, `hooks/`, `.mcp.json`, `.lsp.json`, and `bin/`. Bundles are the single source of truth on disk.
- **Snapshot**: derived distribution artifacts — a dynamic `marketplace.json` catalog and a whole-marketplace `snapshot.zip` containing the catalog plus every public plugin directory with relative-path entries. Rebuilds are atomic and keep serving the last-good snapshot on failure.
- **Git mirror**: an additional publish artifact — a bare git repository under `MARKETPLACE_STORAGE_DIR/git` mirroring the snapshot tree (`main` branch plus a `snapshot-<revision>` tag per rebuild), served as static files so clients can clone it over the dumb-HTTP-compatible protocol.

## Identity And Visibility

Write endpoints require `Authorization: Bearer <token>`. Tokens map to owners and carry `publish` or `admin` scopes:

- `MARKETPLACE_ADMIN_TOKEN`: bootstraps an admin-scoped token for owner `admin` (can manage every owner).
- `MARKETPLACE_OWNER_TOKENS`: per-owner publish tokens in the form `token=owner:scope;token2=owner2:publish`.

Public packages are anonymously readable and downloadable. Private packages are excluded from the public catalog and snapshot and require their owner's token (or admin) for metadata and downloads.

## API

Distribution (read):

| Method | Path | Description |
|---|---|---|
| GET | `/marketplace/marketplace.json` | CodeBuddy-compatible catalog of public packages. Each plugin entry exposes `source` as `{source: "url", url: "<per-plugin git URL>"}` for click-to-install clients. |
| GET | `/marketplace/snapshot/version` | Snapshot freshness payload `{revision, built_at, package_count}` (versionUrl). |
| GET | `/marketplace/snapshot.zip` | Whole-marketplace ZIP (catalog + plugin files), with ETag/Last-Modified. |
| GET | `/marketplace/git/{file}` | Bare git mirror files (`info/refs`, `HEAD`, `objects/...`) for dumb-HTTP clones. |
| GET | `/marketplace/git.git/{file}` | Same mirror with a `.git`-style URL for clients that require git-looking install URLs. |
| GET | `/marketplace/plugins/{name}.git/{file}` | Single-plugin bare git mirror. Plugin `url` fields point here. |
| GET | `/marketplace/packages` | List packages (`owner`, `q`, `category` filters; private ones included with a token). |
| GET | `/marketplace/packages/{owner}/{name}` | Package detail with version history. |
| GET | `/marketplace/packages/{owner}/{name}/versions/{version}/download` | Download one version's bundle ZIP. |

Management (Bearer required):

| Method | Path | Description |
|---|---|---|
| POST | `/marketplace/validate` | Dry-run: parse a ZIP, return manifest and detected components, persist nothing. |
| POST | `/marketplace/packages/{owner}/{name}/versions` | Publish a new version (multipart `file`, optional `version`, `visibility`). |
| DELETE | `/marketplace/packages/{owner}/{name}/versions/{version}` | Yank a version (default) or purge with `?mode=purge`. |
| POST | `/marketplace/packages/{owner}/{name}/versions/{version}/restore` | Restore a yanked version. |
| PATCH | `/marketplace/packages/{owner}/{name}` | Update visibility/description/category/keywords. |
| DELETE | `/marketplace/packages/{owner}/{name}` | Delete a package and all of its versions. |

Upload validation enforces: a parseable `.codebuddy-plugin/plugin.json`, manifest `name` equal to the package name, SemVer versions, size and entry-count limits, and rejects zip-slip paths, symlinks, and absolute-path entries.

## Connecting A vibecoding Client

1. Set `MARKETPLACE_ADMIN_TOKEN` (or an owner token) on the Bee backend and start it.
2. Publish a plugin from the `/plugins` workspace (validate preview → publish) or via `curl`:

   ```powershell
   curl.exe -X POST "http://localhost:8000/marketplace/packages/alice/code-review/versions" `
     -H "Authorization: Bearer <token>" -F "file=@code-review.zip"
   ```

3. Point the client's marketplace source (套件源) at the service, trying in order:
   - `http://<bee-host>:8000/marketplace/git.git` (recommended git clone / `/plugin marketplace add` source)
   - `http://<bee-host>:8000/marketplace/snapshot.zip`
   - `http://<bee-host>:8000/marketplace/marketplace.json`

## Git Mirror Channel

`/plugin marketplace add http://<bee-host>:8000/marketplace/git.git` clones the whole-market mirror; the checked-out tree contains `.codebuddy-plugin/marketplace.json` and `plugins/<name>/...`, so clients can add the marketplace as a source. The legacy `/marketplace/git` path serves the same repository for clients that already use it.

Each plugin entry in `marketplace.json` includes a `source` object whose `url` is a distinct install URL such as `http://<bee-host>:8000/marketplace/plugins/agent-browser.git`. That URL clones a single-plugin repository whose working tree is the plugin root itself, with `.codebuddy-plugin/`, `skills/`, `commands/`, `agents/`, and other bundle files at the top level.

Implementation notes:

- The mirror is written in pure Python (loose objects + refs only, no packfiles, no git binary required) and updated atomically after each successful snapshot swap: objects first, then refs, then `info/refs`. A failed rebuild leaves the previous refs serving.
- Mirror metadata includes a Bee layout marker so old per-plugin mirrors are rebuilt when the clone layout changes even if the marketplace revision is unchanged.
- Clients that request the smart protocol (`?service=git-upload-pack`) transparently fall back to the dumb HTTP protocol, which this static serving supports.
- Each rebuild moves `refs/heads/main` and creates `refs/tags/snapshot-<revision>`; old tags are retained as history.
- Mirror failures are logged and never fail a publish — the ZIP snapshot remains the authoritative artifact.
- Disable with `MARKETPLACE_GIT_MIRROR_ENABLED=0` if the channel is not needed.

## Storage And Environment

- Bundles: `MARKETPLACE_STORAGE_DIR/bundles/<owner>/<package>/<version>.zip` (default `data/marketplace`).
- Snapshots: `MARKETPLACE_STORAGE_DIR/snapshots/` (`snapshot.zip`, `marketplace.json`, `revision.json`), swapped atomically.
- Metadata: PostgreSQL tables `marketplace_owner`, `marketplace_token`, `marketplace_package`, `marketplace_package_version`, `marketplace_snapshot`.
- Limits: `MARKETPLACE_MAX_UPLOAD_BYTES` (default 20 MiB), `MARKETPLACE_MAX_ENTRY_COUNT` (default 4000).
- Naming: `MARKETPLACE_MARKETPLACE_NAME` (default `bee-plugins`), `MARKETPLACE_DESCRIPTION`, `MARKETPLACE_OWNER_NAME`.
- Public URL: set `MARKETPLACE_PUBLIC_BASE_URL` when generated snapshot catalogs need external per-plugin git URLs; live `/marketplace/marketplace.json` responses derive those URLs from the request host.

## Validation

Backend:

```powershell
cd backend
python -m pytest tests/test_marketplace_service.py tests/test_marketplace_routes.py
```

Frontend:

```powershell
cd frontend
node --test app/lib/marketplace-api.test.mjs app/lib/responsive-css.test.mjs
```
