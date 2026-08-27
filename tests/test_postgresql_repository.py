from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from incident_platform.evidence import (
    EvidenceBuilder,
    EvidenceDraft,
)
from incident_platform.incidents import AlertmanagerNormalizer
from incident_platform.postgresql import (
    PostgreSQLIncidentAnalysisWorkRepository,
    PostgreSQLIncidentRepository,
    PostgreSQLStateGraphObservationRepository,
    apply_migrations,
)
from incident_platform.stategraph import (
    StateGraphReconciliationResult,
    stable_graph_id,
)
from incident_platform.stategraph_observations import (
    StateGraphObservationCycle,
    StateGraphObservationRepository,
)

from contract_suites import FIXED_TIME, IncidentRepositoryContract, contract_request


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db" / "migrations" / "001_initial.sql"
AGENT_RUN_MIGRATION = ROOT / "db" / "migrations" / "002_agent_runs.sql"
OBSERVATION_MIGRATION = (
    ROOT / "db" / "migrations" / "003_stategraph_observations.sql"
)
INCIDENT_WORK_MIGRATION = ROOT / "db" / "migrations" / "004_incident_work_items.sql"
LOCALIZATION_WORK_MIGRATION = (
    ROOT / "db" / "migrations" / "005_incident_localization_work_items.sql"
)
ANALYSIS_WORK_MIGRATION = (
    ROOT / "db" / "migrations" / "006_incident_analysis_work_items.sql"
)


def contract_incident() -> dict:
    payload = {
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "PostgreSQLContractTest",
                    "namespace": "online-boutique",
                    "service": "checkoutservice",
                    "severity": "warning",
                },
                "annotations": {},
                "startsAt": "2026-08-12T01:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": "postgresql-repository-contract-01",
            }
        ]
    }
    return AlertmanagerNormalizer().normalize(
        payload, received_at=FIXED_TIME
    )[0].incident


def contract_evidence(incident_id: str) -> dict:
    request = contract_request(incident_id, "checkoutservice")
    return EvidenceBuilder().build(
        EvidenceDraft(
            source="prometheus",
            kind="metric-summary",
            observed_at="2026-08-12T01:04:59Z",
            subject={
                "api_version": "v1",
                "kind": "Service",
                "namespace": "online-boutique",
                "name": "checkoutservice",
                "uid": None,
                "exists": True,
            },
            summary="Checkout error ratio increased.",
            facts={"metric": "request_error_ratio", "peak_ratio": 0.42},
            provider="prometheus-contract-provider",
            query="namespace=online-boutique service=checkoutservice",
            locator="prometheus://online-boutique/checkoutservice",
        ),
        request,
        collected_at=FIXED_TIME,
    )


