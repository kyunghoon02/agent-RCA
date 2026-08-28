from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from incident_platform.contracts import validate_contract
from incident_platform.deterministic import (
    DeterministicDecision,
    RuleEvaluation,
)
from incident_platform.errors import ContractViolation
from incident_platform.rca_evaluation import (
    evaluate_rca_case,
    prediction_from_agent_report,
    prediction_from_failed_agent_run,
    prediction_from_deterministic_decision,
)


EVALUATED_AT = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)


def ground_truth(**overrides):
    value = {
        "schema_version": "1.0.0",
        "evaluation_case_id": "eval-checkout-oom-0001",
        "scenario_id": "scenario-checkout-oom-change-stress",
        "incident_id": "inc-checkout-oom-0001",
        "expected_outcome": "ROOT_CAUSE",
        "expected_root_cause_ids": ["kubernetes.container-oomkilled"],
        "relevant_evidence_ids": [
            "ev-kernel-oom-0001",
            "ev-memory-ratio-0001",
            "ev-restart-delta-0001",
        ],
        "labeled_at": "2026-08-26T08:30:00Z",
        "labeler": "controlled-fault-manifest",
        "provenance": {
            "controlled_fault": True,
            "fault_manifest_sha256": "sha256:" + "a" * 64,
            "workload_profile": "path-weighted",
            "workload_seed": 42,
            "change_applied": True,
        },
    }
    value.update(overrides)
    return value


def prediction(**overrides):
    value = {
        "schema_version": "1.0.0",
        "evaluation_case_id": "eval-checkout-oom-0001",
        "scenario_id": "scenario-checkout-oom-change-stress",
        "incident_id": "inc-checkout-oom-0001",
        "variant_id": "A",
        "path": "fast",
        "outcome": "ROOT_CAUSE",
        "predicted_root_cause_ids": ["kubernetes.container-oomkilled"],
        "cited_evidence_ids": [
            "ev-kernel-oom-0001",
            "ev-memory-ratio-0001",
            "ev-restart-delta-0001",
        ],
        "available_evidence_ids": [
            "ev-kernel-oom-0001",
            "ev-memory-ratio-0001",
            "ev-restart-delta-0001",
            "ev-deployment-state-0001",
        ],
        "completed_at": "2026-08-26T08:45:00Z",
    }
    value.update(overrides)
    return value


def agent_report(**overrides):
    entity = {
        "api_version": "v1",
        "kind": "Pod",
        "namespace": "online-boutique",
        "name": "checkoutservice-abc",
        "uid": "uid-checkoutservice-abc",
        "exists": True,
    }
    value = {
        "schema_version": "1.1.0",
        "report_id": "rpt-checkout-oom-agent-0001",
        "incident_id": "inc-checkout-oom-0001",
        "context_id": "ctx-checkout-oom-agent-0001",
        "path": "deep",
        "status": "conclusive",
        "generated_at": "2026-08-26T08:44:00Z",
        "root_cause": {
            "cause_id": "kubernetes.container-oomkilled",
            "summary": "The checkout container was OOM-killed.",
            "entity": entity,
            "supporting_evidence_ids": [
                "ev-kernel-oom-0001",
                "ev-restart-delta-0001",
            ],
            "reference_document_ids": [],
        },
        "hypotheses": [
            {
                "rank": 1,
                "cause_id": "kubernetes.container-oomkilled",
                "summary": "The checkout container was OOM-killed.",
                "entity": entity,
                "confidence": 0.95,
                "status": "supported",
                "supporting_evidence_ids": [
                    "ev-kernel-oom-0001",
                    "ev-restart-delta-0001",
                ],
                "contradicting_evidence_ids": [],
                "reference_document_ids": [],
                "missing_evidence": [],
            }
        ],
        "remediation": {
            "suggestions": ["Review the memory limit."],
            "verification_conditions": ["The Pod remains Ready."],
        },
        "budget": {
            "applicable": True,
            "llm_calls": 2,
            "tool_calls": 3,
            "tree_depth": 2,
            "wall_time_ms": 12000,
            "exhausted": False,
        },
        "read_only": True,
        "limitations": [],
    }
    value.update(overrides)
    return value


