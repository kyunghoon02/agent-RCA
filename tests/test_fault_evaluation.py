from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceDraft,
    EvidenceWindow,
    ResourceScope,
)
from incident_platform.errors import ContractViolation
from incident_platform.fault_evaluation import build_controlled_fault_evaluation
from incident_platform.fault_evaluation import (
    summarize_controlled_fault_observations,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)


def fixture_bundle(
    *,
    memory_ratio: float | None = None,
    memory_uid: str | None = None,
    incident_id: str | None = None,
) -> tuple[dict, dict]:
    with (ROOT / "tests/fixtures/deterministic/oomkilled.json").open() as handle:
        fixture = json.load(handle)
    if memory_ratio is not None:
        fixture["evidence_drafts"][2]["facts"]["peak_ratio"] = memory_ratio
    if memory_uid is not None:
        fixture["evidence_drafts"][2]["subject"]["uid"] = memory_uid
    if incident_id is not None:
        fixture["incident_id"] = incident_id
    request = CollectionRequest(
        request_id="req-controlled-oom-fixture",
        incident_id=fixture["incident_id"],
        window=EvidenceWindow(
            start="2026-08-12T00:30:00Z",
            end="2026-08-12T05:00:00Z",
        ),
        scope=ResourceScope(
            namespace="online-boutique",
            resource_names=("checkoutservice-abc",),
        ),
        timeout_seconds=1,
    )
    evidence = [
        EvidenceBuilder().build(
            EvidenceDraft(**draft), request, collected_at=NOW
        )
        for draft in fixture["evidence_drafts"]
    ]
    subject = evidence[0]["subject"]
    incident = {
        "schema_version": "1.0.0",
        "incident_id": fixture["incident_id"],
        "deduplication_key": "controlled:checkoutservice:oom:fixture-0001",
        "status": "ANALYZING",
        "severity": "critical",
        "source": "alertmanager",
        "triggered_at": "2026-08-12T01:00:00Z",
        "window": {
            "baseline_start": "2026-08-12T00:30:00Z",
            "incident_start": "2026-08-12T01:00:00Z",
            "incident_end": None,
            "recovery_end": None,
        },
        "alert": {
            "fingerprint": "controlled-oom-fixture-0001",
            "name": "AgentRCAControlledCheckoutOOM",
            "labels": {
                "namespace": "online-boutique",
                "service": "checkoutservice",
                "rca_enabled": "true",
            },
            "annotations": {"summary": "fixture"},
        },
        "source_entity": subject,
        "collector_statuses": [],
        "created_at": "2026-08-12T01:00:00Z",
        "updated_at": "2026-08-12T01:05:00Z",
    }
    context = {
        "schema_version": "1.0.0",
        "context_id": "ctx-fixture-oom-0001",
        "incident_id": fixture["incident_id"],
        "frozen_at": "2026-08-12T01:05:00Z",
        "source_entity": subject,
        "scope": {
            "namespaces": ["online-boutique"],
            "entity_uids": [subject["uid"]],
            "metapaths": [["Pod"]],
            "time_window": {
                "start": "2026-08-12T00:30:00Z",
                "end": "2026-08-12T01:05:00Z",
            },
            "max_entities": 20,
        },
        "state_paths": [],
        "evidence_ids": [item["evidence_id"] for item in evidence],
        "recent_change_evidence_ids": [],
        "missing_evidence": [],
        "collector_failures": [],
        "localization": {
            "strategy": "namespace-fallback",
            "candidate_entities_before": 1,
            "candidate_entities_after": 1,
            "context_completeness": 1.0,
        },
    }
    scenario = yaml.safe_load(
        (ROOT / "evaluation/scenarios/checkoutservice-oom.yaml").read_text()
    )
    return {"incident": incident, "context": context, "evidence": evidence}, scenario


