import unittest

from app.services.chat_streaming.event_bus import ChatEventBus, ChatStreamEvent
from app.services.chat_streaming.stream_manager import MemoryStreamManager, StreamIdentity


class ChatStreamingTests(unittest.TestCase):
    def test_event_bus_sanitizes_private_payload_keys(self):
        bus = ChatEventBus()
        seen = []
        bus.subscribe(seen.append)

        event = bus.publish(
            "agent_thought",
            {
                "text": "public",
                "raw_prompt": "hidden",
                "nested": {"api_key": "secret", "value": 1},
            },
        )

        self.assertEqual("public", event.payload["text"])
        self.assertNotIn("raw_prompt", event.payload)
        self.assertNotIn("api_key", event.payload["nested"])
        self.assertEqual([event], seen)

    def test_memory_stream_manager_appends_and_reads_by_offset(self):
        manager = MemoryStreamManager()
        identity = StreamIdentity("session-1", "message-1")

        first = manager.append(identity, ChatStreamEvent("conversation_id", {"conversation_id": "session-1"}))
        second = manager.append(identity, ChatStreamEvent("token", {"token": "hi"}))
        done = manager.append(identity, ChatStreamEvent("complete", {}, terminal=True))

        self.assertEqual(1, first.offset)
        self.assertEqual(2, second.offset)
        self.assertEqual(3, done.offset)
        self.assertEqual(["token", "complete"], [event.event_type for event in manager.read_after(identity, 1)])
        self.assertTrue(manager.is_terminal(identity))

    def test_memory_stream_manager_cleanup_removes_stream(self):
        manager = MemoryStreamManager()
        identity = StreamIdentity("session-1", "message-1")
        manager.append(identity, ChatStreamEvent("complete", {}, terminal=True))

        manager.cleanup(identity)

        self.assertEqual([], manager.read_after(identity, 0))
        self.assertFalse(manager.is_terminal(identity))

    def test_event_bus_can_feed_stream_manager_in_order(self):
        manager = MemoryStreamManager()
        identity = StreamIdentity("session-1", "message-1")
        bus = ChatEventBus()
        bus.subscribe(lambda event: manager.append(identity, event))

        bus.publish("conversation_id", {"conversation_id": "session-1"})
        bus.publish("sources", {"items": [{"source": "doc.txt"}]})
        bus.publish("token", {"token": "hello"})
        bus.publish("complete", {}, terminal=True)

        events = manager.read_after(identity, 0)
        self.assertEqual(["conversation_id", "sources", "token", "complete"], [event.event_type for event in events])
        self.assertEqual([1, 2, 3, 4], [event.offset for event in events])
        self.assertTrue(events[-1].terminal)

    def test_event_bus_orders_error_before_terminal_done(self):
        manager = MemoryStreamManager()
        identity = StreamIdentity("session-1", "message-1")
        bus = ChatEventBus()
        bus.subscribe(lambda event: manager.append(identity, event))

        bus.publish("error", {"error": "provider timeout"})
        bus.publish("done", {}, terminal=True)

        events = manager.read_after(identity, 0)
        self.assertEqual(["error", "done"], [event.event_type for event in events])
        self.assertFalse(events[0].terminal)
        self.assertTrue(events[1].terminal)
        self.assertTrue(manager.is_terminal(identity))

    def test_event_bus_orders_stop_before_terminal_done(self):
        manager = MemoryStreamManager()
        identity = StreamIdentity("session-1", "message-1")
        bus = ChatEventBus()
        bus.subscribe(lambda event: manager.append(identity, event))

        bus.publish("stop", {"stop": {"reason": "client_requested"}})
        bus.publish("done", {}, terminal=True)

        events = manager.read_after(identity, 0)
        self.assertEqual(["stop", "done"], [event.event_type for event in events])
        self.assertEqual("client_requested", events[0].payload["stop"]["reason"])
        self.assertTrue(events[1].terminal)


if __name__ == "__main__":
    unittest.main()
