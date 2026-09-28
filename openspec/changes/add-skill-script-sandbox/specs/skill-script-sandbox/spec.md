## ADDED Requirements

### Requirement: Sandbox execution is disabled by default
The system SHALL keep skill script execution disabled unless an administrator explicitly configures a supported sandbox backend.

#### Scenario: Default configuration
- **WHEN** Bee starts without skill sandbox configuration
- **THEN** skill instruction loading remains available according to existing skill settings
- **AND** any skill script execution tool reports that sandbox execution is unavailable
- **AND** no uploaded skill script runs on the host

#### Scenario: Unsupported sandbox mode
- **WHEN** Bee starts with an unknown skill sandbox mode
- **THEN** the sandbox manager MUST reject the configuration or mark execution unavailable
- **AND** the runtime MUST NOT fall back to unsandboxed execution

### Requirement: Only selected immutable skill versions can execute
The system SHALL execute scripts only from skill versions selected for the current chat request and validated against workspace activation, version status, and immutable content hash.

#### Scenario: Selected active skill version
- **WHEN** a chat request selects an enabled skill version and the model calls the execution tool for that selected runtime name
- **THEN** the system resolves the exact stored version and hash before execution
- **AND** the script path is resolved inside that immutable version

#### Scenario: Unselected skill execution
- **WHEN** the model calls the execution tool for a skill that is not selected in the current request
- **THEN** the system rejects the call before sandbox startup

#### Scenario: Yanked version on a new request
- **WHEN** a previously selected skill version has been yanked before a new chat request starts
- **THEN** the system rejects execution before model/tool execution
- **AND** the error explains that the selected skill version is no longer executable

### Requirement: Skill bundles are materialized safely
The system SHALL materialize stored skill ZIP bundles into content-addressed execution directories using the same safe path rules as upload validation.

#### Scenario: Safe materialization
- **WHEN** a selected skill version is materialized for execution
- **THEN** the extracted directory contains only normalized relative bundle paths
- **AND** the directory identity is tied to the stored version hash

#### Scenario: Unsafe bundle path
- **WHEN** a stored or requested bundle entry would escape the materialization directory
- **THEN** materialization MUST fail
- **AND** the script MUST NOT execute

### Requirement: Docker backend enforces process and filesystem limits
The system SHALL run skill scripts in a Docker container with non-root execution, a read-only skill mount, a read-only container root filesystem, no network by default, and configured CPU, memory, PID, and timeout limits.

#### Scenario: Read-only execution
- **WHEN** a script executes in read-only sandbox mode
- **THEN** the selected skill version is mounted read-only
- **AND** the container root filesystem is read-only
- **AND** the script receives no writable skill package path

#### Scenario: Workspace-write execution
- **WHEN** a script executes in workspace-write sandbox mode
- **THEN** the selected skill version remains mounted read-only
- **AND** the script may write only to the configured per-run output directory

#### Scenario: Network disabled by default
- **WHEN** sandbox network access is not explicitly enabled
- **THEN** the Docker invocation disables container networking

#### Scenario: Docker unavailable
- **WHEN** Docker is unavailable or the sandbox image cannot be used
- **THEN** the system returns a sandbox unavailable error
- **AND** the script MUST NOT run without sandbox enforcement

### Requirement: Script invocation is validated before execution
The system SHALL validate the requested script path, script type, script content, arguments, and stdin before starting the sandbox.

#### Scenario: Path traversal in script path
- **WHEN** the execution tool receives a script path containing traversal or an absolute path
- **THEN** the system rejects the call before sandbox startup

#### Scenario: Argument injection
- **WHEN** an argument contains shell operators, command substitutions, redirections, or newlines disallowed by policy
- **THEN** the system rejects the call before sandbox startup

#### Scenario: Dangerous script content
- **WHEN** the script content matches configured dangerous command, reverse shell, unsafe import, or network patterns
- **THEN** the system rejects execution unless policy explicitly allows the matched behavior

### Requirement: Execution output is bounded and observable
The system SHALL return bounded stdout, stderr, exit code, timeout status, duration, and sandbox metadata for every attempted script execution.

#### Scenario: Successful execution
- **WHEN** a sandboxed skill script exits successfully
- **THEN** the runtime tool result includes stdout, stderr, exit code, duration, and sandbox metadata
- **AND** stdout and stderr are truncated according to configured limits

#### Scenario: Timed out execution
- **WHEN** a sandboxed skill script exceeds the configured timeout
- **THEN** the system terminates the sandboxed process
- **AND** the tool result marks the execution as timed out

#### Scenario: Denied or unavailable execution
- **WHEN** execution is denied by policy or unavailable because enforcement cannot be established
- **THEN** the result includes a structured error and sandbox metadata
- **AND** no hidden server paths, secrets, or environment values are exposed in the public observation
