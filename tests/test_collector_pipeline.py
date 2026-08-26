from __future__ import annotations

import copy
import json
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from incident_platform.collectors import (
    COLLECTOR_NAMES,
    CollectorOrchestrator,
    CollectorSpec,
    IncidentCollectionService,
)
from incident_platform.contracts import validate_contract
from incident_platform.errors import (
    ContractViolation,
    PermanentProviderError,
    RetryableProviderError,
)
from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceDraft,
    EvidenceWindow,
    ProviderBatch,
    ResourceScope,
    validate_provider_batch,
    verify_evidence_content_hash,
)
from incident_platform.incidents import AlertmanagerIngestionService
from incident_platform.repository import InMemoryIncidentRepository

from contract_suites import (
    FIXED_TIME,
    IncidentRepositoryContract,
    ProviderAdapterContract,
    contract_request,
)


INCIDENT_ID = "inc-collector-fixture-0001"
SUBJECT = {
    "api_version": "v1",
    "kind": "Service",
    "namespace": "online-boutique",
    "name": "checkoutservice",
    "uid": "7df6d266-40df-4fd6-942d-7ebc864c4061",
    "exists": True,
}


def evidence_draft(
    *,
    source: str = "prometheus",
    kind: str = "metric-summary",
    summary: str = "Checkout error ratio increased.",
    facts=None,
    subject=None,
) -> EvidenceDraft:
    return EvidenceDraft(
        source=source,
        kind=kind,
        observed_at="2026-08-12T01:04:59Z",
        subject=subject or SUBJECT,
        summary=summary,
        facts=facts or {"metric": "request_error_ratio", "peak_ratio": 0.42},
        provider=f"{source}-fixture-provider",
        query="scoped-query namespace=online-boutique service=checkoutservice",
        locator=f"{source}://online-boutique/checkoutservice",
    )


class StaticProvider:
    def __init__(
        self,
        batch: ProviderBatch,
        *,
        retryable_failures: int = 0,
        permanent_error: str = "",
        delay_seconds: float = 0,
        barrier=None,
    ) -> None:
        self.batch = batch
        self.retryable_failures = retryable_failures
        self.permanent_error = permanent_error
        self.delay_seconds = delay_seconds
        self.barrier = barrier
        self.calls = 0
        self._lock = threading.Lock()

    def collect(self, request: CollectionRequest) -> ProviderBatch:
        with self._lock:
            self.calls += 1
            call = self.calls
        if self.barrier is not None:
            self.barrier.wait(timeout=0.5)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.permanent_error:
            raise PermanentProviderError(self.permanent_error)
        if call <= self.retryable_failures:
            raise RetryableProviderError("temporary provider outage")
        return self.batch


class RequestRecordingProvider:
    def __init__(self) -> None:
        self.requests = []

    def collect(self, request: CollectionRequest) -> ProviderBatch:
        self.requests.append(request)
        return ProviderBatch(
            items=(
                evidence_draft(
                    subject={
                        **SUBJECT,
                        "name": request.scope.resource_names[0],
                    }
                ),
            )
        )


def collection_window() -> EvidenceWindow:
    return EvidenceWindow(
        start="2026-08-12T00:30:00Z",
        end="2026-08-12T01:05:00Z",
    )


def collection_scope() -> ResourceScope:
    return ResourceScope(
        namespace="online-boutique",
        resource_names=("checkoutservice",),
    )


