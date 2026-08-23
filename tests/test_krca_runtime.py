from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from incident_platform.errors import ContractViolation
from incident_platform.krca_runtime import load_krca_runtime_config


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "online-boutique-krca.yaml"


class KRCARuntimeConfigTests(unittest.TestCase):
    def test_online_boutique_profiles_render_live_promql_scope(self) -> None:
        config = load_krca_runtime_config(CONFIG)

        self.assertEqual(
            [profile.profile_id for profile in config.profiles],
            ["checkout-payment", "recommendation-catalog"],
        )
        checkout = config.profile("checkout-payment")
        expression = config.query_spec.scoped_expression(
            config.query_spec.failure_rate_template,
            config.namespace,
            checkout.alerting_api,
        )
        self.assertEqual(
            expression,
            'agent_rca_api_failure_rate{namespace="online-boutique",'
            'service_name="frontend",span_name="POST"}',
        )
        self.assertNotIn("{{", expression)

        latency_expression = config.query_spec.scoped_expression(
            config.query_spec.latency_template,
            config.namespace,
            checkout.alerting_api,
        )
        self.assertEqual(
            latency_expression,
            'agent_rca_api_latency_p95_milliseconds{namespace="online-boutique",'
            'service_name="frontend",span_name="POST"} >= 0',
        )

    def test_dependency_cannot_escape_the_profile_scope(self) -> None:
        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        raw["profiles"][0]["dependencies"][0]["child"]["service"] = "unknown"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.yaml"
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            with self.assertRaisesRegex(ContractViolation, "escapes resource scope"):
                load_krca_runtime_config(path)

    def test_disconnected_dependency_is_rejected(self) -> None:
        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        dependency = copy.deepcopy(raw["profiles"][0]["dependencies"][1])
        dependency["parent"] = {
            "service": "paymentservice",
            "operation": "Disconnected",
        }
        raw["profiles"][0]["dependencies"][1] = dependency

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.yaml"
            path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            with self.assertRaisesRegex(ContractViolation, "disconnected"):
                load_krca_runtime_config(path)
