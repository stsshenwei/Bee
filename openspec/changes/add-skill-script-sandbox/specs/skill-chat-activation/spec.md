## ADDED Requirements

### Requirement: Chat-selected skills expose scripts only through sandbox policy
The system SHALL expose executable skill scripts in chat only for skills explicitly selected in the current request and only through the configured skill script sandbox.

#### Scenario: Sandbox-enabled selected skill
- **WHEN** a chat request selects an enabled skill version that contains executable scripts
- **AND** the skill sandbox is configured and available
- **THEN** the agent runtime may expose a script execution tool for that request
- **AND** the tool can execute only scripts from the selected skill version

#### Scenario: Sandbox-disabled selected skill
- **WHEN** a chat request selects a skill that contains executable scripts
- **AND** the skill sandbox is disabled or unavailable
- **THEN** the agent runtime continues to load the skill instructions according to existing behavior
- **AND** script execution is unavailable with an explicit reason

#### Scenario: Preloaded skill without execution handle
- **WHEN** the model calls script execution for a preloaded or unknown skill that has no request execution handle
- **THEN** the runtime rejects the call before sandbox startup

### Requirement: Read-only skill activation remains backward compatible
The system SHALL preserve existing read-only skill instruction behavior for chat requests that do not execute scripts.

#### Scenario: No script execution requested
- **WHEN** a chat request selects skills and the model only reads or applies their instructions
- **THEN** existing selected `SKILL.md` loading behavior remains unchanged
- **AND** the response records loaded skill metadata as before

#### Scenario: Skills runtime disabled
- **WHEN** runtime skills are disabled by server or plugin policy
- **THEN** selected skill instruction loading and script execution are both unavailable
- **AND** the chat request fails or reports the same kind of actionable unavailability as existing skill activation

### Requirement: Chat observability distinguishes loading from execution
The system SHALL distinguish skill instruction loading from sandboxed script execution in runtime events, persisted message metadata, and tool results.

#### Scenario: Skill loaded but not executed
- **WHEN** a selected skill is loaded into the chat request
- **THEN** the UI and runtime metadata show the loaded skill name, version, and hash
- **AND** they MUST NOT imply that any skill script ran

#### Scenario: Skill script executed
- **WHEN** a skill script execution tool call completes
- **THEN** the runtime event stream includes tool result metadata with sandbox mode, backend, exit status, timeout status, and output truncation status

### Requirement: Tool availability follows sandbox configuration
The system SHALL advertise skill execution tools only when runtime skills and the sandbox backend are both enabled for the current runtime mode.

#### Scenario: Execution configured
- **WHEN** runtime skills are enabled and the skill sandbox backend is configured
- **THEN** plugin/tool metadata may advertise skill script execution as available in supported chat modes

#### Scenario: Execution not configured
- **WHEN** runtime skills are enabled but no supported sandbox backend is configured
- **THEN** plugin/tool metadata MUST show script execution as unavailable or omit the execution tool
- **AND** read-only skill loading remains available
