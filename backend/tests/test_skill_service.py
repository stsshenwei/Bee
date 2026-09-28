import io
import os
import stat
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.services.marketplace import MarketplacePrincipal
from app.services.skills import InMemorySkillRepository, SkillService, SkillSettings
from app.services.skills.models import SkillConflictError, SkillForbiddenError, SkillNotFoundError, SkillTooLargeError, SkillUnavailableError, SkillValidationError


MD=b"---\nname: review\ndescription: Review code safely\n---\n# Instructions\nNever execute scripts.\n"


def bundle(entries):
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w") as z:
        for name,value in entries: z.writestr(name,value)
    return out.getvalue()


@pytest.fixture
def service():
    with tempfile.TemporaryDirectory() as root:
        yield SkillService(InMemorySkillRepository(),Path(root)/"skills",SkillSettings(max_selected_markdown_bytes=10000))


def test_validate_single_markdown_does_not_publish(service):
    result=service.validate(MD,"SKILL.md")
    assert result["parsed_metadata"]["name"]=="review"
    assert result["file_index"]==[{"path":"SKILL.md","size":len(MD)}]
    assert service.list_skills()["items"]==[]


def test_standalone_markdown_obeys_same_file_and_uncompressed_limits(tmp_path):
    constrained = SkillService(
        InMemorySkillRepository(),
        tmp_path / "skills",
        SkillSettings(max_upload_bytes=10000, max_file_bytes=len(MD)-1, max_uncompressed_bytes=10000),
    )
    with pytest.raises(SkillTooLargeError):
        constrained.validate(MD, "SKILL.md")
    constrained = SkillService(
        InMemorySkillRepository(),
        tmp_path / "skills-2",
        SkillSettings(max_upload_bytes=10000, max_file_bytes=10000, max_uncompressed_bytes=len(MD)-1),
    )
    with pytest.raises(SkillTooLargeError):
        constrained.validate(MD, "SKILL.md")


@pytest.mark.parametrize("name",["../SKILL.md","/SKILL.md","C:/SKILL.md"])
def test_rejects_unsafe_paths(service,name):
    with pytest.raises(SkillValidationError): service.validate(bundle([(name,MD)]),"x.zip")


def test_rejects_symlink_and_normalized_duplicates(service):
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w") as z:
        link=zipfile.ZipInfo("SKILL.md"); link.create_system=3; link.external_attr=(stat.S_IFLNK|0o777)<<16
        z.writestr(link,"target")
    with pytest.raises(SkillValidationError): service.validate(out.getvalue(),"x.zip")
    with pytest.raises(SkillValidationError): service.validate(bundle([("root/SKILL.md",MD),("root/a/../x.txt",b"1"),("root/x.txt",b"2")]),"x.zip")


def test_publish_is_immutable_owner_scoped_and_complete(service):
    alice=MarketplacePrincipal("alice",("publish",)); bob=MarketplacePrincipal("bob",("publish",))
    data=bundle([("folder/SKILL.md",MD),("folder/scripts/tool.py",b"raise SystemExit")])
    first=service.publish(data,"review.zip",{"version":"1.0.0","author":"Ada"},alice)
    assert first["markdown"]==MD.decode()
    assert {f["path"] for f in first["files"]}=={"SKILL.md","scripts/tool.py"}
    assert zipfile.ZipFile(io.BytesIO(service.download(first["skill_id"],"1.0.0"))).read("scripts/tool.py")==b"raise SystemExit"
    with pytest.raises(SkillConflictError): service.publish(data,"review.zip",{"version":"1.0.0"},alice,first["skill_id"])
    with pytest.raises(SkillForbiddenError): service.publish(data,"review.zip",{"version":"2.0.0"},bob,first["skill_id"])


def test_activation_resolution_is_pinned_withdrawal_aware_and_snapshot(service):
    admin=MarketplacePrincipal("root",("admin",)); alice=MarketplacePrincipal("alice",("publish",))
    published=service.publish(MD,"SKILL.md",{"version":"1.0.0"},alice)
    sid=published["skill_id"]
    service.activate("w1",sid,True,"1.0.0",admin)
    resolved=service.resolve("w1",[{"skill_id":sid,"version":"1.0.0"}],True)
    assert resolved[0].runtime_name==f"library:{sid}"
    assert "markdown" not in resolved[0].public_metadata()
    service.set_status(sid,"1.0.0","yanked",alice)
    assert resolved[0].markdown==MD.decode()
    with pytest.raises(SkillUnavailableError): service.resolve("w1",[{"skill_id":sid,"version":"1.0.0"}],True)
    assert service.workspace_skills("w1")["items"][0]["reason"]=="version_yanked"


