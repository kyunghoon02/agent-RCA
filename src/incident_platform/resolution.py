"""Fail-closed service-name resolution before Incident Graph localization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Tuple

from .errors import ContractViolation
from .evidence import EvidenceWindow, parse_time
from .localization import IncidentLocalizationRun, IncidentLocalizationService
from .stategraph import EntityLookup, InvestigationScope, StateGraphRepository


@dataclass(frozen=True)
class EntityResolutionRequest:
    """Trusted cluster scope plus an exact logical service name."""

    incident_id: str
    cluster_id: str
    namespace: str
    service_name: str
    window: EvidenceWindow
    max_candidates: int = 10

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.incident_id,
                self.cluster_id,
                self.namespace,
                self.service_name,
            )
        ):
            raise ContractViolation(
                "EntityResolutionRequest identity fields must be non-empty"
            )
        if not 2 <= self.max_candidates <= 99:
            raise ContractViolation("max_candidates must be between 2 and 99")
        if parse_time(self.window.start, "resolution.window.start") > parse_time(
            self.window.end, "resolution.window.end"
        ):
            raise ContractViolation("resolution window start must not follow end")

    def to_contract(self) -> Dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "cluster_id": self.cluster_id,
            "namespace": self.namespace,
            "service_name": self.service_name,
            "time_window": {"start": self.window.start, "end": self.window.end},
            "max_candidates": self.max_candidates,
        }


@dataclass(frozen=True)
class EntityResolutionCandidate:
    entity_id: str
    entity_type: str
    domain: str
    name: str
    identity_type: str

    @classmethod
    def from_entity(cls, entity: Mapping[str, object]) -> "EntityResolutionCandidate":
        identity = entity["identity"]
        if not isinstance(identity, Mapping):
            raise ContractViolation("resolved Entity identity is malformed")
        identity_type = identity.get("identity_type")
        if not isinstance(identity_type, str):
            raise ContractViolation("resolved Entity identity_type is malformed")
        return cls(
            entity_id=str(entity["entity_id"]),
            entity_type=str(entity["entity_type"]),
            domain=str(entity["domain"]),
            name=str(entity["name"]),
            identity_type=identity_type,
        )

    def to_contract(self) -> Dict[str, str]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "domain": self.domain,
            "name": self.name,
            "identity_type": self.identity_type,
        }


@dataclass(frozen=True)
class EntityResolutionResult:
    """A resolver never chooses a seed when the exact match is not unique."""

    status: str
    request: EntityResolutionRequest
    candidates: Tuple[EntityResolutionCandidate, ...] = field(default_factory=tuple)
    seed_entity_ids: Tuple[str, ...] = field(default_factory=tuple)
    method: Optional[str] = None
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "seed_entity_ids", tuple(self.seed_entity_ids))
        if self.status not in {"RESOLVED", "AMBIGUOUS", "NOT_FOUND"}:
            raise ContractViolation(f"unsupported Entity resolution status: {self.status}")
        if self.status == "RESOLVED":
            if len(self.candidates) != 1 or len(self.seed_entity_ids) != 1:
                raise ContractViolation("RESOLVED requires exactly one candidate and seed")
            if self.seed_entity_ids[0] != self.candidates[0].entity_id:
                raise ContractViolation("resolved seed does not match its candidate")
        elif self.seed_entity_ids:
            raise ContractViolation("unresolved results must not expose seed Entity IDs")

    def to_contract(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "request": self.request.to_contract(),
            "candidates": [item.to_contract() for item in self.candidates],
            "seed_entity_ids": list(self.seed_entity_ids),
            "method": self.method,
            "reason": self.reason,
        }


class ServiceToEntityResolver:
    """Resolve logical Service first, then exact Kubernetes Service as fallback."""

    def __init__(self, repository: StateGraphRepository) -> None:
        self._repository = repository

    def resolve(self, request: EntityResolutionRequest) -> EntityResolutionResult:
        logical = self._repository.find_entities(
            EntityLookup(
                cluster_id=request.cluster_id,
                namespace=request.namespace,
                name=request.service_name,
                window=request.window,
                domains=("web-service",),
                entity_types=("Service",),
                identity_types=("logical-service",),
                limit=request.max_candidates + 1,
            )
        )
        if logical:
            return self._result(request, logical, "logical-service-exact")

        kubernetes = self._repository.find_entities(
            EntityLookup(
                cluster_id=request.cluster_id,
                namespace=request.namespace,
                name=request.service_name,
                window=request.window,
                domains=("kubernetes",),
                entity_types=("Service",),
                identity_types=("kubernetes-resource",),
                limit=request.max_candidates + 1,
            )
        )
        if kubernetes:
            return self._result(request, kubernetes, "kubernetes-service-exact")
        return EntityResolutionResult(
            status="NOT_FOUND",
            request=request,
            reason="no exact time-bounded Service Entity matched",
        )

    @staticmethod
    def _result(
        request: EntityResolutionRequest,
        entities: Tuple[Mapping[str, object], ...],
        method: str,
    ) -> EntityResolutionResult:
        candidates = tuple(EntityResolutionCandidate.from_entity(item) for item in entities)
        if len(candidates) != 1:
            return EntityResolutionResult(
                status="AMBIGUOUS",
                request=request,
                candidates=candidates[: request.max_candidates],
                method=method,
                reason="multiple exact time-bounded Service Entities matched",
            )
        return EntityResolutionResult(
            status="RESOLVED",
            request=request,
            candidates=candidates,
            seed_entity_ids=(candidates[0].entity_id,),
            method=method,
        )


class InvestigationScopeFactory:
    """Create a bounded Graph scope only from an approved resolver result."""

    def create(
        self,
        resolution: EntityResolutionResult,
        *,
        domains: Tuple[str, ...] = ("web-service", "kubernetes"),
        relation_types: Tuple[str, ...] = (
            "REPRESENTED_BY",
            "RESOLVES_TO",
            "REFERENCES",
            "OWNS",
            "SELECTS",
            "SCHEDULED_ON",
            "DEPENDS_ON",
            "CALLS",
            "ROUTES_TO",
        ),
        max_entities: int = 100,
        max_depth: int = 4,
    ) -> InvestigationScope:
        if resolution.status != "RESOLVED":
            raise ContractViolation(
                "InvestigationScope requires a RESOLVED EntityResolutionResult"
            )
        request = resolution.request
        return InvestigationScope(
            incident_id=request.incident_id,
            seed_entity_ids=resolution.seed_entity_ids,
            window=request.window,
            domains=domains,
            correlation_keys={
                "cluster_id": request.cluster_id,
                "namespace": request.namespace,
                "service_name": request.service_name,
            },
            relation_types=relation_types,
            max_entities=max_entities,
            max_depth=max_depth,
        )


@dataclass(frozen=True)
class ResolvedIncidentLocalizationRun:
    resolution: EntityResolutionResult
    localization: Optional[IncidentLocalizationRun]


class ResolvedIncidentLocalizationService:
    """Fail-closed resolver facade placed before IncidentLocalizationService."""

    def __init__(
        self,
        resolver: ServiceToEntityResolver,
        localization_service: IncidentLocalizationService,
        scope_factory: Optional[InvestigationScopeFactory] = None,
    ) -> None:
        self._resolver = resolver
        self._localization_service = localization_service
        self._scope_factory = scope_factory or InvestigationScopeFactory()

    def localize_service(
        self,
        request: EntityResolutionRequest,
        *,
        frozen_at: Optional[datetime] = None,
        max_entities: int = 100,
        max_depth: int = 4,
    ) -> ResolvedIncidentLocalizationRun:
        resolution = self._resolver.resolve(request)
        if resolution.status != "RESOLVED":
            return ResolvedIncidentLocalizationRun(resolution, None)
        scope = self._scope_factory.create(
            resolution,
            max_entities=max_entities,
            max_depth=max_depth,
        )
        run = self._localization_service.localize_incident(
            request.incident_id,
            scope=scope,
            frozen_at=frozen_at,
        )
        return ResolvedIncidentLocalizationRun(resolution, run)