class PostgreSQLMigrationTests(unittest.TestCase):
    def test_initial_migration_contains_all_persistence_boundaries(self) -> None:
        sql = MIGRATION.read_text(encoding="utf-8")
        for table in (
            "incidents",
            "incident_audit_events",
            "evidence_items",
            "context_packages",
            "rca_reports",
        ):
            self.assertIn(f"CREATE TABLE {table}", sql)
        self.assertIn("deduplication_key TEXT NOT NULL UNIQUE", sql)
        self.assertIn("document JSONB NOT NULL", sql)
        self.assertIn("markdown TEXT NOT NULL", sql)

    def test_postgresql_driver_is_loaded_only_by_connection_factory(self) -> None:
        def unavailable_connection():
            raise RuntimeError("database unavailable")

        repository = PostgreSQLIncidentRepository(unavailable_connection)
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            repository.get("inc-does-not-exist")

    def test_observation_adapter_satisfies_the_repository_port(self) -> None:
        repository = PostgreSQLStateGraphObservationRepository(lambda: None)
        self.assertIsInstance(repository, StateGraphObservationRepository)

    def test_agent_run_migration_adds_auditable_runtime_persistence(self) -> None:
        sql = AGENT_RUN_MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE agent_runs", sql)
        self.assertIn("context_id TEXT NOT NULL REFERENCES context_packages", sql)
        self.assertIn("document JSONB NOT NULL", sql)

    def test_observation_migration_separates_background_evidence(self) -> None:
        sql = OBSERVATION_MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE stategraph_observation_cycles", sql)
        self.assertIn("CREATE TABLE stategraph_observation_evidence", sql)
        self.assertIn("status IN ('STAGED', 'APPLIED')", sql)
        self.assertIn("ON DELETE CASCADE", sql)

    def test_incident_work_migration_adds_a_fenced_lease_queue(self) -> None:
        sql = INCIDENT_WORK_MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE incident_work_items", sql)
        self.assertIn("FOR EACH ROW", sql)
        self.assertIn("CREATE TRIGGER incidents_enqueue_collection_work", sql)
        self.assertIn("claim_token TEXT", sql)
        self.assertIn("lease_expires_at TIMESTAMPTZ", sql)
        self.assertIn("state IN ('READY', 'RUNNING', 'SUCCEEDED', 'FAILED')", sql)

    def test_localization_work_is_enqueued_by_the_localizing_transition(self) -> None:
        sql = LOCALIZATION_WORK_MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE incident_localization_work_items", sql)
        self.assertIn("stage = 'LOCALIZATION'", sql)
        self.assertIn("AFTER UPDATE OF status ON incidents", sql)
        self.assertIn("NEW.status = 'LOCALIZING'", sql)
        self.assertIn("CREATE TRIGGER incidents_enqueue_localization_work", sql)
        self.assertIn("claim_token TEXT", sql)
        self.assertIn("lease_expires_at TIMESTAMPTZ", sql)

    def test_analysis_work_is_context_pinned_and_enqueued_from_both_orders(self) -> None:
        sql = ANALYSIS_WORK_MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE incident_analysis_work_items", sql)
        self.assertIn("stage = 'ANALYSIS'", sql)
        self.assertIn(
            "context_id TEXT NOT NULL REFERENCES context_packages", sql
        )
        self.assertIn("CREATE TRIGGER incidents_enqueue_analysis_work", sql)
        self.assertIn("CREATE TRIGGER contexts_enqueue_analysis_work", sql)
        self.assertIn("NEW.status = 'ANALYZING'", sql)
        self.assertIn("claim_token TEXT", sql)
        self.assertIn("lease_expires_at TIMESTAMPTZ", sql)

    def test_viewer_list_query_keeps_search_text_in_sql_parameters(self) -> None:
        class RecordingCursor:
            def __init__(self) -> None:
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, statement, parameters=()):
                self.calls.append((statement, parameters))

            def fetchall(self):
                return []

        class RecordingConnection:
            closed = False

            def __init__(self, cursor) -> None:
                self._cursor = cursor

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def cursor(self):
                return self._cursor

            def close(self):
                self.closed = True

        cursor = RecordingCursor()
        repository = PostgreSQLIncidentRepository(
            lambda: RecordingConnection(cursor)
        )
        search = "%' OR true --"

        self.assertEqual(
            repository.query_incidents(
                statuses=("FAILED",),
                severities=("critical",),
                namespace="online-boutique",
                search=search,
                before_updated_at=None,
                before_incident_id=None,
                limit=10,
            ),
            [],
        )

        statement, parameters = cursor.calls[0]
        self.assertNotIn(search, statement)
        self.assertIn(search, parameters)
        self.assertIn("LIMIT %s", statement)

    def test_targeted_analysis_claim_keeps_incident_id_in_sql_parameters(self) -> None:
        class RecordingCursor:
            def __init__(self) -> None:
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, statement, parameters=()):
                self.calls.append((statement, parameters))

            def fetchone(self):
                return None

        class RecordingConnection:
            closed = False

            def __init__(self, cursor) -> None:
                self._cursor = cursor

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def cursor(self):
                return self._cursor

            def close(self):
                self.closed = True

        cursor = RecordingCursor()
        repository = PostgreSQLIncidentAnalysisWorkRepository(
            lambda: RecordingConnection(cursor)
        )
        incident_id = "inc-does-not-exist"

        self.assertIsNone(
            repository.claim_incident(
                incident_id,
                worker_id="target-worker",
                now=FIXED_TIME,
                lease_duration=timedelta(seconds=30),
                max_attempts=3,
            )
        )

        statement, parameters = cursor.calls[0]
        self.assertNotIn(incident_id, statement)
        self.assertIn("AND work.incident_id = %s", statement)
        self.assertEqual(parameters[-1], incident_id)

    def test_viewer_work_query_returns_safe_stage_state_without_claim_token(self) -> None:
        class RecordingCursor:
            def __init__(self) -> None:
                self.calls = []
                self._fetchone_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, statement, parameters=()):
                self.calls.append((statement, parameters))

            def fetchone(self):
                self._fetchone_calls += 1
                return (1,)

            def fetchall(self):
                observed = datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)
                return [
                    (
                        "analysis",
                        "ANALYSIS",
                        "READY",
                        observed,
                        0,
                        None,
                        None,
                        None,
                        None,
                        None,
                        "ctx-postgresqlviewer01",
                    )
                ]

        class RecordingConnection:
            closed = False

            def __init__(self, cursor) -> None:
                self._cursor = cursor

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def cursor(self):
                return self._cursor

            def close(self):
                self.closed = True

        cursor = RecordingCursor()
        repository = PostgreSQLIncidentRepository(
            lambda: RecordingConnection(cursor)
        )

        work = repository.query_work_state("inc-postgresqlviewer01")

        self.assertIsNone(work["collection"])
        self.assertIsNone(work["localization"])
        self.assertEqual(work["analysis"]["state"], "READY")
        self.assertEqual(
            work["analysis"]["available_at"], "2026-08-26T01:00:00Z"
        )
        statement, parameters = cursor.calls[1]
        self.assertNotIn("claim_token", statement)
        self.assertEqual(parameters, ("inc-postgresqlviewer01",) * 3)


