#!/usr/bin/env python3
"""Offline plan for sustained native detection. No executor, SSH, or API calls.

This plan is deliberately outside the frozen matrices and their executable
scenario registry. Snapshot patch planning is not Kubernetes admission or a
runtime health check; the opt-in executor performs the listed live gates.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.verify_native_alert import ALERT_NAME, _expression_tokens, expected_rule

PLAN_PATH = ROOT / "evaluation/native-cross-service-plan.yaml"
RECORDING_PATH = ROOT / "platform/online-boutique/otel-rca-rules.yaml"
ACTIVE_PHASES = (
    "activation_seconds", "telemetry_seconds", "metric_window_seconds",
    "alert_hold_seconds", "delivery_seconds", "investigation_seconds",
    "safety_margin_seconds",
)


class PlanError(ValueError):
    pass


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise PlanError(reason)


def load_plan() -> dict:
    return yaml.safe_load(PLAN_PATH.read_text(encoding="utf-8"))


def validate_plan(plan: dict) -> None:
    require(isinstance(plan, dict), "plan_must_be_an_object")
    require(set(plan) == {
        "schema_version", "plan_id", "status", "target", "fault", "trigger",
        "timing", "workload",
    }, "unknown_or_missing_plan_fields")
    require(plan["schema_version"] == "1.0.0", "unsupported_plan_version")
    require(plan["plan_id"] == "native-checkout-missing-configmap-v1", "unregistered_plan")
    require(plan["status"] == "implemented-runtime-unverified", "invalid_runtime_claim")
    # The single registered design cannot silently expand its target or taxonomy.
    require(plan["target"] == {
        "environment": "development", "namespace": "online-boutique",
        "deployment": "checkoutservice", "container": "server", "replicas": 1,
    } and type(plan["target"]["replicas"]) is int, "target_boundary_changed")
    require(plan["fault"] == {
        "cause_id": "kubernetes.missing-configmap", "strategy": "Recreate",
        "configmap_name": "checkoutservice-agent-rca-native-missing",
        "volume_name": "agent-rca-native-required-config",
        "mount_path": "/var/run/agent-rca-native-required-config", "optional": False,
    } and plan["fault"]["optional"] is False, "fault_boundary_changed")
    require(plan["trigger"] == {
        "alert_name": ALERT_NAME, "service": "frontend", "krca_profile": "checkout-full",
        "failure_rate_threshold": 0.05, "request_rate_minimum": 0.1, "hold_seconds": 120,
    }, "trigger_policy_changed")
    timing = plan["timing"]
    require(isinstance(timing, dict) and set(timing) == {
        *ACTIVE_PHASES, "baseline_seconds", "maximum_active_seconds", "recovery_timeout_seconds",
    }, "unknown_or_missing_timing_fields")
    require(all(type(v) is int and v > 0 for v in timing.values()), "invalid_timing_value")
    require(timing["baseline_seconds"] == 900, "baseline_window_changed")
    require(timing["metric_window_seconds"] == 120, "metric_window_changed")
    require(timing["alert_hold_seconds"] == plan["trigger"]["hold_seconds"], "hold_budget_mismatch")
    for key, minimum in {
        "activation_seconds": 90, "telemetry_seconds": 30, "delivery_seconds": 30,
        "investigation_seconds": 90, "safety_margin_seconds": 20,
    }.items():
        require(timing[key] >= minimum, f"insufficient_{key}")
    require(sum(timing[key] for key in ACTIVE_PHASES) <= timing["maximum_active_seconds"] == 600,
            "active_deadline_exceeded")
    require(timing["recovery_timeout_seconds"] <= 300, "recovery_timeout_exceeded")
    require(plan["workload"] == {
        "profile": "path-weighted", "seed": 342, "operations_per_second": 4,
        "timeout_seconds": 5,
    }, "workload_boundary_changed")


def check_repository_rules(plan: dict) -> dict:
    """Pin intended policy against source, not an assumed live Prometheus state."""
    rule = expected_rule("FAULT_TARGET_CLUSTER_ID")
    trigger = plan["trigger"]
    require(rule["for"] == "2m", "repository_hold_policy_changed")
    require(rule["labels"].get("service") == trigger["service"]
            and rule["labels"].get("krca_profile") == trigger["krca_profile"]
            and rule["labels"].get("rca_enabled") == "true"
            and rule["labels"].get("agent_rca_enabled") == "true", "repository_trigger_scope_changed")
    # Use token adjacency, not substring matching (e.g. 0.050 vs 0.05).
    tokens = _expression_tokens(rule["expr"])
    comparisons = [tokens[i + 1] for i, token in enumerate(tokens[:-1]) if token == ">"]
    require(comparisons == ["0.05", "0.1"], "repository_threshold_policy_changed")
    require('"POST /cart/checkout"' in tokens, "repository_route_changed")
    records = yaml.safe_load(RECORDING_PATH.read_text(encoding="utf-8"))
    expressions = {
        item["record"]: item["expr"] for group in records["spec"]["groups"]
        for item in group["rules"] if item.get("record") in {
            "agent_rca_api_failure_rate", "agent_rca_api_request_rate",
        }
    }
    require(len(expressions) == 2, "recording_rules_missing")
    require(all(set(re.findall(r"\[([^\]]+)\]", expr)) == {"2m"}
                for expr in expressions.values()), "repository_metric_window_changed")
    return {
        "source_only": True,
        "rule_sha256": hashlib.sha256(json.dumps(rule, sort_keys=True).encode()).hexdigest(),
        "recording_file_sha256": hashlib.sha256(RECORDING_PATH.read_bytes()).hexdigest(),
    }


def deployment_patch_preview(plan: dict, deployment: dict) -> dict:
    """Plan reversible changes for a supplied snapshot; never execute them.

    The restore guard intentionally rejects concurrent spec drift. A future
    executor must account for API defaulting via server-side dry run, then retain
    an owned, narrow cleanup path as well as verify the final exact snapshot.
    """
    validate_plan(plan)
    deployment = copy.deepcopy(deployment)
    target, fault = plan["target"], plan["fault"]
    metadata, spec, status = (deployment.get(key, {}) for key in ("metadata", "spec", "status"))
    require(deployment.get("apiVersion") == "apps/v1" and deployment.get("kind") == "Deployment",
            "not_a_deployment")
    require(metadata.get("name") == target["deployment"]
            and metadata.get("namespace") == target["namespace"], "deployment_scope_mismatch")
    require(bool(metadata.get("uid")) and bool(metadata.get("resourceVersion")), "identity_missing")
    require(not metadata.get("deletionTimestamp") and spec.get("paused") is not True,
            "deployment_not_active")
    require(spec.get("replicas") == 1 and status.get("readyReplicas") == 1
            and status.get("availableReplicas") == 1 and status.get("updatedReplicas") == 1
            and status.get("replicas") == 1
            and metadata.get("generation") is not None
            and status.get("observedGeneration") == metadata["generation"], "deployment_not_stable")
    require(spec.get("strategy", {}).get("type") == "RollingUpdate", "unexpected_baseline_strategy")
    template = spec.get("template", {})
    pod_spec = template.get("spec", {})
    containers = pod_spec.get("containers", [])
    selected = [item for item in containers if item.get("name") == target["container"]]
    require(len(selected) == 1, "target_container_missing_or_ambiguous")
    require(not any(item.get("name") == fault["volume_name"] for item in pod_spec.get("volumes", [])),
            "fault_volume_already_present")
    require(not any(item.get("name") == fault["volume_name"]
                    or item.get("mountPath") == fault["mount_path"]
                    for container in containers for item in container.get("volumeMounts", [])),
            "fault_mount_already_present")
    mutated = copy.deepcopy(spec)
    mutated["strategy"] = {"type": "Recreate"}
    candidate = mutated["template"]["spec"]
    candidate.setdefault("volumes", []).append({
        "name": fault["volume_name"],
        "configMap": {"name": fault["configmap_name"], "optional": False, "defaultMode": 420},
    })
    container = next(item for item in candidate["containers"] if item["name"] == target["container"])
    container.setdefault("volumeMounts", []).append({
        "name": fault["volume_name"], "mountPath": fault["mount_path"], "readOnly": True,
    })

    def changes(before: dict, after: dict) -> list[dict]:
        return [
            {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
            {"op": "test", "path": "/spec", "value": before},
            *({"op": "replace", "path": f"/spec/{key}", "value": after[key]}
              for key in ("strategy", "template")),
        ]

    return {
        "preview_only": True, "server_admission_verified": False,
        "apply_patch": [
            {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
            *changes(spec, mutated),
        ],
        "restore_patch": changes(mutated, spec),
    }


def build_plan(plan: dict) -> dict:
    validate_plan(plan)
    policy = check_repository_rules(plan)
    elapsed = 0
    phases = []
    for name in ACTIVE_PHASES:
        elapsed += plan["timing"][name]
        phases.append({"phase": name.removesuffix("_seconds"), "latest_elapsed_seconds": elapsed})
    return {
        "plan_id": plan["plan_id"], "status": "plan-only", "execution_supported": True,
        "runtime_verified": False, "matrix_member": False,
        "target": plan["target"], "fault": plan["fault"], "trigger": plan["trigger"],
        "source_policy": policy, "timing": plan["timing"], "deadline_plan": phases,
        "unallocated_deadline_seconds": plan["timing"]["maximum_active_seconds"] - elapsed,
        "workload": plan["workload"],
        "required_live_preconditions": [
            "explicit-development-only-approval-and-exact-cluster-identity",
            "all-target-workloads-ready-and-no-chaos-policy-lock-or-fault-marker",
            "single-checkout-pod-ready-restart-zero-and-exact-owner-uid",
            "required-configmap-NotFound-not-Forbidden-or-timeout",
            "unchanged-inactive-native-rule-and-no-existing-matching-Incident",
            "900-second-normal-baseline-and-checkout-full-11-edges-HAS_DATA",
            "server-side-dry-run-and-frozen-template-strategy-uid",
            "owned-remote-restoration-watchdog-armed-before-first-patch",
        ],
        "required_runtime_proof": [
            "old-serving-pod-gone-and-checkout-ready-endpoints-zero",
            "new-exact-pod-uid-required-volume-and-matching-FailedMount-NotFound-Event",
            "frontend-checkout-request-rate-above-0.1-and-failure-rate-above-0.05-for-120s",
            "natural-Prometheus-Alertmanager-Receiver-identity-no-synthetic-alert",
            "frontend-source-preserved-and-KRCA-checkpoint-selects-checkout",
            "additional-collection-before-freeze-and-exact-fault-pod-evidence-inspected-cited",
            "missing-configmap-Evidence-Gate-accepted-or-original-failure-preserved",
        ],
        "restoration_contract": [
            "single-absolute-deadline-from-before-first-mutation-never-reset-on-retry",
            "never-wait-for-Agent-beyond-deadline-before-restoring",
            "restore-original-template-and-strategy-not-just-remove-volume",
            "preserve-unrelated-changes-and-retain-watchdog-on-cleanup-failure",
            "verify-exact-restoration-ready-pod-and-no-owned-lock-marker-or-tunnel",
            "observe-natural-alert-resolution-never-submit-synthetic-resolved",
        ],
        "limitations": [
            "time-budgets-are-allowances-not-measured-latency-or-alert-guarantees",
            "configured-workload-rate-is-not-observed-route-qps",
            "zero-backend-endpoints-alone-is-not-proof-of-ConfigMap-root-cause",
            "native-executor-and-watchdog-implemented-but-not-live-verified",
            "remote-watchdog-requires-running-VM-systemd-and-reachable-Kubernetes-API",
        ],
    }


def main() -> int:
    if len(sys.argv) != 1:
        print("This command is plan-only and accepts no execution options.", file=sys.stderr)
        return 2
    try:
        result = build_plan(load_plan())
    except (PlanError, OSError, KeyError, TypeError, yaml.YAMLError) as error:
        print(f"Native cross-service planning failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
