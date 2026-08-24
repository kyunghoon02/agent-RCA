from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from incident_platform.collectors import (
    CollectorOrchestrator,
    CollectorSpec,
    IncidentCollectionService,
)
from incident_platform.evidence import EvidenceDraft, ProviderBatch
from incident_platform.incident_work import (
    InMemoryIncidentLocalizationWorkRepository,
    InMemoryIncidentWorkRepository,
)
from incident_platform.localization import IncidentLocalizationService
from incident_platform.projectors import KubernetesEvidenceProjector
from incident_platform.incidents import AlertmanagerIngestionService
from incident_platform.repository import InMemoryIncidentRepository
from incident_platform.resolution import (
    ResolvedIncidentLocalizationService,
    ServiceToEntityResolver,
)
from incident_platform.stategraph import InMemoryStateGraphRepository
from tools.run_incident_worker import (
    IncidentWorker,
    IncidentWorkerRuntimeConfig,
    _prometheus_query_specs,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)


def config() -> IncidentWorkerRuntimeConfig:
    return IncidentWorkerRuntimeConfig(
        worker_id="worker-runtime-test",
        cluster_id="agent-rca-dev",
        target_namespace="online-boutique",
        poll_interval_seconds=2,
        lease_seconds=120,
        max_attempts=3,
        provider_timeout_seconds=20,
        max_evidence_items=32,
        kubernetes_api_server="https://kubernetes.default.svc",
        kubernetes_token_file="/not-used/token",
        kubernetes_ca_file="/not-used/ca",
        prometheus_base_url="http://prometheus.observability.svc.cluster.local:9090",
        neo4j_uri="bolt://neo4j:7687",
        neo4j_username="neo4j",
        neo4j_password="test-only",
        neo4j_database="neo4j",
        localization_max_candidates=10,
        localization_max_entities=40,
        localization_max_depth=4,
        postgres_host="postgresql",
        postgres_port=5432,
        postgres_database="agent_rca",
        postgres_username="agent_rca",
        postgres_password="test-only",
    )


def payload(*, source_label: str = "service") -> dict:
    return {
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "IncidentWorkerRuntime",
                    "namespace": "online-boutique",
                    source_label: "frontend",
                    "severity": "warning",
                },
                "annotations": {},
                "startsAt": "2026-08-24T08:55:00Z",
                "endsAt": "2099-01-01T00:00:00Z",
                "fingerprint": f"worker-runtime-{source_label}",
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
                    summary="Worker runtime fixture Evidence.",
                    facts={"result_status": "HAS_DATA", "latest": 1.0},
                    provider="worker-runtime-fixture",
                    query="service=frontend",
                    locator="fixture://worker/frontend",
                ),
            )
        )


class KubernetesStaticProvider:
    def collect(self, request):
        return ProviderBatch(
            items=(
                EvidenceDraft(
                    source="kubernetes",
                    kind="resource-state",
                    observed_at=request.window.end,
                    subject={
                        "cluster_id": "agent-rca-dev",
                        "api_version": "v1",
                        "kind": "Service",
                        "namespace": request.scope.namespace,
                        "name": "frontend",
                        "uid": "frontend-service-runtime-test",
                        "exists": True,
                    },
                    summary="Frontend Kubernetes Service was collected.",
                    facts={"result_status": "FOUND", "service_type": "ClusterIP"},
                    provider="worker-runtime-kubernetes-fixture",
                    query="get v1/Service frontend",
                    locator="fixture://kubernetes/Service/frontend",
                ),
            )
        )


