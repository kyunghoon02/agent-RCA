from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

from tools.run_online_boutique_workload import (
    deterministic_action_names,
    deterministic_coverage_action_names,
    require_loopback_base_url,
    run_workload,
)


class OnlineBoutiqueWorkloadTests(unittest.TestCase):
    def test_loopback_tunnel_is_required(self) -> None:
        self.assertEqual(
            require_loopback_base_url("http://127.0.0.1:18081/k8s/proxy"),
            "http://127.0.0.1:18081/k8s/proxy/",
        )
        for value in (
            "https://127.0.0.1:18081",
            "http://10.0.0.2:8080",
            "http://user@localhost:8080",
            "http://localhost",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    require_loopback_base_url(value)

    def test_action_schedule_is_seeded_and_repeatable(self) -> None:
        first = deterministic_action_names(42, 25)
        second = deterministic_action_names(42, 25)
        different = deterministic_action_names(43, 25)

        self.assertEqual(first, second)
        self.assertNotEqual(first, different)
        self.assertGreater(first.count("checkout"), first.count("home"))

    def test_coverage_schedule_exercises_every_owned_frontend_route(self) -> None:
        first_cycle = deterministic_coverage_action_names(6)
        second_cycle = deterministic_coverage_action_names(12)[6:]

        self.assertEqual(
            first_cycle,
            [
                "home",
                "product",
                "add-cart",
                "view-cart",
                "empty-cart",
                "checkout",
            ],
        )
        self.assertEqual(second_cycle, first_cycle)

    def test_normal_profile_is_bounded_and_not_checkout_weighted(self) -> None:
        clock = [0.0]

        def monotonic() -> float:
            return clock[0]

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        result = run_workload(
            base_url="http://127.0.0.1:18081/",
            duration_seconds=10,
            requests_per_second=2,
            seed=44,
            marker="normal-control",
            timeout_seconds=1,
            stop_file=None,
            profile="normal",
            monotonic=monotonic,
            sleep=sleep,
        )

        self.assertEqual(result["profile"], "normal")
        self.assertGreater(result["actions"].get("home", 0), 0)
        self.assertLess(
            result["actions"].get("checkout", 0),
            result["actions"].get("home", 0),
        )

    def test_transport_errors_are_aggregated_by_reason_type(self) -> None:
        clock = [0.0]

        def monotonic() -> float:
            return clock[0]

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError(
            ConnectionRefusedError("connection refused")
        )
        with mock.patch(
            "tools.run_online_boutique_workload.urllib.request.build_opener",
            return_value=opener,
        ):
            result = run_workload(
                base_url="http://127.0.0.1:18081/",
                duration_seconds=10,
                requests_per_second=2,
                seed=44,
                marker="transport-diagnostic",
                timeout_seconds=1,
                stop_file=None,
                profile="normal",
                monotonic=monotonic,
                sleep=sleep,
            )

        self.assertEqual(result["schema_version"], "1.1.0")
        self.assertEqual(result["transport_errors"], result["request_attempts"])
        self.assertEqual(
            result["transport_error_types"],
            {"ConnectionRefusedError": result["request_attempts"]},
        )


if __name__ == "__main__":
    unittest.main()
