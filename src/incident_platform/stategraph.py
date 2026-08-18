"""Domain-neutral temporal StateGraph contracts, storage, and localization."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .contracts import validate_contract
from .errors import ContractViolation
from .evidence import EvidenceWindow, redact


def _parse_time(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ContractViolation(f"{field_name} must be an RFC3339 timestamp") from error
    if parsed.tzinfo is None:
        raise ContractViolation(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ContractViolation("StateGraph timestamps must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def stable_graph_id(prefix: str, value: Any) -> str:
    """Build a deterministic identifier from canonical JSON content."""

    canonical = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def state_content_hash(state: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        state,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def validate_graph_record(record: Mapping[str, Any]) -> None:
    """Validate structure and reject content that still needs redaction."""

    validate_contract("graph-record.schema.json", record)
    _, redactions = redact(record)
    if redactions:
        raise ContractViolation(
            "Graph records must be redacted before storage: "
            f"{', '.join(sorted(redactions))}"
        )


def _merged_ids(*values: Iterable[str]) -> List[str]:
    return sorted({item for group in values for item in group})


def _overlaps(
    valid_from: str,
    valid_to: Optional[str],
    window: EvidenceWindow,
) -> bool:
    start = _parse_time(valid_from, "valid_from")
    end = _parse_time(valid_to, "valid_to") if valid_to is not None else None
    window_start = _parse_time(window.start, "time_window.start")
    window_end = _parse_time(window.end, "time_window.end")
    return start <= window_end and (end is None or end >= window_start)


@dataclass(frozen=True)
class InvestigationScope:
    """Domain-neutral, bounded scope used by Graph localization and Agent tools."""

    incident_id: str
    seed_entity_ids: Tuple[str, ...]
    window: EvidenceWindow
    domains: Tuple[str, ...] = field(default_factory=tuple)
    correlation_keys: Mapping[str, str] = field(default_factory=dict)
    relation_types: Tuple[str, ...] = field(default_factory=tuple)
    max_entities: int = 100
    max_depth: int = 4

    def __post_init__(self) -> None:
        object.__setattr__(self, "seed_entity_ids", tuple(self.seed_entity_ids))
        object.__setattr__(self, "domains", tuple(self.domains))
        object.__setattr__(self, "relation_types", tuple(self.relation_types))
        object.__setattr__(self, "correlation_keys", dict(self.correlation_keys))
        contract = self.to_contract()
        validate_contract("investigation-scope.schema.json", contract)
        start = _parse_time(self.window.start, "time_window.start")
        end = _parse_time(self.window.end, "time_window.end")
        if start > end:
            raise ContractViolation("InvestigationScope start must not follow end")

    def to_contract(self) -> Dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "seed_entity_ids": list(self.seed_entity_ids),
            "domains": list(self.domains),
            "correlation_keys": dict(self.correlation_keys),
            "relation_types": list(self.relation_types),
            "time_window": {
                "start": self.window.start,
                "end": self.window.end,
            },
            "max_entities": self.max_entities,
            "max_depth": self.max_depth,
        }


@dataclass(frozen=True)
class GraphProjection:
    records: Tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class LocalizedPath:
    entity_ids: Tuple[str, ...]
    relation_types: Tuple[str, ...]
    evidence_ids: Tuple[str, ...]


@dataclass(frozen=True)
class GraphLocalization:
    candidate_entities_before: int
    entities: Mapping[str, Mapping[str, Any]]
    paths: Tuple[LocalizedPath, ...]
    evidence_ids: Tuple[str, ...]
    recent_change_evidence_ids: Tuple[str, ...]
    entity_coverage: float


class InMemoryStateGraphRepository:
    """Thread-safe reference repository implementing temporal interval semantics."""

    def __init__(self) -> None:
        self._entities: Dict[str, Dict[str, Any]] = {}
        self._snapshots_by_entity: Dict[str, List[Dict[str, Any]]] = {}
        self._relations_by_key: Dict[str, List[Dict[str, Any]]] = {}
        self._events: Dict[str, Dict[str, Any]] = {}
        self._lock = RLock()

    def ingest(self, records: Sequence[Mapping[str, Any]]) -> None:
        """Ingest one projection while resolving entity references first."""

        grouped: Dict[str, List[Mapping[str, Any]]] = {
            "entity": [],
            "snapshot_interval": [],
            "relation_interval": [],
            "event_aggregate": [],
        }
        for record in records:
            record_type = record.get("record_type")
            if record_type not in grouped:
                raise ContractViolation(f"unsupported Graph record type: {record_type}")
            grouped[record_type].append(record)
        for record in grouped["entity"]:
            self.upsert_entity(record)
        for record in grouped["snapshot_interval"]:
            self.append_or_extend_snapshot(record)
        for record in grouped["relation_interval"]:
            self.append_or_extend_relation(record)
        for record in grouped["event_aggregate"]:
            self.upsert_event_aggregate(record)

    def upsert_entity(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        candidate = copy.deepcopy(dict(record))
        validate_graph_record(candidate)
        if candidate["record_type"] != "entity":
            raise ContractViolation("upsert_entity requires an entity record")
        first = _parse_time(candidate["first_seen_at"], "Entity.first_seen_at")
        last = _parse_time(candidate["last_seen_at"], "Entity.last_seen_at")
        if first > last:
            raise ContractViolation("Entity first_seen_at must not follow last_seen_at")
        identity_fields = ("entity_type", "domain", "name", "scope", "external_ref")

        with self._lock:
            existing = self._entities.get(candidate["entity_id"])
            if existing is None:
                self._entities[candidate["entity_id"]] = candidate
                self._snapshots_by_entity[candidate["entity_id"]] = []
                return copy.deepcopy(candidate)
            if any(existing[field] != candidate[field] for field in identity_fields):
                raise ContractViolation(
                    f"entity_id collision with different identity: {candidate['entity_id']}"
                )
            updated = copy.deepcopy(existing)
            existing_first = _parse_time(existing["first_seen_at"], "Entity.first_seen_at")
            existing_last = _parse_time(existing["last_seen_at"], "Entity.last_seen_at")
            updated["first_seen_at"] = _format_time(min(existing_first, first))
            updated["last_seen_at"] = _format_time(max(existing_last, last))
            if last >= existing_last:
                updated["exists"] = candidate["exists"]
            updated["evidence_ids"] = _merged_ids(
                existing["evidence_ids"], candidate["evidence_ids"]
            )
            validate_graph_record(updated)
            self._entities[candidate["entity_id"]] = updated
            return copy.deepcopy(updated)

    def append_or_extend_snapshot(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        candidate = copy.deepcopy(dict(record))
        validate_graph_record(candidate)
        if candidate["record_type"] != "snapshot_interval":
            raise ContractViolation(
                "append_or_extend_snapshot requires a snapshot_interval record"
            )
        observed = _parse_time(candidate["observed_at"], "Snapshot.observed_at")
        valid_from = _parse_time(candidate["valid_from"], "Snapshot.valid_from")
        valid_to = (
            _parse_time(candidate["valid_to"], "Snapshot.valid_to")
            if candidate["valid_to"] is not None
            else None
        )
        if valid_from > observed or (valid_to is not None and valid_from > valid_to):
            raise ContractViolation("Snapshot interval timestamps are inconsistent")

        with self._lock:
            history = self._require_snapshot_history(candidate["entity_id"])
            if not history:
                history.append(candidate)
                return copy.deepcopy(candidate)
            latest = history[-1]
            latest_from = _parse_time(latest["valid_from"], "Snapshot.valid_from")
            latest_to = (
                _parse_time(latest["valid_to"], "Snapshot.valid_to")
                if latest["valid_to"] is not None
                else None
            )
            if valid_from < latest_from or (
                latest_to is not None and valid_from < latest_to
            ):
                raise ContractViolation("Snapshot observations must not overlap or go backward")
            if latest_to is None and latest["state_hash"] == candidate["state_hash"]:
                if latest["state"] != candidate["state"]:
                    raise ContractViolation("equal state_hash has different normalized state")
                updated = copy.deepcopy(latest)
                updated["observed_at"] = _format_time(
                    max(
                        _parse_time(latest["observed_at"], "Snapshot.observed_at"),
                        observed,
                    )
                )
                updated["evidence_ids"] = _merged_ids(
                    latest["evidence_ids"], candidate["evidence_ids"]
                )
                validate_graph_record(updated)
                history[-1] = updated
                return copy.deepcopy(updated)
            if latest_to is None:
                closed = copy.deepcopy(latest)
                closed["valid_to"] = candidate["valid_from"]
                validate_graph_record(closed)
                history[-1] = closed
            history.append(candidate)
            return copy.deepcopy(candidate)

    def append_or_extend_relation(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        candidate = copy.deepcopy(dict(record))
        validate_graph_record(candidate)
        if candidate["record_type"] != "relation_interval":
            raise ContractViolation(
                "append_or_extend_relation requires a relation_interval record"
            )
        observed = _parse_time(candidate["observed_at"], "Relation.observed_at")
        valid_from = _parse_time(candidate["valid_from"], "Relation.valid_from")
        valid_to = (
            _parse_time(candidate["valid_to"], "Relation.valid_to")
            if candidate["valid_to"] is not None
            else None
        )
        if valid_from > observed or (valid_to is not None and valid_from > valid_to):
            raise ContractViolation("Relation interval timestamps are inconsistent")
        identity_fields = (
            "source_entity_id",
            "relation_type",
            "destination_entity_id",
            "reference_key",
            "projector",
        )

        with self._lock:
            self._require_entity(candidate["source_entity_id"])
            self._require_entity(candidate["destination_entity_id"])
            history = self._relations_by_key.setdefault(candidate["relation_key"], [])
            if history:
                latest = history[-1]
                if any(latest[field] != candidate[field] for field in identity_fields):
                    raise ContractViolation(
                        "relation_key collision with different relation identity"
                    )
                latest_from = _parse_time(latest["valid_from"], "Relation.valid_from")
                latest_to = (
                    _parse_time(latest["valid_to"], "Relation.valid_to")
                    if latest["valid_to"] is not None
                    else None
                )
                if valid_from < latest_from or (
                    latest_to is not None and valid_from < latest_to
                ):
                    raise ContractViolation(
                        "Relation observations must not overlap or go backward"
                    )
                if latest_to is None:
                    updated = copy.deepcopy(latest)
                    updated["observed_at"] = _format_time(
                        max(
                            _parse_time(
                                latest["observed_at"], "Relation.observed_at"
                            ),
                            observed,
                        )
                    )
                    updated["evidence_ids"] = _merged_ids(
                        latest["evidence_ids"], candidate["evidence_ids"]
                    )
                    validate_graph_record(updated)
                    history[-1] = updated
                    return copy.deepcopy(updated)
            history.append(candidate)
            return copy.deepcopy(candidate)

    def close_relation(self, relation_key: str, *, observed_at: datetime) -> Dict[str, Any]:
        closed_at = _format_time(observed_at)
        with self._lock:
            history = self._relations_by_key.get(relation_key)
            if not history or history[-1]["valid_to"] is not None:
                raise KeyError(f"no active relation: {relation_key}")
            latest = history[-1]
            if observed_at.astimezone(timezone.utc) < _parse_time(
                latest["valid_from"], "Relation.valid_from"
            ):
                raise ContractViolation("Relation cannot close before valid_from")
            updated = copy.deepcopy(latest)
            updated["valid_to"] = closed_at
            updated["observed_at"] = closed_at
            validate_graph_record(updated)
            history[-1] = updated
            return copy.deepcopy(updated)

    def upsert_event_aggregate(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        candidate = copy.deepcopy(dict(record))
        validate_graph_record(candidate)
        if candidate["record_type"] != "event_aggregate":
            raise ContractViolation(
                "upsert_event_aggregate requires an event_aggregate record"
            )
        first = _parse_time(candidate["first_seen_at"], "Event.first_seen_at")
        last = _parse_time(candidate["last_seen_at"], "Event.last_seen_at")
        if first > last:
            raise ContractViolation("Event first_seen_at must not follow last_seen_at")

        with self._lock:
            self._require_entity(candidate["entity_id"])
            existing = self._events.get(candidate["event_id"])
            if existing is None:
                self._events[candidate["event_id"]] = candidate
                return copy.deepcopy(candidate)
            identity_fields = ("entity_id", "event_type", "attributes")
            if any(existing[field] != candidate[field] for field in identity_fields):
                raise ContractViolation(
                    f"event_id collision with different identity: {candidate['event_id']}"
                )
            updated = copy.deepcopy(existing)
            updated["first_seen_at"] = _format_time(
                min(_parse_time(existing["first_seen_at"], "Event.first_seen_at"), first)
            )
            updated["last_seen_at"] = _format_time(
                max(_parse_time(existing["last_seen_at"], "Event.last_seen_at"), last)
            )
            updated["count"] = max(existing["count"], candidate["count"])
            updated["evidence_ids"] = _merged_ids(
                existing["evidence_ids"], candidate["evidence_ids"]
            )
            validate_graph_record(updated)
            self._events[candidate["event_id"]] = updated
            return copy.deepcopy(updated)

    def get_entity(self, entity_id: str) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._require_entity(entity_id))

    def list_snapshots(self, entity_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._require_snapshot_history(entity_id))

    def list_relations(self) -> List[Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(
                [
                    relation
                    for key in sorted(self._relations_by_key)
                    for relation in self._relations_by_key[key]
                ]
            )

    def list_events(self, entity_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            values = [
                event
                for event in self._events.values()
                if entity_id is None or event["entity_id"] == entity_id
            ]
            return copy.deepcopy(sorted(values, key=lambda item: item["event_id"]))

    def find_state_paths(self, scope: InvestigationScope) -> GraphLocalization:
        """Return a deterministic breadth-first subgraph bounded by the scope."""

        with self._lock:
            domain_filter = set(scope.domains)
            candidates = {
                entity_id: copy.deepcopy(entity)
                for entity_id, entity in self._entities.items()
                if not domain_filter or entity["domain"] in domain_filter
            }
            for seed in scope.seed_entity_ids:
                if seed not in candidates:
                    raise ContractViolation(
                        f"InvestigationScope seed is absent from the selected Graph: {seed}"
                    )

            relations = [
                copy.deepcopy(relation)
                for history in self._relations_by_key.values()
                for relation in history
                if relation["source_entity_id"] in candidates
                and relation["destination_entity_id"] in candidates
                and _overlaps(relation["valid_from"], relation["valid_to"], scope.window)
                and (
                    not scope.relation_types
                    or relation["relation_type"] in scope.relation_types
                )
            ]
            relations.sort(
                key=lambda item: (
                    item["relation_type"],
                    item["source_entity_id"],
                    item["destination_entity_id"],
                    item["relation_id"],
                )
            )
            adjacency: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {
                entity_id: [] for entity_id in candidates
            }
            for relation in relations:
                adjacency[relation["source_entity_id"]].append(
                    (relation["destination_entity_id"], relation)
                )
                adjacency[relation["destination_entity_id"]].append(
                    (relation["source_entity_id"], relation)
                )

            visited = set()
            paths: List[LocalizedPath] = []
            queue = deque()
            for seed in scope.seed_entity_ids:
                if len(visited) >= scope.max_entities:
                    break
                visited.add(seed)
                seed_evidence = self._entity_evidence(seed, scope.window)
                path = LocalizedPath((seed,), tuple(), tuple(sorted(seed_evidence)))
                paths.append(path)
                queue.append((seed, (seed,), tuple(), frozenset(seed_evidence), 0))

            while queue and len(visited) < scope.max_entities:
                current, entity_path, relation_path, evidence_path, depth = queue.popleft()
                if depth >= scope.max_depth:
                    continue
                for neighbor, relation in adjacency[current]:
                    if neighbor in visited:
                        continue
                    visited.add(neighbor)
                    evidence = set(evidence_path)
                    evidence.update(relation["evidence_ids"])
                    evidence.update(self._entity_evidence(neighbor, scope.window))
                    next_entities = entity_path + (neighbor,)
                    next_relations = relation_path + (relation["relation_type"],)
                    localized_path = LocalizedPath(
                        next_entities,
                        next_relations,
                        tuple(sorted(evidence)),
                    )
                    paths.append(localized_path)
                    queue.append(
                        (
                            neighbor,
                            next_entities,
                            next_relations,
                            frozenset(evidence),
                            depth + 1,
                        )
                    )
                    if len(visited) >= scope.max_entities:
                        break

            localized_entities = {
                entity_id: candidates[entity_id] for entity_id in sorted(visited)
            }
            evidence_ids = sorted(
                {
                    evidence_id
                    for path in paths
                    for evidence_id in path.evidence_ids
                }
            )
            covered = sum(
                bool(self._entity_evidence(entity_id, scope.window))
                for entity_id in visited
            )
            entity_coverage = covered / len(visited) if visited else 0.0
            recent = self._recent_change_evidence(visited, scope.window)
            return GraphLocalization(
                candidate_entities_before=len(candidates),
                entities=localized_entities,
                paths=tuple(paths),
                evidence_ids=tuple(evidence_ids),
                recent_change_evidence_ids=tuple(sorted(recent)),
                entity_coverage=entity_coverage,
            )

    def _entity_evidence(self, entity_id: str, window: EvidenceWindow) -> set[str]:
        # Entity Evidence proves identity, but it may span many incidents after
        # upserts. Only time-bounded state/event records enter a Context Package.
        evidence: set[str] = set()
        for snapshot in self._snapshots_by_entity[entity_id]:
            if _overlaps(snapshot["valid_from"], snapshot["valid_to"], window):
                evidence.update(snapshot["evidence_ids"])
        for event in self._events.values():
            if event["entity_id"] != entity_id:
                continue
            if _overlaps(event["first_seen_at"], event["last_seen_at"], window):
                evidence.update(event["evidence_ids"])
        return evidence

    def _recent_change_evidence(
        self, entity_ids: set[str], window: EvidenceWindow
    ) -> set[str]:
        start = _parse_time(window.start, "time_window.start")
        end = _parse_time(window.end, "time_window.end")
        evidence: set[str] = set()
        for entity_id in entity_ids:
            for snapshot in self._snapshots_by_entity[entity_id]:
                changed_at = _parse_time(snapshot["valid_from"], "Snapshot.valid_from")
                if start <= changed_at <= end:
                    evidence.update(snapshot["evidence_ids"])
        for history in self._relations_by_key.values():
            for relation in history:
                if (
                    relation["source_entity_id"] not in entity_ids
                    or relation["destination_entity_id"] not in entity_ids
                ):
                    continue
                changed_at = _parse_time(relation["valid_from"], "Relation.valid_from")
                if start <= changed_at <= end:
                    evidence.update(relation["evidence_ids"])
        for event in self._events.values():
            if event["entity_id"] not in entity_ids:
                continue
            changed_at = _parse_time(event["first_seen_at"], "Event.first_seen_at")
            if start <= changed_at <= end:
                evidence.update(event["evidence_ids"])
        return evidence

    def _require_entity(self, entity_id: str) -> Dict[str, Any]:
        try:
            return self._entities[entity_id]
        except KeyError as error:
            raise ContractViolation(f"Graph record references unknown Entity: {entity_id}") from error

    def _require_snapshot_history(self, entity_id: str) -> List[Dict[str, Any]]:
        self._require_entity(entity_id)
        return self._snapshots_by_entity[entity_id]


class GraphLocalizer:
    """Freeze a bounded Graph localization result into a Context Package."""

    def __init__(self, repository: InMemoryStateGraphRepository) -> None:
        self._repository = repository

    def build_context(
        self,
        scope: InvestigationScope,
        evidence: Sequence[Mapping[str, Any]],
        *,
        frozen_at: datetime,
        collector_failures: Sequence[Mapping[str, str]] = (),
    ) -> Dict[str, Any]:
        frozen_at_text = _format_time(frozen_at)
        evidence_by_id: Dict[str, Mapping[str, Any]] = {}
        for item in evidence:
            validate_contract("evidence-item.schema.json", item)
            if item["incident_id"] != scope.incident_id:
                raise ContractViolation(
                    "Graph localization Evidence belongs to a different Incident"
                )
            evidence_by_id[item["evidence_id"]] = item

        localized = self._repository.find_state_paths(scope)
        graph_evidence = set(localized.evidence_ids)
        available = graph_evidence & set(evidence_by_id)
        if not available:
            raise ContractViolation(
                "Graph localization did not retain any stored Evidence references"
            )
        missing_ids = sorted(graph_evidence - set(evidence_by_id))
        reference_coverage = len(available) / len(graph_evidence) if graph_evidence else 0.0
        completeness = round(localized.entity_coverage * reference_coverage, 6)

        source = localized.entities[scope.seed_entity_ids[0]]
        state_paths = []
        for path in localized.paths:
            path_evidence = sorted(set(path.evidence_ids) & available)
            state_paths.append(
                {
                    "path_id": stable_graph_id(
                        "path",
                        {
                            "entities": path.entity_ids,
                            "relations": path.relation_types,
                        },
                    ),
                    "entities": [
                        self._entity_ref(localized.entities[entity_id])
                        for entity_id in path.entity_ids
                    ],
                    "relations": list(path.relation_types),
                    "evidence_ids": path_evidence,
                }
            )
        failure_items = [dict(item) for item in collector_failures]
        context_identity = {
            "scope": scope.to_contract(),
            "frozen_at": frozen_at_text,
            "evidence_ids": sorted(available),
            "paths": [path["path_id"] for path in state_paths],
        }
        context = {
            "schema_version": "1.0.0",
            "context_id": stable_graph_id("ctx", context_identity),
            "incident_id": scope.incident_id,
            "frozen_at": frozen_at_text,
            "source_entity": self._entity_ref(source),
            "scope": scope.to_contract(),
            "state_paths": state_paths,
            "evidence_ids": sorted(available),
            "recent_change_evidence_ids": sorted(
                set(localized.recent_change_evidence_ids) & available
            ),
            "missing_evidence": [
                {
                    "source": "stategraph",
                    "reason": f"Graph record references unavailable Evidence {evidence_id}",
                }
                for evidence_id in missing_ids
            ],
            "collector_failures": failure_items,
            "localization": {
                "strategy": "stategraph",
                "candidate_entities_before": localized.candidate_entities_before,
                "candidate_entities_after": len(localized.entities),
                "context_completeness": completeness,
            },
        }
        validate_contract("context-package.schema.json", context)
        return context

    @staticmethod
    def _entity_ref(entity: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "entity_id": entity["entity_id"],
            "entity_type": entity["entity_type"],
            "domain": entity["domain"],
            "name": entity["name"],
            "scope": copy.deepcopy(entity["scope"]),
            "external_ref": entity["external_ref"],
            "exists": entity["exists"],
        }
