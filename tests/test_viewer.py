from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from incident_platform.agent_rca import AgentRCAService
from incident_platform.errors import ContractViolation
from incident_platform.incidents import AlertmanagerNormalizer
from incident_platform.knowledge import BoundedKnowledgeRetriever
from incident_platform.viewer import IncidentViewerQueryService, ViewerQueryPolicy

from test_agent_rca import (
    NOW,
    StaticKnowledgeRepository,
    SuccessfulFakeRunner,
    prepared_repository,
)


UTC = timezone.utc


def list_query(**overrides: object) -> dict:
    query = {
        "schema_version": "1.0.0",
        "statuses": [],
        "severities": [],
        "namespace": None,
        "search": None,
        "limit": 20,
        "cursor": None,
    }
    query.update(overrides)
    return query


def add_incident(
    repository,
    *,
    suffix: str,
    service: str,
    severity: str,
    received_at: datetime,
) -> str:
    incident = AlertmanagerNormalizer().normalize(
        {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": f"ViewerFixture{suffix}",
                        "namespace": "online-boutique",
                        "service": service,
                        "severity": severity,
                    },
                    "annotations": {},
                    "startsAt": (received_at - timedelta(minutes=2))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "endsAt": "0001-01-01T00:00:00Z",
                    "fingerprint": f"viewer-fixture-{suffix}",
                }
            ]
        },
        received_at=received_at,
    )[0].incident
    repository.create_or_get_by_deduplication_key(
        incident, occurred_at=received_at
    )
    return incident["incident_id"]


