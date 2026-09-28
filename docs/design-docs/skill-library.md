# Skill library and chat selection

The skill library is a registry for local uploads, independent of plugins and knowledge documents.
Browse `/skills`, upload a standalone SKILL.md or a ZIP containing exactly one skill root, review
parsed fields, and publish. The original Markdown and all companion files are retained in a normalized
ZIP. YAML `name` and `description` are authoritative; publisher identity comes from the marketplace
token, while author/category/license/tags/source/version are registration metadata. Versions are
immutable. The detail page provides rendered instructions, original Markdown, bounded text file
previews, version selection, ZIP download, withdrawal/restoration and workspace activation.

## Persistence and authorization

- `skill_package`: stable ID and owner/name identity.
- `skill_version`: immutable Markdown, metadata, file index, ZIP hash/key, and published/yanked status.
- `workspace_skill_activation`: one pinned version and enabled flag per workspace/skill.
- `SKILL_STORAGE_DIR`: object files only; defaults to `backend/skill_packages`.

Public published skills are readable without a token. Upload, version changes and withdrawal require
the owner's publish token or an admin token. Only administrators may activate a skill in a validated
workspace. The first release uses existing marketplace tokens, not a new login system. Downstream
runtime policy and server environment flags remain authority over available tools.

Publication validates paths, links, duplicate members, file counts, decompressed sizes and YAML
types before storage. Each attempt has a unique object key, so concurrent conflicts cannot overwrite
or delete a winning upload. If the database commit result is ambiguous, the object is retained until
repository-aware orphan cleanup. No ZIP member is executed. Unknown YAML metadata remains in the
original source but is not projected into registry fields.

## Chat behavior

Activation makes a fixed version selectable; it does not load that skill into every conversation.
The composer sends `skill_refs: [{skill_id, version}]` only for selected skills. The server resolves
the workspace from existing scope handling, validates activation and version status, enforces the
count/UTF-8 byte budget and takes an immutable snapshot. It rejects stale, disabled, withdrawn or
over-budget selections before model calls. Publishing a new version does not silently upgrade pins.

Selected SKILL.md content is placed in a user-reference message, within existing system and tool
permissions. Quick mode receives it even with no tool calls. The runtime's `read_skill` sees only
preloaded skills plus the current request's selected library IDs (`library:<id>`). Each request uses
its own manager, so same-named skills and concurrent workspaces cannot share selected instructions.
Legacy non-runtime chat paths report `skill_runtime_unavailable` when skills are selected.

`skills_loaded` carries `{items: [{skill_id, name, version, sha256}]}`. It is public loading metadata,
not a claim of successful script execution. The UI displays it on the answer; message persistence
and SSE replay retain the original version. Empty selections preserve old chat behavior. New/opened
conversations clear the composer selection. Companion resources are previewable/downloadable;
the chat does not automatically read them. A skill requiring scripts may need to be used externally.

## Script sandbox

Script execution is a separate runtime capability from instruction loading. The default mode is
`disabled`, so uploaded scripts never run unless an operator configures the Docker sandbox. When the
sandbox is active, the execution tool accepts only a request-selected `library:<id>` skill, a relative
script path, bounded args and bounded stdin. The service revalidates workspace activation, version
status and the immutable ZIP hash before any container starts.

Execution materializes the stored ZIP into `skill_runs/cache/<sha256>/` using the same safe archive
rules as upload validation. Bee mounts that exact directory read-only at `/workspace/skill`; it does
not mount the source tree, `backend/data`, `backend/chroma_db`, the whole skill store or environment
secrets. `docker-read-only` provides no writable skill path. `docker-workspace-write` adds a per-run
output directory mounted at `/workspace/output` and removes it after collecting bounded metadata.

The Docker runner builds argv lists directly and never shells out through a composed command string.
It runs a non-root user with a read-only root filesystem, tmpfs `/tmp`, dropped capabilities,
`no-new-privileges`, no network by default, and configured CPU, memory, PID and timeout limits.
Validation blocks unsafe script paths, unsupported extensions, obvious dangerous commands, reverse
shell patterns, network primitives when network is disabled, and shell-control syntax in args/stdin.
Validation is defense in depth; Docker is the enforcement boundary. If Docker or the image is
unavailable, execution returns `sandbox_unavailable` and no host fallback is attempted.

Tool results include stdout, stderr, exit code, timeout status, output truncation facts and sandbox
metadata. `skills_loaded` continues to mean only that instructions were loaded; script execution is
reported as a separate tool result.

## Operations and rollback

The registry is additive and has no corpus migration. Roll back UI/request fields and then backend
integration; retain skill tables/objects to restore later. Do not delete RAG data or rebuild indexes.
The service exposes `cleanup_orphan_blobs(grace_seconds=86400)` for scheduled/manual maintenance;
run it in a maintenance window with publication paused and a positive grace period. It checks all
referenced version keys before deleting old unreferenced ZIP objects. Failed temporary uploads are
removed synchronously; interrupted temporary files can be reviewed separately during maintenance.

Tests use temporary object directories and an in-memory registry. Browser smoke uses
`tests.skill_ui_server` with a test-only token and no provider calls. These checks do not replace
deployment verification against the configured PostgreSQL instance.
