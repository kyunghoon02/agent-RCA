"""Evidence-backed adaptation of KRCA's API-level drilldown.

The production paper computes time-series features before scoring an API edge.
This module deliberately accepts those features as a normalized contract so the
cloud-neutral core does not pretend that fixture data are live Prometheus proof.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Tuple

from .errors import ContractViolation


@dataclass(frozen=True, order=True)
class APIRef:
    """Stable service and operation identity used by dependency edges."""

    service: str
    operation: str

    def __post_init__(self) -> None:
        if not self.service.strip():
            raise ValueError("APIRef.service is required")
        if not self.operation.strip():
            raise ValueError("APIRef.operation is required")

    @property
    def key(self) -> str:
        return f"{self.service}::{self.operation}"


@dataclass(frozen=True)
class APIEdgeSignal:
    """Precomputed metric features and Evidence supporting one API call edge."""

    parent: APIRef
    child: APIRef
    failure_rate_correlation: float
    failure_rate_p_value: float
    latency_anomaly: float
    latency_fluctuation_contribution: float
    latency_correlation: float
    evidence_ids: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", tuple(dict.fromkeys(self.evidence_ids)))
        if self.parent == self.child:
            raise ValueError("APIEdgeSignal cannot be a self edge")
        numeric = (
            self.failure_rate_correlation,
            self.failure_rate_p_value,
            self.latency_anomaly,
            self.latency_fluctuation_contribution,
            self.latency_correlation,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("API edge score features must be finite")
        if not -1 <= self.failure_rate_correlation <= 1:
            raise ValueError("failure_rate_correlation must be between -1 and 1")
        if not 0 <= self.failure_rate_p_value <= 1:
            raise ValueError("failure_rate_p_value must be between 0 and 1")
        if not -1 <= self.latency_correlation <= 1:
            raise ValueError("latency_correlation must be between -1 and 1")
        if self.latency_anomaly < 0:
            raise ValueError("latency_anomaly cannot be negative")
        if self.latency_fluctuation_contribution < 0:
            raise ValueError("latency_fluctuation_contribution cannot be negative")
        if not self.evidence_ids:
            raise ContractViolation("APIEdgeSignal requires at least one Evidence ID")
        if any(not evidence_id.startswith("ev-") for evidence_id in self.evidence_ids):
            raise ContractViolation("APIEdgeSignal contains an invalid Evidence ID")


@dataclass(frozen=True)
class KRCADrilldownPolicy:
    """Paper-aligned defaults plus explicit traversal safety caps."""

    propagation_threshold: float = 0.8
    significance_alpha: float = 0.05
    latency_weights: Tuple[float, float, float] = (0.2, 0.5, 0.3)
    top_n_services: int = 3
    maximum_time_lag: int = 5
    max_depth: int = 8
    max_evaluated_edges: int = 500

    def __post_init__(self) -> None:
        object.__setattr__(self, "latency_weights", tuple(self.latency_weights))
        if self.propagation_threshold < 0:
            raise ValueError("propagation_threshold cannot be negative")
        if not 0 < self.significance_alpha < 1:
            raise ValueError("significance_alpha must be between 0 and 1")
        if len(self.latency_weights) != 3:
            raise ValueError("latency_weights must contain three values")
        if any(weight < 0 or not math.isfinite(weight) for weight in self.latency_weights):
            raise ValueError("latency_weights must be finite and non-negative")
        if not math.isclose(sum(self.latency_weights), 1.0, abs_tol=1e-9):
            raise ValueError("latency_weights must sum to 1")
        if self.top_n_services < 1:
            raise ValueError("top_n_services must be positive")
        if self.maximum_time_lag < 0:
            raise ValueError("maximum_time_lag cannot be negative")
        if self.max_depth < 1:
            raise ValueError("max_depth must be positive")
        if self.max_evaluated_edges < 1:
            raise ValueError("max_evaluated_edges must be positive")


@dataclass(frozen=True)
class KRCAScoredEdge:
    signal: APIEdgeSignal
    failure_rate_score: float
    latency_score: float
    score: float
    retained: bool


@dataclass(frozen=True)
class KRCACandidate:
    api: APIRef
    score: float
    path: Tuple[APIRef, ...]
    evidence_ids: Tuple[str, ...]


@dataclass(frozen=True)
class KRCADrilldownRun:
    alerting_api: APIRef
    scored_edges: Tuple[KRCAScoredEdge, ...]
    candidates: Tuple[KRCACandidate, ...]
    top_services: Tuple[KRCACandidate, ...]
    stop_reason: str
    budget_exhausted: bool

    @property
    def requires_fallback(self) -> bool:
        return self.budget_exhausted or not self.top_services

    @property
    def next_ranked_candidates(self) -> Tuple[KRCACandidate, ...]:
        selected = {candidate.api.service for candidate in self.top_services}
        return tuple(
            candidate
            for candidate in self.candidates
            if candidate.api.service not in selected
        )

    def to_audit_record(self) -> Mapping[str, object]:
        return {
            "alerting_api": self.alerting_api.key,
            "stop_reason": self.stop_reason,
            "budget_exhausted": self.budget_exhausted,
            "top_services": [
                {
                    "service": candidate.api.service,
                    "operation": candidate.api.operation,
                    "score": candidate.score,
                    "path": [api.key for api in candidate.path],
                    "evidence_ids": list(candidate.evidence_ids),
                }
                for candidate in self.top_services
            ],
            "scored_edges": [
                {
                    "parent": edge.signal.parent.key,
                    "child": edge.signal.child.key,
                    "failure_rate_score": edge.failure_rate_score,
                    "failure_rate_correlation": (
                        edge.signal.failure_rate_correlation
                    ),
                    "failure_rate_p_value": edge.signal.failure_rate_p_value,
                    "latency_anomaly": edge.signal.latency_anomaly,
                    "latency_fluctuation_contribution": (
                        edge.signal.latency_fluctuation_contribution
                    ),
                    "latency_correlation": edge.signal.latency_correlation,
                    "latency_score": edge.latency_score,
                    "score": edge.score,
                    "retained": edge.retained,
                    "evidence_ids": list(edge.signal.evidence_ids),
                }
                for edge in self.scored_edges
            ],
        }


class KRCADrilldownScorer:
    """Apply KRCA failure-rate and composite latency scoring to API edges."""

    def __init__(self, policy: KRCADrilldownPolicy) -> None:
        self._policy = policy

    def score(self, signal: APIEdgeSignal) -> KRCAScoredEdge:
        failure_rate_score = (
            max(0.0, signal.failure_rate_correlation)
            if signal.failure_rate_p_value <= self._policy.significance_alpha
            else 0.0
        )
        anomaly_weight, fluctuation_weight, correlation_weight = (
            self._policy.latency_weights
        )
        latency_score = (
            anomaly_weight * signal.latency_anomaly
            + fluctuation_weight * signal.latency_fluctuation_contribution
            + correlation_weight * max(0.0, signal.latency_correlation)
        )
        score = max(failure_rate_score, latency_score)
        return KRCAScoredEdge(
            signal=signal,
            failure_rate_score=round(failure_rate_score, 6),
            latency_score=round(latency_score, 6),
            score=round(score, 6),
            retained=score >= self._policy.propagation_threshold,
        )


class KRCADrilldownLocalizer:
    """Recursively retain suspicious API propagation paths and rank services."""

    def __init__(self, *, policy: KRCADrilldownPolicy | None = None) -> None:
        self._policy = policy or KRCADrilldownPolicy()
        self._scorer = KRCADrilldownScorer(self._policy)

    def localize(
        self,
        alerting_api: APIRef,
        signals: Iterable[APIEdgeSignal],
    ) -> KRCADrilldownRun:
        adjacency: Dict[APIRef, List[KRCAScoredEdge]] = {}
        identities = set()
        for signal in signals:
            identity = (signal.parent, signal.child)
            if identity in identities:
                raise ContractViolation(
                    f"duplicate API edge signal: {signal.parent.key} -> {signal.child.key}"
                )
            identities.add(identity)
            adjacency.setdefault(signal.parent, []).append(self._scorer.score(signal))
        for edges in adjacency.values():
            edges.sort(key=lambda edge: (-edge.score, edge.signal.child.key))

        queue = deque([(alerting_api, (alerting_api,), tuple(), 0)])
        expanded = set()
        evaluated_edges = 0
        budget_exhausted = False
        scored_edges: List[KRCAScoredEdge] = []
        candidates: Dict[APIRef, KRCACandidate] = {}

        while queue and not budget_exhausted:
            current, path, path_evidence, depth = queue.popleft()
            if current in expanded:
                continue
            expanded.add(current)
            outgoing = adjacency.get(current, [])
            if depth >= self._policy.max_depth:
                if any(
                    edge.retained and edge.signal.child not in path
                    for edge in outgoing
                ):
                    budget_exhausted = True
                continue
            for edge in outgoing:
                if evaluated_edges >= self._policy.max_evaluated_edges:
                    budget_exhausted = True
                    break
                evaluated_edges += 1
                scored_edges.append(edge)
                if not edge.retained or edge.signal.child in path:
                    continue
                child = edge.signal.child
                evidence_ids = tuple(
                    dict.fromkeys(path_evidence + edge.signal.evidence_ids)
                )
                candidate = KRCACandidate(
                    api=child,
                    score=edge.score,
                    path=path + (child,),
                    evidence_ids=evidence_ids,
                )
                existing = candidates.get(child)
                if existing is None or candidate.score > existing.score:
                    candidates[child] = candidate
                queue.append((child, candidate.path, evidence_ids, depth + 1))

        ranked = tuple(
            sorted(
                candidates.values(),
                key=lambda candidate: (-candidate.score, candidate.api.key),
            )
        )
        top_services = self._top_services(ranked)
        if budget_exhausted:
            stop_reason = "DRILLDOWN_BUDGET_EXHAUSTED"
        elif not top_services:
            stop_reason = "NO_SUSPICIOUS_DOWNSTREAM"
        else:
            stop_reason = "TOP_N_READY"
        return KRCADrilldownRun(
            alerting_api=alerting_api,
            scored_edges=tuple(scored_edges),
            candidates=ranked,
            top_services=top_services,
            stop_reason=stop_reason,
            budget_exhausted=budget_exhausted,
        )

    def _top_services(
        self,
        candidates: Tuple[KRCACandidate, ...],
    ) -> Tuple[KRCACandidate, ...]:
        selected = []
        services = set()
        for candidate in candidates:
            if candidate.api.service in services:
                continue
            selected.append(candidate)
            services.add(candidate.api.service)
            if len(selected) >= self._policy.top_n_services:
                break
        return tuple(selected)
