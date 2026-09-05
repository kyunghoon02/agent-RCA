from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import yaml

from incident_platform.collectors import CollectorSpec
from incident_platform.evidence import ResourceScope
from incident_platform.incidents import AlertmanagerIngestionService
from incident_platform.krca_runtime import load_krca_runtime_config
from incident_platform.repository import InMemoryIncidentRepository
from tools.run_incident_worker import (
    ProfileAwareIncidentCollectionService,
    _selected_krca_profile,
)
from tools.test_alert_rules import ALERT, ROOT, expected_alert, load_groups, test_cases


class WorkloadAlertContractTests(unittest.TestCase):
    def test_rule_output_normalizes_to_service_and_rooted_collectors(self):
        repository = InMemoryIncidentRepository()
        labels = {**expected_alert()["exp_labels"], "alertname": ALERT}
        incident = (
            AlertmanagerIngestionService(repository)
            .ingest(
                {
                    "alerts": [
                        {
                            "labels": labels,
                            "annotations": expected_alert()["exp_annotations"],
                            "status": "firing",
                            "fingerprint": "unit-test-oom",
                            "startsAt": "2026-09-05T01:00:00Z",
                        }
                    ]
                },
                received_at=datetime(2026, 9, 5, 1, 0, 1, tzinfo=timezone.utc),
            )[0]
            .incident
        )
        self.assertEqual(incident["source_entity"]["kind"], "Service")
        self.assertEqual(incident["source_entity"]["name"], "checkoutservice")
        config = load_krca_runtime_config(ROOT / "config/online-boutique-krca.yaml")
        self.assertIsNone(_selected_krca_profile(incident, config))
        names = ("kubernetes", "prometheus-workload", "loki-kernel-oom", "hubble")
        collection = ProfileAwareIncidentCollectionService(
            repository,
            tuple(CollectorSpec(name, object()) for name in names),
            object(),
            config,
        )
        with patch("tools.run_incident_worker.IncidentCollectionService") as factory:
            collection.collect_claimed_incident(
                incident["incident_id"],
                scope=ResourceScope(
                    namespace="online-boutique",
                    resource_names=("checkoutservice",),
                    max_items=32,
                ),
            )
        specs = factory.call_args.args[1]._specs
        self.assertEqual(tuple(spec.name for spec in specs), names)
        for spec in specs:
            self.assertEqual(spec.request_scope.resource_names, ("checkoutservice",))
            self.assertEqual(
                spec.request_scope.resource_name_prefixes, ("checkoutservice-",)
            )
            self.assertEqual(spec.request_scope.namespace, "online-boutique")
            self.assertEqual(spec.request_scope.max_items, 32)

    def test_event_alert_is_separate_from_unchanged_service_impact_rules(self):
        groups = load_groups()
        impact = [rule for rule in groups[0]["rules"] if "alert" in rule]
        self.assertEqual(len(impact), 6)
        self.assertTrue(all(rule["for"] == "2m" for rule in impact))
        self.assertTrue(all(rule["labels"]["service"] == "frontend" for rule in impact))
        event = groups[1]["rules"][1]
        self.assertEqual(event["alert"], ALERT)
        self.assertEqual(event["for"], "0s")
        self.assertNotIn("krca_profile", event["labels"])
        self.assertEqual(
            set(event["labels"]), {"severity", "rca_enabled", "agent_rca_enabled"}
        )
        self.assertTrue(
            event["expr"].startswith("max by (cluster_id, namespace, service)")
        )

    def test_rule_tool_is_pinned_and_has_temporal_and_identity_negative_cases(self):
        versions = yaml.safe_load((ROOT / "platform/versions.yaml").read_text())
        self.assertRegex(
            versions["observability"]["kube_prometheus_stack"]["promtool_image"],
            r"^quay.io/prometheus/prometheus:v3\.14\.0-distroless@sha256:[0-9a-f]{64}$",
        )
        names = [test["name"] for test in test_cases()]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(names), 28)
        for name in (
            "ordinary restart is not OOM",
            "owner UID cannot cross generations",
            "old OOM reason expires at exactly five minutes",
            "missing timestamp fails closed",
            "other cluster is excluded",
        ):
            self.assertIn(name, names)

    def test_narrow_deploy_does_not_call_the_stack_or_a_fault_harness(self):
        playbook = yaml.safe_load(
            (
                ROOT / "automation/ansible/playbooks/deploy-workload-alerts.yml"
            ).read_text()
        )
        self.assertEqual(
            [play["hosts"] for play in playbook],
            ["fault_target", "observability_domain"],
        )
        include = playbook[1]["tasks"][0]["ansible.builtin.include_role"]
        self.assertEqual(include["tasks_from"], "workload-alerts.yml")
        tasks = yaml.safe_load(
            (
                ROOT
                / "automation/ansible/roles/three_domain_observability_wiring/tasks/workload-alerts.yml"
            ).read_text()
        )
        self.assertFalse(any("ansible.builtin.uri" in task for task in tasks))
        self.assertFalse(
            any(
                "stresschaos" in str(task).lower()
                or "incident_platform_stack" in str(task)
                for task in tasks
            )
        )


if __name__ == "__main__":
    unittest.main()
