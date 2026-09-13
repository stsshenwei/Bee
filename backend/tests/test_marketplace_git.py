import tempfile
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
    def test_git_files_served_for_dumb_http_clone(self):
        import importlib
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch
        import os

        from tests.test_runtime_config import postgres_runtime_patches

        sys.modules.pop("app.main", None)
        service, _ = self.build_service()
        service.publish_version("alice", "code-review", make_bundle(), principal=service.resolve_principal("Bearer admin-token"))
        repo = service.storage.git_dir
        main_sha = read_repo_refs(repo)["refs/heads/main"]
        object_rel = f"objects/{main_sha[:2]}/{main_sha[2:]}"

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
            with TestClient(module.app) as client:
                info_refs = client.get("/marketplace/git/info/refs")
                head = client.get("/marketplace/git/HEAD")
                blob_object = client.get(f"/marketplace/git/{object_rel}")
                missing = client.get("/marketplace/git/objects/xx/missing")

        self.assertEqual(200, info_refs.status_code)
        self.assertIn("refs/heads/main", info_refs.text)
        self.assertEqual(200, head.status_code)
        self.assertIn("ref: refs/heads/main", head.text)
        self.assertEqual(200, blob_object.status_code)
        self.assertEqual("application/octet-stream", blob_object.headers["content-type"])
        self.assertEqual(404, missing.status_code)


if __name__ == "__main__":
    unittest.main()
