from __future__ import annotations

import hashlib
import io
import json
import logging
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.marketplace.marketplace_bundle import (
    PLUGIN_DIR_PREFIX,
    extract_bundle_to_dir,
    inspect_bundle,
    normalize_entry_path,
)
from app.services.marketplace.marketplace_git import GitMirrorBuilder
from app.services.marketplace.marketplace_models import (
    KEBAB_CASE_PATTERN,
    MarketplaceAuthError,
    MarketplaceConflictError,
    MarketplaceForbiddenError,
    MarketplaceNotFoundError,
    MarketplacePackage,
    MarketplacePackageVersion,
    MarketplacePrincipal,
    MarketplaceSettings,
    MarketplaceValidationError,
    PACKAGE_VISIBILITIES,
    SEMVER_PATTERN,
)
from app.services.marketplace.marketplace_storage import MarketplaceStorage
from app.services.marketplace.postgres_marketplace_repository import PostgresMarketplaceRepository

logger = logging.getLogger(__name__)


class MarketplaceService:
    """Orchestration for the self-hosted plugin marketplace.

    Publishes CodeBuddy-compatible distribution artifacts (a dynamic
    ``marketplace.json`` catalog and a whole-marketplace ``snapshot.zip``)
    from uploaded plugin bundles, which are the single source of truth.
    """

    def __init__(
        self,
        repository: PostgresMarketplaceRepository,
        storage: MarketplaceStorage,
        settings: MarketplaceSettings,
    ):
        self.repository = repository
        self.storage = storage
        self.settings = settings
        self._package_locks: dict[str, threading.Lock] = {}
        self._package_locks_guard = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._ensure_bootstrap_tokens()

    # ---------------------------------------------------------------- identity

    def resolve_principal(self, authorization: str | None) -> MarketplacePrincipal | None:
        if not authorization or not authorization.strip().lower().startswith("bearer "):
            return None
        token = authorization.strip()[7:].strip()
        if not token:
            return None
        record = self.repository.resolve_token(token)
        if record is None:
            return None
        self.repository.touch_token(record.id)
        return MarketplacePrincipal(
            owner_handle=record.owner_handle,
            scopes=tuple(record.scopes),
        )

    def require_principal(
        self,
        principal: MarketplacePrincipal | None,
        *,
        owner_handle: str | None = None,
    ) -> MarketplacePrincipal:
        if principal is None:
            raise MarketplaceAuthError("Missing valid Bearer token")
        if not principal.can_publish:
            raise MarketplaceForbiddenError("Token is missing publish/admin scope")
        if owner_handle and not self._may_manage(principal, owner_handle):
            raise MarketplaceForbiddenError(
                f"浠ょ墝韬唤 {principal.owner_handle} 鏃犳潈鎿嶄綔 owner {owner_handle}"
            )
        return principal

    def _may_manage(self, principal: MarketplacePrincipal, owner_handle: str) -> bool:
        return principal.is_admin or principal.owner_handle == owner_handle

    def _ensure_bootstrap_tokens(self) -> None:
        try:
            if self.settings.admin_token:
                self.repository.upsert_token(
                    self.settings.admin_token,
                    "admin",
                    scopes=("admin",),
                )
            for token, assignment in self.settings.owner_tokens.items():
                owner_handle, scope = assignment.split(":", 1)
                self.repository.upsert_token(token, owner_handle, scopes=(scope,))
        except Exception:  # noqa: BLE001 - bootstrap errors should be visible at startup
            logger.exception("Failed to bootstrap marketplace tokens")
            raise

    # ------------------------------------------------------------ dry-run validate

    def validate_bundle(self, data: bytes) -> dict[str, Any]:
        try:
            inspection = inspect_bundle(
                data,
                max_entry_count=self.settings.max_entry_count,
                max_total_uncompressed_bytes=self.settings.max_total_uncompressed_bytes,
            )
        except MarketplaceValidationError as exc:
            return {
                "valid": False,
                "error": str(exc),
            }
        return {
            "valid": True,
            **inspection.to_dict(),
        }

    # ------------------------------------------------------------------ publish

    def publish_version(
        self,
        owner_handle: str,
        name: str,
        data: bytes,
        *,
        version: str | None = None,
        visibility: str | None = None,
        principal: MarketplacePrincipal | None = None,
    ) -> dict[str, Any]:
        self.require_principal(principal, owner_handle=owner_handle)
        self._validate_owner_and_name(owner_handle, name)
        self.settings.validate_bundle_size(len(data))

        inspection = inspect_bundle(
            data,
            max_entry_count=self.settings.max_entry_count,
            max_total_uncompressed_bytes=self.settings.max_total_uncompressed_bytes,
        )
        if inspection.name != name:
            raise MarketplaceValidationError(
                f"Manifest name does not match package name: {inspection.name} != {name}"
            )
        resolved_version = (version or "").strip() or inspection.version
        if not resolved_version:
            raise MarketplaceValidationError("Missing version: request or manifest must provide a SemVer version")
        if not SEMVER_PATTERN.match(resolved_version):
            raise MarketplaceValidationError(f"Version must be SemVer: {resolved_version}")
        if visibility is not None and visibility not in PACKAGE_VISIBILITIES:
            raise MarketplaceValidationError(f"visibility must be one of: {'/'.join(sorted(PACKAGE_VISIBILITIES))}")

        content_hash = hashlib.sha256(data).hexdigest()
        lock = self._package_lock(owner_handle, name)
        with lock:
            package = self.repository.get_package(name)
            if package is None:
                package = self.repository.insert_package(
                    owner_handle=owner_handle,
                    name=name,
                    description=inspection.description,
                    category=inspection.category,
                    keywords=inspection.keywords,
                    visibility=visibility or "public",
                )
            else:
                if package.owner_handle != owner_handle and not (principal and principal.is_admin):
                    raise MarketplaceConflictError(f"Package {name} already belongs to owner {package.owner_handle}")
                if package.owner_handle != owner_handle:
                    raise MarketplaceForbiddenError(
                        f"Package {name} already belongs to owner {package.owner_handle}"
                    )
                if visibility is not None and visibility != package.visibility:
                    package = self.repository.update_package(name, visibility=visibility)
            if self.repository.get_version(name, resolved_version) is not None:
                raise MarketplaceConflictError(f"Version {resolved_version} already exists and cannot be overwritten")
            self.storage.save_bundle(owner_handle, name, resolved_version, data)
            record = self.repository.insert_version(
                package_name=name,
                version=resolved_version,
                manifest=dict(inspection.manifest),
                components=dict(inspection.components),
                content_hash=content_hash,
                size_bytes=len(data),
                storage_key=_bundle_key(owner_handle, name, resolved_version),
                published_by_owner_handle=principal.owner_handle if principal else "",
            )
            self.repository.recompute_latest_version(name)

        self.rebuild_snapshot()
        return record.to_dict()

    def list_packages(
        self,
        *,
        owner_handle: str | None = None,
        query: str = "",
        category: str = "",
        principal: MarketplacePrincipal | None = None,
    ) -> list[dict[str, Any]]:
        if principal and not principal.is_admin and owner_handle is None:
            public_packages = self.repository.list_packages(
                owner_handle=None,
                query=query,
                category=category,
                include_private=False,
            )
            own_packages = self.repository.list_packages(
                owner_handle=principal.owner_handle,
                query=query,
                category=category,
                include_private=True,
            )
            merged = {package.name: package for package in public_packages}
            for package in own_packages:
                merged[package.name] = package
            packages = list(merged.values())
        else:
            include_private = bool(principal and principal.is_admin)
            if owner_handle and principal and not principal.is_admin and principal.owner_handle == owner_handle:
                include_private = True
            packages = self.repository.list_packages(
                owner_handle=owner_handle,
                query=query,
                category=category,
                include_private=include_private,
            )
        return [self._package_view(package, principal) for package in packages]

    def get_package(
        self,
        owner_handle: str,
        name: str,
        principal: MarketplacePrincipal | None = None,
    ) -> dict[str, Any]:
        package = self.repository.get_package(name)
        if package is None or package.owner_handle != owner_handle:
            raise MarketplaceNotFoundError(f"Package {owner_handle}/{name} not found")
        if package.visibility == "private" and not self._may_read_private(
            principal or MarketplacePrincipal(""), package
        ):
            raise MarketplaceNotFoundError(f"Package {owner_handle}/{name} not found")
        versions = self.repository.list_versions(name)
        return {
            **self._package_view(package, principal),
            "versions": [version.to_dict() for version in versions],
        }

    def update_package(
        self,
        owner_handle: str,
        name: str,
        *,
        description: str | None = None,
        category: str | None = None,
        keywords: tuple[str, ...] | None = None,
        visibility: str | None = None,
        principal: MarketplacePrincipal | None = None,
    ) -> dict[str, Any]:
        self.require_principal(principal, owner_handle=owner_handle)
        package = self.repository.get_package(name)
        if package is None or package.owner_handle != owner_handle:
            raise MarketplaceNotFoundError(f"Package {owner_handle}/{name} not found")
        if visibility is not None and visibility not in PACKAGE_VISIBILITIES:
            raise MarketplaceValidationError(f"visibility must be one of: {'/'.join(sorted(PACKAGE_VISIBILITIES))}")
        updated = self.repository.update_package(
            name,
            description=description,
            category=category,
            keywords=keywords,
            visibility=visibility,
        )
        self.rebuild_snapshot()
        return self._package_view(updated, principal)

    # ---------------------------------------------------------------- deletion

    def set_version_status(
        self,
        owner_handle: str,
        name: str,
        version: str,
        status: str,
        *,
        principal: MarketplacePrincipal | None = None,
    ) -> dict[str, Any]:
        self.require_principal(principal, owner_handle=owner_handle)
        package = self._require_package(owner_handle, name)
        record = self.repository.get_version(package.name, version)
        if record is None:
            raise MarketplaceNotFoundError(f"Version {name}@{version} not found")
        if status not in {"published", "yanked"}:
            raise MarketplaceValidationError(f"status must be published or yanked: {status}")
        self.repository.set_version_status(package.name, version, status)
        self.repository.recompute_latest_version(package.name)
        self.rebuild_snapshot()
        updated = self.repository.get_version(package.name, version)
        return (updated or record).to_dict()

    def purge_version(
        self,
        owner_handle: str,
        name: str,
        version: str,
        *,
        principal: MarketplacePrincipal | None = None,
    ) -> dict[str, Any]:
        self.require_principal(principal, owner_handle=owner_handle)
        package = self._require_package(owner_handle, name)
        record = self.repository.get_version(package.name, version)
        if record is None:
            raise MarketplaceNotFoundError(f"Version {name}@{version} not found")
        self.repository.delete_version(package.name, version)
        self.storage.delete_bundle(owner_handle, name, version)
        self.repository.recompute_latest_version(package.name)
        self.rebuild_snapshot()
        return {"purged": True, "package": name, "version": version}

    def delete_package(
        self,
        owner_handle: str,
        name: str,
        *,
        principal: MarketplacePrincipal | None = None,
    ) -> dict[str, Any]:
        self.require_principal(principal, owner_handle=owner_handle)
        package = self._require_package(owner_handle, name)
        self.repository.delete_package(package.name)
        self.storage.delete_package_dir(owner_handle, package.name)
        self.rebuild_snapshot()
        return {"deleted": True, "package": name, "owner": owner_handle}

    # ---------------------------------------------------------------- download

    def version_download(
        self,
        owner_handle: str,
        name: str,
        version: str,
        *,
        principal: MarketplacePrincipal | None = None,
    ) -> tuple[Path, str]:
        package = self.repository.get_package(name)
        if package is None or package.owner_handle != owner_handle:
            raise MarketplaceNotFoundError(f"Package {owner_handle}/{name} not found")
        record = self.repository.get_version(package.name, version)
        if record is None:
            raise MarketplaceNotFoundError(f"Version {name}@{version} not found")
        if record.status == "yanked" and not self._may_manage(principal or MarketplacePrincipal(""), owner_handle):
            raise MarketplaceNotFoundError(f"Version {name}@{version} is not downloadable")
        if package.visibility == "private" and not self._may_read_private(
            principal or MarketplacePrincipal(""), package
        ):
            raise MarketplaceAuthError("Private package downloads require an owner token")
        path = self.storage.bundle_path(owner_handle, name, version)
        if not path.exists():
            raise MarketplaceNotFoundError(f"Version {name}@{version} not found")
        return path, f"{name}-{version}.zip"

    def read_public_plugin_file(self, name: str, file_path: str) -> bytes:
        if not KEBAB_CASE_PATTERN.match(name):
            raise MarketplaceNotFoundError(f"Plugin {name} not found")
        normalized = normalize_entry_path(file_path)
        package = self.repository.get_package(name)
        if package is None or package.visibility != "public":
            raise MarketplaceNotFoundError(f"Plugin {name} not found")
        record = self.repository.latest_published_version(package.name)
        if record is None:
            raise MarketplaceNotFoundError(f"Plugin {name} has no published version")
        try:
            data = self.storage.read_bundle(package.owner_handle, package.name, record.version)
        except FileNotFoundError as exc:
            raise MarketplaceNotFoundError(f"Plugin {name} bundle is missing") from exc
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if normalize_entry_path(info.filename) == normalized:
                    return archive.read(info)
        raise MarketplaceNotFoundError(f"Plugin file {name}/{normalized} not found")

    # -------------------------------------------------------------- distribution

    def catalog_document(self) -> dict[str, Any]:
        revision = self.storage.read_revision()
        if revision is not None:
            current = str(revision.get("revision") or "")
            if current:
                catalog = self.storage.read_catalog(current)
                if catalog is not None:
                    return catalog
        snapshot = self.repository.get_snapshot()
        if snapshot is not None and snapshot.catalog:
            return dict(snapshot.catalog)
        return self._build_catalog_document()[0]

    def snapshot_version(self) -> dict[str, Any]:
        revision = self.storage.read_revision()
        if revision is not None:
            return {
                "revision": str(revision.get("revision") or ""),
                "built_at": str(revision.get("built_at") or ""),
                "package_count": int(revision.get("package_count") or 0),
            }
        snapshot = self.repository.get_snapshot()
        if snapshot is not None:
            return {
                "revision": snapshot.revision,
                "built_at": snapshot.built_at,
                "package_count": snapshot.package_count,
            }
        return {"revision": "", "built_at": "", "package_count": 0}

    def snapshot_zip_path(self) -> Path:
        path = self.storage.current_snapshot_zip_path()
        if path is None:
            self.rebuild_snapshot()
            path = self.storage.current_snapshot_zip_path()
        if path is None or not path.exists():
            raise MarketplaceNotFoundError("Marketplace snapshot has not been generated")
        return path

    def rebuild_snapshot(self) -> dict[str, Any]:
        with self._snapshot_lock:
            catalog, package_count = self._build_catalog_document()
            serialized = json.dumps(catalog, ensure_ascii=False, sort_keys=True)
            revision = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
            existing = self.storage.read_revision()
            if (
                existing
                and existing.get("revision") == revision
                and self.storage.snapshot_zip_path_for_revision(revision).exists()
            ):
                self.repository.upsert_snapshot(
                    revision=revision,
                    catalog=catalog,
                    package_count=package_count,
                    built_at=str(existing.get("built_at") or ""),
                )
                return {"revision": revision, "package_count": package_count, "rebuilt": False}

            staging = self.storage.new_staging_dir()
            try:
                plugin_root = staging / "plugins"
                plugin_root.mkdir(parents=True, exist_ok=True)
                catalog_dir = staging / ".codebuddy-plugin"
                catalog_dir.mkdir(parents=True, exist_ok=True)
                (catalog_dir / "marketplace.json").write_text(
                    json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                packages = self.repository.list_packages(include_private=False)
                for package in packages:
                    record = self.repository.latest_published_version(package.name)
                    if record is None:
                        continue
                    try:
                        bundle = self.storage.read_bundle(
                            package.owner_handle, package.name, record.version
                        )
                    except FileNotFoundError:
                        continue
                    extract_bundle_to_dir(
                        bundle,
                        plugin_root / package.name,
                        max_total_uncompressed_bytes=self.settings.max_total_uncompressed_bytes,
                    )
                zip_bytes = self._zip_staging_tree(staging)
                built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                self.storage.write_snapshot_atomically(
                    zip_bytes=zip_bytes,
                    catalog=catalog,
                    revision=revision,
                    built_at=built_at,
                    package_count=package_count,
                )
                self.repository.upsert_snapshot(
                    revision=revision,
                    catalog=catalog,
                    package_count=package_count,
                    built_at=built_at,
                )
                if self.settings.git_mirror_enabled:
                    self._update_git_mirror(staging, revision)
                return {"revision": revision, "package_count": package_count, "rebuilt": True}
            finally:
                self.storage.cleanup_staging(staging)

    def _update_git_mirror(self, staging: Path, revision: str) -> None:
        """Refresh the git mirror after a successful snapshot swap.

        The mirror is an additive publish artifact: failures are logged and
        never fail the publish, and it only runs while the staging tree is
        still on disk.
        """

        try:
            tree_files = {
                path.relative_to(staging).as_posix(): path.read_bytes()
                for path in sorted(staging.rglob("*"))
                if path.is_file()
            }
            GitMirrorBuilder(self.storage.git_dir).update(
                tree_files,
                revision=revision,
                message=f"snapshot {revision}",
            )
        except Exception:  # noqa: BLE001 - git mirror must not break publishing
            logger.warning("Failed to update marketplace git mirror; snapshot is unaffected", exc_info=True)

    def git_repo_root(self) -> Path:
        return self.storage.git_dir.resolve()

    def _build_catalog_document(self) -> tuple[dict[str, Any], int]:
        entries: list[dict[str, Any]] = []
        packages = self.repository.list_packages(include_private=False)
        for package in packages:
            record = self.repository.latest_published_version(package.name)
            if record is None:
                continue
            entries.append(
                {
                    "name": package.name,
                    "source": f"./{PLUGIN_DIR_PREFIX}{package.name}",
                    "description": package.description or record.manifest.get("description") or "",
                    "version": record.version,
                    "author": {"name": package.owner_handle},
                    "keywords": list(package.keywords or record.manifest.get("keywords") or []),
                    "category": package.category or record.manifest.get("category") or "",
                    "strict": True,
                }
            )
        catalog = {
            "name": self.settings.marketplace_name,
            "owner": {"name": self.settings.owner_display_name},
            "description": self.settings.marketplace_description,
            "plugins": entries,
        }
        return catalog, len(entries)

    @staticmethod
    def _zip_staging_tree(staging: Path) -> bytes:
        import io

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staging).as_posix())
        return buffer.getvalue()

    # ------------------------------------------------------------------ helpers

    def _require_package(self, owner_handle: str, name: str) -> MarketplacePackage:
        package = self.repository.get_package(name)
        if package is None or package.owner_handle != owner_handle:
            raise MarketplaceNotFoundError(f"Package {owner_handle}/{name} not found")
        return package

    def _package_view(
        self,
        package: MarketplacePackage,
        principal: MarketplacePrincipal | None,
    ) -> dict[str, Any]:
        view = package.to_dict()
        view["components"] = self._latest_components(package)
        view["download_url"] = (
            f"/marketplace/packages/{package.owner_handle}/{package.name}/"
            f"versions/{package.latest_version}/download"
            if package.latest_version
            else ""
        )
        view["manageable"] = bool(principal and self._may_manage(principal, package.owner_handle))
        return view

    def _latest_components(self, package: MarketplacePackage) -> dict[str, Any]:
        if not package.latest_version:
            return dict(package.components)
        try:
            data = self.storage.read_bundle(
                package.owner_handle,
                package.name,
                package.latest_version,
            )
            inspection = inspect_bundle(
                data,
                max_entry_count=self.settings.max_entry_count,
                max_total_uncompressed_bytes=self.settings.max_total_uncompressed_bytes,
            )
            return dict(inspection.components)
        except Exception:  # noqa: BLE001 - package views should survive stale or missing bundles
            logger.debug(
                "Failed to refresh marketplace package components for %s/%s@%s",
                package.owner_handle,
                package.name,
                package.latest_version,
                exc_info=True,
            )
            return dict(package.components)
    def _may_read_private(self, principal: MarketplacePrincipal, package: MarketplacePackage) -> bool:
        return principal.is_admin or principal.owner_handle == package.owner_handle

    def _package_lock(self, owner_handle: str, name: str) -> threading.Lock:
        key = f"{owner_handle}/{name}"
        with self._package_locks_guard:
            if key not in self._package_locks:
                self._package_locks[key] = threading.Lock()
            return self._package_locks[key]

    @staticmethod
    def _validate_owner_and_name(owner_handle: str, name: str) -> None:
        if not KEBAB_CASE_PATTERN.match(owner_handle):
            raise MarketplaceValidationError(f"owner must be kebab-case: {owner_handle}")
        if not KEBAB_CASE_PATTERN.match(name):
            raise MarketplaceValidationError(f"package name must be kebab-case: {name}")


def _bundle_key(owner: str, name: str, version: str) -> str:
    return f"{owner}/{name}/{version}.zip"


