from __future__ import annotations

import copy
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from tools import plan_native_cross_service as planner
from tools.run_evaluation_matrix import MatrixError
from tools.validate_evaluation_scenario import validate_registered_scenario


def deployment() -> dict:
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {
            "name": "checkoutservice", "namespace": "online-boutique",
            "uid": "snapshot-uid", "resourceVersion": "12", "generation": 4,
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "checkoutservice"}},
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": 0}},
            "template": {
                "metadata": {"labels": {"app": "checkoutservice"}, "annotations": {"user-owned": "keep"}},
                "spec": {
                    "containers": [{
                        "name": "server", "image": "example.invalid/checkout@sha256:snapshot",
                        "resources": {"limits": {"cpu": "200m", "memory": "128Mi"}},
                        "env": [{"name": "EXAMPLE", "value": "keep"}],
                    }],
                },
            },
        },
        "status": {"replicas": 1, "readyReplicas": 1, "availableReplicas": 1,
                   "updatedReplicas": 1, "observedGeneration": 4},
    }


def apply_preview(document: dict, operations: list[dict]) -> dict:
    """Small test-only JSON Patch interpreter; not Kubernetes admission."""
    result = copy.deepcopy(document)
    for operation in operations:
        keys = operation["path"].strip("/").split("/")
        parent = result
        for key in keys[:-1]:
            parent = parent[key]
        if operation["op"] == "test":
            if parent[keys[-1]] != operation["value"]:
                raise ValueError("guard_failed")
        elif operation["op"] == "replace":
            if keys[-1] not in parent:
                raise ValueError("replace_target_missing")
            parent[keys[-1]] = copy.deepcopy(operation["value"])
        else:
            raise AssertionError("Unexpected operation in offline preview")
    return result


