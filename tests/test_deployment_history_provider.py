from __future__ import annotations

import unittest
from datetime import datetime, timezone

from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceWindow,
    ResourceScope,
    validate_provider_batch,
)
from incident_platform.projectors import DeploymentChangeEvidenceProjector
from incident_platform.providers.change import DeploymentHistoryProvider
from incident_platform.providers.kubernetes import KubernetesResourcePage
from incident_platform.stategraph import (
    InMemoryStateGraphRepository,
    InvestigationScope,
)


UTC = timezone.utc
WINDOW = EvidenceWindow(
    start="2026-08-24T01:00:00Z",
    end="2026-08-24T01:05:00Z",
)


def request() -> CollectionRequest:
    return CollectionRequest(
        request_id="req-deployment-history-0001",
        incident_id="inc-deployment-history-0001",
        window=WINDOW,
        scope=ResourceScope(
            namespace="online-boutique",
            resource_names=("checkoutservice",),
            max_items=10,
        ),
        timeout_seconds=5,
    )


def deployment() -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "checkoutservice",
            "namespace": "online-boutique",
            "uid": "uid-deployment-checkout",
            "annotations": {"deployment.kubernetes.io/revision": "2"},
        },
        "spec": {},
        "status": {},
    }


def replica_set(revision: int, created_at: str, image: str, memory: str) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {
            "name": f"checkoutservice-r{revision}",
            "namespace": "online-boutique",
            "uid": f"uid-rs-{revision}",
            "creationTimestamp": created_at,
            "annotations": {"deployment.kubernetes.io/revision": str(revision)},
            "ownerReferences": [
                {
                    "kind": "Deployment",
                    "name": "checkoutservice",
                    "uid": "uid-deployment-checkout",
                }
            ],
        },
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "server",
                            "image": image,
                            "env": [{"name": "TOKEN", "value": "must-not-leak"}],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": memory},
                                "limits": {"memory": memory},
                            },
                        }
                    ]
                }
            }
        },
    }


class StaticDeploymentClient:
    def __init__(self, replica_sets: tuple[dict, ...]) -> None:
        self.replica_sets = replica_sets

    def get_resource(self, resource, **kwargs):
        return deployment()

    def list_resource_page(self, resource, **kwargs):
        return KubernetesResourcePage(self.replica_sets)


def normalized_evidence(batch, collection_request):
    return tuple(
        EvidenceBuilder().build(
            item,
            collection_request,
            collected_at=datetime(2026, 8, 24, 1, 5, tzinfo=UTC),
        )
        for item in batch.items
    )


class DeploymentHistoryProviderTests(unittest.TestCase):
    def test_detects_bounded_revision_diff_without_copying_sensitive_config(self) -> None:
        provider = DeploymentHistoryProvider(
            StaticDeploymentClient(
                (
                    replica_set(
                        1,
                        "2026-08-24T00:55:00Z",
                        "private.registry/team/checkout:v1",
                        "64Mi",
                    ),
                    replica_set(
                        2,
                        "2026-08-24T01:02:00Z",
                        "private.registry/team/checkout:v2",
                        "32Mi",
                    ),
                )
            ),
            cluster_id="agent-rca-dev",
        )

        batch = provider.collect(request())
        validate_provider_batch(batch, request())

        self.assertEqual(batch.status, "SUCCEEDED")
        self.assertEqual(len(batch.items), 1)
        change = batch.items[0]
        self.assertEqual(change.facts["result_status"], "CHANGE_DETECTED")
        self.assertEqual(change.facts["previous_revision"], 1)
        self.assertIn("containers.server.image", change.facts["changed_fields"])
        self.assertIn("containers.server.resources", change.facts["changed_fields"])
        self.assertNotIn("private.registry", str(change.facts))
        self.assertNotIn("must-not-leak", str(change.facts))

    def test_no_change_is_explicit_but_not_recent_change_evidence(self) -> None:
        provider = DeploymentHistoryProvider(
            StaticDeploymentClient(
                (
                    replica_set(
                        1,
                        "2026-08-23T23:55:00Z",
                        "checkout:v1",
                        "64Mi",
                    ),
                    replica_set(
                        2,
                        "2026-08-24T00:55:00Z",
                        "checkout:v2",
                        "64Mi",
                    ),
                )
            ),
            cluster_id="agent-rca-dev",
        )
        collection_request = request()
        batch = provider.collect(collection_request)
        evidence = normalized_evidence(batch, collection_request)[0]
        projection = DeploymentChangeEvidenceProjector().project(evidence)

        self.assertEqual(evidence["facts"]["result_status"], "NO_CHANGES")
        entity, event = projection.records
        self.assertEqual(event["event_type"], "DEPLOYMENT_CHANGE_ABSENCE")
        repository = InMemoryStateGraphRepository()
        repository.ingest(projection.records)
        localized = repository.find_state_paths(
            InvestigationScope(
                incident_id=collection_request.incident_id,
                seed_entity_ids=(entity["entity_id"],),
                window=WINDOW,
                domains=("kubernetes",),
                max_entities=5,
                max_depth=0,
            )
        )
        self.assertIn(evidence["evidence_id"], localized.evidence_ids)
        self.assertNotIn(
            evidence["evidence_id"],
            localized.recent_change_evidence_ids,
        )

    def test_detected_change_is_recent_change_evidence(self) -> None:
        provider = DeploymentHistoryProvider(
            StaticDeploymentClient(
                (
                    replica_set(1, "2026-08-24T00:55:00Z", "checkout:v1", "64Mi"),
                    replica_set(2, "2026-08-24T01:02:00Z", "checkout:v2", "32Mi"),
                )
            ),
            cluster_id="agent-rca-dev",
        )
        collection_request = request()
        evidence = normalized_evidence(
            provider.collect(collection_request), collection_request
        )[0]
        projection = DeploymentChangeEvidenceProjector().project(evidence)
        repository = InMemoryStateGraphRepository()
        repository.ingest(projection.records)
        localized = repository.find_state_paths(
            InvestigationScope(
                incident_id=collection_request.incident_id,
                seed_entity_ids=(projection.records[0]["entity_id"],),
                window=WINDOW,
                domains=("kubernetes",),
                max_entities=5,
                max_depth=0,
            )
        )

        self.assertIn(
            evidence["evidence_id"],
            localized.recent_change_evidence_ids,
        )


if __name__ == "__main__":
    unittest.main()
