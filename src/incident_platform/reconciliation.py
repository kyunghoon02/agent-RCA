"""Fail-closed complete-set projection into the temporal StateGraph."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Tuple

from .collectors import EvidenceProvider
from .errors import ContractViolation
from .evidence import (
    CollectionRequest,
    EvidenceBuilder,
    format_time,
    validate_provider_batch,
)
from .projectors import KubernetesEvidenceProjector
from .stategraph import (
    StateGraphReconciliationRepository,
    StateGraphReconciliationResult,
    StateGraphReconciliationScope,
)


KUBERNETES_RECONCILED_ENTITY_TYPES = (
    "Service",
    "Deployment",
    "ReplicaSet",
    "Pod",
    "EndpointSlice",
)
KUBERNETES_RECONCILED_RELATION_TYPES = (
    "REPRESENTED_BY",
    "OWNS",
    "SELECTS",
    "ROUTES_TO",
    "SCHEDULED_ON",
)


@dataclass(frozen=True)
class KubernetesStateGraphReconciliationRun:
    """One complete inventory cycle and its atomic Graph mutation summary."""

    evidence: Tuple[Mapping[str, Any], ...]
    projected_record_count: int
    result: StateGraphReconciliationResult


class KubernetesStateGraphReconciler:
    """Project only complete bounded inventories and retire disappeared state."""

    def __init__(
        self,
        provider: EvidenceProvider,
        repository: StateGraphReconciliationRepository,
        *,
        cluster_id: str,
        evidence_builder: EvidenceBuilder | None = None,
        projector: KubernetesEvidenceProjector | None = None,
    ) -> None:
        if not cluster_id.strip():
            raise ValueError("Kubernetes reconciliation cluster_id is required")
        self._provider = provider
        self._repository = repository
        self._cluster_id = cluster_id
        self._evidence_builder = evidence_builder or EvidenceBuilder()
        self._projector = projector or KubernetesEvidenceProjector()

    def reconcile(
        self,
        request: CollectionRequest,
        *,
        collected_at: datetime,
    ) -> KubernetesStateGraphReconciliationRun:
        format_time(collected_at)
        batch = self._provider.collect(request)
        validate_provider_batch(batch, request)
        if batch.status != "SUCCEEDED":
            raise ContractViolation(
                "StateGraph reconciliation requires a complete ProviderBatch"
            )
        if not batch.items:
            raise ContractViolation(
                "StateGraph reconciliation requires a non-empty ProviderBatch"
            )

        evidence = tuple(
            self._evidence_builder.build(
                draft,
                request,
                collected_at=collected_at,
            )
            for draft in batch.items
        )
        for item in evidence:
            subject = item["subject"]
            if (
                item["source"] != "kubernetes"
                or item["kind"] != "resource-state"
                or subject.get("cluster_id") != self._cluster_id
            ):
                raise ContractViolation(
                    "StateGraph reconciliation accepts only trusted-cluster "
                    "Kubernetes resource-state Evidence"
                )

        records = tuple(
            record
            for item in evidence
            for record in self._projector.project(item).records
        )
        scope = StateGraphReconciliationScope(
            cluster_id=self._cluster_id,
            namespace=request.scope.namespace,
            resource_names=request.scope.resource_names,
            resource_name_prefixes=request.scope.resource_name_prefixes,
            projector=self._projector.projector_name,
            managed_entity_types=KUBERNETES_RECONCILED_ENTITY_TYPES,
            managed_relation_types=KUBERNETES_RECONCILED_RELATION_TYPES,
        )
        result = self._repository.reconcile_projection(
            records,
            scope=scope,
            observed_at=collected_at,
        )
        return KubernetesStateGraphReconciliationRun(
            evidence=evidence,
            projected_record_count=len(records),
            result=result,
        )
