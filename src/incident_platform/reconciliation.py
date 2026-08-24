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
    parse_time,
    validate_provider_batch,
)
from .projectors import KubernetesEvidenceProjector
from .stategraph import (
    StateGraphReconciliationRepository,
    StateGraphReconciliationResult,
    StateGraphReconciliationScope,
    stable_graph_id,
)
from .stategraph_observations import (
    StateGraphObservationCycle,
    StateGraphObservationRepository,
    validate_cycle_evidence,
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
    cycle: StateGraphObservationCycle


class KubernetesStateGraphReconciler:
    """Project only complete bounded inventories and retire disappeared state."""

    def __init__(
        self,
        provider: EvidenceProvider,
        repository: StateGraphReconciliationRepository,
        observation_repository: StateGraphObservationRepository,
        *,
        cluster_id: str,
        evidence_builder: EvidenceBuilder | None = None,
        projector: KubernetesEvidenceProjector | None = None,
    ) -> None:
        if not cluster_id.strip():
            raise ValueError("Kubernetes reconciliation cluster_id is required")
        self._provider = provider
        self._repository = repository
        self._observation_repository = observation_repository
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
        scope = StateGraphReconciliationScope(
            cluster_id=self._cluster_id,
            namespace=request.scope.namespace,
            resource_names=request.scope.resource_names,
            resource_name_prefixes=request.scope.resource_name_prefixes,
            projector=self._projector.projector_name,
            managed_entity_types=KUBERNETES_RECONCILED_ENTITY_TYPES,
            managed_relation_types=KUBERNETES_RECONCILED_RELATION_TYPES,
        )
        cycle_id = stable_graph_id(
            "cycle",
            {
                "request_id": request.request_id,
                "evidence_scope_id": request.incident_id,
                "cluster_id": self._cluster_id,
                "namespace": request.scope.namespace,
                "resource_names": request.scope.resource_names,
                "resource_name_prefixes": (
                    request.scope.resource_name_prefixes
                ),
                "observed_at": request.window.end,
                "projector": scope.projector,
                "managed_entity_types": scope.managed_entity_types,
                "managed_relation_types": scope.managed_relation_types,
            },
        )
        try:
            persisted_cycle = self._observation_repository.get_cycle(cycle_id)
        except KeyError:
            persisted_cycle = None
        if persisted_cycle is not None:
            self._validate_persisted_cycle(persisted_cycle, request)
            evidence = validate_cycle_evidence(
                persisted_cycle,
                self._observation_repository.list_cycle_evidence(cycle_id),
            )
            records = self._project_evidence(evidence)
            if persisted_cycle.status == "APPLIED":
                if persisted_cycle.result is None:
                    raise ContractViolation(
                        "APPLIED StateGraph observation is missing its result"
                    )
                return KubernetesStateGraphReconciliationRun(
                    evidence=evidence,
                    projected_record_count=len(records),
                    result=persisted_cycle.result,
                    cycle=persisted_cycle,
                )
            result = self._repository.reconcile_projection(
                records,
                scope=scope,
                observed_at=parse_time(
                    persisted_cycle.staged_at,
                    "ObservationCycle.staged_at",
                ),
            )
            applied_cycle = self._observation_repository.mark_cycle_applied(
                cycle_id,
                result,
                applied_at=collected_at,
            )
            return KubernetesStateGraphReconciliationRun(
                evidence=evidence,
                projected_record_count=len(records),
                result=result,
                cycle=applied_cycle,
            )

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
        records = self._project_evidence(evidence)
        staged_cycle = StateGraphObservationCycle(
            cycle_id=cycle_id,
            request_id=request.request_id,
            evidence_scope_id=request.incident_id,
            cluster_id=self._cluster_id,
            namespace=request.scope.namespace,
            observed_at=request.window.end,
            staged_at=format_time(collected_at),
            status="STAGED",
            evidence_ids=tuple(sorted(item["evidence_id"] for item in evidence)),
        )
        persisted_cycle = self._observation_repository.stage_cycle(
            staged_cycle,
            evidence,
        )
        if persisted_cycle.status == "APPLIED":
            if persisted_cycle.result is None:
                raise ContractViolation(
                    "APPLIED StateGraph observation is missing its result"
                )
            return KubernetesStateGraphReconciliationRun(
                evidence=evidence,
                projected_record_count=len(records),
                result=persisted_cycle.result,
                cycle=persisted_cycle,
            )
        result = self._repository.reconcile_projection(
            records,
            scope=scope,
            observed_at=collected_at,
        )
        applied_cycle = self._observation_repository.mark_cycle_applied(
            cycle_id,
            result,
            applied_at=collected_at,
        )
        return KubernetesStateGraphReconciliationRun(
            evidence=evidence,
            projected_record_count=len(records),
            result=result,
            cycle=applied_cycle,
        )

    def _project_evidence(
        self,
        evidence: Tuple[Mapping[str, Any], ...],
    ) -> Tuple[Mapping[str, Any], ...]:
        records = []
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
            records.extend(self._projector.project(item).records)
        return tuple(records)

    def _validate_persisted_cycle(
        self,
        cycle: StateGraphObservationCycle,
        request: CollectionRequest,
    ) -> None:
        expected = (
            request.request_id,
            request.incident_id,
            self._cluster_id,
            request.scope.namespace,
            request.window.end,
        )
        actual = (
            cycle.request_id,
            cycle.evidence_scope_id,
            cycle.cluster_id,
            cycle.namespace,
            cycle.observed_at,
        )
        if actual != expected:
            raise ContractViolation(
                "persisted StateGraph observation does not match its request"
            )
