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
    InvestigationScopeFactory,
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
        profile_id: Optional[str] = None,
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
            correlation_keys = {
                "cluster_id": cluster_id,
                "namespace": namespace,
                "seed_source": "krca-top-services",
                "krca_drilldown_stop": feature_run.drilldown.stop_reason,
            }
            if profile_id is not None:
                correlation_keys["krca_profile"] = profile_id
            scope = InvestigationScope(
                incident_id=feature_run.incident_id,
                seed_entity_ids=seed_entity_ids,
                window=feature_run.window,
                domains=("web-service", "kubernetes"),
                correlation_keys=correlation_keys,
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


@dataclass(frozen=True)
class KRCAGuidedIncidentLocalizationRun:
    """One profiled KRCA decision followed by Top-N or safe source fallback."""

    feature_run: KRCAMetricLocalizationRun
    top_resolution: Optional[KRCATopServiceResolutionRun]
    source_resolution: Optional[EntityResolutionResult]
    localization: Optional[IncidentLocalizationRun]
    seed_source: str
    fallback_reason: Optional[str]


class KRCAGuidedIncidentLocalizationService:
    """Use KRCA Top-N seeds when complete; otherwise retain exact source scope."""

    def __init__(
        self,
        resolver: ServiceToEntityResolver,
        localization_service: IncidentLocalizationService,
        *,
        drilldown_service: Optional[EvidenceBackedKRCADrilldownService] = None,
        scope_factory: Optional[InvestigationScopeFactory] = None,
    ) -> None:
        self._resolver = resolver
        self._localization_service = localization_service
        self._drilldown = drilldown_service or EvidenceBackedKRCADrilldownService()
        self._scope_factory = scope_factory or InvestigationScopeFactory()
        self._top_scope_resolver = KRCATopServiceScopeResolver(resolver)
        self._feature_projector = APIEdgeEvidenceProjector()

    def localize(
        self,
        request: EntityResolutionRequest,
        *,
        profile_id: str,
        alerting_api: APIRef,
        expected_edges: Mapping[str, Tuple[APIRef, APIRef]],
        evidence: Sequence[Mapping[str, Any]],
        frozen_at: Optional[datetime] = None,
        max_entities: int = 100,
        max_depth: int = 4,
    ) -> KRCAGuidedIncidentLocalizationRun:
        feature_evidence, feature_window = self._validated_feature_evidence(
            request,
            expected_edges,
            evidence,
        )
        feature_run = self._drilldown.localize(
            request.incident_id,
            window=feature_window,
            alerting_api=alerting_api,
            evidence=feature_evidence,
        )
        top_resolution = None
        fallback_reason = None
        if not feature_run.drilldown.requires_fallback:
            top_resolution = self._top_scope_resolver.resolve(
                feature_run,
                cluster_id=request.cluster_id,
                namespace=request.namespace,
                profile_id=profile_id,
                max_candidates=request.max_candidates,
                max_entities=max_entities,
                max_depth=max_depth,
            )
            if top_resolution.scope is not None:
                localization = self._localization_service.localize_incident(
                    request.incident_id,
                    scope=top_resolution.scope,
                    frozen_at=frozen_at,
                )
                return KRCAGuidedIncidentLocalizationRun(
                    feature_run=feature_run,
                    top_resolution=top_resolution,
                    source_resolution=None,
                    localization=localization,
                    seed_source="krca-top-services",
                    fallback_reason=None,
                )
            fallback_reason = "TOP_SERVICE_ENTITY_RESOLUTION_INCOMPLETE"
        else:
            fallback_reason = feature_run.drilldown.stop_reason

        source_resolution = self._resolver.resolve(request)
        if source_resolution.status != "RESOLVED":
            return KRCAGuidedIncidentLocalizationRun(
                feature_run=feature_run,
                top_resolution=top_resolution,
                source_resolution=source_resolution,
                localization=None,
                seed_source="source-entity-krca-fallback",
                fallback_reason=fallback_reason,
            )
        scope = self._scope_factory.create(
            source_resolution,
            additional_correlation_keys={
                "seed_source": "source-entity-krca-fallback",
                "krca_profile": profile_id,
                "krca_drilldown_stop": feature_run.drilldown.stop_reason,
                "krca_fallback_reason": fallback_reason,
            },
            max_entities=max_entities,
            max_depth=max_depth,
        )
        localization = self._localization_service.localize_incident(
            request.incident_id,
            scope=scope,
            frozen_at=frozen_at,
        )
        return KRCAGuidedIncidentLocalizationRun(
            feature_run=feature_run,
            top_resolution=top_resolution,
            source_resolution=source_resolution,
            localization=localization,
            seed_source="source-entity-krca-fallback",
            fallback_reason=fallback_reason,
        )

    def _validated_feature_evidence(
        self,
        request: EntityResolutionRequest,
        expected_edges: Mapping[str, Tuple[APIRef, APIRef]],
        evidence: Sequence[Mapping[str, Any]],
    ) -> Tuple[Tuple[Mapping[str, Any], ...], EvidenceWindow]:
        selected = tuple(
            item for item in evidence if self._feature_projector.supports(item)
        )
        by_edge: dict[str, Mapping[str, Any]] = {}
        windows = set()
        for item in selected:
            validate_contract("evidence-item.schema.json", item)
            if item["incident_id"] != request.incident_id:
                raise ContractViolation(
                    "KRCA feature Evidence belongs to a different Incident"
                )
            facts = item["facts"]
            edge_id = facts.get("edge_id")
            if not isinstance(edge_id, str) or edge_id in by_edge:
                raise ContractViolation("KRCA feature Evidence edge_id is duplicated")
            expected = expected_edges.get(edge_id)
            if expected is None:
                raise ContractViolation(
                    "KRCA feature Evidence is outside the selected profile"
                )
            parent = self._feature_projector._api_ref(facts.get("parent"), "parent")
            child = self._feature_projector._api_ref(facts.get("child"), "child")
            if (parent, child) != expected:
                raise ContractViolation(
                    "KRCA feature Evidence dependency does not match its profile"
                )
            by_edge[edge_id] = item
            windows.add((item["window"]["start"], item["window"]["end"]))
        if set(by_edge) != set(expected_edges):
            raise ContractViolation(
                "KRCA feature Evidence does not cover the selected profile"
            )
        if len(windows) != 1:
            raise ContractViolation("KRCA feature Evidence windows are inconsistent")
        start, end = next(iter(windows))
        return tuple(by_edge[key] for key in sorted(by_edge)), EvidenceWindow(start, end)
