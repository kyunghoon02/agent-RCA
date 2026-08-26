"""Offline evaluation assembly for controlled-fault Incident snapshots."""

from __future__ import annotations

import copy
import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Sequence, Tuple

from .contracts import validate_contract
from .deterministic import DeterministicRCAEngine
from .evidence import verify_evidence_content_hash
from .errors import ContractViolation
from .rca_evaluation import (
    evaluate_rca_case,
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
            return (
                str(signature["evidence_id"]),
                str(restart["evidence_id"]),
                str(memory["evidence_id"]),
            )
    raise ContractViolation(
        "controlled OOM Ground Truth requires an exact OOM signature, restart "
        "delta, and memory ratio for one Pod UID"
    )


def build_controlled_fault_evaluation(
    bundle: Mapping[str, Any],
    scenario: Mapping[str, Any],
    *,
    scenario_sha256: str,
    evaluated_at: datetime,
) -> Dict[str, Dict[str, Any]]:
    """Build private Ground Truth, a frozen Prediction, and sanitized Result."""

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
    if incident["status"] not in {"ANALYZING", "REPORTED", "PARTIAL"}:
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

    relevant_ids = select_controlled_oom_ground_truth_evidence(
        frozen_evidence, scenario
    )
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
    return {
        "ground_truth": ground_truth,
        "prediction": prediction,
        "result": result,
    }
