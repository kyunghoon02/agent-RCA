"""Reusable behavioral contracts for future PostgreSQL and provider adapters."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

from incident_platform.deterministic import DeterministicDecision
from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceWindow,
    ResourceScope,
    validate_provider_batch,
)
from incident_platform.errors import InvalidTransition
from incident_platform.reporting import FastPathReportBuilder


FIXED_TIME = datetime(2026, 8, 12, 1, 5, tzinfo=timezone.utc)


class IncidentRepositoryContract:
    """One suite that every IncidentRepository adapter must pass."""

    @staticmethod
    def verify(test_case, repository_factory: Callable, incident: dict, evidence: dict):
        repository = repository_factory()

        def create_once(_: int):
            return repository.create_or_get_by_deduplication_key(
                copy.deepcopy(incident), occurred_at=FIXED_TIME
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(create_once, range(24)))
        test_case.assertEqual(sum(result.created for result in results), 1)
        test_case.assertEqual(
            len({result.incident["incident_id"] for result in results}), 1
        )

        incident_id = incident["incident_id"]
        repository.transition(
            incident_id,
            expected_status="RECEIVED",
            next_status="COLLECTING",
            occurred_at=FIXED_TIME,
        )
        repository.replace_collector_statuses(
            incident_id,
            [
                {
                    "collector": "kubernetes",
                    "status": "SUCCEEDED",
                    "attempts": 1,
                    "started_at": "2026-08-12T01:05:00Z",
                    "ended_at": "2026-08-12T01:05:01Z",
                    "error": None,
                }
            ],
            occurred_at=FIXED_TIME,
        )
        repository.store_evidence(incident_id, [evidence])
        repository.store_evidence(incident_id, [evidence])
        test_case.assertEqual(repository.list_evidence(incident_id), [evidence])

        artifacts = FastPathReportBuilder().build(
            incident=repository.get(incident_id),
            evidence=[evidence],
            decision=DeterministicDecision(
                status="ABSTAIN",
                root_cause_id=None,
                statement=None,
                supporting_evidence_ids=tuple(),
                missing_requirements=("fixture has no complete deterministic signature",),
                evaluations=tuple(),
            ),
            generated_at=FIXED_TIME,
        )
        repository.store_context(artifacts.context)
        repository.store_context(artifacts.context)
        repository.store_report(artifacts.report, artifacts.markdown)
        repository.store_report(artifacts.report, artifacts.markdown)
        test_case.assertEqual(
            repository.get_context(artifacts.context["context_id"]),
            artifacts.context,
        )
        test_case.assertEqual(
            repository.get_report(artifacts.report["report_id"]),
            artifacts.report,
        )
        test_case.assertEqual(
            repository.get_report_markdown(artifacts.report["report_id"]),
            artifacts.markdown,
        )
        agent_audit = {
            "schema_version": "1.0.0",
            "agent_run_id": "arun-contract-fixture-0001",
            "incident_id": incident_id,
            "context_id": artifacts.context["context_id"],
            "knowledge_audit_id": "kaud-contract-fixture-0001",
            "knowledge_status": "NO_MATCH",
            "model": "contract-test-model",
            "status": "MODEL_FAILED",
            "reason_code": "MODEL_EXECUTION_FAILED",
            "started_at": "2026-08-12T01:05:00Z",
            "completed_at": "2026-08-12T01:05:01Z",
            "budget": {
                "max_turns": 6,
                "max_llm_calls": 6,
                "max_tool_calls": 12,
                "max_output_tokens": 2000,
                "max_wall_time_ms": 60000,
            },
            "usage": {
                "llm_calls": 0,
                "tool_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "wall_time_ms": 1000,
            },
            "tool_events": [],
            "retrieved_reference_ids": [],
            "inspected_evidence_ids": [],
            "inspected_reference_document_ids": [],
            "cited_evidence_ids": [],
            "cited_reference_document_ids": [],
        }
        repository.store_agent_run(agent_audit)
        repository.store_agent_run(agent_audit)
        test_case.assertEqual(
            repository.get_agent_run(agent_audit["agent_run_id"]), agent_audit
        )

        resolved = repository.record_alert_resolution(
            incident_id,
            incident_end="2026-08-12T01:10:00Z",
            occurred_at=FIXED_TIME,
        )
        test_case.assertEqual(
            resolved["window"]["incident_end"], "2026-08-12T01:10:00Z"
        )
        repository.record_alert_resolution(
            incident_id,
            incident_end="2026-08-12T01:10:00Z",
            occurred_at=FIXED_TIME,
        )
        with test_case.assertRaisesRegex(InvalidTransition, "different incident_end"):
            repository.record_alert_resolution(
                incident_id,
                incident_end="2026-08-12T01:11:00Z",
                occurred_at=FIXED_TIME,
            )

        conflicting = copy.deepcopy(artifacts.report)
        conflicting["limitations"].append("different content for the same report ID")
        with test_case.assertRaisesRegex(InvalidTransition, "report_id collision"):
            repository.store_report(conflicting, artifacts.markdown)


class ProviderAdapterContract:
    """Minimum behavior every Metrics/Logs/Kubernetes adapter must satisfy."""

    @staticmethod
    def verify(test_case, provider, request: CollectionRequest):
        batch = provider.collect(request)
        validate_provider_batch(batch, request)
        builder = EvidenceBuilder()
        built = [
            builder.build(draft, request, collected_at=FIXED_TIME)
            for draft in batch.items
        ]
        test_case.assertTrue(built)
        for evidence in built:
            test_case.assertEqual(
                evidence["subject"]["namespace"], request.scope.namespace
            )
            test_case.assertIn(
                evidence["subject"]["name"], request.scope.resource_names
            )
            test_case.assertTrue(evidence["provenance"]["provider"])
            test_case.assertTrue(evidence["provenance"]["query"])
            test_case.assertTrue(evidence["provenance"]["locator"])


def contract_request(incident_id: str, resource_name: str) -> CollectionRequest:
    return CollectionRequest(
        request_id="req-contract-00000001",
        incident_id=incident_id,
        window=EvidenceWindow(
            start="2026-08-12T00:30:00Z",
            end="2026-08-12T01:05:00Z",
        ),
        scope=ResourceScope(
            namespace="online-boutique",
            resource_names=(resource_name,),
        ),
        timeout_seconds=1.0,
    )
