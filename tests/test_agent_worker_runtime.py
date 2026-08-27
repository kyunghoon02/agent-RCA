from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from incident_platform.agent_rca import AgentRCAService
from incident_platform.incident_work import (
    InMemoryIncidentAnalysisWorkRepository,
    IncidentWorkQueueSnapshot,
    IncidentWorkQueueStageSnapshot,
)
from incident_platform.knowledge import BoundedKnowledgeRetriever
from tests.test_agent_rca import (
    FailingFakeRunner,
    StaticKnowledgeRepository,
    SuccessfulFakeRunner,
    prepared_repository,
)
from tools.run_agent_worker import (
    AgentWorker,
    AgentWorkerMetrics,
    AgentWorkerRuntimeConfig,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 25, 4, 0, tzinfo=UTC)


def config(**overrides: object) -> AgentWorkerRuntimeConfig:
    values = {
        "worker_id": "agent-worker-runtime-test",
        "run_once": False,
        "target_incident_id": None,
        "poll_interval_seconds": 2,
        "lease_seconds": 180,
        "max_attempts": 3,
        "eligibility_label": "agent_rca_enabled",
        "activated_at": datetime(2026, 8, 22, 1, 0, tzinfo=UTC),
        "min_claim_interval_seconds": 60,
        "circuit_failure_threshold": 3,
        "circuit_cooldown_seconds": 300,
        "metrics_host": "127.0.0.1",
        "metrics_port": 9090,
        "model_name": "fake-agent-model",
        "max_turns": 6,
        "max_llm_calls": 6,
        "max_tool_calls": 12,
        "max_evidence_candidates": 8,
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


def worker_with_runner(
    runner: object,
    *,
    target_prepared: bool = False,
    agent_enabled: bool = True,
    **config_overrides: object,
):
    incidents, incident_id, context_id = prepared_repository(
        agent_enabled=agent_enabled
    )
    if target_prepared:
        config_overrides.update(
            run_once=True,
            target_incident_id=incident_id,
        )
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
        config(**config_overrides),
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

    def test_continuous_worker_does_not_claim_incident_without_opt_in(self) -> None:
        worker, incidents, incident_id, _ = worker_with_runner(
            SuccessfulFakeRunner(),
            agent_enabled=False,
        )

        result = worker.process_one()

        self.assertEqual(result["status"], "IDLE")
        self.assertEqual(incidents.get(incident_id)["status"], "ANALYZING")

    def test_continuous_worker_does_not_claim_incident_before_activation(self) -> None:
        worker, incidents, incident_id, _ = worker_with_runner(
            SuccessfulFakeRunner(),
            activated_at=datetime(2026, 8, 23, tzinfo=UTC),
        )

        result = worker.process_one()

        self.assertEqual(result["status"], "IDLE")
        self.assertEqual(incidents.get(incident_id)["status"], "ANALYZING")

    def test_continuous_worker_rate_limits_claims_after_a_run(self) -> None:
        worker, _, _, _ = worker_with_runner(SuccessfulFakeRunner())

        self.assertEqual(worker.process_one()["status"], "PROCESSED")
        self.assertEqual(worker.process_one()["status"], "RATE_LIMITED")

    def test_consecutive_failure_opens_the_circuit(self) -> None:
        worker, _, _, _ = worker_with_runner(
            FailingFakeRunner(),
            circuit_failure_threshold=1,
        )

        self.assertEqual(worker.process_one()["status"], "FAILED")
        self.assertEqual(worker.process_one()["status"], "CIRCUIT_OPEN")
        self.assertTrue(worker.runtime_state()["circuit_open"])

    def test_agent_wall_budget_must_fit_inside_the_work_lease(self) -> None:
        with self.assertRaisesRegex(ValueError, "leave at least 30 seconds"):
            config(lease_seconds=60, max_wall_time_ms=45_000)

    def test_targeted_run_claims_exactly_the_requested_incident(self) -> None:
        worker, incidents, incident_id, context_id = worker_with_runner(
            SuccessfulFakeRunner(),
            run_once=True,
            target_incident_id="inc-does-not-exist",
        )

        result = worker.process_one()

        self.assertEqual(result["status"], "TARGET_NOT_CLAIMABLE")
        self.assertEqual(result["incident_id"], "inc-does-not-exist")
        self.assertEqual(result["reaped"], 0)
        self.assertEqual(incidents.get(incident_id)["status"], "ANALYZING")
        self.assertEqual(incidents.get_context(context_id)["incident_id"], incident_id)

    def test_targeted_run_processes_the_requested_incident_once(self) -> None:
        worker, incidents, incident_id, _ = worker_with_runner(
            SuccessfulFakeRunner(),
            target_prepared=True,
            agent_enabled=False,
        )

        result = worker.process_one()

        self.assertEqual(result["status"], "PROCESSED")
        self.assertEqual(result["incident_id"], incident_id)
        self.assertEqual(result["reaped"], 0)
        self.assertEqual(incidents.get(incident_id)["status"], "REPORTED")

    def test_one_shot_and_target_must_be_configured_together(self) -> None:
        with self.assertRaisesRegex(ValueError, "configured together"):
            config(run_once=True)
        with self.assertRaisesRegex(ValueError, "configured together"):
            config(target_incident_id="inc-does-not-exist")

    def test_continuous_runtime_requires_an_activation_time(self) -> None:
        with self.assertRaisesRegex(ValueError, "ACTIVATED_AT"):
            config(activated_at=None)

    def test_metrics_expose_usage_and_circuit_state(self) -> None:
        metrics = AgentWorkerMetrics()
        metrics.observe(
            {
                "status": "PROCESSED",
                "llm_calls": 2,
                "tool_calls": 3,
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
                "wall_time_ms": 2500,
            },
            observed_at=NOW,
        )

        queue_snapshot = IncidentWorkQueueSnapshot(
            observed_at=NOW,
            stages=(
                IncidentWorkQueueStageSnapshot(
                    stage="collection",
                    ready=2,
                    running=1,
                    succeeded=10,
                    failed=1,
                    oldest_ready_age_seconds=45,
                    oldest_running_age_seconds=15,
                ),
                IncidentWorkQueueStageSnapshot(
                    stage="localization",
                    ready=1,
                    running=0,
                    succeeded=9,
                    failed=1,
                    oldest_ready_age_seconds=30,
                    oldest_running_age_seconds=0,
                ),
                IncidentWorkQueueStageSnapshot(
                    stage="analysis",
                    ready=1,
                    running=0,
                    succeeded=1,
                    failed=0,
                    oldest_ready_age_seconds=60,
                    oldest_running_age_seconds=0,
                ),
            ),
        )
        rendered = metrics.render(
            {"circuit_open": True, "consecutive_failures": 3},
            queue_snapshot=queue_snapshot,
            queue_observation_success=True,
        ).decode("utf-8")

        self.assertIn('agent_rca_worker_runs_total{outcome="processed"} 1', rendered)
        self.assertIn('agent_rca_worker_tokens_total{type="total"} 150', rendered)
        self.assertIn("agent_rca_worker_circuit_open 1", rendered)
        self.assertIn("agent_rca_work_queue_observation_success 1", rendered)
        self.assertIn(
            'agent_rca_work_items{stage="analysis",state="ready"} 1',
            rendered,
        )
        self.assertIn(
            'agent_rca_work_oldest_ready_age_seconds{stage="analysis"} 60.000',
            rendered,
        )

    def test_metrics_fail_closed_when_queue_observation_is_unavailable(self) -> None:
        rendered = AgentWorkerMetrics().render(
            {"circuit_open": False, "consecutive_failures": 0}
        ).decode("utf-8")

        self.assertIn("agent_rca_work_queue_observation_success 0", rendered)
        self.assertNotIn("agent_rca_work_items{", rendered)


if __name__ == "__main__":
    unittest.main()
