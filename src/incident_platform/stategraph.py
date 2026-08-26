"""Domain-neutral temporal StateGraph contracts, storage, and localization."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

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


@dataclass(frozen=True)
class EntityIdentity:
    """Versioned identity material kept separate from mutable Entity state."""

    identity_type: str
    domain: str
    keys: Mapping[str, str]
    version: str = "1.0.0"

    _REQUIRED_KEYS = {
        "kubernetes-resource": frozenset({"cluster_id", "uid"}),
        "kubernetes-placeholder": frozenset(
            {"cluster_id", "api_version", "kind", "namespace", "name"}
        ),
        "logical-service": frozenset(
            {"cluster_id", "namespace", "service_name"}
        ),
        "external": frozenset({"external_key"}),
    }

    def __post_init__(self) -> None:
        object.__setattr__(self, "keys", dict(self.keys))
        if self.version != "1.0.0":
            raise ContractViolation("unsupported EntityIdentity version")
        required = self._REQUIRED_KEYS.get(self.identity_type)
        if required is None:
            raise ContractViolation(
                f"unsupported EntityIdentity type: {self.identity_type}"
            )
        if set(self.keys) != required:
            raise ContractViolation(
                f"{self.identity_type} identity requires exactly "
                f"{', '.join(sorted(required))}"
            )
        if not self.domain:
            raise ContractViolation("EntityIdentity domain is required")
        if any(not isinstance(value, str) or not value for value in self.keys.values()):
            raise ContractViolation("EntityIdentity key values must be non-empty strings")

    @classmethod
    def from_contract(cls, value: Mapping[str, Any]) -> "EntityIdentity":
        try:
            return cls(
                version=value["version"],
                identity_type=value["identity_type"],
                domain=value["domain"],
                keys=value["keys"],
            )
        except (KeyError, TypeError) as error:
            raise ContractViolation("Entity identity is malformed") from error

    @classmethod
    def kubernetes_resource(cls, *, cluster_id: str, uid: str) -> "EntityIdentity":
        return cls(
            identity_type="kubernetes-resource",
            domain="kubernetes",
            keys={"cluster_id": cluster_id, "uid": uid},
        )

    @classmethod
    def kubernetes_placeholder(
        cls,
        *,
        cluster_id: str,
        api_version: str,
        kind: str,
        namespace: str,
        name: str,
    ) -> "EntityIdentity":
        return cls(
            identity_type="kubernetes-placeholder",
            domain="kubernetes",
            keys={
                "cluster_id": cluster_id,
                "api_version": api_version,
                "kind": kind,
                "namespace": namespace,
                "name": name,
            },
        )

    @classmethod
    def logical_service(
        cls, *, cluster_id: str, namespace: str, service_name: str
    ) -> "EntityIdentity":
        return cls(
            identity_type="logical-service",
            domain="web-service",
            keys={
                "cluster_id": cluster_id,
                "namespace": namespace,
                "service_name": service_name,
            },
        )

    @classmethod
    def external(cls, *, domain: str, external_key: str) -> "EntityIdentity":
        return cls(
            identity_type="external",
            domain=domain,
            keys={"external_key": external_key},
        )

    def to_contract(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "identity_type": self.identity_type,
            "domain": self.domain,
            "keys": dict(self.keys),
        }

    @property
    def entity_id(self) -> str:
        return stable_graph_id("ent", self.to_contract())


def validate_graph_record(record: Mapping[str, Any]) -> None:
    """Validate structure and reject content that still needs redaction."""

    validate_contract("graph-record.schema.json", record)
    if record.get("record_type") == "entity":
        identity = EntityIdentity.from_contract(record["identity"])
        if identity.domain != record["domain"]:
            raise ContractViolation("Entity identity domain does not match Entity domain")
        if identity.entity_id != record["entity_id"]:
            raise ContractViolation("Entity entity_id does not match EntityIdentity")
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
class EntityLookup:
    """Exact, time-bounded Entity lookup contract used by resolvers."""

    cluster_id: str
    namespace: str
    name: str
    window: EvidenceWindow
    domains: Tuple[str, ...] = field(default_factory=tuple)
    entity_types: Tuple[str, ...] = field(default_factory=tuple)
    identity_types: Tuple[str, ...] = field(default_factory=tuple)
    include_placeholders: bool = False
    limit: int = 10

    def __post_init__(self) -> None:
        object.__setattr__(self, "domains", tuple(self.domains))
        object.__setattr__(self, "entity_types", tuple(self.entity_types))
        object.__setattr__(self, "identity_types", tuple(self.identity_types))
        if not self.cluster_id or not self.namespace or not self.name:
            raise ContractViolation(
                "EntityLookup cluster_id, namespace, and name are required"
            )
        if not 1 <= self.limit <= 100:
            raise ContractViolation("EntityLookup limit must be between 1 and 100")
        if _parse_time(self.window.start, "EntityLookup.window.start") > _parse_time(
            self.window.end, "EntityLookup.window.end"
        ):
            raise ContractViolation("EntityLookup start must not follow end")


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


@dataclass(frozen=True)
class StateGraphRetentionPolicy:
    """Hot-history and Incident pin lifetimes for a persistent StateGraph."""

    ordinary_history: timedelta = timedelta(hours=72)
    incident_pinned_history: timedelta = timedelta(days=30)

    def __post_init__(self) -> None:
        if self.ordinary_history <= timedelta(0):
            raise ValueError("ordinary StateGraph history must be positive")
        if self.incident_pinned_history <= self.ordinary_history:
            raise ValueError(
                "Incident-pinned StateGraph history must exceed ordinary history"
            )


@dataclass(frozen=True)
class IncidentHistoryPin:
    """A bounded retention lease for the Entities used by one frozen Context."""

    incident_id: str
    entity_ids: Tuple[str, ...]
    window: EvidenceWindow
    pinned_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class StateGraphPruneResult:
    """Deletion counts from one bounded persistent Graph garbage-collection pass."""

    expired_pins: int = 0
    snapshot_intervals: int = 0
    relation_intervals: int = 0
    event_aggregates: int = 0
    unreferenced_entities: int = 0


@dataclass(frozen=True)
class StateGraphReconciliationScope:
    """Authoritative bounded ownership for one complete projection cycle."""

    cluster_id: str
    namespace: str
    resource_names: Tuple[str, ...]
    resource_name_prefixes: Tuple[str, ...]
    projector: str
    managed_entity_types: Tuple[str, ...]
    managed_relation_types: Tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "resource_names",
            "managed_entity_types",
            "managed_relation_types",
        ):
            values = tuple(getattr(self, field_name))
            object.__setattr__(self, field_name, values)
            if not values or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ContractViolation(
                    f"StateGraphReconciliationScope.{field_name} must be non-empty"
                )
            if len(values) != len(set(values)):
                raise ContractViolation(
                    f"StateGraphReconciliationScope.{field_name} must be unique"
                )
        prefixes = tuple(self.resource_name_prefixes)
        object.__setattr__(self, "resource_name_prefixes", prefixes)
        if any(
            not isinstance(prefix, str) or not prefix.strip() for prefix in prefixes
        ):
            raise ContractViolation(
                "StateGraphReconciliationScope.resource_name_prefixes must not "
                "contain empty values"
            )
        if len(prefixes) != len(set(prefixes)):
            raise ContractViolation(
                "StateGraphReconciliationScope.resource_name_prefixes must be unique"
            )
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.cluster_id, self.namespace, self.projector)
        ):
            raise ContractViolation(
                "StateGraph reconciliation cluster, namespace, and projector are required"
            )
        allowed_prefixes = {f"{name}-" for name in self.resource_names}
        if set(self.resource_name_prefixes) - allowed_prefixes:
            raise ContractViolation(
                "StateGraph reconciliation prefixes must derive from exact roots"
            )

    def contains_name(self, name: object) -> bool:
        return isinstance(name, str) and (
            name in self.resource_names
            or any(name.startswith(prefix) for prefix in self.resource_name_prefixes)
        )

    def owns_entity(self, entity: Mapping[str, Any]) -> bool:
        scope = entity.get("scope", {})
        return (
            isinstance(scope, Mapping)
            and scope.get("cluster_id") == self.cluster_id
            and scope.get("namespace") == self.namespace
            and entity.get("entity_type") in self.managed_entity_types
            and self.contains_name(entity.get("name"))
        )


@dataclass(frozen=True)
class StateGraphReconciliationResult:
    """Bounded mutation counts from one complete reconciliation transaction."""

    ingested_records: int
    current_entities: int
    current_relations: int
    retired_entities: int
    closed_snapshot_intervals: int
    closed_relation_intervals: int


@dataclass(frozen=True)
class _StateGraphReconciliationPlan:
    grouped: Mapping[str, Tuple[Dict[str, Any], ...]]
    current_entity_ids: frozenset[str]
    current_snapshot_entity_ids: frozenset[str]
    current_relation_keys: frozenset[str]
    observed_at: datetime

    @property
    def record_count(self) -> int:
        return sum(len(records) for records in self.grouped.values())


def _group_graph_records(
    records: Sequence[Mapping[str, Any]],
) -> Dict[str, Tuple[Dict[str, Any], ...]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {
        "entity": [],
        "snapshot_interval": [],
        "relation_interval": [],
        "event_aggregate": [],
    }
    for record in records:
        candidate = copy.deepcopy(dict(record))
        validate_graph_record(candidate)
        record_type = candidate.get("record_type")
        if record_type not in grouped:
            raise ContractViolation(f"unsupported Graph record type: {record_type}")
        grouped[record_type].append(candidate)
    return {key: tuple(value) for key, value in grouped.items()}


def _build_reconciliation_plan(
    records: Sequence[Mapping[str, Any]],
    scope: StateGraphReconciliationScope,
    observed_at: datetime,
) -> _StateGraphReconciliationPlan:
    observed_text = _format_time(observed_at)
    observed_utc = _parse_time(
        observed_text, "StateGraph reconciliation observed_at"
    )
    grouped = _group_graph_records(records)
    if grouped["event_aggregate"]:
        raise ContractViolation(
            "StateGraph reconciliation accepts state and relation records only"
        )
    entities_by_id: Dict[str, Dict[str, Any]] = {}
    for entity in grouped["entity"]:
        entities_by_id[entity["entity_id"]] = entity
        if _parse_time(entity["last_seen_at"], "Entity.last_seen_at") > observed_utc:
            raise ContractViolation(
                "StateGraph reconciliation cannot precede Entity observation"
            )

    current_entity_ids = frozenset(
        entity_id
        for entity_id, entity in entities_by_id.items()
        if scope.owns_entity(entity)
    )
    current_snapshot_entity_ids = set()
    for snapshot in grouped["snapshot_interval"]:
        entity = entities_by_id.get(snapshot["entity_id"])
        if entity is None:
            raise ContractViolation(
                "StateGraph reconciliation Snapshot requires its Entity in the cycle"
            )
        if _parse_time(snapshot["observed_at"], "Snapshot.observed_at") > observed_utc:
            raise ContractViolation(
                "StateGraph reconciliation cannot precede Snapshot observation"
            )
        if scope.owns_entity(entity):
            current_snapshot_entity_ids.add(snapshot["entity_id"])

    current_relation_keys = set()
    for relation in grouped["relation_interval"]:
        source = entities_by_id.get(relation["source_entity_id"])
        if source is None or not scope.owns_entity(source):
            raise ContractViolation(
                "StateGraph reconciliation Relation source is outside ownership scope"
            )
        if relation["projector"] != scope.projector:
            raise ContractViolation(
                "StateGraph reconciliation Relation projector does not match scope"
            )
        if relation["relation_type"] not in scope.managed_relation_types:
            raise ContractViolation(
                "StateGraph reconciliation Relation type is outside managed scope"
            )
        if _parse_time(relation["observed_at"], "Relation.observed_at") > observed_utc:
            raise ContractViolation(
                "StateGraph reconciliation cannot precede Relation observation"
            )
        current_relation_keys.add(relation["relation_key"])

    if not current_entity_ids or not current_snapshot_entity_ids:
        raise ContractViolation(
            "StateGraph reconciliation requires a non-empty complete state projection"
        )
    return _StateGraphReconciliationPlan(
        grouped=grouped,
        current_entity_ids=current_entity_ids,
        current_snapshot_entity_ids=frozenset(current_snapshot_entity_ids),
        current_relation_keys=frozenset(current_relation_keys),
        observed_at=observed_utc,
    )


@runtime_checkable
class StateGraphRepository(Protocol):
    """Storage port required by projection and bounded Graph localization.

    Persistent adapters may use a Graph database, a relational database, or
    another store internally. Callers depend only on these bounded operations
    and never issue backend-specific query language directly.
    """

    def ingest(self, records: Sequence[Mapping[str, Any]]) -> None:
        ...

    def find_entities(self, lookup: EntityLookup) -> Tuple[Mapping[str, Any], ...]:
        ...

    def find_state_paths(self, scope: InvestigationScope) -> GraphLocalization:
        ...


@runtime_checkable
class StateGraphHistoryRepository(Protocol):
    """Optional persistent-history operations kept outside the localization Port."""

    def pin_incident_history(
        self,
        scope: InvestigationScope,
        entity_ids: Sequence[str],
        *,
        pinned_at: datetime,
    ) -> IncidentHistoryPin:
        ...

    def prune_history(
        self,
        *,
        now: datetime,
        batch_size: int = 1000,
    ) -> StateGraphPruneResult:
        ...


@runtime_checkable
class StateGraphReconciliationRepository(Protocol):
    """Atomic complete-set reconciliation kept outside localization reads."""

    def reconcile_projection(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        scope: StateGraphReconciliationScope,
        observed_at: datetime,
    ) -> StateGraphReconciliationResult:
        ...


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

        grouped = _group_graph_records(records)
        self._ingest_grouped(grouped)

    def _ingest_grouped(
        self,
        grouped: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        replace_evidence_ids: bool = False,
    ) -> None:
        for record in grouped["entity"]:
            self.upsert_entity(
                record, replace_evidence_ids=replace_evidence_ids
            )
        for record in grouped["snapshot_interval"]:
            self.append_or_extend_snapshot(
                record, replace_evidence_ids=replace_evidence_ids
            )
        for record in grouped["relation_interval"]:
            self.append_or_extend_relation(
                record, replace_evidence_ids=replace_evidence_ids
            )
        for record in grouped["event_aggregate"]:
            self.upsert_event_aggregate(record)

    def reconcile_projection(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        scope: StateGraphReconciliationScope,
        observed_at: datetime,
    ) -> StateGraphReconciliationResult:
        plan = _build_reconciliation_plan(records, scope, observed_at)
        observed_text = _format_time(plan.observed_at)
        with self._lock:
            backup = (
                copy.deepcopy(self._entities),
                copy.deepcopy(self._snapshots_by_entity),
                copy.deepcopy(self._relations_by_key),
                copy.deepcopy(self._events),
            )
            try:
                self._ingest_grouped(
                    plan.grouped,
                    replace_evidence_ids=True,
                )
                retired_entities = 0
                closed_snapshots = 0
                for entity_id, entity in tuple(self._entities.items()):
                    if (
                        not scope.owns_entity(entity)
                        or entity_id in plan.current_entity_ids
                    ):
                        continue
                    if plan.observed_at < _parse_time(
                        entity["last_seen_at"], "Entity.last_seen_at"
                    ):
                        raise ContractViolation(
                            "StateGraph reconciliation observations went backward"
                        )
                    if entity["exists"]:
                        updated_entity = copy.deepcopy(entity)
                        updated_entity["exists"] = False
                        updated_entity["last_seen_at"] = observed_text
                        validate_graph_record(updated_entity)
                        self._entities[entity_id] = updated_entity
                        retired_entities += 1
                    history = self._snapshots_by_entity.get(entity_id, [])
                    if history and history[-1]["valid_to"] is None:
                        latest = copy.deepcopy(history[-1])
                        if plan.observed_at < _parse_time(
                            latest["valid_from"], "Snapshot.valid_from"
                        ):
                            raise ContractViolation(
                                "Snapshot cannot close before valid_from"
                            )
                        latest["valid_to"] = observed_text
                        latest["observed_at"] = observed_text
                        validate_graph_record(latest)
                        history[-1] = latest
                        closed_snapshots += 1

                closed_relations = 0
                for relation_key, history in self._relations_by_key.items():
                    latest = history[-1]
                    source = self._entities.get(latest["source_entity_id"])
                    if (
                        latest["valid_to"] is not None
                        or latest["projector"] != scope.projector
                        or latest["relation_type"] not in scope.managed_relation_types
                        or source is None
                        or not scope.owns_entity(source)
                        or relation_key in plan.current_relation_keys
                    ):
                        continue
                    if plan.observed_at < _parse_time(
                        latest["valid_from"], "Relation.valid_from"
                    ):
                        raise ContractViolation(
                            "Relation cannot close before valid_from"
                        )
                    updated_relation = copy.deepcopy(latest)
                    updated_relation["valid_to"] = observed_text
                    updated_relation["observed_at"] = observed_text
                    validate_graph_record(updated_relation)
                    history[-1] = updated_relation
                    closed_relations += 1
            except Exception:
                (
                    self._entities,
                    self._snapshots_by_entity,
                    self._relations_by_key,
                    self._events,
                ) = backup
                raise
        return StateGraphReconciliationResult(
            ingested_records=plan.record_count,
            current_entities=len(plan.current_entity_ids),
            current_relations=len(plan.current_relation_keys),
            retired_entities=retired_entities,
            closed_snapshot_intervals=closed_snapshots,
            closed_relation_intervals=closed_relations,
        )

    def upsert_entity(
        self,
        record: Mapping[str, Any],
        *,
        replace_evidence_ids: bool = False,
    ) -> Dict[str, Any]:
        candidate = copy.deepcopy(dict(record))
        validate_graph_record(candidate)
        if candidate["record_type"] != "entity":
            raise ContractViolation("upsert_entity requires an entity record")
        first = _parse_time(candidate["first_seen_at"], "Entity.first_seen_at")
        last = _parse_time(candidate["last_seen_at"], "Entity.last_seen_at")
        if first > last:
            raise ContractViolation("Entity first_seen_at must not follow last_seen_at")
        identity_fields = (
            "identity",
            "entity_type",
            "domain",
            "name",
            "scope",
            "external_ref",
        )

        with self._lock:
            existing = self._entities.get(candidate["entity_id"])
            if existing is None:
                self._entities[candidate["entity_id"]] = candidate
                self._snapshots_by_entity[candidate["entity_id"]] = []
                self._reconcile_kubernetes_placeholder(candidate)
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
            updated["evidence_ids"] = (
                copy.deepcopy(candidate["evidence_ids"])
                if replace_evidence_ids
                else _merged_ids(
                    existing["evidence_ids"], candidate["evidence_ids"]
                )
            )
            validate_graph_record(updated)
            self._entities[candidate["entity_id"]] = updated
            self._reconcile_kubernetes_placeholder(updated)
            return copy.deepcopy(updated)

    def _reconcile_kubernetes_placeholder(self, entity: Mapping[str, Any]) -> None:
        identity = EntityIdentity.from_contract(entity["identity"])
        if identity.identity_type not in {
            "kubernetes-resource",
            "kubernetes-placeholder",
        }:
            return
        for other in tuple(self._entities.values()):
            if other["entity_id"] == entity["entity_id"]:
                continue
            other_identity = EntityIdentity.from_contract(other["identity"])
            pair = {identity.identity_type, other_identity.identity_type}
            if pair != {"kubernetes-resource", "kubernetes-placeholder"}:
                continue
            placeholder, resource = (
                (entity, other)
                if identity.identity_type == "kubernetes-placeholder"
                else (other, entity)
            )
            if not self._same_kubernetes_coordinates(placeholder, resource):
                continue
            observed_at = _format_time(
                max(
                    _parse_time(placeholder["last_seen_at"], "Entity.last_seen_at"),
                    _parse_time(resource["last_seen_at"], "Entity.last_seen_at"),
                )
            )
            valid_from = _format_time(
                max(
                    _parse_time(placeholder["first_seen_at"], "Entity.first_seen_at"),
                    _parse_time(resource["first_seen_at"], "Entity.first_seen_at"),
                )
            )
            relation_identity = {
                "source_entity_id": placeholder["entity_id"],
                "relation_type": "RESOLVES_TO",
                "destination_entity_id": resource["entity_id"],
                "reference_key": placeholder["entity_id"],
                "projector": "stategraph-identity-reconciler",
            }
            relation_key = stable_graph_id("relkey", relation_identity)
            self.append_or_extend_relation(
                {
                    "record_type": "relation_interval",
                    "relation_id": stable_graph_id(
                        "rel",
                        {"relation_key": relation_key, "valid_from": valid_from},
                    ),
                    "relation_key": relation_key,
                    **relation_identity,
                    "observed_at": observed_at,
                    "valid_from": valid_from,
                    "valid_to": None,
                    "evidence_ids": _merged_ids(
                        placeholder["evidence_ids"], resource["evidence_ids"]
                    ),
                }
            )

    @staticmethod
    def _same_kubernetes_coordinates(
        placeholder: Mapping[str, Any], resource: Mapping[str, Any]
    ) -> bool:
        placeholder_keys = EntityIdentity.from_contract(placeholder["identity"]).keys
        return (
            placeholder_keys["cluster_id"] == resource["scope"].get("cluster_id")
            and placeholder_keys["api_version"] == resource["scope"].get("api_version")
            and placeholder_keys["kind"] == resource["entity_type"]
            and placeholder_keys["namespace"] == resource["scope"].get("namespace")
            and placeholder_keys["name"] == resource["name"]
        )

    def find_entities(self, lookup: EntityLookup) -> Tuple[Mapping[str, Any], ...]:
        """Return deterministic exact matches without exposing backend query syntax."""

        with self._lock:
            matches = []
            for entity in self._entities.values():
                identity = EntityIdentity.from_contract(entity["identity"])
                keys = identity.keys
                if entity["name"] != lookup.name:
                    continue
                cluster_id = keys.get("cluster_id") or entity["scope"].get(
                    "cluster_id"
                )
                namespace = keys.get("namespace") or entity["scope"].get("namespace")
                if cluster_id != lookup.cluster_id:
                    continue
                if namespace != lookup.namespace:
                    continue
                if lookup.domains and entity["domain"] not in lookup.domains:
                    continue
                if lookup.entity_types and entity["entity_type"] not in lookup.entity_types:
                    continue
                if (
                    lookup.identity_types
                    and identity.identity_type not in lookup.identity_types
                ):
                    continue
                if (
                    not lookup.include_placeholders
                    and identity.identity_type == "kubernetes-placeholder"
                ):
                    continue
                snapshots = self._snapshots_by_entity[entity["entity_id"]]
                if snapshots:
                    in_window = any(
                        _overlaps(item["valid_from"], item["valid_to"], lookup.window)
                        for item in snapshots
                    )
                else:
                    in_window = _overlaps(
                        entity["first_seen_at"], entity["last_seen_at"], lookup.window
                    )
                if in_window:
                    matches.append(copy.deepcopy(entity))
            matches.sort(key=lambda item: item["entity_id"])
            return tuple(matches[: lookup.limit])

    def append_or_extend_snapshot(
        self,
        record: Mapping[str, Any],
        *,
        replace_evidence_ids: bool = False,
    ) -> Dict[str, Any]:
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
                updated["evidence_ids"] = (
                    copy.deepcopy(candidate["evidence_ids"])
                    if replace_evidence_ids
                    else _merged_ids(
                        latest["evidence_ids"], candidate["evidence_ids"]
                    )
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

    def append_or_extend_relation(
        self,
        record: Mapping[str, Any],
        *,
        replace_evidence_ids: bool = False,
    ) -> Dict[str, Any]:
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
                    updated["evidence_ids"] = (
                        copy.deepcopy(candidate["evidence_ids"])
                        if replace_evidence_ids
                        else _merged_ids(
                            latest["evidence_ids"], candidate["evidence_ids"]
                        )
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

            observed_relations = [
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
            observed_relations.sort(
                key=lambda item: (
                    item["relation_type"],
                    item["source_entity_id"],
                    item["destination_entity_id"],
                    item["relation_id"],
                )
            )
            relations_by_path: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
            for relation in observed_relations:
                key = (
                    relation["source_entity_id"],
                    relation["relation_type"],
                    relation["destination_entity_id"],
                )
                existing = relations_by_path.get(key)
                if existing is None:
                    relations_by_path[key] = relation
                    continue
                existing["evidence_ids"] = _merged_ids(
                    existing["evidence_ids"], relation["evidence_ids"]
                )
            relations = list(relations_by_path.values())
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
            observed_relation_evidence: set[str] = set()
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
                    observed_relation_evidence.update(relation["evidence_ids"])
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
                | observed_relation_evidence
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
                if relation["projector"] == "krca-api-edge-evidence-projector":
                    continue
                changed_at = _parse_time(relation["valid_from"], "Relation.valid_from")
                if start <= changed_at <= end:
                    evidence.update(relation["evidence_ids"])
        for event in self._events.values():
            if event["entity_id"] not in entity_ids:
                continue
            if event["event_type"] in {
                "PROMETHEUS_METRIC_SUMMARY",
                "DEPLOYMENT_CHANGE_ABSENCE",
            }:
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

    def __init__(self, repository: StateGraphRepository) -> None:
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
        covered_paths = sum(bool(path["evidence_ids"]) for path in state_paths)
        path_coverage = covered_paths / len(state_paths) if state_paths else 0.0
        completeness = round(localized.entity_coverage * path_coverage, 6)
        missing_paths = [
            path["path_id"] for path in state_paths if not path["evidence_ids"]
        ]
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
                    "reason": (
                        "Localized path has no Evidence from the current Incident: "
                        f"{path_id}"
                    ),
                }
                for path_id in missing_paths
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
