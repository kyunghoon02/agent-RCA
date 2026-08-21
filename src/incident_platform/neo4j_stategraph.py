"""Neo4j adapter for the domain-neutral temporal StateGraph ports.

The JSON Graph record remains the validation and projector interchange contract.
This adapter stores normalized lookup fields and the complete validated document on
Neo4j nodes/relationships, while callers remain isolated from Cypher.
"""

from __future__ import annotations

import copy
import json
from collections import deque
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from .errors import ContractViolation
from .stategraph import (
    EntityIdentity,
    EntityLookup,
    GraphLocalization,
    IncidentHistoryPin,
    InvestigationScope,
    LocalizedPath,
    StateGraphPruneResult,
    StateGraphRetentionPolicy,
    _format_time,
    _merged_ids,
    _parse_time,
    stable_graph_id,
    validate_graph_record,
)


NEO4J_SCHEMA_STATEMENTS: Tuple[Tuple[str, str], ...] = (
    (
        "stategraph_entity_id",
        """
        CREATE CONSTRAINT stategraph_entity_id IF NOT EXISTS
        FOR (entity:StateGraphEntity)
        REQUIRE entity.entity_id IS UNIQUE
        """,
    ),
    (
        "stategraph_snapshot_id",
        """
        CREATE CONSTRAINT stategraph_snapshot_id IF NOT EXISTS
        FOR (snapshot:StateGraphSnapshot)
        REQUIRE snapshot.snapshot_id IS UNIQUE
        """,
    ),
    (
        "stategraph_event_id",
        """
        CREATE CONSTRAINT stategraph_event_id IF NOT EXISTS
        FOR (event:StateGraphEvent)
        REQUIRE event.event_id IS UNIQUE
        """,
    ),
    (
        "stategraph_pin_incident_id",
        """
        CREATE CONSTRAINT stategraph_pin_incident_id IF NOT EXISTS
        FOR (pin:StateGraphHistoryPin)
        REQUIRE pin.incident_id IS UNIQUE
        """,
    ),
    (
        "stategraph_entity_lookup",
        """
        CREATE INDEX stategraph_entity_lookup IF NOT EXISTS
        FOR (entity:StateGraphEntity)
        ON (entity.cluster_id, entity.namespace, entity.name)
        """,
    ),
    (
        "stategraph_relation_key",
        """
        CREATE INDEX stategraph_relation_key IF NOT EXISTS
        FOR ()-[relation:STATEGRAPH_RELATION]-()
        ON (relation.relation_key)
        """,
    ),
    (
        "stategraph_relation_id",
        """
        CREATE INDEX stategraph_relation_id IF NOT EXISTS
        FOR ()-[relation:STATEGRAPH_RELATION]-()
        ON (relation.relation_id)
        """,
    ),
    (
        "stategraph_snapshot_window",
        """
        CREATE INDEX stategraph_snapshot_window IF NOT EXISTS
        FOR (snapshot:StateGraphSnapshot)
        ON (snapshot.valid_from, snapshot.valid_to)
        """,
    ),
    (
        "stategraph_event_window",
        """
        CREATE INDEX stategraph_event_window IF NOT EXISTS
        FOR (event:StateGraphEvent)
        ON (event.first_seen_at, event.last_seen_at)
        """,
    ),
)


def create_neo4j_driver(uri: str, username: str, password: str) -> Any:
    """Create an official Neo4j driver without leaking credentials into the Port."""

    if not uri or not username or not password:
        raise ValueError("Neo4j URI, username, and password are required")
    try:
        from neo4j import GraphDatabase
    except ImportError as error:  # pragma: no cover - depends on runtime install
        raise RuntimeError("install the neo4j package to create a runtime driver") from error
    return GraphDatabase.driver(uri, auth=(username, password))


def apply_neo4j_schema(driver: Any, *, database: Optional[str] = None) -> Tuple[str, ...]:
    """Idempotently install the constraints and indexes required by this adapter."""

    session_options = {"database": database} if database is not None else {}
    with driver.session(**session_options) as session:
        for _, statement in NEO4J_SCHEMA_STATEMENTS:
            session.run(statement).consume()
    return tuple(name for name, _ in NEO4J_SCHEMA_STATEMENTS)


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_document(value: Any) -> Dict[str, Any]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, Mapping):
            return copy.deepcopy(dict(decoded))
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    raise TypeError("Neo4j document_json is not an object")


def _row_value(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError) as error:
        raise TypeError(f"Neo4j result does not contain {key}") from error


def _rows(result: Any) -> List[Any]:
    return list(result)


def _single(result: Any) -> Any:
    row = result.single()
    if row is None:
        return None
    return row


def _flatten_ids(groups: Iterable[Optional[Iterable[str]]]) -> set[str]:
    return {
        evidence_id
        for group in groups
        if group is not None
        for evidence_id in group
    }


