from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


class SkillError(Exception):
    code = "skill_error"
    status_code = 400


class SkillValidationError(SkillError):
    code, status_code = "skill_invalid", 422


class SkillTooLargeError(SkillValidationError):
    code, status_code = "skill_too_large", 413


class SkillNotFoundError(SkillError):
    code, status_code = "skill_not_found", 404


class SkillConflictError(SkillError):
    code, status_code = "skill_version_conflict", 409


class SkillForbiddenError(SkillError):
    code, status_code = "skill_forbidden", 403


class SkillAuthError(SkillError):
    code, status_code = "skill_unauthorized", 401


class SkillUnavailableError(SkillError):
    code, status_code = "skill_unavailable", 409


@dataclass(frozen=True)
class SkillSettings:
    max_upload_bytes: int = 20 * 1024 * 1024
    max_files: int = 1000
    max_uncompressed_bytes: int = 100 * 1024 * 1024
    max_file_bytes: int = 20 * 1024 * 1024
    max_markdown_bytes: int = 1024 * 1024
    max_preview_bytes: int = 256 * 1024
    max_selected: int = 8
    max_selected_markdown_bytes: int = 256 * 1024

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SkillSettings":
        env = environ if environ is not None else os.environ
        def value(key: str, default: int) -> int:
            raw = str(env.get(key, "")).strip()
            try:
                result = int(raw) if raw else default
            except ValueError as exc:
                raise SkillValidationError(f"{key} must be an integer") from exc
            if result <= 0:
                raise SkillValidationError(f"{key} must be positive")
            return result
        return cls(**{
            "max_upload_bytes": value("SKILL_MAX_UPLOAD_BYTES", cls.max_upload_bytes),
            "max_files": value("SKILL_MAX_FILES", cls.max_files),
            "max_uncompressed_bytes": value("SKILL_MAX_UNCOMPRESSED_BYTES", cls.max_uncompressed_bytes),
            "max_file_bytes": value("SKILL_MAX_FILE_BYTES", cls.max_file_bytes),
            "max_markdown_bytes": value("SKILL_MAX_MARKDOWN_BYTES", cls.max_markdown_bytes),
            "max_preview_bytes": value("SKILL_MAX_PREVIEW_BYTES", cls.max_preview_bytes),
            "max_selected": value("SKILL_MAX_SELECTED", cls.max_selected),
            "max_selected_markdown_bytes": value("SKILL_MAX_SELECTED_MARKDOWN_BYTES", cls.max_selected_markdown_bytes),
        })


@dataclass(frozen=True)
class ResolvedSkill:
    skill_id: str
    name: str
    version: str
    sha256: str
    markdown: str
    runtime_name: str
    blob_key: str = ""
    files: tuple[dict[str, Any], ...] = ()

    def public_metadata(self) -> dict[str, str]:
        return {"skill_id": self.skill_id, "name": self.name, "version": self.version, "sha256": self.sha256}


@dataclass
class SkillPackage:
    id: str
    owner: str
    name: str
    description: str
    category: str = ""
    author: str = ""
    tags: tuple[str, ...] = ()
    license: str = ""
    source_url: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class SkillVersion:
    id: str
    skill_id: str
    version: str
    status: str
    metadata: dict[str, Any]
    markdown: str
    files: tuple[dict[str, Any], ...]
    blob_key: str
    sha256: str
    size: int
    created_at: str = ""


@dataclass
class SkillActivation:
    workspace_id: str
    skill_id: str
    version: str
    enabled: bool
    updated_at: str = ""
