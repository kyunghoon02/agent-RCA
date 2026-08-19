"""Evidence-gated adaptive expansion around bounded Graph localization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol, Sequence, Tuple

from .errors import ContractViolation
from .stategraph import GraphLocalizer, InvestigationScope


@dataclass(frozen=True)
class AdaptiveScopePolicy:
    """Hard limits and deterministic growth steps for one localization run."""

    max_entities: int = 100
    max_depth: int = 4
    max_rounds: int = 4
    entity_step: int = 20
    depth_step: int = 1
    minimum_context_completeness: float = 0.7

    def __post_init__(self) -> None:
        if not 1 <= self.max_entities <= 1000:
            raise ValueError("max_entities must be between 1 and 1000")
        if not 0 <= self.max_depth <= 16:
            raise ValueError("max_depth must be between 0 and 16")
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be positive")
        if self.entity_step < 1:
            raise ValueError("entity_step must be positive")
        if self.depth_step < 1:
            raise ValueError("depth_step must be positive")
        if not 0 <= self.minimum_context_completeness <= 1:
            raise ValueError("minimum_context_completeness must be between 0 and 1")


@dataclass(frozen=True)
class LocalizationAssessment:
    """Reasoning signal that decides whether the current Context is sufficient."""

    evidence_sufficient: bool
    contradiction_count: int = 0
    competing_hypotheses: int = 0
    multi_factor_suspected: bool = False
    requested_seed_entity_ids: Tuple[str, ...] = field(default_factory=tuple)
    reason_codes: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "requested_seed_entity_ids",
            tuple(dict.fromkeys(self.requested_seed_entity_ids)),
        )
        object.__setattr__(self, "reason_codes", tuple(dict.fromkeys(self.reason_codes)))
        if self.contradiction_count < 0:
            raise ValueError("contradiction_count cannot be negative")
        if self.competing_hypotheses < 0:
            raise ValueError("competing_hypotheses cannot be negative")


class LocalizationAssessor(Protocol):
    """Evaluate only the frozen Context and Evidence selected for that Context."""

    def assess(
        self,
        context: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]],
    ) -> LocalizationAssessment:
        ...


@dataclass(frozen=True)
class AdaptiveLocalizationRound:
    """Auditable input and result for one localization attempt."""

    number: int
    scope: InvestigationScope
    assessment: LocalizationAssessment
    expansion_triggers: Tuple[str, ...]
    entity_ids: Tuple[str, ...]
    evidence_ids: Tuple[str, ...]
    new_evidence_ids: Tuple[str, ...]
    context_completeness: float

    def to_audit_record(self) -> Mapping[str, Any]:
        return {
            "round": self.number,
            "scope": self.scope.to_contract(),
            "assessment": {
                "evidence_sufficient": self.assessment.evidence_sufficient,
                "contradiction_count": self.assessment.contradiction_count,
                "competing_hypotheses": self.assessment.competing_hypotheses,
                "multi_factor_suspected": self.assessment.multi_factor_suspected,
                "requested_seed_entity_ids": list(
                    self.assessment.requested_seed_entity_ids
                ),
                "reason_codes": list(self.assessment.reason_codes),
            },
            "expansion_triggers": list(self.expansion_triggers),
            "entity_ids": list(self.entity_ids),
            "evidence_ids": list(self.evidence_ids),
            "new_evidence_ids": list(self.new_evidence_ids),
            "context_completeness": self.context_completeness,
        }


@dataclass(frozen=True)
class AdaptiveLocalizationRun:
    """Final Context plus the reason localization stopped expanding."""

    context: Mapping[str, Any]
    rounds: Tuple[AdaptiveLocalizationRound, ...]
    stop_reason: str
    budget_exhausted: bool

    @property
    def requires_abstention(self) -> bool:
        return self.stop_reason != "EVIDENCE_SUFFICIENT"


class AdaptiveScopeController:
    """Expand a Graph scope only when auditable reasoning signals require it.

    The controller never widens the Incident time window, domain allowlist, relation
    allowlist, or correlation keys. It can promote an Entity already present in the
    frozen Context, or a seed explicitly approved by an upstream drilldown result,
    and grow entity/depth budgets only up to hard caps.
    """

    def __init__(
        self,
        localizer: GraphLocalizer,
        *,
        policy: Optional[AdaptiveScopePolicy] = None,
    ) -> None:
        self._localizer = localizer
        self._policy = policy or AdaptiveScopePolicy()

    def run(
        self,
        initial_scope: InvestigationScope,
        evidence: Sequence[Mapping[str, Any]],
        assessor: LocalizationAssessor,
        *,
        frozen_at: datetime,
        collector_failures: Sequence[Mapping[str, str]] = (),
        approved_seed_entity_ids: Sequence[str] = (),
    ) -> AdaptiveLocalizationRun:
        self._validate_initial_scope(initial_scope)
        evidence_by_id = {item.get("evidence_id"): item for item in evidence}
        approved_seeds = frozenset(approved_seed_entity_ids)
        scope = initial_scope
        rounds = []
        previous_signature = None
        previous_evidence_ids: set[str] = set()

        for round_number in range(1, self._policy.max_rounds + 1):
            context = self._localizer.build_context(
                scope,
                evidence,
                frozen_at=frozen_at,
                collector_failures=collector_failures,
            )
            entity_ids = self._context_entity_ids(context)
            evidence_ids = tuple(context["evidence_ids"])
            selected_evidence = tuple(
                evidence_by_id[evidence_id] for evidence_id in evidence_ids
            )
            assessment = assessor.assess(context, selected_evidence)
            if not isinstance(assessment, LocalizationAssessment):
                raise TypeError("LocalizationAssessor must return LocalizationAssessment")
            triggers = self._expansion_triggers(context, assessment)
            signature = (entity_ids, evidence_ids)
            round_result = AdaptiveLocalizationRound(
                number=round_number,
                scope=scope,
                assessment=assessment,
                expansion_triggers=triggers,
                entity_ids=entity_ids,
                evidence_ids=evidence_ids,
                new_evidence_ids=tuple(
                    sorted(set(evidence_ids) - previous_evidence_ids)
                ),
                context_completeness=context["localization"][
                    "context_completeness"
                ],
            )
            rounds.append(round_result)

            if not triggers:
                return AdaptiveLocalizationRun(
                    context=context,
                    rounds=tuple(rounds),
                    stop_reason="EVIDENCE_SUFFICIENT",
                    budget_exhausted=False,
                )
            if previous_signature == signature:
                return AdaptiveLocalizationRun(
                    context=context,
                    rounds=tuple(rounds),
                    stop_reason="NO_NEW_CONTEXT",
                    budget_exhausted=False,
                )
            if round_number >= self._policy.max_rounds:
                return AdaptiveLocalizationRun(
                    context=context,
                    rounds=tuple(rounds),
                    stop_reason="ROUND_BUDGET_EXHAUSTED",
                    budget_exhausted=True,
                )

            expanded_scope = self._expand_scope(
                scope,
                context,
                assessment,
                approved_seeds,
            )
            if expanded_scope == scope:
                return AdaptiveLocalizationRun(
                    context=context,
                    rounds=tuple(rounds),
                    stop_reason="SCOPE_BUDGET_EXHAUSTED",
                    budget_exhausted=True,
                )
            previous_signature = signature
            previous_evidence_ids = set(evidence_ids)
            scope = expanded_scope

        raise AssertionError("adaptive localization loop did not return")

    def _validate_initial_scope(self, scope: InvestigationScope) -> None:
        if scope.max_entities > self._policy.max_entities:
            raise ContractViolation(
                "initial max_entities exceeds adaptive localization hard cap"
            )
        if scope.max_depth > self._policy.max_depth:
            raise ContractViolation(
                "initial max_depth exceeds adaptive localization hard cap"
            )

    def _expansion_triggers(
        self,
        context: Mapping[str, Any],
        assessment: LocalizationAssessment,
    ) -> Tuple[str, ...]:
        triggers = []
        completeness = context["localization"]["context_completeness"]
        if completeness < self._policy.minimum_context_completeness:
            triggers.append("LOW_CONTEXT_COMPLETENESS")
        if not assessment.evidence_sufficient:
            triggers.append("INSUFFICIENT_EVIDENCE")
        if assessment.contradiction_count:
            triggers.append("CONTRADICTORY_EVIDENCE")
        if assessment.competing_hypotheses >= 2:
            triggers.append("COMPETING_HYPOTHESES")
        if assessment.multi_factor_suspected:
            triggers.append("MULTI_FACTOR_SUSPECTED")
        return tuple(triggers)

    def _expand_scope(
        self,
        scope: InvestigationScope,
        context: Mapping[str, Any],
        assessment: LocalizationAssessment,
        approved_seed_entity_ids: frozenset[str],
    ) -> InvestigationScope:
        available_entities = set(self._context_entity_ids(context))
        requested = assessment.requested_seed_entity_ids
        allowed_promotions = available_entities | approved_seed_entity_ids
        unknown = sorted(set(requested) - allowed_promotions)
        if unknown:
            raise ContractViolation(
                "adaptive localization can only promote current Context Entities "
                "or seeds approved by upstream drilldown: "
                f"{', '.join(unknown)}"
            )
        seed_entity_ids = tuple(dict.fromkeys(scope.seed_entity_ids + requested))
        max_entities = min(
            self._policy.max_entities,
            scope.max_entities + self._policy.entity_step,
        )
        max_depth = min(
            self._policy.max_depth,
            scope.max_depth + self._policy.depth_step,
        )
        if len(seed_entity_ids) > max_entities:
            raise ContractViolation(
                "adaptive localization seed count exceeds the expanded entity budget"
            )
        return InvestigationScope(
            incident_id=scope.incident_id,
            seed_entity_ids=seed_entity_ids,
            window=scope.window,
            domains=scope.domains,
            correlation_keys=scope.correlation_keys,
            relation_types=scope.relation_types,
            max_entities=max_entities,
            max_depth=max_depth,
        )

    @staticmethod
    def _context_entity_ids(context: Mapping[str, Any]) -> Tuple[str, ...]:
        return tuple(
            sorted(
                {
                    entity["entity_id"]
                    for path in context["state_paths"]
                    for entity in path["entities"]
                }
            )
        )
