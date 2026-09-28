import io
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from app.services.agent.runtime_skills import RequestSkillsManager, RuntimeSkillError
from app.services.marketplace import MarketplacePrincipal
from app.services.skills import InMemorySkillRepository, SkillService
from app.services.skills.models import ResolvedSkill
from app.services.skills.sandbox import (
    DockerSkillSandbox,
    SkillExecutionPolicy,
    SkillMaterializer,
    SkillSandboxDenied,
    SkillSandboxResult,
    SkillSandboxSettings,
    SkillScriptValidator,
)

MD = b"---\nname: exec-skill\ndescription: Runs scripts\n---\n# Exec\n"
SCRIPT = b"print('hello')\n"


def bundle(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return out.getvalue()


def published_service(tmp_path):
    service = SkillService(InMemorySkillRepository(), tmp_path / "skills")
    result = service.publish(
        bundle([("SKILL.md", MD), ("scripts/run.py", SCRIPT)]),
        "skill.zip",
        {"version": "1.0.0"},
        MarketplacePrincipal("alice", ("publish",)),
    )
    return service, result


def test_settings_default_disable_execution():
    settings = SkillSandboxSettings.from_env({})
    assert settings.mode == "disabled"
    assert settings.enabled is False


def test_settings_reject_unsupported_mode():
    with pytest.raises(Exception):
        SkillSandboxSettings.from_env({"AGENT_RUNTIME_SKILL_SANDBOX_MODE": "local"})


def test_materializer_extracts_content_addressed_safe_bundle(tmp_path):
    service, result = published_service(tmp_path)
    resolved = service.resolve("workspace", [], True)
    record = service.repository.get_version(result["skill_id"], "1.0.0")
    skill = ResolvedSkill(result["skill_id"], "exec-skill", "1.0.0", record.sha256, record.markdown, f"library:{result['skill_id']}", record.blob_key, tuple(record.files))

    materialized = SkillMaterializer(service.storage_dir, tmp_path / "cache").materialize(skill)

    assert materialized.root.name == record.sha256
    assert (materialized.root / "SKILL.md").exists()
    assert (materialized.root / "scripts" / "run.py").read_bytes() == SCRIPT


def test_materializer_rejects_hash_mismatch(tmp_path):
    service, result = published_service(tmp_path)
    record = service.repository.get_version(result["skill_id"], "1.0.0")
    skill = ResolvedSkill(result["skill_id"], "exec-skill", "1.0.0", "bad", record.markdown, f"library:{result['skill_id']}", record.blob_key, tuple(record.files))

    with pytest.raises(SkillSandboxDenied):
        SkillMaterializer(service.storage_dir, tmp_path / "cache").materialize(skill)


def test_validator_rejects_path_traversal_arg_injection_and_network(tmp_path):
    root = tmp_path / "skill"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "run.py").write_text("print('ok')", encoding="utf-8")
    (root / "scripts" / "net.py").write_text("import requests\nrequests.get('https://example.com')", encoding="utf-8")
    validator = SkillScriptValidator(SkillSandboxSettings(mode="docker"))

    with pytest.raises(SkillSandboxDenied):
        validator.validate(root, "../run.py", [], "")
    with pytest.raises(SkillSandboxDenied):
        validator.validate(root, "scripts/run.py", ["ok; rm -rf /"], "")
    with pytest.raises(SkillSandboxDenied):
        validator.validate(root, "scripts/net.py", [], "")


def test_docker_argv_uses_hardening_flags_and_no_shell(tmp_path):
    settings = SkillSandboxSettings(mode="docker-workspace-write", docker_image="bee-test:local", network_enabled=False)
    skill_root = tmp_path / "skill"
    output_root = tmp_path / "out"
    skill_root.mkdir()
    output_root.mkdir()
    skill = ResolvedSkill("sid", "skill", "1.0.0", "sha", "md", "library:sid")
    policy = SkillExecutionPolicy("docker-workspace-write", "docker", skill, skill_root, output_root, settings)

    argv = DockerSkillSandbox(settings).build_argv(policy, "scripts/run.py", "python3", ["a"])

    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--read-only" in argv
    assert ["--network", "none"] == argv[argv.index("--network"):argv.index("--network") + 2]
    assert "--cap-drop" in argv and "ALL" in argv
    assert "--security-opt" in argv and "no-new-privileges" in argv
    assert any(str(skill_root) in item and "readonly" in item for item in argv)
    assert any(str(output_root) in item for item in argv)
    assert "shell=True" not in " ".join(argv)


class FakeSandbox:
    enabled = True

    def __init__(self):
        self.calls = []

    def execute(self, skill, script_path, args, stdin):
        self.calls.append((skill.runtime_name, script_path, args, stdin))
        return SkillSandboxResult(success=True, stdout="ok", exit_code=0, metadata={"sandbox": {"mode": "docker-read-only"}})


def test_request_skills_manager_executes_only_selected_library_skill():
    skill = ResolvedSkill("sid", "skill", "1.0.0", "sha", "md", "library:sid", "blob.zip")
    manager = RequestSkillsManager(None, (skill,))
    sandbox = FakeSandbox()

    result = manager.execute_script("library:sid", "scripts/run.py", ["x"], "", sandbox)

    assert result.stdout == "ok"
    assert sandbox.calls == [("library:sid", "scripts/run.py", ["x"], "")]
    with pytest.raises(RuntimeSkillError):
        manager.execute_script("library:other", "scripts/run.py", [], "", sandbox)


@pytest.mark.skipif(os.environ.get("BEE_RUN_DOCKER_SKILL_SANDBOX_TEST") != "1" or shutil.which("docker") is None, reason="set BEE_RUN_DOCKER_SKILL_SANDBOX_TEST=1 with Docker available")
def test_guarded_docker_smoke_executes_when_enabled(tmp_path):
    image = os.environ.get("AGENT_RUNTIME_SKILL_SANDBOX_DOCKER_IMAGE", "bee-skill-sandbox:latest")
    dockerfile = Path(__file__).resolve().parents[2] / "docker" / "Dockerfile.skill-sandbox"
    subprocess.run(["docker", "build", "-t", image, "-f", str(dockerfile), str(dockerfile.parents[1])], check=True)
    service, result = published_service(tmp_path)
    admin = MarketplacePrincipal("root", ("admin",))
    service.activate("w1", result["skill_id"], True, "1.0.0", admin)
    skill = service.resolve("w1", [{"skill_id": result["skill_id"], "version": "1.0.0"}], True)[0]
    from app.services.skills.sandbox import SkillSandboxManager
    manager = SkillSandboxManager(service.storage_dir, SkillSandboxSettings(mode="docker", docker_image=image, cache_dir=str(tmp_path / "cache"), output_dir=str(tmp_path / "out")))
    executed = manager.execute(skill, "scripts/run.py", [], "")
    assert executed.exit_code == 0
    assert "hello" in executed.stdout
