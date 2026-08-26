from __future__ import annotations

import unittest
from datetime import datetime, timezone

from incident_platform.errors import PermanentProviderError
from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceWindow,
    ResourceScope,
    validate_provider_batch,
)
from incident_platform.projectors import KubernetesEvidenceProjector
from incident_platform.providers.kubernetes import (
    KubernetesInventoryProvider,
    KubernetesResourcePage,
)
from incident_platform.resolution import EntityResolutionRequest, ServiceToEntityResolver
from incident_platform.stategraph import InMemoryStateGraphRepository


UTC = timezone.utc
WINDOW = EvidenceWindow(
    start="2026-08-24T01:00:00Z",
    end="2026-08-24T01:05:00Z",
)


def resource(
    api_version: str,
    kind: str,
    name: str,
    uid: str,
    *,
    namespace: str | None = "online-boutique",
    labels: dict | None = None,
    owners: list | None = None,
    spec: dict | None = None,
    status: dict | None = None,
    **extra,
) -> dict:
    metadata = {
        "name": name,
        "uid": uid,
        "resourceVersion": "42",
        "labels": labels or {},
        "ownerReferences": owners or [],
    }
    if namespace is not None:
        metadata["namespace"] = namespace
    return {
        "apiVersion": api_version,
        "kind": kind,
        "metadata": metadata,
        "spec": spec or {},
        "status": status or {},
        **extra,
    }


def inventory_resources() -> dict[str, tuple[dict, ...]]:
    deployment = resource(
        "apps/v1",
        "Deployment",
        "checkoutservice",
        "uid-deployment-checkout",
        spec={"replicas": 1},
        status={"replicas": 1, "readyReplicas": 1},
    )
    replica_set = resource(
        "apps/v1",
        "ReplicaSet",
        "checkoutservice-7d9f8",
        "uid-rs-checkout",
        owners=[
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": "checkoutservice",
                "uid": "uid-deployment-checkout",
            }
        ],
        spec={"replicas": 1},
        status={"replicas": 1, "readyReplicas": 1},
    )
    pod = resource(
        "v1",
        "Pod",
        "checkoutservice-7d9f8-q1w2e",
        "uid-pod-checkout",
        labels={"app": "checkoutservice"},
        owners=[
            {
                "apiVersion": "apps/v1",
                "kind": "ReplicaSet",
                "name": "checkoutservice-7d9f8",
                "uid": "uid-rs-checkout",
            }
        ],
        spec={
            "nodeName": "node-01",
            "containers": [
                {"name": "server", "env": [{"name": "PASSWORD", "value": "omit-me"}]}
            ],
        },
        status={
            "phase": "Running",
            "containerStatuses": [
                {
                    "name": "server",
                    "ready": True,
                    "restartCount": 1,
                    "lastState": {
                        "terminated": {"reason": "OOMKilled", "exitCode": 137}
                    },
                }
            ],
        },
    )
    service = resource(
        "v1",
        "Service",
        "checkoutservice",
        "uid-service-checkout",
        spec={"type": "ClusterIP", "selector": {"app": "checkoutservice"}},
    )
    endpoint_slice = resource(
        "discovery.k8s.io/v1",
        "EndpointSlice",
        "checkoutservice-j4k5l",
        "uid-slice-checkout",
        labels={"kubernetes.io/service-name": "checkoutservice"},
        endpoints=[
            {
                "conditions": {"ready": True},
                "addresses": ["10.244.0.20"],
                "targetRef": {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "name": "checkoutservice-7d9f8-q1w2e",
                    "uid": "uid-pod-checkout",
                },
            }
        ],
        addressType="IPv4",
    )
    node = resource(
        "v1",
        "Node",
        "node-01",
        "uid-node-01",
        namespace=None,
    )
    unrelated = resource(
        "v1",
        "Service",
        "unrelated",
        "uid-service-unrelated",
        spec={"type": "ClusterIP", "selector": {"app": "unrelated"}},
    )
    return {
        "Service": (service, unrelated),
        "Deployment": (deployment,),
        "ReplicaSet": (replica_set,),
        "Pod": (pod,),
        "EndpointSlice": (endpoint_slice,),
        "Node": (node,),
    }


class StaticInventoryClient:
    def __init__(self, resources: dict[str, tuple[dict, ...]]) -> None:
        self.resources = resources
        self.calls = []

    def list_resource_page(self, resource_spec, **kwargs):
        self.calls.append((resource_spec, kwargs))
        return KubernetesResourcePage(self.resources[resource_spec.kind])


