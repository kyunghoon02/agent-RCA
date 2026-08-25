from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from incident_platform.agent_rca import AgentRCAService
from incident_platform.incident_work import InMemoryIncidentAnalysisWorkRepository
from incident_platform.knowledge import BoundedKnowledgeRetriever
from tests.test_agent_rca import (
    FailingFakeRunner,
    StaticKnowledgeRepository,
    SuccessfulFakeRunner,
    prepared_repository,
)
from tools.run_agent_worker import AgentWorker, AgentWorkerRuntimeConfig


UTC = timezone.utc
NOW = datetime(2026, 8, 25, 4, 0, tzinfo=UTC)


def config(**overrides: object) -> AgentWorkerRuntimeConfig:
    values = {
        "worker_id": "agent-worker-runtime-test",
        "poll_interval_seconds": 2,
        "lease_seconds": 180,
        "max_attempts": 3,
        "model_name": "fake-agent-model",
        "max_turns": 6,
        "max_llm_calls": 6,
        "max_tool_calls": 12,
        "max_output_tokens": 2000,
        "max_wall_time_ms": 60_000,
        "knowledge_root": "/app/knowledge",
        "knowledge_index_path": "/app/knowledge/index.yaml",
        "postgres_host": "postgresql",
        "postgres_port": 5432,
        "postgres_database": "agent_rca",
        "postgres_username": "agent_rca",
        "postgres_password": "test-only",
    }
    values.update(overrides)
    return AgentWorkerRuntimeConfig(**values)


def worker_with_runner(runner: object):
    incidents, incident_id, context_id = prepared_repository()
    work = InMemoryIncidentAnalysisWorkRepository(incidents)
    work.enqueue(incident_id, context_id=context_id, available_at=NOW)
    service = AgentRCAService(
        incidents,
        BoundedKnowledgeRetriever(
            StaticKnowledgeRepository(), utc_now=lambda: NOW
        ),
        runner,
    )
    worker = AgentWorker(
        config(),
        work,
        service,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    return worker, incidents, incident_id, context_id


class AgentWorkerRuntimeTests(unittest.TestCase):
    def test_context_pinned_claim_runs_agent_and_completes_report(self) -> None:
        worker, incidents, incident_id, context_id = worker_with_runner(
            SuccessfulFakeRunner()
        )

        result = worker.process_one()

        self.assertEqual(result["status"], "PROCESSED")
        self.assertEqual(result["stage"], "ANALYSIS")
        self.assertEqual(result["context_id"], context_id)
        self.assertEqual(result["report_status"], "conclusive")
        self.assertEqual(incidents.get(incident_id)["status"], "REPORTED")
        self.assertEqual(
            incidents.get_report(result["report_id"])["context_id"], context_id
        )
        self.assertNotIn("root_cause", result)

    def test_model_failure_fails_closed_and_persists_work_failure(self) -> None:
        worker, incidents, incident_id, context_id = worker_with_runner(
            FailingFakeRunner()
        )

        result = worker.process_one()

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["stage"], "ANALYSIS")
        self.assertEqual(result["context_id"], context_id)
        self.assertEqual(result["error_code"], "RUNTIMEERROR")
        self.assertEqual(incidents.get(incident_id)["status"], "FAILED")

    def test_agent_wall_budget_must_fit_inside_the_work_lease(self) -> None:
        with self.assertRaisesRegex(ValueError, "leave at least 30 seconds"):
            config(lease_seconds=60, max_wall_time_ms=45_000)


if __name__ == "__main__":
    unittest.main()