@unittest.skipUnless(
    os.environ.get("POSTGRES_TEST_DSN"),
    "set POSTGRES_TEST_DSN to run the live PostgreSQL adapter contract",
)
class PostgreSQLLiveContractTests(unittest.TestCase):
    """Opt-in integration test isolated in a random schema.

    The suite never truncates a shared database and removes only the schema it
    created. POSTGRES_TEST_DSN must point to an explicitly approved test DB.
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import psycopg
            from psycopg import sql
        except ImportError as error:
            raise unittest.SkipTest("psycopg is not installed") from error

        cls._psycopg = psycopg
        cls._sql = sql
        cls._dsn = os.environ["POSTGRES_TEST_DSN"]
        cls._schema = f"incident_contract_{uuid.uuid4().hex}"
        cls._admin = psycopg.connect(cls._dsn, autocommit=True)
        with cls._admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls._schema))
            )
        applied = apply_migrations(cls.connection_factory)
        if applied != [
            "001_initial.sql",
            "002_agent_runs.sql",
            "003_stategraph_observations.sql",
            "004_incident_work_items.sql",
            "005_incident_localization_work_items.sql",
            "006_incident_analysis_work_items.sql",
        ]:
            raise AssertionError(f"unexpected applied migrations: {applied}")
        if apply_migrations(cls.connection_factory) != []:
            raise AssertionError("PostgreSQL migrations are not idempotent")

    @classmethod
    def tearDownClass(cls) -> None:
        with cls._admin.cursor() as cursor:
            cursor.execute(
                cls._sql.SQL("DROP SCHEMA {} CASCADE").format(
                    cls._sql.Identifier(cls._schema)
                )
            )
        cls._admin.close()

    @classmethod
    def connection_factory(cls):
        return cls._psycopg.connect(
            cls._dsn,
            options=f"-c search_path={cls._schema},public",
        )

    def test_adapter_passes_reusable_repository_contract(self) -> None:
        incident = contract_incident()
        evidence = contract_evidence(incident["incident_id"])
        IncidentRepositoryContract.verify(
            self,
            lambda: PostgreSQLIncidentRepository(self.connection_factory),
            incident,
            evidence,
        )

    def test_stategraph_observation_journal_is_durable_and_prunable(self) -> None:
        incident = contract_incident()
        evidence = contract_evidence(incident["incident_id"])
        identity = {
            "request_id": f"req-postgresql-observation-{uuid.uuid4().hex}",
            "cluster_id": "postgresql-contract-cluster",
            "namespace": "online-boutique",
            "observed_at": evidence["observed_at"],
        }
        cycle = StateGraphObservationCycle(
            cycle_id=stable_graph_id("cycle", identity),
            request_id=identity["request_id"],
            evidence_scope_id=incident["incident_id"],
            cluster_id=identity["cluster_id"],
            namespace=identity["namespace"],
            observed_at=identity["observed_at"],
            staged_at="2026-08-12T01:05:00Z",
            status="STAGED",
            evidence_ids=(evidence["evidence_id"],),
        )
        result = StateGraphReconciliationResult(
            ingested_records=3,
            current_entities=1,
            current_relations=0,
            retired_entities=0,
            closed_snapshot_intervals=0,
            closed_relation_intervals=0,
        )
        repository = PostgreSQLStateGraphObservationRepository(
            self.connection_factory
        )

        staged = repository.stage_cycle(cycle, (evidence,))
        repeated = repository.stage_cycle(cycle, (evidence,))
        applied = repository.mark_cycle_applied(
            cycle.cycle_id,
            result,
            applied_at=FIXED_TIME,
        )

        self.assertEqual(staged, repeated)
        self.assertEqual(applied.status, "APPLIED")
        self.assertEqual(repository.get_cycle(cycle.cycle_id), applied)
        self.assertEqual(repository.list_cycle_evidence(cycle.cycle_id), (evidence,))
        pruned = repository.prune_observations(
            now=FIXED_TIME + timedelta(hours=73)
        )
        self.assertEqual(pruned.cycles, 1)
        self.assertEqual(pruned.evidence_items, 1)


if __name__ == "__main__":
    unittest.main()
