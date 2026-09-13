from __future__ import annotations

import io
import json
import posixpath
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.marketplace.marketplace_models import (
    KEBAB_CASE_PATTERN,
    MarketplaceValidationError,
    SEMVER_PATTERN,
)

SUPPORTED_MANIFEST_PATHS = (
    (".codebuddy-plugin/plugin.json", "codebuddy"),
    (".codex-plugin/plugin.json", "codex"),
    (".qoder-plugin/plugin.json", "qoder"),
    (".qder-plugin/plugin.json", "qder"),
    ("plugin.json", "generic"),
)
MANIFEST_PATH = SUPPORTED_MANIFEST_PATHS[0][0]
_MANIFEST_FORMAT_BY_PATH = dict(SUPPORTED_MANIFEST_PATHS)
ROOT_SKILL_COMPONENT = "__root_skill__"
PLUGIN_DIR_PREFIX = "plugins/"

_CODEBBUDDY_COMPONENT_DIRS = ("commands", "agents", "bin")
_TEXT_SUFFIXES = (".md", ".json")


@dataclass
class BundleInspection:
    manifest: dict[str, Any] = field(default_factory=dict)
    components: dict[str, Any] = field(default_factory=dict)
    name: str = ""
    version: str = ""
    description: str = ""
    category: str = ""
    keywords: tuple[str, ...] = ()
    file_count: int = 0
    total_uncompressed_bytes: int = 0
    manifest_path: str = ""
    plugin_format: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": dict(self.manifest),
            "components": dict(self.components),
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "category": self.category,
            "keywords": list(self.keywords),
            "file_count": self.file_count,
            "total_uncompressed_bytes": self.total_uncompressed_bytes,
            "manifest_path": self.manifest_path,
            "plugin_format": self.plugin_format,
        }


def normalize_entry_path(raw: str) -> str:
    """Normalize a ZIP member path, rejecting unsafe forms."""

    name = str(raw or "").replace("\\", "/").strip()
    if not name:
        raise MarketplaceValidationError("ZIP entry path is empty")
    if name.startswith("/") or name.startswith("~"):
        raise MarketplaceValidationError(f"ZIP entry cannot use an absolute path: {raw}")
    drive = name[:2]
    if len(name) >= 2 and drive[1] == ":" and drive[0].isalpha():
        raise MarketplaceValidationError(f"ZIP entry cannot use a drive path: {raw}")
    normalized = posixpath.normpath(name)
    if normalized in (".", ""):
        raise MarketplaceValidationError(f"ZIP entry path is invalid: {raw}")
    if normalized.startswith("../") or normalized == ".." or "/../" in normalized:
        raise MarketplaceValidationError(f"ZIP entry cannot traverse directories: {raw}")
    return normalized


