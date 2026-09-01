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
                "agent_rca_enabled": "true",
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


def image_pull_fixture_bundle(*, mismatched_event_uid: bool = False) -> tuple[dict, dict]:
    bundle, _ = fixture_bundle()
    with (ROOT / "tests/fixtures/deterministic/image-pull.json").open() as handle:
        fixture = json.load(handle)
    drafts = fixture["evidence_drafts"]
    drafts[1]["facts"] = {
        "message_code": "BackOff",
        "image_pull_code": "ImagePullBackOff",
    }
    if mismatched_event_uid:
        drafts[1]["subject"]["uid"] = "different-image-pull-pod-uid"
    incident_id = bundle["incident"]["incident_id"]
    request = CollectionRequest(
        request_id="req-controlled-image-pull-fixture",
        incident_id=incident_id,
        window=EvidenceWindow(
            start="2026-08-12T00:30:00Z",
            end="2026-08-12T05:00:00Z",
        ),
        scope=ResourceScope(
            namespace="online-boutique",
            resource_names=("paymentservice",),
            resource_name_prefixes=("paymentservice-",),
        ),
        timeout_seconds=1,
    )
    evidence = [
        EvidenceBuilder().build(
            EvidenceDraft(**draft), request, collected_at=NOW
        )
        for draft in drafts
    ]
    subject = evidence[0]["subject"]
    bundle["evidence"] = evidence
    bundle["incident"]["source_entity"] = subject
    bundle["incident"]["alert"]["name"] = (
        "AgentRCAControlledPaymentImagePull"
    )
    bundle["incident"]["alert"]["labels"]["service"] = "paymentservice"
    bundle["context"]["source_entity"] = subject
    bundle["context"]["scope"]["entity_uids"] = [subject["uid"]]
    bundle["context"]["evidence_ids"] = [
        item["evidence_id"] for item in evidence
    ]
    scenario = yaml.safe_load(
        (ROOT / "evaluation/scenarios/paymentservice-image-pull.yaml").read_text()
    )
    return bundle, scenario


