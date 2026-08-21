from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone

from incident_platform.errors import ContractViolation, InvalidTransition
from incident_platform.localization import IncidentLocalizationService
from incident_platform.projectors import KubernetesEvidenceProjector
from incident_platform.repository import InMemoryIncidentRepository
from incident_platform.stategraph import (
    IncidentHistoryPin,
    InMemoryStateGraphRepository,
    InvestigationScope,
    StateGraphPruneResult,
)

from tests.test_stategraph import WINDOW, kubernetes_evidence


UTC = timezone.utc
FROZEN_AT = datetime(2026, 8, 12, 1, 11, tzinfo=UTC)


def incident_for(evidence: tuple[dict, ...], *, status: str = "LOCALIZING") -> dict:
    return {
        "schema_version": "1.0.0",
        "incident_id": evidence[0]["incident_id"],
        "deduplication_key": "fixture:stategraph-localization:0001",
        "status": status,
        "severity": "critical",
        "source": "alertmanager",
        "triggered_at": "2026-08-12T01:04:00Z",
        "window": {
            "baseline_start": WINDOW.start,
            "incident_start": "2026-08-12T01:04:00Z",
            "incident_end": WINDOW.end,
            "recovery_end": None,
        },
        "alert": {
            "fingerprint": "stategraph-localization-fixture",
            "name": "CheckoutConfigFailure",
            "labels": {
                "alertname": "CheckoutConfigFailure",
                "namespace": "online-boutique",
                "severity": "critical",
            },
            "annotations": {},
        },
        "source_entity": copy.deepcopy(evidence[0]["subject"]),
        "collector_statuses": [
            {
                "collector": "kubernetes",
                "status": "SUCCEEDED",
                "attempts": 1,
                "started_at": "2026-08-12T01:04:58Z",
                "ended_at": "2026-08-12T01:05:03Z",
                "error": None,
            },
            {
                "collector": "logs",
                "status": "TIMED_OUT",
                "attempts": 2,
                "started_at": "2026-08-12T01:04:58Z",
                "ended_at": "2026-08-12T01:05:05Z",
                "error": "collector exceeded its query budget",
            },
        ],
        "created_at": "2026-08-12T01:04:00Z",
        "updated_at": "2026-08-12T01:05:05Z",
    }


def localization_scope(
    evidence: tuple[dict, ...],
    projector: KubernetesEvidenceProjector,
) -> InvestigationScope:
    projection = projector.project(evidence[0])
    pod = next(
        record
        for record in projection.records
        if record["record_type"] == "entity" and record["entity_type"] == "Pod"
    )
    return InvestigationScope(
        incident_id=evidence[0]["incident_id"],
        seed_entity_ids=(pod["entity_id"],),
        window=WINDOW,
        domains=("kubernetes",),
        relation_types=("REFERENCES",),
        max_entities=2,
        max_depth=1,
    )


class IncidentLocalizationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = kubernetes_evidence()
        self.projector = KubernetesEvidenceProjector()
        self.incident_repository = InMemoryIncidentRepository()
        self.stategraph_repository = InMemoryStateGraphRepository()

    def store_incident(self, *, status: str = "LOCALIZING") -> dict:
        incident = incident_for(self.evidence, status=status)
        self.incident_repository.create_or_get_by_deduplication_key(
            incident,
            occurred_at=FROZEN_AT,
        )
        self.incident_repository.store_evidence(
            incident["incident_id"],
            self.evidence,
        )
        return incident

    def service(self) -> IncidentLocalizationService:
        return IncidentLocalizationService(
            self.incident_repository,
            self.stategraph_repository,
            (self.projector,),
        )

    def test_projects_stored_evidence_freezes_context_and_advances_lifecycle(self) -> None:
        incident = self.store_incident()
        scope = localization_scope(self.evidence, self.projector)

        run = self.service().localize_incident(
            incident["incident_id"],
            scope=scope,
            frozen_at=FROZEN_AT,
        )

        self.assertEqual(run.incident["status"], "ANALYZING")
        self.assertEqual(run.context["localization"]["strategy"], "stategraph")
        self.assertEqual(run.projected_record_count, 7)
        self.assertEqual(
            set(run.projected_evidence_ids),
            {item["evidence_id"] for item in self.evidence},
        )
        self.assertEqual(
            self.incident_repository.get_context(run.context["context_id"]),
            run.context,
        )
        self.assertEqual(
            run.context["collector_failures"],
            [
                {
                    "collector": "logs",
                    "error": "collector exceeded its query budget",
                }
            ],
        )

    def test_wrong_lifecycle_state_is_rejected_without_mutation(self) -> None:
        incident = self.store_incident(status="RECEIVED")

        with self.assertRaisesRegex(InvalidTransition, "requires LOCALIZING"):
            self.service().localize_incident(
                incident["incident_id"],
                scope=localization_scope(self.evidence, self.projector),
                frozen_at=FROZEN_AT,
            )

        self.assertEqual(
            self.incident_repository.get(incident["incident_id"])["status"],
            "RECEIVED",
        )

    def test_localization_failure_marks_the_incident_failed(self) -> None:
        incident = self.store_incident()
        scope = InvestigationScope(
            incident_id=incident["incident_id"],
            seed_entity_ids=("ent-stategraph-absent-seed-0001",),
            window=WINDOW,
            domains=("kubernetes",),
            max_entities=2,
            max_depth=1,
        )

        with self.assertRaisesRegex(ContractViolation, "seed is absent"):
            self.service().localize_incident(
                incident["incident_id"],
                scope=scope,
                frozen_at=FROZEN_AT,
            )

        self.assertEqual(
            self.incident_repository.get(incident["incident_id"])["status"],
            "FAILED",
        )

    def test_scope_outside_incident_window_is_rejected_without_mutation(self) -> None:
        incident = self.store_incident()
        valid_scope = localization_scope(self.evidence, self.projector)
        invalid_scope = InvestigationScope(
            incident_id=incident["incident_id"],
            seed_entity_ids=valid_scope.seed_entity_ids,
            window=type(WINDOW)(
                start="2026-08-12T00:59:59Z",
                end=WINDOW.end,
            ),
            domains=valid_scope.domains,
            relation_types=valid_scope.relation_types,
            max_entities=valid_scope.max_entities,
            max_depth=valid_scope.max_depth,
        )

        with self.assertRaisesRegex(ContractViolation, "before the Incident baseline"):
            self.service().localize_incident(
                incident["incident_id"],
                scope=invalid_scope,
                frozen_at=FROZEN_AT,
            )

        self.assertEqual(
            self.incident_repository.get(incident["incident_id"])["status"],
            "LOCALIZING",
        )

    def test_persistent_history_is_pinned_after_context_storage(self) -> None:
        class PinningStateGraphRepository(InMemoryStateGraphRepository):
            def __init__(self) -> None:
                super().__init__()
                self.pins = []

            def pin_incident_history(
                self, scope, entity_ids, *, pinned_at
            ) -> IncidentHistoryPin:
                pin = IncidentHistoryPin(
                    incident_id=scope.incident_id,
                    entity_ids=tuple(entity_ids),
                    window=scope.window,
                    pinned_at=pinned_at,
                    expires_at=pinned_at.replace(day=pinned_at.day + 1),
                )
                self.pins.append(pin)
                return pin

            def prune_history(self, *, now, batch_size=1000) -> StateGraphPruneResult:
                return StateGraphPruneResult()

        incident = self.store_incident()
        repository = PinningStateGraphRepository()
        service = IncidentLocalizationService(
            self.incident_repository,
            repository,
            (self.projector,),
        )

        run = service.localize_incident(
            incident["incident_id"],
            scope=localization_scope(self.evidence, self.projector),
            frozen_at=FROZEN_AT,
        )

        self.assertEqual(len(repository.pins), 1)
        self.assertEqual(repository.pins[0].incident_id, incident["incident_id"])
        self.assertEqual(
            set(repository.pins[0].entity_ids),
            {
                entity["entity_id"]
                for path in run.context["state_paths"]
                for entity in path["entities"]
            },
        )


if __name__ == "__main__":
    unittest.main()
