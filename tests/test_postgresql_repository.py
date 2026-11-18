from __future__ import annotations

import os
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from incident_platform.deterministic import DeterministicDecision
from incident_platform.reporting import FastPathReportBuilder
from incident_platform.evidence import (
    EvidenceBuilder,
    EvidenceDraft,
    EvidenceWindow,
)
from incident_platform.errors import ContractViolation, InvalidTransition
from incident_platform.incidents import AlertmanagerNormalizer
from incident_platform.postgresql import (
    PostgreSQLIncidentAnalysisWorkRepository,
    PostgreSQLIncidentLocalizationWorkRepository,
    PostgreSQLIncidentRepository,
    PostgreSQLIncidentWorkRepository,
    PostgreSQLIncidentWorkQueueTelemetryRepository,
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
from incident_platform.viewer import IncidentViewerQueryService

from contract_suites import FIXED_TIME, IncidentRepositoryContract, contract_request
from tests.test_incident_work import collection_result


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


def contract_incident(fingerprint="postgresql-repository-contract-01") -> dict:
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
                "fingerprint": fingerprint,
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

    def test_continuous_analysis_claim_parameterizes_eligibility_boundary(self) -> None:
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
        label = "agent_rca_enabled"
        activated_at = datetime(2026, 8, 27, 6, 30, tzinfo=timezone.utc)

        self.assertIsNone(
            repository.claim_next_eligible(
                worker_id="continuous-worker",
                now=FIXED_TIME,
                lease_duration=timedelta(seconds=30),
                max_attempts=3,
                eligibility_label=label,
                activated_at=activated_at,
            )
        )

        statement, parameters = cursor.calls[0]
        self.assertNotIn(label, statement)
        self.assertIn("incident.created_at >= %s", statement)
        self.assertIn("->>%s = 'true'", statement)
        self.assertEqual(parameters[-2:], [activated_at, label])

    def test_continuous_analysis_reaper_applies_the_same_eligibility_boundary(self) -> None:
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
        repository = PostgreSQLIncidentAnalysisWorkRepository(
            lambda: RecordingConnection(cursor)
        )
        label = "agent_rca_enabled"
        activated_at = datetime(2026, 8, 27, 6, 30, tzinfo=timezone.utc)

        self.assertEqual(
            repository.reap_exhausted_eligible(
                now=FIXED_TIME,
                max_attempts=3,
                eligibility_label=label,
                activated_at=activated_at,
            ),
            0,
        )

        statement, parameters = cursor.calls[0]
        self.assertNotIn(label, statement)
        self.assertIn("incident.created_at >= %s", statement)
        self.assertIn("->>%s = 'true'", statement)
        self.assertEqual(parameters[-2:], [activated_at, label])

    def test_queue_telemetry_uses_the_continuous_analysis_boundary(self) -> None:
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
                return [("analysis", 1, 0, 2, 1, 120.0, 0.0)]

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
        repository = PostgreSQLIncidentWorkQueueTelemetryRepository(
            lambda: RecordingConnection(cursor)
        )
        label = "agent_rca_enabled"
        activated_at = datetime(2026, 8, 27, 6, 30, tzinfo=timezone.utc)

        snapshot = repository.snapshot(
            now=FIXED_TIME,
            analysis_eligibility_label=label,
            analysis_activated_at=activated_at,
        )

        statement, parameters = cursor.calls[0]
        self.assertNotIn(label, statement)
        self.assertIn("incident.created_at >= %s", statement)
        self.assertIn("->>%s = 'true'", statement)
        self.assertEqual(parameters[:2], (activated_at, label))
        self.assertEqual(tuple(stage.stage for stage in snapshot.stages), (
            "collection",
            "localization",
            "analysis",
        ))
        self.assertEqual(snapshot.stages[0].ready, 0)
        self.assertEqual(snapshot.stages[2].ready, 1)
        self.assertEqual(snapshot.stages[2].oldest_ready_age_seconds, 120.0)

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
        import psycopg
        from psycopg import sql

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

    def test_downstream_checkpoint_is_atomic_durable_and_lease_fenced(self) -> None:
        from tests.test_downstream_collection import (
            DownstreamProvider,
            prepare_incident,
        )
        from tests.test_incident_worker_runtime import NOW, config
        from incident_platform.collectors import CollectorOrchestrator, CollectorSpec
        from incident_platform.evidence import ResourceScope

        repository = PostgreSQLIncidentRepository(self.connection_factory)
        incident, request, initial_evidence = prepare_incident(repository)
        work = PostgreSQLIncidentLocalizationWorkRepository(self.connection_factory)
        now = NOW + timedelta(seconds=1)
        claim = work.claim_next(
            worker_id="downstream-contract",
            now=now,
            lease_duration=timedelta(seconds=30),
            max_attempts=3,
        )
        self.assertEqual(claim.incident_id, incident["incident_id"])
        run = CollectorOrchestrator(
            (CollectorSpec("kubernetes", DownstreamProvider()),)
        ).collect(
            incident_id=incident["incident_id"],
            window=request.window,
            scope=ResourceScope(
                namespace=config().target_namespace,
                resource_names=("checkoutservice",),
                resource_name_prefixes=("checkoutservice-",),
            ),
            observed_at=now,
        )
        selection = {
            "cluster_id": config().cluster_id,
            "namespace": config().target_namespace,
            "profile_id": "checkout-fixture",
            "services": ["checkoutservice"],
            "feature_evidence_ids": [initial_evidence[1]["evidence_id"]],
        }
        kwargs = dict(
            selection=selection,
            window=request.window,
            collector_statuses=run.collector_statuses,
            evidence_items=run.evidence,
            now=now,
        )
        # Invalid Evidence is rejected before writes.
        invalid = dict(run.evidence[-1], incident_id="inc-unrelated-fixture")
        with self.assertRaises(InvalidTransition):
            work.store_collection(
                claim, **dict(kwargs, evidence_items=(*run.evidence, invalid))
            )
        self.assertIsNone(
            repository.get_localization_collection(incident["incident_id"])
        )
        self.assertEqual(
            len(repository.list_evidence(incident["incident_id"])),
            len(initial_evidence),
        )
        # A collision on the second INSERT also rolls back the first INSERT.
        collision = dict(run.evidence[-1], evidence_id=initial_evidence[0]["evidence_id"])
        with self.assertRaisesRegex(InvalidTransition, "collision"):
            work.store_collection(
                claim, **dict(kwargs, evidence_items=(run.evidence[0], collision))
            )
        self.assertEqual(
            len(repository.list_evidence(incident["incident_id"])), len(initial_evidence)
        )
        self.assertIsNone(repository.get_localization_collection(incident["incident_id"]))
        self.assertEqual(repository.get(incident["incident_id"])["collector_statuses"], incident["collector_statuses"])
        checkpoint = work.store_collection(claim, **kwargs)
        self.assertEqual(work.store_collection(claim, **kwargs), checkpoint)
        self.assertEqual(
            PostgreSQLIncidentRepository(
                self.connection_factory
            ).get_localization_collection(incident["incident_id"]),
            checkpoint,
        )
        # Expiry itself rejects writes, even before another worker reclaims.
        with self.assertRaisesRegex(InvalidTransition, "expired"):
            work.store_collection(
                claim, **dict(kwargs, now=now + timedelta(seconds=31))
            )
        reclaimed = work.claim_next(
            worker_id="replacement-contract",
            now=now + timedelta(seconds=31),
            lease_duration=timedelta(seconds=30),
            max_attempts=3,
        )
        self.assertEqual(reclaimed.incident_id, claim.incident_id)
        with self.assertRaisesRegex(InvalidTransition, "stale"):
            work.store_collection(claim, **kwargs)
        self.assertEqual(
            repository.get_localization_collection(incident["incident_id"]), checkpoint
        )

    def _collection_fixture(self):
        repository = PostgreSQLIncidentRepository(self.connection_factory)
        work = PostgreSQLIncidentWorkRepository(self.connection_factory)
        incident = contract_incident(f"collection-commit-{uuid.uuid4().hex}")
        repository.create_or_get_by_deduplication_key(incident, occurred_at=FIXED_TIME)
        claim = work.claim_next(worker_id="collection-a", now=FIXED_TIME,
                               lease_duration=timedelta(seconds=30), max_attempts=3)
        self.assertEqual(claim.incident_id, incident["incident_id"])
        statuses, evidence = collection_result(claim.incident_id, now=FIXED_TIME, name="checkoutservice")
        # Successful collection creates real localization work through the
        # trigger. Retire only this fixture's Incident after assertions so a
        # later test's untargeted localization claim cannot consume its row.
        def retire_fixture():
            current = repository.get(claim.incident_id)
            if current["status"] == "LOCALIZING":
                repository.transition(claim.incident_id, expected_status="LOCALIZING",
                                      next_status="FAILED", occurred_at=FIXED_TIME + timedelta(seconds=90))
        self.addCleanup(retire_fixture)
        return repository, work, claim, statuses, evidence

    def _collection_snapshot(self, repository, incident_id):
        with self.connection_factory() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT state, claim_token, worker_id, lease_expires_at, attempt_count, completed_at, last_error_code FROM incident_work_items WHERE incident_id = %s", (incident_id,))
                work_row = cursor.fetchone()
                cursor.execute("SELECT count(*) FROM incident_localization_work_items WHERE incident_id = %s", (incident_id,))
                localization_count = cursor.fetchone()[0]
        return (repository.get(incident_id), repository.list_evidence(incident_id),
                repository.query_audit_events(incident_id, limit=100), work_row, localization_count)

    def test_collection_commit_rejects_replaced_claim_without_business_writes(self) -> None:
        repository, work, stale, statuses, evidence = self._collection_fixture()
        current = work.claim_next(worker_id="collection-b", now=FIXED_TIME + timedelta(seconds=31),
                                  lease_duration=timedelta(seconds=30), max_attempts=3)
        self.assertEqual(current.incident_id, stale.incident_id)
        before = self._collection_snapshot(repository, current.incident_id)
        for rejected in (stale, replace(current, worker_id="wrong-worker"), replace(current, claim_token="claim-wrong-token")):
            with self.assertRaisesRegex(InvalidTransition, "stale"):
                work.commit_collection(rejected, collector_statuses=statuses, evidence_items=evidence,
                                       outcome="SUCCEEDED", now=FIXED_TIME + timedelta(seconds=32))
            self.assertEqual(self._collection_snapshot(repository, current.incident_id), before)
        self.assertEqual(before[3][1:3], (current.claim_token, "collection-b"))
        work.commit_collection(current, collector_statuses=statuses, evidence_items=evidence,
                               outcome="SUCCEEDED", now=FIXED_TIME + timedelta(seconds=32))
        self.assertEqual(repository.list_evidence(current.incident_id), evidence)
        self.assertEqual(repository.get(current.incident_id)["status"], "LOCALIZING")
        self.assertEqual(self._collection_snapshot(repository, current.incident_id)[4], 1)
        work.complete(current, now=FIXED_TIME + timedelta(seconds=33), outcome="SUCCEEDED")

    def test_collection_commit_rejects_expired_unreclaimed_lease(self) -> None:
        repository, work, claim, statuses, evidence = self._collection_fixture()
        before = self._collection_snapshot(repository, claim.incident_id)
        with self.assertRaisesRegex(InvalidTransition, "expired"):
            work.commit_collection(claim, collector_statuses=statuses, evidence_items=evidence,
                                   outcome="SUCCEEDED", now=claim.lease_expires_at)
        self.assertEqual(self._collection_snapshot(repository, claim.incident_id), before)
        work.fail(claim, now=FIXED_TIME + timedelta(seconds=31), error_code="TEST_CLEANUP")

    def test_collection_commit_honors_renewal_of_persisted_lease(self) -> None:
        repository, work, claim, statuses, evidence = self._collection_fixture()
        renewed = work.renew(claim, now=FIXED_TIME + timedelta(seconds=20), lease_duration=timedelta(seconds=30))
        self.assertGreater(renewed.lease_expires_at, claim.lease_expires_at)
        work.commit_collection(claim, collector_statuses=statuses, evidence_items=evidence,
                               outcome="SUCCEEDED", now=FIXED_TIME + timedelta(seconds=31))
        self.assertEqual(repository.list_evidence(claim.incident_id), evidence)
        self.assertEqual(repository.get(claim.incident_id)["status"], "LOCALIZING")
        work.complete(claim, now=FIXED_TIME + timedelta(seconds=32), outcome="SUCCEEDED")

    def test_collection_commit_partial_and_failed_outcomes_preserve_results(self) -> None:
        for outcome in ("PARTIAL", "FAILED"):
            with self.subTest(outcome=outcome):
                repository, work, claim, statuses, evidence = self._collection_fixture()
                statuses[0].update(status=outcome, error="bounded fixture failure")
                work.commit_collection(claim, collector_statuses=statuses, evidence_items=evidence,
                                       outcome=outcome, now=FIXED_TIME + timedelta(seconds=1))
                expected = "FAILED" if outcome == "FAILED" else "LOCALIZING"
                self.assertEqual(repository.get(claim.incident_id)["status"], expected)
                self.assertEqual(repository.get(claim.incident_id)["collector_statuses"], statuses)
                self.assertEqual(repository.list_evidence(claim.incident_id), evidence)
                snapshot = self._collection_snapshot(repository, claim.incident_id)
                self.assertEqual(snapshot[4], 0 if outcome == "FAILED" else 1)
                completed = [event for event in snapshot[2] if event.event_type == "COLLECTION_COMPLETED"]
                self.assertEqual(len(completed), 1)
                self.assertEqual(completed[0].details, {"collector_statuses": {"prometheus": outcome}})
                work.complete(claim, now=FIXED_TIME + timedelta(seconds=2), outcome=outcome)

    def test_collection_commit_invalid_batch_rolls_back_every_boundary(self) -> None:
        repository, work, claim, statuses, evidence = self._collection_fixture()
        before = self._collection_snapshot(repository, claim.incident_id)
        invalid = {**evidence[0], "schema_version": "invalid-fixture-marker"}
        foreign = collection_result("inc-other-collection-test", now=FIXED_TIME)[1][0]
        for status_batch, evidence_batch, outcome, exception in (
            (statuses, [*evidence, invalid], "SUCCEEDED", ContractViolation),
            (statuses, [*evidence, foreign], "SUCCEEDED", InvalidTransition),
            ([*statuses, *statuses], evidence, "SUCCEEDED", InvalidTransition),
            ([{**statuses[0], "status": "invalid"}], evidence, "SUCCEEDED", ContractViolation),
            (statuses, evidence, "unknown", ValueError),
        ):
            with self.assertRaises(exception) as raised:
                work.commit_collection(claim, collector_statuses=status_batch, evidence_items=evidence_batch,
                                       outcome=outcome, now=FIXED_TIME + timedelta(seconds=1))
            self.assertNotIn("invalid-fixture-marker", str(raised.exception))
            self.assertEqual(self._collection_snapshot(repository, claim.incident_id), before)
        work.fail(claim, now=FIXED_TIME + timedelta(seconds=2), error_code="TEST_CLEANUP")

    def test_collection_commit_collision_rolls_back_inserted_prefix(self) -> None:
        for persisted_collision in (False, True):
            with self.subTest(persisted=persisted_collision):
                repository, work, claim, statuses, evidence = self._collection_fixture()
                other = collection_result(claim.incident_id, now=FIXED_TIME, name="checkoutservice", value=2)[1][0]
                collision = {**other, "evidence_id": evidence[0]["evidence_id"]}
                if persisted_collision:
                    repository.store_evidence(claim.incident_id, evidence)
                    candidates = [other, collision]
                else:
                    candidates = [*evidence, collision]
                before = self._collection_snapshot(repository, claim.incident_id)
                with self.assertRaisesRegex(InvalidTransition, "collision"):
                    work.commit_collection(claim, collector_statuses=statuses, evidence_items=candidates,
                                           outcome="SUCCEEDED", now=FIXED_TIME + timedelta(seconds=1))
                self.assertEqual(self._collection_snapshot(repository, claim.incident_id), before)
                work.fail(claim, now=FIXED_TIME + timedelta(seconds=2), error_code="TEST_CLEANUP")

    def test_collection_commit_failure_after_trigger_rolls_back_every_write(self) -> None:
        repository, work, claim, statuses, evidence = self._collection_fixture()
        before = self._collection_snapshot(repository, claim.incident_id)
        append_audit = PostgreSQLIncidentRepository._append_audit_event
        observed_pending_writes = []

        def fail_after_audit(cursor, incident_id, event_type, occurred_at, details):
            append_audit(cursor, incident_id, event_type, occurred_at, details)
            if event_type == "COLLECTION_COMPLETED":
                cursor.execute("SELECT status FROM incidents WHERE incident_id = %s", (incident_id,))
                self.assertEqual(cursor.fetchone()[0], "LOCALIZING")
                cursor.execute("SELECT count(*) FROM incident_localization_work_items WHERE incident_id = %s", (incident_id,))
                self.assertEqual(cursor.fetchone()[0], 1)
                observed_pending_writes.append(True)
                raise RuntimeError("injected collection commit failure")

        with patch.object(PostgreSQLIncidentRepository, "_append_audit_event", side_effect=fail_after_audit):
            with self.assertRaisesRegex(RuntimeError, "injected collection commit failure"):
                work.commit_collection(claim, collector_statuses=statuses, evidence_items=evidence,
                                       outcome="SUCCEEDED", now=FIXED_TIME + timedelta(seconds=1))
        self.assertEqual(observed_pending_writes, [True])
        self.assertEqual(self._collection_snapshot(repository, claim.incident_id), before)
        work.commit_collection(claim, collector_statuses=statuses, evidence_items=evidence,
                               outcome="SUCCEEDED", now=FIXED_TIME + timedelta(seconds=2))
        self.assertEqual(repository.list_evidence(claim.incident_id), evidence)
        work.complete(claim, now=FIXED_TIME + timedelta(seconds=3), outcome="SUCCEEDED")

    def test_collection_commit_before_ack_is_recovered_without_duplicate_enqueue(self) -> None:
        repository, work, claim, statuses, evidence = self._collection_fixture()
        work.commit_collection(claim, collector_statuses=statuses, evidence_items=evidence,
                               outcome="SUCCEEDED", now=FIXED_TIME + timedelta(seconds=1))
        before = self._collection_snapshot(repository, claim.incident_id)
        self.assertEqual(before[3][0], "RUNNING")
        self.assertEqual(before[4], 1)
        self.assertEqual(work.reap_exhausted(now=FIXED_TIME + timedelta(seconds=31), max_attempts=3), 1)
        after = self._collection_snapshot(repository, claim.incident_id)
        self.assertEqual(after[:2], before[:2])
        self.assertEqual(after[3][0], "SUCCEEDED")
        self.assertIsNone(after[3][3])
        self.assertIsNone(after[3][6])
        self.assertEqual(after[4], 1)
        self.assertEqual([event for event in after[2] if event.event_type == "COLLECTION_COMPLETED"],
                         [event for event in before[2] if event.event_type == "COLLECTION_COMPLETED"])
        self.assertEqual(work.reap_exhausted(now=FIXED_TIME + timedelta(seconds=32), max_attempts=3), 0)
        self.assertEqual(self._collection_snapshot(repository, claim.incident_id), after)
        with self.assertRaisesRegex(InvalidTransition, "stale"):
            work.commit_collection(claim, collector_statuses=statuses, evidence_items=evidence,
                                   outcome="SUCCEEDED", now=FIXED_TIME + timedelta(seconds=32))

    def _reported_localization_fixture(self):
        repository = PostgreSQLIncidentRepository(self.connection_factory)
        work = PostgreSQLIncidentLocalizationWorkRepository(self.connection_factory)
        incident = contract_incident(f"localization-reported-{uuid.uuid4().hex}")
        incident_id = incident["incident_id"]
        repository.create_or_get_by_deduplication_key(incident, occurred_at=FIXED_TIME)
        repository.transition(incident_id, expected_status="RECEIVED", next_status="COLLECTING", occurred_at=FIXED_TIME)
        evidence = contract_evidence(incident_id)
        repository.store_evidence(incident_id, [evidence])
        repository.transition(incident_id, expected_status="COLLECTING", next_status="LOCALIZING", occurred_at=FIXED_TIME + timedelta(seconds=1))
        claim = work.claim_next(
            worker_id="localization-reported-test", now=FIXED_TIME + timedelta(seconds=2),
            lease_duration=timedelta(seconds=30), max_attempts=3,
        )
        self.assertEqual(claim.incident_id, incident_id)
        artifacts = FastPathReportBuilder().build(
            incident=repository.get(incident_id), evidence=[evidence],
            decision=DeterministicDecision(
                status="ABSTAIN", root_cause_id=None, statement=None,
                supporting_evidence_ids=(), missing_requirements=("fixture has no complete signature",),
                evaluations=(),
            ),
            generated_at=FIXED_TIME + timedelta(seconds=3),
        )
        repository.store_context(artifacts.context)
        repository.transition(incident_id, expected_status="LOCALIZING", next_status="ANALYZING", occurred_at=FIXED_TIME + timedelta(seconds=4))
        analysis = repository.query_work_state(incident_id)["analysis"]
        self.assertEqual(analysis["state"], "READY")
        self.assertEqual(analysis["context_id"], artifacts.context["context_id"])
        repository.store_report(artifacts.report, artifacts.markdown)
        repository.transition(incident_id, expected_status="ANALYZING", next_status="REPORTED", occurred_at=FIXED_TIME + timedelta(seconds=5))
        return repository, work, claim, artifacts

    def _assert_localization_succeeded_without_downstream_changes(
        self, repository, claim, artifacts, incident_before, work_before, *, completed_at, event_type, outcome,
    ):
        work_after = repository.query_work_state(claim.incident_id)
        localization = work_after["localization"]
        self.assertEqual(localization["state"], "SUCCEEDED")
        self.assertIsNone(localization["lease_expires_at"])
        self.assertIsNone(localization["last_error_code"])
        self.assertEqual(localization["completed_at"], completed_at.isoformat().replace("+00:00", "Z"))
        self.assertEqual(work_after["analysis"], work_before["analysis"])
        self.assertEqual(work_after["collection"], work_before["collection"])
        self.assertEqual(repository.get(claim.incident_id), incident_before)
        self.assertEqual(repository.get_context(artifacts.context["context_id"]), artifacts.context)
        self.assertEqual(repository.query_reports(claim.incident_id, limit=10), [(artifacts.report, artifacts.markdown)])
        events = [event for event in repository.query_audit_events(claim.incident_id, limit=100) if event.event_type == event_type]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].details, {"attempt": claim.attempt_count, "outcome": outcome})

    def test_localization_complete_after_reported(self) -> None:
        repository, work, claim, artifacts = self._reported_localization_fixture()
        incident_before = repository.get(claim.incident_id)
        work_before = repository.query_work_state(claim.incident_id)
        completed_at = FIXED_TIME + timedelta(seconds=6)

        work.complete(claim, now=completed_at, outcome="SUCCEEDED")

        self._assert_localization_succeeded_without_downstream_changes(
            repository, claim, artifacts, incident_before, work_before,
            completed_at=completed_at, event_type="INCIDENT_LOCALIZATION_WORK_COMPLETED", outcome="SUCCEEDED",
        )
        with self.assertRaisesRegex(InvalidTransition, "stale"):
            work.complete(claim, now=completed_at + timedelta(seconds=1), outcome="SUCCEEDED")

    def test_localization_reaper_after_reported(self) -> None:
        repository, work, claim, artifacts = self._reported_localization_fixture()
        incident_before = repository.get(claim.incident_id)
        work_before = repository.query_work_state(claim.incident_id)
        self.assertEqual(work.reap_exhausted(now=FIXED_TIME + timedelta(seconds=31), max_attempts=3), 0)
        self.assertEqual(repository.query_work_state(claim.incident_id), work_before)
        completed_at = FIXED_TIME + timedelta(seconds=33)

        self.assertEqual(work.reap_exhausted(now=completed_at, max_attempts=3), 1)

        self._assert_localization_succeeded_without_downstream_changes(
            repository, claim, artifacts, incident_before, work_before,
            completed_at=completed_at, event_type="INCIDENT_LOCALIZATION_WORK_REAPED", outcome="RECOVERED_SUCCEEDED",
        )
        events_before = repository.query_audit_events(claim.incident_id, limit=100)
        self.assertEqual(work.reap_exhausted(now=completed_at + timedelta(seconds=1), max_attempts=3), 0)
        self.assertEqual(repository.query_audit_events(claim.incident_id, limit=100), events_before)
        with self.assertRaisesRegex(InvalidTransition, "stale"):
            work.complete(claim, now=completed_at + timedelta(seconds=2), outcome="SUCCEEDED")

    def test_viewer_search_and_filtered_pagination_execute_in_postgresql(self) -> None:
        repository = PostgreSQLIncidentRepository(self.connection_factory)
        viewer = IncidentViewerQueryService(repository)
        marker = f"ViewerSearch{uuid.uuid4().hex}"
        namespace = f"viewer-search-{uuid.uuid4().hex}"
        incidents = []
        for index in range(4):
            incident = contract_incident()
            incident["incident_id"] = f"inc-{uuid.uuid4().hex}"
            incident["deduplication_key"] = f"viewer-search-{uuid.uuid4().hex}"
            incident["alert"]["name"] = marker
            incident["alert"]["labels"] = {
                "alertname": marker,
                "literal": "30%_literal ' quoted",
            }
            incident["source_entity"]["name"] = "viewer-search-service"
            incident["source_entity"]["namespace"] = namespace
            if index == 1:
                # Resolved graph entities keep the namespace under scope.
                incident["source_entity"] = {
                    "entity_id": f"ent-{uuid.uuid4().hex}",
                    "entity_type": "Service",
                    "domain": "application",
                    "name": "viewer-search-service",
                    "scope": {"namespace": namespace},
                    "external_ref": None,
                    "exists": True,
                }
            if index == 2:
                incident["updated_at"] = "2026-08-12T01:04:00Z"
            if index == 3:
                incident["severity"] = "critical"
            repository.create_or_get_by_deduplication_key(incident)
            incidents.append(incident)

        query = {
            "schema_version": "1.0.0",
            "statuses": ["RECEIVED"],
            "severities": ["warning"],
            "namespace": namespace,
            "search": marker.lower(),
            "limit": 2,
            "cursor": None,
        }
        expected_ids = [
            item["incident_id"] for item in sorted(
                incidents[:3],
                key=lambda item: (item["updated_at"], item["incident_id"]),
                reverse=True,
            )
        ]
        for search in (marker.upper(), "VIEWER-SEARCH-SERVICE", "30%_literal ' quoted"):
            with self.subTest(search=search):
                result = viewer.list_incidents(
                    {**query, "search": search, "limit": 10}
                )
                self.assertEqual(
                    [item["incident_id"] for item in result["items"]], expected_ids
                )
                self.assertIsNone(result["next_cursor"])
        for incident_id in expected_ids:
            result = viewer.list_incidents({**query, "search": incident_id.upper()})
            self.assertEqual(
                [item["incident_id"] for item in result["items"]], [incident_id]
            )
        for search in ("does-not-exist", "%' OR true --", "30X_literal", "30%Xliteral"):
            with self.subTest(search=search):
                result = viewer.list_incidents({**query, "search": search})
                self.assertEqual(result["items"], [])
                self.assertIsNone(result["next_cursor"])
        for changed_filter in (
            {"statuses": ["REPORTED"]}, {"namespace": "other-namespace"}
        ):
            self.assertEqual(
                viewer.list_incidents({**query, **changed_filter})["items"], []
            )

        first = viewer.list_incidents(query)
        self.assertEqual(
            [item["incident_id"] for item in first["items"]], expected_ids[:2]
        )
        self.assertIsNotNone(first["next_cursor"])
        second = viewer.list_incidents({**query, "cursor": first["next_cursor"]})
        self.assertEqual(
            [item["incident_id"] for item in second["items"]], expected_ids[2:]
        )
        self.assertIsNone(second["next_cursor"])
        with self.assertRaises(ContractViolation):
            viewer.list_incidents(
                {**query, "search": "different", "cursor": first["next_cursor"]}
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
