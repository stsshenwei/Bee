## Context

The completed skill library change gives Bee an immutable skill package registry, ZIP download, workspace activation, and request-level chat selection. Its runtime path deliberately loads only selected `SKILL.md` content: referenced files and scripts are not executed, and `execute_skill` currently returns unavailable until a secure sandbox exists.

Two reference implementations were reviewed. Weknora provides the best execution boundary for skill scripts: Docker containers with non-root execution, no network by default, resource limits, script validation, and fail-closed sandbox modes. DeepSeek Harness provides a stronger policy vocabulary: each operation resolves a per-call mode, carries sandbox facts in results, and refuses to run unconfined when enforcement is unavailable. Bee should combine these approaches instead of copying either implementation directly.

Bee is a Python/FastAPI application. Weknora is Go and DeepSeek Harness is TypeScript/Node with platform-specific runners, so the implementation must be a Python-native integration. Docker execution is the first supported backend. Local host execution, Windows ACL runner behavior, and platform-specific kernel sandboxes are excluded from the first release because uploaded skill scripts require process, filesystem, and network containment that is clearer with containers.

## Goals / Non-Goals

**Goals:**

- Execute scripts from chat-selected skill versions only through a configured sandbox.
- Keep script execution disabled by default and fail closed when Docker or policy configuration is unavailable.
- Mount the selected immutable skill version read-only and expose a bounded writable output directory only when policy permits it.
- Validate script paths, script content, arguments, and stdin before execution.
- Return bounded stdout, stderr, exit status, timeout status, and sandbox metadata to the agent runtime.
- Preserve current read-only skill instruction behavior when sandbox execution is disabled.

**Non-Goals:**

- Running scripts directly on the host.
- Installing arbitrary skill dependencies at request time.
- Giving skill scripts access to Bee source, vector stores, backend data, secrets, or the full uploaded package store.
- Supporting network access by default.
- Replacing Bee's plugin permission model or creating a full user-role system.
- Implementing DeepSeek Harness Windows ACL, Landlock, Seatbelt, or bwrap runners in the first version.

## Decisions

### 1. Docker is the first execution backend

Bee will add a `SkillSandboxManager` with modes `disabled` and `docker`. `disabled` is the default. When the mode is `docker`, the manager delegates to a Docker runner that builds `docker run` argv as a list, never a shell string.

The Docker invocation mounts exactly one materialized skill version read-only at `/workspace/skill` and, for writable modes, one per-run output directory at `/workspace/output`. The container runs as a non-root user with `--network none` unless explicitly configured otherwise, `--read-only`, tmpfs `/tmp`, `--cap-drop ALL`, `--pids-limit`, `--memory`, `--memory-swap`, `--cpus`, and `--security-opt no-new-privileges`.

Alternative considered: port DeepSeek Harness local runners. This was rejected for the first release because the Windows ACL runner only limits writes and reports partial enforcement, while Linux/macOS runners depend on host-installed bwrap/Landlock/Seatbelt. That model is useful for file-effect policy but weaker and more operationally varied for uploaded skill code.

### 2. Per-call policy is resolved before every execution

Bee will introduce a Python policy object for skill script execution:

- mode: `disabled`, `docker-read-only`, or `docker-workspace-write`
- selected skill runtime name and immutable version identity
- skill materialization root
- per-run output root
- timeout, memory, CPU, PID, input, and output limits
- network allowed flag, default false

The policy is resolved from server configuration and the current chat request. A skill must be selected for the request, enabled for the workspace, still active, and pinned to the same version hash before execution. A yanked or disabled version is rejected before model/tool execution on new requests.

Alternative considered: store executable permission on the skill package itself. This was rejected because execution authority is a runtime policy decision, not a property that uploaded content can grant itself.

### 3. Skill bundles are materialized by immutable hash

The skill service already stores normalized ZIP blobs and immutable version hashes. Script execution needs filesystem access, so Bee will add a materializer that extracts a stored version into a content-addressed cache such as `skill_runs/cache/<sha256>/`. Extraction reuses the upload parser's normalized path rules: no absolute paths, no traversal, no symlinks, no normalized duplicates, and no writes outside the target directory.

