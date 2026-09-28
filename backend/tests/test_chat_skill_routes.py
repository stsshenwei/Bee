from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.services.agent.agent_runtime import AgentRuntimeEvent
from app.services.marketplace import MarketplacePrincipal
from app.services.skills import InMemorySkillRepository, SkillService
from app.services.plugins.plugin_management import InMemoryPluginSettingsRepository, PluginManagementService
from app.services.plugins.plugin_models import PluginRuntimeEnvironment
from app.services.chat_streaming.stream_manager import MemoryStreamManager
from tests import test_rag_api_routes as rag_fakes


SKILL_MD = b"---\nname: concise-answer\ndescription: Answer briefly\n---\nUse one short paragraph."


class CapturingRuntime:
    def __init__(self, *, skills_enabled: bool = True):
        self.config = SimpleNamespace(skills_enabled=skills_enabled)
        self.calls = []

    def stream_query_events(
        self,
        question,
        *,
        conversation_context="",
        memory_context="",
        scope=None,
        mode="reasoning",
        resolved_skills=(),
    ):
        self.calls.append({"question": question, "mode": mode, "scope": scope, "resolved_skills": resolved_skills})
        if resolved_skills:
            yield AgentRuntimeEvent("skills_loaded", {"items": [item.public_metadata() for item in resolved_skills]})
        yield AgentRuntimeEvent("token", {"token": "answer"})
        yield AgentRuntimeEvent("final", {"answer": "answer", "citations": []})


def _plugin_service():
    service = PluginManagementService(
        InMemoryPluginSettingsRepository(),
        workspace_id="default-workspace",
        runtime_environment=PluginRuntimeEnvironment(skills_enabled=True, wiki_tools_enabled=True),
    )
    service.update_plugin(
        "skills",
        {"enabled": True, "enabled_modes": ["quick", "reasoning", "wiki", "rag_wiki"]},
        "default-workspace",
    )
    service.update_plugin(
        "skills",
        {"enabled": True, "enabled_modes": ["quick", "reasoning", "wiki", "rag_wiki"]},
        "other-workspace",
    )
    return service


def _module_and_skill(root: Path):
    with patch.dict("os.environ", {"STREAM_MANAGER_TYPE": "memory", "LANGFUSE_ENABLED": "false"}, clear=False):
        module = rag_fakes.RagApiRouteTests().import_main()
    rag = rag_fakes.FakeRagService()
    runtime = CapturingRuntime()
    skills = SkillService(InMemorySkillRepository(), root / "skills")
    published = skills.publish(SKILL_MD, "SKILL.md", {"version": "1.0.0"}, MarketplacePrincipal("alice", ("publish",)))
    skills.activate("default-workspace", published["skill_id"], True, "1.0.0", MarketplacePrincipal("admin", ("admin",)))
    rag.skill_service = skills
    rag.plugin_management_service = _plugin_service()
    rag.agent_runtime = runtime
    rag.agent_runtime_enabled = True
    rag.unified_chat_runtime_enabled = True
    rag.quick_runtime_enabled = True
    rag.wiki_runtime_enabled = True
    rag.rag_wiki_runtime_enabled = True
    module.rag_service = rag
    module.conversation_service = rag_fakes.FakeConversationService()
    module.memory_service = rag_fakes.FakeMemoryService()
    module.chat_stream_manager = MemoryStreamManager()
    return module, rag, runtime, skills, published


def _ref(published):
    return {"skill_id": published["skill_id"], "version": "1.0.0"}


def test_selected_skill_loads_in_all_four_chat_modes_and_persists_metadata():
    with tempfile.TemporaryDirectory() as directory:
        module, _rag, runtime, _skills, published = _module_and_skill(Path(directory))
        with TestClient(module.app) as client:
            for mode in ("quick", "reasoning", "wiki", "rag_wiki"):
                response = client.post("/chat/stream", json={"message": mode, "chat_mode": mode, "skill_refs": [_ref(published)]})
                assert response.status_code == 200
                assert '"skills_loaded"' in response.text
                assert published["sha256"] in response.text
        assert [call["mode"] for call in runtime.calls] == ["quick", "reasoning", "wiki", "rag_wiki"]
        for call in runtime.calls:
            assert call["resolved_skills"][0].skill_id == published["skill_id"]
        assistant_rows = [row for row in module.conversation_service.appended if row["role"] == "assistant"]
        assert len(assistant_rows) == 4
        assert all(row["metadata_json"]["skills_loaded"][0]["version"] == "1.0.0" for row in assistant_rows)


def test_stale_activation_and_runtime_disabled_reject_before_model_invocation():
    with tempfile.TemporaryDirectory() as directory:
        module, rag, runtime, skills, published = _module_and_skill(Path(directory))
        admin = MarketplacePrincipal("admin", ("admin",))
        skills.activate("default-workspace", published["skill_id"], False, "1.0.0", admin)
        with TestClient(module.app) as client:
            stale = client.post("/chat/stream", json={"message": "stale", "chat_mode": "reasoning", "skill_refs": [_ref(published)]})
            skills.activate("default-workspace", published["skill_id"], True, "1.0.0", admin)
            runtime.config.skills_enabled = False
            disabled = client.post("/chat/stream", json={"message": "disabled", "chat_mode": "reasoning", "skill_refs": [_ref(published)]})
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "skill_unavailable"
        assert disabled.status_code == 409
        assert disabled.json()["detail"]["code"] == "skill_unavailable"
        assert runtime.calls == []


def test_empty_skill_refs_preserve_legacy_raw_chat_path():
    with tempfile.TemporaryDirectory() as directory:
        module, rag, runtime, _skills, _published = _module_and_skill(Path(directory))
        rag.agent_runtime = None
        rag.agent_runtime_enabled = False
        rag.unified_chat_runtime_enabled = False
        rag.quick_runtime_enabled = False
        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "legacy", "chat_mode": "quick", "skill_refs": []})
        assert response.status_code == 200
        assert '"token": "answer"' in response.text
        assert runtime.calls == []
        assert len(rag.stream_calls) == 1


def test_workspace_activation_does_not_authorize_another_workspace():
    with tempfile.TemporaryDirectory() as directory:
        module, rag, runtime, skills, published = _module_and_skill(Path(directory))
        base_resolve_scope = rag.resolve_scope

        def other_scope(_knowledge_base_ids=None, _document_ids=None):
            scope = base_resolve_scope()
            return type(scope)(
                workspace_id="other-workspace",
                selected_knowledge_base_ids=scope.selected_knowledge_base_ids,
                document_ids=scope.document_ids,
                compatibility_default=False,
            )

        rag.resolve_scope = other_scope
        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "isolated", "chat_mode": "reasoning", "skill_refs": [_ref(published)]})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "skill_unavailable"
        assert runtime.calls == []
