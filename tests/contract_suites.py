"""Reusable behavioral contracts for future PostgreSQL and provider adapters."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceWindow,
    ResourceScope,
    validate_provider_batch,
)


FIXED_TIME = datetime(2026, 8, 12, 1, 5, tzinfo=timezone.utc)


class IncidentRepositoryContract:
    """One suite that every IncidentRepository adapter must pass."""

    @staticmethod
    def verify(test_case, repository_factory: Callable, incident: dict, evidence: dict):
        repository = repository_factory()

        def create_once(_: int):
            return repository.create_or_get_by_deduplication_key(
                copy.deepcopy(incident), occurred_at=FIXED_TIME
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(create_once, range(24)))
        test_case.assertEqual(sum(result.created for result in results), 1)
        test_case.assertEqual(
            len({result.incident["incident_id"] for result in results}), 1
        )

        incident_id = incident["incident_id"]
        repository.transition(
            incident_id,
            expected_status="RECEIVED",
            next_status="COLLECTING",
            occurred_at=FIXED_TIME,
        )
        repository.replace_collector_statuses(
            incident_id,
            [
                {
                    "collector": "kubernetes",
                    "status": "SUCCEEDED",
                    "attempts": 1,
                    "started_at": "2026-08-12T01:05:00Z",
                    "ended_at": "2026-08-12T01:05:01Z",
                    "error": None,
                }
            ],
            occurred_at=FIXED_TIME,
        )
        repository.store_evidence(incident_id, [evidence])
        repository.store_evidence(incident_id, [evidence])
        test_case.assertEqual(repository.list_evidence(incident_id), [evidence])


class ProviderAdapterContract:
    """Minimum behavior every Metrics/Logs/Kubernetes adapter must satisfy."""

    @staticmethod
    def verify(test_case, provider, request: CollectionRequest):
        batch = provider.collect(request)
        validate_provider_batch(batch, request)
        builder = EvidenceBuilder()
        built = [
            builder.build(draft, request, collected_at=FIXED_TIME)
            for draft in batch.items
        ]
        test_case.assertTrue(built)
        for evidence in built:
            test_case.assertEqual(
                evidence["subject"]["namespace"], request.scope.namespace
            )
            test_case.assertIn(
                evidence["subject"]["name"], request.scope.resource_names
            )
            test_case.assertTrue(evidence["provenance"]["provider"])
            test_case.assertTrue(evidence["provenance"]["query"])
            test_case.assertTrue(evidence["provenance"]["locator"])


def contract_request(incident_id: str, resource_name: str) -> CollectionRequest:
    return CollectionRequest(
        request_id="req-contract-00000001",
        incident_id=incident_id,
        window=EvidenceWindow(
            start="2026-08-12T00:30:00Z",
            end="2026-08-12T01:05:00Z",
        ),
        scope=ResourceScope(
            namespace="online-boutique",
            resource_names=(resource_name,),
        ),
        timeout_seconds=1.0,
    )
