from __future__ import annotations

import unittest
from datetime import datetime, timezone

from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceDraft,
    ResourceScope,
)
from incident_platform.localization import IncidentLocalizationService
from incident_platform.projectors import KubernetesEvidenceProjector
from incident_platform.repository import InMemoryIncidentRepository
from incident_platform.resolution import (
    EntityResolutionRequest,
    ResolvedIncidentLocalizationService,
    ServiceToEntityResolver,
)
from incident_platform.stategraph import EntityIdentity, InMemoryStateGraphRepository

from tests.test_incident_localization_service import FROZEN_AT, incident_for
from tests.test_stategraph import WINDOW, build_evidence, kubernetes_evidence


UTC = timezone.utc
CLUSTER_ID = "gcp-dev-01"


def service_evidence(
    *,
    uid: str = "service-checkout-uid-0001",
    observed_at: str = "2026-08-12T01:05:00Z",
) -> dict:
    request = CollectionRequest(
        request_id="req-service-resolution-0001",
        incident_id="inc-stategraph-fixture-0001",
        window=WINDOW,
        scope=ResourceScope(
            namespace="online-boutique",
            resource_names=("checkoutservice",),
            max_items=10,
        ),
        timeout_seconds=5,
    )
    return EvidenceBuilder().build(
        EvidenceDraft(
            source="kubernetes",
            kind="resource-state",
            observed_at=observed_at,
            subject={
                "cluster_id": CLUSTER_ID,
                "api_version": "v1",
                "kind": "Service",
                "namespace": "online-boutique",
                "name": "checkoutservice",
                "uid": uid,
                "exists": True,
            },
            summary="Kubernetes Service state was read.",
            facts={"result_status": "FOUND", "service_type": "ClusterIP"},
            provider="kubernetes-http-api",
            query="get v1/Service checkoutservice",
            locator="k8s://online-boutique/Service/checkoutservice",
        ),
        request,
        collected_at=datetime(2026, 8, 12, 1, 6, tzinfo=UTC),
    )


def kubernetes_service_entity(uid: str, *, observed_at: str) -> dict:
    identity = EntityIdentity.kubernetes_resource(cluster_id=CLUSTER_ID, uid=uid)
    return {
        "record_type": "entity",
        "entity_id": identity.entity_id,
        "identity": identity.to_contract(),
        "entity_type": "Service",
        "domain": "kubernetes",
        "name": "checkoutservice",
        "scope": {
            "cluster_id": CLUSTER_ID,
            "namespace": "online-boutique",
            "api_version": "v1",
        },
        "external_ref": uid,
        "exists": True,
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "evidence_ids": [f"ev-resolution-{uid}"],
    }


def resolution_request(*, window=WINDOW) -> EntityResolutionRequest:
    return EntityResolutionRequest(
        incident_id="inc-stategraph-fixture-0001",
        cluster_id=CLUSTER_ID,
        namespace="online-boutique",
        service_name="checkoutservice",
        window=window,
    )


class EntityIdentityAndReconciliationTests(unittest.TestCase):
    def test_cluster_id_is_part_of_kubernetes_resource_identity(self) -> None:
        first = EntityIdentity.kubernetes_resource(cluster_id="cluster-a", uid="uid-1")
        repeated = EntityIdentity.kubernetes_resource(cluster_id="cluster-a", uid="uid-1")
        another_cluster = EntityIdentity.kubernetes_resource(
            cluster_id="cluster-b", uid="uid-1"
        )

        self.assertEqual(first.entity_id, repeated.entity_id)
        self.assertNotEqual(first.entity_id, another_cluster.entity_id)

    def test_logical_service_and_kubernetes_service_are_distinct_and_related(self) -> None:
        evidence = service_evidence()
        projection = KubernetesEvidenceProjector().project(evidence)
        entities = [
            item for item in projection.records if item["record_type"] == "entity"
        ]

        self.assertEqual({item["domain"] for item in entities}, {"web-service", "kubernetes"})
        self.assertEqual(len({item["entity_id"] for item in entities}), 2)
        relation = next(
            item
            for item in projection.records
            if item["record_type"] == "relation_interval"
        )
        self.assertEqual(relation["relation_type"], "REPRESENTED_BY")

    def test_placeholder_history_is_preserved_and_linked_to_resolved_uid(self) -> None:
        event, missing = kubernetes_evidence()
        projector = KubernetesEvidenceProjector()
        repository = InMemoryStateGraphRepository()
        repository.ingest(projector.project(event).records)
        repository.ingest(projector.project(missing).records)
        placeholder = next(
            item
            for item in projector.project(missing).records
            if item["record_type"] == "entity"
        )
        resolved = build_evidence(
            EvidenceDraft(
                source="kubernetes",
                kind="resource-state",
                observed_at="2026-08-12T01:06:00Z",
                subject={
                    "cluster_id": CLUSTER_ID,
                    "api_version": "v1",
                    "kind": "ConfigMap",
                    "namespace": "online-boutique",
                    "name": "checkout-settings",
                    "uid": "configmap-checkout-uid-0001",
                    "exists": True,
                },
                summary="ConfigMap now exists.",
                facts={"result_status": "FOUND", "required": True},
                provider="kubernetes-http-api",
                query="get ConfigMap checkout-settings",
                locator="k8s://online-boutique/ConfigMap/checkout-settings",
            )
        )
        resolved_projection = projector.project(resolved)
        repository.ingest(resolved_projection.records)
        resolved_entity = next(
            item
            for item in resolved_projection.records
            if item["record_type"] == "entity"
        )

        relation = next(
            item
            for item in repository.list_relations()
            if item["relation_type"] == "RESOLVES_TO"
        )
        self.assertNotEqual(placeholder["entity_id"], resolved_entity["entity_id"])
        self.assertEqual(relation["source_entity_id"], placeholder["entity_id"])
        self.assertEqual(relation["destination_entity_id"], resolved_entity["entity_id"])