def failed_agent_run(**overrides):
    value = {
        "schema_version": "1.0.0",
        "agent_run_id": "arun-checkout-oom-agent-0001",
        "incident_id": "inc-checkout-oom-0001",
        "context_id": "ctx-checkout-oom-agent-0001",
        "knowledge_audit_id": "kaud-checkout-oom-agent-0001",
        "knowledge_status": "SUCCEEDED",
        "model": "fixture-agent",
        "status": "GATE_REJECTED",
        "reason_code": "EVIDENCE_GATE_REJECTED",
        "started_at": "2026-08-26T08:43:00Z",
        "completed_at": "2026-08-26T08:44:00Z",
        "budget": {
            "max_turns": 6,
            "max_llm_calls": 6,
            "max_tool_calls": 12,
            "max_evidence_candidates": 8,
            "max_output_tokens": 2000,
            "max_wall_time_ms": 60000,
        },
        "usage": {
            "llm_calls": 2,
            "tool_calls": 3,
            "input_tokens": 1000,
            "output_tokens": 200,
            "total_tokens": 1200,
            "wall_time_ms": 12000,
        },
        "tool_events": [],
        "retrieved_reference_ids": [],
        "inspected_evidence_ids": ["ev-kernel-oom-0001"],
        "inspected_reference_document_ids": [],
        "cited_evidence_ids": ["ev-kernel-oom-0001"],
        "cited_reference_document_ids": [],
    }
    value.update(overrides)
    return value