def is_symlink_entry(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def inspect_bundle(
    data: bytes,
    *,
    max_entry_count: int,
    max_total_uncompressed_bytes: int,
) -> BundleInspection:
    """Validate and inspect an uploaded plugin ZIP without persisting it."""

    if not data:
        raise MarketplaceValidationError("Upload content is empty")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise MarketplaceValidationError("Upload content is not a valid ZIP file") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > max_entry_count:
            raise MarketplaceValidationError(
                f"ZIP entry count exceeds limit: {len(infos)} > {max_entry_count}"
            )

        inspection = BundleInspection()
        manifest_raw: bytes | None = None
        skill_names: list[str] = []
        commands: list[str] = []
        agents: list[str] = []
        binaries: list[str] = []
        rules: list[str] = []
        skill_details: dict[str, dict[str, str]] = {}
        has_mcp = False
        has_lsp = False
        has_hooks = False

        for info in infos:
            if info.is_dir():
                continue
            if is_symlink_entry(info):
                raise MarketplaceValidationError(f"ZIP entry cannot be a symlink: {info.filename}")
            normalized = normalize_entry_path(info.filename)
            inspection.file_count += 1
            inspection.total_uncompressed_bytes += int(info.file_size)
            if inspection.total_uncompressed_bytes > max_total_uncompressed_bytes:
                raise MarketplaceValidationError(
                    f"ZIP uncompressed size exceeds limit: {inspection.total_uncompressed_bytes}"
                )

            skill_component = _skill_component_name(normalized)
            if skill_component:
                skill_details[skill_component] = _skill_detail(
                    skill_component,
                    normalized,
                    archive.read(info),
                )
            if normalized in _MANIFEST_FORMAT_BY_PATH:
                if manifest_raw is not None:
                    raise MarketplaceValidationError("ZIP can contain only one plugin manifest")
                manifest_raw = archive.read(info)
                inspection.manifest_path = normalized
                inspection.plugin_format = _MANIFEST_FORMAT_BY_PATH[normalized]
                continue
            _collect_component(normalized, skill_names, commands, agents, binaries, rules)
            if normalized == ".mcp.json":
                has_mcp = True
            if normalized == ".lsp.json":
                has_lsp = True
            if normalized.startswith("hooks/"):
                has_hooks = True

        if manifest_raw is None:
            raise MarketplaceValidationError(
                "Missing plugin manifest: ZIP must contain one of "
                + ", ".join(path for path, _format in SUPPORTED_MANIFEST_PATHS)
            )
        try:
            manifest = json.loads(manifest_raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise MarketplaceValidationError("plugin.json must be valid UTF-8 JSON") from exc
        if not isinstance(manifest, dict):
            raise MarketplaceValidationError("plugin.json must be a JSON object")

        name = str(manifest.get("name") or "").strip()
        if not name:
            raise MarketplaceValidationError("plugin.json is missing name")
        if not KEBAB_CASE_PATTERN.match(name):
            raise MarketplaceValidationError(f"Plugin name must be kebab-case: {name}")

        version = str(manifest.get("version") or "").strip()
        if version and not SEMVER_PATTERN.match(version):
            raise MarketplaceValidationError(f"Version must be SemVer: {version}")

        skill_names = [name if item == ROOT_SKILL_COMPONENT else item for item in skill_names]
        skill_details = {
            name if key == ROOT_SKILL_COMPONENT else key: {
                **value,
                "name": name if key == ROOT_SKILL_COMPONENT else key,
            }
            for key, value in skill_details.items()
        }
        hooks_declared = isinstance(manifest.get("hooks"), (dict, str)) and bool(manifest.get("hooks"))
        inspection.manifest = manifest
        inspection.name = name
        inspection.version = version
        inspection.description = str(manifest.get("description") or "").strip()
        inspection.category = str(manifest.get("category") or "").strip()
        keywords = manifest.get("keywords")
        inspection.keywords = tuple(
            str(item).strip() for item in keywords if str(item).strip()
        ) if isinstance(keywords, list) else ()
        unique_skills = sorted(dict.fromkeys(skill_names))
        inspection.components = {
            "skills": unique_skills,
            "skill_details": [
                skill_details.get(skill, {"name": skill, "description": "", "path": ""})
                for skill in unique_skills
            ],
            "commands": sorted(dict.fromkeys(commands)),
            "agents": sorted(dict.fromkeys(agents)),
            "hooks": has_hooks or bool(hooks_declared),
            "mcp": has_mcp or bool(manifest.get("mcpServers")),
            "lsp": has_lsp or bool(manifest.get("lsp")),
            "bin": sorted(dict.fromkeys(binaries)),
            "rules": sorted(dict.fromkeys(rules)),
            "format": inspection.plugin_format,
            "manifest_path": inspection.manifest_path,
        }
        return inspection


def _skill_component_name(normalized: str) -> str:
    parts = normalized.split("/")
    if normalized == "SKILL.md":
        return ROOT_SKILL_COMPONENT
    if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md" and parts[1]:
        return parts[1]
    return ""


def _skill_detail(name: str, path: str, data: bytes) -> dict[str, str]:
    text = data.decode("utf-8", errors="replace")
    return {
        "name": name,
        "description": _skill_description(text),
        "path": path,
    }


def _skill_description(text: str) -> str:
    stripped = text.lstrip()
    body = stripped
    if stripped.startswith("---"):
        lines = stripped.splitlines()
        end_index = next(
            (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
            -1,
        )
        if end_index > 0:
            for line in lines[1:end_index]:
                key, separator, value = line.partition(":")
                if separator and key.strip() == "description":
                    return _clean_description(value)
            body = "\n".join(lines[end_index + 1:])
    for line in body.splitlines():
        candidate = line.strip()
        if candidate and not candidate.startswith("#") and not candidate.startswith("---"):
            return _clean_description(candidate)
    return ""


def _clean_description(value: str) -> str:
    cleaned = value.strip().strip('"\'')
    if len(cleaned) > 220:
        return cleaned[:217].rstrip() + "..."
    return cleaned


def _collect_component(
    normalized: str,
    skills: list[str],
    commands: list[str],
    agents: list[str],
    binaries: list[str],
    rules: list[str],
) -> None:
    parts = normalized.split("/")
    if normalized == "SKILL.md":
        skills.append(ROOT_SKILL_COMPONENT)
        return
    if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md" and parts[1]:
        skills.append(parts[1])
        return
    if len(parts) == 2 and parts[0] == "commands" and parts[1].endswith(".md"):
        commands.append(posixpath.splitext(parts[1])[0])
        return
    if len(parts) == 2 and parts[0] == "agents" and parts[1].endswith(".md"):
        agents.append(posixpath.splitext(parts[1])[0])
        return
    if len(parts) == 2 and parts[0] == "bin" and parts[1]:
        binaries.append(parts[1])
        return
    if len(parts) >= 2 and parts[0] == "rules" and parts[-1]:
        rules.append(posixpath.splitext(parts[-1])[0])


def extract_bundle_to_dir(
    data: bytes,
    target_dir: Path,
    *,
    max_total_uncompressed_bytes: int,
) -> None:
    """Extract a plugin ZIP into ``target_dir`` re-checking every member path."""

    target = zipfile.ZipFile(io.BytesIO(data))
    extracted_bytes = 0
    with target:
        for info in target.infolist():
            if info.is_dir():
                continue
            if is_symlink_entry(info):
                raise MarketplaceValidationError(f"ZIP entry cannot be a symlink: {info.filename}")
            normalized = normalize_entry_path(info.filename)
            extracted_bytes += int(info.file_size)
            if extracted_bytes > max_total_uncompressed_bytes:
                raise MarketplaceValidationError("ZIP uncompressed size exceeds limit")
            destination = target_dir / Path(normalized)
            resolved_root = Path(target_dir).resolve()
            resolved_destination = destination.resolve()
            if resolved_root != resolved_destination and resolved_root not in resolved_destination.parents:
                raise MarketplaceValidationError(f"ZIP entry escapes extract root: {info.filename}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(target.read(info))