from __future__ import annotations

import copy
import unittest
from pathlib import Path

import yaml

from tools.verify_native_alert import (
    ALERT_NAME,
    OOM_ALERT_NAME,
    NativeAlertError,
    _expression_tokens,
    attest,
    capture,
    expected_rule,
    preflight,
)

CLUSTER = "native-test-cluster"
ROOT = Path(__file__).resolve().parents[1]


def payload(firing: bool = True) -> dict:
    expected = expected_rule(CLUSTER)
    labels = {**expected["labels"], "alertname": ALERT_NAME, "cluster_id": CLUSTER}
    alert = {
        "labels": labels,
        "annotations": {"summary": "Checkout failure ratio exceeded threshold"},
        "fingerprint": "0123456789abcdef",
        "startsAt": "2026-09-05T01:02:01.123456789Z",
        "endsAt": "2026-09-05T01:10:00Z",
        "status": {"state": "active"},
    }
    rule = {
        "name": ALERT_NAME,
        "health": "ok",
        "lastError": "",
        "query": expected["expr"],
        "duration": 120,
        "labels": expected["labels"],
        "state": "firing" if firing else "inactive",
        "alerts": (
            [
                {
                    "labels": labels,
                    "state": "firing",
                    "activeAt": "2026-09-05T01:00:01.123456789Z",
                }
            ]
            if firing
            else []
        ),
    }
    return {
        "not_before": "2026-09-05T01:00:00Z",
        "prometheus": {"status": "success", "data": {"groups": [{"rules": [rule]}]}},
        "alertmanager": [alert],
    }


def rule(value: dict) -> dict:
    return value["prometheus"]["data"]["groups"][0]["rules"][0]


def oom_payload(firing: bool = True) -> dict:
    value = payload(firing)
    expected = expected_rule(CLUSTER, OOM_ALERT_NAME)
    labels = {
        **expected["labels"],
        "alertname": OOM_ALERT_NAME,
        "cluster_id": CLUSTER,
        "namespace": "online-boutique",
        "service": "checkoutservice",
    }
    rule(value).update(
        name=OOM_ALERT_NAME,
        query=expected["expr"],
        duration=0,
        labels=expected["labels"],
    )
    if firing:
        rule(value)["alerts"][0]["labels"] = labels
    value["alertmanager"][0]["labels"] = labels
    document = yaml.safe_load(
        (ROOT / "platform/observability/remote-workload-alerts.yaml").read_text()
    )
    dependency = document["spec"]["groups"][0]["rules"][0]
    value["prometheus"]["data"]["groups"][0]["rules"].append(
        {
            "name": dependency["record"],
            "query": dependency["expr"].replace("FAULT_TARGET_CLUSTER_ID", CLUSTER),
            "health": "ok",
        }
    )
    return value


