import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.services.chat_streaming.event_bus import ChatStreamEvent
from app.services.chat_streaming.stream_manager import MemoryStreamManager, StreamIdentity
from app.services.memory.conversation_repository import ConversationRepository
from app.services.memory.conversation_service import ConversationService
from app.services.memory.principal import Principal
from tests.test_rag_api_routes import FakeConversationService, FakeMemoryService, FakeRagService, RagApiRouteTests


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


class ChatRagPipelineApiTests(unittest.TestCase):
    def import_main(self):
        return RagApiRouteTests().import_main()

    def install_history_repository(self, module):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        repository = ConversationRepository(Path(tmpdir.name) / "chat-history.sqlite3")
        module.conversation_service = ConversationService(repository=repository, recent_message_limit=4, summary_message_threshold=20)
        module.chat_stream_manager = MemoryStreamManager()
        with module.chat_stream_cancellations_lock:
            module.chat_stream_cancellations.clear()
        return repository

    def test_chat_stream_uses_pipeline_when_flag_enabled(self):
        module = self.import_main()
        fake_rag = FakeRagService()
        fake_conversation = FakeConversationService()
        fake_memory = FakeMemoryService()
        module.rag_service = fake_rag
        module.conversation_service = fake_conversation
        module.memory_service = fake_memory
        module.chat_rag_pipeline_enabled = True

        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "pipeline", "chat_mode": "quick"})

        payload = response.text
        self.assertIn('"conversation_id": "conv-new"', payload)
        self.assertIn('"sources"', payload)
        self.assertIn('"reasoning"', payload)
        self.assertIn('"token": "answer"', payload)
        self.assertIn('"memory_updated"', payload)
        self.assertIn("[DONE]", payload)
        self.assertLess(payload.index('"conversation_id"'), payload.index('"sources"'))
        self.assertLess(payload.index('"sources"'), payload.index('"reasoning"'))
        self.assertLess(payload.index('"reasoning"'), payload.index('"token"'))
        self.assertEqual(["user", "assistant"], [message["role"] for message in fake_conversation.appended])
        self.assertEqual("answer", fake_conversation.appended[1]["content"])
        self.assertEqual("conv-new", fake_memory.extract_calls[0]["conversation_id"])

    def test_chat_stream_keeps_raw_path_when_pipeline_flag_disabled(self):
        module = self.import_main()
        module.rag_service = FakeRagService()
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()
        module.chat_rag_pipeline_enabled = False

        def fail_if_called(*args, **kwargs):
            raise AssertionError("pipeline should not run when disabled")

        module.run_quick_rag_pipeline = fail_if_called

        with TestClient(module.app) as client:
            response = client.post("/chat/stream", json={"message": "raw", "chat_mode": "quick"})

        self.assertIn('"token": "answer"', response.text)
        self.assertIn("[DONE]", response.text)

    def test_pipeline_stream_events_remain_replayable_by_offset(self):
        module = self.import_main()
        module.rag_service = FakeRagService()
        module.conversation_service = FakeConversationService()
        module.memory_service = FakeMemoryService()
        module.chat_rag_pipeline_enabled = True

        with TestClient(module.app) as client:
            initial = client.post(
                "/chat/stream",
                json={"message": "pipeline replay", "chat_mode": "quick", "stream_message_id": "msg-replay"},
            )
            replay = client.post(
                "/chat/stream",
                json={
                    "message": "pipeline replay",
                    "chat_mode": "quick",
                    "conversation_id": "conv-new",
                    "stream_message_id": "msg-replay",
                    "stream_offset": 0,
                },
            )

        self.assertIn("[DONE]", initial.text)
        self.assertIn('"conversation_id": "conv-new"', replay.text)
        self.assertIn('"token": "answer"', replay.text)
        self.assertIn("[DONE]", replay.text)

    def test_messages_load_returns_paginated_history(self):
        module = self.import_main()
        repository = self.install_history_repository(module)
        conversation = repository.create_conversation()
        for index in range(3):
            repository.append_message(conversation["id"], "user", f"message {index}", {})

        data = module.load_session_messages(conversation["id"], FakeRequest(), None, 2)

        self.assertEqual(conversation["id"], data.session_id)
        self.assertEqual(["message 1", "message 2"], [item.content for item in data.items])
        self.assertTrue(data.hasMoreHistory)

    def test_recent_sessions_returns_sidebar_summaries(self):
        module = self.import_main()
        repository = self.install_history_repository(module)
        first = repository.create_conversation(title="Named chat")
        second = repository.create_conversation()
        repository.append_message(first["id"], "user", "first", {})
        _user_message, assistant_message, _request_id = repository.create_turn(second["id"], "sidebar title", {})

        data = module.list_recent_sessions(FakeRequest(), 20)

        self.assertEqual([second["id"], first["id"]], [item.session_id for item in data.items])
        self.assertEqual("sidebar title", data.items[0].title)
        self.assertEqual("Named chat", data.items[1].title)
        self.assertTrue(data.items[0].is_running)
        repository.complete_assistant_message(second["id"], assistant_message["id"], "done", {})
        completed = module.list_recent_sessions(FakeRequest(), 20)
        self.assertFalse(completed.items[0].is_running)

    def test_rename_and_delete_session_routes(self):
        module = self.import_main()
        repository = self.install_history_repository(module)
        conversation = repository.create_conversation()
        repository.append_message(conversation["id"], "user", "original title", {})

        renamed = module.rename_session(conversation["id"], module.SessionRenameRequest(title="Better title"), FakeRequest())
        deleted = module.delete_session(conversation["id"], FakeRequest())
        recent = module.list_recent_sessions(FakeRequest(), 20)

        self.assertEqual("Better title", renamed.title)
        self.assertTrue(deleted.deleted)
        self.assertEqual([], recent.items)

    def test_continue_stream_returns_not_found_when_buffer_missing(self):
        module = self.import_main()
        repository = self.install_history_repository(module)
        conversation = repository.create_conversation()
        _user, assistant, _request_id = repository.create_turn(conversation["id"], "question", {})

        with self.assertRaises(HTTPException) as raised:
            module.continue_session_stream(conversation["id"], FakeRequest(), assistant["id"], 0)

        self.assertEqual(404, raised.exception.status_code)

    def test_continue_stream_replays_retained_events(self):
        module = self.import_main()
        repository = self.install_history_repository(module)
        conversation = repository.create_conversation()
        _user, assistant, _request_id = repository.create_turn(conversation["id"], "question", {})
        identity = StreamIdentity(conversation["id"], assistant["id"])
        module.chat_stream_manager.append(identity, ChatStreamEvent("conversation_id", {"conversation_id": conversation["id"]}))
        module.chat_stream_manager.append(identity, ChatStreamEvent("token", {"token": "partial"}))
        module.chat_stream_manager.append(identity, ChatStreamEvent("done", {}, terminal=True))

        response_text = "".join(module._replay_chat_stream(identity, 1, poll_until_terminal=True))

        self.assertIn('"token": "partial"', response_text)
        self.assertIn('"offset": 2', response_text)

    def test_stop_session_generation_success_completed_and_unauthorized(self):
        module = self.import_main()
        repository = self.install_history_repository(module)
        owner = Principal(kind="web_user", user_id="owner")
        conversation = repository.create_conversation(principal=owner)
        _user, assistant, _request_id = repository.create_turn(conversation["id"], "question", {})

        stopped = module.stop_session_generation(
            conversation["id"],
            module.SessionStopRequest(message_id=assistant["id"]),
            FakeRequest({"x-user-id": "owner"}),
        )
        with self.assertRaises(HTTPException) as unauthorized:
            module.stop_session_generation(
                conversation["id"],
                module.SessionStopRequest(message_id=assistant["id"]),
                FakeRequest({"x-user-id": "other"}),
            )

        completed = repository.complete_assistant_message(conversation["id"], assistant["id"], "final", {})
        already_completed = module.stop_session_generation(
            conversation["id"],
            module.SessionStopRequest(message_id=assistant["id"]),
            FakeRequest({"x-user-id": "owner"}),
        )

        self.assertEqual("stopped", stopped.status)
        self.assertEqual(404, unauthorized.exception.status_code)
        self.assertTrue(completed["is_completed"])
        self.assertEqual("completed", already_completed.status)

    def test_stop_session_generation_is_idempotent_before_completion(self):
        module = self.import_main()
        repository = self.install_history_repository(module)
        conversation = repository.create_conversation()
        _user, assistant, _request_id = repository.create_turn(conversation["id"], "question", {})

        first = module.stop_session_generation(
            conversation["id"],
            module.SessionStopRequest(message_id=assistant["id"]),
            FakeRequest(),
        )
        second = module.stop_session_generation(
            conversation["id"],
            module.SessionStopRequest(message_id=assistant["id"]),
            FakeRequest(),
        )
        events = module.chat_stream_manager.read_after(StreamIdentity(conversation["id"], assistant["id"]), 0)

        self.assertEqual("stopped", first.status)
        self.assertEqual("stopping", second.status)
        self.assertEqual(1, [event.event_type for event in events].count("stop"))


if __name__ == "__main__":
    unittest.main()
