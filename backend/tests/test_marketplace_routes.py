import importlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.services.marketplace import (
    MarketplacePrincipal,
    MarketplaceService,
    MarketplaceSettings,
    MarketplaceStorage,
)
from app.services.marketplace.memory_marketplace_repository import InMemoryMarketplaceRepository
from tests.test_runtime_config import postgres_runtime_patches


def make_bundle(name: str = "code-review", version: str = "1.0.0") -> bytes:
    manifest = {"name": name, "version": version, "description": "璇勫鎶€鑳藉寘", "category": "dev-workflow"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(".codebuddy-plugin/plugin.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr("skills/review/SKILL.md", b"# review")
    return buffer.getvalue()


class MarketplaceRoutesTests(unittest.TestCase):
    def import_main(self):
        sys.modules.pop("app.main", None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmpdir = tmp.name
        env = {
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": "",
            "VECTOR_STORE_DIR": str(Path(tmpdir) / "vector_db"),
            "DATABASE_URL": "postgresql://rag:rag@localhost:5432/rag_test",
            "POSTGRES_SCHEMA": "rag",
            "RAG_DATA_DIR": str(Path(tmpdir) / "data"),
            "STORAGE_RUNTIME_LOCK": str(Path(tmpdir) / "vector_db" / "runtime.lock"),
            "AUTO_INGEST_ON_STARTUP": "false",
            "WIKI_INGEST_ENABLED": "false",
            "MARKETPLACE_STORAGE_DIR": str(Path(tmpdir) / "marketplace"),
        }
        with patch.dict(os.environ, env, clear=False):
            with postgres_runtime_patches():
                return importlib.import_module("app.main")

    def marketplace_rag_service(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = MarketplaceSettings(
            marketplace_name="bee-plugins",
            admin_token="admin-token",
            storage_dir=str(Path(tmp.name) / "marketplace"),
        )
        service = MarketplaceService(
            InMemoryMarketplaceRepository(),
            MarketplaceStorage(settings.storage_dir),
            settings,
        )
        return SimpleNamespace(marketplace_service=service, needs_reingest=lambda: False), service

    def test_marketplace_distribution_and_admin_routes(self):
        module = self.import_main()
        fake_rag, service = self.marketplace_rag_service()
        module.rag_service = fake_rag

        with TestClient(module.app) as client:
            empty_catalog = client.get("/marketplace/marketplace.json")
            publish = client.post(
                "/marketplace/packages/alice/code-review/versions",
                files={"file": ("code-review.zip", make_bundle(), "application/zip")},
                headers={"Authorization": "Bearer admin-token"},
            )
            catalog = client.get("/marketplace/marketplace.json")
            packages = client.get("/marketplace/packages")
            detail = client.get("/marketplace/packages/alice/code-review")
            snapshot_version = client.get("/marketplace/snapshot/version")
            snapshot_zip = client.get("/marketplace/snapshot.zip")
            plugin_manifest = client.get("/marketplace/plugins/code-review/.codebuddy-plugin/plugin.json")
            plugin_skill = client.get("/marketplace/plugins/code-review/skills/review/SKILL.md")
            download = client.get("/marketplace/packages/alice/code-review/versions/1.0.0/download")
            validate = client.post(
                "/marketplace/validate",
                files={"file": ("code-review.zip", make_bundle(version="2.0.0"), "application/zip")},
            )

        self.assertEqual(200, empty_catalog.status_code)
        self.assertEqual([], empty_catalog.json()["plugins"])

        self.assertEqual(200, publish.status_code, publish.text)
        self.assertEqual("1.0.0", publish.json()["version"])

        self.assertEqual(200, catalog.status_code)
        plugins = catalog.json()["plugins"]
        self.assertEqual(1, len(plugins))
        self.assertEqual("code-review", plugins[0]["name"])
        self.assertEqual("./plugins/code-review", plugins[0]["source"])

        self.assertEqual(200, packages.status_code)
        self.assertEqual("alice", packages.json()["items"][0]["owner"])

        self.assertEqual(200, detail.status_code)
        self.assertEqual(1, len(detail.json()["versions"]))
        self.assertEqual(["review"], detail.json()["components"]["skills"])

        self.assertEqual(200, snapshot_version.status_code)
        self.assertEqual(1, snapshot_version.json()["package_count"])

        self.assertEqual(200, snapshot_zip.status_code)
        self.assertTrue(snapshot_zip.headers.get("etag", "").strip('"'))
        self.assertIn("plugins/code-review/skills/review/SKILL.md", str(snapshot_zip.content))
        self.assertEqual(200, plugin_manifest.status_code)
        self.assertEqual("code-review", plugin_manifest.json()["name"])
        self.assertEqual(200, plugin_skill.status_code)
        self.assertEqual(b"# review", plugin_skill.content)

        self.assertEqual(200, download.status_code)
        self.assertTrue(download.content.startswith(b"PK"))

        self.assertEqual(200, validate.status_code)
        self.assertTrue(validate.json()["valid"])
        self.assertEqual("2.0.0", validate.json()["version"])

    def test_marketplace_auth_and_conflict_responses(self):
        module = self.import_main()
        fake_rag, service = self.marketplace_rag_service()
        module.rag_service = fake_rag

        with TestClient(module.app) as client:
            publish = client.post(
                "/marketplace/packages/alice/code-review/versions",
                files={"file": ("code-review.zip", make_bundle(), "application/zip")},
                headers={"Authorization": "Bearer admin-token"},
            )
            missing_token = client.post(
                "/marketplace/packages/alice/second/versions",
                files={"file": ("second.zip", make_bundle(name="second"), "application/zip")},
            )
            foreign_owner = client.post(
                "/marketplace/packages/bob/code-review/versions",
                files={"file": ("code-review.zip", make_bundle(version="9.0.0"), "application/zip")},
                headers={"Authorization": "Bearer token-alice"},
            )
            duplicate_version = client.post(
                "/marketplace/packages/alice/code-review/versions",
                files={"file": ("code-review.zip", make_bundle(), "application/zip")},
                headers={"Authorization": "Bearer admin-token"},
            )
            unknown_download = client.get("/marketplace/packages/alice/code-review/versions/9.9.9/download")
            unknown_detail = client.get("/marketplace/packages/alice/missing")
            yanked = client.delete(
                "/marketplace/packages/alice/code-review/versions/1.0.0",
                headers={"Authorization": "Bearer admin-token"},
            )
            yanked_download = client.get("/marketplace/packages/alice/code-review/versions/1.0.0/download")
            restored = client.post(
                "/marketplace/packages/alice/code-review/versions/1.0.0/restore",
                headers={"Authorization": "Bearer admin-token"},
            )
            purged = client.delete(
                "/marketplace/packages/alice/code-review/versions/1.0.0?mode=purge",
                headers={"Authorization": "Bearer admin-token"},
            )
            visibility = client.patch(
                "/marketplace/packages/alice/code-review",
                json={"visibility": "private"},
                headers={"Authorization": "Bearer admin-token"},
            )
            private_catalog = client.get("/marketplace/marketplace.json")
            private_plugin_file = client.get("/marketplace/plugins/code-review/.codebuddy-plugin/plugin.json")
            package_deleted = client.delete(
                "/marketplace/packages/alice/code-review",
                headers={"Authorization": "Bearer admin-token"},
            )

        self.assertEqual(200, publish.status_code)
        self.assertEqual(401, missing_token.status_code)
        self.assertEqual(401, foreign_owner.status_code)
        self.assertEqual(409, duplicate_version.status_code)
        self.assertEqual(404, unknown_download.status_code)
        self.assertEqual(404, unknown_detail.status_code)
        self.assertEqual(200, yanked.status_code)
        self.assertEqual("yanked", yanked.json()["status"])
        self.assertEqual(404, yanked_download.status_code)
        self.assertEqual(200, restored.status_code)
        self.assertEqual(200, purged.status_code)
        self.assertTrue(purged.json()["purged"])
        self.assertEqual(200, visibility.status_code)
        self.assertEqual("private", visibility.json()["visibility"])
        self.assertEqual([], private_catalog.json()["plugins"])
        self.assertEqual(404, private_plugin_file.status_code)
        self.assertEqual(200, package_deleted.status_code)
        self.assertTrue(package_deleted.json()["deleted"])

    def test_marketplace_owner_scoped_publish_token(self):
        module = self.import_main()
        fake_rag, service = self.marketplace_rag_service()
        module.rag_service = fake_rag
        service.repository.upsert_token("alice-publish-token", "alice", scopes=("publish",))

        with TestClient(module.app) as client:
            publish = client.post(
                "/marketplace/packages/alice/code-review/versions",
                files={"file": ("code-review.zip", make_bundle(), "application/zip")},
                headers={"Authorization": "Bearer alice-publish-token"},
            )
            private_toggle = client.patch(
                "/marketplace/packages/alice/code-review",
                json={"visibility": "private"},
                headers={"Authorization": "Bearer alice-publish-token"},
            )
            packages = client.get("/marketplace/packages")
            owner_packages = client.get(
                "/marketplace/packages",
                headers={"Authorization": "Bearer alice-publish-token"},
            )

        self.assertEqual(200, publish.status_code, publish.text)
        self.assertEqual(200, private_toggle.status_code)
        self.assertEqual([], packages.json()["items"])
        self.assertEqual(1, len(owner_packages.json()["items"]))
        self.assertEqual("private", owner_packages.json()["items"][0]["visibility"])


if __name__ == "__main__":
    unittest.main()

