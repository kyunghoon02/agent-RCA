from __future__ import annotations

import unittest
from datetime import datetime, timezone

from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceDraft,
    ResourceScope,
)
from incident_platform.errors import ContractViolation
from incident_platform.localization import IncidentLocalizationService
from incident_platform.projectors import (
    KubernetesEvidenceProjector,
    PrometheusMetricEvidenceProjector,
)
from incident_platform.repository import InMemoryIncidentRepository
from incident_platform.resolution import (
    EntityResolutionRequest,
    ResolvedIncidentLocalizationService,
    ServiceToEntityResolver,
)
from incident_platform.stategraph import InMemoryStateGraphRepository

from tests.test_entity_resolution import CLUSTER_ID, service_evidence
from tests.test_incident_localization_service import FROZEN_AT, incident_for
from tests.test_stategraph import WINDOW


UTC = timezone.utc
METRICS = (
    "api.request-rate",
    "api.failure-rate",
    "api.latency-p95-milliseconds",
    "api.latency-baseline-p95-milliseconds",
)


def metric_evidence(metric: str, *, cluster_id: str | None = CLUSTER_ID) -> dict:
    request = CollectionRequest(
        request_id=f"req-prometheus-projector-{metric}",
        incident_id="inc-stategraph-fixture-0001",
        window=WINDOW,
        scope=ResourceScope(
            namespace="online-boutique",
            resource_names=("checkoutservice",),
            max_items=10,
        ),
        timeout_seconds=5,
    )
    subject = {
        "api_version": "v1",
        "kind": "Service",
        "namespace": "online-boutique",
        "name": "checkoutservice",
        "uid": None,
        "exists": True,
    }
    if cluster_id is not None:
        subject["cluster_id"] = cluster_id
    return EvidenceBuilder().build(
        EvidenceDraft(
            source="prometheus",
            kind="metric-summary",
            observed_at=WINDOW.end,
            subject=subject,
            summary=f"Prometheus {metric} returned one scoped sample.",
            facts={
                "metric": metric,
                "result_status": "HAS_DATA",
                "sample_count": 1,
                "minimum": 0.25,
                "maximum": 0.25,
                "average": 0.25,
                "latest": 0.25,
                "peak_test_value": 0.25,
            },
            provider="prometheus-http-api",
            query=f"{metric} scoped-query",
            locator=f"prometheus://query/{metric}",
        ),
        request,
        collected_at=datetime(2026, 8, 12, 1, 6, tzinfo=UTC),
    )


class PrometheusMetricEvidenceProjectorTests(unittest.TestCase):
    def test_projects_a_metric_to_a_logical_service_event(self) -> None:
        evidence = metric_evidence(METRICS[0])

        projection = PrometheusMetricEvidenceProjector().project(evidence)

        self.assertEqual(len(projection.records), 2)
        entity, event = projection.records
        self.assertEqual(entity["domain"], "web-service")
        self.assertEqual(entity["identity"]["identity_type"], "logical-service")
        self.assertEqual(event["event_type"], "PROMETHEUS_METRIC_SUMMARY")
        self.assertEqual(event["entity_id"], entity["entity_id"])
        self.assertEqual(event["evidence_ids"], [evidence["evidence_id"]])
        self.assertEqual(event["attributes"]["peak_test_value"], 0.25)
        self.assertNotIn("query", event["attributes"])

    def test_metric_without_trusted_cluster_identity_is_not_projectable(self) -> None:
        evidence = metric_evidence(METRICS[0], cluster_id=None)
        projector = PrometheusMetricEvidenceProjector()

        self.assertFalse(projector.supports(evidence))
        with self.assertRaisesRegex(ContractViolation, "trusted cluster-scoped"):
            projector.project(evidence)

    def test_localized_context_retains_kubernetes_and_all_metric_evidence(self) -> None:
        kubernetes = service_evidence()
        metrics = tuple(metric_evidence(metric) for metric in METRICS)
        incident = incident_for((kubernetes,))
        incidents = InMemoryIncidentRepository()
        incidents.create_or_get_by_deduplication_key(
            incident,
            occurred_at=FROZEN_AT,
        )
        incidents.store_evidence(
            incident["incident_id"],
            (kubernetes, *metrics),
        )
        graph = InMemoryStateGraphRepository()
        graph.ingest(KubernetesEvidenceProjector().project(kubernetes).records)
        service = ResolvedIncidentLocalizationService(
            ServiceToEntityResolver(graph),
            IncidentLocalizationService(
                incidents,
                graph,
                (
                    KubernetesEvidenceProjector(),
                    PrometheusMetricEvidenceProjector(),
                ),
            ),
        )

        run = service.localize_service(
            EntityResolutionRequest(
                incident_id=incident["incident_id"],
                cluster_id=CLUSTER_ID,
                namespace="online-boutique",
                service_name="checkoutservice",
                window=WINDOW,
            ),
            frozen_at=FROZEN_AT,
            max_entities=10,
            max_depth=2,
        )

        self.assertIsNotNone(run.localization)
        context = run.localization.context
        self.assertEqual(
            set(context["evidence_ids"]),
            {kubernetes["evidence_id"], *(item["evidence_id"] for item in metrics)},
        )
        self.assertTrue(
            set(item["evidence_id"] for item in metrics).isdisjoint(
                context["recent_change_evidence_ids"]
            )
        )
        self.assertEqual(run.localization.incident["status"], "ANALYZING")


if __name__ == "__main__":
    unittest.main()