def test_runtime_disabled_fails_before_resolution(service):
    with pytest.raises(SkillUnavailableError): service.resolve("w",[{"skill_id":"x","version":"1.0.0"}],False)


def test_metadata_is_typed_and_owner_override_requires_admin(service):
    alice = MarketplacePrincipal("alice", ("publish",))
    admin = MarketplacePrincipal("admin", ("admin",))
    with pytest.raises(SkillValidationError):
        service.validate(MD, "SKILL.md", {"tags": "wrong"})
    bad_yaml = b"---\nname: review\ndescription: test\ntags: 3\n---\nbody"
    with pytest.raises(SkillValidationError):
        service.validate(bad_yaml, "SKILL.md")
    cyclic = b"---\nname: review\ndescription: test\nextra: &loop [*loop]\n---\nbody"
    assert service.validate(cyclic, "SKILL.md")["parsed_metadata"]["name"] == "review"
    with pytest.raises(SkillForbiddenError):
        service.publish(MD, "SKILL.md", {"owner": "bob"}, alice)
    result = service.publish(MD, "SKILL.md", {"owner": "bob", "version": "1.0.0"}, admin)
    assert service.detail(result["skill_id"], admin)["owner"] == "bob"
    with pytest.raises(SkillNotFoundError):
        service.publish(MD, "SKILL.md", {"version": "2.0.0"}, admin, "missing")


def test_concurrent_duplicate_keeps_winner_blob(service):
    alice = MarketplacePrincipal("alice", ("publish",))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.publish, MD, "SKILL.md", {"version": "1.0.0"}, alice) for _ in range(2)]
    results, errors = [], []
    for future in futures:
        try:
            results.append(future.result())
        except Exception as exc:
            errors.append(exc)
    assert len(results) == 1
    assert len(errors) == 1 and isinstance(errors[0], SkillConflictError)
    winner = results[0]
    assert service.download(winner["skill_id"], winner["version"]).startswith(b"PK")


def test_yanked_versions_are_hidden_from_anonymous_detail(service):
    alice = MarketplacePrincipal("alice", ("publish",))
    result = service.publish(MD, "SKILL.md", {"version": "1.0.0"}, alice)
    service.set_status(result["skill_id"], "1.0.0", "yanked", alice)
    with pytest.raises(SkillNotFoundError):
        service.detail(result["skill_id"])
    managed = service.detail(result["skill_id"], alice)
    assert managed["can_manage"] is True
    assert managed["versions"][0]["status"] == "yanked"


def test_ambiguous_commit_ack_preserves_committed_blob(tmp_path):
    class AmbiguousRepository(InMemorySkillRepository):
        def add_version(self, **values):
            super().add_version(**values)
            raise OSError("connection dropped after commit")
    service = SkillService(AmbiguousRepository(), tmp_path / "skills")
    result = service.publish(MD, "SKILL.md", {"version": "1.0.0"}, MarketplacePrincipal("alice", ("publish",)))
    assert service.download(result["skill_id"], "1.0.0").startswith(b"PK")


def test_missing_status_target_is_404(service):
    admin = MarketplacePrincipal("admin", ("admin",))
    result = service.publish(MD, "SKILL.md", {"version": "1.0.0"}, admin)
    with pytest.raises(SkillNotFoundError):
        service.set_status(result["skill_id"], "99.0.0", "yanked", admin)


def test_failed_database_publish_removes_uncommitted_blob(tmp_path):
    class FailingRepository(InMemorySkillRepository):
        def add_version(self, **values):
            raise OSError("database unavailable")
    service = SkillService(FailingRepository(), tmp_path / "skills")
    with pytest.raises(OSError):
        service.publish(MD, "SKILL.md", {"version": "1.0.0"}, MarketplacePrincipal("alice", ("publish",)))
    assert list((tmp_path / "skills").rglob("*.zip")) == []


def test_orphan_cleanup_only_removes_unreferenced_old_blobs(service):
    result = service.publish(MD, "SKILL.md", {"version": "1.0.0"}, MarketplacePrincipal("alice", ("publish",)))
    orphan = service.storage_dir / "orphan.zip"
    orphan.write_bytes(b"orphan")
    os.utime(orphan, (1, 1))
    assert service.cleanup_orphan_blobs(grace_seconds=0) == 1
    assert not orphan.exists()
    assert service.download(result["skill_id"], "1.0.0").startswith(b"PK")
