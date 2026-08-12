import unittest

from app.models.knowledge_base import (
    EffectiveKnowledgeBaseConfig,
    IndexingStrategy,
    KnowledgeBase,
    KnowledgeBaseAggregate,
    KnowledgeBaseScope,
    ProviderReferences,
    Workspace,
)


class KnowledgeBaseModelTests(unittest.TestCase):
    def test_domain_models_serialize_stable_contract(self):
        workspace = Workspace(id="ws-1", name="研发")
        knowledge_base = KnowledgeBase(
            id="kb-1",
            workspace_id=workspace.id,
            name="产品文档",
            indexing_strategy=IndexingStrategy(graph_enabled=True),
            provider_config=EffectiveKnowledgeBaseConfig(
                requested=ProviderReferences(embedding="qwen"),
                effective=ProviderReferences(),
                inactive_overrides=("embedding",),
            ),
            aggregate=KnowledgeBaseAggregate(document_count=2, indexed_chunk_count=8),
        )

        payload = knowledge_base.to_dict()

        self.assertEqual("kb-1", payload["id"])
        self.assertTrue(payload["indexing_strategy"]["graph_enabled"])
        self.assertEqual("qwen", payload["provider_config"]["requested"]["embedding"])
        self.assertEqual(["embedding"], payload["provider_config"]["inactive_overrides"])
        self.assertEqual(2, payload["aggregate"]["document_count"])

    def test_scope_normalizes_ids_round_trips_and_requires_single_scope_when_requested(self):
        scope = KnowledgeBaseScope(
            workspace_id=" ws-1 ",
            selected_knowledge_base_ids=("kb-1", "kb-1", " kb-2 "),
            document_ids=("doc-1", "doc-1"),
            compatibility_default=True,
        )

        restored = KnowledgeBaseScope.from_dict(scope.to_dict())

        self.assertEqual(("kb-1", "kb-2"), restored.selected_knowledge_base_ids)
        self.assertEqual(("doc-1",), restored.document_ids)
        self.assertTrue(restored.compatibility_default)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            _ = restored.knowledge_base_id

    def test_scope_rejects_empty_identity(self):
        with self.assertRaisesRegex(ValueError, "workspace_id"):
            KnowledgeBaseScope(workspace_id="", selected_knowledge_base_ids=("kb-1",))
        with self.assertRaisesRegex(ValueError, "knowledge_base_id"):
            KnowledgeBaseScope(workspace_id="ws-1", selected_knowledge_base_ids=())

    def test_wiki_default_strategy_retains_chunks_without_retrieval_indexes(self):
        strategy = IndexingStrategy.default_for_type("wiki")

        self.assertTrue(strategy.wiki_enabled)
        self.assertFalse(strategy.dense_enabled)
        self.assertFalse(strategy.keyword_enabled)
        self.assertFalse(strategy.graph_enabled)
        self.assertFalse(strategy.needs_embedding)
        self.assertTrue(strategy.needs_chunks)


if __name__ == "__main__":
    unittest.main()
