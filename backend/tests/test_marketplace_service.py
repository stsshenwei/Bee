import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.services.marketplace import (
    MarketplaceConflictError,
    MarketplaceForbiddenError,
    MarketplaceAuthError,
    MarketplaceNotFoundError,
    MarketplacePrincipal,
    MarketplaceService,
    MarketplaceSettings,
    MarketplaceStorage,
    MarketplaceValidationError,
)
from app.services.marketplace.memory_marketplace_repository import InMemoryMarketplaceRepository


def make_bundle(
    *,
    name: str = "code-review",
    version: str = "1.0.0",
    manifest: dict | None = None,
    files: dict[str, bytes] | None = None,
    raw_entries: dict[str, bytes] | None = None,
    symlink_entries: dict[str, bytes] | None = None,
    manifest_path: str = ".codebuddy-plugin/plugin.json",
) -> bytes:
    payload = manifest if manifest is not None else {
        "name": name,
        "version": version,
        "description": "璇勫鎶€鑳藉寘",
        "category": "dev-workflow",
        "keywords": ["review", "quality"],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(manifest_path, json.dumps(payload, ensure_ascii=False))
        for path, data in (files or {
            "skills/review/SKILL.md": b"---\ndescription: Reviews code changes and summarizes quality risks.\n---\n# review\n",
            "commands/review.md": b"run review",
            "rules/review.md": b"# rule",
            ".mcp.json": b'{"mcpServers": {}}',
        }).items():
            archive.writestr(path, data)
        for path, data in (raw_entries or {}).items():
            archive.writestr(path, data)
        for path, data in (symlink_entries or {}).items():
            info = zipfile.ZipInfo(path)
            info.external_attr = 0o120777 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def admin_principal() -> MarketplacePrincipal:
    return MarketplacePrincipal(owner_handle="admin", scopes=("admin",))


class MarketplaceServiceTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.storage_root = Path(tmp.name)
        self.repository = InMemoryMarketplaceRepository()
        self.settings = MarketplaceSettings(
            marketplace_name="bee-plugins",
            storage_dir=str(self.storage_root),
            admin_token="admin-token",
        )
        self.service = MarketplaceService(
            self.repository,
            MarketplaceStorage(self.settings.storage_dir),
            self.settings,
        )
        self.principal = MarketplacePrincipal(owner_handle="alice", scopes=("publish",))


class MarketplacePublishTests(MarketplaceServiceTestBase):
    def test_publish_creates_package_version_and_snapshot(self):
        record = self.service.publish_version(
            "alice", "code-review", make_bundle(), principal=self.principal
        )
        self.assertEqual("1.0.0", record["version"])
        self.assertEqual("published", record["status"])
        package = self.repository.get_package("code-review")
        self.assertIsNotNone(package)
        self.assertEqual("alice", package.owner_handle)
        self.assertEqual("1.0.0", package.latest_version)
        snapshot_zip = self.storage_root / "snapshots" / "snapshot.zip"
        self.assertTrue(snapshot_zip.exists())
        with zipfile.ZipFile(snapshot_zip) as archive:
            names = set(archive.namelist())
        self.assertIn(".codebuddy-plugin/marketplace.json", names)
        self.assertIn("plugins/code-review/skills/review/SKILL.md", names)
        self.assertIn("plugins/code-review/.codebuddy-plugin/plugin.json", names)

    def test_catalog_lists_public_package_with_relative_source(self):
        self.service.publish_version("alice", "code-review", make_bundle(), principal=self.principal)
        catalog = self.service.catalog_document()
        self.assertEqual("bee-plugins", catalog["name"])
        entries = catalog["plugins"]
        self.assertEqual(1, len(entries))
        self.assertEqual("code-review", entries[0]["name"])
        self.assertEqual("./plugins/code-review", entries[0]["source"])
        self.assertEqual("alice", entries[0]["author"]["name"])

    def test_catalog_relative_source_file_can_be_served(self):
        self.service.publish_version("alice", "code-review", make_bundle(), principal=self.principal)
        manifest = self.service.read_public_plugin_file("code-review", ".codebuddy-plugin/plugin.json")
        skill = self.service.read_public_plugin_file("code-review", "skills/review/SKILL.md")
        self.assertEqual("code-review", json.loads(manifest.decode("utf-8"))["name"])
        self.assertIn(b"# review", skill)

    def test_manifest_name_mismatch_rejected(self):
        with self.assertRaises(MarketplaceValidationError):
            self.service.publish_version(
                "alice", "other-name", make_bundle(name="code-review"), principal=self.principal
            )

    def test_duplicate_version_conflict(self):
        self.service.publish_version("alice", "code-review", make_bundle(), principal=self.principal)
        with self.assertRaises(MarketplaceConflictError):
            self.service.publish_version("alice", "code-review", make_bundle(), principal=self.principal)

    def test_duplicate_name_conflict_across_owners(self):
        self.service.publish_version("alice", "code-review", make_bundle(), principal=self.principal)
        bob = MarketplacePrincipal(owner_handle="bob", scopes=("publish",))
        with self.assertRaises(MarketplaceConflictError):
            self.service.publish_version("bob", "code-review", make_bundle(), principal=bob)

    def test_missing_manifest_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("skills/review/SKILL.md", b"# review")
        with self.assertRaises(MarketplaceValidationError):
            self.service.publish_version("alice", "code-review", buffer.getvalue(), principal=self.principal)

    def test_version_must_be_semver(self):
        with self.assertRaises(MarketplaceValidationError):
            self.service.publish_version(
                "alice",
                "code-review",
                make_bundle(version="not-semver"),
                version="v1",
                principal=self.principal,
            )

    def test_bundle_size_limit_rejected(self):
        small = MarketplaceSettings(
            marketplace_name="bee-plugins",
            storage_dir=str(self.storage_root / "small"),
            max_upload_bytes=16,
        )
        service = MarketplaceService(
            self.repository,
            MarketplaceStorage(small.storage_dir),
            small,
        )
        with self.assertRaises(MarketplaceValidationError):
            service.publish_version(
                "alice", "code-review", make_bundle(), principal=self.principal
            )


class MarketplaceSecurityTests(MarketplaceServiceTestBase):
    def test_zip_slip_entry_rejected(self):
        with self.assertRaises(MarketplaceValidationError):
            self.service.publish_version(
                "alice",
                "code-review",
                make_bundle(raw_entries={"../evil.txt": b"escaped"}),
                principal=self.principal,
            )

    def test_absolute_path_entry_rejected(self):
        with self.assertRaises(MarketplaceValidationError):
            self.service.publish_version(
                "alice",
                "code-review",
                make_bundle(raw_entries={"/etc/evil.txt": b"escaped"}),
                principal=self.principal,
            )

    def test_symlink_entry_rejected(self):
        with self.assertRaises(MarketplaceValidationError):
            self.service.publish_version(
                "alice",
                "code-review",
                make_bundle(symlink_entries={"evil-link": b"../../target"}),
                principal=self.principal,
            )

    def test_publish_requires_token(self):
        with self.assertRaises(MarketplaceAuthError):
            self.service.publish_version("alice", "code-review", make_bundle(), principal=None)

    def test_foreign_owner_forbidden(self):
        bob = MarketplacePrincipal(owner_handle="bob", scopes=("publish",))
        with self.assertRaises(MarketplaceForbiddenError):
            self.service.publish_version("alice", "code-review", make_bundle(), principal=bob)

    def test_admin_can_publish_for_any_owner(self):
        record = self.service.publish_version(
            "alice", "code-review", make_bundle(), principal=admin_principal()
        )
        self.assertEqual("1.0.0", record["version"])


class MarketplaceDryRunTests(MarketplaceServiceTestBase):
    def test_validate_returns_manifest_and_components_without_persisting(self):
        result = self.service.validate_bundle(make_bundle())
        self.assertTrue(result["valid"])
        self.assertEqual("code-review", result["name"])
        self.assertEqual("1.0.0", result["version"])
        self.assertEqual(["review"], result["components"]["skills"])
        self.assertEqual(
            "Reviews code changes and summarizes quality risks.",
            result["components"]["skill_details"][0]["description"],
        )
        self.assertEqual(["review"], result["components"]["commands"])
        self.assertEqual(["review"], result["components"]["rules"])
        self.assertTrue(result["components"]["mcp"])
        self.assertEqual([], self.repository.list_packages())

    def test_validate_detects_root_skill_file(self):
        result = self.service.validate_bundle(
            make_bundle(files={"SKILL.md": b"---\ndescription: Root browser skill.\n---\n# skill"})
        )
        self.assertTrue(result["valid"])
        self.assertEqual(["code-review"], result["components"]["skills"])
        self.assertEqual("Root browser skill.", result["components"]["skill_details"][0]["description"])

    def test_validate_accepts_supported_manifest_formats(self):
        cases = (
            (".codebuddy-plugin/plugin.json", "codebuddy"),
            (".codex-plugin/plugin.json", "codex"),
            (".qoder-plugin/plugin.json", "qoder"),
            (".qder-plugin/plugin.json", "qder"),
            ("plugin.json", "generic"),
        )
        for manifest_path, expected_format in cases:
            with self.subTest(manifest_path=manifest_path):
                result = self.service.validate_bundle(make_bundle(manifest_path=manifest_path))
                self.assertTrue(result["valid"])
                self.assertEqual("code-review", result["name"])
                self.assertEqual(expected_format, result["components"]["format"])
                self.assertEqual(manifest_path, result["components"]["manifest_path"])

    def test_validate_reports_invalid_bundle(self):
        result = self.service.validate_bundle(b"not a zip")
        self.assertFalse(result["valid"])
        self.assertTrue(result["error"])

    def test_validate_missing_manifest(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("README.md", b"nothing here")
        result = self.service.validate_bundle(buffer.getvalue())
        self.assertFalse(result["valid"])


class MarketplaceVersionLifecycleTests(MarketplaceServiceTestBase):
    def setUp(self):
        super().setUp()
        self.service.publish_version("alice", "code-review", make_bundle(), principal=self.principal)
        self.service.publish_version(
            "alice",
            "code-review",
            make_bundle(version="1.1.0"),
            principal=self.principal,
        )

    def test_latest_version_is_newest_publish(self):
        package = self.repository.get_package("code-review")
        self.assertEqual("1.1.0", package.latest_version)

    def test_yank_hides_version_from_catalog_and_restore_recovers(self):
        self.service.set_version_status("alice", "code-review", "1.1.0", "yanked", principal=self.principal)
        catalog = self.service.catalog_document()
        self.assertEqual("1.0.0", catalog["plugins"][0]["version"])

        self.service.set_version_status("alice", "code-review", "1.1.0", "published", principal=self.principal)
        catalog = self.service.catalog_document()
        self.assertEqual("1.1.0", catalog["plugins"][0]["version"])

    def test_purge_removes_version_and_payload(self):
        self.service.purge_version("alice", "code-review", "1.1.0", principal=self.principal)
        self.assertIsNone(self.repository.get_version("code-review", "1.1.0"))
        self.assertFalse(
            (self.storage_root / "bundles" / "alice" / "code-review" / "1.1.0.zip").exists()
        )
        with self.assertRaises(MarketplaceNotFoundError):
            self.service.purge_version("alice", "code-review", "1.1.0", principal=self.principal)

    def test_delete_package_removes_everything(self):
        self.service.delete_package("alice", "code-review", principal=self.principal)
        self.assertIsNone(self.repository.get_package("code-review"))
        self.assertEqual([], self.service.catalog_document()["plugins"])

    def test_version_management_requires_token(self):
        with self.assertRaises(MarketplaceAuthError):
            self.service.set_version_status("alice", "code-review", "1.1.0", "yanked", principal=None)
        with self.assertRaises(MarketplaceForbiddenError):
            bob = MarketplacePrincipal(owner_handle="bob", scopes=("publish",))
            self.service.set_version_status("alice", "code-review", "1.1.0", "yanked", principal=bob)


class MarketplaceVisibilityTests(MarketplaceServiceTestBase):
    def test_private_package_excluded_from_public_catalog(self):
        self.service.publish_version(
            "alice", "code-review", make_bundle(), visibility="private", principal=self.principal
        )
        self.assertEqual([], self.service.catalog_document()["plugins"])
        snapshot_version = self.service.snapshot_version()
        self.assertEqual(0, snapshot_version["package_count"])
        with self.assertRaises(MarketplaceNotFoundError):
            self.service.read_public_plugin_file("code-review", ".codebuddy-plugin/plugin.json")

    def test_owner_global_list_includes_own_private_packages(self):
        self.service.publish_version(
            "alice", "code-review", make_bundle(), visibility="private", principal=self.principal
        )
        anonymous = self.service.list_packages()
        owned = self.service.list_packages(principal=self.principal)
        self.assertEqual([], anonymous)
        self.assertEqual(1, len(owned))
        self.assertEqual("private", owned[0]["visibility"])

    def test_visibility_toggle_updates_catalog(self):
        self.service.publish_version(
            "alice", "code-review", make_bundle(), visibility="private", principal=self.principal
        )
        self.service.update_package(
            "alice", "code-review", visibility="public", principal=self.principal
        )
        catalog = self.service.catalog_document()
        self.assertEqual(1, len(catalog["plugins"]))

    def test_private_download_requires_owner_token(self):
        self.service.publish_version(
            "alice", "code-review", make_bundle(), visibility="private", principal=self.principal
        )
        with self.assertRaises(MarketplaceAuthError):
            self.service.version_download("alice", "code-review", "1.0.0", principal=None)
        path, filename = self.service.version_download(
            "alice", "code-review", "1.0.0", principal=self.principal
        )
        self.assertTrue(path.exists())
        self.assertEqual("code-review-1.0.0.zip", filename)

    def test_private_package_hidden_from_anonymous_detail(self):
        self.service.publish_version(
            "alice", "code-review", make_bundle(), visibility="private", principal=self.principal
        )
        with self.assertRaises(MarketplaceNotFoundError):
            self.service.get_package("alice", "code-review", principal=None)
        detail = self.service.get_package("alice", "code-review", principal=self.principal)
        self.assertEqual(1, len(detail["versions"]))


class MarketplaceSnapshotTests(MarketplaceServiceTestBase):
    def test_revision_stable_when_catalog_unchanged(self):
        self.service.publish_version("alice", "code-review", make_bundle(), principal=self.principal)
        first = self.service.snapshot_version()
        result = self.service.rebuild_snapshot()
        self.assertFalse(result["rebuilt"])
        second = self.service.snapshot_version()
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(first["built_at"], second["built_at"])

    def test_revision_changes_after_publish(self):
        empty = self.service.snapshot_version()
        self.service.publish_version("alice", "code-review", make_bundle(), principal=self.principal)
        after = self.service.snapshot_version()
        self.assertNotEqual(empty["revision"], after["revision"])
        self.assertEqual(1, after["package_count"])

    def test_snapshot_zip_matches_catalog(self):
        self.service.publish_version("alice", "code-review", make_bundle(), principal=self.principal)
        catalog = self.service.catalog_document()
        snapshot_zip = self.storage_root / "snapshots" / "snapshot.zip"
        with zipfile.ZipFile(snapshot_zip) as archive:
            embedded = json.loads(archive.read(".codebuddy-plugin/marketplace.json").decode("utf-8"))
        self.assertEqual(catalog["plugins"], embedded["plugins"])

    def test_snapshot_rebuild_failure_keeps_last_good(self):
        self.service.publish_version("alice", "code-review", make_bundle(), principal=self.principal)
        self.service.publish_version(
            "alice", "code-review", make_bundle(version="2.0.0"), principal=self.principal
        )
        second = self.service.snapshot_version()

        original_zipper = self.service._zip_staging_tree

        def broken_zipper(staging):
            raise RuntimeError("snapshot rebuild failed")

        self.service._zip_staging_tree = broken_zipper
        try:
            with self.assertRaises(RuntimeError):
                self.service.publish_version(
                    "alice", "code-review", make_bundle(version="3.0.0"), principal=self.principal
                )
        finally:
            self.service._zip_staging_tree = original_zipper

        # 鍙戝竷澶辫触鍚庯紝纾佺洏蹇収淇濇寔 last-good锛屽厓鏁版嵁涓庡凡鍙戝竷鐗堟湰涓€鑷?
        after_failure = self.service.snapshot_version()
        self.assertEqual(second["revision"], after_failure["revision"])
        self.assertEqual(second["built_at"], after_failure["built_at"])

        # 鍚庣画鍙戝竷鎭㈠姝ｅ父閲嶅缓
        result = self.service.publish_version(
            "alice", "code-review", make_bundle(version="3.1.0"), principal=self.principal
        )
        self.assertEqual("3.1.0", result["version"])
        self.assertNotEqual(second["revision"], self.service.snapshot_version()["revision"])


if __name__ == "__main__":
    unittest.main()
