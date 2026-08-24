from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from incident_platform.collectors import (
    CollectorOrchestrator,
    CollectorSpec,
    IncidentCollectionService,
)
from incident_platform.errors import InvalidTransition
from incident_platform.evidence import EvidenceDraft, ProviderBatch, ResourceScope
from incident_platform.incident_work import InMemoryIncidentWorkRepository
from incident_platform.incidents import AlertmanagerIngestionService
from incident_platform.repository import InMemoryIncidentRepository


UTC = timezone.utc
NOW = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)


def incident_payload(fingerprint: str) -> dict:
    return {
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "IncidentWorkerContract",
                    "namespace": "online-boutique",
                    "service": "frontend",
                    "severity": "warning",
                },
                "annotations": {},
                "startsAt": "2026-08-24T07:55:00Z",
                "endsAt": "2099-01-01T00:00:00Z",
                "fingerprint": fingerprint,
            }
        ]
    }


class StaticProvider:
    def collect(self, request):
        return ProviderBatch(
            items=(
                EvidenceDraft(
                    source="prometheus",
                    kind="metric-summary",
                    observed_at=request.window.end,
                    subject={
                        "api_version": "v1",
                        "kind": "Service",
                        "namespace": request.scope.namespace,
                        "name": "frontend",
                        "uid": None,
                        "exists": True,
                    },
                    summary="Frontend metric was collected.",
                    facts={"result_status": "HAS_DATA", "latest": 1.0},
                    provider="incident-worker-contract",
                    query="service=frontend",
                    locator="prometheus://frontend/contract",
                ),
            )
        )


class IncidentWorkClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.incidents = InMemoryIncidentRepository()
        self.work = InMemoryIncidentWorkRepository(self.incidents)
        self.incident = AlertmanagerIngestionService(self.incidents).ingest(
            incident_payload("incident-work-contract"),
            received_at=NOW,
        )[0].incident
        self.work.enqueue(self.incident["incident_id"], available_at=NOW)

    def claim(self, *, worker_id: str = "worker-a", now: datetime = NOW):
        return self.work.claim_next(
            worker_id=worker_id,
            now=now,
            lease_duration=timedelta(seconds=30),
            max_attempts=3,
        )

    def test_claim_is_exclusive_and_transitions_incident_atomically(self) -> None:
        claim = self.claim()

        self.assertIsNotNone(claim)
        self.assertEqual(claim.attempt_count, 1)
        self.assertEqual(
            self.incidents.get(self.incident["incident_id"])["status"],
            "COLLECTING",
        )
        self.assertIsNone(self.claim(worker_id="worker-b"))

    def test_expired_claim_is_fenced_when_another_worker_reclaims_it(self) -> None:
        stale = self.claim()
        reclaimed = self.claim(worker_id="worker-b", now=NOW + timedelta(seconds=31))

        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed.attempt_count, 2)
        self.assertNotEqual(stale.claim_token, reclaimed.claim_token)
        with self.assertRaisesRegex(InvalidTransition, "stale"):
            self.work.fail(
                stale,
                now=NOW + timedelta(seconds=32),
                error_code="STALE_WORKER",
            )

    def test_exhausted_crash_retries_fail_closed(self) -> None:
        self.claim(now=NOW)
        self.claim(worker_id="worker-b", now=NOW + timedelta(seconds=31))
        self.claim(worker_id="worker-c", now=NOW + timedelta(seconds=62))

        reaped = self.work.reap_exhausted(
            now=NOW + timedelta(seconds=93),
            max_attempts=3,
        )

        self.assertEqual(reaped, 1)
        self.assertEqual(
            self.incidents.get(self.incident["incident_id"])["status"],
            "FAILED",
        )

    def test_claimed_collection_persists_evidence_then_completes_work(self) -> None:
        claim = self.claim()
        service = IncidentCollectionService(
            self.incidents,
            CollectorOrchestrator(
                [CollectorSpec("prometheus", StaticProvider())]
            ),
        )

        run = service.collect_claimed_incident(
            claim.incident_id,
            scope=ResourceScope(
                namespace="online-boutique",
                resource_names=("frontend",),
                max_items=8,
            ),
            observed_at=NOW + timedelta(seconds=1),
        )
        self.work.complete(
            claim,
            now=NOW + timedelta(seconds=2),
            outcome=run.status,
        )

        self.assertEqual(run.status, "SUCCEEDED")
        self.assertEqual(
            self.incidents.get(claim.incident_id)["status"],
            "LOCALIZING",
        )
        self.assertEqual(len(self.incidents.list_evidence(claim.incident_id)), 1)

    def test_reaper_recovers_when_collection_committed_before_work_completion(self) -> None:
        claim = self.claim()
        service = IncidentCollectionService(
            self.incidents,
            CollectorOrchestrator(
                [CollectorSpec("prometheus", StaticProvider())]
            ),
        )
        service.collect_claimed_incident(
            claim.incident_id,
            scope=ResourceScope(
                namespace="online-boutique",
                resource_names=("frontend",),
                max_items=8,
            ),
            observed_at=NOW + timedelta(seconds=1),
        )

        reaped = self.work.reap_exhausted(
            now=NOW + timedelta(seconds=31),
            max_attempts=3,
        )

        self.assertEqual(reaped, 1)
        self.assertEqual(
            self.incidents.get(claim.incident_id)["status"],
            "LOCALIZING",
        )
        with self.assertRaisesRegex(InvalidTransition, "stale"):
            self.work.complete(
                claim,
                now=NOW + timedelta(seconds=32),
                outcome="SUCCEEDED",
            )


if __name__ == "__main__":
    unittest.main()