class NativeAlertVerificationTests(unittest.TestCase):
    def test_selector_order_is_canonical_but_values_and_operators_are_not_erased(self):
        first = (
            'metric{owner_kind="ReplicaSet",owner_is_controller="true",name=~"a,b{c}"}'
        )
        reordered = (
            'metric{name=~"a,b{c}",owner_is_controller="true",owner_kind="ReplicaSet"}'
        )
        self.assertEqual(_expression_tokens(first), _expression_tokens(reordered))
        self.assertNotEqual(
            _expression_tokens(first), _expression_tokens(reordered.replace("=~", "="))
        )
        self.assertNotEqual(
            _expression_tokens(first),
            _expression_tokens(reordered.replace('"true"', '"false"')),
        )

    def test_oom_mode_checks_zero_hold_and_exact_checkout_service(self):
        self.assertEqual(
            preflight(oom_payload(False), CLUSTER, OOM_ALERT_NAME)["rule_hold_seconds"],
            0,
        )
        result = capture(oom_payload(), CLUSTER, OOM_ALERT_NAME)
        self.assertEqual(result["alert_name"], OOM_ALERT_NAME)
        self.assertEqual(result["alert_labels"]["service"], "checkoutservice")
        changed = oom_payload()
        changed["alertmanager"][0]["labels"] = {
            **changed["alertmanager"][0]["labels"],
            "service": "paymentservice",
        }
        with self.assertRaisesRegex(NativeAlertError, "alertmanager_alert_missing"):
            capture(changed, CLUSTER, OOM_ALERT_NAME)

    def test_oom_mode_rejects_missing_or_modified_ownership_rule(self):
        value = oom_payload(False)
        value["prometheus"]["data"]["groups"][0]["rules"].pop()
        with self.assertRaisesRegex(NativeAlertError, "native_ownership_rule_missing"):
            preflight(value, CLUSTER, OOM_ALERT_NAME)
        value = oom_payload(False)
        value["prometheus"]["data"]["groups"][0]["rules"][1]["query"] = "vector(1)"
        with self.assertRaisesRegex(
            NativeAlertError, "native_ownership_rule_expression_drift"
        ):
            preflight(value, CLUSTER, OOM_ALERT_NAME)

    def test_preflight_accepts_formatting_but_not_threshold_or_label_changes(self):
        value = payload(False)
        rule(value)["query"] = " ".join(rule(value)["query"].split()).replace(", ", ",")
        self.assertEqual(preflight(value, CLUSTER)["rule_hold_seconds"], 120)
        for field, replacement in (
            ("query", rule(value)["query"].replace("0.05", "0.01")),
            ("duration", 1),
            ("labels", {**rule(value)["labels"], "agent_rca_enabled": "false"}),
            ("health", "err"),
            ("state", "pending"),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(value)
                rule(changed)[field] = replacement
                with self.assertRaises(NativeAlertError):
                    preflight(changed, CLUSTER)

    def test_quoted_label_whitespace_is_not_erased(self):
        value = payload(False)
        rule(value)["query"] = rule(value)["query"].replace(
            "POST /cart/checkout", "POST/cart/checkout"
        )
        with self.assertRaisesRegex(NativeAlertError, "expression_drift"):
            preflight(value, CLUSTER)

    def test_capture_derives_stable_incident_identity_without_writing(self):
        value = payload()
        first = capture(value, CLUSTER)
        second = capture(value, CLUSTER)
        self.assertEqual(first["incident_id"], second["incident_id"])
        self.assertRegex(first["incident_id"], r"^inc-[a-f0-9]{24}$")
        self.assertEqual(first["alertmanager_fingerprint"], "0123456789abcdef")
        self.assertFalse(first["synthetic_alert_submitted"])

    def test_capture_rejects_missing_duplicate_old_or_synthetic_alerts(self):
        variants = []
        value = payload()
        value["alertmanager"] = []
        variants.append(value)
        value = payload()
        value["alertmanager"].append(copy.deepcopy(value["alertmanager"][0]))
        variants.append(value)
        value = payload()
        value["alertmanager"][0]["startsAt"] = "2026-09-04T00:00:00Z"
        variants.append(value)
        value = payload()
        rule(value)["alerts"][0]["activeAt"] = "2026-09-04T00:00:00Z"
        variants.append(value)
        value = payload()
        value["alertmanager"][0]["labels"]["verification_id"] = "controlled-run"
        variants.append(value)
        value = payload()
        value["alertmanager"][0]["status"]["state"] = "suppressed"
        variants.append(value)
        for index, value in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(NativeAlertError):
                capture(value, CLUSTER)

    def test_attestation_preserves_failed_or_wrong_rca_outcomes(self):
        captured = capture(payload(), CLUSTER)
        incident_id = captured["incident_id"]
        bundle = {
            "incident": {
                "incident_id": incident_id,
                "triggered_at": "2026-09-05T01:02:01Z",
                "created_at": "2026-09-05T01:02:05Z",
                "alert": {
                    "name": ALERT_NAME,
                    "fingerprint": captured["alertmanager_fingerprint"],
                    "labels": captured["alert_labels"],
                },
            },
            "context": {"incident_id": incident_id, "context_id": "context-1"},
            "agent_run": {
                "incident_id": incident_id,
                "context_id": "context-1",
                "status": "SUCCEEDED",
                "reason_code": "REPORT_ACCEPTED",
            },
            "report": {
                "incident_id": incident_id,
                "context_id": "context-1",
                "status": "conclusive",
                "generated_at": "2026-09-05T01:02:30Z",
                "root_cause": {"cause_id": "kubernetes.container-oomkilled"},
            },
        }
        result = attest({"capture": captured, "bundle": bundle})
        self.assertTrue(result["expected_cause_match"])
        self.assertEqual(result["ingest_to_report_seconds"], 25)
        bundle["report"]["root_cause"] = None
        self.assertFalse(
            attest({"capture": captured, "bundle": bundle})["expected_cause_match"]
        )
        bundle["agent_run"]["status"] = "GATE_REJECTED"
        bundle["report"] = None
        result = attest({"capture": captured, "bundle": bundle})
        self.assertTrue(result["native_detection_verified"])
        self.assertFalse(result["report_accepted"])
        bundle["incident"]["alert"]["fingerprint"] = "different"
        with self.assertRaisesRegex(NativeAlertError, "fingerprint_mismatch"):
            attest({"capture": captured, "bundle": bundle})

    def test_native_mode_cannot_submit_or_resolve_a_synthetic_alert(self):
        tasks = yaml.safe_load(
            (
                ROOT
                / "automation/ansible/roles/checkout_oom_fault_harness/tasks/main.yml"
            ).read_text()
        )
        block = next(task["block"] for task in tasks if "block" in task)
        post = next(
            task
            for task in block
            if task["name"] == "Submit the controlled OOM alert through Alertmanager"
        )
        self.assertEqual(
            post["when"],
            "checkout_fault_trigger_mode | default('synthetic') == 'synthetic'",
        )
        setter = next(
            task
            for task in block
            if task["name"] == "Mark the controlled Alertmanager alert as submitted"
        )
        self.assertEqual(setter["when"], post["when"])
        cleanup = next(task["always"] for task in tasks if "always" in task)
        resolve = next(
            task
            for task in cleanup
            if task["name"] == "Resolve the controlled Alertmanager alert"
        )
        self.assertIn("checkout_fault_alert_submitted | bool", str(resolve["when"]))
        self.assertIn("'synthetic'", str(resolve["when"]))

    def test_native_cleanup_also_covers_an_alert_that_never_fired(self):
        task_root = ROOT / "automation/ansible/roles/checkout_oom_fault_harness/tasks"
        tasks = yaml.safe_load((task_root / "main.yml").read_text())
        cleanup = next(task["always"] for task in tasks if "always" in task)
        recovery = next(
            task
            for task in cleanup
            if task.get("ansible.builtin.include_tasks") == "native_alert_recovery.yml"
        )
        self.assertIn("checkout_native_preflight is defined", recovery["when"])
        self.assertNotIn("checkout_fault_native_capture is defined", recovery["when"])
        recovery_tasks = yaml.safe_load(
            (task_root / "native_alert_recovery.yml").read_text()
        )[0]
        resolved = next(
            task
            for task in recovery_tasks["block"]
            if task["name"]
            == "Wait for the native resolved webhook to close the Incident alert window"
        )
        self.assertEqual(resolved["when"], "checkout_fault_native_capture is defined")
        self.assertTrue(
            any("ansible.builtin.file" in task for task in recovery_tasks["always"])
        )


if __name__ == "__main__":
    unittest.main()
