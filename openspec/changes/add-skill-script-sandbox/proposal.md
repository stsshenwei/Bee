## Why

Bee now supports managing downloadable skills and selecting enabled skills in chat, but uploaded skill scripts remain intentionally non-executable. To make script-capable skills usable without expanding chat tool authority directly, Bee needs a fail-closed sandbox boundary that can execute a selected skill version with bounded file, process, network, and output effects.

## What Changes

- Add a Docker-backed skill script sandbox for chat runtime execution, disabled by default and exposed only when configured.
- Replace the current unavailable `execute_skill` placeholder with an explicit script execution tool that accepts a selected skill reference, script path, arguments, and optional stdin.
- Materialize immutable skill versions from stored ZIP bundles into content-addressed execution directories and mount only the selected version into the sandbox.
- Add per-call sandbox policy resolution inspired by DeepSeek Harness: execution mode, workspace/output root, fail-closed behavior, and structured sandbox metadata on every result.
- Add Weknora-style hardening for execution: non-root user, no network by default, read-only root filesystem, read-only skill mount, bounded writable output directory, CPU/memory/PID/time limits, interpreter allowlist, and script/argument/stdin validation.
- Keep local host execution out of first scope. If Docker or sandbox configuration is unavailable, script execution fails closed and the chat still supports read-only skill instructions.

## Capabilities

### New Capabilities

- `skill-script-sandbox`: Sandboxed execution of scripts from user-selected, immutable skill versions in Bee chat.

### Modified Capabilities

- `skill-chat-activation`: Chat-selected skills may expose executable scripts only through the configured sandbox; existing read-only skill loading remains supported.

## Impact

- Backend runtime: `AgentRuntime`, `RuntimeSkillsManager`, `RequestSkillsManager`, tool registry, tool metadata, and chat request handling.
- Backend skills service: version materialization from stored ZIP blobs, script path resolution, immutable execution handles, and yanked/disabled revalidation before execution.
- Backend sandbox modules: Docker runner, validator, execution policy, result metadata, output bounding, and cleanup.
- API/config: new environment variables for sandbox mode, Docker image, timeout, CPU, memory, network, writable output, and max input/output sizes.
- Frontend: skill detail/chat affordances for executable-script availability and sandbox-disabled reasons; no change to existing skill download behavior.
- Deployment: optional sandbox image build/pull and Docker daemon access in environments that enable execution.
- Docs/tests: architecture and development docs for sandbox setup plus tests for disabled mode, validation, path containment, fail-closed behavior, and Docker command construction.