def with_agent_report(bundle: dict, *, cause_id: str) -> dict:
    copied = dict(bundle)
    context = copied["context"]
    evidence = copied["evidence"]
    subject = evidence[0]["subject"]
    supporting_ids = [evidence[0]["evidence_id"], evidence[1]["evidence_id"]]
    copied["report"] = {
        "schema_version": "1.1.0",
        "report_id": "rpt-controlled-oom-agent-0001",
        "incident_id": copied["incident"]["incident_id"],
        "context_id": context["context_id"],
        "path": "deep",
        "status": "conclusive",
        "generated_at": "2026-08-26T08:59:00Z",
        "root_cause": {
            "cause_id": cause_id,
            "summary": "The Agent selected a registered cause.",
            "entity": subject,
            "supporting_evidence_ids": supporting_ids,
            "reference_document_ids": [],
        },
        "hypotheses": [
            {
                "rank": 1,
                "cause_id": cause_id,
                "summary": "The Agent selected a registered cause.",
                "entity": subject,
                "confidence": 0.95,
                "status": "supported",
                "supporting_evidence_ids": supporting_ids,
                "contradicting_evidence_ids": [],
                "reference_document_ids": [],
                "missing_evidence": [],
            }
        ],
        "remediation": {
            "suggestions": ["Review the memory policy."],
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
    return copied


def with_failed_agent_run(bundle: dict) -> dict:
    copied = dict(bundle)
    evidence_id = copied["evidence"][0]["evidence_id"]
    copied["incident"] = dict(copied["incident"], status="FAILED")
    copied["agent_run"] = {
        "schema_version": "1.0.0",
        "agent_run_id": "arun-controlled-oom-agent-0001",
        "incident_id": copied["incident"]["incident_id"],
        "context_id": copied["context"]["context_id"],
        "knowledge_audit_id": "kaud-controlled-oom-agent-0001",
        "knowledge_status": "SUCCEEDED",
        "model": "fixture-agent",
        "status": "GATE_REJECTED",
        "reason_code": "EVIDENCE_GATE_REJECTED",
        "started_at": "2026-08-26T08:58:00Z",
        "completed_at": "2026-08-26T08:59:00Z",
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
        "inspected_evidence_ids": [evidence_id],
        "inspected_reference_document_ids": [],
        "cited_evidence_ids": [evidence_id],
        "cited_reference_document_ids": [],
    }
    return copied


class ControlledFaultEvaluationTests(unittest.TestCase):
    def test_image_pull_snapshot_scores_normalized_state_and_event_roles(self) -> None:
        bundle, scenario = image_pull_fixture_bundle()
        bundle = with_agent_report(
            bundle, cause_id="kubernetes.image-pull-failure"
        )

        artifacts = build_controlled_fault_evaluation(
            bundle,
            scenario,
            scenario_sha256="b" * 64,
            evaluated_at=NOW,
        )

        self.assertNotIn("observation", artifacts)
        self.assertEqual(
            artifacts["prediction"]["predicted_root_cause_ids"],
            ["kubernetes.image-pull-failure"],
        )
        self.assertEqual(
            [
                group["role"]
                for group in artifacts["ground_truth"]["relevant_evidence_groups"]
            ],
            ["image-pull-waiting-state", "matching-image-pull-event"],
        )
        self.assertEqual(
            artifacts["agent_result"]["metrics"]["evidence_precision"], 1.0
        )
        self.assertEqual(
            artifacts["agent_result"]["metrics"]["evidence_recall"], 1.0
        )

    def test_image_pull_ground_truth_rejects_a_cross_uid_event(self) -> None:
        bundle, scenario = image_pull_fixture_bundle(
            mismatched_event_uid=True
        )

        with self.assertRaisesRegex(ContractViolation, "one Pod UID"):
            build_controlled_fault_evaluation(
                bundle,
                scenario,
                scenario_sha256="b" * 64,
                evaluated_at=NOW,
            )

    def test_gate_rejected_agent_is_scored_as_failed_not_abstained(self) -> None:
        bundle, scenario = fixture_bundle()
        bundle = with_failed_agent_run(bundle)

        artifacts = build_controlled_fault_evaluation(
            bundle,
            scenario,
            scenario_sha256="a" * 64,
            evaluated_at=NOW,
        )

        self.assertEqual(artifacts["prediction"]["outcome"], "ROOT_CAUSE")
        self.assertEqual(artifacts["agent_prediction"]["outcome"], "FAILED")
        self.assertEqual(
            artifacts["agent_result"]["metrics"]["root_cause_top1_accuracy"],
            0.0,
        )
        self.assertEqual(
            artifacts["agent_result"]["metrics"]["abstention_correctness"],
            0.0,
        )

    def test_agent_report_is_scored_separately_from_variant_a_baseline(self) -> None:
        bundle, scenario = fixture_bundle()
        bundle = with_agent_report(
            bundle, cause_id="kubernetes.container-oomkilled"
        )

        artifacts = build_controlled_fault_evaluation(
            bundle,
            scenario,
            scenario_sha256="a" * 64,
            evaluated_at=NOW,
        )

        self.assertEqual(artifacts["prediction"]["variant_id"], "A")
        self.assertEqual(artifacts["agent_prediction"]["variant_id"], "C")
        self.assertEqual(
            artifacts["agent_prediction"]["predicted_root_cause_ids"],
            ["kubernetes.container-oomkilled"],
        )
        self.assertEqual(
            artifacts["agent_result"]["metrics"]["root_cause_top1_accuracy"],
            1.0,
        )

    def test_agent_score_uses_taxonomy_id_instead_of_summary(self) -> None:
        bundle, scenario = fixture_bundle()
        bundle = with_agent_report(
            bundle, cause_id="kubernetes.image-pull-failure"
        )
        bundle["report"]["root_cause"]["summary"] = "This text says OOMKilled."

        artifacts = build_controlled_fault_evaluation(
            bundle,
            scenario,
            scenario_sha256="a" * 64,
            evaluated_at=NOW,
        )

        self.assertEqual(
            artifacts["agent_result"]["metrics"]["root_cause_top1_accuracy"],
            0.0,
        )

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
            artifacts["result"]["metrics"]["evidence_recall"], 1.0
        )
        self.assertEqual(
            artifacts["ground_truth"]["relevant_evidence_ids"],
            [
                bundle["evidence"][0]["evidence_id"],
                bundle["evidence"][1]["evidence_id"],
            ],
        )
        self.assertEqual(
            artifacts["observation"]["memory_working_set_ratio_peak"], 0.99
        )
        self.assertTrue(
            artifacts["observation"]["memory_reference_threshold_met"]
        )
        self.assertEqual(
            artifacts["observation"]["evidence_gate_policy"],
            "oom-signature-union-restart-v3",
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

    def test_ground_truth_includes_all_exact_signatures_but_not_auxiliary_memory(
        self,
    ) -> None:
        bundle, scenario = fixture_bundle()
        subject = bundle["evidence"][0]["subject"]
        request = CollectionRequest(
            request_id="req-controlled-oom-loki-fixture",
            incident_id=bundle["incident"]["incident_id"],
            window=EvidenceWindow(
                start="2026-08-12T00:30:00Z",
                end="2026-08-12T05:00:00Z",
            ),
            scope=ResourceScope(
                namespace="online-boutique",
                resource_names=(subject["name"],),
            ),
            timeout_seconds=1,
        )
        loki_signature = EvidenceBuilder().build(
            EvidenceDraft(
                source="loki",
                kind="log-pattern",
                observed_at="2026-08-12T01:04:58Z",
                subject=subject,
                summary="Kernel memcg OOM matched the same checkout Pod UID.",
                facts={
                    "pattern_id": "kernel-cgroup-oom",
                    "kernel_constraint": "CONSTRAINT_MEMCG",
                    "match_count": 1,
                    "pod_uid": subject["uid"],
                },
                provider="loki-kernel-oom-provider",
                query="bounded kernel OOM query",
                locator="loki://kernel-oom/checkoutservice-abc",
            ),
            request,
            collected_at=NOW,
        )
        bundle["evidence"].append(loki_signature)
        bundle["context"]["evidence_ids"].append(loki_signature["evidence_id"])
        bundle = with_agent_report(
            bundle, cause_id="kubernetes.container-oomkilled"
        )
        bundle["report"]["root_cause"]["supporting_evidence_ids"].append(
            loki_signature["evidence_id"]
        )

        artifacts = build_controlled_fault_evaluation(
            bundle,
            scenario,
            scenario_sha256="a" * 64,
            evaluated_at=NOW,
        )

        relevant_ids = set(artifacts["ground_truth"]["relevant_evidence_ids"])
        self.assertEqual(
            relevant_ids,
            {
                bundle["evidence"][0]["evidence_id"],
                bundle["evidence"][1]["evidence_id"],
                loki_signature["evidence_id"],
            },
        )
        self.assertNotIn(bundle["evidence"][2]["evidence_id"], relevant_ids)
        self.assertEqual(
            artifacts["agent_result"]["metrics"]["evidence_precision"], 1.0
        )
        self.assertEqual(
            artifacts["agent_result"]["metrics"]["evidence_recall"], 1.0
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