class ControlledFaultEvaluationTests(unittest.TestCase):
    def test_exact_oom_snapshot_builds_private_label_and_correct_root_cause(self) -> None:
        bundle, scenario = fixture_bundle()

        artifacts = build_controlled_fault_evaluation(
            bundle,
            scenario,
            scenario_sha256="a" * 64,
            evaluated_at=NOW,
        )

        self.assertEqual(artifacts["prediction"]["outcome"], "ROOT_CAUSE")
        self.assertEqual(
            artifacts["prediction"]["predicted_root_cause_ids"],
            ["kubernetes.container-oomkilled"],
        )
        self.assertEqual(
            artifacts["result"]["metrics"]["root_cause_top1_accuracy"], 1.0
        )
        self.assertEqual(
            artifacts["result"]["metrics"]["evidence_recall"], 0.666667
        )
        self.assertEqual(
            artifacts["observation"]["memory_working_set_ratio_peak"], 0.99
        )
        self.assertTrue(
            artifacts["observation"]["memory_reference_threshold_met"]
        )
        self.assertEqual(
            artifacts["observation"]["evidence_gate_policy"],
            "oom-signature-restart-v2",
        )
        self.assertEqual(
            artifacts["observation"]["oom_signature_source"],
            "kubernetes-oomkilled",
        )
        serialized_result = json.dumps(artifacts["result"])
        self.assertNotIn("kubernetes.container-oomkilled", serialized_result)
        self.assertNotIn(
            artifacts["ground_truth"]["relevant_evidence_ids"][0],
            serialized_result,
        )

    def test_ground_truth_rejects_cross_uid_metric_corroboration(self) -> None:
        bundle, scenario = fixture_bundle(memory_uid="different-pod-uid")

        with self.assertRaises(ContractViolation):
            build_controlled_fault_evaluation(
                bundle,
                scenario,
                scenario_sha256="a" * 64,
                evaluated_at=NOW,
            )

    def test_low_memory_sample_is_scored_without_cherry_picking(self) -> None:
        bundle, scenario = fixture_bundle(memory_ratio=0.4)

        artifacts = build_controlled_fault_evaluation(
            bundle,
            scenario,
            scenario_sha256="a" * 64,
            evaluated_at=NOW,
        )

        self.assertEqual(artifacts["prediction"]["outcome"], "ROOT_CAUSE")
        self.assertEqual(
            artifacts["result"]["metrics"]["root_cause_top1_accuracy"], 1.0
        )
        self.assertEqual(
            artifacts["result"]["metrics"]["abstention_correctness"], 1.0
        )
        self.assertFalse(
            artifacts["observation"]["memory_reference_threshold_met"]
        )
        self.assertNotIn(
            "evidence_id", json.dumps(artifacts["observation"])
        )

    def test_observation_summary_reports_distribution_without_private_ids(self) -> None:
        observations = []
        for index, memory_ratio in enumerate((0.4, 0.8, 0.94, 0.95, 0.99)):
            bundle, scenario = fixture_bundle(
                memory_ratio=memory_ratio,
                incident_id=f"inc-fixture-oom-000{index + 1}",
            )
            artifacts = build_controlled_fault_evaluation(
                bundle,
                scenario,
                scenario_sha256="a" * 64,
                evaluated_at=NOW,
            )
            observations.append(artifacts["observation"])

        summary = summarize_controlled_fault_observations(
            observations, generated_at=NOW
        )

        self.assertEqual(summary["run_count"], 5)
        self.assertEqual(summary["prediction_outcomes"]["root_cause"], 5)
        self.assertEqual(summary["prediction_outcomes"]["abstain"], 0)
        self.assertEqual(
            summary["memory_working_set_ratio_peak"]["median"], 0.94
        )
        self.assertEqual(
            summary["memory_working_set_ratio_peak"][
                "reference_threshold_met_rate"
            ],
            0.4,
        )
        serialized = json.dumps(summary)
        self.assertNotIn("evaluation_case_id", serialized)
        self.assertNotIn("incident_id", serialized)


if __name__ == "__main__":
    unittest.main()
