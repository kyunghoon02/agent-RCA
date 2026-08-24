from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from incident_platform.errors import InvalidTransition
from incident_platform.stategraph import (
    StateGraphReconciliationResult,
    stable_graph_id,
)
from incident_platform.stategraph_observations import (
    InMemoryStateGraphObservationRepository,
    StateGraphObservationCycle,
    StateGraphObservationRepository,
)

from tests.test_stategraph import kubernetes_evidence


UTC = timezone.utc
STAGED_AT = datetime(2026, 8, 12, 1, 6, tzinfo=UTC)
RESULT = StateGraphReconciliationResult(
    ingested_records=7,
    current_entities=2,
    current_relations=1,
    retired_entities=0,
    closed_snapshot_intervals=0,
    closed_relation_intervals=0,
)


def staged_cycle(evidence: tuple[dict, ...]) -> StateGraphObservationCycle:
    identity = {
        "request_id": "req-stategraph-observation-0001",
        "cluster_id": "gcp-dev-01",
        "namespace": "online-boutique",
        "observed_at": "2026-08-12T01:05:02Z",
    }
    return StateGraphObservationCycle(
        cycle_id=stable_graph_id("cycle", identity),
        request_id=identity["request_id"],
        evidence_scope_id=evidence[0]["incident_id"],
        cluster_id=identity["cluster_id"],
        namespace=identity["namespace"],
        observed_at=identity["observed_at"],
        staged_at="2026-08-12T01:06:00Z",
        status="STAGED",
        evidence_ids=tuple(sorted(item["evidence_id"] for item in evidence)),
    )


class InMemoryStateGraphObservationRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = kubernetes_evidence()
        self.cycle = staged_cycle(self.evidence)
        self.repository = InMemoryStateGraphObservationRepository()

    def test_journal_is_idempotent_and_satisfies_the_port(self) -> None:
        self.assertIsInstance(self.repository, StateGraphObservationRepository)

        first = self.repository.stage_cycle(self.cycle, self.evidence)
        repeated = self.repository.stage_cycle(self.cycle, self.evidence)
        applied = self.repository.mark_cycle_applied(
            self.cycle.cycle_id,
            RESULT,
            applied_at=STAGED_AT,
        )

        self.assertEqual(first, repeated)
        self.assertEqual(applied.status, "APPLIED")
        self.assertEqual(applied.result, RESULT)
        self.assertEqual(self.repository.get_cycle(self.cycle.cycle_id), applied)
        self.assertEqual(
            self.repository.list_cycle_evidence(self.cycle.cycle_id),
            tuple(sorted(self.evidence, key=lambda item: item["evidence_id"])),
        )

    def test_cycle_and_evidence_collisions_fail_closed(self) -> None:
        self.repository.stage_cycle(self.cycle, self.evidence)
        conflicting = StateGraphObservationCycle(
            **{
                **self.cycle.__dict__,
                "namespace": "another-namespace",
            }
        )

        with self.assertRaisesRegex(InvalidTransition, "cycle collision"):
            self.repository.stage_cycle(conflicting, self.evidence)

    def test_applied_and_abandoned_staged_cycles_have_separate_retention(self) -> None:
        self.repository.stage_cycle(self.cycle, self.evidence)
        self.repository.mark_cycle_applied(
            self.cycle.cycle_id,
            RESULT,
            applied_at=STAGED_AT,
        )

        retained = self.repository.prune_observations(
            now=STAGED_AT + timedelta(hours=71),
        )
        pruned = self.repository.prune_observations(
            now=STAGED_AT + timedelta(hours=73),
        )

        self.assertEqual(retained.cycles, 0)
        self.assertEqual(pruned.cycles, 1)
        self.assertEqual(pruned.evidence_items, len(self.evidence))
        with self.assertRaises(KeyError):
            self.repository.get_cycle(self.cycle.cycle_id)

        abandoned_repository = InMemoryStateGraphObservationRepository()
        abandoned_repository.stage_cycle(self.cycle, self.evidence)
        abandoned = abandoned_repository.prune_observations(
            now=STAGED_AT + timedelta(hours=25),
        )
        self.assertEqual(abandoned.cycles, 1)


if __name__ == "__main__":
    unittest.main()
