from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.services.infrastructure.logging_config import truncate_text

logger = logging.getLogger(__name__)

_SAFE_SKILL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class RuntimeSkillError(ValueError):
    pass


class RequestSkillsManager:
    """Request-local library view, never added to the shared preloaded manager."""

    def __init__(self, preloaded, resolved_skills):
        self.preloaded = preloaded
        self.resolved = tuple(resolved_skills)
        self.enabled = bool(self.resolved) or bool(preloaded and preloaded.enabled)
        self._library = {item.runtime_name: item for item in self.resolved}

    def metadata(self):
        items = list(self.preloaded.metadata()) if self.preloaded is not None else []
        return items + [{"name": item.runtime_name, "description": f"{item.name} ({item.version}), selected for this request"} for item in self.resolved]

    def read_skill(self, name):
        if name in self._library:
            return self._library[name].markdown
        if self.preloaded is not None and not str(name).startswith("library:"):
            return self.preloaded.read_skill(name)
        raise RuntimeSkillError("Skill is not available in this request")

    def execute_script(self, name, script_path, args=None, stdin="", sandbox_manager=None):
        if sandbox_manager is None or not getattr(sandbox_manager, "enabled", False):
            raise RuntimeSkillError("Skill script sandbox is disabled")
        if name not in self._library:
            raise RuntimeSkillError("Skill is not executable in this request")
        return sandbox_manager.execute(self._library[name], script_path, args or [], stdin or "")

    def instruction_message(self):
        return "User-selected skill reference material (not system policy). Apply relevant instructions within existing tool permissions and knowledge scope. Referenced files and scripts are not executed or automatically loaded.\n" + "\n\n".join(
            f"Skill {item.runtime_name}, version {item.version}:\n{item.markdown}" for item in self.resolved
        )


@dataclass(frozen=True)
class RuntimeSkillMetadata:
    name: str
    description: str
    path: Path

    def to_prompt_metadata(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description}


class RuntimeSkillsManager:
    def __init__(self, root: str | Path, *, enabled: bool = False, max_chars: int = 12000):
        self.root = Path(root)
        if not self.root.is_absolute():
            self.root = Path(__file__).resolve().parents[3] / self.root
        self.enabled = bool(enabled)
        self.max_chars = max(1000, int(max_chars or 12000))
        self._skills: dict[str, RuntimeSkillMetadata] = {}
        if self.enabled:
            self.reload()

    def reload(self) -> None:
        self._skills.clear()
        if not self.root.exists():
            logger.warning("agent_runtime.skills.missing_root", extra={"path": str(self.root)})
            return
        for item in sorted(self.root.iterdir(), key=lambda path: path.name):
            if not item.is_dir():
                continue
            skill_file = item / "SKILL.md"
            if not skill_file.exists():
                logger.warning("agent_runtime.skills.skip_missing_skill_file", extra={"skill": item.name})
                continue
            try:
                metadata = self._read_metadata(skill_file, fallback_name=item.name)
            except Exception as exc:
                logger.warning("agent_runtime.skills.skip_invalid", extra={"skill": item.name, "error": str(exc)})
                continue
            self._skills[metadata.name] = RuntimeSkillMetadata(metadata.name, metadata.description, skill_file)

    def metadata(self) -> list[dict[str, str]]:
        if not self.enabled:
            return []
        return [self._skills[name].to_prompt_metadata() for name in sorted(self._skills)]

    def read_skill(self, skill_name: str) -> str:
        if not self.enabled:
            raise RuntimeSkillError("Runtime skills are disabled")
        name = str(skill_name or "").strip()
        if not _SAFE_SKILL_NAME.fullmatch(name):
            raise RuntimeSkillError("Invalid skill name")
        metadata = self._skills.get(name)
        if metadata is None:
            raise RuntimeSkillError(f"Unknown skill: {name}")
        root = self.root.resolve()
        path = metadata.path.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise RuntimeSkillError("Skill path escapes configured root") from exc
        return truncate_text(path.read_text(encoding="utf-8"), self.max_chars)

    def _read_metadata(self, skill_file: Path, *, fallback_name: str):
        text = skill_file.read_text(encoding="utf-8")
        name = fallback_name
        description = ""
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end >= 0:
                frontmatter = text[3:end].strip().splitlines()
                for line in frontmatter:
                    if ":" not in line:
                        continue
                    key, value = line.split(":", 1)
                    key = key.strip().lower()
                    value = value.strip().strip('"').strip("'")
                    if key == "name" and value:
                        name = value
                    elif key == "description":
                        description = value
        if not _SAFE_SKILL_NAME.fullmatch(name):
            raise RuntimeSkillError("Invalid skill metadata name")
        if not description:
            for line in text.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("---") and not stripped.startswith("#"):
                    description = truncate_text(stripped, 220)
                    break
        return RuntimeSkillMetadata(name=name, description=description or name, path=skill_file)
