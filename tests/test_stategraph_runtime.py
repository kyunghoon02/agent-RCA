from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tools.run_stategraph_reconciler import (
    RuntimeConfig,
    build_collection_request,
    scheduled_observation_time,
)


UTC = timezone.utc


def runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        cluster_id="agent-rca-runtime-test",
        target_namespace="online-boutique",
        application_services=("frontend", "checkoutservice"),
        schedule_interval_seconds=300,
        kubernetes_api_server="https://kubernetes.default.svc",
        kubernetes_token_file="/var/run/secrets/token",
        kubernetes_ca_file="/var/run/secrets/ca.crt",
        neo4j_uri="bolt://neo4j.graph-rca.svc.cluster.local:7687",
        neo4j_username="neo4j",
        neo4j_password="test-only",
        neo4j_database="neo4j",
        postgres_host="postgresql.incident-platform.svc.cluster.local",
        postgres_port=5432,
        postgres_database="agent_rca",
        postgres_username="agent_rca",
        postgres_password="test-only",
    )


class StateGraphRuntimeTests(unittest.TestCase):
    def test_schedule_slot_is_stable_for_job_retries(self) -> None:
        first = scheduled_observation_time(
            datetime(2026, 8, 24, 11, 7, 1, tzinfo=UTC),
            300,
        )
        retried = scheduled_observation_time(
            datetime(2026, 8, 24, 11, 9, 59, tzinfo=UTC),
            300,
        )

        self.assertEqual(first, datetime(2026, 8, 24, 11, 5, tzinfo=UTC))
        self.assertEqual(first, retried)

    def test_collection_request_is_bounded_and_deterministic(self) -> None:
        config = runtime_config()
        observed_at = datetime(2026, 8, 24, 11, 5, tzinfo=UTC)

        request = build_collection_request(config, observed_at)

        self.assertEqual(
            request.request_id,
            "req-stategraph-inventory-20260824t110500z",
        )
        self.assertEqual(
            request.incident_id,
            "inc-stategraph-inventory-20260824t110500z",
        )
        self.assertEqual(
            request.scope.resource_names,
            ("checkoutservice", "frontend"),
        )
        self.assertEqual(
            request.scope.resource_name_prefixes,
            ("checkoutservice-", "frontend-"),
        )
        self.assertEqual(request.scope.max_items, 100)

    def test_runtime_rejects_an_interval_that_cannot_align_to_an_hour(self) -> None:
        candidate = runtime_config()

        with self.assertRaisesRegex(ValueError, "divide one hour"):
            RuntimeConfig(
                **{
                    **candidate.__dict__,
                    "schedule_interval_seconds": 420,
                }
            )

    def test_secret_environment_values_preserve_exact_bytes(self) -> None:
        environment = {
            "STATEGRAPH_CLUSTER_ID": "agent-rca-runtime-test",
            "STATEGRAPH_TARGET_NAMESPACE": "online-boutique",
            "STATEGRAPH_APPLICATION_SERVICES": "frontend,checkoutservice",
            "NEO4J_URI": "bolt://neo4j.graph-rca.svc.cluster.local:7687",
            "NEO4J_USERNAME": "neo4j",
            "NEO4J_PASSWORD": "neo4j-test-password\n",
            "POSTGRES_HOST": "postgresql.incident-platform.svc.cluster.local",
            "POSTGRES_DATABASE": "agent_rca",
            "POSTGRES_USERNAME": "agent_rca",
            "POSTGRES_PASSWORD": "postgres-test-password\n",
        }

        with patch.dict("os.environ", environment, clear=True):
            config = RuntimeConfig.from_environment()

        self.assertEqual(config.neo4j_password, "neo4j-test-password\n")
        self.assertEqual(config.postgres_password, "postgres-test-password\n")


if __name__ == "__main__":
    unittest.main()
