from __future__ import annotations

import hashlib
import io
import posixpath
import stat
import zipfile
from dataclasses import dataclass
from typing import Any

import yaml

from .models import SEMVER, SkillSettings, SkillTooLargeError, SkillValidationError


@dataclass(frozen=True)
class ParsedSkillBundle:
    metadata: dict[str, Any]
    markdown: str
    files: tuple[dict[str, Any], ...]
    zip_bytes: bytes
    sha256: str


def _path(raw: str) -> str:
    value = str(raw).replace("\\", "/")
    if not value or value.startswith(("/", "~")) or (len(value) > 1 and value[1] == ":"):
        raise SkillValidationError(f"unsafe archive path: {raw}")
    value = posixpath.normpath(value)
    if value in ("", ".", "..") or value.startswith("../"):
        raise SkillValidationError(f"unsafe archive path: {raw}")
    return value


def _frontmatter(markdown: str) -> dict[str, Any]:
    if not markdown.startswith("---"):
        raise SkillValidationError("SKILL.md requires YAML frontmatter")
    lines = markdown.splitlines()
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise SkillValidationError("SKILL.md frontmatter is not closed") from exc
    try:
        value = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise SkillValidationError("SKILL.md frontmatter is invalid YAML") from exc
    if not isinstance(value, dict):
        raise SkillValidationError("SKILL.md frontmatter must be an object")
    for key in ("name", "description"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise SkillValidationError(f"SKILL.md frontmatter requires {key}")
    projected: dict[str, Any] = {
        "name": value["name"].strip(),
        "description": value["description"].strip(),
    }
    for key in ("version", "category", "author", "license", "source_url"):
        if key not in value:
            continue
        if not isinstance(value[key], str):
            raise SkillValidationError(f"SKILL.md frontmatter field {key} must be a string")
        projected[key] = value[key].strip()
    if "tags" in value:
        tags = value["tags"]
        if not isinstance(tags, list) or any(not isinstance(item, str) for item in tags):
            raise SkillValidationError("SKILL.md frontmatter field tags must be an array of strings")
        projected["tags"] = [item.strip() for item in tags if item.strip()]
    return projected


def parse_skill_bundle(data: bytes, filename: str, settings: SkillSettings) -> ParsedSkillBundle:
    if not data:
        raise SkillValidationError("upload is empty")
    if len(data) > settings.max_upload_bytes:
        raise SkillTooLargeError("upload exceeds configured size")
    entries: dict[str, bytes]
    if filename.lower().endswith(".md") or not data.startswith(b"PK"):
        if len(data) > settings.max_file_bytes:
            raise SkillTooLargeError("SKILL.md exceeds per-file size limit")
        if len(data) > settings.max_uncompressed_bytes:
            raise SkillTooLargeError("SKILL.md exceeds uncompressed size limit")
        entries = {"SKILL.md": data}
    else:
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise SkillValidationError("upload is not a valid ZIP") from exc
        entries = {}
        with archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(infos) > settings.max_files:
                raise SkillTooLargeError("archive has too many files")
            total = 0
            for info in infos:
                if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
                    raise SkillValidationError("archive symlinks are forbidden")
                name = _path(info.filename)
                folded = name.casefold()
                if any(existing.casefold() == folded for existing in entries):
                    raise SkillValidationError("archive contains duplicate normalized paths")
                if info.file_size > settings.max_file_bytes:
                    raise SkillTooLargeError(f"archive member exceeds limit: {name}")
                total += info.file_size
                if total > settings.max_uncompressed_bytes:
                    raise SkillTooLargeError("archive expands beyond configured size")
                try:
                    entries[name] = archive.read(info)
                except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                    raise SkillValidationError(f"archive member cannot be read safely: {name}") from exc
        roots = [name[:-len("SKILL.md")].rstrip("/") for name in entries if name == "SKILL.md" or name.endswith("/SKILL.md")]
        if len(roots) != 1:
            raise SkillValidationError("archive must contain exactly one skill root")
        root = roots[0]
        if root:
            prefix = root + "/"
            if any(not name.startswith(prefix) for name in entries):
                raise SkillValidationError("archive contains files outside its skill root")
            entries = {name[len(prefix):]: value for name, value in entries.items()}
    raw = entries.get("SKILL.md")
    if raw is None:
        raise SkillValidationError("SKILL.md is missing")
    if len(raw) > settings.max_markdown_bytes:
        raise SkillTooLargeError("SKILL.md exceeds configured size")
    try:
        markdown = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillValidationError("SKILL.md must be UTF-8") from exc
    metadata = _frontmatter(markdown)
    if metadata.get("version") and not SEMVER.match(str(metadata["version"])):
        raise SkillValidationError("version must be SemVer")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            archive.writestr(name, entries[name])
    zip_bytes = out.getvalue()
    files = tuple({"path": name, "size": len(value)} for name, value in sorted(entries.items()))
    return ParsedSkillBundle(metadata, markdown, files, zip_bytes, hashlib.sha256(zip_bytes).hexdigest())


def read_bundle_file(data: bytes, path: str, max_bytes: int) -> tuple[bytes, str]:
    safe = _path(path)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = {_path(info.filename): info for info in archive.infolist() if not info.is_dir()}
        info = names.get(safe)
        if info is None:
            raise SkillValidationError("file is not in the bundle")
        if info.file_size > max_bytes:
            raise SkillTooLargeError("file exceeds preview limit")
        content = archive.read(info)
    try:
        return content, content.decode("utf-8")
    except UnicodeDecodeError:
        return content, ""
