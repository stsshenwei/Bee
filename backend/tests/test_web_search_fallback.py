import unittest
from unittest.mock import patch

from app.services.agent.agent_runtime_tools import TavilySearchProvider
from app.services.web_search_fallback import (
    WEB_FALLBACK_NOTICE,
    EvidenceSufficiencyConfig,
    WebSearchFallbackConfig,
    WebSearchFallbackService,
    assess_evidence_sufficiency,
)


class FakeProvider:
    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.calls = []

    def search(self, query, *, top_k=5):
        self.calls.append({"query": query, "top_k": top_k})
        if self.error:
            raise self.error
        return list(self.results)


class WebSearchFallbackTests(unittest.TestCase):
    def test_tavily_provider_posts_query_and_normalizes_results(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return (
                    b'{"results":[{"title":"Redis docs","url":"https://docs.example.com/redis",'
                    b'"content":"Redis is an in-memory data store.","score":0.91}]}'
                )

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            captured["body"] = request.data.decode("utf-8")
            return Response()

        provider = TavilySearchProvider("tvly-test-key", timeout_seconds=7.0, max_results=5, endpoint="https://api.tavily.test/search")
        with patch("urllib.request.urlopen", fake_urlopen):
            results = provider.search("What is Redis?", top_k=3)

        self.assertEqual("https://api.tavily.test/search", captured["url"])
        self.assertEqual("Bearer tvly-test-key", captured["headers"]["Authorization"])
        self.assertEqual(7.0, captured["timeout"])
        self.assertIn('"query": "What is Redis?"', captured["body"])
        self.assertIn('"max_results": 3', captured["body"])
        self.assertEqual(
            [
                {
                    "title": "Redis docs",
                    "url": "https://docs.example.com/redis",
                    "snippet": "Redis is an in-memory data store.",
                    "score": 0.91,
                }
            ],
            results,
        )

    def test_fallback_service_uses_tavily_when_api_key_is_configured_without_endpoint(self):
        with patch.dict(
            "os.environ",
            {
                "WEB_SEARCH_FALLBACK_ENABLED": "true",
                "WEB_SEARCH_FALLBACK_URL": "",
                "AGENT_RUNTIME_WEB_SEARCH_URL": "",
                "TAVILY_API_KEY": "tvly-test-key",
            },
            clear=False,
        ):
            service = WebSearchFallbackService()

        self.assertIsInstance(service.provider, TavilySearchProvider)

    def test_successful_fallback_normalizes_notice_sources_context_and_metadata(self):
        provider = FakeProvider(
            [
                {
                    "title": "Redis docs",
                    "url": "https://docs.example.com/redis",
                    "snippet": "Redis is an in-memory data store.",
                }
            ]
        )
        service = WebSearchFallbackService(provider=provider, config=WebSearchFallbackConfig(enabled=True, top_k=3))

        result = service.search("What is Redis?", trigger_reason="no_internal_sources", mode="quick")

        self.assertTrue(result.used)
        self.assertEqual(WEB_FALLBACK_NOTICE, result.notice)
        self.assertTrue(result.answer_context.startswith(WEB_FALLBACK_NOTICE))
        self.assertEqual([{"query": "What is Redis?", "top_k": 3}], provider.calls)
        self.assertEqual("web", result.sources[0]["source_type"])
        self.assertEqual("Redis docs", result.sources[0]["source"])
        self.assertEqual("https://docs.example.com/redis", result.sources[0]["url"])
        self.assertEqual("Redis is an in-memory data store.", result.sources[0]["snippet"])
        self.assertEqual("web_search", result.sources[0]["provider"])
        self.assertEqual(
            {
                "attempted": True,
                "used": True,
                "available": True,
                "mode": "quick",
                "trigger_reason": "no_internal_sources",
                "result_count": 1,
                "error": "",
            },
            result.metadata,
        )

    def test_unavailable_fallback_fails_closed_without_provider_call(self):
        provider = FakeProvider([{"title": "ignored"}])
        service = WebSearchFallbackService(provider=provider, config=WebSearchFallbackConfig(enabled=False))

        result = service.search("What is Redis?", trigger_reason="disabled", mode="reasoning")

        self.assertFalse(result.used)
        self.assertEqual([], provider.calls)
        self.assertEqual([], result.sources)
        self.assertIn("网络搜索不可用", result.answer_context)
        self.assertTrue(result.metadata["attempted"])
        self.assertFalse(result.metadata["used"])
        self.assertFalse(result.metadata["available"])

    def test_provider_error_fails_closed_with_safe_metadata(self):
        provider = FakeProvider(error=TimeoutError("secret endpoint timeout"))
        service = WebSearchFallbackService(provider=provider, config=WebSearchFallbackConfig(enabled=True))

        result = service.search("What is Redis?", trigger_reason="low_confidence", mode="wiki")

        self.assertFalse(result.used)
        self.assertEqual([], result.sources)
        self.assertIn("网络搜索失败", result.answer_context)
        self.assertEqual("TimeoutError", result.metadata["error_type"])
        self.assertNotIn("secret endpoint", result.metadata.get("error", ""))

    def test_empty_results_fail_closed(self):
        service = WebSearchFallbackService(provider=FakeProvider([]), config=WebSearchFallbackConfig(enabled=True))

        result = service.search("What is Redis?", trigger_reason="no_results", mode="rag_wiki")

        self.assertFalse(result.used)
        self.assertEqual([], result.sources)
        self.assertIn("未找到可用的知识库或网络搜索证据", result.answer_context)
        self.assertEqual(0, result.metadata["result_count"])

    def test_evidence_sufficiency_classifies_empty_missing_low_and_valid_sources(self):
        config = EvidenceSufficiencyConfig(min_score=0.4)

        empty = assess_evidence_sufficiency([], [], config=config)
        missing_source = assess_evidence_sufficiency([{"hybrid_score": 0.9}], [], config=config)
        low_score = assess_evidence_sufficiency(
            [{"hybrid_score": 0.2, "metadata": {"source": "manual.md"}}],
            [{"source": "manual.md", "score": 0.2}],
            config=config,
        )
        valid = assess_evidence_sufficiency(
            [{"hybrid_score": 0.8, "metadata": {"source": "manual.md"}}],
            [{"source": "manual.md", "score": 0.8}],
            config=config,
        )

        self.assertFalse(empty.sufficient)
        self.assertEqual("no_internal_hits", empty.reason)
        self.assertFalse(missing_source.sufficient)
        self.assertEqual("no_internal_sources", missing_source.reason)
        self.assertFalse(low_score.sufficient)
        self.assertEqual("low_confidence", low_score.reason)
        self.assertTrue(valid.sufficient)
        self.assertEqual("sufficient", valid.reason)


if __name__ == "__main__":
    unittest.main()