class Neo4jStateGraphRepository:
    """Transaction-safe Neo4j implementation of StateGraph and history Ports."""

    def __init__(
        self,
        driver: Any,
        *,
        database: Optional[str] = None,
        retention_policy: Optional[StateGraphRetentionPolicy] = None,
    ) -> None:
        if driver is None:
            raise ValueError("Neo4j driver is required")
        if database is not None and not database:
            raise ValueError("Neo4j database cannot be empty")
        self._driver = driver
        self._database = database
        self._retention_policy = retention_policy or StateGraphRetentionPolicy()

    def ingest(self, records: Sequence[Mapping[str, Any]]) -> None:
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

        def work(transaction: Any) -> None:
            for entity in grouped["entity"]:
                self._upsert_entity_tx(transaction, entity)
            for snapshot in grouped["snapshot_interval"]:
                self._upsert_snapshot_tx(transaction, snapshot)
            for relation in grouped["relation_interval"]:
                self._upsert_relation_tx(transaction, relation)
            for event in grouped["event_aggregate"]:
                self._upsert_event_tx(transaction, event)

        self._execute_write(work)

    def find_entities(self, lookup: EntityLookup) -> Tuple[Mapping[str, Any], ...]:
        parameters = {
            "cluster_id": lookup.cluster_id,
            "namespace": lookup.namespace,
            "name": lookup.name,
            "domains": list(lookup.domains),
            "entity_types": list(lookup.entity_types),
            "identity_types": list(lookup.identity_types),
            "include_placeholders": lookup.include_placeholders,
            "window_start": _parse_time(lookup.window.start, "EntityLookup.window.start"),
            "window_end": _parse_time(lookup.window.end, "EntityLookup.window.end"),
            "limit": lookup.limit,
        }

        def work(transaction: Any) -> Tuple[Mapping[str, Any], ...]:
            result = transaction.run(
                """
                MATCH (entity:StateGraphEntity)
                WHERE entity.cluster_id = $cluster_id
                  AND entity.namespace = $namespace
                  AND entity.name = $name
                  AND (size($domains) = 0 OR entity.domain IN $domains)
                  AND (size($entity_types) = 0 OR entity.entity_type IN $entity_types)
                  AND (
                    size($identity_types) = 0
                    OR entity.identity_type IN $identity_types
                  )
                  AND (
                    $include_placeholders
                    OR entity.identity_type <> 'kubernetes-placeholder'
                  )
                OPTIONAL MATCH (entity)-[:HAS_SNAPSHOT]->(snapshot:StateGraphSnapshot)
                WITH entity, collect(snapshot) AS snapshots
                WHERE (
                    size(snapshots) > 0
                    AND any(snapshot IN snapshots WHERE
                      snapshot.valid_from <= $window_end
                      AND (
                        snapshot.valid_to IS NULL
                        OR snapshot.valid_to >= $window_start
                      )
                    )
                  ) OR (
                    size(snapshots) = 0
                    AND entity.first_seen_at <= $window_end
                    AND entity.last_seen_at >= $window_start
                  )
                RETURN entity.document_json AS document_json
                ORDER BY entity.entity_id
                LIMIT $limit
                """,
                **parameters,
            )
            return tuple(
                _decode_document(_row_value(row, "document_json"))
                for row in _rows(result)
            )

        return self._execute_read(work)

    def find_state_paths(self, scope: InvestigationScope) -> GraphLocalization:
        return self._execute_read(
            lambda transaction: self._find_state_paths_tx(transaction, scope)
        )

    def close_relation(
        self, relation_key: str, *, observed_at: datetime
    ) -> Dict[str, Any]:
        closed_at = _format_time(observed_at)

        def work(transaction: Any) -> Dict[str, Any]:
            row = _single(
                transaction.run(
                    """
                    MATCH ()-[relation:STATEGRAPH_RELATION]->()
                    WHERE relation.relation_key = $relation_key
                    WITH relation
                    ORDER BY relation.valid_from DESC, relation.relation_id DESC
                    LIMIT 1
                    SET relation.ingest_lock = $lock_token
                    REMOVE relation.ingest_lock
                    RETURN relation.document_json AS document_json
                    """,
                    relation_key=relation_key,
                    lock_token=uuid4().hex,
                )
            )
            if row is None:
                raise KeyError(f"no active relation: {relation_key}")
            latest = _decode_document(_row_value(row, "document_json"))
            if latest["valid_to"] is not None:
                raise KeyError(f"no active relation: {relation_key}")
            if _parse_time(closed_at, "Relation.valid_to") < _parse_time(
                latest["valid_from"], "Relation.valid_from"
            ):
                raise ContractViolation("Relation cannot close before valid_from")
            updated = copy.deepcopy(latest)
            updated["valid_to"] = closed_at
            updated["observed_at"] = closed_at
            validate_graph_record(updated)
            transaction.run(
                """
                MATCH ()-[relation:STATEGRAPH_RELATION]->()
                WHERE relation.relation_id = $relation_id
                SET relation.document_json = $document_json,
                    relation.observed_at = $observed_at,
                    relation.valid_to = $valid_to
                """,
                relation_id=updated["relation_id"],
                document_json=_json(updated),
                observed_at=_parse_time(updated["observed_at"], "Relation.observed_at"),
                valid_to=_parse_time(updated["valid_to"], "Relation.valid_to"),
            ).consume()
            return updated

        return self._execute_write(work)

    def pin_incident_history(
        self,
        scope: InvestigationScope,
        entity_ids: Sequence[str],
        *,
        pinned_at: datetime,
    ) -> IncidentHistoryPin:
        pinned_at_text = _format_time(pinned_at)
        canonical_ids = tuple(sorted(dict.fromkeys(entity_ids)))
        if not canonical_ids:
            raise ContractViolation("Incident history pin requires at least one Entity")
        if any(not entity_id.startswith("ent-") for entity_id in canonical_ids):
            raise ContractViolation("Incident history pin contains an invalid Entity ID")
        expires_at = _parse_time(
            pinned_at_text, "IncidentHistoryPin.pinned_at"
        ) + self._retention_policy.incident_pinned_history
        window_start = _parse_time(scope.window.start, "IncidentHistoryPin.window.start")
        window_end = _parse_time(scope.window.end, "IncidentHistoryPin.window.end")

        def work(transaction: Any) -> None:
            rows = _rows(
                transaction.run(
                    """
                    MATCH (entity:StateGraphEntity)
                    WHERE entity.entity_id IN $entity_ids
                    RETURN entity.entity_id AS entity_id
                    """,
                    entity_ids=list(canonical_ids),
                )
            )
            found = {_row_value(row, "entity_id") for row in rows}
            missing = sorted(set(canonical_ids) - found)
            if missing:
                raise ContractViolation(
                    "Incident history pin references unknown Entities: "
                    + ", ".join(missing)
                )
            transaction.run(
                """
                MERGE (pin:StateGraphHistoryPin {incident_id: $incident_id})
                ON CREATE SET
                  pin.window_start = $window_start,
                  pin.window_end = $window_end,
                  pin.pinned_at = $pinned_at,
                  pin.expires_at = $expires_at
                ON MATCH SET
                  pin.window_start = CASE
                    WHEN pin.window_start > $window_start THEN $window_start
                    ELSE pin.window_start
                  END,
                  pin.window_end = CASE
                    WHEN pin.window_end < $window_end THEN $window_end
                    ELSE pin.window_end
                  END,
                  pin.pinned_at = CASE
                    WHEN pin.pinned_at > $pinned_at THEN $pinned_at
                    ELSE pin.pinned_at
                  END,
                  pin.expires_at = CASE
                    WHEN pin.expires_at < $expires_at THEN $expires_at
                    ELSE pin.expires_at
                  END
                WITH pin
                UNWIND $entity_ids AS entity_id
                MATCH (entity:StateGraphEntity {entity_id: entity_id})
                MERGE (pin)-[:PINS]->(entity)
                """,
                incident_id=scope.incident_id,
                entity_ids=list(canonical_ids),
                window_start=window_start,
                window_end=window_end,
                pinned_at=_parse_time(pinned_at_text, "IncidentHistoryPin.pinned_at"),
                expires_at=expires_at,
            ).consume()

        self._execute_write(work)
        return IncidentHistoryPin(
            incident_id=scope.incident_id,
            entity_ids=canonical_ids,
            window=scope.window,
            pinned_at=_parse_time(pinned_at_text, "IncidentHistoryPin.pinned_at"),
            expires_at=expires_at,
        )

    def prune_history(
        self,
        *,
        now: datetime,
        batch_size: int = 1000,
    ) -> StateGraphPruneResult:
        now_text = _format_time(now)
        if not 1 <= batch_size <= 10_000:
            raise ValueError("StateGraph prune batch_size must be between 1 and 10000")
        now_utc = _parse_time(now_text, "StateGraphPrune.now")
        cutoff = now_utc - self._retention_policy.ordinary_history

        def work(transaction: Any) -> StateGraphPruneResult:
            expired_pins = self._delete_count(
                transaction,
                """
                MATCH (pin:StateGraphHistoryPin)
                WHERE pin.expires_at <= $now
                WITH pin ORDER BY pin.expires_at LIMIT $batch_size
                WITH collect(pin) AS doomed
                FOREACH (item IN doomed | DETACH DELETE item)
                RETURN size(doomed) AS deleted
                """,
                now=now_utc,
                batch_size=batch_size,
            )
            snapshots = self._delete_count(
                transaction,
                """
                MATCH (entity:StateGraphEntity)-[:HAS_SNAPSHOT]->
                      (snapshot:StateGraphSnapshot)
                WHERE snapshot.valid_to IS NOT NULL
                  AND snapshot.valid_to < $cutoff
                  AND NOT EXISTS {
                    MATCH (pin:StateGraphHistoryPin)-[:PINS]->(entity)
                    WHERE pin.expires_at > $now
                      AND pin.window_start <= snapshot.valid_to
                      AND pin.window_end >= snapshot.valid_from
                  }
                WITH snapshot ORDER BY snapshot.valid_to LIMIT $batch_size
                WITH collect(snapshot) AS doomed
                FOREACH (item IN doomed | DETACH DELETE item)
                RETURN size(doomed) AS deleted
                """,
                now=now_utc,
                cutoff=cutoff,
                batch_size=batch_size,
            )
            relations = self._delete_count(
                transaction,
                """
                MATCH (source:StateGraphEntity)-[relation:STATEGRAPH_RELATION]->
                      (destination:StateGraphEntity)
                WHERE relation.valid_to IS NOT NULL
                  AND relation.valid_to < $cutoff
                  AND NOT EXISTS {
                    MATCH (pin:StateGraphHistoryPin)-[:PINS]->(source),
                          (pin)-[:PINS]->(destination)
                    WHERE pin.expires_at > $now
                      AND pin.window_start <= relation.valid_to
                      AND pin.window_end >= relation.valid_from
                  }
                WITH relation ORDER BY relation.valid_to LIMIT $batch_size
                WITH collect(relation) AS doomed
                FOREACH (item IN doomed | DELETE item)
                RETURN size(doomed) AS deleted
                """,
                now=now_utc,
                cutoff=cutoff,
                batch_size=batch_size,
            )
            events = self._delete_count(
                transaction,
                """
                MATCH (entity:StateGraphEntity)-[:HAS_EVENT]->
                      (event:StateGraphEvent)
                WHERE event.last_seen_at < $cutoff
                  AND NOT EXISTS {
                    MATCH (pin:StateGraphHistoryPin)-[:PINS]->(entity)
                    WHERE pin.expires_at > $now
                      AND pin.window_start <= event.last_seen_at
                      AND pin.window_end >= event.first_seen_at
                  }
                WITH event ORDER BY event.last_seen_at LIMIT $batch_size
                WITH collect(event) AS doomed
                FOREACH (item IN doomed | DETACH DELETE item)
                RETURN size(doomed) AS deleted
                """,
                now=now_utc,
                cutoff=cutoff,
                batch_size=batch_size,
            )
            entities = self._delete_count(
                transaction,
                """
                MATCH (entity:StateGraphEntity)
                WHERE NOT (entity)--()
                  AND entity.last_seen_at < $cutoff
                WITH entity ORDER BY entity.last_seen_at LIMIT $batch_size
                WITH collect(entity) AS doomed
                FOREACH (item IN doomed | DELETE item)
                RETURN size(doomed) AS deleted
                """,
                cutoff=cutoff,
                batch_size=batch_size,
            )
            return StateGraphPruneResult(
                expired_pins=expired_pins,
                snapshot_intervals=snapshots,
                relation_intervals=relations,
                event_aggregates=events,
                unreferenced_entities=entities,
            )

        return self._execute_write(work)

    @staticmethod
    def _delete_count(transaction: Any, query: str, **parameters: Any) -> int:
        row = _single(transaction.run(query, **parameters))
        return int(_row_value(row, "deleted")) if row is not None else 0

    def _upsert_entity_tx(
        self, transaction: Any, candidate: Mapping[str, Any]
    ) -> Dict[str, Any]:
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
        properties = self._entity_properties(candidate)
        creation_token = uuid4().hex
        row = _single(
            transaction.run(
                """
                MERGE (entity:StateGraphEntity {entity_id: $entity_id})
                ON CREATE SET
                  entity.document_json = $document_json,
                  entity.identity_type = $identity_type,
                  entity.identity_version = $identity_version,
                  entity.domain = $domain,
                  entity.entity_type = $entity_type,
                  entity.name = $name,
                  entity.cluster_id = $cluster_id,
                  entity.namespace = $namespace,
                  entity.api_version = $api_version,
                  entity.first_seen_at = $first_seen_at,
                  entity.last_seen_at = $last_seen_at,
                  entity.creation_token = $creation_token
                WITH entity, entity.creation_token = $creation_token AS created
                REMOVE entity.creation_token
                RETURN entity.document_json AS document_json, created AS created
                """,
                **properties,
                creation_token=creation_token,
            )
        )
        if row is None:
            raise RuntimeError("Neo4j Entity MERGE returned no row")
        existing = _decode_document(_row_value(row, "document_json"))
        if bool(_row_value(row, "created")):
            updated = copy.deepcopy(dict(candidate))
        else:
            if any(existing[field] != candidate[field] for field in identity_fields):
                raise ContractViolation(
                    f"entity_id collision with different identity: {candidate['entity_id']}"
                )
            existing_first = _parse_time(existing["first_seen_at"], "Entity.first_seen_at")
            existing_last = _parse_time(existing["last_seen_at"], "Entity.last_seen_at")
            updated = copy.deepcopy(existing)
            updated["first_seen_at"] = _format_time(min(existing_first, first))
            updated["last_seen_at"] = _format_time(max(existing_last, last))
            if last >= existing_last:
                updated["exists"] = candidate["exists"]
            updated["evidence_ids"] = _merged_ids(
                existing["evidence_ids"], candidate["evidence_ids"]
            )
            validate_graph_record(updated)
            updated_properties = self._entity_properties(updated)
            transaction.run(
                """
                MATCH (entity:StateGraphEntity {entity_id: $entity_id})
                SET entity.document_json = $document_json,
                    entity.first_seen_at = $first_seen_at,
                    entity.last_seen_at = $last_seen_at
                """,
                **updated_properties,
            ).consume()
        self._reconcile_placeholder_tx(transaction, updated)
        return updated

    @staticmethod
    def _entity_properties(entity: Mapping[str, Any]) -> Dict[str, Any]:
        identity = EntityIdentity.from_contract(entity["identity"])
        keys = identity.keys
        scope = entity["scope"]
        return {
            "entity_id": entity["entity_id"],
            "document_json": _json(entity),
            "identity_type": identity.identity_type,
            "identity_version": identity.version,
            "domain": entity["domain"],
            "entity_type": entity["entity_type"],
            "name": entity["name"],
            "cluster_id": keys.get("cluster_id") or scope.get("cluster_id"),
            "namespace": keys.get("namespace") or scope.get("namespace"),
            "api_version": keys.get("api_version") or scope.get("api_version"),
            "first_seen_at": _parse_time(entity["first_seen_at"], "Entity.first_seen_at"),
            "last_seen_at": _parse_time(entity["last_seen_at"], "Entity.last_seen_at"),
        }

    def _reconcile_placeholder_tx(
        self, transaction: Any, entity: Mapping[str, Any]
    ) -> None:
        identity = EntityIdentity.from_contract(entity["identity"])
        if identity.identity_type not in {
            "kubernetes-resource",
            "kubernetes-placeholder",
        }:
            return
        properties = self._entity_properties(entity)
        counterpart_type = (
            "kubernetes-resource"
            if identity.identity_type == "kubernetes-placeholder"
            else "kubernetes-placeholder"
        )
        rows = _rows(
            transaction.run(
                """
                MATCH (other:StateGraphEntity)
                WHERE other.entity_id <> $entity_id
                  AND other.identity_type = $counterpart_type
                  AND other.cluster_id = $cluster_id
                  AND other.api_version = $api_version
                  AND other.entity_type = $entity_type
                  AND other.namespace = $namespace
                  AND other.name = $name
                RETURN other.document_json AS document_json
                ORDER BY other.entity_id
                """,
                entity_id=entity["entity_id"],
                counterpart_type=counterpart_type,
                cluster_id=properties["cluster_id"],
                api_version=properties["api_version"],
                entity_type=entity["entity_type"],
                namespace=properties["namespace"],
                name=entity["name"],
            )
        )
        for row in rows:
            other = _decode_document(_row_value(row, "document_json"))
            placeholder, resource = (
                (entity, other)
                if identity.identity_type == "kubernetes-placeholder"
                else (other, entity)
            )
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
            relation = {
                "record_type": "relation_interval",
                "relation_id": stable_graph_id(
                    "rel", {"relation_key": relation_key, "valid_from": valid_from}
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
            validate_graph_record(relation)
            self._upsert_relation_tx(transaction, relation)

    def _upsert_snapshot_tx(
        self, transaction: Any, candidate: Mapping[str, Any]
    ) -> Dict[str, Any]:
        observed = _parse_time(candidate["observed_at"], "Snapshot.observed_at")
        valid_from = _parse_time(candidate["valid_from"], "Snapshot.valid_from")
        valid_to = (
            _parse_time(candidate["valid_to"], "Snapshot.valid_to")
            if candidate["valid_to"] is not None
            else None
        )
        if valid_from > observed or (valid_to is not None and valid_from > valid_to):
            raise ContractViolation("Snapshot interval timestamps are inconsistent")
        row = _single(
            transaction.run(
                """
                MATCH (entity:StateGraphEntity {entity_id: $entity_id})
                SET entity.ingest_lock = $lock_token
                WITH entity
                OPTIONAL MATCH (entity)-[:HAS_SNAPSHOT]->
                      (snapshot:StateGraphSnapshot)
                WITH entity, snapshot ORDER BY snapshot.valid_from DESC,
                                               snapshot.snapshot_id DESC
                WITH entity, collect(snapshot)[0] AS latest
                REMOVE entity.ingest_lock
                RETURN latest.document_json AS document_json
                """,
                entity_id=candidate["entity_id"],
                lock_token=uuid4().hex,
            )
        )
        if row is None:
            raise ContractViolation(
                f"Graph record references unknown Entity: {candidate['entity_id']}"
            )
        latest_value = _row_value(row, "document_json")
        if latest_value is None:
            self._create_snapshot_tx(transaction, candidate)
            return copy.deepcopy(dict(candidate))
        latest = _decode_document(latest_value)
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
            transaction.run(
                """
                MATCH (snapshot:StateGraphSnapshot {snapshot_id: $snapshot_id})
                SET snapshot.document_json = $document_json,
                    snapshot.observed_at = $observed_at,
                    snapshot.evidence_ids = $evidence_ids
                """,
                snapshot_id=updated["snapshot_id"],
                document_json=_json(updated),
                observed_at=_parse_time(updated["observed_at"], "Snapshot.observed_at"),
                evidence_ids=updated["evidence_ids"],
            ).consume()
            return updated
        if latest_to is None:
            closed = copy.deepcopy(latest)
            closed["valid_to"] = candidate["valid_from"]
            validate_graph_record(closed)
            transaction.run(
                """
                MATCH (snapshot:StateGraphSnapshot {snapshot_id: $snapshot_id})
                SET snapshot.document_json = $document_json,
                    snapshot.valid_to = $valid_to
                """,
                snapshot_id=closed["snapshot_id"],
                document_json=_json(closed),
                valid_to=valid_from,
            ).consume()
        self._create_snapshot_tx(transaction, candidate)
        return copy.deepcopy(dict(candidate))

    @staticmethod
    def _create_snapshot_tx(transaction: Any, snapshot: Mapping[str, Any]) -> None:
        transaction.run(
            """
            MATCH (entity:StateGraphEntity {entity_id: $entity_id})
            CREATE (snapshot:StateGraphSnapshot {
              snapshot_id: $snapshot_id,
              document_json: $document_json,
              state_hash: $state_hash,
              evidence_ids: $evidence_ids,
              observed_at: $observed_at,
              valid_from: $valid_from,
              valid_to: $valid_to
            })
            CREATE (entity)-[:HAS_SNAPSHOT]->(snapshot)
            """,
            entity_id=snapshot["entity_id"],
            snapshot_id=snapshot["snapshot_id"],
            document_json=_json(snapshot),
            state_hash=snapshot["state_hash"],
            evidence_ids=snapshot["evidence_ids"],
            observed_at=_parse_time(snapshot["observed_at"], "Snapshot.observed_at"),
            valid_from=_parse_time(snapshot["valid_from"], "Snapshot.valid_from"),
            valid_to=(
                _parse_time(snapshot["valid_to"], "Snapshot.valid_to")
                if snapshot["valid_to"] is not None
                else None
            ),
        ).consume()

    def _lock_entities_tx(
        self, transaction: Any, entity_ids: Sequence[str]
    ) -> None:
        canonical = sorted(set(entity_ids))
        rows = _rows(
            transaction.run(
                """
                MATCH (entity:StateGraphEntity)
                WHERE entity.entity_id IN $entity_ids
                SET entity.ingest_lock = $lock_token
                WITH entity
                REMOVE entity.ingest_lock
                RETURN entity.entity_id AS entity_id
                ORDER BY entity.entity_id
                """,
                entity_ids=canonical,
                lock_token=uuid4().hex,
            )
        )
        found = {_row_value(row, "entity_id") for row in rows}
        missing = sorted(set(canonical) - found)
        if missing:
            raise ContractViolation(
                "Graph record references unknown Entity: " + ", ".join(missing)
            )

    def _upsert_relation_tx(
        self, transaction: Any, candidate: Mapping[str, Any]
    ) -> Dict[str, Any]:
        observed = _parse_time(candidate["observed_at"], "Relation.observed_at")
        valid_from = _parse_time(candidate["valid_from"], "Relation.valid_from")
        valid_to = (
            _parse_time(candidate["valid_to"], "Relation.valid_to")
            if candidate["valid_to"] is not None
            else None
        )
        if valid_from > observed or (valid_to is not None and valid_from > valid_to):
            raise ContractViolation("Relation interval timestamps are inconsistent")
        self._lock_entities_tx(
            transaction,
            (candidate["source_entity_id"], candidate["destination_entity_id"]),
        )
        row = _single(
            transaction.run(
                """
                MATCH ()-[relation:STATEGRAPH_RELATION]->()
                WHERE relation.relation_key = $relation_key
                WITH relation
                ORDER BY relation.valid_from DESC, relation.relation_id DESC
                LIMIT 1
                SET relation.ingest_lock = $lock_token
                REMOVE relation.ingest_lock
                RETURN relation.document_json AS document_json
                """,
                relation_key=candidate["relation_key"],
                lock_token=uuid4().hex,
            )
        )
        if row is None:
            self._create_relation_tx(transaction, candidate)
            return copy.deepcopy(dict(candidate))
        latest = _decode_document(_row_value(row, "document_json"))
        identity_fields = (
            "source_entity_id",
            "relation_type",
            "destination_entity_id",
            "reference_key",
            "projector",
        )
        if any(latest[field] != candidate[field] for field in identity_fields):
            raise ContractViolation("relation_key collision with different relation identity")
        latest_from = _parse_time(latest["valid_from"], "Relation.valid_from")
        latest_to = (
            _parse_time(latest["valid_to"], "Relation.valid_to")
            if latest["valid_to"] is not None
            else None
        )
        if valid_from < latest_from or (
            latest_to is not None and valid_from < latest_to
        ):
            raise ContractViolation("Relation observations must not overlap or go backward")
        if latest_to is None:
            updated = copy.deepcopy(latest)
            updated["observed_at"] = _format_time(
                max(
                    _parse_time(latest["observed_at"], "Relation.observed_at"),
                    observed,
                )
            )
            updated["evidence_ids"] = _merged_ids(
                latest["evidence_ids"], candidate["evidence_ids"]
            )
            validate_graph_record(updated)
            transaction.run(
                """
                MATCH ()-[relation:STATEGRAPH_RELATION]->()
                WHERE relation.relation_id = $relation_id
                SET relation.document_json = $document_json,
                    relation.observed_at = $observed_at,
                    relation.evidence_ids = $evidence_ids
                """,
                relation_id=updated["relation_id"],
                document_json=_json(updated),
                observed_at=_parse_time(updated["observed_at"], "Relation.observed_at"),
                evidence_ids=updated["evidence_ids"],
            ).consume()
            return updated
        self._create_relation_tx(transaction, candidate)
        return copy.deepcopy(dict(candidate))

    @staticmethod
    def _create_relation_tx(transaction: Any, relation: Mapping[str, Any]) -> None:
        transaction.run(
            """
            MATCH (source:StateGraphEntity {entity_id: $source_entity_id})
            MATCH (destination:StateGraphEntity {entity_id: $destination_entity_id})
            CREATE (source)-[edge:STATEGRAPH_RELATION {
              relation_id: $relation_id,
              relation_key: $relation_key,
              relation_type: $relation_type,
              source_entity_id: $source_entity_id,
              destination_entity_id: $destination_entity_id,
              document_json: $document_json,
              evidence_ids: $evidence_ids,
              observed_at: $observed_at,
              valid_from: $valid_from,
              valid_to: $valid_to
            }]->(destination)
            """,
            relation_id=relation["relation_id"],
            relation_key=relation["relation_key"],
            relation_type=relation["relation_type"],
            source_entity_id=relation["source_entity_id"],
            destination_entity_id=relation["destination_entity_id"],
            document_json=_json(relation),
            evidence_ids=relation["evidence_ids"],
            observed_at=_parse_time(relation["observed_at"], "Relation.observed_at"),
            valid_from=_parse_time(relation["valid_from"], "Relation.valid_from"),
            valid_to=(
                _parse_time(relation["valid_to"], "Relation.valid_to")
                if relation["valid_to"] is not None
                else None
            ),
        ).consume()

    def _upsert_event_tx(
        self, transaction: Any, candidate: Mapping[str, Any]
    ) -> Dict[str, Any]:
        first = _parse_time(candidate["first_seen_at"], "Event.first_seen_at")
        last = _parse_time(candidate["last_seen_at"], "Event.last_seen_at")
        if first > last:
            raise ContractViolation("Event first_seen_at must not follow last_seen_at")
        self._lock_entities_tx(transaction, (candidate["entity_id"],))
        row = _single(
            transaction.run(
                """
                MATCH (event:StateGraphEvent {event_id: $event_id})
                RETURN event.document_json AS document_json
                """,
                event_id=candidate["event_id"],
            )
        )
        if row is None:
            transaction.run(
                """
                MATCH (entity:StateGraphEntity {entity_id: $entity_id})
                CREATE (event:StateGraphEvent {
                  event_id: $event_id,
                  document_json: $document_json,
                  evidence_ids: $evidence_ids,
                  first_seen_at: $first_seen_at,
                  last_seen_at: $last_seen_at
                })
                CREATE (entity)-[:HAS_EVENT]->(event)
                """,
                entity_id=candidate["entity_id"],
                event_id=candidate["event_id"],
                document_json=_json(candidate),
                evidence_ids=candidate["evidence_ids"],
                first_seen_at=first,
                last_seen_at=last,
            ).consume()
            return copy.deepcopy(dict(candidate))
        existing = _decode_document(_row_value(row, "document_json"))
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
        transaction.run(
            """
            MATCH (event:StateGraphEvent {event_id: $event_id})
            SET event.document_json = $document_json,
                event.evidence_ids = $evidence_ids,
                event.first_seen_at = $first_seen_at,
                event.last_seen_at = $last_seen_at
            """,
            event_id=updated["event_id"],
            document_json=_json(updated),
            evidence_ids=updated["evidence_ids"],
            first_seen_at=_parse_time(updated["first_seen_at"], "Event.first_seen_at"),
            last_seen_at=_parse_time(updated["last_seen_at"], "Event.last_seen_at"),
        ).consume()
        return updated

    def _find_state_paths_tx(
        self, transaction: Any, scope: InvestigationScope
    ) -> GraphLocalization:
        domain_parameters = {"domains": list(scope.domains)}
        count_row = _single(
            transaction.run(
                """
                MATCH (entity:StateGraphEntity)
                WHERE size($domains) = 0 OR entity.domain IN $domains
                RETURN count(entity) AS candidate_count
                """,
                **domain_parameters,
            )
        )
        candidate_count = int(_row_value(count_row, "candidate_count"))
        seed_rows = _rows(
            transaction.run(
                """
                MATCH (entity:StateGraphEntity)
                WHERE entity.entity_id IN $seed_entity_ids
                  AND (size($domains) = 0 OR entity.domain IN $domains)
                RETURN entity.entity_id AS entity_id,
                       entity.document_json AS document_json
                """,
                seed_entity_ids=list(scope.seed_entity_ids),
                **domain_parameters,
            )
        )
        entities = {
            _row_value(row, "entity_id"): _decode_document(
                _row_value(row, "document_json")
            )
            for row in seed_rows
        }
        for seed in scope.seed_entity_ids:
            if seed not in entities:
                raise ContractViolation(
                    f"InvestigationScope seed is absent from the selected Graph: {seed}"
                )

        visited: set[str] = set()
        discovery_order: List[str] = []
        parent: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        queue: deque[Tuple[str, int]] = deque()
        for seed in scope.seed_entity_ids:
            if len(visited) >= scope.max_entities:
                break
            visited.add(seed)
            discovery_order.append(seed)
            queue.append((seed, 0))

        window_start = _parse_time(scope.window.start, "InvestigationScope.window.start")
        window_end = _parse_time(scope.window.end, "InvestigationScope.window.end")
        while queue and len(visited) < scope.max_entities:
            current, depth = queue.popleft()
            if depth >= scope.max_depth:
                continue
            remaining = scope.max_entities - len(visited)
            rows = _rows(
                transaction.run(
                    """
                    MATCH (current:StateGraphEntity {entity_id: $current_entity_id})
                          -[relation:STATEGRAPH_RELATION]-
                          (neighbor:StateGraphEntity)
                    WHERE NOT (neighbor.entity_id IN $visited_entity_ids)
                      AND (size($domains) = 0 OR neighbor.domain IN $domains)
                      AND (
                        size($relation_types) = 0
                        OR relation.relation_type IN $relation_types
                      )
                      AND relation.valid_from <= $window_end
                      AND (
                        relation.valid_to IS NULL
                        OR relation.valid_to >= $window_start
                      )
                    WITH neighbor, relation
                    ORDER BY relation.relation_type,
                             relation.source_entity_id,
                             relation.destination_entity_id,
                             relation.relation_id
                    WITH neighbor, collect(relation)[0] AS relation
                    RETURN neighbor.entity_id AS entity_id,
                           neighbor.document_json AS entity_json,
                           relation.document_json AS relation_json,
                           relation.relation_type AS relation_type,
                           relation.source_entity_id AS source_entity_id,
                           relation.destination_entity_id AS destination_entity_id,
                           relation.relation_id AS relation_id
                    ORDER BY relation_type, source_entity_id,
                             destination_entity_id, relation_id
                    LIMIT $remaining
                    """,
                    current_entity_id=current,
                    visited_entity_ids=sorted(visited),
                    domains=list(scope.domains),
                    relation_types=list(scope.relation_types),
                    window_start=window_start,
                    window_end=window_end,
                    remaining=remaining,
                )
            )
            for row in rows:
                entity_id = _row_value(row, "entity_id")
                if entity_id in visited:
                    continue
                visited.add(entity_id)
                discovery_order.append(entity_id)
                entities[entity_id] = _decode_document(_row_value(row, "entity_json"))
                parent[entity_id] = (
                    current,
                    _decode_document(_row_value(row, "relation_json")),
                )
                queue.append((entity_id, depth + 1))
                if len(visited) >= scope.max_entities:
                    break

        evidence_by_entity, recent = self._entity_evidence_tx(
            transaction,
            sorted(visited),
            window_start=window_start,
            window_end=window_end,
        )
        recent.update(
            self._recent_relation_evidence_tx(
                transaction,
                sorted(visited),
                window_start=window_start,
                window_end=window_end,
            )
        )
        paths: List[LocalizedPath] = []
        seeds = set(scope.seed_entity_ids)
        for entity_id in discovery_order:
            entity_path = [entity_id]
            relations: List[Dict[str, Any]] = []
            cursor = entity_id
            while cursor not in seeds:
                predecessor, relation = parent[cursor]
                entity_path.append(predecessor)
                relations.append(relation)
                cursor = predecessor
            entity_path.reverse()
            relations.reverse()
            evidence = set()
            for path_entity_id in entity_path:
                evidence.update(evidence_by_entity.get(path_entity_id, set()))
            for relation in relations:
                evidence.update(relation["evidence_ids"])
            paths.append(
                LocalizedPath(
                    entity_ids=tuple(entity_path),
                    relation_types=tuple(
                        relation["relation_type"] for relation in relations
                    ),
                    evidence_ids=tuple(sorted(evidence)),
                )
            )
        evidence_ids = tuple(
            sorted(
                {
                    evidence_id
                    for path in paths
                    for evidence_id in path.evidence_ids
                }
            )
        )
        covered = sum(bool(evidence_by_entity.get(entity_id)) for entity_id in visited)
        return GraphLocalization(
            candidate_entities_before=candidate_count,
            entities={entity_id: entities[entity_id] for entity_id in sorted(visited)},
            paths=tuple(paths),
            evidence_ids=evidence_ids,
            recent_change_evidence_ids=tuple(sorted(recent)),
            entity_coverage=covered / len(visited) if visited else 0.0,
        )

    @staticmethod
    def _entity_evidence_tx(
        transaction: Any,
        entity_ids: Sequence[str],
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> Tuple[Dict[str, set[str]], set[str]]:
        rows = _rows(
            transaction.run(
                """
                MATCH (entity:StateGraphEntity)
                WHERE entity.entity_id IN $entity_ids
                OPTIONAL MATCH (entity)-[:HAS_SNAPSHOT]->
                      (snapshot:StateGraphSnapshot)
                WHERE snapshot.valid_from <= $window_end
                  AND (snapshot.valid_to IS NULL OR snapshot.valid_to >= $window_start)
                WITH entity,
                     collect(snapshot.evidence_ids) AS snapshot_evidence,
                     collect(CASE
                       WHEN snapshot.valid_from >= $window_start
                        AND snapshot.valid_from <= $window_end
                       THEN snapshot.evidence_ids ELSE []
                     END) AS recent_snapshot_evidence
                OPTIONAL MATCH (entity)-[:HAS_EVENT]->(event:StateGraphEvent)
                WHERE event.first_seen_at <= $window_end
                  AND event.last_seen_at >= $window_start
                RETURN entity.entity_id AS entity_id,
                       snapshot_evidence,
                       recent_snapshot_evidence,
                       collect(event.evidence_ids) AS event_evidence,
                       collect(CASE
                         WHEN event.first_seen_at >= $window_start
                          AND event.first_seen_at <= $window_end
                         THEN event.evidence_ids ELSE []
                       END) AS recent_event_evidence
                """,
                entity_ids=list(entity_ids),
                window_start=window_start,
                window_end=window_end,
            )
        )
        evidence: Dict[str, set[str]] = {entity_id: set() for entity_id in entity_ids}
        recent: set[str] = set()
        for row in rows:
            entity_id = _row_value(row, "entity_id")
            evidence[entity_id].update(
                _flatten_ids(_row_value(row, "snapshot_evidence"))
            )
            evidence[entity_id].update(
                _flatten_ids(_row_value(row, "event_evidence"))
            )
            recent.update(
                _flatten_ids(_row_value(row, "recent_snapshot_evidence"))
            )
            recent.update(
                _flatten_ids(_row_value(row, "recent_event_evidence"))
            )
        return evidence, recent

    @staticmethod
    def _recent_relation_evidence_tx(
        transaction: Any,
        entity_ids: Sequence[str],
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> set[str]:
        rows = _rows(
            transaction.run(
                """
                MATCH (source:StateGraphEntity)-[relation:STATEGRAPH_RELATION]->
                      (destination:StateGraphEntity)
                WHERE source.entity_id IN $entity_ids
                  AND destination.entity_id IN $entity_ids
                  AND relation.valid_from >= $window_start
                  AND relation.valid_from <= $window_end
                RETURN relation.evidence_ids AS evidence_ids
                """,
                entity_ids=list(entity_ids),
                window_start=window_start,
                window_end=window_end,
            )
        )
        return _flatten_ids(
            _row_value(row, "evidence_ids") for row in rows
        )

    def _execute_read(self, work: Any) -> Any:
        session_options = (
            {"database": self._database} if self._database is not None else {}
        )
        with self._driver.session(**session_options) as session:
            return session.execute_read(work)

    def _execute_write(self, work: Any) -> Any:
        session_options = (
            {"database": self._database} if self._database is not None else {}
        )
        with self._driver.session(**session_options) as session:
            return session.execute_write(work)
