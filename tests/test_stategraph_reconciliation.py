from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from incident_platform.errors import ContractViolation
from incident_platform.evidence import (
    CollectionRequest,
    EvidenceWindow,
    ProviderBatch,
    ResourceScope,
    format_time,
)
from incident_platform.providers.kubernetes import KubernetesInventoryProvider
from incident_platform.reconciliation import KubernetesStateGraphReconciler
from incident_platform.stategraph import (
    EntityIdentity,
    InMemoryStateGraphRepository,
    StateGraphReconciliationScope,
)

from tests.test_kubernetes_inventory import (
    StaticInventoryClient,
    inventory_resources,
)


UTC = timezone.utc
CLUSTER_ID = "agent-rca-reconciliation-test"
NAMESPACE = "online-boutique"
POD_ENTITY_ID = EntityIdentity.kubernetes_resource(
    cluster_id=CLUSTER_ID,
    uid="uid-pod-checkout",
).entity_id


def request_at(sequence: int, observed_at: datetime) -> CollectionRequest:
    return CollectionRequest(
        request_id=f"req-stategraph-reconcile-{sequence:02d}",
        incident_id=f"inc-stategraph-reconcile-{sequence:02d}",
        window=EvidenceWindow(
            start=format_time(observed_at - timedelta(minutes=5)),
            end=format_time(observed_at),
        ),
        scope=ResourceScope(
            namespace=NAMESPACE,
            resource_names=("checkoutservice",),
            resource_name_prefixes=("checkoutservice-",),
            max_items=20,
        ),
        timeout_seconds=5,
    )


class KubernetesStateGraphReconcilerTests(unittest.TestCase):
    def test_ownership_scope_can_be_exact_only_but_rejects_wide_prefixes(
        self,
    ) -> None:
        scope = StateGraphReconciliationScope(
            cluster_id=CLUSTER_ID,
            namespace=NAMESPACE,
            resource_names=("checkoutservice",),
            resource_name_prefixes=(),
            projector="test-projector",
            managed_entity_types=("Service",),
            managed_relation_types=("REPRESENTED_BY",),
        )

        self.assertTrue(scope.contains_name("checkoutservice"))
        self.assertFalse(scope.contains_name("checkoutservice-pod"))
        with self.assertRaisesRegex(ContractViolation, "derive from exact roots"):
            StateGraphReconciliationScope(
                cluster_id=CLUSTER_ID,
                namespace=NAMESPACE,
                resource_names=("checkoutservice",),
                resource_name_prefixes=("checkout",),
                projector="test-projector",
                managed_entity_types=("Service",),
                managed_relation_types=("REPRESENTED_BY",),
            )

    def setUp(self) -> None:
        self.resources = inventory_resources()
        self.client = StaticInventoryClient(self.resources)
        self.repository = InMemoryStateGraphRepository()
        self.reconciler = KubernetesStateGraphReconciler(
            KubernetesInventoryProvider(
                self.client,
                cluster_id=CLUSTER_ID,
            ),
            self.repository,
            cluster_id=CLUSTER_ID,
        )

    def test_disappearance_closes_intervals_and_reappearance_opens_new_ones(
        self,
    ) -> None:
        first_at = datetime(2026, 8, 24, 1, 5, tzinfo=UTC)
        first = self.reconciler.reconcile(
            request_at(1, first_at),
            collected_at=first_at,
        )
        initial_relations = self.repository.list_relations()
        initial_active_keys = {
            item["relation_key"]
            for item in initial_relations
            if item["valid_to"] is None
        }

        self.assertEqual(first.result.retired_entities, 0)
        self.assertEqual(first.result.closed_snapshot_intervals, 0)
        self.assertEqual(first.result.closed_relation_intervals, 0)
        self.assertGreater(first.projected_record_count, len(first.evidence))
        self.assertTrue(initial_active_keys)

        without_pod = copy.deepcopy(self.resources)
        without_pod["Pod"] = ()
        without_pod["EndpointSlice"] = ()
        self.client.resources = without_pod
        second_at = first_at + timedelta(minutes=1)
        second = self.reconciler.reconcile(
            request_at(2, second_at),
            collected_at=second_at,
        )

        self.assertEqual(second.result.retired_entities, 2)
        self.assertEqual(second.result.closed_snapshot_intervals, 2)
        self.assertGreaterEqual(second.result.closed_relation_intervals, 3)
        self.assertFalse(self.repository.get_entity(POD_ENTITY_ID)["exists"])
        pod_history = self.repository.list_snapshots(POD_ENTITY_ID)
        self.assertEqual(pod_history[-1]["valid_to"], format_time(second_at))

        self.client.resources = self.resources
        third_at = second_at + timedelta(minutes=1)
        third = self.reconciler.reconcile(
            request_at(3, third_at),
            collected_at=third_at,
        )

        self.assertEqual(third.result.retired_entities, 0)
        self.assertTrue(self.repository.get_entity(POD_ENTITY_ID)["exists"])
        reopened_snapshots = self.repository.list_snapshots(POD_ENTITY_ID)
        self.assertEqual(len(reopened_snapshots), 2)
        self.assertIsNone(reopened_snapshots[-1]["valid_to"])
        reopened_relations = self.repository.list_relations()
        reopened_keys = {
            item["relation_key"]
            for item in reopened_relations
            if item["valid_to"] is None
        }
        self.assertEqual(reopened_keys, initial_active_keys)
        self.assertGreater(len(reopened_relations), len(initial_relations))

    def test_partial_provider_batch_cannot_retire_live_graph_state(self) -> None:
        observed_at = datetime(2026, 8, 24, 1, 5, tzinfo=UTC)
        self.reconciler.reconcile(
            request_at(1, observed_at),
            collected_at=observed_at,
        )
        before_entities = self.repository.get_entity(POD_ENTITY_ID)
        before_relations = self.repository.list_relations()

        class PartialProvider:
            def collect(self, _request: CollectionRequest) -> ProviderBatch:
                return ProviderBatch(
                    status="PARTIAL",
                    error="inventory page timed out",
                )

        reconciler = KubernetesStateGraphReconciler(
            PartialProvider(),
            self.repository,
            cluster_id=CLUSTER_ID,
        )
        next_at = observed_at + timedelta(minutes=1)
        with self.assertRaisesRegex(ContractViolation, "complete ProviderBatch"):
            reconciler.reconcile(
                request_at(2, next_at),
                collected_at=next_at,
            )

        self.assertEqual(self.repository.get_entity(POD_ENTITY_ID), before_entities)
        self.assertEqual(self.repository.list_relations(), before_relations)


if __name__ == "__main__":
    unittest.main()
