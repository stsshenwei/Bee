import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.models.document_models import Chunk
from app.services.documents.document_repository import DocumentRepository
from tests.test_runtime_config import postgres_runtime_patches


class EnterpriseEvaluationSuiteTests(unittest.TestCase):
    def temp_path(self, name: str) -> Path:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        return Path(tmpdir.name) / name

    def write_dataset(self, root: Path, name: str = "evalset.json") -> Path:
        root.mkdir(parents=True, exist_ok=True)
        path = root / name
        path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "id": "enterprise-smoke",
                    "name": "Enterprise Smoke",
                    "version": "2026.07",
                    "metadata": {"owner": "qa"},
                    "cases": [
                        {
                            "id": "fact-redis",
                            "question": "What does API Gateway use?",
                            "query_type": "fact",
                            "tags": ["smoke"],
                            "expected_answer_terms": ["Redis"],
                            "expected_source_chunk_ids": ["chunk-1"],
                            "expected_tools": ["RawRAGTool"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def make_document_repo(self) -> DocumentRepository:
        repo = DocumentRepository(self.temp_path("metadata.sqlite3"))
        repo.upsert_document("doc-1", "manual.md", ".md", "manual.md", "parsed")
        repo.replace_chunks(
            "doc-1",
            [
                Chunk(
                    id="chunk-1",
                    doc_id="doc-1",
                    parent_id="parent-1",
                    chunk_type="child",
                    title_path="Manual",
                    content="API Gateway uses Redis.",
                    content_markdown="API Gateway uses Redis.",
                    page_start=1,
                    page_end=1,
                    token_count=5,
                )
            ],
        )
        return repo

    def test_repository_persists_runs_results_and_json_fields(self):
        from app.models.evaluation import EvalResultRecord, EvalRunRecord
        from app.services.evaluation.evaluation_repository import EvaluationRepository

        repo = EvaluationRepository(self.temp_path("eval.sqlite3"))

        run = repo.create_run(
            dataset_id="enterprise-smoke",
            dataset_version="2026.07",
            dataset_path="evalsets/smoke.json",
            config_snapshot={"agentic": True},
            knowledge_base_ids=["kb-a"],
        )
        repo.update_run(run["id"], status="completed", aggregate_scores={"citation_resolvable_rate": 1.0}, report_paths={"markdown": "report.md"})
        repo.add_result(
            EvalResultRecord(
                run_id=run["id"],
                case_id="fact-redis",
                status="passed",
                question="What does API Gateway use?",
                query_type="fact",
                tags=["smoke"],
                knowledge_base_ids=["kb-a"],
                case_snapshot={"expected_tools": ["RawRAGTool"]},
                answer="API Gateway uses Redis.",
                response_snapshot={"confidence": 0.9},
                evidence_snapshot={"used_chunks": ["chunk-1"]},
                metric_scores={"citation_resolvable_rate": {"score": 1.0, "passed": True}},
                latency_ms=12.5,
            )
        )

        loaded_run = EvalRunRecord.from_dict(repo.get_run(run["id"]))
        results = repo.list_results(run["id"])

        self.assertEqual("completed", loaded_run.status)
        self.assertEqual({"agentic": True}, loaded_run.config_snapshot)
        self.assertEqual(["kb-a"], loaded_run.knowledge_base_ids)
        self.assertEqual({"markdown": "report.md"}, loaded_run.report_paths)
        self.assertEqual(1, len(results))
        self.assertEqual({"used_chunks": ["chunk-1"]}, results[0]["evidence_snapshot"])
        self.assertEqual(["kb-a"], results[0]["knowledge_base_ids"])

    def test_dataset_loader_validates_json_yaml_and_path_safety(self):
        from app.services.evaluation.evaluation_dataset_loader import EvaluationDatasetLoader

        allowed_root = self.temp_path("evalsets")
        dataset_path = self.write_dataset(allowed_root)
        loader = EvaluationDatasetLoader(allowed_roots=[allowed_root])

        dataset = loader.load(dataset_path)

        self.assertEqual("enterprise-smoke", dataset.id)
        self.assertEqual("fact-redis", dataset.cases[0].id)
        self.assertEqual(["RawRAGTool"], dataset.cases[0].expected_tools)
        self.assertEqual(["chunk-1"], dataset.cases[0].expected_source_chunk_ids)
        with self.assertRaises(ValueError):
            loader.load(self.temp_path("outside.json"))

    def test_rule_based_scorer_scores_citations_graph_tools_and_uncertainty(self):
        from app.models.evaluation import EvalCase, EvaluationAnswerSnapshot
        from app.services.evaluation.evaluation_metrics import NoOpEvaluationJudgeProvider, RuleBasedEvaluationScorer

        scorer = RuleBasedEvaluationScorer(document_repository=self.make_document_repo(), judge_provider=NoOpEvaluationJudgeProvider())
        case = EvalCase(
            id="dependency",
            question="Does API Gateway depend on Redis?",
            query_type="dependency",
            expected_answer_terms=["Redis"],
            expected_source_chunk_ids=["chunk-1"],
            expected_tools=["GraphRetrieverTool"],
            forbidden_tools=["UnapprovedTool"],
        )
        snapshot = EvaluationAnswerSnapshot(
            answer="API Gateway depends on Redis.",
            citations=[{"chunk_id": "chunk-1", "doc_id": "doc-1"}],
            used_chunks=["chunk-1"],
            graph_paths=[{"relations": [{"source_chunk_id": "chunk-1"}]}],
            tool_calls=[{"tool": "GraphRetrieverTool", "status": "completed"}],
            latency_ms=15.0,
        )

        scores = scorer.score(case, snapshot)

        self.assertTrue(scores["citation_resolvable_rate"].passed)
        self.assertTrue(scores["required_source_hit_rate"].passed)
        self.assertTrue(scores["graph_path_traceability_rate"].passed)
        self.assertTrue(scores["tool_plan_match_rate"].passed)
        self.assertEqual(1.0, scores["answer_contains_expected_terms"].score)
        self.assertIn("judge_answer_correctness", scores)

    def test_runner_continues_after_case_failure_and_writes_reports(self):
        from app.services.evaluation.evaluation_dataset_loader import EvaluationDatasetLoader
        from app.services.evaluation.evaluation_metrics import RuleBasedEvaluationScorer
        from app.services.evaluation.evaluation_repository import EvaluationRepository
        from app.services.evaluation.evaluation_reporter import EvaluationReporter
        from app.services.evaluation.evaluation_runner import EvaluationRunner

        class FakeRAG:
            def answer_query(self, question, top_k=None, filters=None):
                if "fail" in question:
                    raise RuntimeError("query failed")
                return {
                    "answer": "API Gateway uses Redis.",
                    "citations": [{"chunk_id": "chunk-1", "doc_id": "doc-1"}],
                    "used_chunks": ["chunk-1"],
                    "used_entities": [{"id": "redis"}],
                    "graph_paths": [],
                    "confidence": 0.91,
                    "agent_trace": [{"stage": "AnalyzeQuestion", "metadata": {"raw_prompt": "secret"}}],
                    "tool_calls": [{"tool": "RawRAGTool", "status": "completed"}],
                    "evidence_summary": {"citations": 1},
                    "debug_info": {"route": "fact"},
                }

        allowed_root = self.temp_path("evalsets")
        dataset_path = self.write_dataset(allowed_root)
        data = json.loads(dataset_path.read_text(encoding="utf-8"))
        data["cases"].append({"id": "broken", "question": "fail this query"})
        dataset_path.write_text(json.dumps(data), encoding="utf-8")
        repo = EvaluationRepository(self.temp_path("eval.sqlite3"))
        reporter = EvaluationReporter(report_dir=self.temp_path("reports"), repository=repo)
        runner = EvaluationRunner(
            rag_service=FakeRAG(),
            repository=repo,
            dataset_loader=EvaluationDatasetLoader([allowed_root]),
            scorer=RuleBasedEvaluationScorer(self.make_document_repo()),
            reporter=reporter,
        )

        run = runner.run(dataset_path)
        results = repo.list_results(run["id"])

        self.assertEqual("partial_failed", repo.get_run(run["id"])["status"])
        self.assertEqual(["failed", "passed"], sorted(item["status"] for item in results))
        passed = [item for item in results if item["status"] == "passed"][0]
        self.assertNotIn("raw_prompt", json.dumps(passed["response_snapshot"]))
        self.assertTrue(Path(repo.get_run(run["id"])["report_paths"]["json"]).exists())
        self.assertTrue(Path(repo.get_run(run["id"])["report_paths"]["markdown"]).exists())

    def test_reporter_compares_runs_against_baseline(self):
        from app.models.evaluation import EvalResultRecord
        from app.services.evaluation.evaluation_repository import EvaluationRepository
        from app.services.evaluation.evaluation_reporter import EvaluationReporter

        repo = EvaluationRepository(self.temp_path("eval.sqlite3"))
        baseline = repo.create_run("enterprise-smoke", "2026.07", "baseline.json")
        current = repo.create_run("enterprise-smoke", "2026.07", "current.json")
        repo.add_result(
            EvalResultRecord(
                run_id=baseline["id"],
                case_id="fact-redis",
                status="failed",
                question="q",
                metric_scores={"required_source_hit_rate": {"score": 0.0, "passed": False}},
            )
        )
        repo.add_result(
            EvalResultRecord(
                run_id=current["id"],
                case_id="fact-redis",
                status="passed",
                question="q",
                metric_scores={"required_source_hit_rate": {"score": 1.0, "passed": True}},
            )
        )
        reporter = EvaluationReporter(report_dir=self.temp_path("reports"), repository=repo)

        comparison = reporter.compare_runs(current["id"], baseline["id"])

        self.assertEqual(1.0, comparison["metric_deltas"]["required_source_hit_rate"])
        self.assertEqual(["fact-redis"], comparison["fixed_cases"])

    def test_eval_routes_start_list_and_inspect_runs(self):
        class FakeEvaluationService:
            def __init__(self):
                self.started = []

            def start_run(self, dataset_path, case_ids=None, baseline_run_id=None):
                self.started.append({"dataset_path": dataset_path, "case_ids": case_ids, "baseline_run_id": baseline_run_id})
                return {"id": "run-1", "status": "completed", "dataset_id": "enterprise-smoke"}

            def list_runs(self):
                return [{"id": "run-1", "status": "completed", "dataset_id": "enterprise-smoke"}]

            def get_run(self, run_id):
                return {"id": run_id, "status": "completed", "aggregate_scores": {"citation_resolvable_rate": 1.0}}

            def list_results(self, run_id):
                return [{"run_id": run_id, "case_id": "fact-redis", "status": "passed"}]

        sys.modules.pop("app.main", None)
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                "OPENAI_API_KEY": "test-key",
                "VECTOR_STORE_DIR": str(Path(tmpdir) / "vector_db"),
                "DATABASE_URL": "postgresql://rag:rag@localhost:5432/rag_test",
                "POSTGRES_SCHEMA": "rag",
                "EVAL_DATASET_DIR": str(Path(tmpdir) / "evalsets"),
                "EVAL_REPORT_DIR": str(Path(tmpdir) / "eval_reports"),
                "RAG_DATA_DIR": str(Path(tmpdir) / "data"),
                "AUTO_INGEST_ON_STARTUP": "false",
                "WIKI_INGEST_ENABLED": "false",
            }
            with patch.dict(os.environ, env, clear=False):
                with postgres_runtime_patches():
                    module = importlib.import_module("app.main")

        fake = FakeEvaluationService()
        module.evaluation_service = fake
        module.rag_service = type("NoStartupRAG", (), {"needs_reingest": lambda self: False})()
        with TestClient(module.app) as client:
            started = client.post("/eval/runs", json={"dataset_path": "smoke.json", "case_ids": ["fact-redis"]})
            listed = client.get("/eval/runs")
            detail = client.get("/eval/runs/run-1")
            results = client.get("/eval/runs/run-1/results")

        self.assertEqual(200, started.status_code)
        self.assertEqual("run-1", started.json()["id"])
        self.assertEqual("smoke.json", fake.started[0]["dataset_path"])
        self.assertEqual("run-1", listed.json()["items"][0]["id"])
        self.assertEqual(1.0, detail.json()["aggregate_scores"]["citation_resolvable_rate"])
        self.assertEqual("fact-redis", results.json()["items"][0]["case_id"])


if __name__ == "__main__":
    unittest.main()