class CollectorOrchestratorTests(unittest.TestCase):
    def test_runtime_collector_allowlist_matches_incident_contract(self) -> None:
        schema_path = (
            Path(__file__).parents[1]
            / "contracts"
            / "schemas"
            / "incident.schema.json"
        )
        with schema_path.open(encoding="utf-8") as handle:
            schema = json.load(handle)
        contract_names = set(
            schema["properties"]["collector_statuses"]["items"]["properties"]
            ["collector"]["enum"]
        )

        self.assertEqual(contract_names, set(COLLECTOR_NAMES))

    def test_collector_can_use_a_narrower_window_and_trusted_profile_scope(self) -> None:
        provider = RequestRecordingProvider()
        profile_scope = ResourceScope(
            namespace="online-boutique",
            resource_names=("frontend", "checkoutservice"),
            max_items=10,
        )
        run = CollectorOrchestrator(
            [
                CollectorSpec(
                    "prometheus-api",
                    provider,
                    request_scope=profile_scope,
                    lookback_seconds=900,
                )
            ]
        ).collect(
            incident_id=INCIDENT_ID,
            window=collection_window(),
            scope=collection_scope(),
            observed_at=FIXED_TIME,
        )

        self.assertEqual(run.status, "SUCCEEDED")
        self.assertEqual(provider.requests[0].scope, profile_scope)
        self.assertEqual(provider.requests[0].window.start, "2026-08-12T00:50:00Z")
        self.assertEqual(run.evidence[0]["subject"]["name"], "frontend")

    def test_collectors_reach_a_barrier_concurrently(self) -> None:
        barrier = threading.Barrier(2)
        metrics = StaticProvider(
            ProviderBatch(items=(evidence_draft(),)), barrier=barrier
        )
        logs = StaticProvider(
            ProviderBatch(
                items=(evidence_draft(source="logs", kind="log-pattern"),)
            ),
            barrier=barrier,
        )
        orchestrator = CollectorOrchestrator(
            [
                CollectorSpec("prometheus", metrics, timeout_seconds=1),
                CollectorSpec("logs", logs, timeout_seconds=1),
            ]
        )

        run = orchestrator.collect(
            incident_id=INCIDENT_ID,
            window=collection_window(),
            scope=collection_scope(),
            observed_at=FIXED_TIME,
        )

        self.assertEqual(run.status, "SUCCEEDED")
        self.assertEqual(len(run.evidence), 2)
        self.assertEqual(metrics.calls, 1)
        self.assertEqual(logs.calls, 1)

    def test_retryable_failure_is_retried_within_attempt_budget(self) -> None:
        provider = StaticProvider(
            ProviderBatch(items=(evidence_draft(),)), retryable_failures=1
        )
        orchestrator = CollectorOrchestrator(
            [CollectorSpec("prometheus", provider, max_attempts=2)]
        )

        run = orchestrator.collect(
            incident_id=INCIDENT_ID,
            window=collection_window(),
            scope=collection_scope(),
            observed_at=FIXED_TIME,
        )

        self.assertEqual(run.status, "SUCCEEDED")
        self.assertEqual(run.executions[0].attempts, 2)
        self.assertEqual(provider.calls, 2)

    def test_timeout_and_provider_failure_do_not_discard_successful_evidence(self) -> None:
        metrics = StaticProvider(ProviderBatch(items=(evidence_draft(),)))
        logs = StaticProvider(ProviderBatch(), permanent_error="invalid log query")
        kubernetes = StaticProvider(
            ProviderBatch(
                items=(evidence_draft(source="kubernetes", kind="resource-state"),)
            ),
            delay_seconds=0.15,
        )
        orchestrator = CollectorOrchestrator(
            [
                CollectorSpec("prometheus", metrics, timeout_seconds=0.2),
                CollectorSpec("logs", logs, timeout_seconds=0.2),
                CollectorSpec("kubernetes", kubernetes, timeout_seconds=0.03),
            ]
        )

        run = orchestrator.collect(
            incident_id=INCIDENT_ID,
            window=collection_window(),
            scope=collection_scope(),
            observed_at=FIXED_TIME,
        )

        self.assertEqual(run.status, "PARTIAL")
        self.assertEqual(len(run.evidence), 1)
        statuses = {item.name: item.status for item in run.executions}
        self.assertEqual(statuses["prometheus"], "SUCCEEDED")
        self.assertEqual(statuses["logs"], "FAILED")
        self.assertEqual(statuses["kubernetes"], "TIMED_OUT")

    def test_all_failed_collectors_return_failed_run(self) -> None:
        provider = StaticProvider(ProviderBatch(), permanent_error="provider denied")
        run = CollectorOrchestrator(
            [CollectorSpec("logs", provider, max_attempts=3)]
        ).collect(
            incident_id=INCIDENT_ID,
            window=collection_window(),
            scope=collection_scope(),
            observed_at=FIXED_TIME,
        )

        self.assertEqual(run.status, "FAILED")
        self.assertEqual(run.executions[0].attempts, 1)
        self.assertFalse(run.evidence)

    def test_provider_partial_batch_keeps_items_and_marks_partial(self) -> None:
        provider = StaticProvider(
            ProviderBatch(
                items=(evidence_draft(source="logs", kind="log-pattern"),),
                status="PARTIAL",
                error="result page limit reached",
            )
        )
        run = CollectorOrchestrator(
            [CollectorSpec("logs", provider)]
        ).collect(
            incident_id=INCIDENT_ID,
            window=collection_window(),
            scope=collection_scope(),
            observed_at=FIXED_TIME,
        )

        self.assertEqual(run.status, "PARTIAL")
        self.assertEqual(run.executions[0].status, "PARTIAL")
        self.assertEqual(len(run.evidence), 1)


