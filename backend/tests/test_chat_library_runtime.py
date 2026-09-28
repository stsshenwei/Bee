"""Request skill loading must affect generation without leaking across requests."""
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import unittest

from tests.test_agent_runtime_loop import build_runtime
from app.services.agent.runtime_skills import RequestSkillsManager, RuntimeSkillError
from app.services.plugins.plugin_management import PluginManagementService
from app.services.plugins.plugin_models import PluginRuntimeEnvironment


@dataclass(frozen=True)
class Skill:
    skill_id: str
    name: str = "writing"
    version: str = "1.0.0"
    sha256: str = "abc"
    markdown: str = "Use a concise numbered answer."

    @property
    def runtime_name(self):
        return f"library:{self.skill_id}"

    def public_metadata(self):
        return {"skill_id": self.skill_id, "name": self.name, "version": self.version, "sha256": self.sha256}


class ChatLibraryRuntimeTests(unittest.TestCase):
    def test_workspace_policy_can_enable_each_instruction_loading_mode(self):
        service = PluginManagementService(runtime_environment=PluginRuntimeEnvironment(skills_enabled=True))
        result = service.update_plugin("skills", {"enabled": True, "enabled_modes": ["quick", "reasoning", "wiki", "rag_wiki"]})
        self.assertEqual(set(result["enabled_modes"]), {"quick", "reasoning", "wiki", "rag_wiki"})
    def test_request_managers_do_not_share_same_named_skills(self):
        one, two = Skill("one"), Skill("two", markdown="Respond in paragraphs.")
        with ThreadPoolExecutor() as pool:
            managers = list(pool.map(lambda skill: RequestSkillsManager(None, (skill,)), (one, two)))
        self.assertEqual(one.markdown, managers[0].read_skill(one.runtime_name))
        with self.assertRaises(RuntimeSkillError):
            managers[0].read_skill(two.runtime_name)
        self.assertEqual(two.markdown, managers[1].read_skill(two.runtime_name))

    def test_explicit_skill_is_loaded_for_every_mode_without_global_mutation(self):
        for mode in ("quick", "reasoning", "wiki", "rag_wiki"):
            with self.subTest(mode=mode):
                runtime = build_runtime()
                skill = Skill(mode)
                runtime.config.skills_enabled = True
                events = list(runtime.stream_query_events("What uses Redis?", mode=mode, resolved_skills=(skill,)))
                loaded = [event for event in events if event.event_type == "skills_loaded"]
                self.assertEqual([skill.public_metadata()], loaded[0].payload["items"])
                messages = runtime.llm_client.chat.completions.last_kwargs["messages"]
                self.assertTrue(any(skill.markdown in str(m.get("content")) for m in messages))
                self.assertIsNone(runtime.skills_manager)
                clean = runtime._build_messages("Next question", runtime.rag_service.default_scope, None, None, None)
                self.assertFalse(any(skill.markdown in m["content"] for m in clean))

    def test_disabled_runtime_rejects_explicit_skills(self):
        runtime = build_runtime()
        with self.assertRaises(RuntimeSkillError):
            list(runtime.stream_query_events("Question", resolved_skills=(Skill("one"),)))


if __name__ == "__main__":
    unittest.main()
