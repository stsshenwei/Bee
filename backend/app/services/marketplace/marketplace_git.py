from __future__ import annotations

import hashlib
import logging
import os
import time
import zlib
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GIT_IDENTITY_NAME = "Bee Marketplace"
GIT_IDENTITY_EMAIL = "marketplace@bee.local"
MAIN_REF = "refs/heads/main"


def _nested_files(files: dict[str, bytes]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for path, content in files.items():
        parts = [part for part in path.split("/") if part]
        if not parts:
            continue
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = content
    return root


class GitMirrorBuilder:
    """Maintains a bare git repository mirroring the marketplace snapshot tree.

    Writes only loose objects and refs (no packfiles, no git binary), so the
    repo can be served as static files and cloned over the dumb HTTP
    protocol — a zero-dependency fallback channel for clients that cannot
    consume the ZIP snapshot directly.
    """

    def __init__(self, repo_dir: Path):
        self.repo_dir = Path(repo_dir)
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        (self.repo_dir / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "refs" / "tags").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "info").mkdir(parents=True, exist_ok=True)
        head = self.repo_dir / "HEAD"
        if not head.exists():
            head.write_text("ref: refs/heads/main\n", encoding="utf-8")
        packs = self.repo_dir / "objects" / "info" / "packs"
        if not packs.exists():
            packs.write_text("", encoding="utf-8")

    def update(self, files: dict[str, bytes], *, revision: str, message: str) -> str:
        """Commit ``files`` and move ``main`` plus a revision tag to it.

        Object writes are content-addressed and idempotent; refs are updated
        atomically after all objects exist, so a failure keeps the previous
        refs serving (last-good).
        """

        root_tree = self._write_tree(_nested_files(files))
        commit_sha = self._write_commit(root_tree, message)
        self._update_ref(MAIN_REF, commit_sha)
        self._update_ref(f"refs/tags/snapshot-{revision}", commit_sha)
        self._write_info_refs()
        return commit_sha

    def _write_object(self, obj_type: bytes, content: bytes) -> str:
        store = obj_type + b" " + str(len(content)).encode("ascii") + b"\x00" + content
        sha = hashlib.sha1(store).hexdigest()
        obj_path = self.repo_dir / "objects" / sha[:2] / sha[2:]
        if not obj_path.exists():
            obj_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = obj_path.parent / (sha[2:] + ".tmp")
            tmp.write_bytes(zlib.compress(store))
            os.replace(tmp, obj_path)
        return sha

    def _write_tree(self, node: dict[str, Any]) -> str:
        entries: list[tuple[bytes, str, str]] = []
        for name, value in node.items():
            if isinstance(value, dict):
                entries.append((b"40000", name, self._write_tree(value)))
            else:
                entries.append((b"100644", name, self._write_object(b"blob", value)))
        # git sorts tree entries comparing directories as "name/"
        entries.sort(key=lambda entry: entry[1] + ("/" if entry[0] == b"40000" else ""))
        body = b"".join(
            mode + b" " + name.encode("utf-8") + b"\x00" + bytes.fromhex(sha)
            for mode, name, sha in entries
        )
        return self._write_object(b"tree", body)

    def _write_commit(self, root_tree: str, message: str) -> str:
        timestamp = int(time.time())
        identity = f"{GIT_IDENTITY_NAME} <{GIT_IDENTITY_EMAIL}>".encode("utf-8")
        content = b"\n".join(
            [
                b"tree " + root_tree.encode("ascii"),
                b"author " + identity + b" " + str(timestamp).encode("ascii") + b" +0000",
                b"committer " + identity + b" " + str(timestamp).encode("ascii") + b" +0000",
                b"",
                message.encode("utf-8"),
            ]
        )
        return self._write_object(b"commit", content + b"\n")

    def _update_ref(self, ref: str, sha: str) -> None:
        ref_path = self.repo_dir / ref
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = ref_path.with_name(ref_path.name + ".tmp")
        tmp.write_text(sha + "\n", encoding="utf-8")
        os.replace(tmp, ref_path)

    def _write_info_refs(self) -> None:
        refs: list[tuple[str, str]] = []
        refs_root = self.repo_dir / "refs"
        for ref_path in sorted(refs_root.rglob("*")):
            if ref_path.is_file():
                ref_name = ref_path.relative_to(self.repo_dir).as_posix()
                sha = ref_path.read_text(encoding="utf-8").strip()
                if sha:
                    refs.append((sha, ref_name))
        body = "".join(f"{sha}\t{ref}\n" for sha, ref in sorted(refs))
        target = self.repo_dir / "info" / "refs"
        tmp = target.with_name("refs.tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, target)


def read_repo_refs(repo_dir: Path) -> dict[str, str]:
    """Return ``{refname: sha}`` for a mirror repository."""

    repo_dir = Path(repo_dir)
    refs: dict[str, str] = {}
    refs_root = repo_dir / "refs"
    if not refs_root.exists():
        return refs
    for ref_path in sorted(refs_root.rglob("*")):
        if ref_path.is_file():
            sha = ref_path.read_text(encoding="utf-8").strip()
            if sha:
                refs[ref_path.relative_to(repo_dir).as_posix()] = sha
    return refs


def resolve_repo_file(repo_dir: Path, file_path: str) -> Path | None:
    """Resolve a path inside the bare repo, rejecting traversal."""

    repo_dir = Path(repo_dir).resolve()
    clean = str(file_path or "").replace("\\", "/").strip().lstrip("/")
    if not clean:
        return None
    candidate = (repo_dir / clean).resolve()
    if candidate != repo_dir and repo_dir not in candidate.parents:
        return None
    if candidate.is_file():
        return candidate
    return None