class EvidenceTests(unittest.TestCase):
    def test_sensitive_values_are_redacted_before_hash_and_storage(self) -> None:
        request = contract_request(INCIDENT_ID, "checkoutservice")
        draft = evidence_draft(
            source="logs",
            kind="log-pattern",
            summary="request failed Authorization: Bearer abc.def.ghi",
            facts={
                "password": "plain-text-password",
                "message": "token=secret-token user request failed",
                "nested": {"api_key": "private-api-key"},
            },
        )

        evidence = EvidenceBuilder().build(
            draft, request, collected_at=FIXED_TIME
        )
        serialized = str(evidence)

        self.assertNotIn("plain-text-password", serialized)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("private-api-key", serialized)
        self.assertGreaterEqual(len(evidence["redactions"]), 4)
        self.assertTrue(verify_evidence_content_hash(evidence))
        validate_contract("evidence-item.schema.json", evidence)

        tampered = copy.deepcopy(evidence)
        tampered["facts"]["message"] = "changed"
        self.assertFalse(verify_evidence_content_hash(tampered))

    def test_provider_output_outside_scope_is_rejected(self) -> None:
        request = contract_request(INCIDENT_ID, "checkoutservice")
        wrong_subject = dict(SUBJECT, namespace="other-namespace")
        batch = ProviderBatch(items=(evidence_draft(subject=wrong_subject),))

        with self.assertRaisesRegex(ContractViolation, "outside namespace"):
            validate_provider_batch(batch, request)

    def test_evidence_outside_time_window_is_rejected(self) -> None:
        request = contract_request(INCIDENT_ID, "checkoutservice")
        draft = replace(
            evidence_draft(), observed_at="2026-08-12T02:00:00Z"
        )

        with self.assertRaisesRegex(ContractViolation, "outside the requested"):
            EvidenceBuilder().build(draft, request, collected_at=FIXED_TIME)


class IncidentCollectionServiceTests(unittest.TestCase):
    def test_partial_collection_is_persisted_and_lifecycle_continues(self) -> None:
        repository = InMemoryIncidentRepository()
        incident = AlertmanagerIngestionService(repository).ingest(
            {
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {
                            "alertname": "CheckoutHighErrorRate",
                            "namespace": "online-boutique",
                            "service": "checkoutservice",
                            "severity": "critical",
                        },
                        "annotations": {},
                        "startsAt": "2026-08-12T01:00:00Z",
                        "endsAt": "0001-01-01T00:00:00Z",
                        "fingerprint": "collector-pipeline-01",
                    }
                ]
            },
            received_at=FIXED_TIME,
        )[0].incident
        metrics = StaticProvider(ProviderBatch(items=(evidence_draft(),)))
        logs = StaticProvider(ProviderBatch(), permanent_error="log backend unavailable")
        service = IncidentCollectionService(
            repository,
            CollectorOrchestrator(
                [CollectorSpec("prometheus", metrics), CollectorSpec("logs", logs)]
            ),
        )

        run = service.collect_incident(
            incident["incident_id"],
            scope=collection_scope(),
            observed_at=datetime(2026, 8, 12, 1, 5, 10, tzinfo=timezone.utc),
        )

        stored = repository.get(incident["incident_id"])
        self.assertEqual(run.status, "PARTIAL")
        self.assertEqual(stored["status"], "LOCALIZING")
        self.assertEqual(len(stored["collector_statuses"]), 2)
        self.assertEqual(len(repository.list_evidence(incident["incident_id"])), 1)


class AdapterContractTests(unittest.TestCase):
    def test_in_memory_repository_passes_reusable_repository_contract(self) -> None:
        repository = InMemoryIncidentRepository()
        incident = AlertmanagerIngestionService(repository).ingest(
            {
                "alerts": [
                    {
                        "status": "firing",
                        "labels": {
                            "alertname": "ContractTest",
                            "namespace": "online-boutique",
                            "service": "checkoutservice",
                            "severity": "warning",
                        },
                        "annotations": {},
                        "startsAt": "2026-08-12T01:00:00Z",
                        "endsAt": "0001-01-01T00:00:00Z",
                        "fingerprint": "repository-contract-01",
                    }
                ]
            },
            received_at=FIXED_TIME,
        )[0].incident
        # Use a fresh repository inside the reusable contract.
        request = contract_request(incident["incident_id"], "checkoutservice")
        evidence = EvidenceBuilder().build(
            evidence_draft(), request, collected_at=FIXED_TIME
        )
        IncidentRepositoryContract.verify(
            self, InMemoryIncidentRepository, incident, evidence
        )

    def test_fixture_provider_passes_reusable_provider_contract(self) -> None:
        provider = StaticProvider(ProviderBatch(items=(evidence_draft(),)))
        ProviderAdapterContract.verify(
            self,
            provider,
            contract_request(INCIDENT_ID, "checkoutservice"),
        )


if __name__ == "__main__":
    unittest.main()
