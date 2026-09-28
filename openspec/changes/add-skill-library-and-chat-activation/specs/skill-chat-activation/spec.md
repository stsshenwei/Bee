## ADDED Requirements

### Requirement: Workspace activation with pinned versions
The system SHALL persist enabled state and a pinned version per workspace and skill. First-version activation mutations MUST require an admin token and validated workspace scope. Publishing a new version SHALL NOT automatically upgrade activation. Environment and workspace runtime skills policy MUST remain authoritative.

#### Scenario: Enable and upgrade explicitly
- **WHEN** an admin enables version 1.0.0 and its publisher later publishes 1.1.0
- **THEN** the workspace continues using 1.0.0 until an authorized explicit version change

#### Scenario: Runtime disabled
- **WHEN** server configuration or workspace policy disables runtime skills
- **THEN** chat cannot load library skills and the UI explains their unavailability

### Requirement: Explicit per-request skill selection
The chat composer SHALL allow selection of enabled skills and send immutable skill_id/version references. The backend MUST resolve the authoritative workspace, reject disabled, withdrawn, mismatched, unauthorized or over-budget selections before model invocation, and clear UI selections on new conversation. Omitted or empty selections SHALL add no library skills and preserve legacy preloaded behavior.

#### Scenario: Select a skill for a question
- **WHEN** a user selects an enabled skill and sends a message
- **THEN** the request includes its pinned reference and the server validates it in the conversation's workspace

#### Scenario: Stale selection
- **WHEN** a selected version has been disabled, withdrawn or replaced in the activation record before sending
- **THEN** the request fails with an actionable skill error before model invocation rather than silently ignoring or upgrading it

### Requirement: Request-isolated instruction loading
The runtime SHALL load selected SKILL.md instructions before generation in quick, reasoning, wiki and rag_wiki modes using an immutable request-local snapshot. It MUST NOT mutate shared skill managers, broaden tools or execute uploaded scripts. A legacy non-runtime path MUST report skill_runtime_unavailable when selections are supplied. File companions remain downloadable and previewable but SHALL NOT be automatically read into chat in this release.

#### Scenario: Quick mode loads instructions
- **WHEN** a valid skill is selected for quick mode on the runtime path
- **THEN** its instructions are available to generation even when no read_skill tool call occurs

#### Scenario: Concurrent workspaces
- **WHEN** two workspaces concurrently select different skills, including skills with equal declared names
- **THEN** each request resolves only its own authorized references and receives no instructions or runtime state from the other

#### Scenario: Skill requests extra privileges
- **WHEN** uploaded instructions request shell execution, dependency installation or an unauthorized tool
- **THEN** runtime tool policy remains unchanged and no uploaded script is executed

### Requirement: Observable durable skill loading
The system SHALL emit and persist loaded skill IDs, names, versions and hashes as public metadata, display an accurate loaded-skills indicator, and retain it through history and replay. It MUST preserve existing SSE, citations, stop and continuation behavior and SHALL NOT claim successful execution solely from instruction loading.

#### Scenario: Replay an answer
- **WHEN** a user reloads or reconnects after an answer using a selected skill
- **THEN** the UI displays the original loaded version metadata even if a newer version now exists

#### Scenario: Legacy chat compatibility
- **WHEN** an older client omits skill_refs or history lacks skill metadata
- **THEN** chat, stream cancellation, citations and history continue with existing behavior
