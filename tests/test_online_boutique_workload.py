from __future__ import annotations

import unittest

from tools.run_online_boutique_workload import (
    deterministic_action_names,
    deterministic_coverage_action_names,
    require_loopback_base_url,
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


if __name__ == "__main__":
    unittest.main()
