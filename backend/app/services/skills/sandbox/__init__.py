from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from app.services.infrastructure.logging_config import truncate_text
from app.services.skills.bundle import _path
from app.services.skills.models import ResolvedSkill, SkillValidationError

SUPPORTED_SANDBOX_MODES = {"disabled", "docker", "docker-read-only", "docker-workspace-write"}
SCRIPT_INTERPRETERS = {
    ".py": "python3",
    ".sh": "bash",
    ".bash": "bash",
    ".js": "node",
    ".mjs": "node",
}
DANGEROUS_CONTENT_PATTERNS = (
    r"rm\s+-rf\s+/(?:\s|$)",
    r"mkfs(?:\.|\s)",
    r"dd\s+if=/dev/zero",
    r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;\s*:",
    r"\b(?:shutdown|reboot|halt|poweroff)\b",
    r"\b(?:killall|pkill)\b",
    r"chmod\s+777\s+/",
    r"chown\s+root",
    r"/etc/(?:passwd|shadow)",
    r"\.ssh/|id_rsa",
    r"\b(?:systemctl|service|crontab|modprobe|insmod|rmmod|docker|kubectl|nsenter|unshare|capsh)\b",
    r"curl\b[^\n|;]*\|\s*(?:sh|bash)",
    r"wget\b[^\n|;]*\|\s*(?:sh|bash)",
    r"subprocess\.[^(]+\([^)]*shell\s*=\s*True",
    r"os\.(?:system|popen)\(",
    r"\beval\s*\(",
    r"__import__\s*\(",
    r"importlib\.import_module\s*\(",
    r"pickle\.loads\s*\(",
    r"yaml\.load\s*\(",
    r"/dev/tcp/",
    r"bash\s+-i",
    r"nc\s+-e|netcat\s+-e|socat\s+exec|mkfifo\b",
)
NETWORK_PATTERNS = (
    r"\b(?:curl|wget|nc|netcat|telnet|ssh|scp|rsync|ftp|sftp)\b",
    r"socket\.connect\s*\(",
    r"urllib\.request",
    r"requests\.(?:get|post|put|delete|request)\s*\(",
    r"http\.client",
    r"\b(?:fetch|axios)\s*\(",
    r"XMLHttpRequest",
)
ARG_INJECTION = re.compile(r"(?:&&|\|\||;|\||`|\$\(|>|<|\r|\n)")
STDIN_INJECTION = re.compile(r"(?:`|\$\(|\r?\n\s*(?:&&|\|\||;|\|))")


class SkillSandboxError(RuntimeError):
    code = "skill_sandbox_error"


class SkillSandboxUnavailable(SkillSandboxError):
    code = "sandbox_unavailable"


class SkillSandboxDenied(SkillSandboxError):
    code = "sandbox_denied"