class RCAEvaluationTests(unittest.TestCase):
    def test_failed_agent_run_is_not_misreported_as_abstention(self) -> None:
        actual = prediction_from_failed_agent_run(
            evaluation_case_id="eval-checkout-oom-0001",
            scenario_id="scenario-checkout-oom-change-stress",
            incident_id="inc-checkout-oom-0001",
            agent_run=failed_agent_run(),
            available_evidence_ids=prediction()["available_evidence_ids"],
            completed_at=EVALUATED_AT,
        )

        self.assertEqual(actual["variant_id"], "C")
        self.assertEqual(actual["outcome"], "FAILED")
        self.assertEqual(actual["predicted_root_cause_ids"], [])
        result = evaluate_rca_case(
            ground_truth(), actual, evaluated_at=EVALUATED_AT
        )
        self.assertEqual(result["metrics"]["root_cause_top1_accuracy"], 0.0)
        self.assertEqual(result["metrics"]["abstention_correctness"], 0.0)

    def test_failed_agent_does_not_receive_correct_abstention_credit(self) -> None:
        actual = prediction_from_failed_agent_run(
            evaluation_case_id="eval-checkout-oom-0001",
            scenario_id="scenario-checkout-oom-change-stress",
            incident_id="inc-checkout-oom-0001",
            agent_run=failed_agent_run(),
            available_evidence_ids=prediction()["available_evidence_ids"],
            completed_at=EVALUATED_AT,
        )

        result = evaluate_rca_case(
            ground_truth(
                expected_outcome="ABSTAIN",
                expected_root_cause_ids=[],
                relevant_evidence_ids=[],
            ),
            actual,
            evaluated_at=EVALUATED_AT,
        )

        self.assertEqual(result["metrics"]["abstention_correctness"], 0.0)

    def test_agent_report_becomes_variant_c_prediction_without_text_inference(self) -> None:
        report = agent_report()
        report["root_cause"]["summary"] = "Free text is not used as the label."

        actual = prediction_from_agent_report(
            evaluation_case_id="eval-checkout-oom-0001",
            scenario_id="scenario-checkout-oom-change-stress",
            incident_id="inc-checkout-oom-0001",
            report=report,
            available_evidence_ids=prediction()["available_evidence_ids"],
            completed_at=EVALUATED_AT,
        )

        self.assertEqual(actual["variant_id"], "C")
        self.assertEqual(actual["path"], "deep")
        self.assertEqual(actual["outcome"], "ROOT_CAUSE")
        self.assertEqual(
            actual["predicted_root_cause_ids"],
            ["kubernetes.container-oomkilled"],
        )
        self.assertEqual(
            actual["cited_evidence_ids"],
            ["ev-kernel-oom-0001", "ev-restart-delta-0001"],
        )

    def test_rootless_agent_report_maps_to_abstain(self) -> None:
        report = agent_report(
            status="inconclusive",
            root_cause=None,
            remediation={
                "suggestions": [],
                "verification_conditions": ["Collect another runtime signal."],
            },
        )
        report["hypotheses"][0]["status"] = "unresolved"

        actual = prediction_from_agent_report(
            evaluation_case_id="eval-checkout-oom-0001",
            scenario_id="scenario-checkout-oom-change-stress",
            incident_id="inc-checkout-oom-0001",
            report=report,
            available_evidence_ids=prediction()["available_evidence_ids"],
            completed_at=EVALUATED_AT,
        )

        self.assertEqual(actual["outcome"], "ABSTAIN")
        self.assertEqual(actual["predicted_root_cause_ids"], [])

    def test_fast_path_decision_becomes_a_variant_a_prediction(self) -> None:
        decision = DeterministicDecision(
            status="PROVEN",
            root_cause_id="kubernetes.container-oomkilled",
            statement="The Pod was OOM-killed.",
            supporting_evidence_ids=(
                "ev-kernel-oom-0001",
                "ev-memory-ratio-0001",
                "ev-restart-delta-0001",
            ),
            missing_requirements=tuple(),
            evaluations=(
                RuleEvaluation(
                    rule_id="kubernetes.container-oomkilled",
                    status="PROVEN",
                    statement="The Pod was OOM-killed.",
                    supporting_evidence_ids=(
                        "ev-kernel-oom-0001",
                        "ev-memory-ratio-0001",
                        "ev-restart-delta-0001",
                    ),
                ),
            ),
        )

        actual = prediction_from_deterministic_decision(
            evaluation_case_id="eval-checkout-oom-0001",
            scenario_id="scenario-checkout-oom-change-stress",
            incident_id="inc-checkout-oom-0001",
            decision=decision,
            available_evidence_ids=prediction()["available_evidence_ids"],
            completed_at=EVALUATED_AT,
        )

        validate_contract("rca-evaluation-prediction.schema.json", actual)
        self.assertEqual(actual["variant_id"], "A")
        self.assertEqual(actual["outcome"], "ROOT_CAUSE")
        self.assertEqual(
            actual["predicted_root_cause_ids"],
            ["kubernetes.container-oomkilled"],
        )
        self.assertEqual(len(actual["cited_evidence_ids"]), 3)

    def test_abstain_prediction_retains_only_incomplete_rule_evidence(self) -> None:
        decision = DeterministicDecision(
            status="ABSTAIN",
            root_cause_id=None,
            statement=None,
            supporting_evidence_ids=tuple(),
            missing_requirements=("restart metric",),
            evaluations=(
                RuleEvaluation(
                    rule_id="kubernetes.container-oomkilled",
                    status="INSUFFICIENT",
                    statement="OOM signal lacks corroboration.",
                    supporting_evidence_ids=("ev-kernel-oom-0001",),
                ),
            ),
        )

        actual = prediction_from_deterministic_decision(
            evaluation_case_id="eval-checkout-oom-0001",
            scenario_id="scenario-checkout-oom-change-stress",
            incident_id="inc-checkout-oom-0001",
            decision=decision,
            available_evidence_ids=prediction()["available_evidence_ids"],
            completed_at=EVALUATED_AT,
        )

        self.assertEqual(actual["outcome"], "ABSTAIN")
        self.assertEqual(actual["predicted_root_cause_ids"], [])
        self.assertEqual(actual["cited_evidence_ids"], ["ev-kernel-oom-0001"])

    def test_ambiguous_prediction_preserves_proven_rule_order(self) -> None:
        evaluations = tuple(
            RuleEvaluation(
                rule_id=rule_id,
                status="PROVEN",
                statement="fixture",
                supporting_evidence_ids=(evidence_id,),
            )
            for rule_id, evidence_id in (
                ("kubernetes.container-oomkilled", "ev-kernel-oom-0001"),
                ("kubernetes.image-pull-failure", "ev-deployment-state-0001"),
            )
        )
        decision = DeterministicDecision(
            status="AMBIGUOUS",
            root_cause_id=None,
            statement=None,
            supporting_evidence_ids=(
                "ev-kernel-oom-0001",
                "ev-deployment-state-0001",
            ),
            missing_requirements=("multiple roots",),
            evaluations=evaluations,
        )

        actual = prediction_from_deterministic_decision(
            evaluation_case_id="eval-checkout-oom-0001",
            scenario_id="scenario-checkout-oom-change-stress",
            incident_id="inc-checkout-oom-0001",
            decision=decision,
            available_evidence_ids=prediction()["available_evidence_ids"],
            completed_at=EVALUATED_AT,
        )

        self.assertEqual(actual["outcome"], "AMBIGUOUS")
        self.assertEqual(
            actual["predicted_root_cause_ids"],
            [
                "kubernetes.container-oomkilled",
                "kubernetes.image-pull-failure",
            ],
        )

    def test_exact_oom_prediction_scores_complete_evidence(self) -> None:
        result = evaluate_rca_case(
            ground_truth(),
            prediction(),
            evaluated_at=EVALUATED_AT,
        )

        validate_contract("rca-evaluation-result.schema.json", result)
        self.assertEqual(result["metrics"]["root_cause_top1_accuracy"], 1.0)
        self.assertEqual(result["metrics"]["root_cause_top3_recall"], 1.0)
        self.assertEqual(result["metrics"]["evidence_precision"], 1.0)
        self.assertEqual(result["metrics"]["evidence_recall"], 1.0)
        self.assertEqual(
            result["metrics"]["unsupported_evidence_citation_rate"], 0.0
        )
        self.assertEqual(result["metrics"]["abstention_correctness"], 1.0)

        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("kubernetes.container-oomkilled", serialized)
        self.assertNotIn("ev-kernel-oom-0001", serialized)

    def test_irrelevant_and_unsupported_citations_reduce_precision(self) -> None:
        result = evaluate_rca_case(
            ground_truth(
                relevant_evidence_ids=[
                    "ev-kernel-oom-0001",
                    "ev-memory-ratio-0001",
                ]
            ),
            prediction(
                cited_evidence_ids=[
                    "ev-kernel-oom-0001",
                    "ev-deployment-state-0001",
                    "ev-unsupported-citation-0001",
                ]
            ),
            evaluated_at=EVALUATED_AT,
        )

        self.assertEqual(result["counts"]["matched_evidence"], 1)
        self.assertEqual(result["counts"]["unsupported_citations"], 1)
        self.assertAlmostEqual(result["metrics"]["evidence_precision"], 1 / 3, 6)
        self.assertEqual(result["metrics"]["evidence_recall"], 0.5)
        self.assertAlmostEqual(
            result["metrics"]["unsupported_evidence_citation_rate"], 1 / 3, 6
        )

    def test_correct_abstention_has_no_undefined_evidence_scores(self) -> None:
        result = evaluate_rca_case(
            ground_truth(
                expected_outcome="ABSTAIN",
                expected_root_cause_ids=[],
                relevant_evidence_ids=[],
                provenance={
                    "controlled_fault": False,
                    "fault_manifest_sha256": None,
                    "workload_profile": "normal",
                    "workload_seed": 7,
                    "change_applied": False,
                },
            ),
            prediction(
                outcome="ABSTAIN",
                predicted_root_cause_ids=[],
                cited_evidence_ids=[],
            ),
            evaluated_at=EVALUATED_AT,
        )

        self.assertIsNone(result["metrics"]["root_cause_top1_accuracy"])
        self.assertIsNone(result["metrics"]["root_cause_top3_recall"])
        self.assertIsNone(result["metrics"]["evidence_precision"])
        self.assertIsNone(result["metrics"]["evidence_recall"])
        self.assertEqual(
            result["metrics"]["unsupported_evidence_citation_rate"], 0.0
        )
        self.assertEqual(result["metrics"]["abstention_correctness"], 1.0)

    def test_fault_with_no_citations_scores_zero_precision_and_recall(self) -> None:
        result = evaluate_rca_case(
            ground_truth(),
            prediction(
                outcome="ABSTAIN",
                predicted_root_cause_ids=[],
                cited_evidence_ids=[],
            ),
            evaluated_at=EVALUATED_AT,
        )

        self.assertEqual(result["metrics"]["evidence_precision"], 0.0)
        self.assertEqual(result["metrics"]["evidence_recall"], 0.0)
        self.assertEqual(result["metrics"]["abstention_correctness"], 0.0)

    def test_top1_and_top3_are_scored_separately(self) -> None:
        result = evaluate_rca_case(
            ground_truth(),
            prediction(
                outcome="AMBIGUOUS",
                predicted_root_cause_ids=[
                    "kubernetes.image-pull-failure",
                    "kubernetes.container-oomkilled",
                ],
            ),
            evaluated_at=EVALUATED_AT,
        )

        self.assertEqual(result["metrics"]["root_cause_top1_accuracy"], 0.0)
        self.assertEqual(result["metrics"]["root_cause_top3_recall"], 1.0)
        self.assertEqual(result["metrics"]["abstention_correctness"], 1.0)

    def test_multi_factor_scores_exact_and_partial_matches(self) -> None:
        result = evaluate_rca_case(
            ground_truth(
                expected_root_cause_ids=[
                    "kubernetes.container-oomkilled",
                    "kubernetes.network-policy-drop",
                ]
            ),
            prediction(
                predicted_root_cause_ids=["kubernetes.container-oomkilled"]
            ),
            evaluated_at=EVALUATED_AT,
        )

        self.assertEqual(result["metrics"]["multi_factor_exact_match"], 0.0)
        self.assertEqual(result["metrics"]["multi_factor_partial_match"], 0.5)

    def test_ground_truth_must_match_the_prediction_snapshot(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "outside.*snapshot"):
            evaluate_rca_case(
                ground_truth(
                    relevant_evidence_ids=["ev-label-drifted-0001"]
                ),
                prediction(),
                evaluated_at=EVALUATED_AT,
            )

    def test_case_identity_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "identity mismatch"):
            evaluate_rca_case(
                ground_truth(),
                prediction(incident_id="inc-different-case-0001"),
                evaluated_at=EVALUATED_AT,
            )

    def test_contract_rejects_abstain_with_a_predicted_cause(self) -> None:
        invalid = prediction(outcome="ABSTAIN")

        with self.assertRaises(ContractViolation):
            evaluate_rca_case(
                ground_truth(),
                invalid,
                evaluated_at=EVALUATED_AT,
            )


if __name__ == "__main__":
    unittest.main()
