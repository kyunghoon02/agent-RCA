from __future__ import annotations

import unittest
from datetime import datetime, timezone

from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceDraft,
    EvidenceWindow,
    ResourceScope,
)
from incident_platform.errors import ContractViolation
from incident_platform.localization import (
    AdaptiveScopeController,
    AdaptiveScopePolicy,
    LocalizationAssessment,
)
from incident_platform.stategraph import (
    EntityIdentity,
    GraphLocalizer,
    InMemoryStateGraphRepository,
    InvestigationScope,
    stable_graph_id,
    state_content_hash,
)


UTC = timezone.utc
INCIDENT_ID = "inc-adaptive-localization-0001"
WINDOW = EvidenceWindow(
    start="2026-08-12T01:00:00Z",
    end="2026-08-12T01:10:00Z",
)
ENTITY_IDS = {
    name: EntityIdentity.external(
        domain="web-service", external_key=f"adaptive-fixture:{name}"
    ).entity_id
    for name in ("checkout", "payment", "worker", "gateway")
}


def build_evidence(name: str, *, second: int) -> dict:
    request = CollectionRequest(
        request_id=f"req-adaptive-{name}-0001",
        incident_id=INCIDENT_ID,
        window=WINDOW,
        scope=ResourceScope(
            namespace="online-boutique",
            resource_names=tuple(ENTITY_IDS),
            max_items=10,
        ),
        timeout_seconds=5,
    )
    observed_at = f"2026-08-12T01:0{second}:00Z"
    return EvidenceBuilder().build(
        EvidenceDraft(
            source="prometheus",
            kind="metric-summary",
            observed_at=observed_at,
            subject={
                "api_version": "v1",
                "kind": "Service",
                "namespace": "online-boutique",
                "name": name,
                "uid": f"fixture-{name}-uid",
                "exists": True,
            },
            summary=f"{name} showed an incident-time health signal.",
            facts={"health": "degraded", "signal": name},
            provider="adaptive-localization-fixture",
            query=f"fixture health {name}",
            locator=f"fixture://service/{name}",
        ),
        request,
        collected_at=datetime(2026, 8, 12, 1, 9, tzinfo=UTC),
    )


def entity_record(name: str, evidence_id: str) -> dict:
    entity_id = ENTITY_IDS[name]
    identity = EntityIdentity.external(
        domain="web-service", external_key=f"adaptive-fixture:{name}"
    )
    return {
        "record_type": "entity",
        "entity_id": entity_id,
        "identity": identity.to_contract(),
        "entity_type": "Service",
        "domain": "web-service",
        "name": name,
        "scope": {"environment": "fixture"},
        "external_ref": f"service://{name}",
        "exists": True,
        "first_seen_at": "2026-08-12T01:01:00Z",
        "last_seen_at": "2026-08-12T01:09:00Z",
        "evidence_ids": [evidence_id],
    }


def snapshot_record(name: str, evidence_id: str, *, second: int) -> dict:
    entity_id = ENTITY_IDS[name]
    state = {"health": "degraded", "signal": name}
    digest = state_content_hash(state)
    observed_at = f"2026-08-12T01:0{second}:00Z"
    return {
        "record_type": "snapshot_interval",
        "snapshot_id": stable_graph_id(
            "snap",
            {
                "entity_id": entity_id,
                "state_hash": digest,
                "valid_from": observed_at,
            },
        ),
        "entity_id": entity_id,
        "observed_at": observed_at,
        "valid_from": observed_at,
        "valid_to": None,
        "state_hash": digest,
        "state": state,
        "evidence_ids": [evidence_id],
    }


def relation_record(source: str, destination: str, evidence_id: str) -> dict:
    identity = {
        "source_entity_id": ENTITY_IDS[source],
        "relation_type": "CALLS",
        "destination_entity_id": ENTITY_IDS[destination],
        "reference_key": f"{source}-calls-{destination}",
        "projector": "adaptive-localization-fixture",
    }
    relation_key = stable_graph_id("relkey", identity)
    return {
        "record_type": "relation_interval",
        "relation_id": stable_graph_id(
            "rel",
            {"relation_key": relation_key, "valid_from": "2026-08-12T01:01:00Z"},
        ),
        "relation_key": relation_key,
        **identity,
        "observed_at": "2026-08-12T01:01:00Z",
        "valid_from": "2026-08-12T01:01:00Z",
        "valid_to": None,
        "evidence_ids": [evidence_id],
    }