@dataclass(frozen=True)
class SkillSandboxSettings:
    mode: str = "disabled"
    docker_image: str = "bee-skill-sandbox:latest"
    timeout_seconds: float = 60.0
    memory_bytes: int = 256 * 1024 * 1024
    cpus: float = 1.0
    pids_limit: int = 100
    network_enabled: bool = False
    max_input_chars: int = 64 * 1024
    max_stdout_chars: int = 64 * 1024
    max_stderr_chars: int = 64 * 1024
    max_output_files: int = 100
    max_output_file_bytes: int = 20 * 1024 * 1024
    cache_dir: str = "skill_runs/cache"
    output_dir: str = "skill_runs/outputs"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SkillSandboxSettings":
        env = environ if environ is not None else os.environ

        def text(key: str, default: str) -> str:
            return str(env.get(key, default)).strip() or default

        def integer(key: str, default: int) -> int:
            raw = str(env.get(key, "")).strip()
            value = int(raw) if raw else default
            if value <= 0:
                raise SkillValidationError(f"{key} must be positive")
            return value

        def number(key: str, default: float) -> float:
            raw = str(env.get(key, "")).strip()
            value = float(raw) if raw else default
            if value <= 0:
                raise SkillValidationError(f"{key} must be positive")
            return value

        def boolean(key: str, default: bool) -> bool:
            raw = str(env.get(key, "")).strip().lower()
            if not raw:
                return default
            return raw in {"1", "true", "yes", "on"}

        mode = text("AGENT_RUNTIME_SKILL_SANDBOX_MODE", cls.mode).lower()
        if mode not in SUPPORTED_SANDBOX_MODES:
            raise SkillValidationError("AGENT_RUNTIME_SKILL_SANDBOX_MODE is unsupported")
        return cls(
            mode=mode,
            docker_image=text("AGENT_RUNTIME_SKILL_SANDBOX_DOCKER_IMAGE", cls.docker_image),
            timeout_seconds=number("AGENT_RUNTIME_SKILL_SANDBOX_TIMEOUT_SECONDS", cls.timeout_seconds),
            memory_bytes=integer("AGENT_RUNTIME_SKILL_SANDBOX_MEMORY_BYTES", cls.memory_bytes),
            cpus=number("AGENT_RUNTIME_SKILL_SANDBOX_CPU", cls.cpus),
            pids_limit=integer("AGENT_RUNTIME_SKILL_SANDBOX_PIDS_LIMIT", cls.pids_limit),
            network_enabled=boolean("AGENT_RUNTIME_SKILL_SANDBOX_NETWORK", cls.network_enabled),
            max_input_chars=integer("AGENT_RUNTIME_SKILL_SANDBOX_MAX_INPUT_CHARS", cls.max_input_chars),
            max_stdout_chars=integer("AGENT_RUNTIME_SKILL_SANDBOX_MAX_STDOUT_CHARS", cls.max_stdout_chars),
            max_stderr_chars=integer("AGENT_RUNTIME_SKILL_SANDBOX_MAX_STDERR_CHARS", cls.max_stderr_chars),
            max_output_files=integer("AGENT_RUNTIME_SKILL_SANDBOX_MAX_OUTPUT_FILES", cls.max_output_files),
            max_output_file_bytes=integer("AGENT_RUNTIME_SKILL_SANDBOX_MAX_OUTPUT_FILE_BYTES", cls.max_output_file_bytes),
            cache_dir=text("AGENT_RUNTIME_SKILL_SANDBOX_CACHE_DIR", cls.cache_dir),
            output_dir=text("AGENT_RUNTIME_SKILL_SANDBOX_OUTPUT_DIR", cls.output_dir),
        )

    @property
    def enabled(self) -> bool:
        return self.mode != "disabled"

    @property
    def policy_mode(self) -> str:
        if self.mode == "docker-workspace-write":
            return "docker-workspace-write"
        if self.mode in {"docker", "docker-read-only"}:
            return "docker-read-only"
        return "disabled"


@dataclass(frozen=True)
class MaterializedSkill:
    root: Path
    sha256: str
    files: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class SkillExecutionPolicy:
    mode: str
    backend: str
    skill: ResolvedSkill
    skill_root: Path
    output_root: Path | None
    settings: SkillSandboxSettings


@dataclass(frozen=True)
class SkillSandboxResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    duration_ms: int = 0
    output_files: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error: str = ""


class SkillMaterializer:
    def __init__(self, storage_dir: str | Path, cache_dir: str | Path):
        self.storage_dir = Path(storage_dir)
        self.cache_dir = Path(cache_dir)
        if not self.cache_dir.is_absolute():
            self.cache_dir = Path(__file__).resolve().parents[4] / self.cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def materialize(self, skill: ResolvedSkill) -> MaterializedSkill:
        if not skill.blob_key:
            raise SkillSandboxDenied("Selected skill has no executable bundle handle")
        bundle_path = (self.storage_dir / skill.blob_key).resolve()
        try:
            bundle_path.relative_to(self.storage_dir.resolve())
        except ValueError as exc:
            raise SkillSandboxDenied("Skill bundle path escapes storage root") from exc
        data = bundle_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != skill.sha256:
            raise SkillSandboxDenied("Skill bundle hash mismatch")
        target = self.cache_dir / skill.sha256
        if (target / "SKILL.md").exists():
            return MaterializedSkill(root=target, sha256=skill.sha256, files=tuple(skill.files))
        tmp = Path(tempfile.mkdtemp(prefix=f".{skill.sha256[:12]}-", dir=self.cache_dir))
        try:
            seen: set[str] = set()
            with zipfile.ZipFile(bundle_path) as archive:
                infos = [info for info in archive.infolist() if not info.is_dir()]
                for info in infos:
                    if _zipinfo_is_symlink(info):
                        raise SkillSandboxDenied("Skill bundle symlinks are forbidden")
                    safe = _path(info.filename)
                    folded = safe.casefold()
                    if folded in seen:
                        raise SkillSandboxDenied("Skill bundle contains duplicate normalized paths")
                    seen.add(folded)
                    destination = (tmp / safe).resolve()
                    try:
                        destination.relative_to(tmp.resolve())
                    except ValueError as exc:
                        raise SkillSandboxDenied("Skill bundle path escapes materialization root") from exc
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, destination.open("wb") as handle:
                        shutil.copyfileobj(source, handle)
            if not (tmp / "SKILL.md").exists():
                raise SkillSandboxDenied("Materialized skill is missing SKILL.md")
            try:
                os.replace(tmp, target)
            except FileExistsError:
                shutil.rmtree(tmp, ignore_errors=True)
            except OSError:
                if target.exists():
                    shutil.rmtree(tmp, ignore_errors=True)
                else:
                    raise
            return MaterializedSkill(root=target, sha256=skill.sha256, files=tuple(skill.files))
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise


def _zipinfo_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return (mode & 0o170000) == 0o120000


class SkillScriptValidator:
    def __init__(self, settings: SkillSandboxSettings):
        self.settings = settings

    def validate(self, skill_root: Path, script_path: str, args: list[str], stdin: str) -> tuple[Path, str, str]:
        try:
            safe = _path(script_path)
        except SkillValidationError as exc:
            raise SkillSandboxDenied("Skill script path is unsafe") from exc
        suffix = Path(safe).suffix.lower()
        interpreter = SCRIPT_INTERPRETERS.get(suffix)
        if interpreter is None:
            raise SkillSandboxDenied("Unsupported skill script type")
        script = (skill_root / safe).resolve()
        try:
            script.relative_to(skill_root.resolve())
        except ValueError as exc:
            raise SkillSandboxDenied("Skill script path escapes selected skill") from exc
        if not script.exists() or not script.is_file():
            raise SkillSandboxDenied("Skill script does not exist")
        if len(args) > 50:
            raise SkillSandboxDenied("Too many skill script arguments")
        for arg in args:
            if len(arg) > 4096:
                raise SkillSandboxDenied("Skill script argument exceeds limit")
            if ARG_INJECTION.search(arg):
                raise SkillSandboxDenied("Skill script argument contains shell control syntax")
        if len(stdin) > self.settings.max_input_chars:
            raise SkillSandboxDenied("Skill script input exceeds limit")
        if STDIN_INJECTION.search(stdin):
            raise SkillSandboxDenied("Skill script input contains shell control syntax")
        try:
            content = script.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            raise SkillSandboxDenied("Skill script cannot be read") from exc
        lowered = content.lower()
        for pattern in DANGEROUS_CONTENT_PATTERNS:
            if re.search(pattern, lowered, flags=re.IGNORECASE):
                raise SkillSandboxDenied("Skill script contains a blocked dangerous pattern")
        if not self.settings.network_enabled:
            for pattern in NETWORK_PATTERNS:
                if re.search(pattern, lowered, flags=re.IGNORECASE):
                    raise SkillSandboxDenied("Skill script contains network operations but network is disabled")
        return script, safe, interpreter