class NativeCrossServicePlanTests(unittest.TestCase):
    def test_registered_plan_is_offline_not_runtime_or_matrix_evidence(self):
        result = planner.build_plan(planner.load_plan())
        self.assertTrue(result["execution_supported"])
        self.assertFalse(result["runtime_verified"])
        self.assertFalse(result["matrix_member"])
        self.assertTrue(result["source_policy"]["source_only"])
        self.assertEqual(result["deadline_plan"][-1]["latest_elapsed_seconds"], 500)
        self.assertEqual(result["unallocated_deadline_seconds"], 100)
        self.assertEqual(result["trigger"]["failure_rate_threshold"], 0.05)
        self.assertEqual(result["trigger"]["hold_seconds"], 120)

    def test_plan_not_registered_as_executable_matrix_scenario(self):
        with self.assertRaises(MatrixError):
            validate_registered_scenario("evaluation/native-cross-service-plan.yaml", "kubernetes.missing-configmap")

    def test_refuse_execution_and_other_cli_options(self):
        for argument in ("--execute", "--scenario", "--force"):
            with self.subTest(argument=argument), patch.object(planner.sys, "argv", ["planner", argument]):
                with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(planner.main(), 2)
                    self.assertEqual(output.getvalue(), "")

    def test_cli_returns_only_an_offline_plan(self):
        with patch.object(planner.sys, "argv", ["planner"]), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(planner.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "plan-only")

    def test_reject_scope_and_fault_drift(self):
        for section, key, value in (
            ("target", "environment", "production"),
            ("target", "namespace", "kube-system"),
            ("target", "deployment", "frontend"),
            ("target", "replicas", 2),
            ("target", "replicas", True),
            ("fault", "optional", True),
            ("fault", "optional", 0),
            ("fault", "strategy", "RollingUpdate"),
            ("fault", "configmap_name", "real-production-config"),
            ("fault", "cause_id", "network.drop"),
            ("trigger", "hold_seconds", 30),
            ("trigger", "failure_rate_threshold", 0.01),
            ("trigger", "request_rate_minimum", 0),
            ("trigger", "service", "checkoutservice"),
            ("workload", "operations_per_second", 50),
        ):
            with self.subTest(section=section, key=key, value=value):
                plan = planner.load_plan()
                plan[section][key] = value
                with self.assertRaises(planner.PlanError):
                    planner.validate_plan(plan)

    def test_reject_unknown_fields_and_execution_status(self):
        for change in ({"execute": True}, {"status": "ready"}, {"schema_version": "2.0.0"}):
            with self.subTest(change=change):
                with self.assertRaises(planner.PlanError):
                    planner.validate_plan({**planner.load_plan(), **change})

    def test_deadline_budget_cannot_be_shortened_or_extended(self):
        for key, value in (
            ("maximum_active_seconds", 300), ("maximum_active_seconds", 601),
            ("activation_seconds", 60), ("delivery_seconds", 0),
            ("metric_window_seconds", 60), ("alert_hold_seconds", 30),
            ("investigation_seconds", 200), ("recovery_timeout_seconds", 301),
            ("baseline_seconds", 120), ("telemetry_seconds", True),
            ("safety_margin_seconds", float("nan")), ("unknown", 10),
        ):
            with self.subTest(key=key, value=value):
                plan = planner.load_plan()
                plan["timing"][key] = value
                with self.assertRaises(planner.PlanError):
                    planner.validate_plan(plan)

    def test_repository_rule_drift_is_rejected(self):
        for key, value in (
            ("for", "30s"),
            ("expr", "metric > 0.01 and traffic > 0.1"),
            ("labels", {"service": "checkoutservice"}),
        ):
            with self.subTest(key=key):
                rule = planner.expected_rule("FAULT_TARGET_CLUSTER_ID")
                rule[key] = value
                with patch.object(planner, "expected_rule", return_value=rule):
                    with self.assertRaises(planner.PlanError):
                        planner.build_plan(planner.load_plan())

    def test_recording_window_drift_is_rejected(self):
        changed = planner.RECORDING_PATH.read_text().replace("[2m]", "[5m]")
        with patch.object(planner, "RECORDING_PATH") as recording:
            recording.read_text.return_value = changed
            with self.assertRaisesRegex(planner.PlanError, "metric_window_changed"):
                planner.build_plan(planner.load_plan())

    def test_change_and_restore_are_exact_and_do_not_touch_other_spec(self):
        before = deployment()
        original = copy.deepcopy(before)
        preview = planner.deployment_patch_preview(planner.load_plan(), before)
        changed = apply_preview(before, preview["apply_patch"])
        self.assertTrue(preview["preview_only"])
        self.assertFalse(preview["server_admission_verified"])
        self.assertEqual(changed["spec"]["strategy"], {"type": "Recreate"})
        pod = changed["spec"]["template"]["spec"]
        self.assertFalse(pod["volumes"][-1]["configMap"]["optional"])
        self.assertEqual(changed["spec"]["replicas"], 1)
        self.assertEqual(changed["spec"]["selector"], before["spec"]["selector"])
        self.assertEqual(pod["containers"][0]["resources"], before["spec"]["template"]["spec"]["containers"][0]["resources"])
        # ResourceVersion changes after an API write; restoration uses UID/spec guards.
        changed["metadata"]["resourceVersion"] = "13"
        restored = apply_preview(changed, preview["restore_patch"])
        self.assertEqual(restored["spec"], before["spec"])
        self.assertEqual(before, original)
        self.assertNotIn("volumes", restored["spec"]["template"]["spec"])
        self.assertNotIn("volumeMounts", restored["spec"]["template"]["spec"]["containers"][0])

    def test_preexisting_volumes_sidecars_and_strategy_are_preserved(self):
        before = deployment()
        pod = before["spec"]["template"]["spec"]
        pod["volumes"] = [{"name": "user-owned", "emptyDir": {}}]
        pod["containers"][0]["volumeMounts"] = [{"name": "user-owned", "mountPath": "/data"}]
        pod["containers"].append({"name": "sidecar", "image": "keep"})
        preview = planner.deployment_patch_preview(planner.load_plan(), before)
        changed = apply_preview(before, preview["apply_patch"])
        restored = apply_preview(changed, preview["restore_patch"])
        self.assertEqual(restored, before)

    def test_snapshot_plan_is_detached_from_mutable_input(self):
        before = deployment()
        expected = copy.deepcopy(before)
        preview = planner.deployment_patch_preview(planner.load_plan(), before)
        before["spec"]["replicas"] = 9
        changed = apply_preview(expected, preview["apply_patch"])
        self.assertEqual(apply_preview(changed, preview["restore_patch"]), expected)

    def test_preview_rejects_unhealthy_or_wrong_target(self):
        for section, key, value in (
            ("metadata", "namespace", "other"), ("metadata", "uid", ""),
            ("metadata", "deletionTimestamp", "2026-09-08T00:00:00Z"),
            ("metadata", "resourceVersion", ""), ("spec", "replicas", 2),
            ("spec", "paused", True), ("spec", "strategy", {"type": "Recreate"}),
            ("status", "readyReplicas", 0), ("status", "observedGeneration", 3),
            ("status", "replicas", 2), ("status", "updatedReplicas", 0),
        ):
            with self.subTest(section=section, key=key):
                before = deployment()
                before[section][key] = value
                with self.assertRaises(planner.PlanError):
                    planner.deployment_patch_preview(planner.load_plan(), before)

    def test_preview_rejects_existing_fault_references(self):
        plan = planner.load_plan()
        for field in ("volume", "mount", "mount_path"):
            with self.subTest(field=field):
                before = deployment()
                pod = before["spec"]["template"]["spec"]
                if field == "volume":
                    pod["volumes"] = [{"name": plan["fault"]["volume_name"]}]
                else:
                    pod["containers"][0]["volumeMounts"] = [{
                        "name": plan["fault"]["volume_name"] if field == "mount" else "other",
                        "mountPath": plan["fault"]["mount_path"],
                    }]
                with self.assertRaises(planner.PlanError):
                    planner.deployment_patch_preview(plan, before)

    def test_patch_guards_reject_stale_resource_version_or_spec(self):
        before = deployment()
        preview = planner.deployment_patch_preview(planner.load_plan(), before)
        for section, key, value in (
            ("metadata", "uid", "replacement"), ("metadata", "resourceVersion", "99"),
            ("spec", "replicas", 2),
        ):
            with self.subTest(key=key):
                stale = copy.deepcopy(before)
                stale[section][key] = value
                with self.assertRaisesRegex(ValueError, "guard_failed"):
                    apply_preview(stale, preview["apply_patch"])

    def test_restore_refuses_to_overwrite_concurrent_user_change(self):
        before = deployment()
        preview = planner.deployment_patch_preview(planner.load_plan(), before)
        for section, key, value in (("metadata", "uid", "replacement"), ("spec", "replicas", 2)):
            with self.subTest(key=key):
                changed = apply_preview(before, preview["apply_patch"])
                changed[section][key] = value
                with self.assertRaisesRegex(ValueError, "guard_failed"):
                    apply_preview(changed, preview["restore_patch"])


if __name__ == "__main__":
    unittest.main()