The runner mounts only this extracted version directory. It never mounts the entire skill package store or Bee workspace. Per-run output directories are separate and can be removed by retention cleanup after results or downloadable artifacts are collected.

Alternative considered: stream scripts directly from ZIP into the container. This complicates resource files and interpreter behavior. A content-addressed materialization cache gives a stable directory while preserving immutable version identity.

### 4. Execution tool uses explicit script fields

Bee will add `execute_skill_script` or replace the placeholder `execute_skill` with a script-specific schema:

- `skill_name`: the request-visible runtime name, such as `library:<skill_id>`
- `script_path`: relative path inside the selected skill bundle
- `args`: bounded string array
- `input`: bounded stdin string

The implementation may keep `execute_skill` as a compatibility alias only if it maps to the same explicit fields. Tool registration must expose script execution only when both runtime skills and sandbox execution are configured. The plugin descriptor must stop advertising execution as permanently unavailable once the sandbox is active.

Alternative considered: generic `arguments` object passed to skill code. This was rejected because it hides path and argument validation from the runtime boundary.

### 5. Validator is defense in depth, not the sandbox boundary

Bee will port the Weknora validator concepts into Python: reject unsafe script paths, non-script files, dangerous command patterns, shell injection in args/stdin, reverse shell patterns, suspicious environment manipulation, and disallowed network primitives when network is disabled.

The validator does not replace Docker isolation. It reduces accidental misuse and obvious malicious inputs before the container starts. The hard boundary remains the container configuration and the narrow mounts.

### 6. Results include sandbox facts and bounded output

The runtime tool result will include:

- `stdout` and `stderr`, truncated to configured per-stream and total limits
- `exit_code`
- `timed_out`
- `duration_ms`
- `output_files` metadata when a writable output directory is enabled
- `sandbox`: mode, backend, network flag, enforcement, denied/unavailable reason, image, memory/CPU/PID limits

The public observation should summarize execution outcome without leaking hidden prompts, server paths, environment variables, or secrets. Tool registry output truncation remains a final backstop.

### 7. Deployment keeps sandbox image optional

Bee will add a sandbox Dockerfile based on Python with optional Node/bash support, similar in spirit to Weknora's `Dockerfile.sandbox`. The backend must not pull images unexpectedly in the request path unless an explicit configuration allows preflight image ensure. Docker daemon access is required only when sandbox mode is enabled.

If Bee itself runs in Docker, operators must decide whether to expose Docker access to the backend or run execution through a later sandbox worker service. The first version may document Docker access as an operator requirement while keeping the code structured so a worker backend can replace the direct runner later.

## Risks / Trade-offs

- Docker unavailable in local or production environments → script execution fails closed and UI/API explain that the sandbox is disabled.
- Mounting too much host state could leak data → materialize and mount only the selected immutable skill version plus a per-run output directory.
- Static validation can produce false positives or miss obfuscation → treat validation as defense in depth and keep Docker as the enforcement boundary.
- Docker socket access from a containerized Bee backend is sensitive → document the deployment risk and keep a future sandbox-worker boundary open.
- Long-running or noisy scripts can degrade chat latency → enforce timeout, CPU, memory, PID, stdout/stderr, and input limits.
- Existing skills may reference dependencies not present in the sandbox image → return clear execution errors; dependency installation is a separate future proposal.

## Migration Plan

1. Add sandbox configuration with mode defaulting to `disabled`; no existing deployment changes behavior.
2. Add materialization, validator, Docker runner, and execution tool behind configuration.
3. Update plugin/tool descriptors so execution is advertised only when configured and unavailable reasons remain explicit otherwise.
4. Add UI indicators for executable scripts and sandbox-disabled reasons.
5. Document sandbox image build/run requirements and rollback by setting mode back to `disabled`.

## Open Questions

- Whether the public tool name should be `execute_skill_script` only, or whether `execute_skill` should remain as an alias for compatibility.
- Whether first release should expose downloadable output artifacts from `/workspace/output`, or return only stdout/stderr and defer artifacts.