def request() -> CollectionRequest:
    return CollectionRequest(
        request_id="req-live-stategraph-inventory",
        incident_id="inc-live-stategraph-inventory",
        window=WINDOW,
        scope=ResourceScope(
            namespace="online-boutique",
            resource_names=("checkoutservice",),
            resource_name_prefixes=("checkoutservice-",),
            max_items=20,
        ),
        timeout_seconds=5,
    )


class KubernetesInventoryProviderTests(unittest.TestCase):
    def test_inventory_is_rooted_bounded_and_contains_only_safe_facts(self) -> None:
        provider = KubernetesInventoryProvider(
            StaticInventoryClient(inventory_resources()),
            cluster_id="agent-rca-dev",
        )

        batch = provider.collect(request())
        validate_provider_batch(batch, request())

        self.assertEqual(batch.status, "SUCCEEDED")
        self.assertEqual(len(batch.items), 5)
        self.assertNotIn("unrelated", {item.subject["name"] for item in batch.items})
        self.assertNotIn("omit-me", str(batch.items))
        relation_types = {
            relation["relation_type"]
            for item in batch.items
            for relation in item.facts["relationships"]
        }
        self.assertEqual(
            relation_types,
            {"OWNS", "ROUTES_TO", "SCHEDULED_ON", "SELECTS"},
        )
        endpoint = next(item for item in batch.items if item.subject["kind"] == "EndpointSlice")
        self.assertEqual(endpoint.facts["endpoint_count"], 1)
        self.assertNotIn("10.244.0.20", str(endpoint.facts))
        pod = next(item for item in batch.items if item.subject["kind"] == "Pod")
        self.assertEqual(pod.facts["last_termination_reason"], "OOMKilled")
        self.assertEqual(pod.facts["last_exit_code"], 137)

    def test_inventory_projects_idempotently_and_resolves_logical_service(self) -> None:
        provider = KubernetesInventoryProvider(
            StaticInventoryClient(inventory_resources()),
            cluster_id="agent-rca-dev",
        )
        collection_request = request()
        batch = provider.collect(collection_request)
        evidence = tuple(
            EvidenceBuilder().build(
                draft,
                collection_request,
                collected_at=datetime(2026, 8, 24, 1, 5, tzinfo=UTC),
            )
            for draft in batch.items
        )
        projector = KubernetesEvidenceProjector()
        records = tuple(
            record
            for item in evidence
            for record in projector.project(item).records
        )
        repository = InMemoryStateGraphRepository()

        repository.ingest(records)
        first_relation_count = len(repository.list_relations())
        repository.ingest(records)

        self.assertEqual(len(repository.list_relations()), first_relation_count)
        self.assertGreaterEqual(first_relation_count, 7)
        result = ServiceToEntityResolver(repository).resolve(
            EntityResolutionRequest(
                incident_id=collection_request.incident_id,
                cluster_id="agent-rca-dev",
                namespace="online-boutique",
                service_name="checkoutservice",
                window=WINDOW,
            )
        )
        self.assertEqual(result.status, "RESOLVED")
        self.assertEqual(result.method, "logical-service-exact")

    def test_inventory_rejects_a_prefix_not_derived_from_an_exact_root(self) -> None:
        provider = KubernetesInventoryProvider(
            StaticInventoryClient(inventory_resources()),
            cluster_id="agent-rca-dev",
        )
        collection_request = CollectionRequest(
            request_id="req-wide-prefix",
            incident_id="inc-wide-prefix",
            window=WINDOW,
            scope=ResourceScope(
                namespace="online-boutique",
                resource_names=("checkoutservice",),
                resource_name_prefixes=("checkout",),
                max_items=20,
            ),
            timeout_seconds=5,
        )

        with self.assertRaisesRegex(
            PermanentProviderError, "derived from exact roots"
        ):
            provider.collect(collection_request)


class ResourceScopePrefixTests(unittest.TestCase):
    def test_dynamic_names_require_an_explicit_nonempty_prefix(self) -> None:
        scope = request().scope

        self.assertTrue(scope.contains_resource_name("checkoutservice"))
        self.assertTrue(scope.contains_resource_name("checkoutservice-7d9f8"))
        self.assertFalse(scope.contains_resource_name("checkoutserviceevil"))
        with self.assertRaisesRegex(Exception, "prefixes must not be empty"):
            ResourceScope(
                namespace="online-boutique",
                resource_names=("checkoutservice",),
                resource_name_prefixes=("",),
            )


if __name__ == "__main__":
    unittest.main()