class ServiceToEntityResolverTests(unittest.TestCase):
    def test_exact_logical_service_resolves_to_one_seed(self) -> None:
        repository = InMemoryStateGraphRepository()
        projection = KubernetesEvidenceProjector().project(service_evidence())
        repository.ingest(projection.records)

        result = ServiceToEntityResolver(repository).resolve(resolution_request())

        self.assertEqual(result.status, "RESOLVED")
        self.assertEqual(result.method, "logical-service-exact")
        self.assertEqual(result.candidates[0].domain, "web-service")
        self.assertEqual(result.seed_entity_ids, (result.candidates[0].entity_id,))
        self.assertEqual(result.to_contract()["status"], "RESOLVED")

    def test_multiple_exact_kubernetes_recreations_are_ambiguous(self) -> None:
        repository = InMemoryStateGraphRepository()
        repository.ingest(
            (
                kubernetes_service_entity("service-uid-a", observed_at="2026-08-12T01:04:00Z"),
                kubernetes_service_entity("service-uid-b", observed_at="2026-08-12T01:06:00Z"),
            )
        )

        result = ServiceToEntityResolver(repository).resolve(resolution_request())

        self.assertEqual(result.status, "AMBIGUOUS")
        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.seed_entity_ids, ())

    def test_absent_or_out_of_window_service_is_not_found(self) -> None:
        repository = InMemoryStateGraphRepository()
        projector = KubernetesEvidenceProjector()
        logical = next(
            item
            for item in projector.project(
                service_evidence(observed_at="2026-08-12T01:05:00Z")
            ).records
            if item["record_type"] == "entity" and item["domain"] == "web-service"
        )
        repository.ingest((logical,))
        outside = type(WINDOW)(
            start="2026-08-12T02:00:00Z",
            end="2026-08-12T02:10:00Z",
        )

        result = ServiceToEntityResolver(repository).resolve(
            resolution_request(window=outside)
        )

        self.assertEqual(result.status, "NOT_FOUND")
        self.assertEqual(result.seed_entity_ids, ())


class ResolvedIncidentLocalizationServiceTests(unittest.TestCase):
    def test_resolver_builds_scope_before_incident_localization(self) -> None:
        evidence = service_evidence()
        projector = KubernetesEvidenceProjector()
        incident_repository = InMemoryIncidentRepository()
        graph_repository = InMemoryStateGraphRepository()
        incident = incident_for((evidence,))
        incident_repository.create_or_get_by_deduplication_key(
            incident,
            occurred_at=FROZEN_AT,
        )
        incident_repository.store_evidence(incident["incident_id"], (evidence,))
        graph_repository.ingest(projector.project(evidence).records)
        facade = ResolvedIncidentLocalizationService(
            ServiceToEntityResolver(graph_repository),
            IncidentLocalizationService(
                incident_repository,
                graph_repository,
                (projector,),
            ),
        )

        run = facade.localize_service(
            resolution_request(),
            frozen_at=FROZEN_AT,
            max_entities=4,
            max_depth=1,
        )

        self.assertEqual(run.resolution.status, "RESOLVED")
        self.assertIsNotNone(run.localization)
        assert run.localization is not None
        self.assertEqual(run.localization.incident["status"], "ANALYZING")
        self.assertEqual(run.localization.context["source_entity"]["domain"], "web-service")
        self.assertTrue(
            any(
                path["relations"] == ["REPRESENTED_BY"]
                for path in run.localization.context["state_paths"]
            )
        )

    def test_unresolved_result_does_not_advance_incident(self) -> None:
        evidence = service_evidence()
        incident_repository = InMemoryIncidentRepository()
        graph_repository = InMemoryStateGraphRepository()
        incident = incident_for((evidence,))
        incident_repository.create_or_get_by_deduplication_key(
            incident,
            occurred_at=FROZEN_AT,
        )
        incident_repository.store_evidence(incident["incident_id"], (evidence,))
        facade = ResolvedIncidentLocalizationService(
            ServiceToEntityResolver(graph_repository),
            IncidentLocalizationService(
                incident_repository,
                graph_repository,
                (KubernetesEvidenceProjector(),),
            ),
        )

        run = facade.localize_service(resolution_request(), frozen_at=FROZEN_AT)

        self.assertEqual(run.resolution.status, "NOT_FOUND")
        self.assertIsNone(run.localization)
        self.assertEqual(
            incident_repository.get(incident["incident_id"])["status"],
            "LOCALIZING",
        )


if __name__ == "__main__":
    unittest.main()
