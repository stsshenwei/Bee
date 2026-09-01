from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from app.models.evaluation import EvalResultRecord
from app.services.storage.postgres import PostgresDatabase
from app.services.storage.postgres_schema import PostgresSchemaConfig, inspect_postgres_startup_storage, qname


class PostgresEvaluationRepository:
    def __init__(
        self,
        database: PostgresDatabase,
        *,
        schema: str | None = None,
        validate_schema: bool = True,
    ):
        self.database = database
        self.schema = schema or database.settings.schema
        if validate_schema:
            inspect_postgres_startup_storage(database, config=PostgresSchemaConfig(schema=self.schema))

    def create_run(
        self,
        dataset_id: str,
        dataset_version: str,
        dataset_path: str,
        config_snapshot: dict[str, Any] | None = None,
        knowledge_base_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        run_id = f"eval-run-{uuid.uuid4().hex[:12]}"
        self._execute(
            f"""
            insert into {self._table('eval_run')}(
                id, dataset_id, dataset_version, dataset_path, status, started_at, finished_at,
                created_at, updated_at, config_snapshot, aggregate_scores, report_paths, error_message,
                knowledge_base_ids_json
            ) values (%s, %s, %s, %s, %s, %s, null, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s::jsonb)
            """,
            (
                run_id,
                dataset_id,
                dataset_version,
                dataset_path,
                "running",
                now,
                now,
                now,
                _dump(config_snapshot or {}),
                _dump({}),
                _dump({}),
                "",
                _dump(knowledge_base_ids or []),
            ),
        )
        return self.get_run(run_id)

    def update_run(self, run_id: str, **updates: Any) -> dict[str, Any]:
        allowed = {"status", "finished_at", "aggregate_scores", "report_paths", "error_message", "config_snapshot"}
        fields: list[str] = []
        params: list[Any] = []
        for key, value in updates.items():
            if key not in allowed:
                continue
            cast = "::jsonb" if key in {"aggregate_scores", "report_paths", "config_snapshot"} else ""
            fields.append(f"{key} = %s{cast}")
            params.append(_dump(value) if cast else value)
        if not fields:
            return self.get_run(run_id)
        fields.append("updated_at = %s")
        params.append(_now())
        params.append(run_id)
        self._execute(f"update {self._table('eval_run')} set {', '.join(fields)} where id = %s", tuple(params))
        return self.get_run(run_id)

    def finish_run(
        self,
        run_id: str,
        status: str,
        aggregate_scores: dict[str, Any] | None = None,
        report_paths: dict[str, str] | None = None,
        error_message: str = "",
    ) -> dict[str, Any]:
        return self.update_run(
            run_id,
            status=status,
            finished_at=_now(),
            aggregate_scores=aggregate_scores or {},
            report_paths=report_paths or {},
            error_message=error_message,
        )

    def add_result(self, result: EvalResultRecord) -> dict[str, Any]:
        result_id = result.id or f"eval-result-{uuid.uuid4().hex[:12]}"
        now = result.created_at or _now()
        self._execute(
            f"""
            insert into {self._table('eval_result')}(
                id, run_id, case_id, status, question, query_type, tags, case_snapshot, answer,
                response_snapshot, evidence_snapshot, metric_scores, latency_ms, error_message, created_at,
                knowledge_base_ids_json
            ) values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s::jsonb)
            """,
            (
                result_id,
                result.run_id,
                result.case_id,
                result.status,
                result.question,
                result.query_type,
                _dump(result.tags),
                _dump(result.case_snapshot),
                result.answer,
                _dump(result.response_snapshot),
                _dump(result.evidence_snapshot),
                _dump(result.metric_scores),
                float(result.latency_ms or 0.0),
                result.error_message,
                now,
                _dump(result.knowledge_base_ids),
            ),
        )
        return self.get_result(result_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self._fetch_one(f"select * from {self._table('eval_run')} where id = %s", (run_id,))
        if row is None:
            raise KeyError(f"Evaluation run not found: {run_id}")
        return self._decode_run(row)

    def list_runs(self) -> list[dict[str, Any]]:
        rows = self._fetch_all(f"select * from {self._table('eval_run')} order by updated_at desc", ())
        return [self._decode_run(row) for row in rows]

    def get_result(self, result_id: str) -> dict[str, Any]:
        row = self._fetch_one(f"select * from {self._table('eval_result')} where id = %s", (result_id,))
        if row is None:
            raise KeyError(f"Evaluation result not found: {result_id}")
        return self._decode_result(row)

    def list_results(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._fetch_all(
            f"select * from {self._table('eval_result')} where run_id = %s order by created_at, case_id",
            (run_id,),
        )
        return [self._decode_result(row) for row in rows]

    def _decode_run(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        for key in ("config_snapshot", "aggregate_scores", "report_paths"):
            data[key] = _load_json(data[key], {})
        data["knowledge_base_ids"] = _load_json(data.pop("knowledge_base_ids_json"), [])
        return data

    def _decode_result(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        for key, default in {
            "tags": [],
            "case_snapshot": {},
            "response_snapshot": {},
            "evidence_snapshot": {},
            "metric_scores": {},
        }.items():
            data[key] = _load_json(data[key], default)
        data["knowledge_base_ids"] = _load_json(data.pop("knowledge_base_ids_json"), [])
        return data

    def _fetch_one(self, sql: str, params: tuple[Any, ...]) -> Any | None:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()

    def _fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
        with self.database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())

    def _execute(self, sql: str, params: tuple[Any, ...]) -> int:
        with self.database.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return int(getattr(cur, "rowcount", 0) or 0)

    def _table(self, table: str) -> str:
        return qname(self.schema, table)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    return json.loads(value or _dump(default))
