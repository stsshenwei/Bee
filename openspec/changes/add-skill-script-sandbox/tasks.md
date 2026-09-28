## 1. Configuration and policy

- [x] 1.1 Extend `AgentRuntimeConfig` with skill sandbox mode, Docker image, timeout, CPU, memory, PID, network, input, and output limits.
- [x] 1.2 Load sandbox environment variables in `backend/app/main.py` with execution disabled by default.
- [x] 1.3 Add a skill execution policy model that resolves per-call mode, selected skill identity, version hash, output root, and sandbox limits.
- [x] 1.4 Update plugin/tool metadata so skill execution is advertised only when runtime skills and a supported sandbox backend are enabled.

## 2. Skill version materialization

- [x] 2.1 Extend resolved library skills with immutable execution metadata needed to locate the stored ZIP blob and version hash.
- [x] 2.2 Add a materializer that extracts a stored skill ZIP into a content-addressed cache using safe normalized paths.
- [x] 2.3 Reject materialization on traversal, absolute paths, symlinks, normalized duplicates, missing `SKILL.md`, or hash mismatch.
- [x] 2.4 Add per-run output directory creation and cleanup hooks without mounting Bee source, `backend/data`, `backend/chroma_db`, or the full skill store.

## 3. Validator

- [x] 3.1 Implement script path validation for relative paths inside the selected materialized skill version.
- [x] 3.2 Implement interpreter/script type allowlist for supported extensions.
- [x] 3.3 Port dangerous command, reverse shell, unsafe import, environment manipulation, and network-pattern checks into Python.
- [x] 3.4 Validate args and stdin for configured length limits and injection patterns before sandbox startup.
- [x] 3.5 Return structured validation errors suitable for runtime tool results and frontend unavailable messages.

## 4. Docker sandbox backend

- [x] 4.1 Add a sandbox Dockerfile with non-root user and the supported interpreters needed for first release.
- [x] 4.2 Implement Docker command construction as argv lists, never shell strings.
- [x] 4.3 Apply Docker hardening flags: non-root user, read-only rootfs, read-only skill mount, tmpfs `/tmp`, no network by default, dropped capabilities, no-new-privileges, memory, CPU, swap, and PID limits.
- [x] 4.4 Mount a writable per-run output directory only for workspace-write mode.
- [x] 4.5 Enforce timeout and termination behavior for long-running containers.
- [x] 4.6 Capture stdout, stderr, exit code, duration, timeout status, and truncation facts.
- [x] 4.7 Fail closed with `sandbox_unavailable` when Docker, the image, or container startup cannot establish enforcement.

## 5. Runtime tool integration

- [x] 5.1 Replace the placeholder execution behavior with an explicit skill script execution tool schema.
- [x] 5.2 Ensure the tool can execute only request-selected library skills and rejects unknown, unselected, preloaded-without-handle, yanked, or disabled versions.
- [x] 5.3 Wire `RequestSkillsManager` to provide read-only skill content and execution handles without mutating the shared preloaded manager.
- [x] 5.4 Include sandbox metadata in `RuntimeToolResult.metadata` and keep public observations bounded and secret-safe.
- [x] 5.5 Preserve current read-only skill loading behavior when no script execution is requested.

## 6. Frontend and API visibility

- [x] 6.1 Show executable-script availability and sandbox-disabled reasons in skill detail or chat skill selection UI.
- [x] 6.2 Ensure chat events distinguish skill loading from script execution.
- [x] 6.3 Display script execution outcomes with sandbox mode, exit status, timeout status, and truncated-output indicators where tool events are shown.
- [x] 6.4 Keep skill download and read-only chat activation UI unchanged for skills that do not execute scripts.

## 7. Documentation and validation

- [x] 7.1 Update architecture and runtime design docs with the sandbox policy, Docker backend, materialization flow, and deployment requirements.
- [x] 7.2 Add backend tests for disabled mode, unsupported mode, selected-skill enforcement, yanked-version rejection, materialization safety, path traversal, and validator failures.
- [x] 7.3 Add Docker runner unit tests that verify command argv construction and hardening flags without requiring a live Docker daemon.
- [x] 7.4 Add integration tests or guarded smoke tests for successful sandbox execution when Docker is available.
- [x] 7.5 Add frontend tests for sandbox-disabled messaging and execution metadata rendering.
- [x] 7.6 Run OpenSpec validation and the relevant backend/frontend validation commands from `docs/DEVELOPMENT.md`.
