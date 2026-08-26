from __future__ import annotations

import unittest

from incident_platform.evidence import (
    CollectionRequest,
    EvidenceDraft,
    EvidenceWindow,
    ProviderBatch,
    ResourceScope,
    validate_provider_batch,
)
from incident_platform.providers.kubernetes import KubernetesIncidentProvider


WINDOW = EvidenceWindow(
    start="2026-08-24T01:00:00Z",
    end="2026-08-24T01:05:00Z",
)


def request() -> CollectionRequest:
    return CollectionRequest(
        request_id="req-kubernetes-incident-0001",
        incident_id="inc-kubernetes-incident-0001",
        window=WINDOW,
        scope=ResourceScope(
            namespace="online-boutique",
            resource_names=("checkoutservice",),
            resource_name_prefixes=("checkoutservice-",),
            max_items=10,
        ),
        timeout_seconds=5,
    )


def draft(kind: str, resource_kind: str, name: str) -> EvidenceDraft:
    return EvidenceDraft(
        source="kubernetes",
        kind=kind,
        observed_at=WINDOW.end,
        subject={
            "cluster_id": "agent-rca-dev",
            "api_version": "v1",
            "kind": resource_kind,
            "namespace": "online-boutique",
            "name": name,
            "uid": f"uid-{name}",
            "exists": True,
        },
        summary=f"Fixture {kind} for {name}.",
        facts={"result_status": "FOUND"},
        provider="fixture-kubernetes",
        query="fixture query",
        locator=f"fixture://{resource_kind}/{name}",
    )


class StaticInventory:
    def collect(self, collection_request):
        return ProviderBatch(
            items=(
                draft("resource-state", "Service", "checkoutservice"),
                draft(
                    "resource-state",
                    "Pod",
                    "checkoutservice-7d9f8-q1w2e",
                ),
            )
        )


class RecordingEventProvider:
    def __init__(self) -> None:
        self.scopes = []

    def collect(self, collection_request):
        self.scopes.append(collection_request.scope)
        name = collection_request.scope.resource_names[0]
        resource_kind = "Pod" if "-" in name else "Service"
        return ProviderBatch(
            items=(
                draft("resource-state", resource_kind, name),
                draft("kubernetes-event", resource_kind, name),
            )
        )


class KubernetesIncidentProviderTests(unittest.TestCase):
    def test_pod_event_scope_is_derived_from_rooted_inventory(self) -> None:
        service_events = RecordingEventProvider()
        pod_events = RecordingEventProvider()
        provider = KubernetesIncidentProvider(
            StaticInventory(),
            service_events,
            pod_events,
        )
        collection_request = request()

        batch = provider.collect(collection_request)
        validate_provider_batch(batch, collection_request)

        self.assertEqual(batch.status, "SUCCEEDED")
        self.assertEqual(
            pod_events.scopes[0].resource_names,
            ("checkoutservice-7d9f8-q1w2e",),
        )
        self.assertEqual(
            [item.kind for item in batch.items].count("resource-state"),
            2,
        )
        self.assertEqual(
            [item.kind for item in batch.items].count("kubernetes-event"),
            2,
        )


if __name__ == "__main__":
    unittest.main()
