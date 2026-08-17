from __future__ import annotations

import copy
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from incident_platform.contracts import validate_contract
from incident_platform.errors import InvalidAlert, InvalidTransition
from incident_platform.incidents import AlertmanagerIngestionService
from incident_platform.repository import InMemoryIncidentRepository


RECEIVED_AT = datetime(2026, 8, 11, 1, 5, tzinfo=timezone.utc)


def alertmanager_payload(
    *,
    status: str = "firing",
    starts_at: str = "2026-08-11T01:00:00Z",
    ends_at: str = "0001-01-01T00:00:00Z",
) -> dict:
    return {
        "receiver": "incident-platform",
        "status": status,
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": "CheckoutHighErrorRate",
                    "namespace": "online-boutique",
                    "service": "checkoutservice",
                    "severity": "critical",
                },
                "annotations": {
                    "summary": "checkoutservice error rate is above threshold"
                },
                "startsAt": starts_at,
                "endsAt": ends_at,
                "fingerprint": "f1a2b3c4d5e6",
            }
        ],
    }


class AlertmanagerIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryIncidentRepository()
        self.service = AlertmanagerIngestionService(self.repository)

    def test_firing_alert_creates_contract_valid_incident(self) -> None:
        result = self.service.ingest(
            alertmanager_payload(), received_at=RECEIVED_AT
        )[0]

        self.assertTrue(result.created)
        self.assertEqual(result.alert_status, "firing")
        self.assertEqual(result.incident["status"], "RECEIVED")
        self.assertEqual(
            result.incident["window"]["baseline_start"],
            "2026-08-11T00:30:00Z",
        )
        self.assertIsNone(result.incident["window"]["incident_end"])
        self.assertEqual(result.incident["source_entity"]["kind"], "Service")
        self.assertIsNone(result.incident["source_entity"]["uid"])
        validate_contract("incident.schema.json", result.incident)

    def test_repeated_notification_returns_the_existing_incident(self) -> None:
        first = self.service.ingest(
            alertmanager_payload(), received_at=RECEIVED_AT
        )[0]
        second = self.service.ingest(
            alertmanager_payload(), received_at=RECEIVED_AT
        )[0]

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.incident["incident_id"], second.incident["incident_id"])
        self.assertEqual(self.repository.count(), 1)

    def test_same_fingerprint_with_new_start_time_is_a_new_incident(self) -> None:
        first = self.service.ingest(
            alertmanager_payload(), received_at=RECEIVED_AT
        )[0]
        second = self.service.ingest(
            alertmanager_payload(starts_at="2026-08-11T02:00:00Z"),
            received_at=datetime(2026, 8, 11, 2, 5, tzinfo=timezone.utc),
        )[0]

        self.assertNotEqual(first.incident["incident_id"], second.incident["incident_id"])
        self.assertEqual(self.repository.count(), 2)

    def test_resolved_notification_updates_existing_incident_window(self) -> None:
        firing = self.service.ingest(
            alertmanager_payload(), received_at=RECEIVED_AT
        )[0]
        resolved = self.service.ingest(
            alertmanager_payload(
                status="resolved",
                ends_at="2026-08-11T01:12:00Z",
            ),
            received_at=datetime(2026, 8, 11, 1, 12, tzinfo=timezone.utc),
        )[0]

        self.assertFalse(resolved.created)
        self.assertEqual(
            resolved.incident["incident_id"], firing.incident["incident_id"]
        )
        self.assertEqual(
            resolved.incident["window"]["incident_end"],
            "2026-08-11T01:12:00Z",
        )
        self.assertEqual(
            [event.event_type for event in self.repository.list_audit_events(
                firing.incident["incident_id"]
            )],
            ["INCIDENT_CREATED", "ALERT_RESOLVED"],
        )

    def test_concurrent_retries_create_only_one_incident(self) -> None:
        payload = alertmanager_payload()

        def ingest_once(_: int):
            return self.service.ingest(payload, received_at=RECEIVED_AT)[0]

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(ingest_once, range(32)))

        self.assertEqual(sum(result.created for result in results), 1)
        self.assertEqual(self.repository.count(), 1)
        self.assertEqual(len({result.incident["incident_id"] for result in results}), 1)

    def test_missing_namespace_is_rejected_before_persistence(self) -> None:
        payload = alertmanager_payload()
        del payload["alerts"][0]["labels"]["namespace"]

        with self.assertRaisesRegex(InvalidAlert, "namespace"):
            self.service.ingest(payload, received_at=RECEIVED_AT)
        self.assertEqual(self.repository.count(), 0)

    def test_unscoped_resource_is_rejected_before_persistence(self) -> None:
        payload = alertmanager_payload()
        del payload["alerts"][0]["labels"]["service"]

        with self.assertRaisesRegex(InvalidAlert, "must identify"):
            self.service.ingest(payload, received_at=RECEIVED_AT)
        self.assertEqual(self.repository.count(), 0)

    def test_unsupported_severity_is_rejected(self) -> None:
        payload = alertmanager_payload()
        payload["alerts"][0]["labels"]["severity"] = "page"

        with self.assertRaisesRegex(InvalidAlert, "unsupported severity"):
            self.service.ingest(payload, received_at=RECEIVED_AT)

    def test_repository_copy_cannot_mutate_stored_incident(self) -> None:
        result = self.service.ingest(
            alertmanager_payload(), received_at=RECEIVED_AT
        )[0]
        caller_copy = copy.deepcopy(result.incident)
        caller_copy["status"] = "REPORTED"

        stored = self.repository.get(result.incident["incident_id"])
        self.assertEqual(stored["status"], "RECEIVED")


class IncidentTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryIncidentRepository()
        service = AlertmanagerIngestionService(self.repository)
        self.incident = service.ingest(
            alertmanager_payload(), received_at=RECEIVED_AT
        )[0].incident

    def test_happy_path_transitions_are_audited(self) -> None:
        incident_id = self.incident["incident_id"]
        collecting = self.repository.transition(
            incident_id,
            expected_status="RECEIVED",
            next_status="COLLECTING",
            occurred_at=datetime(2026, 8, 11, 1, 5, 1, tzinfo=timezone.utc),
        )
        localizing = self.repository.transition(
            incident_id,
            expected_status="COLLECTING",
            next_status="LOCALIZING",
            occurred_at=datetime(2026, 8, 11, 1, 5, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(collecting["status"], "COLLECTING")
        self.assertEqual(localizing["status"], "LOCALIZING")
        self.assertEqual(
            [event.event_type for event in self.repository.list_audit_events(incident_id)],
            ["INCIDENT_CREATED", "STATUS_TRANSITIONED", "STATUS_TRANSITIONED"],
        )

    def test_skipping_lifecycle_stage_is_rejected(self) -> None:
        with self.assertRaisesRegex(InvalidTransition, "not allowed"):
            self.repository.transition(
                self.incident["incident_id"],
                expected_status="RECEIVED",
                next_status="ANALYZING",
                occurred_at=RECEIVED_AT,
            )

    def test_stale_expected_status_is_rejected(self) -> None:
        incident_id = self.incident["incident_id"]
        self.repository.transition(
            incident_id,
            expected_status="RECEIVED",
            next_status="COLLECTING",
            occurred_at=RECEIVED_AT,
        )

        with self.assertRaisesRegex(InvalidTransition, "stale transition"):
            self.repository.transition(
                incident_id,
                expected_status="RECEIVED",
                next_status="FAILED",
                occurred_at=RECEIVED_AT,
            )

    def test_terminal_status_cannot_transition(self) -> None:
        incident_id = self.incident["incident_id"]
        self.repository.transition(
            incident_id,
            expected_status="RECEIVED",
            next_status="FAILED",
            occurred_at=RECEIVED_AT,
        )

        with self.assertRaisesRegex(InvalidTransition, "not allowed"):
            self.repository.transition(
                incident_id,
                expected_status="FAILED",
                next_status="COLLECTING",
                occurred_at=RECEIVED_AT,
            )


if __name__ == "__main__":
    unittest.main()
