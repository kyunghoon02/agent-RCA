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
from incident_platform.stategraph_observations import (
    InMemoryStateGraphObservationRepository,
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
        self.observation_repository = InMemoryStateGraphObservationRepository()
        self.reconciler = KubernetesStateGraphReconciler(
            KubernetesInventoryProvider(
                self.client,
                cluster_id=CLUSTER_ID,
            ),
            self.repository,
            self.observation_repository,
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
        self.assertEqual(first.cycle.status, "APPLIED")
        self.assertEqual(
            self.observation_repository.list_cycle_evidence(first.cycle.cycle_id),
            tuple(sorted(first.evidence, key=lambda item: item["evidence_id"])),
        )
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
        third_evidence_ids = {item["evidence_id"] for item in third.evidence}
        reopened_pod = self.repository.get_entity(POD_ENTITY_ID)
        self.assertTrue(reopened_pod["exists"])
        self.assertEqual(len(reopened_pod["evidence_ids"]), 1)
        self.assertIn(reopened_pod["evidence_ids"][0], third_evidence_ids)
        reopened_snapshots = self.repository.list_snapshots(POD_ENTITY_ID)
        self.assertEqual(len(reopened_snapshots), 2)
        self.assertIsNone(reopened_snapshots[-1]["valid_to"])
        self.assertEqual(len(reopened_snapshots[-1]["evidence_ids"]), 1)
        self.assertIn(
            reopened_snapshots[-1]["evidence_ids"][0],
            third_evidence_ids,
        )
        reopened_relations = self.repository.list_relations()
        reopened_keys = {
            item["relation_key"]
            for item in reopened_relations
            if item["valid_to"] is None
        }
        self.assertEqual(reopened_keys, initial_active_keys)
        self.assertGreater(len(reopened_relations), len(initial_relations))
        for relation in reopened_relations:
            if relation["valid_to"] is None:
                self.assertEqual(len(relation["evidence_ids"]), 1)
                self.assertIn(relation["evidence_ids"][0], third_evidence_ids)

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
            self.observation_repository,
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

    def test_observation_storage_failure_prevents_graph_mutation(self) -> None:
        class FailingObservationRepository:
            def get_cycle(self, _cycle_id):
                raise KeyError("not staged")

            def stage_cycle(self, _cycle, _evidence):
                raise RuntimeError("observation storage unavailable")

        graph_repository = InMemoryStateGraphRepository()
        reconciler = KubernetesStateGraphReconciler(
            KubernetesInventoryProvider(self.client, cluster_id=CLUSTER_ID),
            graph_repository,
            FailingObservationRepository(),
            cluster_id=CLUSTER_ID,
        )
        observed_at = datetime(2026, 8, 24, 1, 5, tzinfo=UTC)

        with self.assertRaisesRegex(RuntimeError, "storage unavailable"):
            reconciler.reconcile(
                request_at(1, observed_at),
                collected_at=observed_at,
            )

        self.assertEqual(graph_repository.list_relations(), [])

    def test_graph_failure_leaves_a_retryable_staged_cycle(self) -> None:
        class FailingGraphRepository(InMemoryStateGraphRepository):
            def reconcile_projection(self, *_args, **_kwargs):
                raise RuntimeError("graph unavailable")

        class RecordingObservationRepository(
            InMemoryStateGraphObservationRepository
        ):
            def __init__(self) -> None:
                super().__init__()
                self.staged_cycle_ids = []

            def stage_cycle(self, cycle, evidence):
                staged = super().stage_cycle(cycle, evidence)
                self.staged_cycle_ids.append(staged.cycle_id)
                return staged

        observations = RecordingObservationRepository()
        reconciler = KubernetesStateGraphReconciler(
            KubernetesInventoryProvider(self.client, cluster_id=CLUSTER_ID),
            FailingGraphRepository(),
            observations,
            cluster_id=CLUSTER_ID,
        )
        observed_at = datetime(2026, 8, 24, 1, 5, tzinfo=UTC)

        with self.assertRaisesRegex(RuntimeError, "graph unavailable"):
            reconciler.reconcile(
                request_at(1, observed_at),
                collected_at=observed_at,
            )

        cycle = observations.get_cycle(observations.staged_cycle_ids[0])
        self.assertEqual(cycle.status, "STAGED")
        self.assertIsNone(cycle.result)

        self.client.resources = {
            resource_type: () for resource_type in self.resources
        }
        recovery_graph = InMemoryStateGraphRepository()
        recovery = KubernetesStateGraphReconciler(
            KubernetesInventoryProvider(self.client, cluster_id=CLUSTER_ID),
            recovery_graph,
            observations,
            cluster_id=CLUSTER_ID,
        ).reconcile(
            request_at(1, observed_at),
            collected_at=observed_at + timedelta(minutes=1),
        )

        self.assertEqual(recovery.cycle.status, "APPLIED")
        self.assertEqual(
            recovery.cycle.applied_at,
            format_time(observed_at + timedelta(minutes=1)),
        )
        self.assertTrue(recovery_graph.get_entity(POD_ENTITY_ID)["exists"])

    def test_applied_cycle_retry_does_not_mutate_the_graph_twice(self) -> None:
        class CountingProvider:
            def __init__(self, provider) -> None:
                self.provider = provider
                self.calls = 0

            def collect(self, request):
                self.calls += 1
                return self.provider.collect(request)

        class CountingGraphRepository(InMemoryStateGraphRepository):
            def __init__(self) -> None:
                super().__init__()
                self.reconciliation_calls = 0

            def reconcile_projection(self, *args, **kwargs):
                self.reconciliation_calls += 1
                return super().reconcile_projection(*args, **kwargs)

        graph_repository = CountingGraphRepository()
        observations = InMemoryStateGraphObservationRepository()
        provider = CountingProvider(
            KubernetesInventoryProvider(self.client, cluster_id=CLUSTER_ID)
        )
        reconciler = KubernetesStateGraphReconciler(
            provider,
            graph_repository,
            observations,
            cluster_id=CLUSTER_ID,
        )
        observed_at = datetime(2026, 8, 24, 1, 5, tzinfo=UTC)
        collection_request = request_at(1, observed_at)

        first = reconciler.reconcile(
            collection_request,
            collected_at=observed_at,
        )
        repeated = reconciler.reconcile(
            collection_request,
            collected_at=observed_at + timedelta(minutes=1),
        )

        self.assertEqual(provider.calls, 1)
        self.assertEqual(graph_repository.reconciliation_calls, 1)
        self.assertEqual(repeated.cycle, first.cycle)
        self.assertEqual(repeated.result, first.result)


if __name__ == "__main__":
    unittest.main()