def graph_fixture(names: tuple[str, ...]) -> tuple[InMemoryStateGraphRepository, tuple[dict, ...]]:
    repository = InMemoryStateGraphRepository()
    evidence = tuple(
        build_evidence(name, second=index + 1) for index, name in enumerate(names)
    )
    records = []
    for index, (name, item) in enumerate(zip(names, evidence), start=1):
        records.append(entity_record(name, item["evidence_id"]))
        records.append(snapshot_record(name, item["evidence_id"], second=index))
    repository.ingest(records)
    return repository, evidence


class SequenceAssessor:
    def __init__(self, *assessments: LocalizationAssessment) -> None:
        self._assessments = list(assessments)
        self.seen_evidence_ids = []

    def assess(self, context, evidence):
        self.seen_evidence_ids.append(
            tuple(sorted(item["evidence_id"] for item in evidence))
        )
        if len(self._assessments) > 1:
            return self._assessments.pop(0)
        return self._assessments[0]


class AdaptiveScopeControllerTests(unittest.TestCase):
    def test_competing_multi_factor_signals_expand_to_new_branches(self) -> None:
        repository, evidence = graph_fixture(
            ("checkout", "payment", "worker", "gateway")
        )
        evidence_by_name = dict(zip(ENTITY_IDS, evidence))
        repository.ingest(
            [
                relation_record(
                    "checkout", "payment", evidence_by_name["payment"]["evidence_id"]
                ),
                relation_record(
                    "payment", "worker", evidence_by_name["worker"]["evidence_id"]
                ),
                relation_record(
                    "payment", "gateway", evidence_by_name["gateway"]["evidence_id"]
                ),
            ]
        )
        initial_scope = InvestigationScope(
            incident_id=INCIDENT_ID,
            seed_entity_ids=(ENTITY_IDS["checkout"],),
            window=WINDOW,
            domains=("web-service",),
            relation_types=("CALLS",),
            max_entities=2,
            max_depth=1,
        )
        assessor = SequenceAssessor(
            LocalizationAssessment(
                evidence_sufficient=False,
                competing_hypotheses=2,
                multi_factor_suspected=True,
                requested_seed_entity_ids=(ENTITY_IDS["payment"],),
                reason_codes=("PAYMENT_AND_SHARED_INFRA_BRANCHES",),
            ),
            LocalizationAssessment(evidence_sufficient=True),
        )
        controller = AdaptiveScopeController(
            GraphLocalizer(repository),
            policy=AdaptiveScopePolicy(
                max_entities=6,
                max_depth=3,
                max_rounds=4,
                entity_step=4,
                depth_step=1,
                minimum_context_completeness=0.7,
            ),
        )

        run = controller.run(
            initial_scope,
            evidence,
            assessor,
            frozen_at=datetime(2026, 8, 12, 1, 10, tzinfo=UTC),
        )

        self.assertEqual(run.stop_reason, "EVIDENCE_SUFFICIENT")
        self.assertFalse(run.budget_exhausted)
        self.assertFalse(run.requires_abstention)
        self.assertEqual(len(run.rounds), 2)
        self.assertEqual(len(run.rounds[0].entity_ids), 2)
        self.assertEqual(len(run.rounds[1].entity_ids), 4)
        self.assertIn("COMPETING_HYPOTHESES", run.rounds[0].expansion_triggers)
        self.assertIn("MULTI_FACTOR_SUSPECTED", run.rounds[0].expansion_triggers)
        self.assertEqual(
            set(run.rounds[1].new_evidence_ids),
            {
                evidence_by_name["worker"]["evidence_id"],
                evidence_by_name["gateway"]["evidence_id"],
            },
        )
        self.assertEqual(run.rounds[1].scope.window, initial_scope.window)
        self.assertEqual(run.rounds[1].scope.domains, initial_scope.domains)
        self.assertEqual(
            run.rounds[1].scope.relation_types,
            initial_scope.relation_types,
        )
        self.assertEqual(len(assessor.seen_evidence_ids[0]), 2)
        self.assertEqual(len(assessor.seen_evidence_ids[1]), 4)

    def test_expansion_stops_when_a_larger_scope_adds_no_context(self) -> None:
        repository, evidence = graph_fixture(("checkout",))
        scope = InvestigationScope(
            incident_id=INCIDENT_ID,
            seed_entity_ids=(ENTITY_IDS["checkout"],),
            window=WINDOW,
            domains=("web-service",),
            relation_types=("CALLS",),
            max_entities=1,
            max_depth=0,
        )
        assessor = SequenceAssessor(
            LocalizationAssessment(
                evidence_sufficient=False,
                reason_codes=("NO_CORROBORATING_SOURCE",),
            )
        )
        controller = AdaptiveScopeController(
            GraphLocalizer(repository),
            policy=AdaptiveScopePolicy(
                max_entities=3,
                max_depth=2,
                max_rounds=4,
                entity_step=1,
                depth_step=1,
            ),
        )

        run = controller.run(
            scope,
            evidence,
            assessor,
            frozen_at=datetime(2026, 8, 12, 1, 10, tzinfo=UTC),
        )

        self.assertEqual(run.stop_reason, "NO_NEW_CONTEXT")
        self.assertFalse(run.budget_exhausted)
        self.assertTrue(run.requires_abstention)
        self.assertEqual(len(run.rounds), 2)
        self.assertEqual(run.rounds[1].new_evidence_ids, ())

    def test_scope_hard_cap_exhaustion_requires_abstention(self) -> None:
        repository, evidence = graph_fixture(("checkout",))
        scope = InvestigationScope(
            incident_id=INCIDENT_ID,
            seed_entity_ids=(ENTITY_IDS["checkout"],),
            window=WINDOW,
            domains=("web-service",),
            max_entities=1,
            max_depth=0,
        )
        assessor = SequenceAssessor(
            LocalizationAssessment(
                evidence_sufficient=False,
                contradiction_count=1,
                reason_codes=("CONFLICTING_SIGNALS",),
            )
        )
        controller = AdaptiveScopeController(
            GraphLocalizer(repository),
            policy=AdaptiveScopePolicy(
                max_entities=1,
                max_depth=0,
                max_rounds=2,
            ),
        )

        run = controller.run(
            scope,
            evidence,
            assessor,
            frozen_at=datetime(2026, 8, 12, 1, 10, tzinfo=UTC),
        )

        self.assertEqual(run.stop_reason, "SCOPE_BUDGET_EXHAUSTED")
        self.assertTrue(run.budget_exhausted)
        self.assertTrue(run.requires_abstention)
        self.assertIn("CONTRADICTORY_EVIDENCE", run.rounds[0].expansion_triggers)

    def test_krca_approved_next_ranked_seed_can_open_a_disconnected_branch(self) -> None:
        repository, evidence = graph_fixture(("checkout", "payment", "gateway"))
        evidence_by_name = dict(zip(("checkout", "payment", "gateway"), evidence))
        repository.ingest(
            [
                relation_record(
                    "checkout", "payment", evidence_by_name["payment"]["evidence_id"]
                )
            ]
        )
        scope = InvestigationScope(
            incident_id=INCIDENT_ID,
            seed_entity_ids=(ENTITY_IDS["checkout"],),
            window=WINDOW,
            domains=("web-service",),
            relation_types=("CALLS",),
            max_entities=2,
            max_depth=1,
        )
        assessor = SequenceAssessor(
            LocalizationAssessment(
                evidence_sufficient=False,
                competing_hypotheses=2,
                requested_seed_entity_ids=(ENTITY_IDS["gateway"],),
                reason_codes=("KRCA_NEXT_RANKED_BRANCH",),
            ),
            LocalizationAssessment(evidence_sufficient=True),
        )
        controller = AdaptiveScopeController(
            GraphLocalizer(repository),
            policy=AdaptiveScopePolicy(
                max_entities=4,
                max_depth=2,
                max_rounds=3,
                entity_step=2,
            ),
        )

        run = controller.run(
            scope,
            evidence,
            assessor,
            frozen_at=datetime(2026, 8, 12, 1, 10, tzinfo=UTC),
            approved_seed_entity_ids=(ENTITY_IDS["gateway"],),
        )

        self.assertEqual(run.stop_reason, "EVIDENCE_SUFFICIENT")
        self.assertIn(ENTITY_IDS["gateway"], run.rounds[1].entity_ids)
        self.assertIn(
            evidence_by_name["gateway"]["evidence_id"],
            run.rounds[1].new_evidence_ids,
        )

    def test_unapproved_external_seed_is_rejected(self) -> None:
        repository, evidence = graph_fixture(("checkout", "gateway"))
        scope = InvestigationScope(
            incident_id=INCIDENT_ID,
            seed_entity_ids=(ENTITY_IDS["checkout"],),
            window=WINDOW,
            domains=("web-service",),
            max_entities=1,
            max_depth=0,
        )
        assessor = SequenceAssessor(
            LocalizationAssessment(
                evidence_sufficient=False,
                requested_seed_entity_ids=(ENTITY_IDS["gateway"],),
            )
        )
        controller = AdaptiveScopeController(
            GraphLocalizer(repository),
            policy=AdaptiveScopePolicy(max_entities=2, max_depth=1),
        )

        with self.assertRaisesRegex(ContractViolation, "approved by upstream"):
            controller.run(
                scope,
                evidence,
                assessor,
                frozen_at=datetime(2026, 8, 12, 1, 10, tzinfo=UTC),
            )


if __name__ == "__main__":
    unittest.main()
