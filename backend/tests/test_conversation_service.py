import tempfile
import unittest
from pathlib import Path

from app.services.memory.conversation_repository import ConversationRepository
from app.services.memory.principal import Principal
from app.services.memory.conversation_service import ConversationService


class FakeSummarizer:
    def __init__(self):
        self.calls = []

    def summarize(self, previous_summary, messages):
        self.calls.append({"previous_summary": previous_summary, "messages": messages})
        return "Summary: user prefers Chinese and is building RAG memory."


class ConversationServiceTests(unittest.TestCase):
    def make_service(self, recent_limit=3, summary_threshold=5):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        repository = ConversationRepository(Path(tmpdir.name) / "memory.sqlite3")
        summarizer = FakeSummarizer()
        service = ConversationService(
            repository=repository,
            recent_message_limit=recent_limit,
            summary_message_threshold=summary_threshold,
            summarizer=summarizer,
        )
        return service, repository, summarizer

    def test_get_or_create_conversation_creates_when_missing(self):
        service, repository, _ = self.make_service()

        conversation = service.get_or_create_conversation(None)

        self.assertTrue(conversation["id"].startswith("conv_"))
        self.assertIsNotNone(repository.get_conversation(conversation["id"]))

    def test_get_or_create_conversation_reuses_existing_id(self):
        service, repository, _ = self.make_service()
        existing = repository.create_conversation(title="Existing")

        conversation = service.get_or_create_conversation(existing["id"])

        self.assertEqual(existing["id"], conversation["id"])
        self.assertEqual("Existing", conversation["title"])

    def test_build_context_returns_summary_and_recent_window(self):
        service, repository, _ = self.make_service(recent_limit=2, summary_threshold=10)
        conversation = repository.create_conversation()
        repository.update_summary(conversation["id"], "Earlier: project uses FastAPI.")
        repository.append_message(conversation["id"], "user", "first", {})
        repository.append_message(conversation["id"], "assistant", "second", {})
        repository.append_message(conversation["id"], "user", "third", {})

        context = service.build_context(conversation["id"])

        self.assertEqual("Earlier: project uses FastAPI.", context["summary"])
        self.assertEqual(["assistant", "user"], [item["role"] for item in context["recent_messages"]])
        self.assertEqual(["second", "third"], [item["content"] for item in context["recent_messages"]])

    def test_maybe_summarize_updates_summary_when_threshold_exceeded(self):
        service, repository, summarizer = self.make_service(recent_limit=2, summary_threshold=3)
        conversation = repository.create_conversation()
        for index in range(4):
            repository.append_message(conversation["id"], "user", f"message {index}", {})

        summary = service.maybe_summarize(conversation["id"])

        loaded = repository.get_conversation(conversation["id"])
        self.assertEqual("Summary: user prefers Chinese and is building RAG memory.", summary)
        self.assertEqual(summary, loaded["summary"])
        self.assertEqual(["message 0", "message 1"], [item["content"] for item in summarizer.calls[0]["messages"]])

    def test_maybe_summarize_does_nothing_below_threshold(self):
        service, repository, summarizer = self.make_service(recent_limit=2, summary_threshold=10)
        conversation = repository.create_conversation()
        repository.append_message(conversation["id"], "user", "only", {})

        self.assertEqual("", service.maybe_summarize(conversation["id"]))
        self.assertEqual([], summarizer.calls)

    def test_repository_scopes_owned_and_shared_conversations(self):
        _service, repository, _ = self.make_service()
        owner = Principal(kind="web_user", tenant_id="tenant-a", user_id="user-1")
        other = Principal(kind="web_user", tenant_id="tenant-a", user_id="user-2")
        shared = Principal(kind="api_tenant", tenant_id="tenant-a")

        owned_conversation = repository.create_conversation(principal=owner)
        shared_conversation = repository.create_conversation(principal=shared)
        repository.append_message(owned_conversation["id"], "user", "private", {})
        repository.append_message(shared_conversation["id"], "user", "shared", {})

        self.assertIsNotNone(repository.get_conversation(owned_conversation["id"], principal=owner))
        self.assertIsNone(repository.get_conversation(owned_conversation["id"], principal=other))
        self.assertEqual([], repository.list_messages(owned_conversation["id"], principal=other))
        self.assertIsNotNone(repository.get_conversation(shared_conversation["id"], principal=owner))
        self.assertEqual(["shared"], [item["content"] for item in repository.list_messages(shared_conversation["id"], principal=owner)])

    def test_list_conversations_returns_recent_scoped_titles(self):
        _service, repository, _ = self.make_service()
        owner = Principal(kind="web_user", tenant_id="tenant-a", user_id="user-1")
        other = Principal(kind="web_user", tenant_id="tenant-a", user_id="user-2")
        first = repository.create_conversation(title="Pinned title", principal=owner)
        second = repository.create_conversation(principal=owner)
        hidden = repository.create_conversation(principal=other)
        repository.append_message(first["id"], "user", "older question", {})
        repository.append_message(second["id"], "user", "newer question becomes title", {})
        repository.append_message(hidden["id"], "user", "hidden", {})

        recent = repository.list_conversations(limit=10, principal=owner)

        self.assertEqual([second["id"], first["id"]], [item["id"] for item in recent])
        self.assertEqual("newer question becomes title", recent[0]["display_title"])
        self.assertEqual("Pinned title", recent[1]["display_title"])
        self.assertFalse(recent[0]["is_running"])

    def test_list_conversations_marks_running_assistant_turns(self):
        _service, repository, _ = self.make_service()
        conversation = repository.create_conversation()
        _user_message, assistant_message, _request_id = repository.create_turn(conversation["id"], "streaming question", {})

        recent = repository.list_conversations(limit=10)

        self.assertTrue(recent[0]["is_running"])
        repository.complete_assistant_message(conversation["id"], assistant_message["id"], "done", {})
        completed_recent = repository.list_conversations(limit=10)
        self.assertFalse(completed_recent[0]["is_running"])

    def test_rename_and_delete_conversation_respect_scope(self):
        _service, repository, _ = self.make_service()
        owner = Principal(kind="web_user", tenant_id="tenant-a", user_id="user-1")
        other = Principal(kind="web_user", tenant_id="tenant-a", user_id="user-2")
        conversation = repository.create_conversation(principal=owner)
        repository.append_message(conversation["id"], "user", "hello", {}, request_id="req-1")

        self.assertIsNone(repository.rename_conversation(conversation["id"], "Other", principal=other))
        renamed = repository.rename_conversation(conversation["id"], "Renamed chat", principal=owner)
        self.assertEqual("Renamed chat", renamed["title"])

        self.assertFalse(repository.delete_conversation(conversation["id"], principal=other))
        self.assertTrue(repository.delete_conversation(conversation["id"], principal=owner))
        self.assertIsNone(repository.get_conversation(conversation["id"], principal=owner))
        self.assertEqual([], repository.list_messages(conversation["id"], principal=owner))
        self.assertEqual([], repository.list_conversations(limit=10, principal=owner))

    def test_create_turn_pairs_request_and_completion_is_idempotent(self):
        _service, repository, _ = self.make_service()
        conversation = repository.create_conversation()

        user_message, assistant_message, request_id = repository.create_turn(conversation["id"], "hello", {"chat_mode": "quick"})
        completed = repository.complete_assistant_message(
            conversation["id"],
            assistant_message["id"],
            "partial answer",
            {"sources": [{"source": "doc.md"}]},
            stopped=True,
        )
        second = repository.complete_assistant_message(conversation["id"], assistant_message["id"], "replacement", {})

        self.assertEqual(request_id, user_message["request_id"])
        self.assertEqual(request_id, assistant_message["request_id"])
        self.assertTrue(user_message["is_completed"])
        self.assertFalse(assistant_message["is_completed"])
        self.assertEqual("partial answer", completed["content"])
        self.assertTrue(completed["is_completed"])
        self.assertTrue(completed["metadata_json"]["stopped"])
        self.assertEqual("partial answer", second["content"])

    def test_recent_and_paginated_messages_are_bounded_and_ordered(self):
        _service, repository, _ = self.make_service()
        conversation = repository.create_conversation()
        messages = [
            repository.append_message(conversation["id"], "user", f"message {index}", {})
            for index in range(5)
        ]

        recent = repository.list_recent_messages(conversation["id"], 2)
        latest_page = repository.list_messages_before_time(conversation["id"], limit=2)
        older_page = repository.list_messages_before_time(
            conversation["id"],
            before_time=latest_page["items"][0]["created_at"],
            limit=2,
        )

        self.assertEqual(["message 3", "message 4"], [item["content"] for item in recent])
        self.assertEqual(["message 3", "message 4"], [item["content"] for item in latest_page["items"]])
        self.assertTrue(latest_page["hasMoreHistory"])
        self.assertEqual(["message 1", "message 2"], [item["content"] for item in older_page["items"]])
        self.assertTrue(older_page["hasMoreHistory"])
        self.assertEqual(messages[-1]["id"], recent[-1]["id"])

    def test_same_timestamp_user_precedes_assistant(self):
        _service, repository, _ = self.make_service()
        conversation = repository.create_conversation()
        user_message, assistant_message, _request_id = repository.create_turn(conversation["id"], "question", {})
        same_time = "2026-01-01T00:00:00.000000"
        with repository._connect() as conn:
            conn.execute(
                "update conversation_message set created_at = ? where id in (?, ?)",
                (same_time, user_message["id"], assistant_message["id"]),
            )

        ordered = repository.list_messages(conversation["id"])

        self.assertEqual(["user", "assistant"], [item["role"] for item in ordered])


if __name__ == "__main__":
    unittest.main()
