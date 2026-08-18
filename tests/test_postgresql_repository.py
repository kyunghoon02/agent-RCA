from __future__ import annotations

import os
import unittest
import uuid
from pathlib import Path

from incident_platform.evidence import (
    EvidenceBuilder,
    EvidenceDraft,
)
from incident_platform.incidents import AlertmanagerNormalizer
from incident_platform.postgresql import (
    PostgreSQLIncidentRepository,
    apply_migrations,
)

from contract_suites import FIXED_TIME, IncidentRepositoryContract, contract_request


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db" / "migrations" / "001_initial.sql"


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
        if applied != ["001_initial.sql"]:
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


if __name__ == "__main__":
    unittest.main()