class IncidentWorkerRuntimeTests(unittest.TestCase):
    def test_environment_preserves_postgresql_secret_bytes(self) -> None:
        environment = {
            "HOSTNAME": "incident-worker-abc",
            "INCIDENT_WORKER_CLUSTER_ID": "agent-rca-dev",
            "INCIDENT_WORKER_TARGET_NAMESPACE": "online-boutique",
            "PROMETHEUS_BASE_URL": "http://prometheus:9090",
            "NEO4J_URI": "bolt://neo4j:7687",
            "NEO4J_USERNAME": "neo4j",
            "NEO4J_PASSWORD": "neo4j-password-with-newline\n",
            "POSTGRES_HOST": "postgresql",
            "POSTGRES_DATABASE": "agent_rca",
            "POSTGRES_USERNAME": "agent_rca",
            "POSTGRES_PASSWORD": "password-with-newline\n",
        }
        with patch.dict("os.environ", environment, clear=True):
            runtime = IncidentWorkerRuntimeConfig.from_environment()
        self.assertEqual(runtime.postgres_password, "password-with-newline\n")
        self.assertEqual(
            runtime.neo4j_password, "neo4j-password-with-newline\n"
        )

    def test_prometheus_queries_are_fixed_and_service_scoped(self) -> None:
        specs = _prometheus_query_specs()
        self.assertEqual(len(specs), 4)
        self.assertEqual({spec.resource_label for spec in specs}, {"service_name"})
        self.assertTrue(all(spec.expression_template.count("{scope}") == 1 for spec in specs))

    def test_worker_claims_collects_and_completes_one_service_incident(self) -> None:
        incidents = InMemoryIncidentRepository()
        work = InMemoryIncidentWorkRepository(incidents)
        incident = AlertmanagerIngestionService(incidents).ingest(
            payload(), received_at=NOW
        )[0].incident
        work.enqueue(incident["incident_id"], available_at=NOW)
        collection = IncidentCollectionService(
            incidents,
            CollectorOrchestrator(
                [CollectorSpec("prometheus", StaticProvider())]
            ),
        )
        worker = IncidentWorker(
            config(),
            incidents,
            work,
            collection,
            clock=lambda: NOW + timedelta(seconds=1),
        )

        result = worker.process_one()

        self.assertEqual(result["status"], "PROCESSED")
        self.assertEqual(result["collection_status"], "SUCCEEDED")
        self.assertEqual(incidents.get(incident["incident_id"])["status"], "LOCALIZING")
        self.assertEqual(len(incidents.list_evidence(incident["incident_id"])), 1)

    def test_worker_finishes_localization_before_claiming_more_collection(self) -> None:
        incidents = InMemoryIncidentRepository()
        collection_work = InMemoryIncidentWorkRepository(incidents)
        localization_work = InMemoryIncidentLocalizationWorkRepository(incidents)
        incident = AlertmanagerIngestionService(incidents).ingest(
            payload(), received_at=NOW
        )[0].incident
        collection_work.enqueue(incident["incident_id"], available_at=NOW)
        collection = IncidentCollectionService(
            incidents,
            CollectorOrchestrator(
                [CollectorSpec("kubernetes", KubernetesStaticProvider())]
            ),
        )
        graph = InMemoryStateGraphRepository()
        localization = ResolvedIncidentLocalizationService(
            ServiceToEntityResolver(graph),
            IncidentLocalizationService(
                incidents,
                graph,
                (KubernetesEvidenceProjector(),),
            ),
        )
        worker = IncidentWorker(
            config(),
            incidents,
            collection_work,
            collection,
            localization_work,
            localization,
            clock=lambda: NOW + timedelta(seconds=1),
        )

        collected = worker.process_one()
        evidence = incidents.list_evidence(incident["incident_id"])
        graph.ingest(KubernetesEvidenceProjector().project(evidence[0]).records)
        localization_work.enqueue(
            incident["incident_id"],
            available_at=NOW + timedelta(seconds=1),
        )
        localized = worker.process_one()

        self.assertEqual(collected["stage"], "COLLECTION")
        self.assertEqual(localized["stage"], "LOCALIZATION")
        self.assertEqual(localized["status"], "PROCESSED")
        self.assertEqual(
            incidents.get(incident["incident_id"])["status"], "ANALYZING"
        )
        self.assertEqual(localized["evidence_count"], 1)

    def test_worker_fails_closed_for_an_unsupported_source_kind(self) -> None:
        incidents = InMemoryIncidentRepository()
        work = InMemoryIncidentWorkRepository(incidents)
        incident = AlertmanagerIngestionService(incidents).ingest(
            payload(source_label="pod"), received_at=NOW
        )[0].incident
        work.enqueue(incident["incident_id"], available_at=NOW)
        collection = IncidentCollectionService(
            incidents,
            CollectorOrchestrator(
                [CollectorSpec("prometheus", StaticProvider())]
            ),
        )
        worker = IncidentWorker(
            config(),
            incidents,
            work,
            collection,
            clock=lambda: NOW + timedelta(seconds=1),
        )

        result = worker.process_one()

        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error_code"], "VALUEERROR")
        self.assertEqual(incidents.get(incident["incident_id"])["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
