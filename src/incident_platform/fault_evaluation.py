"""Offline evaluation assembly for controlled-fault Incident snapshots."""

from __future__ import annotations

import copy
import hashlib
import re
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Sequence, Tuple

from .contracts import validate_contract
from .deterministic import (
    OOM_EVIDENCE_GATE_POLICY,
    OOM_MEMORY_RATIO_REFERENCE_THRESHOLD,
    DeterministicRCAEngine,
)
from .evidence import verify_evidence_content_hash
from .errors import ContractViolation
from .rca_evaluation import (
    evaluate_rca_case,
    prediction_from_agent_report,
    prediction_from_failed_agent_run,
    prediction_from_deterministic_decision,
)


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ContractViolation("evaluation timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _facts(item: Mapping[str, Any]) -> Mapping[str, Any]:
    value = item.get("facts", {})
    return value if isinstance(value, Mapping) else {}


def _same_pod_uid(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    first_subject = first.get("subject", {})
    second_subject = second.get("subject", {})
    if not isinstance(first_subject, Mapping) or not isinstance(
        second_subject, Mapping
    ):
        return False
    return bool(first_subject.get("uid")) and (
        first_subject.get("cluster_id"),
        first_subject.get("namespace"),
        first_subject.get("kind"),
        first_subject.get("name"),
        first_subject.get("uid"),
    ) == (
        second_subject.get("cluster_id"),
        second_subject.get("namespace"),
        second_subject.get("kind"),
        second_subject.get("name"),
        second_subject.get("uid"),
    )


def _at_least(value: object, threshold: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and value >= threshold
    )


def select_controlled_oom_ground_truth_evidence(
    evidence: Sequence[Mapping[str, Any]],
    scenario: Mapping[str, Any],
) -> Tuple[str, str, str]:
    """Select independently observed OOM labels for the injected fault.

    The selection is intentionally stricter than simply copying the Evidence
    Gate citations: it evaluates the preregistered predicates against the
    frozen Evidence snapshot and requires one exact Pod UID across all items.
    """

    selected = _select_controlled_oom_ground_truth_items(evidence, scenario)
    return tuple(str(item["evidence_id"]) for item in selected)


def _select_controlled_oom_ground_truth_items(
    evidence: Sequence[Mapping[str, Any]],
    scenario: Mapping[str, Any],
) -> Tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    predicates = scenario["expected"]["evidence_predicates"]
    restart_minimum = float(predicates["restart_count_delta_minimum"])
    signatures = [
        item
        for item in evidence
        if (
            item.get("source") == "kubernetes"
            and item.get("kind") == "resource-state"
            and _facts(item).get("last_termination_reason") == "OOMKilled"
            and item.get("subject", {}).get("kind") == "Pod"
            and bool(item.get("subject", {}).get("uid"))
        )
        or (
            item.get("source") == "loki"
            and item.get("kind") == "log-pattern"
            and item.get("provenance", {}).get("provider")
            == "loki-kernel-oom-provider"
            and _facts(item).get("pattern_id") == "kernel-cgroup-oom"
            and _facts(item).get("kernel_constraint") == "CONSTRAINT_MEMCG"
            and _at_least(_facts(item).get("match_count"), 1)
            and item.get("subject", {}).get("kind") == "Pod"
            and bool(item.get("subject", {}).get("uid"))
            and _facts(item).get("pod_uid") == item.get("subject", {}).get("uid")
        )
    ]
    for signature in signatures:
        restart = next(
            (
                item
                for item in evidence
                if item.get("source") == "prometheus"
                and item.get("kind") == "metric-summary"
                and _facts(item).get("metric") == "restart_count_delta"
                and _at_least(_facts(item).get("peak_delta"), restart_minimum)
                and _same_pod_uid(signature, item)
            ),
            None,
        )
        memory = next(
            (
                item
                for item in evidence
                if item.get("source") == "prometheus"
                and item.get("kind") == "metric-summary"
                and _facts(item).get("metric") == "memory_working_set_ratio"
                and _at_least(_facts(item).get("peak_ratio"), 0)
                and _same_pod_uid(signature, item)
            ),
            None,
        )
        if restart is not None and memory is not None:
            return signature, restart, memory
    raise ContractViolation(
        "controlled OOM Ground Truth requires an exact OOM signature, restart "
        "delta, and memory ratio for one Pod UID"
    )


def _build_observation(
    *,
    evaluation_case_id: str,
    scenario_id: str,
    incident_id: str,
    selected_items: Tuple[
        Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]
    ],
    prediction: Mapping[str, Any],
    result: Mapping[str, Any],
    observed_at: str,
) -> Dict[str, Any]:
    signature, restart, memory = selected_items
    signature_facts = _facts(signature)
    if signature.get("source") == "kubernetes":
        signature_source = "kubernetes-oomkilled"
        signature_match_count = 1
    else:
        signature_source = "loki-kernel-memcg"
        signature_match_count = int(signature_facts["match_count"])

    memory_peak = float(_facts(memory)["peak_ratio"])
    observation = {
        "schema_version": "1.0.0",
        "evaluation_case_id": evaluation_case_id,
        "scenario_id": scenario_id,
        "incident_id": incident_id,
        "observed_at": observed_at,
        "evidence_gate_policy": OOM_EVIDENCE_GATE_POLICY,
        "same_pod_uid": True,
        "oom_signature_source": signature_source,
        "oom_signature_match_count": signature_match_count,
        "restart_count_delta_peak": float(_facts(restart)["peak_delta"]),
        "memory_working_set_ratio_peak": memory_peak,
        "memory_working_set_ratio_reference_threshold": (
            OOM_MEMORY_RATIO_REFERENCE_THRESHOLD
        ),
        "memory_reference_threshold_met": (
            memory_peak >= OOM_MEMORY_RATIO_REFERENCE_THRESHOLD
        ),
        "prediction_outcome": prediction["outcome"],
        "root_cause_top1_accuracy": result["metrics"][
            "root_cause_top1_accuracy"
        ],
        "evidence_recall": result["metrics"]["evidence_recall"],
    }
    validate_contract("rca-evaluation-observation.schema.json", observation)
    return observation


def summarize_controlled_fault_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    generated_at: datetime,
) -> Dict[str, Any]:
    """Build an ID-free distribution summary for one controlled scenario."""

    if not observations:
        raise ContractViolation("at least one controlled-fault observation is required")
    copied = [copy.deepcopy(dict(item)) for item in observations]
    for observation in copied:
        validate_contract("rca-evaluation-observation.schema.json", observation)
        reference_threshold_met = (
            observation["memory_working_set_ratio_peak"]
            >= observation["memory_working_set_ratio_reference_threshold"]
        )
        if observation["memory_reference_threshold_met"] != reference_threshold_met:
            raise ContractViolation(
                "controlled-fault observation reference threshold is inconsistent"
            )
    scenario_ids = {item["scenario_id"] for item in copied}
    if len(scenario_ids) != 1:
        raise ContractViolation(
            "controlled-fault observations must belong to one scenario"
        )
    gate_policies = {item["evidence_gate_policy"] for item in copied}
    if len(gate_policies) != 1:
        raise ContractViolation(
            "controlled-fault observations use different Evidence Gate policies"
        )
    for identity_field in ("evaluation_case_id", "incident_id"):
        identities = [item[identity_field] for item in copied]
        if len(set(identities)) != len(identities):
            raise ContractViolation(
                f"controlled-fault observations repeat {identity_field}"
            )

    memory_peaks = [item["memory_working_set_ratio_peak"] for item in copied]
    restart_peaks = [item["restart_count_delta_peak"] for item in copied]
    thresholds = {
        item["memory_working_set_ratio_reference_threshold"] for item in copied
    }
    if len(thresholds) != 1:
        raise ContractViolation(
            "controlled-fault observations use different memory thresholds"
        )
    threshold = thresholds.pop()

    def _count(field: str, value: str) -> int:
        return sum(item[field] == value for item in copied)

    def _mean_score(field: str) -> float:
        return round(statistics.fmean(item[field] for item in copied), 6)

    met_count = sum(item["memory_reference_threshold_met"] for item in copied)
    summary = {
        "schema_version": "1.0.0",
        "scenario_id": next(iter(scenario_ids)),
        "evidence_gate_policy": next(iter(gate_policies)),
        "generated_at": _format_time(generated_at),
        "run_count": len(copied),
        "prediction_outcomes": {
            "root_cause": _count("prediction_outcome", "ROOT_CAUSE"),
            "abstain": _count("prediction_outcome", "ABSTAIN"),
            "ambiguous": _count("prediction_outcome", "AMBIGUOUS"),
        },
        "oom_signature_sources": {
            "kubernetes_oomkilled": _count(
                "oom_signature_source", "kubernetes-oomkilled"
            ),
            "loki_kernel_memcg": _count(
                "oom_signature_source", "loki-kernel-memcg"
            ),
        },
        "memory_working_set_ratio_peak": {
            "minimum": min(memory_peaks),
            "median": statistics.median(memory_peaks),
            "maximum": max(memory_peaks),
            "reference_threshold": threshold,
            "reference_threshold_met_count": met_count,
            "reference_threshold_met_rate": round(met_count / len(copied), 6),
        },
        "restart_count_delta_peak": {
            "minimum": min(restart_peaks),
            "median": statistics.median(restart_peaks),
            "maximum": max(restart_peaks),
        },
        "mean_metrics": {
            "root_cause_top1_accuracy": _mean_score(
                "root_cause_top1_accuracy"
            ),
            "evidence_recall": _mean_score("evidence_recall"),
        },
    }
    validate_contract(
        "rca-evaluation-observation-summary.schema.json", summary
    )
    return summary