class DockerSkillSandbox:
    def __init__(self, settings: SkillSandboxSettings):
        self.settings = settings

    def build_argv(self, policy: SkillExecutionPolicy, script_path: str, interpreter: str, args: list[str]) -> list[str]:
        command = [
            "docker", "run", "--rm",
            "--user", "1000:1000",
            "--cap-drop", "ALL",
            "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--pids-limit", str(self.settings.pids_limit),
            "--memory", str(self.settings.memory_bytes),
            "--memory-swap", str(self.settings.memory_bytes),
            "--cpus", str(self.settings.cpus),
            "--security-opt", "no-new-privileges",
            "--network", "bridge" if self.settings.network_enabled else "none",
            "--mount", f"type=bind,src={policy.skill_root},dst=/workspace/skill,readonly",
        ]
        if policy.output_root is not None:
            command.extend(["--mount", f"type=bind,src={policy.output_root},dst=/workspace/output"])
        command.extend(["--workdir", "/workspace/skill", self.settings.docker_image, interpreter, f"/workspace/skill/{script_path}", *args])
        return command

    def execute(self, policy: SkillExecutionPolicy, script_path: str, interpreter: str, args: list[str], stdin: str) -> SkillSandboxResult:
        argv = self.build_argv(policy, script_path, interpreter, args)
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                argv,
                input=stdin,
                text=True,
                capture_output=True,
                timeout=self.settings.timeout_seconds,
                check=False,
            )
            duration_ms = int((time.perf_counter() - started) * 1000)
            stdout, stdout_truncated = _truncate_with_flag(completed.stdout or "", self.settings.max_stdout_chars)
            stderr, stderr_truncated = _truncate_with_flag(completed.stderr or "", self.settings.max_stderr_chars)
            metadata = self._metadata(policy, stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated)
            return SkillSandboxResult(
                success=completed.returncode == 0,
                stdout=stdout,
                stderr=stderr,
                exit_code=completed.returncode,
                duration_ms=duration_ms,
                output_files=self._output_files(policy.output_root),
                metadata=metadata,
                error="" if completed.returncode == 0 else "Skill script exited with a non-zero status",
                error_code="" if completed.returncode == 0 else "script_failed",
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            stdout, stdout_truncated = _truncate_with_flag(exc.stdout or "", self.settings.max_stdout_chars)
            stderr, stderr_truncated = _truncate_with_flag(exc.stderr or "", self.settings.max_stderr_chars)
            return SkillSandboxResult(
                success=False,
                stdout=stdout,
                stderr=stderr,
                exit_code=None,
                timed_out=True,
                duration_ms=duration_ms,
                metadata=self._metadata(policy, stdout_truncated=stdout_truncated, stderr_truncated=stderr_truncated),
                error_code="sandbox_timeout",
                error="Skill script exceeded the sandbox timeout",
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            return SkillSandboxResult(
                success=False,
                duration_ms=duration_ms,
                metadata=self._metadata(policy, unavailable_reason=exc.__class__.__name__),
                error_code="sandbox_unavailable",
                error="Skill sandbox is unavailable",
            )

    def _metadata(self, policy: SkillExecutionPolicy, **extra: Any) -> dict[str, Any]:
        return {
            "sandbox": {
                "mode": policy.mode,
                "backend": "docker",
                "network": self.settings.network_enabled,
                "enforcement": "full",
                "image": self.settings.docker_image,
                "memory_bytes": self.settings.memory_bytes,
                "cpus": self.settings.cpus,
                "pids_limit": self.settings.pids_limit,
                **{key: value for key, value in extra.items() if value not in (None, "")},
            }
        }

    def _output_files(self, output_root: Path | None) -> tuple[dict[str, Any], ...]:
        if output_root is None or not output_root.exists():
            return ()
        files: list[dict[str, Any]] = []
        root = output_root.resolve()
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                rel = path.resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            size = path.stat().st_size
            files.append({"path": rel, "size": size, "truncated": size > self.settings.max_output_file_bytes})
            if len(files) >= self.settings.max_output_files:
                break
        return tuple(files)


class SkillSandboxManager:
    def __init__(self, storage_dir: str | Path, settings: SkillSandboxSettings):
        self.settings = settings
        self.storage_dir = Path(storage_dir)
        base = Path(__file__).resolve().parents[4]
        cache_dir = Path(settings.cache_dir)
        output_dir = Path(settings.output_dir)
        self.output_dir = output_dir if output_dir.is_absolute() else base / output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.materializer = SkillMaterializer(self.storage_dir, cache_dir if cache_dir.is_absolute() else base / cache_dir)
        self.validator = SkillScriptValidator(settings)
        self.docker = DockerSkillSandbox(settings)

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    def unavailable_reason(self) -> str:
        if not self.enabled:
            return "Skill script sandbox is disabled."
        if self.settings.mode not in SUPPORTED_SANDBOX_MODES:
            return "Skill script sandbox mode is unsupported."
        return ""

    def execute(self, skill: ResolvedSkill, script_path: str, args: list[str] | None = None, stdin: str = "") -> SkillSandboxResult:
        if not self.enabled:
            raise SkillSandboxUnavailable("Skill script sandbox is disabled")
        if self.settings.mode not in SUPPORTED_SANDBOX_MODES:
            raise SkillSandboxUnavailable("Skill script sandbox mode is unsupported")
        materialized = self.materializer.materialize(skill)
        clean_args = [str(item) for item in (args or [])]
        clean_stdin = str(stdin or "")
        _script, safe_path, interpreter = self.validator.validate(materialized.root, script_path, clean_args, clean_stdin)
        output_root: Path | None = None
        if self.settings.policy_mode == "docker-workspace-write":
            output_root = Path(tempfile.mkdtemp(prefix=f"{skill.skill_id[:12]}-", dir=self.output_dir))
        policy = SkillExecutionPolicy(
            mode=self.settings.policy_mode,
            backend="docker",
            skill=skill,
            skill_root=materialized.root,
            output_root=output_root,
            settings=self.settings,
        )
        try:
            return self.docker.execute(policy, safe_path, interpreter, clean_args, clean_stdin)
        finally:
            if output_root is not None:
                shutil.rmtree(output_root, ignore_errors=True)


def _truncate_with_flag(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return truncate_text(text, limit), True
