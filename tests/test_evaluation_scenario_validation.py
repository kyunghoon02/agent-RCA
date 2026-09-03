from __future__ import annotations

import unittest

from tools.run_evaluation_matrix import MatrixError, load_matrix
from tools.validate_evaluation_scenario import validate_registered_scenario


class EvaluationScenarioValidationTests(unittest.TestCase):
    def test_every_registered_scenario_is_accepted_for_its_family(self) -> None:
        regression_families = {
            "scenario-checkoutservice-oom-chaos-mesh-change-stress": (
                "kubernetes.container-oomkilled"
            ),
            "scenario-paymentservice-image-pull-change-normal": (
                "kubernetes.image-pull-failure"
            ),
            "scenario-checkoutservice-missing-configmap-normal": (
                "kubernetes.missing-configmap"
            ),
            "scenario-frontend-no-fault-normal": "no-fault",
        }
        registered = [
            (item["scenario_path"], regression_families[item["scenario_id"]])
            for item in load_matrix()["scenarios"]
        ] + [
            (item["scenario_path"], item["scenario_family"])
            for item in load_matrix("holdout-v1")["scenarios"]
        ]

        for scenario_path, family in registered:
            with self.subTest(scenario=scenario_path):
                self.assertTrue(
                    validate_registered_scenario(scenario_path, family).is_file()
                )

    def test_unregistered_scenario_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(MatrixError, "registered scenario root"):
            validate_registered_scenario(
                "README.md", "kubernetes.container-oomkilled"
            )

    def test_registered_scenario_cannot_cross_fault_families(self) -> None:
        with self.assertRaisesRegex(MatrixError, "requested fault family"):
            validate_registered_scenario(
                "evaluation/scenarios/checkoutservice-oom.yaml", "no-fault"
            )


if __name__ == "__main__":
    unittest.main()
