import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models.document_models import Chunk
from app.services.documents.document_repository import DocumentRepository
from app.services.knowledge.knowledge_base_repository import KnowledgeBaseRepository
from app.services.knowledge.knowledge_base_service import KnowledgeBaseService
from app.services.storage.storage_schema import initialize_metadata_database
from app.services.wiki.wiki_repository import WikiRepository, WikiVersionConflictError
from app.services.wiki.wiki_service import WikiPageService, WikiValidationError


class WikiRepositoryServiceTests(unittest.TestCase):
    def _services(self, path: Path):
        kb_repo = KnowledgeBaseRepository(path)
        kb_service = KnowledgeBaseService(kb_repo)
        wiki_service = WikiPageService(WikiRepository(path), kb_service)
        kb = kb_service.create("Wiki KB", knowledge_base_type="wiki")
        scope = kb_service.resolve_scope([kb.id])
        return kb_service, wiki_service, scope

    def test_schema_initializes_wiki_tables_and_constraints(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            initialize_metadata_database(path)

            conn = sqlite3.connect(path)
            try:
                tables = {
                    row[0]
                    for row in conn.execute("select name from sqlite_master where type = 'table'").fetchall()
                }
                self.assertTrue(
                    {
                        "wiki_page",
                        "wiki_folder",
                        "wiki_page_issue",
                        "wiki_page_proposal",
                        "wiki_page_source_ref",
                        "wiki_generation_task",
                    }.issubset(tables)
                )
            finally:
                conn.close()

    def test_pages_are_scoped_unique_and_archived_slug_can_be_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, wiki_service, scope = self._services(Path(tmp) / "metadata.sqlite3")
            page = wiki_service.create_page(scope, {"title": "API Gateway", "status": "published"})

            with self.assertRaises(sqlite3.IntegrityError):
                wiki_service.create_page(scope, {"title": "API Gateway", "status": "draft"})

            archived = wiki_service.archive_page(scope, page.slug)
            replacement = wiki_service.create_page(scope, {"title": "API Gateway", "status": "draft"})

            self.assertEqual("archived", archived.status)
            self.assertEqual(page.slug, replacement.slug)

    def test_page_commits_are_optimistic_idempotent_and_preserve_generation_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, wiki_service, scope = self._services(Path(tmp) / "metadata.sqlite3")
            repository = wiki_service.repository
            created = repository.create_page(
                scope,
                slug="onu",
                title="ONU",
                status="published",
                metadata={"generated": True, "source_revision": "r1", "protected_manual_content": ["notes"]},
            )
            committed = repository.update_page(
                scope,
                "onu",
                {"summary": "Managed ONU", "metadata": {**created.metadata, "generation_run_id": "run-1"}},
                expected_version=created.version,
                idempotency_key="reduce:run-1:onu",
                generation_run_id="run-1",
            )
            duplicate = repository.update_page(
                scope,
                "onu",
                {"summary": "must not replace committed content"},
                expected_version=created.version,
                idempotency_key="reduce:run-1:onu",
                generation_run_id="run-1",
            )

            self.assertEqual(committed.version, duplicate.version)
            self.assertEqual("Managed ONU", duplicate.summary)
            self.assertEqual(["notes"], duplicate.metadata["protected_manual_content"])
            self.assertEqual("run-1", duplicate.metadata["generation_run_id"])
            with self.assertRaises(WikiVersionConflictError):
                repository.update_page(scope, "onu", {"summary": "stale"}, expected_version=created.version)

    def test_pages_with_same_slug_are_isolated_across_knowledge_bases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(path))
            wiki_service = WikiPageService(WikiRepository(path), kb_service)
            kb_a = kb_service.create("Wiki A", knowledge_base_type="wiki")
            kb_b = kb_service.create("Wiki B", knowledge_base_type="wiki")
            scope_a = kb_service.resolve_scope([kb_a.id])
            scope_b = kb_service.resolve_scope([kb_b.id])

            page_a = wiki_service.create_page(scope_a, {"slug": "redis", "title": "Redis A"})
            page_b = wiki_service.create_page(scope_b, {"slug": "redis", "title": "Redis B"})

            self.assertEqual("Redis A", page_a.title)
            self.assertEqual("Redis B", page_b.title)
            self.assertEqual("Redis A", wiki_service.get_page(scope_a, "redis").title)
            self.assertEqual("Redis B", wiki_service.get_page(scope_b, "redis").title)

    def test_pending_batch_claim_is_scoped_due_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(path))
            repository = WikiRepository(path)
            kb_a = kb_service.create("Wiki A", knowledge_base_type="wiki")
            kb_b = kb_service.create("Wiki B", knowledge_base_type="wiki")
            scope_a = kb_service.resolve_scope([kb_a.id])
            scope_b = kb_service.resolve_scope([kb_b.id])
            now = datetime.now(timezone.utc)
            due = (now - timedelta(seconds=1)).isoformat()
            future = (now + timedelta(minutes=5)).isoformat()
            claim_at = now.isoformat()

            repository.enqueue_pending(scope_a, document_id="doc-a1", document_revision="r1", available_at=due)
            repository.enqueue_pending(scope_a, document_id="doc-a2", document_revision="r1", available_at=due)
            repository.enqueue_pending(scope_a, document_id="doc-future", document_revision="r1", available_at=future)
            repository.enqueue_pending(scope_b, document_id="doc-b1", document_revision="r1", available_at=due)

            first = repository.claim_pending_batch(scope_a, limit=1, now=claim_at)
            second = repository.claim_pending_batch(scope_a, limit=5, now=claim_at)

            self.assertEqual(1, len(first))
            self.assertEqual(1, len(second))
            self.assertEqual({"doc-a1", "doc-a2"}, {first[0]["document_id"], second[0]["document_id"]})
            self.assertEqual("claimed", first[0]["status"])
            self.assertEqual("pending", repository.list_pending(scope_a)[-1]["status"])
            self.assertEqual("doc-b1", repository.claim_pending_batch(scope_b, now=claim_at)[0]["document_id"])

    def test_contribution_queries_report_changes_affected_slugs_and_orphans(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, wiki_service, scope = self._services(Path(tmp) / "metadata.sqlite3")
            repository = wiki_service.repository
            original = [
                {"page_slug": "onu", "page_type": "entity", "summary": "old"},
                {"page_slug": "gpon", "page_type": "concept", "summary": "stable"},
            ]
            first_affected = repository.replace_document_contributions(
                scope,
                document_id="missing-doc",
                document_revision="r1",
                generation_run_id="run-1",
                contributions=original,
            )
            duplicate_affected = repository.replace_document_contributions(
                scope,
                document_id="missing-doc",
                document_revision="r1",
                generation_run_id="run-1",
                contributions=[],
            )
            changes = repository.contribution_changes(
                scope,
                document_id="missing-doc",
                proposed=[
                    {"page_slug": "onu", "page_type": "entity", "summary": "new"},
                    {"page_slug": "olt", "page_type": "entity", "summary": "added"},
                ],
            )

            self.assertEqual(["olt"], changes["additions"])
            self.assertEqual(["onu"], changes["replacements"])
            self.assertEqual(["gpon"], changes["retractions"])
            self.assertEqual(["gpon", "olt", "onu"], changes["affected_slugs"])
            self.assertEqual(first_affected, duplicate_affected)
            self.assertEqual(2, len(repository.orphaned_contributions(scope)))

    def test_page_keyset_pagination_and_alias_search_are_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, wiki_service, scope = self._services(Path(tmp) / "metadata.sqlite3")
            for slug, title, aliases in [
                ("onu", "光网络单元", ["ONU", "光猫"]),
                ("olt", "光线路终端", ["OLT"]),
                ("gpon", "GPON", ["吉比特无源光网络"]),
            ]:
                wiki_service.create_page(
                    scope,
                    {"slug": slug, "title": title, "aliases": aliases, "status": "published", "summary": f"{title} 技术说明"},
                )

            first = wiki_service.list_pages(scope, status="published", limit=2)
            second = wiki_service.list_pages(scope, status="published", limit=2, cursor=first["next_cursor"])
            alias_matches = wiki_service.search_pages(scope, "光猫", limit=5)

            self.assertIsNotNone(first["next_cursor"])
            self.assertEqual(2, len(first["items"]))
            self.assertEqual(1, len(second["items"]))
            self.assertFalse({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})
            self.assertEqual(["onu"], [item["slug"] for item in alias_matches])
            with self.assertRaisesRegex(ValueError, "cursor"):
                wiki_service.list_pages(scope, cursor="not-a-cursor")

    def test_link_refresh_graph_issue_and_proposal_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, wiki_service, scope = self._services(Path(tmp) / "metadata.sqlite3")
            folder = wiki_service.create_folder(scope, {"name": "Runtime"})
            with self.assertRaises(sqlite3.IntegrityError):
                wiki_service.create_folder(scope, {"name": "Runtime"})
            wiki_service.create_page(scope, {"slug": "redis", "title": "Redis", "status": "published"})
            gateway = wiki_service.create_page(
                scope,
                {
                    "slug": "api-gateway",
                    "title": "API Gateway",
                    "status": "published",
                    "content_markdown": "Uses [[redis|Redis cache]].",
                    "folder_id": folder.id,
                    "source_refs": [{"doc_id": "doc-1", "chunk_id": "c1"}],
                },
            )

            redis = wiki_service.get_page(scope, "redis")
            graph = wiki_service.graph(scope)
            source_pages = wiki_service.repository.pages_by_source_ref(scope, doc_id="doc-1", chunk_id="c1")
            issue = wiki_service.create_issue(scope, {"slug": gateway.slug, "description": "Need fresher source"})
            proposal = wiki_service.create_proposal(
                scope,
                {
                    "action": "replace_text",
                    "slug": gateway.slug,
                    "payload": {"old_text": "Uses", "new_text": "Depends on"},
                    "reason": "clearer wording",
                },
            )
            applied = wiki_service.apply_proposal(scope, proposal.id)

            self.assertEqual(("redis",), gateway.out_links)
            self.assertEqual(("api-gateway",), redis.in_links)
            self.assertEqual(2, graph["meta"]["node_count"])
            self.assertEqual(["api-gateway"], [page.slug for page in source_pages])
            self.assertEqual("open", issue.status)
            self.assertEqual("applied", applied["proposal"]["status"])
            self.assertIn("Depends on", applied["page"]["content_markdown"])

    def test_nested_folder_moves_recompute_paths_and_reject_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, wiki_service, scope = self._services(Path(tmp) / "metadata.sqlite3")
            root = wiki_service.create_folder(scope, {"name": "网络技术"})
            child = wiki_service.create_folder(scope, {"name": "PON", "parent_id": root.id})
            leaf = wiki_service.create_folder(scope, {"name": "ONU", "parent_id": child.id})
            other = wiki_service.create_folder(scope, {"name": "硬件设施"})

            moved = wiki_service.update_folder(scope, child.id, {"parent_id": other.id, "name": "接入网"})
            updated_leaf = wiki_service.repository.get_folder(scope, leaf.id)

            self.assertEqual("硬件设施/接入网", moved.path)
            self.assertEqual("硬件设施/接入网/ONU", updated_leaf.path)
            with self.assertRaisesRegex(ValueError, "cycle"):
                wiki_service.update_folder(scope, other.id, {"parent_id": leaf.id})

    def test_link_refresh_resolves_aliases_and_removes_unavailable_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, wiki_service, scope = self._services(Path(tmp) / "metadata.sqlite3")
            wiki_service.create_page(
                scope,
                {"slug": "redis", "title": "Redis", "aliases": ["cache"], "status": "published"},
            )
            wiki_service.create_page(
                scope,
                {
                    "slug": "gateway", "title": "Gateway", "status": "published",
                    "content_markdown": "Uses [[cache|缓存]] and [[missing|缺失页面]].",
                },
            )

            wiki_service.refresh_links(scope)
            gateway = wiki_service.get_page(scope, "gateway")

            self.assertIn("[[redis|缓存]]", gateway.content_markdown)
            self.assertIn("缺失页面", gateway.content_markdown)
            self.assertNotIn("[[missing", gateway.content_markdown)
            self.assertEqual(("redis",), gateway.out_links)

    def test_issue_filters_and_cleanup_choose_deterministic_or_reviewable_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, wiki_service, scope = self._services(Path(tmp) / "metadata.sqlite3")
            wiki_service.create_page(scope, {"slug": "onu", "title": "ONU", "status": "published"})
            dead = wiki_service.create_issue(scope, {"slug": "onu", "issue_type": "dead_link", "description": "dead"})
            unsupported = wiki_service.create_issue(scope, {"slug": "onu", "issue_type": "ungrounded_content", "description": "review"})

            filtered = wiki_service.list_issues(scope, issue_type="dead_link")
            cleaned = wiki_service.cleanup_issue(scope, dead.id)
            review = wiki_service.cleanup_issue(scope, unsupported.id)

            self.assertEqual([dead.id], [item["id"] for item in filtered])
            self.assertTrue(cleaned["cleaned"])
            self.assertEqual("resolved", cleaned["issue"]["status"])
            self.assertFalse(review["cleaned"])
            self.assertEqual("delete_page", review["proposal"]["action"])

    def test_mark_source_stale_updates_page_metadata_and_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, wiki_service, scope = self._services(Path(tmp) / "metadata.sqlite3")
            page = wiki_service.create_page(
                scope,
                {
                    "slug": "api-gateway",
                    "title": "API Gateway",
                    "status": "published",
                    "source_refs": [{"doc_id": "doc-1", "chunk_id": "c1"}],
                },
            )

            updated = wiki_service.mark_source_stale(scope, doc_id="doc-1", reason="source_deleted")
            issues = wiki_service.list_issues(scope, slug=page.slug)

            self.assertEqual(1, len(updated))
            self.assertEqual("stale", updated[0]["status"])
            self.assertEqual("source_deleted", updated[0]["metadata"]["stale_reason"])
            self.assertEqual("outdated", issues[0]["issue_type"])

    def test_wiki_capability_requires_wiki_type_or_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(path))
            wiki_service = WikiPageService(WikiRepository(path), kb_service)
            document_kb = kb_service.create("Document KB", knowledge_base_type="document")
            disabled_scope = kb_service.resolve_scope([document_kb.id])

            with self.assertRaises(WikiValidationError):
                wiki_service.create_page(disabled_scope, {"title": "No Wiki"})

            updated = kb_service.update(document_kb.id, indexing_strategy={"wiki_enabled": True})
            enabled_scope = kb_service.resolve_scope([updated.id])
            page = wiki_service.create_page(enabled_scope, {"title": "Enabled Wiki"})

            self.assertEqual("enabled-wiki", page.slug)

    def test_generation_creates_draft_from_source_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            kb_repo = KnowledgeBaseRepository(path)
            kb_service = KnowledgeBaseService(kb_repo)
            doc_repo = DocumentRepository(path)
            wiki_service = WikiPageService(WikiRepository(path), kb_service, doc_repo)
            kb = kb_service.create("Wiki KB", knowledge_base_type="wiki")
            scope = kb_service.resolve_scope([kb.id])
            self._seed_document(doc_repo, scope)

            result = wiki_service.generate_draft_for_document(scope, "doc-1")

            self.assertEqual("completed", result["task"]["status"])
            self.assertEqual("draft", result["page"]["status"])
            self.assertEqual("api-handbook", result["page"]["slug"])
            self.assertEqual(["chunk-parent-1", "chunk-table-1"], result["page"]["chunk_refs"])
            self.assertIn("Source-Bound Notes", result["page"]["content_markdown"])

    def test_generation_skips_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(path))
            doc_repo = DocumentRepository(path)
            wiki_service = WikiPageService(WikiRepository(path), kb_service, doc_repo, generation_enabled=False)
            kb = kb_service.create("Wiki KB", knowledge_base_type="wiki")
            scope = kb_service.resolve_scope([kb.id])
            self._seed_document(doc_repo, scope)

            result = wiki_service.generate_draft_for_document(scope, "doc-1")

            self.assertEqual("skipped", result["task"]["status"])
            self.assertIsNone(result["page"])

    def test_generation_auto_publish_and_missing_source_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(path))
            doc_repo = DocumentRepository(path)
            wiki_service = WikiPageService(WikiRepository(path), kb_service, doc_repo)
            kb = kb_service.create("Wiki KB", knowledge_base_type="wiki")
            scope = kb_service.resolve_scope([kb.id])
            self._seed_document(doc_repo, scope)

            published = wiki_service.generate_draft_for_document(scope, "doc-1", auto_publish=True)
            doc_repo.upsert_document(
                id="doc-empty",
                name="Empty.md",
                file_type="md",
                storage_path="uploads/empty.md",
                parse_status="parsed",
                metadata_json={},
                workspace_id=scope.workspace_id,
                knowledge_base_id=scope.knowledge_base_id,
            )
            failed = wiki_service.generate_draft_for_document(scope, "doc-empty")

            self.assertEqual("published", published["page"]["status"])
            self.assertEqual("failed", failed["task"]["status"])
            self.assertIn("No indexed source chunks", failed["task"]["error_message"])

    def test_generation_creates_proposal_for_existing_published_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.sqlite3"
            kb_service = KnowledgeBaseService(KnowledgeBaseRepository(path))
            doc_repo = DocumentRepository(path)
            wiki_service = WikiPageService(WikiRepository(path), kb_service, doc_repo)
            kb = kb_service.create("Wiki KB", knowledge_base_type="wiki")
            scope = kb_service.resolve_scope([kb.id])
            self._seed_document(doc_repo, scope)
            wiki_service.create_page(scope, {"slug": "api-handbook", "title": "API Handbook", "status": "published"})

            result = wiki_service.generate_draft_for_document(scope, "doc-1")

            self.assertEqual("completed", result["task"]["status"])
            self.assertIsNone(result["page"])
            self.assertEqual("pending", result["proposal"]["status"])
            self.assertEqual("write_page", result["proposal"]["action"])

    def _seed_document(self, doc_repo: DocumentRepository, scope):
        doc_repo.upsert_document(
            id="doc-1",
            name="API Handbook.md",
            file_type="md",
            storage_path="uploads/api-handbook.md",
            parse_status="parsed",
            metadata_json={},
            workspace_id=scope.workspace_id,
            knowledge_base_id=scope.knowledge_base_id,
        )
        doc_repo.replace_chunks(
            "doc-1",
            [
                Chunk(
                    id="chunk-parent-1",
                    doc_id="doc-1",
                    parent_id=None,
                    chunk_type="parent",
                    title_path="Overview",
                    content="The API gateway routes traffic and verifies authentication tokens.",
                    content_markdown="The API gateway routes traffic and verifies authentication tokens.",
                    page_start=None,
                    page_end=None,
                    token_count=12,
                    metadata={},
                ),
                Chunk(
                    id="chunk-table-1",
                    doc_id="doc-1",
                    parent_id=None,
                    chunk_type="table",
                    title_path="Limits",
                    content="| Limit | Value |\n| Requests | 1000/min |",
                    content_markdown="| Limit | Value |\n| Requests | 1000/min |",
                    page_start=None,
                    page_end=None,
                    token_count=10,
                    metadata={},
                ),
            ],
            scope,
        )


if __name__ == "__main__":
    unittest.main()
