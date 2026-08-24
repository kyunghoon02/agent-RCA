from __future__ import annotations

import unittest
from unittest.mock import patch

from tools.run_incident_receiver import (
    LazyIncidentReceiverApplication,
    ReceiverRuntimeConfig,
)


class IncidentReceiverRuntimeTests(unittest.TestCase):
    def test_secret_environment_values_preserve_exact_bytes(self) -> None:
        environment = {
            "WEBHOOK_BEARER_TOKEN": "receiver-token-with-newline\n",
            "POSTGRES_HOST": "postgresql.incident-platform.svc.cluster.local",
            "POSTGRES_DATABASE": "agent_rca",
            "POSTGRES_USERNAME": "agent_rca",
            "POSTGRES_PASSWORD": "postgres-password-with-newline\n",
        }

        with patch.dict("os.environ", environment, clear=True):
            config = ReceiverRuntimeConfig.from_environment()

        self.assertEqual(config.bearer_token, "receiver-token-with-newline\n")
        self.assertEqual(
            config.postgres_password,
            "postgres-password-with-newline\n",
        )

    def test_runtime_rejects_an_oversized_body_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "body limit"):
            ReceiverRuntimeConfig(
                bearer_token="receiver-token-test-only",
                max_body_bytes=4 * 1024 * 1024 + 1,
                max_alerts_per_request=100,
                postgres_host="postgresql",
                postgres_port=5432,
                postgres_database="agent_rca",
                postgres_username="agent_rca",
                postgres_password="test-only",
            )

    def test_lazy_application_is_initialized_once(self) -> None:
        calls: list[str] = []

        def config_factory() -> ReceiverRuntimeConfig:
            calls.append("config")
            return ReceiverRuntimeConfig(
                bearer_token="receiver-token-test-only",
                max_body_bytes=1024,
                max_alerts_per_request=10,
                postgres_host="postgresql",
                postgres_port=5432,
                postgres_database="agent_rca",
                postgres_username="agent_rca",
                postgres_password="test-only",
            )

        def application_factory(config: ReceiverRuntimeConfig):
            calls.append(config.postgres_host)

            def application(environ, start_response):
                start_response("200 OK", [])
                return [b"ok"]

            return application

        application = LazyIncidentReceiverApplication(
            config_factory=config_factory,
            application_factory=application_factory,
        )

        for _ in range(2):
            statuses: list[str] = []
            body = application({}, lambda status, _: statuses.append(status))
            self.assertEqual(body, [b"ok"])
            self.assertEqual(statuses, ["200 OK"])

        self.assertEqual(calls, ["config", "postgresql"])


if __name__ == "__main__":
    unittest.main()