class IncidentViewerQueryServiceTests(unittest.TestCase):
    def test_detail_exposes_evidence_provenance_reports_budget_and_timeline(self) -> None:
        repository, incident_id, context_id = prepared_repository()
        AgentRCAService(
            repository,
            BoundedKnowledgeRetriever(StaticKnowledgeRepository()),
            SuccessfulFakeRunner(),
        ).run(incident_id, context_id=context_id, generated_at=NOW)

        detail = IncidentViewerQueryService(repository).get_incident_detail(
            incident_id
        )

        self.assertEqual(detail["incident"]["status"], "REPORTED")
        self.assertEqual(len(detail["evidence"]), 2)
        self.assertTrue(
            all(item["provenance"]["content_hash"] for item in detail["evidence"])
        )
        self.assertEqual(len(detail["contexts"]), 1)
        self.assertEqual(len(detail["reports"]), 1)
        self.assertEqual(len(detail["agent_runs"]), 1)
        self.assertEqual(detail["agent_runs"][0]["usage"]["tool_calls"], 3)
        self.assertIn("# RCA Report", detail["reports"][0]["markdown"])
        event_types = {item["event_type"] for item in detail["timeline"]}
        self.assertIn("EVIDENCE_OBSERVED", event_types)
        self.assertIn("CONTEXT_FROZEN", event_types)
        self.assertIn("AGENT_RUN_COMPLETED", event_types)
        self.assertIn("REPORT_GENERATED", event_types)
        self.assertEqual(
            [item["occurred_at"] for item in detail["timeline"]],
            sorted(item["occurred_at"] for item in detail["timeline"]),
        )
        self.assertFalse(any(detail["truncated"].values()))

    def test_list_supports_filters_search_and_keyset_cursor(self) -> None:
        repository, main_id, _ = prepared_repository()
        payment_id = add_incident(
            repository,
            suffix="payment",
            service="paymentservice",
            severity="warning",
            received_at=NOW + timedelta(minutes=1),
        )
        add_incident(
            repository,
            suffix="frontend",
            service="frontend",
            severity="info",
            received_at=NOW + timedelta(minutes=2),
        )
        viewer = IncidentViewerQueryService(repository)

        filtered = viewer.list_incidents(
            list_query(
                severities=["warning"],
                namespace="online-boutique",
                search="paymentservice",
            )
        )
        self.assertEqual(
            [item["incident_id"] for item in filtered["items"]], [payment_id]
        )

        first = viewer.list_incidents(list_query(limit=1))
        self.assertEqual(len(first["items"]), 1)
        self.assertIsNotNone(first["next_cursor"])
        second = viewer.list_incidents(
            list_query(limit=1, cursor=first["next_cursor"])
        )
        third = viewer.list_incidents(
            list_query(limit=1, cursor=second["next_cursor"])
        )
        self.assertEqual(len(second["items"]), 1)
        self.assertEqual(
            [
                first["items"][0]["source_entity"]["name"],
                second["items"][0]["source_entity"]["name"],
                third["items"][0]["source_entity"]["name"],
            ],
            ["frontend", "paymentservice", "checkoutservice"],
        )
        self.assertEqual(third["items"][0]["incident_id"], main_id)
        self.assertEqual(second["items"][0]["incident_id"], payment_id)

    def test_cursor_is_bound_to_the_original_filters(self) -> None:
        repository, _, _ = prepared_repository()
        add_incident(
            repository,
            suffix="second",
            service="paymentservice",
            severity="warning",
            received_at=NOW + timedelta(minutes=1),
        )
        viewer = IncidentViewerQueryService(repository)
        first = viewer.list_incidents(list_query(limit=1))

        with self.assertRaisesRegex(ContractViolation, "does not match"):
            viewer.list_incidents(
                list_query(
                    limit=1,
                    statuses=["RECEIVED"],
                    cursor=first["next_cursor"],
                )
            )

    def test_detail_reports_explicit_truncation_at_policy_boundary(self) -> None:
        repository, incident_id, _ = prepared_repository()
        viewer = IncidentViewerQueryService(
            repository, policy=ViewerQueryPolicy(max_evidence=1)
        )

        detail = viewer.get_incident_detail(incident_id)

        self.assertEqual(len(detail["evidence"]), 1)
        self.assertTrue(detail["truncated"]["evidence"])

    def test_malformed_cursor_and_response_mutation_are_isolated(self) -> None:
        repository, incident_id, _ = prepared_repository()
        viewer = IncidentViewerQueryService(repository)

        with self.assertRaisesRegex(ContractViolation, "cursor"):
            viewer.list_incidents(list_query(cursor="not-json"))

        detail = viewer.get_incident_detail(incident_id)
        detail["incident"]["status"] = "FAILED"
        self.assertEqual(
            viewer.get_incident_detail(incident_id)["incident"]["status"],
            "ANALYZING",
        )

    def test_page_size_above_policy_is_rejected_before_repository_query(self) -> None:
        repository, _, _ = prepared_repository()
        viewer = IncidentViewerQueryService(
            repository, policy=ViewerQueryPolicy(max_page_size=5)
        )

        with self.assertRaisesRegex(ContractViolation, "page size"):
            viewer.list_incidents(list_query(limit=6))

    def test_work_state_is_schema_valid_read_only_and_hides_claim_token(self) -> None:
        class WorkStateRepository:
            def get(self, incident_id: str) -> dict:
                if incident_id != "inc-viewerwork01":
                    raise KeyError(incident_id)
                return {"incident_id": incident_id}

            def query_work_state(self, incident_id: str) -> dict:
                return {
                    "collection": {
                        "stage": "COLLECTION",
                        "state": "SUCCEEDED",
                        "available_at": "2026-08-26T00:00:00Z",
                        "attempt_count": 1,
                        "worker_id": "incident-worker-a",
                        "lease_expires_at": None,
                        "claimed_at": "2026-08-26T00:00:01Z",
                        "completed_at": "2026-08-26T00:00:03Z",
                        "last_error_code": None,
                        "context_id": None,
                    },
                    "localization": None,
                    "analysis": {
                        "stage": "ANALYSIS",
                        "state": "READY",
                        "available_at": "2026-08-26T00:00:05Z",
                        "attempt_count": 0,
                        "worker_id": None,
                        "lease_expires_at": None,
                        "claimed_at": None,
                        "completed_at": None,
                        "last_error_code": None,
                        "context_id": "ctx-viewerwork01",
                    },
                }

        viewer = IncidentViewerQueryService(WorkStateRepository())

        state = viewer.get_incident_work_state("inc-viewerwork01")

        self.assertEqual(state["analysis"]["state"], "READY")
        self.assertEqual(state["analysis"]["context_id"], "ctx-viewerwork01")
        self.assertNotIn("claim_token", state["collection"])
        state["analysis"]["state"] = "FAILED"
        self.assertEqual(
            viewer.get_incident_work_state("inc-viewerwork01")["analysis"][
                "state"
            ],
            "READY",
        )


if __name__ == "__main__":
    unittest.main()
