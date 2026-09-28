from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone

from .models import SkillActivation, SkillPackage, SkillVersion


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemorySkillRepository:
    def __init__(self):
        self.packages: dict[str, SkillPackage] = {}
        self.versions: dict[tuple[str, str], SkillVersion] = {}
        self.activations: dict[tuple[str, str], SkillActivation] = {}

    def create_package(self, **values) -> SkillPackage:
        package = SkillPackage(id=values.pop("id", uuid.uuid4().hex), created_at=_now(), updated_at=_now(), **values)
        self.packages[package.id] = package
        return copy.deepcopy(package)

    def get_package(self, skill_id: str): return copy.deepcopy(self.packages.get(skill_id))
    def find_package(self, owner: str, name: str):
        return next((copy.deepcopy(p) for p in self.packages.values() if p.owner == owner and p.name == name), None)
    def list_packages(self): return copy.deepcopy(list(self.packages.values()))
    def add_version(self, **values) -> SkillVersion:
        key = (values["skill_id"], values["version"])
        if key in self.versions: raise KeyError(key)
        record = SkillVersion(id=uuid.uuid4().hex, created_at=_now(), **values)
        self.versions[key] = record
        self.packages[record.skill_id].updated_at = record.created_at
        return copy.deepcopy(record)
    def get_version(self, skill_id: str, version: str): return copy.deepcopy(self.versions.get((skill_id, version)))
    def list_versions(self, skill_id: str): return copy.deepcopy([v for (sid, _), v in self.versions.items() if sid == skill_id])
    def set_version_status(self, skill_id: str, version: str, status: str):
        self.versions[(skill_id, version)].status = status
        return copy.deepcopy(self.versions[(skill_id, version)])
    def set_activation(self, workspace_id: str, skill_id: str, version: str, enabled: bool):
        value = SkillActivation(workspace_id, skill_id, version, enabled, _now())
        self.activations[(workspace_id, skill_id)] = value
        return copy.deepcopy(value)
    def get_activation(self, workspace_id: str, skill_id: str): return copy.deepcopy(self.activations.get((workspace_id, skill_id)))
    def list_activations(self, workspace_id: str): return copy.deepcopy([a for (wid, _), a in self.activations.items() if wid == workspace_id])
