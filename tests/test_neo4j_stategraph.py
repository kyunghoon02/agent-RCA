from __future__ import annotations

import copy
import json
import os
import unittest
import uuid
from datetime import datetime, timezone

from incident_platform.errors import ContractViolation
from incident_platform.evidence import EvidenceWindow
from incident_platform.neo4j_stategraph import (
    NEO4J_SCHEMA_STATEMENTS,
    Neo4jStateGraphRepository,
    apply_neo4j_schema,
    create_neo4j_driver,
)
from incident_platform.stategraph import (
    EntityIdentity,
    EntityLookup,
    InvestigationScope,
    StateGraphHistoryRepository,
    StateGraphRepository,
    stable_graph_id,
    state_content_hash,
)


UTC = timezone.utc


class _ConsumedResult:
    def consume(self) -> None:
        return None


class _RecordingSession:
    def __init__(self, statements: list[str]) -> None:
        self._statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def run(self, statement: str):
        self._statements.append(statement)
        return _ConsumedResult()


class _RecordingDriver:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.session_options: list[dict] = []

    def session(self, **options):
        self.session_options.append(options)
        return _RecordingSession(self.statements)


class Neo4jStateGraphStaticTests(unittest.TestCase):
    def test_schema_is_idempotent_and_covers_each_graph_record(self) -> None:
        driver = _RecordingDriver()

        first = apply_neo4j_schema(driver, database="neo4j")
        second = apply_neo4j_schema(driver, database="neo4j")

        self.assertEqual(first, second)
        self.assertEqual(len(driver.statements), len(NEO4J_SCHEMA_STATEMENTS) * 2)
        combined = "\n".join(driver.statements)
        for label in (
            "StateGraphEntity",
            "StateGraphSnapshot",
            "StateGraphEvent",
            "StateGraphHistoryPin",
            "STATEGRAPH_RELATION",
        ):
            self.assertIn(label, combined)
        self.assertTrue(all("IF NOT EXISTS" in statement for statement in driver.statements))

    def test_adapter_satisfies_localization_and_history_ports(self) -> None:
        repository = Neo4jStateGraphRepository(_RecordingDriver())

        self.assertIsInstance(repository, StateGraphRepository)
        self.assertIsInstance(repository, StateGraphHistoryRepository)

    def test_ingest_validates_before_opening_a_database_session(self) -> None:
        driver = _RecordingDriver()
        repository = Neo4jStateGraphRepository(driver)

        with self.assertRaises(ContractViolation):
            repository.ingest(({"record_type": "entity"},))

        self.assertEqual(driver.session_options, [])

    def test_runtime_driver_rejects_missing_credentials_before_import(self) -> None:
        with self.assertRaisesRegex(ValueError, "URI, username, and password"):
            create_neo4j_driver("bolt://localhost:7687", "neo4j", "")


def _entity(cluster_id: str, name: str, evidence_id: str) -> dict:
    identity = EntityIdentity.logical_service(
        cluster_id=cluster_id,
        namespace="neo4j-contract",
        service_name=name,
    )
    return {
        "record_type": "entity",
        "entity_id": identity.entity_id,
        "identity": identity.to_contract(),
        "entity_type": "Service",
        "domain": "web-service",
        "name": name,
        "scope": {
            "cluster_id": cluster_id,
            "namespace": "neo4j-contract",
        },
        "external_ref": f"service://neo4j-contract/{name}",
        "exists": True,
        "first_seen_at": "2026-08-12T01:01:00Z",
        "last_seen_at": "2026-08-12T01:09:00Z",
        "evidence_ids": [evidence_id],
    }


def _snapshot(entity_id: str, state: dict, at: str, evidence_id: str) -> dict:
    state_hash = state_content_hash(state)
    return {
        "record_type": "snapshot_interval",
        "snapshot_id": stable_graph_id(
            "snap", {"entity_id": entity_id, "state_hash": state_hash, "at": at}
        ),
        "entity_id": entity_id,
        "observed_at": at,
        "valid_from": at,
        "valid_to": None,
        "state_hash": state_hash,
        "state": copy.deepcopy(state),
        "evidence_ids": [evidence_id],
    }


