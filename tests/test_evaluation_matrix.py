from __future__ import annotations

import copy
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tools.run_evaluation_matrix import (
    ALLOWED_SCENARIOS,
    HOLDOUT_FAMILIES,
    MatrixError,
    _validate_holdout_matrix,
    build_schedule,
    execute_matrix,
    load_matrix,
)
from tools.summarize_evaluation_matrix import METRIC_NAMES, build_summary


NOW = datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc)


class EvaluationMatrixTests(unittest.TestCase):
    def test_frozen_matrix_builds_rotated_twenty_attempt_schedule(self) -> None:
        matrix = load_matrix()
        schedule = build_schedule(matrix)

        self.assertEqual(len(schedule), 20)
        self.assertEqual(
            {item["scenario_id"] for item in schedule}, set(ALLOWED_SCENARIOS)
        )
        for scenario_id in ALLOWED_SCENARIOS:
            self.assertEqual(
                sum(item["scenario_id"] == scenario_id for item in schedule), 5
            )
        self.assertEqual(
            schedule[4]["scenario_id"], matrix["scenarios"][1]["scenario_id"]
        )
        self.assertTrue(
            all(
                item["make_target"]
                == ALLOWED_SCENARIOS[item["scenario_id"]]["make_target"]
                for item in schedule
            )
        )

    def test_actual_matrix_requires_commit_bound_confirmation_before_writes(
        self,
    ) -> None:
        matrix = load_matrix()
        schedule = build_schedule(matrix)
        source = {
            "source_commit": "a" * 40,
            "origin_main_commit": "a" * 40,
            "runtime_image_tag": "runtime-fixture",
            "runtime_image_digest": f"sha256:{'b' * 64}",
        }

        with patch(
            "tools.run_evaluation_matrix._source_snapshot", return_value=source
        ), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(MatrixError, "full current HEAD commit"):
                execute_matrix(
                    matrix,
                    schedule,
                    resume=None,
                )

    def test_structured_output_matrix_is_two_rotated_passes(self) -> None:
        matrix = load_matrix("structured-output-v2")
        schedule = build_schedule(matrix)

        self.assertEqual(matrix["repetitions_per_scenario"], 2)
        self.assertEqual(len(schedule), 8)
        for scenario_id in ALLOWED_SCENARIOS:
            self.assertEqual(
                sum(item["scenario_id"] == scenario_id for item in schedule),
                2,
            )
        self.assertEqual(
            schedule[4]["scenario_id"], matrix["scenarios"][1]["scenario_id"]
        )

    def test_holdout_matrix_freezes_twelve_unique_single_run_variants(self) -> None:
        matrix = load_matrix("holdout-v1")
        schedule = build_schedule(matrix)

        self.assertEqual(matrix["repetitions_per_scenario"], 1)
        self.assertEqual(len(schedule), 12)
        self.assertEqual(len({item["scenario_id"] for item in schedule}), 12)
        self.assertEqual(
            {item["scenario_family"] for item in schedule}, set(HOLDOUT_FAMILIES)
        )
        for family in HOLDOUT_FAMILIES:
            family_attempts = [
                item for item in schedule if item["scenario_family"] == family
            ]
            self.assertEqual(
                {item["variant_id"] for item in family_attempts}, {"a", "b", "c"}
            )
            self.assertTrue(
                all(item.get("scenario_path_variable") for item in family_attempts)
            )

    def test_holdout_matrix_rejects_changed_scenario_digest(self) -> None:
        matrix = copy.deepcopy(load_matrix("holdout-v1"))
        matrix["scenarios"][0]["scenario_sha256"] = "0" * 64

        with self.assertRaisesRegex(MatrixError, "frozen digest"):
            _validate_holdout_matrix(matrix)

    def test_holdout_requires_its_own_commit_bound_confirmation(self) -> None:
        matrix = load_matrix("holdout-v1")
        schedule = build_schedule(matrix)
        source = {
            "source_commit": "a" * 40,
            "origin_main_commit": "a" * 40,
            "runtime_image_tag": "runtime-fixture",
            "runtime_image_digest": f"sha256:{'b' * 64}",
        }

        with patch(
            "tools.run_evaluation_matrix._source_snapshot", return_value=source
        ), patch.dict(
            os.environ, {"CONFIRM_EVALUATION_MATRIX": "a" * 40}, clear=True
        ):
            with self.assertRaisesRegex(
                MatrixError, "CONFIRM_HOLDOUT_EVALUATION_MATRIX"
            ):
                execute_matrix(matrix, schedule, resume=None)

    def test_summary_is_id_free_and_keeps_latency_usage_and_accuracy(self) -> None:
        matrix = load_matrix()
        schedule = build_schedule(matrix)
        attempts = []
        records = []
        for planned in schedule[:4]:
            attempts.append(
                {
                    "attempt": planned["attempt"],
                    "repetition": planned["repetition"],
                    "scenario_id": planned["scenario_id"],
                    "expected_outcome": planned["expected_outcome"],
                    "state": "PASSED",
                }
            )
            metrics = {name: 1.0 for name in METRIC_NAMES}
            records.append(
                {
                    "attempt": attempts[-1],
                    "prediction": {"outcome": planned["expected_outcome"]},
                    "result": {"metrics": metrics},
                    "runtime": {
                        "agent_status": "SUCCEEDED",
                        "reason_code": "REPORT_ACCEPTED",
                        "ingest_to_agent_start_ms": 7000,
                        "ingest_to_terminal_ms": 23000,
                        "ingest_to_report_ms": 24000,
                        "usage": {
                            "llm_calls": 2,
                            "tool_calls": 3,
                            "input_tokens": 1000,
                            "output_tokens": 200,
                            "total_tokens": 1200,
                            "wall_time_ms": 16000,
                        },
                    },
                }
            )
        manifest = {
            "state": "RUNNING",
            "source": {
                "source_commit": "a" * 40,
                "runtime_image_tag": "runtime-fixture",
                "runtime_image_digest": f"sha256:{'b' * 64}",
            },
            "schedule": schedule,
            "attempts": attempts,
        }

        summary = build_summary(
            matrix, manifest, records, generated_at=NOW
        )
        serialized = json.dumps(summary)

        self.assertNotIn("incident_id", serialized)
        self.assertNotIn("evaluation_case_id", serialized)
        self.assertEqual(summary["scored_runs"], 4)
        self.assertEqual(summary["cost"]["status"], "NOT_CALCULATED")
        self.assertEqual(
            summary["scenarios"][0]["latency_ms"]["ingest_to_report_ms"][
                "mean"
            ],
            24000.0,
        )
        self.assertEqual(
            summary["scenarios"][0]["usage"]["total_tokens"]["total"],
            1200,
        )
        self.assertEqual(
            summary["scenarios"][0]["metrics"][
                "root_cause_top1_accuracy"
            ]["bootstrap_95"],
            [1.0, 1.0],
        )

    def test_holdout_summary_aggregates_each_family_without_private_ids(self) -> None:
        matrix = load_matrix("holdout-v1")
        schedule = build_schedule(matrix)
        attempts = []
        records = []
        for planned in schedule:
            attempt = {
                "attempt": planned["attempt"],
                "repetition": planned["repetition"],
                "scenario_id": planned["scenario_id"],
                "expected_outcome": planned["expected_outcome"],
                "state": "PASSED",
            }
            attempts.append(attempt)
            records.append(
                {
                    "attempt": attempt,
                    "prediction": {"outcome": planned["expected_outcome"]},
                    "result": {"metrics": {name: 1.0 for name in METRIC_NAMES}},
                    "runtime": {
                        "agent_status": "SUCCEEDED",
                        "reason_code": "REPORT_ACCEPTED",
                        "ingest_to_agent_start_ms": 1000,
                        "ingest_to_terminal_ms": 3000,
                        "ingest_to_report_ms": 3000,
                        "usage": {
                            "llm_calls": 2,
                            "tool_calls": 2,
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "total_tokens": 120,
                            "wall_time_ms": 2000,
                        },
                    },
                }
            )
        manifest = {
            "state": "COMPLETED",
            "source": {
                "source_commit": "a" * 40,
                "runtime_image_tag": "runtime-fixture",
                "runtime_image_digest": f"sha256:{'b' * 64}",
            },
            "schedule": schedule,
            "attempts": attempts,
        }

        summary = build_summary(matrix, manifest, records, generated_at=NOW)
        serialized = json.dumps(summary)

        self.assertEqual(summary["expected_outcome_matches"], 12)
        self.assertEqual(summary["agent_reason_codes"], {"REPORT_ACCEPTED": 12})
        self.assertEqual(len(summary["families"]), 4)
        self.assertTrue(
            all(family["scored_runs"] == 3 for family in summary["families"])
        )
        self.assertTrue(
            all(
                family["expected_outcome_matches"] == 3
                for family in summary["families"]
            )
        )
        self.assertNotIn("incident_id", serialized)


if __name__ == "__main__":
    unittest.main()
