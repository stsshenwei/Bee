import tempfile
import functools
import http.server
import shutil
import subprocess
import threading
import unittest
import zlib
from pathlib import Path

from fastapi.testclient import TestClient

from app.services.marketplace import (
    MarketplaceService,
    MarketplaceSettings,
    MarketplaceStorage,
    resolve_repo_file,
)
from app.services.marketplace.marketplace_git import GitMirrorBuilder, read_repo_refs
from app.services.marketplace.memory_marketplace_repository import InMemoryMarketplaceRepository
from tests.test_marketplace_service import make_bundle


def _is_loose_object(path: Path) -> bool:
    """Loose objects live at ``objects/<2-hex>/<38-hex>``."""

    if not path.is_file():
        return False
    return (
        len(path.parent.name) == 2
        and path.parent.parent.name == "objects"
        and path.parent.name.isalnum()
    )


class MarketplaceGitTestBase(unittest.TestCase):
    def build_service(self, *, git_enabled: bool = True) -> tuple[MarketplaceService, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        settings = MarketplaceSettings(
            marketplace_name="bee-plugins",
            storage_dir=str(root / "marketplace"),
            admin_token="admin-token",
            git_mirror_enabled=git_enabled,
        )
        service = MarketplaceService(
            InMemoryMarketplaceRepository(),
            MarketplaceStorage(settings.storage_dir),
            settings,
        )
        return service, root


class MarketplaceGitMirrorTests(MarketplaceGitTestBase):
    def test_publish_creates_git_mirror(self):
        service, _ = self.build_service()
        service.publish_version("alice", "code-review", make_bundle(), principal=service.resolve_principal("Bearer admin-token"))
        repo = service.storage.git_dir
        self.assertTrue((repo / "HEAD").exists())
        refs = read_repo_refs(repo)
        revision = service.snapshot_version()["revision"]
        self.assertIn("refs/heads/main", refs)
        self.assertIn(f"refs/tags/snapshot-{revision}", refs)
        self.assertEqual(refs["refs/heads/main"], refs[f"refs/tags/snapshot-{revision}"])
        info_refs = (repo / "info" / "refs").read_text(encoding="utf-8")
        self.assertIn("refs/heads/main", info_refs)
        self.assertIn(f"refs/tags/snapshot-{revision}", info_refs)

    def test_publish_creates_per_plugin_git_mirrors(self):
        service, _ = self.build_service()
        service.publish_version("alice", "code-review", make_bundle(), principal=service.resolve_principal("Bearer admin-token"))
        service.publish_version(
            "alice",
            "agent-browser",
            make_bundle(name="agent-browser", files={"skills/browser/SKILL.md": b"# browser\n"}),
            principal=service.resolve_principal("Bearer admin-token"),
        )

        code_repo = service.plugin_git_repo_root("code-review")
        browser_repo = service.plugin_git_repo_root("agent-browser")

        self.assertTrue((code_repo / "HEAD").exists())
        self.assertTrue((browser_repo / "HEAD").exists())
        self.assertIn("refs/heads/main", (code_repo / "info" / "refs").read_text(encoding="utf-8"))
        self.assertIn("refs/heads/main", (browser_repo / "info" / "refs").read_text(encoding="utf-8"))
        self.assertNotEqual(read_repo_refs(code_repo)["refs/heads/main"], read_repo_refs(browser_repo)["refs/heads/main"])

    def test_plugin_git_mirror_can_be_cloned_over_dumb_http(self):
        if shutil.which("git") is None:
            self.skipTest("git CLI is required for dumb HTTP clone verification")
        service, root = self.build_service()
        service.publish_version("alice", "code-review", make_bundle(), principal=service.resolve_principal("Bearer admin-token"))
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler,
            directory=str(service.storage.plugin_git_dir),
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        clone_dir = root / "clone"
        url = f"http://127.0.0.1:{server.server_address[1]}/code-review.git"

        result = subprocess.run(
            ["git", "clone", url, str(clone_dir)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            "main",
            subprocess.check_output(["git", "-C", str(clone_dir), "branch", "--show-current"], text=True).strip(),
        )
        self.assertTrue((clone_dir / ".codebuddy-plugin" / "plugin.json").exists(), result.stderr)
        self.assertTrue((clone_dir / "skills" / "review" / "SKILL.md").exists(), result.stderr)
        self.assertFalse((clone_dir / "plugins").exists(), result.stderr)
        self.assertNotIn("remote HEAD refers to nonexistent ref", result.stderr)

    def test_rebuild_refreshes_missing_plugin_mirrors_when_revision_is_unchanged(self):
        service, _ = self.build_service(git_enabled=False)
        service.publish_version("alice", "code-review", make_bundle(), principal=service.resolve_principal("Bearer admin-token"))
        revision = service.snapshot_version()["revision"]

        settings = MarketplaceSettings(
            marketplace_name="bee-plugins",
            storage_dir=service.settings.storage_dir,
            admin_token="admin-token",
            git_mirror_enabled=True,
        )
        refreshed = MarketplaceService(
            service.repository,
            MarketplaceStorage(settings.storage_dir),
            settings,
        )
        result = refreshed.rebuild_snapshot()

        self.assertEqual(revision, result["revision"])
        self.assertTrue((refreshed.plugin_git_repo_root("code-review") / "HEAD").exists())

    def test_rebuild_refreshes_crlf_git_metadata_when_revision_is_unchanged(self):
        service, _ = self.build_service()
        service.publish_version("alice", "code-review", make_bundle(), principal=service.resolve_principal("Bearer admin-token"))
        revision = service.snapshot_version()["revision"]
        info_refs = service.plugin_git_repo_root("code-review") / "info" / "refs"
        info_refs.write_bytes(info_refs.read_bytes().replace(b"\n", b"\r\n"))

        settings = MarketplaceSettings(
            marketplace_name="bee-plugins",
            storage_dir=service.settings.storage_dir,
            admin_token="admin-token",
            git_mirror_enabled=True,
        )
        refreshed = MarketplaceService(
            service.repository,
            MarketplaceStorage(settings.storage_dir),
            settings,
        )
        result = refreshed.rebuild_snapshot()

        self.assertEqual(revision, result["revision"])
        self.assertNotIn(b"\r\n", info_refs.read_bytes())

    def test_blob_objects_store_verifiable_content(self):
        service, _ = self.build_service()
        skill_md = b"# review skill\n"
        service.publish_version(
            "alice",
            "code-review",
            make_bundle(files={"skills/review/SKILL.md": skill_md}),
            principal=service.resolve_principal("Bearer admin-token"),
        )
        repo = service.storage.git_dir
        found = False
        for object_file in (repo / "objects").rglob("*"):
            if not _is_loose_object(object_file):
                continue
            store = zlib.decompress(object_file.read_bytes())
            if store.startswith(b"blob "):
                header, _, content = store.partition(b"\x00")
                if content == skill_md:
                    length = int(header.split(b" ")[1])
                    self.assertEqual(len(skill_md), length)
                    found = True
        self.assertTrue(found, "未找到包含 SKILL.md 内容的 blob 对象")

    def test_commit_references_root_tree(self):
        service, _ = self.build_service()
        service.publish_version("alice", "code-review", make_bundle(), principal=service.resolve_principal("Bearer admin-token"))
        repo = service.storage.git_dir
        main_sha = read_repo_refs(repo)["refs/heads/main"]
        commit_store = zlib.decompress(
            (repo / "objects" / main_sha[:2] / main_sha[2:]).read_bytes()
        )
        self.assertTrue(commit_store.startswith(b"commit "))
        _, _, content = commit_store.partition(b"\x00")
        self.assertIn(b"tree ", content.split(b"\n")[0])
        self.assertIn(b"snapshot ", content)

    def test_tree_entry_uses_git_sorting_and_modes(self):
        builder = GitMirrorBuilder(Path(tempfile.mkdtemp()))
        files = {
            ".codebuddy-plugin/plugin.json": b"{}",
            "plugins/code-review/skills/review/SKILL.md": b"# review",
            "plugins/zzz/readme.md": b"x",
        }
        builder.update(files, revision="abc", message="snapshot abc")
        # 找到包含 plugins 目录条目的根 tree
        root_tree_sha = None
        for object_file in builder.repo_dir.rglob("objects/*/*"):
            if not _is_loose_object(object_file):
                continue
            store = zlib.decompress(object_file.read_bytes())
            if store.startswith(b"commit "):
                first_line = store.partition(b"\x00")[2].split(b"\n")[0]
                root_tree_sha = first_line.split()[1].decode()
        self.assertIsNotNone(root_tree_sha)
        tree_store = zlib.decompress(
            (builder.repo_dir / "objects" / root_tree_sha[:2] / root_tree_sha[2:]).read_bytes()
        )
        _, _, body = tree_store.partition(b"\x00")
        # 目录条目使用 40000 模式且按 "name/" 排序：.codebuddy-plugin < plugins
        self.assertIn(b"40000 plugins", body)
        self.assertLess(body.index(b".codebuddy-plugin"), body.index(b"plugins"))

    def test_mirror_tracks_new_revision_and_keeps_old_tags(self):
        service, _ = self.build_service()
        service.publish_version("alice", "code-review", make_bundle(), principal=service.resolve_principal("Bearer admin-token"))
        first_revision = service.snapshot_version()["revision"]
        first_main = read_repo_refs(service.storage.git_dir)["refs/heads/main"]
        service.publish_version(
            "alice",
            "code-review",
            make_bundle(version="2.0.0"),
            principal=service.resolve_principal("Bearer admin-token"),
        )
        second_revision = service.snapshot_version()["revision"]
        self.assertNotEqual(first_revision, second_revision)
        refs = read_repo_refs(service.storage.git_dir)
        self.assertIn(f"refs/tags/snapshot-{first_revision}", refs)
        self.assertIn(f"refs/tags/snapshot-{second_revision}", refs)
        self.assertNotEqual(first_main, refs["refs/heads/main"])

    def test_failed_snapshot_rebuild_keeps_previous_refs(self):
        service, _ = self.build_service()
        service.publish_version("alice", "code-review", make_bundle(), principal=service.resolve_principal("Bearer admin-token"))
        refs_before = read_repo_refs(service.storage.git_dir)
        original_zipper = service._zip_staging_tree

        def broken_zipper(staging):
            raise RuntimeError("snapshot rebuild failed")

        service._zip_staging_tree = broken_zipper
        try:
            with self.assertRaises(RuntimeError):
                service.publish_version(
                    "alice",
                    "code-review",
                    make_bundle(version="2.0.0"),
                    principal=service.resolve_principal("Bearer admin-token"),
                )
        finally:
            service._zip_staging_tree = original_zipper
        self.assertEqual(refs_before, read_repo_refs(service.storage.git_dir))

    def test_git_mirror_can_be_disabled(self):
        service, _ = self.build_service(git_enabled=False)
        service.publish_version("alice", "code-review", make_bundle(), principal=service.resolve_principal("Bearer admin-token"))
        self.assertFalse((service.storage.git_dir / "HEAD").exists())

    def test_resolve_repo_file_rejects_traversal(self):
        service, _ = self.build_service()
        service.publish_version("alice", "code-review", make_bundle(), principal=service.resolve_principal("Bearer admin-token"))
        root = service.git_repo_root()
        self.assertIsNone(resolve_repo_file(root, "../secret.txt"))
        self.assertIsNone(resolve_repo_file(root, "..\\..\\secret.txt"))
        self.assertIsNone(resolve_repo_file(root, "/etc/passwd"))
        self.assertIsNone(resolve_repo_file(root, ""))
        self.assertIsNotNone(resolve_repo_file(root, "HEAD"))
        self.assertIsNotNone(resolve_repo_file(root, "info/refs"))


class MarketplaceGitRouteTests(MarketplaceGitTestBase):
    def import_main_with_service(self, service: MarketplaceService):
        import importlib
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch
        import os

        from tests.test_runtime_config import postgres_runtime_patches

        sys.modules.pop("app.main", None)
        with tempfile.TemporaryDirectory() as envdir:
            env = {
                "OPENAI_API_KEY": "test-key",
                "DATABASE_URL": "postgresql://rag:rag@localhost:5432/rag_test",
                "POSTGRES_SCHEMA": "rag",
                "RAG_DATA_DIR": str(Path(envdir) / "data"),
                "VECTOR_STORE_DIR": str(Path(envdir) / "vector_db"),
                "STORAGE_RUNTIME_LOCK": str(Path(envdir) / "vector_db" / "runtime.lock"),
                "AUTO_INGEST_ON_STARTUP": "false",
                "WIKI_INGEST_ENABLED": "false",
                "MARKETPLACE_STORAGE_DIR": str(Path(envdir) / "marketplace"),
            }
            with patch.dict(os.environ, env, clear=False):
                with postgres_runtime_patches():
                    module = importlib.import_module("app.main")
        module.rag_service = SimpleNamespace(marketplace_service=service, needs_reingest=lambda: False)
        return module

    def test_plugin_git_route_lazily_refreshes_missing_mirror(self):
        service, _ = self.build_service(git_enabled=False)
        service.publish_version("alice", "code-review", make_bundle(), principal=service.resolve_principal("Bearer admin-token"))
        settings = MarketplaceSettings(
            marketplace_name="bee-plugins",
            storage_dir=service.settings.storage_dir,
            admin_token="admin-token",
            git_mirror_enabled=True,
        )
        refreshed = MarketplaceService(
            service.repository,
            MarketplaceStorage(settings.storage_dir),
            settings,
        )
        module = self.import_main_with_service(refreshed)

        with TestClient(module.app) as client:
            info_refs = client.get("/marketplace/plugins/code-review.git/info/refs")

        self.assertEqual(200, info_refs.status_code)
        self.assertIn("refs/heads/main", info_refs.text)
        self.assertTrue((refreshed.plugin_git_repo_root("code-review") / "HEAD").exists())

    def test_plugin_git_route_lazily_refreshes_crlf_mirror_metadata(self):
        service, _ = self.build_service()
        service.publish_version("alice", "code-review", make_bundle(), principal=service.resolve_principal("Bearer admin-token"))
        info_refs_path = service.plugin_git_repo_root("code-review") / "info" / "refs"
        info_refs_path.write_bytes(info_refs_path.read_bytes().replace(b"\n", b"\r\n"))
        module = self.import_main_with_service(service)

        with TestClient(module.app) as client:
            info_refs = client.get("/marketplace/plugins/code-review.git/info/refs")

        self.assertEqual(200, info_refs.status_code)
        self.assertIn("refs/heads/main", info_refs.text)
        self.assertNotIn(b"\r\n", info_refs_path.read_bytes())

    def test_git_files_served_for_dumb_http_clone(self):
        service, _ = self.build_service()
        service.publish_version("alice", "code-review", make_bundle(), principal=service.resolve_principal("Bearer admin-token"))
        repo = service.storage.git_dir
        main_sha = read_repo_refs(repo)["refs/heads/main"]
        object_rel = f"objects/{main_sha[:2]}/{main_sha[2:]}"
        module = self.import_main_with_service(service)
        with TestClient(module.app) as client:
            info_refs = client.get("/marketplace/git/info/refs")
            info_refs_git_alias = client.get("/marketplace/git.git/info/refs")
            plugin_info_refs = client.get("/marketplace/plugins/code-review.git/info/refs")
            head = client.get("/marketplace/git/HEAD")
            head_git_alias = client.get("/marketplace/git.git/HEAD")
            plugin_head = client.get("/marketplace/plugins/code-review.git/HEAD")
            blob_object = client.get(f"/marketplace/git/{object_rel}")
            blob_object_git_alias = client.get(f"/marketplace/git.git/{object_rel}")
            missing = client.get("/marketplace/git/objects/xx/missing")

        self.assertEqual(200, info_refs.status_code)
        self.assertIn("refs/heads/main", info_refs.text)
        self.assertEqual(200, info_refs_git_alias.status_code)
        self.assertEqual(info_refs.text, info_refs_git_alias.text)
        self.assertEqual(200, plugin_info_refs.status_code)
        self.assertIn("refs/heads/main", plugin_info_refs.text)
        self.assertNotEqual(info_refs.text, plugin_info_refs.text)
        self.assertEqual(200, head.status_code)
        self.assertIn("ref: refs/heads/main", head.text)
        self.assertEqual(200, head_git_alias.status_code)
        self.assertEqual(head.text, head_git_alias.text)
        self.assertEqual(200, plugin_head.status_code)
        self.assertEqual(head.text, plugin_head.text)
        self.assertEqual(200, blob_object.status_code)
        self.assertEqual("application/octet-stream", blob_object.headers["content-type"])
        self.assertEqual(200, blob_object_git_alias.status_code)
        self.assertEqual(blob_object.content, blob_object_git_alias.content)
        self.assertEqual(404, missing.status_code)


if __name__ == "__main__":
    unittest.main()
