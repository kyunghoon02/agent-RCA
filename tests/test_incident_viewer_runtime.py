from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tools.run_incident_viewer import (
    LazyIncidentViewerApplication,
    ViewerRuntimeConfig,
)


class IncidentViewerRuntimeTests(unittest.TestCase):
    def test_environment_preserves_secret_values_and_bounds_response_size(self) -> None:
        environment = {
            "VIEWER_BEARER_TOKEN": "viewer-runtime-token-with-space ",
            "VIEWER_MAX_RESPONSE_BYTES": "1048576",
            "POSTGRES_HOST": "postgresql",
            "POSTGRES_PORT": "5432",
            "POSTGRES_DATABASE": "agent_rca",
            "POSTGRES_USERNAME": "viewer",
            "POSTGRES_PASSWORD": "database-password-with-space ",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = ViewerRuntimeConfig.from_environment()

        self.assertEqual(
            config.bearer_token, "viewer-runtime-token-with-space "
        )
        self.assertEqual(
            config.postgres_password, "database-password-with-space "
        )
        self.assertEqual(config.max_response_bytes, 1048576)

    def test_lazy_application_initializes_once(self) -> None:
        calls = []

        class Application:
            def __call__(self, environ, start_response):
                start_response("200 OK", [])
                return [b"ok"]

        config = object()

        def build(candidate):
            calls.append(candidate)
            return Application()

        application = LazyIncidentViewerApplication(
            config_factory=lambda: config,
            application_factory=build,
        )
        start_response = lambda *_: None

        self.assertEqual(application({}, start_response), [b"ok"])
        self.assertEqual(application({}, start_response), [b"ok"])
        self.assertEqual(calls, [config])


if __name__ == "__main__":
    unittest.main()
