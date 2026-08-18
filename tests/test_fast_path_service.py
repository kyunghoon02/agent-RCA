from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone

from incident_platform.collectors import (
    CollectorOrchestrator,
    CollectorSpec,
    IncidentCollectionService,
)
from incident_platform.errors import ContractViolation, InvalidTransition
from incident_platform.evidence import EvidenceDraft, ProviderBatch, ResourceScope
from incident_platform.fast_path import IncidentFastPathService
from incident_platform.incidents import AlertmanagerIngestionService
from incident_platform.repository import InMemoryIncidentRepository

from test_deterministic_rca import FIXTURE_DIR, load_fixture
from test_fast_path_reporting import GENERATED_AT, incident_for


class FastPathServiceTests(unittest.TestCase):
    def repository_with(self, fixture_name: str, *, store_evidence: bool = True):
        _, evidence = load_fixture(FIXTURE_DIR / fixture_name)
        incident = incident_for(evidence)
        repository = InMemoryIncidentRepository()
        repository.create_or_get_by_deduplication_key(
            incident, occurred_at=GENERATED_AT
        )
        if store_evidence:
            repository.store_evidence(incident["incident_id"], evidence)
        return repository, incident, evidence

    def test_proven_evidence_is_persisted_and_incident_becomes_reported(self) -> None:
        repository, incident, _ = self.repository_with("oomkilled.json")

        run = IncidentFastPathService(repository).run(
            incident["incident_id"], generated_at=GENERATED_AT
        )

        self.assertEqual(run.decision.status, "PROVEN")
        self.assertEqual(run.artifacts.report["status"], "conclusive")
        self.assertEqual(run.incident["status"], "REPORTED")
        self.assertEqual(
            repository.get_context(run.artifacts.context["context_id"]),
            run.artifacts.context,
        )
        self.assertEqual(
            repository.get_report(run.artifacts.report["report_id"]),
            run.artifacts.report,
        )
        self.assertEqual(
            repository.get_report_markdown(run.artifacts.report["report_id"]),
            run.artifacts.markdown,
        )
        self.assertEqual(
            [event.event_type for event in repository.list_audit_events(
                incident["incident_id"]
            )],
            ["INCIDENT_CREATED", "STATUS_TRANSITIONED", "STATUS_TRANSITIONED"],
        )

    def test_insufficient_evidence_persists_inconclusive_report(self) -> None:
        repository, incident, _ = self.repository_with("insufficient-oom.json")

        run = IncidentFastPathService(repository).run(
            incident["incident_id"], generated_at=GENERATED_AT
        )

        self.assertEqual(run.decision.status, "ABSTAIN")
        self.assertEqual(run.artifacts.report["status"], "inconclusive")
        self.assertIsNone(run.artifacts.report["root_cause"])
        self.assertEqual(run.incident["status"], "REPORTED")

    def test_no_evidence_marks_analysis_failed(self) -> None:
        repository, incident, _ = self.repository_with(
            "oomkilled.json", store_evidence=False
        )

        with self.assertRaisesRegex(ContractViolation, "requires at least one Evidence"):
            IncidentFastPathService(repository).run(
                incident["incident_id"], generated_at=GENERATED_AT
            )

        self.assertEqual(repository.get(incident["incident_id"])["status"], "FAILED")

    def test_wrong_lifecycle_state_is_rejected_without_mutation(self) -> None:
        _, evidence = load_fixture(FIXTURE_DIR / "oomkilled.json")
        incident = copy.deepcopy(incident_for(evidence))
        incident["status"] = "RECEIVED"
        repository = InMemoryIncidentRepository()
        repository.create_or_get_by_deduplication_key(
            incident, occurred_at=GENERATED_AT
        )
        repository.store_evidence(incident["incident_id"], evidence)

        with self.assertRaisesRegex(InvalidTransition, "requires LOCALIZING"):
            IncidentFastPathService(repository).run(
                incident["incident_id"], generated_at=GENERATED_AT
            )

        self.assertEqual(repository.get(incident["incident_id"])["status"], "RECEIVED")

    def test_report_persistence_failure_marks_incident_failed(self) -> None:
        class FailingReportRepository(InMemoryIncidentRepository):
            def store_report(self, report, markdown):
                raise RuntimeError("report storage unavailable")

        _, evidence = load_fixture(FIXTURE_DIR / "image-pull.json")
        incident = incident_for(evidence)
        repository = FailingReportRepository()
        repository.create_or_get_by_deduplication_key(
            incident, occurred_at=GENERATED_AT
        )
        repository.store_evidence(incident["incident_id"], evidence)

        with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
            IncidentFastPathService(repository).run(
                incident["incident_id"], generated_at=GENERATED_AT
            )

        self.assertEqual(repository.get(incident["incident_id"])["status"], "FAILED")

    def test_alert_collection_and_fast_path_form_one_persisted_flow(self) -> None:
        with (FIXTURE_DIR / "image-pull.json").open(encoding="utf-8") as handle:
            fixture = json.load(handle)

        class FixtureProvider:
            def collect(self, request):
                return ProviderBatch(
                    items=tuple(
                        EvidenceDraft(**draft)
                        for draft in fixture["evidence_drafts"]
                    )
                )

        repository = InMemoryIncidentRepository()
        observed_at = datetime(2026, 8, 12, 2, 5, tzinfo=timezone.utc)
        incident = AlertmanagerIngestionService(repository).ingest(
            {
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {
                            "alertname": "KubeImagePullBackOff",
                            "namespace": "online-boutique",
                            "pod": "paymentservice-def",
                            "severity": "critical",
                        },
                        "annotations": {},
                        "startsAt": "2026-08-12T02:00:00Z",
                        "endsAt": "0001-01-01T00:00:00Z",
                        "fingerprint": "end-to-end-image-pull-01",
                    }
                ]
            },
            received_at=observed_at,
        )[0].incident
        collection = IncidentCollectionService(
            repository,
            CollectorOrchestrator(
                [CollectorSpec("kubernetes", FixtureProvider())]
            ),
        ).collect_incident(
            incident["incident_id"],
            scope=ResourceScope(
                namespace="online-boutique",
                resource_names=("paymentservice-def",),
            ),
            observed_at=observed_at,
        )
        run = IncidentFastPathService(repository).run(
            incident["incident_id"],
            generated_at=datetime(2026, 8, 12, 2, 5, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(collection.status, "SUCCEEDED")
        self.assertEqual(run.decision.root_cause_id, "kubernetes.image-pull-failure")
        self.assertEqual(run.incident["status"], "REPORTED")
        self.assertEqual(len(repository.list_evidence(incident["incident_id"])), 2)
        self.assertEqual(
            repository.get_report(run.artifacts.report["report_id"])["status"],
            "conclusive",
        )


if __name__ == "__main__":
    unittest.main()
