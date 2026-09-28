from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.services.marketplace.marketplace_models import MarketplacePrincipal

from .bundle import parse_skill_bundle, read_bundle_file
from .models import (ResolvedSkill, SEMVER, SkillAuthError, SkillConflictError, SkillForbiddenError,
                     SkillNotFoundError, SkillSettings, SkillUnavailableError, SkillValidationError)


class SkillService:
    def __init__(self, repository, storage_dir, settings: SkillSettings | None = None):
        self.repository = repository
        self.storage_dir = Path(storage_dir)
        self.settings = settings or SkillSettings.from_env()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._publish_lock = threading.Lock()

    def validate(self, data: bytes, filename: str, metadata: dict[str, Any] | None = None):
        parsed = parse_skill_bundle(data, filename, self.settings)
        merged = self._merge_metadata(parsed.metadata, metadata or {})
        merged.setdefault("version", "1.0.0")
        if not SEMVER.match(str(merged["version"])): raise SkillValidationError("version must be SemVer")
        return {"valid": True, "parsed_metadata": merged, "file_index": list(parsed.files), "errors": [], "sha256": parsed.sha256}

    def publish(self, data: bytes, filename: str, metadata: dict[str, Any], principal: MarketplacePrincipal, skill_id: str | None = None):
        with self._publish_lock:
            return self._publish_locked(data, filename, metadata, principal, skill_id)

    def _publish_locked(self, data: bytes, filename: str, metadata: dict[str, Any], principal: MarketplacePrincipal, skill_id: str | None = None):
        if principal is None: raise SkillAuthError("valid bearer token required")
        if not principal.can_publish: raise SkillForbiddenError("publish scope required")
        parsed = parse_skill_bundle(data, filename, self.settings)
        merged = self._merge_metadata(parsed.metadata, metadata)
        version = str(merged.get("version") or "1.0.0")
        if not SEMVER.match(version): raise SkillValidationError("version must be SemVer")
        requested_owner = str(metadata.get("owner") or principal.owner_handle).strip()
        if requested_owner != principal.owner_handle and not principal.is_admin:
            raise SkillForbiddenError("only admin may publish for another owner")
        package = self.repository.get_package(skill_id) if skill_id else self.repository.find_package(requested_owner, str(merged["name"]))
        if skill_id and package is None:
            raise SkillNotFoundError("skill not found")
        if package and package.owner != principal.owner_handle and not principal.is_admin: raise SkillForbiddenError("skill belongs to another owner")
        if package and package.name != merged["name"]:
            raise SkillValidationError("published skill name must match the existing package")
        if package is None:
            try:
                package = self.repository.create_package(owner=requested_owner, name=str(merged["name"]), description=str(merged["description"]), category=str(merged.get("category", "")), author=str(merged.get("author", "")), tags=tuple(merged.get("tags") or ()), license=str(merged.get("license", "")), source_url=str(merged.get("source_url", "")))
            except Exception:
                package = self.repository.find_package(requested_owner, str(merged["name"]))
                if package is None:
                    raise
        if self.repository.get_version(package.id, version): raise SkillConflictError("version already exists")
        blob_key = f"{package.id}/{version}/{parsed.sha256}-{uuid.uuid4().hex}.zip"
        target = self.storage_dir / blob_key; target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as handle: handle.write(parsed.zip_bytes)
            os.replace(temp_name, target)
            try:
                record = self.repository.add_version(skill_id=package.id, version=version, status="published", metadata=merged, markdown=parsed.markdown, files=parsed.files, blob_key=blob_key, sha256=parsed.sha256, size=len(parsed.zip_bytes))
            except Exception as exc:
                try:
                    persisted = self.repository.get_version(package.id, version)
                except Exception:
                    # The transaction outcome is unknown. Retain the uniquely named
                    # blob for grace-period orphan recovery rather than breaking a
                    # possibly committed row.
                    raise
                if persisted is not None:
                    if persisted.blob_key == blob_key:
                        return self._version_view(persisted)
                    target.unlink(missing_ok=True)
                    raise SkillConflictError("version already exists") from exc
                target.unlink(missing_ok=True)
                raise
        finally:
            Path(temp_name).unlink(missing_ok=True)
        return self._version_view(record)

    def list_skills(self, q="", category="", author="", sort="updated", cursor=0, limit=20):
        items=[]
        for p in self.repository.list_packages():
            versions=[v for v in self.repository.list_versions(p.id) if v.status == "published"]
            if not versions: continue
            latest=sorted(versions,key=lambda v:v.created_at,reverse=True)[0]
            view = self._package_view(p, latest)
            if q and q.lower() not in f"{view['name']} {view['description']}".lower(): continue
            if category and view["category"] != category: continue
            if author and view["author"] != author: continue
            view["latest_version"] = latest.version
            items.append(view)
        items.sort(key=lambda x: x["name"] if sort == "name" else x.get("updated_at", ""), reverse=sort != "name")
        start=max(0,int(cursor or 0)); page=items[start:start+limit]
        return {"items":page,"next_cursor":str(start+limit) if start+limit<len(items) else None}

    def detail(self, skill_id, principal=None):
        p = self._package(skill_id)
        manageable = self._may_manage(principal, p.owner)
        versions = self.repository.list_versions(skill_id)
        visible = versions if manageable else [v for v in versions if v.status == "published"]
        if not visible and not manageable:
            raise SkillNotFoundError("skill not found")
        latest = max(visible, key=lambda v: v.created_at) if visible else None
        return {**self._package_view(p, latest), "can_manage": manageable, "versions": [self._version_view(v) for v in visible]}

    def version(self, skill_id, version, principal=None):
        p = self._package(skill_id)
        record = self._record(skill_id, version)
        if record.status == "yanked" and not self._may_manage(principal, p.owner):
            raise SkillNotFoundError("skill version not found")
        view = self._version_view(record)
        view["can_manage"] = self._may_manage(principal, p.owner)
        return view
    def set_status(self, skill_id, version, status, principal):
        if status not in {"published","yanked"}: raise SkillValidationError("status must be published or yanked")
        p=self._package(skill_id); self._record(skill_id, version); self._manage(principal,p.owner)
        return self._version_view(self.repository.set_version_status(skill_id,version,status))
    def download(self, skill_id, version):
        record=self._record(skill_id,version)
        if record.status != "published": raise SkillNotFoundError("version is withdrawn")
        return (self.storage_dir/record.blob_key).read_bytes()
    def preview(self, skill_id, version, path, principal=None):
        record=self._record(skill_id,version)
        package = self._package(skill_id)
        if record.status != "published" and not self._may_manage(principal, package.owner): raise SkillNotFoundError("version is withdrawn")
        content,text=read_bundle_file((self.storage_dir/record.blob_key).read_bytes(),path,self.settings.max_preview_bytes)
        return {"path":path,"size":len(content),"text":text,"binary":not bool(text)}
    def activate(self, workspace_id, skill_id, enabled, version, principal, runtime_enabled=True):
        if principal is None: raise SkillAuthError("admin token required")
        if not principal.is_admin: raise SkillForbiddenError("admin scope required")
        record=self._record(skill_id,version)
        if enabled and record.status != "published": raise SkillUnavailableError("version is withdrawn")
        return vars(self.repository.set_activation(workspace_id,skill_id,version,bool(enabled)))
    def workspace_skills(self, workspace_id, runtime_enabled=True):
        items=[]
        for a in self.repository.list_activations(workspace_id):
            p=self._package(a.skill_id); v=self._record(a.skill_id,a.version)
            available=bool(runtime_enabled and a.enabled and v.status=="published")
            reason=None if available else ("runtime_disabled" if not runtime_enabled else "disabled" if not a.enabled else "version_yanked")
            items.append({"skill_id":p.id,"name":p.name,"version":v.version,"enabled":a.enabled,"available":available,"reason":reason,"sha256":v.sha256})
        return {"items":items,"runtime_enabled":bool(runtime_enabled)}
    def resolve(self, workspace_id, refs, runtime_enabled):
        if not refs:return ()
        if not runtime_enabled: raise SkillUnavailableError("skill runtime is disabled")
        if len(refs)>self.settings.max_selected: raise SkillValidationError("too many selected skills")
        out=[]; total=0
        for ref in refs:
            sid=str(ref.get("skill_id") or ""); version=str(ref.get("version") or "")
            a=self.repository.get_activation(workspace_id,sid)
            if not a or not a.enabled or a.version!=version: raise SkillUnavailableError(f"skill {sid}@{version} is not enabled at its pinned version")
            p=self._package(sid); v=self._record(sid,version)
            if v.status!="published": raise SkillUnavailableError(f"skill {sid}@{version} is withdrawn")
            total+=len(v.markdown.encode("utf-8"))
            if total>self.settings.max_selected_markdown_bytes: raise SkillValidationError("selected skill instructions exceed budget")
            out.append(ResolvedSkill(sid,p.name,version,v.sha256,v.markdown,f"library:{sid}",v.blob_key,tuple(v.files)))
        return tuple(out)
    def _package(self,sid):
        p=self.repository.get_package(sid)
        if not p: raise SkillNotFoundError("skill not found")
        return p
    def _record(self,sid,version):
        v=self.repository.get_version(sid,version)
        if not v: raise SkillNotFoundError("skill version not found")
        return v
    def _manage(self,p,owner):
        if p is None: raise SkillAuthError("valid bearer token required")
        if not (p.is_admin or (p.can_publish and p.owner_handle==owner)): raise SkillForbiddenError("owner or admin required")
    @staticmethod
    def _may_manage(principal, owner):
        return bool(principal and (principal.is_admin or (principal.can_publish and principal.owner_handle == owner)))

    @staticmethod
    def _merge_metadata(parsed: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(supplied, dict):
            raise SkillValidationError("metadata must be an object")
        allowed = {"version", "category", "author", "tags", "license", "source_url", "owner"}
        unknown = set(supplied) - allowed
        if unknown:
            raise SkillValidationError(f"unsupported metadata fields: {', '.join(sorted(unknown))}")
        text_fields = ("version", "category", "author", "license", "source_url", "owner")
        for key in text_fields:
            if key in supplied and not isinstance(supplied[key], str):
                raise SkillValidationError(f"metadata field {key} must be a string")
        if "tags" in supplied and (not isinstance(supplied["tags"], list) or any(not isinstance(item, str) for item in supplied["tags"])):
            raise SkillValidationError("metadata field tags must be an array of strings")
        result = dict(parsed)
        for key in allowed - {"owner"}:
            if supplied.get(key) not in (None, "", []):
                result[key] = supplied[key]
        result["name"] = parsed["name"]
        result["description"] = parsed["description"]
        return result

    def cleanup_orphan_blobs(self, grace_seconds: int = 86400) -> int:
        """Remove old blobs not referenced by the repository; intended for a manual maintenance command."""
        referenced = {v.blob_key for p in self.repository.list_packages() for v in self.repository.list_versions(p.id)}
        cutoff = time.time() - max(0, grace_seconds)
        removed = 0
        for path in self.storage_dir.rglob("*.zip"):
            key = path.relative_to(self.storage_dir).as_posix()
            if key not in referenced and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed
    @staticmethod
    def _package_view(p, version=None):
        metadata = version.metadata if version else {}
        return {"id":p.id,"name":p.name,"owner":p.owner,"description":metadata.get("description",p.description),"category":metadata.get("category",p.category),"author":metadata.get("author",p.author),"tags":list(metadata.get("tags",p.tags)),"license":metadata.get("license",p.license),"source_url":metadata.get("source_url",p.source_url),"updated_at":version.created_at if version else p.updated_at}
    @staticmethod
    def _version_view(v): return {"id":v.id,"skill_id":v.skill_id,"version":v.version,"status":v.status,"markdown":v.markdown,"files":list(v.files),"sha256":v.sha256,"size":v.size,"metadata":dict(v.metadata),"created_at":v.created_at}
