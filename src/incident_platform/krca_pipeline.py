"""Evidence-to-KRCA-to-StateGraph orchestration without raw telemetry access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence, Tuple

from .contracts import validate_contract
from .errors import ContractViolation
from .evidence import EvidenceWindow
from .krca import APIEdgeSignal, APIRef, KRCADrilldownLocalizer, KRCADrilldownRun
from .localization import IncidentLocalizationRun, IncidentLocalizationService
from .resolution import (
    EntityResolutionRequest,
    EntityResolutionResult,
    ServiceToEntityResolver,
)
from .stategraph import InvestigationScope


class APIEdgeEvidenceProjector:
    """Translate one normalized feature Evidence item into an APIEdgeSignal."""

    feature_set = "krca-api-edge-v1"

    def supports(self, evidence: Mapping[str, Any]) -> bool:
        facts = evidence.get("facts")
        return (
            evidence.get("source") == "prometheus"
            and evidence.get("kind") == "metric-summary"
            and isinstance(facts, Mapping)
            and facts.get("feature_set") == self.feature_set
        )

    def project(self, evidence: Mapping[str, Any]) -> APIEdgeSignal:
        validate_contract("evidence-item.schema.json", evidence)
        if not self.supports(evidence):
            raise ContractViolation("Evidence is not a KRCA API edge feature")
        facts = evidence["facts"]
        if facts.get("result_status") != "HAS_DATA":
            raise ContractViolation(
                "KRCA API edge Evidence has no complete feature vector"
            )
        parent = self._api_ref(facts.get("parent"), "parent")
        child = self._api_ref(facts.get("child"), "child")
        return APIEdgeSignal(
            parent=parent,
            child=child,
            failure_rate_correlation=self._number(
                facts, "failure_rate_correlation"
            ),
            failure_rate_p_value=self._number(facts, "failure_rate_p_value"),
            latency_anomaly=self._number(facts, "latency_anomaly"),
            latency_fluctuation_contribution=self._number(
                facts, "latency_fluctuation_contribution"
            ),
            latency_correlation=self._number(facts, "latency_correlation"),
            evidence_ids=(evidence["evidence_id"],),
        )

    @staticmethod
    def _api_ref(value: Any, field: str) -> APIRef:
        if not isinstance(value, Mapping):
            raise ContractViolation(f"KRCA feature {field} API is malformed")
        service = value.get("service")
        operation = value.get("operation")
        if not isinstance(service, str) or not isinstance(operation, str):
            raise ContractViolation(f"KRCA feature {field} API is malformed")
        return APIRef(service, operation)

    @staticmethod
    def _number(facts: Mapping[str, Any], field: str) -> float:
        value = facts.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ContractViolation(f"KRCA feature {field} is malformed")
        return float(value)


@dataclass(frozen=True)
class KRCAMetricLocalizationRun:
    incident_id: str
    window: EvidenceWindow
    drilldown: KRCADrilldownRun
    consumed_evidence_ids: Tuple[str, ...]
    unavailable_feature_evidence_ids: Tuple[str, ...]


class EvidenceBackedKRCADrilldownService:
    """Run KRCA only from stored, schema-valid feature Evidence."""

    def __init__(
        self,
        localizer: Optional[KRCADrilldownLocalizer] = None,
        projector: Optional[APIEdgeEvidenceProjector] = None,
    ) -> None:
        self._localizer = localizer or KRCADrilldownLocalizer()
        self._projector = projector or APIEdgeEvidenceProjector()

    def localize(
        self,
        incident_id: str,
        *,
        window: EvidenceWindow,
        alerting_api: APIRef,
        evidence: Sequence[Mapping[str, Any]],
    ) -> KRCAMetricLocalizationRun:
        signals = []
        consumed = []
        unavailable = []
        for item in evidence:
            if not self._projector.supports(item):
                continue
            validate_contract("evidence-item.schema.json", item)
            if item["incident_id"] != incident_id:
                raise ContractViolation(
                    "KRCA feature Evidence belongs to a different Incident"
                )
            if item["window"] != {"start": window.start, "end": window.end}:
                raise ContractViolation(
                    "KRCA feature Evidence uses a different time window"
                )
            if item["facts"].get("result_status") != "HAS_DATA":
                unavailable.append(item["evidence_id"])
                continue
            signals.append(self._projector.project(item))
            consumed.append(item["evidence_id"])
        drilldown = self._localizer.localize(alerting_api, signals)
        return KRCAMetricLocalizationRun(
            incident_id=incident_id,
            window=window,
            drilldown=drilldown,
            consumed_evidence_ids=tuple(dict.fromkeys(consumed)),
            unavailable_feature_evidence_ids=tuple(dict.fromkeys(unavailable)),
        )


@dataclass(frozen=True)
class KRCATopServiceResolutionRun:
    feature_run: KRCAMetricLocalizationRun
    resolutions: Tuple[EntityResolutionResult, ...]
    seed_entity_ids: Tuple[str, ...]
    scope: Optional[InvestigationScope]

    @property
    def complete(self) -> bool:
        return bool(self.resolutions) and all(
            item.status == "RESOLVED" for item in self.resolutions
        )


class KRCATopServiceScopeResolver:
    """Resolve each KRCA Top-N service and build a multi-seed bounded scope."""

    def __init__(self, resolver: ServiceToEntityResolver) -> None:
        self._resolver = resolver

    def resolve(
        self,
        feature_run: KRCAMetricLocalizationRun,
        *,
        cluster_id: str,
        namespace: str,
        max_candidates: int = 10,
        max_entities: int = 100,
        max_depth: int = 4,
    ) -> KRCATopServiceResolutionRun:
        if not cluster_id.strip() or not namespace.strip():
            raise ContractViolation("KRCA seed resolution scope is required")
        resolutions = tuple(
            self._resolver.resolve(
                EntityResolutionRequest(
                    incident_id=feature_run.incident_id,
                    cluster_id=cluster_id,
                    namespace=namespace,
                    service_name=candidate.api.service,
                    window=feature_run.window,
                    max_candidates=max_candidates,
                )
            )
            for candidate in feature_run.drilldown.top_services
        )
        seed_entity_ids = tuple(
            dict.fromkeys(
                entity_id
                for resolution in resolutions
                if resolution.status == "RESOLVED"
                for entity_id in resolution.seed_entity_ids
            )
        )
        complete = bool(resolutions) and all(
            item.status == "RESOLVED" for item in resolutions
        )
        scope = None
        if seed_entity_ids and complete:
            scope = InvestigationScope(
                incident_id=feature_run.incident_id,
                seed_entity_ids=seed_entity_ids,
                window=feature_run.window,
                domains=("web-service", "kubernetes"),
                correlation_keys={
                    "cluster_id": cluster_id,
                    "namespace": namespace,
                    "seed_source": "krca-top-services",
                },
                relation_types=(
                    "REPRESENTED_BY",
                    "RESOLVES_TO",
                    "REFERENCES",
                    "DEPENDS_ON",
                    "CALLS",
                    "ROUTES_TO",
                ),
                max_entities=max_entities,
                max_depth=max_depth,
            )
        return KRCATopServiceResolutionRun(
            feature_run=feature_run,
            resolutions=resolutions,
            seed_entity_ids=seed_entity_ids,
            scope=scope,
        )


@dataclass(frozen=True)
class KRCATopServiceLocalizationRun:
    resolution: KRCATopServiceResolutionRun
    localization: Optional[IncidentLocalizationRun]


class KRCATopServiceLocalizationService:
    """Run Incident localization only when KRCA resolves at least one safe seed."""

    def __init__(
        self,
        scope_resolver: KRCATopServiceScopeResolver,
        localization_service: IncidentLocalizationService,
    ) -> None:
        self._scope_resolver = scope_resolver
        self._localization_service = localization_service

    def localize(
        self,
        feature_run: KRCAMetricLocalizationRun,
        *,
        cluster_id: str,
        namespace: str,
        frozen_at: Optional[datetime] = None,
        max_entities: int = 100,
        max_depth: int = 4,
    ) -> KRCATopServiceLocalizationRun:
        resolution = self._scope_resolver.resolve(
            feature_run,
            cluster_id=cluster_id,
            namespace=namespace,
            max_entities=max_entities,
            max_depth=max_depth,
        )
        if resolution.scope is None:
            return KRCATopServiceLocalizationRun(resolution, None)
        localization = self._localization_service.localize_incident(
            feature_run.incident_id,
            scope=resolution.scope,
            frozen_at=frozen_at,
        )
        return KRCATopServiceLocalizationRun(resolution, localization)
