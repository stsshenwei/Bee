from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any


BUNDLE_FILENAME = "bundle.zip"
SNAPSHOT_ZIP_FILENAME = "snapshot.zip"
SNAPSHOT_CATALOG_FILENAME = "marketplace.json"
SNAPSHOT_REVISION_FILENAME = "revision.json"


class MarketplaceStorage:
    """On-disk layout for marketplace bundles, staging areas, and snapshots.

    Layout under ``root``::

        bundles/{owner}/{package}/{version}.zip   # single source of truth
        staging/{uuid}/                           # short-lived scratch areas
        snapshots/snapshot.zip                    # live whole-marketplace ZIP
        snapshots/marketplace.json                # catalog served by the API
        snapshots/revision.json                   # {"revision", "built_at"}
    """

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)

    @property
    def bundles_dir(self) -> Path:
        return self.root / "bundles"

    @property
    def staging_dir(self) -> Path:
        return self.root / "staging"

    @property
    def snapshots_dir(self) -> Path:
        return self.root / "snapshots"

    @property
    def git_dir(self) -> Path:
        return self.root / "git"

    def bundle_path(self, owner: str, name: str, version: str) -> Path:
        return self.bundles_dir / owner / name / f"{version}.zip"

    def save_bundle(self, owner: str, name: str, version: str, data: bytes) -> Path:
        target = self.bundle_path(owner, name, version)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return target

    def delete_bundle(self, owner: str, name: str, version: str) -> None:
        path = self.bundle_path(owner, name, version)
        if path.exists():
            path.unlink()

    def delete_package_dir(self, owner: str, name: str) -> None:
        package_dir = self.bundles_dir / owner / name
        if package_dir.exists():
            shutil.rmtree(package_dir, ignore_errors=True)

    def read_bundle(self, owner: str, name: str, version: str) -> bytes:
        path = self.bundle_path(owner, name, version)
        if not path.exists():
            raise FileNotFoundError(str(path))
        return path.read_bytes()

    def new_staging_dir(self) -> Path:
        staging = self.staging_dir / uuid.uuid4().hex
        staging.mkdir(parents=True, exist_ok=True)
        return staging

    def cleanup_staging(self, staging: Path) -> None:
        shutil.rmtree(staging, ignore_errors=True)

    @property
    def snapshot_zip_path(self) -> Path:
        return self.snapshots_dir / SNAPSHOT_ZIP_FILENAME

    @property
    def snapshot_catalog_path(self) -> Path:
        return self.snapshots_dir / SNAPSHOT_CATALOG_FILENAME

    @property
    def snapshot_revision_path(self) -> Path:
        return self.snapshots_dir / SNAPSHOT_REVISION_FILENAME

    def snapshot_zip_path_for_revision(self, revision: str) -> Path:
        return self.snapshots_dir / f"snapshot-{revision}.zip"

    def snapshot_catalog_path_for_revision(self, revision: str) -> Path:
        return self.snapshots_dir / f"marketplace-{revision}.json"

    def snapshot_exists(self) -> bool:
        revision = self.read_revision()
        if revision is None:
            return self.snapshot_zip_path.exists() and self.snapshot_revision_path.exists()
        current = str(revision.get("revision") or "")
        return bool(current) and self.snapshot_zip_path_for_revision(current).exists()

    def read_revision(self) -> dict[str, Any] | None:
        path = self.snapshot_revision_path
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def read_catalog(self, revision: str) -> dict[str, Any] | None:
        path = self.snapshot_catalog_path_for_revision(revision)
        if not path.exists():
            path = self.snapshot_catalog_path
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def current_snapshot_zip_path(self) -> Path | None:
        revision = self.read_revision()
        if revision is not None:
            current = str(revision.get("revision") or "")
            if current:
                path = self.snapshot_zip_path_for_revision(current)
                if path.exists():
                    return path
        if self.snapshot_zip_path.exists():
            return self.snapshot_zip_path
        return None

    def write_snapshot_atomically(
        self,
        *,
        zip_bytes: bytes,
        catalog: dict[str, Any],
        revision: str,
        built_at: str,
        package_count: int,
    ) -> None:
        """Publish snapshot artifacts via temp-file + ``os.replace`` swaps."""

        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        revision_payload = {
            "revision": revision,
            "built_at": built_at,
            "package_count": package_count,
        }
        zip_path = self.snapshot_zip_path_for_revision(revision)
        catalog_path = self.snapshot_catalog_path_for_revision(revision)
        catalog_bytes = json.dumps(catalog, ensure_ascii=False, indent=2).encode("utf-8")

        self._atomic_write_bytes(zip_path, zip_bytes)
        self._atomic_write_bytes(catalog_path, catalog_bytes)
        self._atomic_write_bytes(self.snapshot_zip_path, zip_bytes)
        self._atomic_write_bytes(self.snapshot_catalog_path, catalog_bytes)
        self._atomic_write_bytes(
            self.snapshot_revision_path,
            json.dumps(revision_payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )

    @staticmethod
    def _atomic_write_bytes(target: Path, data: bytes) -> None:
        tmp = target.with_name(f".{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