@unittest.skipUnless(
    os.environ.get("NEO4J_TEST_URI")
    and os.environ.get("NEO4J_TEST_USERNAME")
    and os.environ.get("NEO4J_TEST_PASSWORD"),
    "set NEO4J_TEST_URI/USERNAME/PASSWORD to run the live Neo4j contract",
)
class Neo4jStateGraphLiveContractTests(unittest.TestCase):
    """Opt-in contract that deletes only IDs created by this test run."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._driver = create_neo4j_driver(
            os.environ["NEO4J_TEST_URI"],
            os.environ["NEO4J_TEST_USERNAME"],
            os.environ["NEO4J_TEST_PASSWORD"],
        )
        cls._database = os.environ.get("NEO4J_TEST_DATABASE", "neo4j")
        cls._driver.verify_connectivity()
        apply_neo4j_schema(cls._driver, database=cls._database)
        cls._suffix = uuid.uuid4().hex
        cls._cluster_id = f"neo4j-contract-{cls._suffix}"
        cls._incident_id = f"inc-neo4j-{cls._suffix[:24]}"
        cls._evidence_ids = (
            f"ev-neo4j-{cls._suffix[:24]}-a",
            f"ev-neo4j-{cls._suffix[:24]}-b",
            f"ev-neo4j-{cls._suffix[:24]}-c",
            f"ev-neo4j-{cls._suffix[:24]}-d",
        )
        cls._source = _entity(
            cls._cluster_id, f"checkout-{cls._suffix}", cls._evidence_ids[0]
        )
        cls._destination = _entity(
            cls._cluster_id, f"payment-{cls._suffix}", cls._evidence_ids[1]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        entity_ids = [cls._source["entity_id"], cls._destination["entity_id"]]
        with cls._driver.session(database=cls._database) as session:
            session.run(
                """
                MATCH (pin:StateGraphHistoryPin {incident_id: $incident_id})
                DETACH DELETE pin
                """,
                incident_id=cls._incident_id,
            ).consume()
            session.run(
                """
                MATCH (entity:StateGraphEntity)
                WHERE entity.entity_id IN $entity_ids
                OPTIONAL MATCH (entity)-[:HAS_SNAPSHOT|HAS_EVENT]->(artifact)
                WITH collect(DISTINCT artifact) + collect(DISTINCT entity) AS nodes
                UNWIND nodes AS node
                DETACH DELETE node
                """,
                entity_ids=entity_ids,
            ).consume()
        cls._driver.close()

    def test_ingest_lookup_localize_and_pin(self) -> None:
        repository = Neo4jStateGraphRepository(
            self._driver,
            database=self._database,
        )
        degraded = _snapshot(
            self._source["entity_id"],
            {"health": "degraded"},
            "2026-08-12T01:02:00Z",
            self._evidence_ids[0],
        )
        repeated = _snapshot(
            self._source["entity_id"],
            {"health": "degraded"},
            "2026-08-12T01:03:00Z",
            self._evidence_ids[1],
        )
        healthy = _snapshot(
            self._source["entity_id"],
            {"health": "healthy"},
            "2026-08-12T01:06:00Z",
            self._evidence_ids[2],
        )
        destination_state = _snapshot(
            self._destination["entity_id"],
            {"health": "healthy"},
            "2026-08-12T01:02:00Z",
            self._evidence_ids[1],
        )
        relation_identity = {
            "source_entity_id": self._source["entity_id"],
            "relation_type": "CALLS",
            "destination_entity_id": self._destination["entity_id"],
            "reference_key": "api-contract",
            "projector": "neo4j-contract-projector",
        }
        relation_key = stable_graph_id("relkey", relation_identity)
        relation = {
            "record_type": "relation_interval",
            "relation_id": stable_graph_id(
                "rel", {"relation_key": relation_key, "at": "2026-08-12T01:02:00Z"}
            ),
            "relation_key": relation_key,
            **relation_identity,
            "observed_at": "2026-08-12T01:02:00Z",
            "valid_from": "2026-08-12T01:02:00Z",
            "valid_to": None,
            "evidence_ids": [self._evidence_ids[3]],
        }
        event = {
            "record_type": "event_aggregate",
            "event_id": stable_graph_id(
                "evt", {"entity_id": self._source["entity_id"], "reason": "Timeout"}
            ),
            "entity_id": self._source["entity_id"],
            "event_type": "Timeout",
            "first_seen_at": "2026-08-12T01:04:00Z",
            "last_seen_at": "2026-08-12T01:05:00Z",
            "count": 3,
            "attributes": {"reason": "upstream-timeout"},
            "evidence_ids": [self._evidence_ids[3]],
        }
        repository.ingest(
            (
                self._source,
                self._destination,
                degraded,
                destination_state,
                relation,
                event,
            )
        )
        repository.ingest((repeated,))
        repository.ingest((healthy,))
        repository.close_relation(
            relation_key,
            observed_at=datetime(2026, 8, 12, 1, 8, tzinfo=UTC),
        )

        with self._driver.session(database=self._database) as session:
            snapshot_rows = list(
                session.run(
                    """
                    MATCH (:StateGraphEntity {entity_id: $entity_id})
                          -[:HAS_SNAPSHOT]->(snapshot:StateGraphSnapshot)
                    RETURN snapshot.document_json AS document_json
                    ORDER BY snapshot.valid_from
                    """,
                    entity_id=self._source["entity_id"],
                )
            )
            relation_row = session.run(
                """
                MATCH ()-[relation:STATEGRAPH_RELATION]->()
                WHERE relation.relation_key = $relation_key
                RETURN relation.document_json AS document_json
                """,
                relation_key=relation_key,
            ).single()
        self.assertEqual(len(snapshot_rows), 2)
        first_snapshot = json.loads(snapshot_rows[0]["document_json"])
        self.assertEqual(first_snapshot["valid_to"], "2026-08-12T01:06:00Z")
        self.assertEqual(len(first_snapshot["evidence_ids"]), 2)
        closed_relation = json.loads(relation_row["document_json"])
        self.assertEqual(closed_relation["valid_to"], "2026-08-12T01:08:00Z")

        window = EvidenceWindow(
            start="2026-08-12T01:00:00Z",
            end="2026-08-12T01:10:00Z",
        )
        matches = repository.find_entities(
            EntityLookup(
                cluster_id=self._cluster_id,
                namespace="neo4j-contract",
                name=self._source["name"],
                window=window,
                domains=("web-service",),
                identity_types=("logical-service",),
            )
        )
        self.assertEqual([item["entity_id"] for item in matches], [self._source["entity_id"]])

        scope = InvestigationScope(
            incident_id=self._incident_id,
            seed_entity_ids=(self._source["entity_id"],),
            window=window,
            domains=("web-service",),
            relation_types=("CALLS",),
            max_entities=2,
            max_depth=1,
        )
        localized = repository.find_state_paths(scope)
        self.assertEqual(len(localized.entities), 2)
        self.assertEqual(localized.paths[-1].relation_types, ("CALLS",))
        self.assertIn(self._evidence_ids[3], localized.evidence_ids)

        pin = repository.pin_incident_history(
            scope,
            tuple(localized.entities),
            pinned_at=datetime(2026, 8, 12, 1, 11, tzinfo=UTC),
        )
        self.assertEqual(pin.incident_id, self._incident_id)
        self.assertEqual(set(pin.entity_ids), set(localized.entities))

        protected = repository.prune_history(
            now=datetime(2026, 8, 16, 1, 11, tzinfo=UTC)
        )
        self.assertEqual(protected.expired_pins, 0)
        self.assertEqual(protected.snapshot_intervals, 0)
        self.assertEqual(protected.relation_intervals, 0)
        self.assertEqual(protected.event_aggregates, 0)

        expired = repository.prune_history(
            now=datetime(2026, 9, 15, 1, 11, tzinfo=UTC)
        )
        self.assertEqual(expired.expired_pins, 1)
        self.assertEqual(expired.snapshot_intervals, 1)
        self.assertEqual(expired.relation_intervals, 1)
        self.assertEqual(expired.event_aggregates, 1)


if __name__ == "__main__":
    unittest.main()
