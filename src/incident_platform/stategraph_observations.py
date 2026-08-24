"""Durable journal boundary for background StateGraph observations."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Any, Dict, Mapping, Protocol, Sequence, Tuple, runtime_checkable

from .contracts import validate_contract
from .errors import InvalidTransition
from .evidence import format_time, parse_time
from .stategraph import StateGraphReconciliationResult


@dataclass(frozen=True)
class StateGraphObservationRetentionPolicy:
    """Retention for applied audit history and abandoned staged cycles."""

    applied_history: timedelta = timedelta(hours=72)
    abandoned_staged_history: timedelta = timedelta(hours=24)

    def __post_init__(self) -> None:
        if self.applied_history <= timedelta(0):
            raise ValueError("applied observation history must be positive")
        if self.abandoned_staged_history <= timedelta(0):
            raise ValueError("staged observation history must be positive")
        if self.abandoned_staged_history > self.applied_history:
            raise ValueError(
                "staged observation history must not exceed applied history"
            )


@dataclass(frozen=True)
class StateGraphObservationCycle:
    """Idempotent STAGED/APPLIED journal entry for one complete inventory."""

    cycle_id: str
    request_id: str
    evidence_scope_id: str
    cluster_id: str
    namespace: str
    observed_at: str
    staged_at: str
    status: str
    evidence_ids: Tuple[str, ...]
    applied_at: str | None = None
    result: StateGraphReconciliationResult | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        if not self.cycle_id.startswith("cycle-"):
            raise InvalidTransition("StateGraph observation cycle_id is invalid")
        if not self.request_id.strip():
            raise InvalidTransition("StateGraph observation request_id is required")
        if not self.evidence_scope_id.startswith("inc-"):
            raise InvalidTransition(
                "StateGraph observation evidence_scope_id is invalid"
            )
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.cluster_id, self.namespace)
        ):
            raise InvalidTransition(
                "StateGraph observation cluster and namespace are required"
            )
        observed = parse_time(self.observed_at, "ObservationCycle.observed_at")
        staged = parse_time(self.staged_at, "ObservationCycle.staged_at")
        if observed > staged:
            raise InvalidTransition(
                "StateGraph observation cannot be staged before it was observed"
            )
        if not self.evidence_ids or len(self.evidence_ids) != len(
            set(self.evidence_ids)
        ):
            raise InvalidTransition(
                "StateGraph observation evidence_ids must be non-empty and unique"
            )
        if self.status == "STAGED":
            if self.applied_at is not None or self.result is not None:
                raise InvalidTransition(
                    "STAGED StateGraph observation cannot have an applied result"
                )
        elif self.status == "APPLIED":
            if self.applied_at is None or self.result is None:
                raise InvalidTransition(
                    "APPLIED StateGraph observation requires time and result"
                )
            if parse_time(self.applied_at, "ObservationCycle.applied_at") < staged:
                raise InvalidTransition(
                    "StateGraph observation applied_at cannot precede staged_at"
                )
        else:
            raise InvalidTransition(
                "StateGraph observation status must be STAGED or APPLIED"
            )

    def staging_identity(self) -> Tuple[Any, ...]:
        return (
            self.cycle_id,
            self.request_id,
            self.evidence_scope_id,
            self.cluster_id,
            self.namespace,
            self.observed_at,
            self.staged_at,
            self.evidence_ids,
        )

    def to_document(self) -> Dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "request_id": self.request_id,
            "evidence_scope_id": self.evidence_scope_id,
            "cluster_id": self.cluster_id,
            "namespace": self.namespace,
            "observed_at": self.observed_at,
            "staged_at": self.staged_at,
            "status": self.status,
            "evidence_ids": list(self.evidence_ids),
            "applied_at": self.applied_at,
            "result": asdict(self.result) if self.result is not None else None,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "StateGraphObservationCycle":
        result_document = document.get("result")
        result = (
            StateGraphReconciliationResult(**dict(result_document))
            if isinstance(result_document, Mapping)
            else None
        )
        return cls(
            cycle_id=str(document.get("cycle_id", "")),
            request_id=str(document.get("request_id", "")),
            evidence_scope_id=str(document.get("evidence_scope_id", "")),
            cluster_id=str(document.get("cluster_id", "")),
            namespace=str(document.get("namespace", "")),
            observed_at=str(document.get("observed_at", "")),
            staged_at=str(document.get("staged_at", "")),
            status=str(document.get("status", "")),
            evidence_ids=tuple(document.get("evidence_ids", ())),
            applied_at=document.get("applied_at"),
            result=result,
        )


@dataclass(frozen=True)
class StateGraphObservationPruneResult:
    cycles: int = 0
    evidence_items: int = 0


@runtime_checkable
class StateGraphObservationRepository(Protocol):
    """Journal Port that makes Evidence durable before Graph mutation."""

    def stage_cycle(
        self,
        cycle: StateGraphObservationCycle,
        evidence: Sequence[Mapping[str, Any]],
    ) -> StateGraphObservationCycle:
        ...

    def mark_cycle_applied(
        self,
        cycle_id: str,
        result: StateGraphReconciliationResult,
        *,
        applied_at: datetime,
    ) -> StateGraphObservationCycle:
        ...

    def get_cycle(self, cycle_id: str) -> StateGraphObservationCycle:
        ...

    def list_cycle_evidence(self, cycle_id: str) -> Tuple[Mapping[str, Any], ...]:
        ...

    def prune_observations(
        self,
        *,
        now: datetime,
        batch_size: int = 1000,
    ) -> StateGraphObservationPruneResult:
        ...


def validate_cycle_evidence(
    cycle: StateGraphObservationCycle,
    evidence: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[str, Any], ...]:
    candidates = tuple(copy.deepcopy(dict(item)) for item in evidence)
    for candidate in candidates:
        validate_contract("evidence-item.schema.json", candidate)
        if candidate["incident_id"] != cycle.evidence_scope_id:
            raise InvalidTransition(
                "Observation Evidence scope does not match its cycle"
            )
    evidence_ids = tuple(sorted(item["evidence_id"] for item in candidates))
    if evidence_ids != tuple(sorted(cycle.evidence_ids)):
        raise InvalidTransition(
            "Observation Evidence IDs do not match the staged cycle"
        )
    return tuple(sorted(candidates, key=lambda item: item["evidence_id"]))


class InMemoryStateGraphObservationRepository:
    """Thread-safe reference journal with idempotent retry semantics."""

    def __init__(
        self,
        retention_policy: StateGraphObservationRetentionPolicy | None = None,
    ) -> None:
        self._retention_policy = (
            retention_policy or StateGraphObservationRetentionPolicy()
        )
        self._cycles: Dict[str, StateGraphObservationCycle] = {}
        self._evidence_by_cycle: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._evidence_owner: Dict[str, str] = {}
        self._lock = RLock()

    def stage_cycle(
        self,
        cycle: StateGraphObservationCycle,
        evidence: Sequence[Mapping[str, Any]],
    ) -> StateGraphObservationCycle:
        if cycle.status != "STAGED":
            raise InvalidTransition("only a STAGED observation cycle can be staged")
        candidates = validate_cycle_evidence(cycle, evidence)
        with self._lock:
            existing = self._cycles.get(cycle.cycle_id)
            if existing is not None:
                if existing.staging_identity() != cycle.staging_identity():
                    raise InvalidTransition(
                        f"observation cycle collision: {cycle.cycle_id}"
                    )
                stored = self._evidence_by_cycle[cycle.cycle_id]
                if tuple(stored[key] for key in sorted(stored)) != candidates:
                    raise InvalidTransition(
                        f"observation cycle Evidence collision: {cycle.cycle_id}"
                    )
                return copy.deepcopy(existing)
            for candidate in candidates:
                evidence_id = candidate["evidence_id"]
                owner = self._evidence_owner.get(evidence_id)
                if owner is not None and owner != cycle.cycle_id:
                    raise InvalidTransition(
                        f"observation evidence_id belongs to another cycle: {evidence_id}"
                    )
            self._cycles[cycle.cycle_id] = copy.deepcopy(cycle)
            self._evidence_by_cycle[cycle.cycle_id] = {
                item["evidence_id"]: item for item in candidates
            }
            for evidence_id in cycle.evidence_ids:
                self._evidence_owner[evidence_id] = cycle.cycle_id
            return copy.deepcopy(cycle)

    def mark_cycle_applied(
        self,
        cycle_id: str,
        result: StateGraphReconciliationResult,
        *,
        applied_at: datetime,
    ) -> StateGraphObservationCycle:
        applied_text = format_time(applied_at)
        with self._lock:
            try:
                existing = self._cycles[cycle_id]
            except KeyError as error:
                raise KeyError(f"unknown observation cycle: {cycle_id}") from error
            updated = replace(
                existing,
                status="APPLIED",
                applied_at=applied_text,
                result=result,
            )
            if existing.status == "APPLIED":
                if existing != updated:
                    raise InvalidTransition(
                        f"observation cycle result collision: {cycle_id}"
                    )
                return copy.deepcopy(existing)
            self._cycles[cycle_id] = updated
            return copy.deepcopy(updated)

    def get_cycle(self, cycle_id: str) -> StateGraphObservationCycle:
        with self._lock:
            try:
                return copy.deepcopy(self._cycles[cycle_id])
            except KeyError as error:
                raise KeyError(f"unknown observation cycle: {cycle_id}") from error

    def list_cycle_evidence(self, cycle_id: str) -> Tuple[Mapping[str, Any], ...]:
        with self._lock:
            if cycle_id not in self._cycles:
                raise KeyError(f"unknown observation cycle: {cycle_id}")
            stored = self._evidence_by_cycle[cycle_id]
            return tuple(copy.deepcopy(stored[key]) for key in sorted(stored))

    def prune_observations(
        self,
        *,
        now: datetime,
        batch_size: int = 1000,
    ) -> StateGraphObservationPruneResult:
        if not 1 <= batch_size <= 10_000:
            raise ValueError("observation prune batch_size must be between 1 and 10000")
        now_text = format_time(now)
        now_utc = parse_time(now_text, "ObservationPrune.now")
        applied_cutoff = now_utc - self._retention_policy.applied_history
        staged_cutoff = now_utc - self._retention_policy.abandoned_staged_history
        with self._lock:
            doomed = []
            for cycle in self._cycles.values():
                retention_time = parse_time(
                    cycle.applied_at or cycle.staged_at,
                    "ObservationCycle.retention_time",
                )
                cutoff = applied_cutoff if cycle.status == "APPLIED" else staged_cutoff
                if retention_time <= cutoff:
                    doomed.append((retention_time, cycle.cycle_id))
            doomed.sort()
            deleted_evidence = 0
            for _, cycle_id in doomed[:batch_size]:
                stored = self._evidence_by_cycle.pop(cycle_id)
                for evidence_id in stored:
                    self._evidence_owner.pop(evidence_id, None)
                deleted_evidence += len(stored)
                self._cycles.pop(cycle_id)
            return StateGraphObservationPruneResult(
                cycles=min(len(doomed), batch_size),
                evidence_items=deleted_evidence,
            )