def build_controlled_fault_evaluation(
    bundle: Mapping[str, Any],
    scenario: Mapping[str, Any],
    *,
    scenario_sha256: str,
    evaluated_at: datetime,
) -> Dict[str, Dict[str, Any]]:
    """Build Ground Truth plus separate baseline and Agent score artifacts."""

    validate_contract("controlled-fault-scenario.schema.json", scenario)
    if not isinstance(scenario_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", scenario_sha256
    ) is None:
        raise ContractViolation("scenario_sha256 must be a lowercase SHA-256 digest")

    incident = copy.deepcopy(dict(bundle.get("incident", {})))
    context = copy.deepcopy(dict(bundle.get("context", {})))
    raw_evidence = bundle.get("evidence", [])
    if not isinstance(raw_evidence, list):
        raise ContractViolation("fault evaluation Evidence must be an array")
    evidence = [copy.deepcopy(dict(item)) for item in raw_evidence]
    validate_contract("incident.schema.json", incident)
    validate_contract("context-package.schema.json", context)
    for item in evidence:
        validate_contract("evidence-item.schema.json", item)
        if not verify_evidence_content_hash(item):
            raise ContractViolation(
                "fault evaluation Evidence content hash verification failed"
            )

    incident_id = incident["incident_id"]
    alert = incident["alert"]
    alert_labels = alert["labels"]
    if (
        alert["name"] != scenario["alert"]["name"]
        or alert_labels.get("service") != scenario["alert"]["service"]
        or alert_labels.get("rca_enabled")
        != scenario["alert"]["rca_enabled"]
    ):
        raise ContractViolation(
            "fault evaluation Incident does not match the controlled scenario"
        )
    if context["incident_id"] != incident_id or any(
        item["incident_id"] != incident_id for item in evidence
    ):
        raise ContractViolation(
            "fault evaluation Incident, Context, and Evidence identities disagree"
        )
    if incident["status"] not in {"ANALYZING", "REPORTED", "PARTIAL", "FAILED"}:
        raise ContractViolation(
            "fault evaluation requires a completed frozen Context snapshot"
        )
    by_id = {item["evidence_id"]: item for item in evidence}
    if len(by_id) != len(evidence):
        raise ContractViolation("fault evaluation Evidence IDs must be unique")
    missing = [
        evidence_id
        for evidence_id in context["evidence_ids"]
        if evidence_id not in by_id
    ]
    if missing:
        raise ContractViolation(
            "fault evaluation bundle omits Evidence referenced by its Context"
        )
    frozen_evidence = [by_id[item] for item in context["evidence_ids"]]

    selected_items = _select_controlled_oom_ground_truth_items(
        frozen_evidence, scenario
    )
    relevant_ids = tuple(str(item["evidence_id"]) for item in selected_items)
    evaluation_digest = hashlib.sha256(
        f"{scenario['scenario_id']}:{incident_id}".encode("utf-8")
    ).hexdigest()
    evaluation_case_id = f"eval-{evaluation_digest[:24]}"
    timestamp = _format_time(evaluated_at)
    ground_truth = {
        "schema_version": "1.0.0",
        "evaluation_case_id": evaluation_case_id,
        "scenario_id": scenario["scenario_id"],
        "incident_id": incident_id,
        "expected_outcome": scenario["expected"]["outcome"],
        "expected_root_cause_ids": list(
            scenario["expected"]["root_cause_ids"]
        ),
        "relevant_evidence_ids": list(relevant_ids),
        "labeled_at": timestamp,
        "labeler": "controlled-fault-manifest",
        "provenance": {
            "controlled_fault": True,
            "fault_manifest_sha256": f"sha256:{scenario_sha256}",
            "workload_profile": scenario["workload"]["profile"],
            "workload_seed": scenario["workload"]["seed"],
            "change_applied": True,
        },
    }
    validate_contract("rca-evaluation-ground-truth.schema.json", ground_truth)

    decision = DeterministicRCAEngine().evaluate(frozen_evidence)
    prediction = prediction_from_deterministic_decision(
        evaluation_case_id=evaluation_case_id,
        scenario_id=scenario["scenario_id"],
        incident_id=incident_id,
        decision=decision,
        available_evidence_ids=context["evidence_ids"],
        completed_at=evaluated_at,
    )
    result = evaluate_rca_case(
        ground_truth,
        prediction,
        evaluated_at=evaluated_at,
    )
    observation = _build_observation(
        evaluation_case_id=evaluation_case_id,
        scenario_id=scenario["scenario_id"],
        incident_id=incident_id,
        selected_items=selected_items,
        prediction=prediction,
        result=result,
        observed_at=timestamp,
    )
    artifacts = {
        "ground_truth": ground_truth,
        "prediction": prediction,
        "result": result,
        "observation": observation,
    }
    raw_report = bundle.get("report")
    raw_agent_run = bundle.get("agent_run")
    agent_run = None
    if raw_agent_run is not None:
        if not isinstance(raw_agent_run, Mapping):
            raise ContractViolation("fault evaluation Agent run must be an object")
        agent_run = copy.deepcopy(dict(raw_agent_run))
        validate_contract("agent-run-audit.schema.json", agent_run)
        if agent_run["incident_id"] != incident_id:
            raise ContractViolation(
                "fault evaluation Agent run belongs to another Incident"
            )
    if raw_report is not None:
        if not isinstance(raw_report, Mapping):
            raise ContractViolation("fault evaluation Agent Report must be an object")
        report = copy.deepcopy(dict(raw_report))
        if agent_run is not None and (
            agent_run["status"] != "SUCCEEDED"
            or agent_run["context_id"] != report["context_id"]
        ):
            raise ContractViolation(
                "accepted Agent Report does not match the completed Agent run"
            )
        agent_prediction = prediction_from_agent_report(
            evaluation_case_id=evaluation_case_id,
            scenario_id=scenario["scenario_id"],
            incident_id=incident_id,
            report=report,
            available_evidence_ids=context["evidence_ids"],
            completed_at=evaluated_at,
            variant_id="C",
        )
        agent_result = evaluate_rca_case(
            ground_truth,
            agent_prediction,
            evaluated_at=evaluated_at,
        )
        artifacts["agent_prediction"] = agent_prediction
        artifacts["agent_result"] = agent_result
    elif agent_run is not None:
        agent_prediction = prediction_from_failed_agent_run(
            evaluation_case_id=evaluation_case_id,
            scenario_id=scenario["scenario_id"],
            incident_id=incident_id,
            agent_run=agent_run,
            available_evidence_ids=context["evidence_ids"],
            completed_at=evaluated_at,
            variant_id="C",
        )
        artifacts["agent_prediction"] = agent_prediction
        artifacts["agent_result"] = evaluate_rca_case(
            ground_truth,
            agent_prediction,
            evaluated_at=evaluated_at,
        )
    return artifacts
