from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from incident_platform.collectors import (
    CollectorOrchestrator,
    CollectorSpec,
    IncidentCollectionService,
)
from incident_platform.errors import ContractViolation, InvalidTransition
from incident_platform.evidence import (
    CollectionRequest, EvidenceBuilder, EvidenceDraft, EvidenceWindow, ProviderBatch, ResourceScope,
)
from incident_platform.incident_work import (
    InMemoryIncidentAnalysisWorkRepository,
    InMemoryIncidentLocalizationWorkRepository,
    InMemoryIncidentWorkRepository,
)
from incident_platform.incidents import AlertmanagerIngestionService
from incident_platform.repository import InMemoryIncidentRepository
from tests.test_agent_rca import prepared_repository


UTC = timezone.utc
NOW = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)


def incident_payload(fingerprint: str) -> dict:
    return {
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "IncidentWorkerContract",
                    "namespace": "online-boutique",
                    "service": "frontend",
                    "severity": "warning",
                },
                "annotations": {},
                "startsAt": "2026-08-24T07:55:00Z",
                "endsAt": "2099-01-01T00:00:00Z",
                "fingerprint": fingerprint,
            }
        ]
    }


def collection_result(incident_id, *, now=NOW, name="frontend", value=1.0):
    timestamp = now.isoformat().replace("+00:00", "Z")
    request = CollectionRequest(
        request_id="req-collection-commit-test", incident_id=incident_id,
        window=EvidenceWindow(start=timestamp, end=timestamp),
        scope=ResourceScope(namespace="online-boutique", resource_names=(name,)),
        timeout_seconds=1,
    )
    evidence = EvidenceBuilder().build(EvidenceDraft(
        source="prometheus", kind="metric-summary", observed_at=timestamp,
        subject={"api_version": "v1", "kind": "Service", "namespace": "online-boutique", "name": name, "uid": None, "exists": True},
        summary="Collection commit fixture.", facts={"latest": value},
        provider="commit-test", query="bounded fixture", locator="fixture://collection-commit",
    ), request, collected_at=now)
    statuses = [{"collector": "prometheus", "status": "SUCCEEDED", "attempts": 1,
                 "started_at": timestamp, "ended_at": timestamp, "error": None}]
    return statuses, [evidence]


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
                    summary="Frontend metric was collected.",
                    facts={"result_status": "HAS_DATA", "latest": 1.0},
                    provider="incident-worker-contract",
                    query="service=frontend",
                    locator="prometheus://frontend/contract",
                ),
            )
        )


class IncidentWorkClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.incidents = InMemoryIncidentRepository()
        self.work = InMemoryIncidentWorkRepository(self.incidents)
        self.incident = AlertmanagerIngestionService(self.incidents).ingest(
            incident_payload("incident-work-contract"),
            received_at=NOW,
        )[0].incident
        self.work.enqueue(self.incident["incident_id"], available_at=NOW)

    def claim(self, *, worker_id: str = "worker-a", now: datetime = NOW):
        return self.work.claim_next(
            worker_id=worker_id,
            now=now,
            lease_duration=timedelta(seconds=30),
            max_attempts=3,
        )

    def test_claim_is_exclusive_and_transitions_incident_atomically(self) -> None:
        claim = self.claim()

        self.assertIsNotNone(claim)
        self.assertEqual(claim.attempt_count, 1)
        self.assertEqual(
            self.incidents.get(self.incident["incident_id"])["status"],
            "COLLECTING",
        )
        self.assertIsNone(self.claim(worker_id="worker-b"))

    def test_expired_claim_is_fenced_when_another_worker_reclaims_it(self) -> None:
        stale = self.claim()
        reclaimed = self.claim(worker_id="worker-b", now=NOW + timedelta(seconds=31))

        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed.attempt_count, 2)
        self.assertNotEqual(stale.claim_token, reclaimed.claim_token)
        with self.assertRaisesRegex(InvalidTransition, "stale"):
            self.work.fail(
                stale,
                now=NOW + timedelta(seconds=32),
                error_code="STALE_WORKER",
            )

    def test_exhausted_crash_retries_fail_closed(self) -> None:
        self.claim(now=NOW)
        self.claim(worker_id="worker-b", now=NOW + timedelta(seconds=31))
        self.claim(worker_id="worker-c", now=NOW + timedelta(seconds=62))

        reaped = self.work.reap_exhausted(
            now=NOW + timedelta(seconds=93),
            max_attempts=3,
        )

        self.assertEqual(reaped, 1)
        self.assertEqual(
            self.incidents.get(self.incident["incident_id"])["status"],
            "FAILED",
        )

    def test_claimed_collection_persists_evidence_then_completes_work(self) -> None:
        claim = self.claim()
        service = IncidentCollectionService(
            self.incidents,
            CollectorOrchestrator(
                [CollectorSpec("prometheus", StaticProvider())]
            ),
            clock=lambda: NOW + timedelta(seconds=2),
        )

        run = service.collect_claimed_incident(
            claim,
            work_repository=self.work,
            scope=ResourceScope(
                namespace="online-boutique",
                resource_names=("frontend",),
                max_items=8,
            ),
            observed_at=NOW + timedelta(seconds=1),
        )
        self.work.complete(
            claim,
            now=NOW + timedelta(seconds=2),
            outcome=run.status,
        )

        self.assertEqual(run.status, "SUCCEEDED")
        self.assertEqual(
            self.incidents.get(claim.incident_id)["status"],
            "LOCALIZING",
        )
        self.assertEqual(len(self.incidents.list_evidence(claim.incident_id)), 1)

    def _collection_snapshot(self, incident_id):
        return (self.incidents.get(incident_id), self.incidents.list_evidence(incident_id),
                self.incidents.list_audit_events(incident_id), copy.deepcopy(self.work._items[incident_id]))

    def test_collection_commit_rejects_wrong_token_and_worker(self) -> None:
        claim = self.claim()
        statuses, evidence = collection_result(claim.incident_id)
        before = self._collection_snapshot(claim.incident_id)
        for invalid in (replace(claim, claim_token="claim-not-current"), replace(claim, worker_id="wrong-worker")):
            with self.assertRaisesRegex(InvalidTransition, "stale"):
                self.work.commit_collection(invalid, collector_statuses=statuses, evidence_items=evidence,
                                            outcome="SUCCEEDED", now=NOW + timedelta(seconds=1))
            self.assertEqual(self._collection_snapshot(claim.incident_id), before)

    def test_collection_commit_rejects_expired_unreclaimed_lease(self) -> None:
        claim = self.claim()
        statuses, evidence = collection_result(claim.incident_id)
        before = self._collection_snapshot(claim.incident_id)
        with self.assertRaisesRegex(InvalidTransition, "expired"):
            self.work.commit_collection(claim, collector_statuses=statuses, evidence_items=evidence,
                                        outcome="SUCCEEDED", now=claim.lease_expires_at)
        self.assertEqual(self._collection_snapshot(claim.incident_id), before)

    def test_collection_commit_uses_renewed_persisted_lease(self) -> None:
        claim = self.claim()
        self.work.renew(claim, now=NOW + timedelta(seconds=20), lease_duration=timedelta(seconds=30))
        statuses, evidence = collection_result(claim.incident_id)
        self.work.commit_collection(claim, collector_statuses=statuses, evidence_items=evidence,
                                    outcome="SUCCEEDED", now=NOW + timedelta(seconds=31))
        self.assertEqual(self.incidents.list_evidence(claim.incident_id), evidence)
        self.assertEqual(self.incidents.get(claim.incident_id)["status"], "LOCALIZING")
        self.assertEqual(self.work._items[claim.incident_id]["state"], "RUNNING")
        with self.assertRaisesRegex(InvalidTransition, "COLLECTING"):
            self.work.commit_collection(claim, collector_statuses=statuses, evidence_items=evidence,
                                        outcome="SUCCEEDED", now=NOW + timedelta(seconds=32))

    def test_collection_commit_partial_and_failed_preserve_evidence(self) -> None:
        for outcome in ("PARTIAL", "FAILED"):
            with self.subTest(outcome=outcome):
                self.setUp()
                claim = self.claim()
                statuses, evidence = collection_result(claim.incident_id)
                statuses[0].update(status=outcome, error="bounded fixture failure")
                self.work.commit_collection(claim, collector_statuses=statuses, evidence_items=evidence,
                                            outcome=outcome, now=NOW + timedelta(seconds=1))
                self.assertEqual(self.incidents.list_evidence(claim.incident_id), evidence)
                self.assertEqual(self.incidents.get(claim.incident_id)["status"], "FAILED" if outcome == "FAILED" else "LOCALIZING")
                self.assertEqual(self.incidents.get(claim.incident_id)["collector_statuses"], statuses)

    def test_invalid_collection_batch_has_no_partial_mutations(self) -> None:
        claim = self.claim()
        statuses, evidence = collection_result(claim.incident_id)
        before = self._collection_snapshot(claim.incident_id)
        invalid = {**evidence[0], "schema_version": "invalid-fixture-marker"}
        foreign = collection_result("inc-foreign-evidence-fixture")[1][0]
        cases = [
            (statuses, [*evidence, invalid], "SUCCEEDED", ContractViolation),
            (statuses, [*evidence, foreign], "SUCCEEDED", InvalidTransition),
            ([*statuses, *statuses], evidence, "SUCCEEDED", InvalidTransition),
            ([{**statuses[0], "status": "invalid"}], evidence, "SUCCEEDED", ContractViolation),
            (statuses, evidence, "unknown", ValueError),
        ]
        for status_batch, evidence_batch, outcome, exception in cases:
            with self.subTest(outcome=outcome, exception=exception):
                with self.assertRaises(exception) as raised:
                    self.work.commit_collection(claim, collector_statuses=status_batch, evidence_items=evidence_batch,
                                                outcome=outcome, now=NOW + timedelta(seconds=1))
                self.assertNotIn("invalid-fixture-marker", str(raised.exception))
                self.assertEqual(self._collection_snapshot(claim.incident_id), before)

    def test_collection_collision_rolls_back_valid_prefix_for_stored_and_batch_ids(self) -> None:
        for persisted_collision in (False, True):
            with self.subTest(persisted=persisted_collision):
                self.setUp()
                claim = self.claim()
                statuses, first = collection_result(claim.incident_id, value=1)
                second = collection_result(claim.incident_id, value=2)[1][0]
                collision = {**second, "evidence_id": first[0]["evidence_id"]}
                if persisted_collision:
                    self.incidents.store_evidence(claim.incident_id, first)
                    candidates = [second, collision]
                else:
                    candidates = [*first, collision]
                before = self._collection_snapshot(claim.incident_id)
                with self.assertRaisesRegex(InvalidTransition, "collision"):
                    self.work.commit_collection(claim, collector_statuses=statuses, evidence_items=candidates,
                                                outcome="SUCCEEDED", now=NOW + timedelta(seconds=1))
                self.assertEqual(self._collection_snapshot(claim.incident_id), before)

    def test_stale_collection_result_is_not_persisted(self) -> None:
        stale = self.claim()
        snapshots = []

        def snapshot():
            return (
                self.incidents.get(stale.incident_id),
                self.incidents.list_evidence(stale.incident_id),
                self.incidents.list_audit_events(stale.incident_id),
                copy.deepcopy(self.work._items[stale.incident_id]),
            )

        class PausingProvider(StaticProvider):
            def collect(provider, request):
                replacement = self.claim(worker_id="worker-b", now=NOW + timedelta(seconds=31))
                self.assertNotEqual(replacement.claim_token, stale.claim_token)
                snapshots.append(snapshot())
                return super().collect(request)

        service = IncidentCollectionService(
            self.incidents, CollectorOrchestrator([CollectorSpec("prometheus", PausingProvider())]),
            clock=lambda: NOW + timedelta(seconds=32),
        )
        rejected = None
        try:
            service.collect_claimed_incident(
                stale,
                work_repository=self.work,
                scope=ResourceScope(namespace="online-boutique", resource_names=("frontend",)),
                observed_at=NOW,
            )
        except InvalidTransition as error:
            rejected = error
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshot(), snapshots[0])
        self.assertIsNotNone(rejected)

    def test_reaper_recovers_when_collection_committed_before_work_completion(self) -> None:
        claim = self.claim()
        service = IncidentCollectionService(
            self.incidents,
            CollectorOrchestrator(
                [CollectorSpec("prometheus", StaticProvider())]
            ),
            clock=lambda: NOW + timedelta(seconds=2),
        )
        service.collect_claimed_incident(
            claim,
            work_repository=self.work,
            scope=ResourceScope(
                namespace="online-boutique",
                resource_names=("frontend",),
                max_items=8,
            ),
            observed_at=NOW + timedelta(seconds=1),
        )

        reaped = self.work.reap_exhausted(
            now=NOW + timedelta(seconds=31),
            max_attempts=3,
        )

        self.assertEqual(reaped, 1)
        self.assertEqual(
            self.incidents.get(claim.incident_id)["status"],
            "LOCALIZING",
        )
        with self.assertRaisesRegex(InvalidTransition, "stale"):
            self.work.complete(
                claim,
                now=NOW + timedelta(seconds=32),
                outcome="SUCCEEDED",
            )


class IncidentLocalizationWorkClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.incidents = InMemoryIncidentRepository()
        self.collection_work = InMemoryIncidentWorkRepository(self.incidents)
        incident = AlertmanagerIngestionService(self.incidents).ingest(
            incident_payload("incident-localization-work-contract"),
            received_at=NOW,
        )[0].incident
        self.collection_work.enqueue(incident["incident_id"], available_at=NOW)
        claim = self.collection_work.claim_next(
            worker_id="collection-worker",
            now=NOW,
            lease_duration=timedelta(seconds=30),
            max_attempts=3,
        )
        service = IncidentCollectionService(
            self.incidents,
            CollectorOrchestrator(
                [CollectorSpec("prometheus", StaticProvider())]
            ),
            clock=lambda: NOW + timedelta(seconds=2),
        )
        service.collect_claimed_incident(
            claim,
            work_repository=self.collection_work,
            scope=ResourceScope(
                namespace="online-boutique",
                resource_names=("frontend",),
                max_items=8,
            ),
            observed_at=NOW + timedelta(seconds=1),
        )
        self.incident = self.incidents.get(incident["incident_id"])
        self.work = InMemoryIncidentLocalizationWorkRepository(self.incidents)
        self.work.enqueue(
            self.incident["incident_id"],
            available_at=NOW + timedelta(seconds=1),
        )

    def claim(self, *, worker_id: str = "worker-a", now=NOW + timedelta(seconds=1)):
        return self.work.claim_next(
            worker_id=worker_id,
            now=now,
            lease_duration=timedelta(seconds=30),
            max_attempts=3,
        )

    def test_claim_is_exclusive_and_fenced(self) -> None:
        stale = self.claim()
        self.assertIsNone(self.claim(worker_id="worker-b"))

        reclaimed = self.claim(
            worker_id="worker-b",
            now=NOW + timedelta(seconds=32),
        )

        self.assertEqual(reclaimed.attempt_count, 2)
        with self.assertRaisesRegex(InvalidTransition, "stale"):
            self.work.fail(
                stale,
                now=NOW + timedelta(seconds=33),
                error_code="STALE_WORKER",
            )

    def _advance_to_reported(self, claim):
        localized_at = claim.lease_expires_at - timedelta(seconds=29)
        self.incidents.transition(
            claim.incident_id, expected_status="LOCALIZING", next_status="ANALYZING",
            occurred_at=localized_at,
        )
        return self.incidents.transition(
            claim.incident_id, expected_status="ANALYZING", next_status="REPORTED",
            occurred_at=localized_at + timedelta(seconds=1),
        )

    def test_localization_complete_after_reported(self) -> None:
        claim = self.claim()
        reported = self._advance_to_reported(claim)

        self.work.complete(claim, now=NOW + timedelta(seconds=4), outcome="SUCCEEDED")

        item = self.work._items[claim.incident_id]
        self.assertEqual(item["state"], "SUCCEEDED")
        self.assertIsNone(item["lease_expires_at"])
        self.assertIsNone(item.get("error_code"))
        self.assertEqual(self.incidents.get(claim.incident_id), reported)

    def test_localization_reaper_after_reported(self) -> None:
        claim = self.claim()
        reported = self._advance_to_reported(claim)

        reaped = self.work.reap_exhausted(now=NOW + timedelta(seconds=32), max_attempts=3)

        self.assertEqual(reaped, 1)
        item = self.work._items[claim.incident_id]
        self.assertEqual(item["state"], "SUCCEEDED")
        self.assertIsNone(item["lease_expires_at"])
        self.assertIsNone(item.get("error_code"))
        self.assertEqual(self.incidents.get(claim.incident_id), reported)
        self.assertEqual(self.work.reap_exhausted(now=NOW + timedelta(seconds=33), max_attempts=3), 0)

    def test_reported_localization_with_unexpired_lease_is_not_reaped(self) -> None:
        claim = self.claim()
        self._advance_to_reported(claim)

        self.assertEqual(self.work.reap_exhausted(now=NOW + timedelta(seconds=30), max_attempts=3), 0)
        self.assertEqual(self.work._items[claim.incident_id]["state"], "RUNNING")
        self.assertEqual(self.work._items[claim.incident_id]["lease_expires_at"], claim.lease_expires_at)

    def test_stale_localization_claim_cannot_complete_reported_incident(self) -> None:
        stale = self.claim()
        current = self.claim(worker_id="worker-b", now=NOW + timedelta(seconds=32))
        self._advance_to_reported(current)
        with self.assertRaisesRegex(InvalidTransition, "stale"):
            self.work.complete(stale, now=NOW + timedelta(seconds=33), outcome="SUCCEEDED")
        self.work.complete(current, now=NOW + timedelta(seconds=34), outcome="SUCCEEDED")
        with self.assertRaisesRegex(InvalidTransition, "stale"):
            self.work.complete(current, now=NOW + timedelta(seconds=35), outcome="SUCCEEDED")

    def test_localization_reaper_preserves_failed_outcome(self) -> None:
        claim = self.claim()
        self.incidents.transition(claim.incident_id, expected_status="LOCALIZING", next_status="FAILED", occurred_at=NOW + timedelta(seconds=2))
        self.assertEqual(self.work.reap_exhausted(now=NOW + timedelta(seconds=32), max_attempts=3), 1)
        self.assertEqual(self.work._items[claim.incident_id]["state"], "FAILED")
        self.assertEqual(self.work._items[claim.incident_id]["error_code"], "INCIDENT_ALREADY_FAILED")
        self.assertEqual(self.incidents.get(claim.incident_id)["status"], "FAILED")

    def test_exhausted_localization_claim_still_fails(self) -> None:
        claim = self.claim()
        self.assertEqual(self.work.reap_exhausted(now=NOW + timedelta(seconds=32), max_attempts=1), 1)
        self.assertEqual(self.work._items[claim.incident_id]["state"], "FAILED")
        self.assertEqual(self.work._items[claim.incident_id]["error_code"], "LEASE_ATTEMPTS_EXHAUSTED")
        self.assertEqual(self.incidents.get(claim.incident_id)["status"], "FAILED")

    def test_partial_localization_is_not_assumed_successful(self) -> None:
        claim = self.claim()
        self.incidents.transition(claim.incident_id, expected_status="LOCALIZING", next_status="PARTIAL", occurred_at=NOW + timedelta(seconds=2))
        with self.assertRaises(InvalidTransition):
            self.work.complete(claim, now=NOW + timedelta(seconds=3), outcome="SUCCEEDED")
        self.assertEqual(self.work.reap_exhausted(now=NOW + timedelta(seconds=32), max_attempts=1), 0)
        self.assertEqual(self.work._items[claim.incident_id]["state"], "RUNNING")
        self.assertEqual(self.incidents.get(claim.incident_id)["status"], "PARTIAL")

    def test_reaper_recovers_committed_localization(self) -> None:
        claim = self.claim()
        self.incidents.transition(
            claim.incident_id,
            expected_status="LOCALIZING",
            next_status="ANALYZING",
            occurred_at=NOW + timedelta(seconds=2),
        )

        reaped = self.work.reap_exhausted(
            now=NOW + timedelta(seconds=32),
            max_attempts=3,
        )
        collection_reaped = self.collection_work.reap_exhausted(
            now=NOW + timedelta(seconds=32),
            max_attempts=3,
        )

        self.assertEqual(reaped, 1)
        self.assertEqual(collection_reaped, 1)
        self.assertEqual(
            self.incidents.get(claim.incident_id)["status"], "ANALYZING"
        )


class IncidentAnalysisWorkClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.incidents, self.incident_id, self.context_id = prepared_repository()
        self.work = InMemoryIncidentAnalysisWorkRepository(self.incidents)
        self.work.enqueue(
            self.incident_id,
            context_id=self.context_id,
            available_at=NOW,
        )

    def claim(self, *, worker_id: str = "worker-a", now: datetime = NOW):
        return self.work.claim_next(
            worker_id=worker_id,
            now=now,
            lease_duration=timedelta(seconds=30),
            max_attempts=3,
        )

    def test_claim_is_exclusive_context_pinned_and_fenced(self) -> None:
        stale = self.claim()

        self.assertEqual(stale.context_id, self.context_id)
        self.assertIsNone(self.claim(worker_id="worker-b"))
        reclaimed = self.claim(
            worker_id="worker-b",
            now=NOW + timedelta(seconds=31),
        )

        self.assertEqual(reclaimed.context_id, self.context_id)
        self.assertEqual(reclaimed.attempt_count, 2)
        with self.assertRaisesRegex(InvalidTransition, "stale"):
            self.work.fail(
                stale,
                now=NOW + timedelta(seconds=32),
                error_code="STALE_WORKER",
            )

    def test_targeted_claim_never_falls_back_to_another_incident(self) -> None:
        self.assertIsNone(
            self.work.claim_incident(
                "inc-does-not-exist",
                worker_id="target-worker",
                now=NOW,
                lease_duration=timedelta(seconds=30),
                max_attempts=3,
            )
        )

        claim = self.work.claim_incident(
            self.incident_id,
            worker_id="target-worker",
            now=NOW,
            lease_duration=timedelta(seconds=30),
            max_attempts=3,
        )

        self.assertIsNotNone(claim)
        self.assertEqual(claim.incident_id, self.incident_id)
        self.assertEqual(claim.context_id, self.context_id)

    def test_continuous_claim_requires_opt_in_and_activation(self) -> None:
        self.assertIsNone(
            self.work.claim_next_eligible(
                worker_id="continuous-worker",
                now=NOW,
                lease_duration=timedelta(seconds=30),
                max_attempts=3,
                eligibility_label="agent_rca_enabled",
                activated_at=datetime(2026, 8, 1, tzinfo=UTC),
            )
        )

        incidents, incident_id, context_id = prepared_repository(
            agent_enabled=True
        )
        opted_in_work = InMemoryIncidentAnalysisWorkRepository(incidents)
        opted_in_work.enqueue(
            incident_id,
            context_id=context_id,
            available_at=NOW,
        )
        self.assertIsNone(
            opted_in_work.claim_next_eligible(
                worker_id="continuous-worker",
                now=NOW,
                lease_duration=timedelta(seconds=30),
                max_attempts=3,
                eligibility_label="agent_rca_enabled",
                activated_at=datetime(2026, 8, 23, tzinfo=UTC),
            )
        )
        claim = opted_in_work.claim_next_eligible(
            worker_id="continuous-worker",
            now=NOW,
            lease_duration=timedelta(seconds=30),
            max_attempts=3,
            eligibility_label="agent_rca_enabled",
            activated_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
        self.assertIsNotNone(claim)
        self.assertEqual(claim.incident_id, incident_id)

    def test_reported_incident_completes_analysis_work(self) -> None:
        claim = self.claim()
        self.incidents.transition(
            claim.incident_id,
            expected_status="ANALYZING",
            next_status="REPORTED",
            occurred_at=NOW + timedelta(seconds=1),
        )

        self.work.complete(
            claim,
            now=NOW + timedelta(seconds=2),
            outcome="SUCCEEDED",
        )

        self.assertIsNone(self.claim(worker_id="worker-b"))

    def test_exhausted_analysis_claim_fails_closed(self) -> None:
        self.claim(now=NOW)
        self.claim(worker_id="worker-b", now=NOW + timedelta(seconds=31))
        self.claim(worker_id="worker-c", now=NOW + timedelta(seconds=62))

        reaped = self.work.reap_exhausted(
            now=NOW + timedelta(seconds=93),
            max_attempts=3,
        )

        self.assertEqual(reaped, 1)
        self.assertEqual(self.incidents.get(self.incident_id)["status"], "FAILED")

    def test_reaper_recovers_when_report_committed_before_work_completion(self) -> None:
        self.claim()
        self.incidents.transition(
            self.incident_id,
            expected_status="ANALYZING",
            next_status="REPORTED",
            occurred_at=NOW + timedelta(seconds=1),
        )

        reaped = self.work.reap_exhausted(
            now=NOW + timedelta(seconds=31),
            max_attempts=3,
        )

        self.assertEqual(reaped, 1)
        self.assertEqual(self.incidents.get(self.incident_id)["status"], "REPORTED")


if __name__ == "__main__":
    unittest.main()
