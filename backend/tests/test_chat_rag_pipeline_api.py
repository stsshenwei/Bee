import unittest

from fastapi.testclient import TestClient

from tests.test_rag_api_routes import FakeConversationService, FakeMemoryService, FakeRagService, RagApiRouteTests


class ChatRagPipelineApiTests(unittest.TestCase):
    def import_main(self):
        return RagApiRouteTests().import_main()

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


if __name__ == "__main__":
    unittest.main()
