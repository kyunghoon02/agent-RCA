from __future__ import annotations

import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.verify_hubble_evidence import (
    CLUSTER, LOCK, NAMESPACE, alert_payload, cleanup_code, http_probe, metrics_ready, policy, validate_audit,
)


RUN_ID = "hubble-evidence-0123456789ab"


class NetworkEvidenceVerificationTests(unittest.TestCase):
    def test_probe_generates_valid_python_without_replacing_urlerror(self):
        def run(argv, **kwargs):
            compile(argv[2], "http-probe", "exec")
            self.assertIn("urllib.error.URLError", argv[2])
            return '{"http_status":200,"elapsed_ms":1}'

        result = http_probe(SimpleNamespace(run=run), "http://127.0.0.1", "/product/example")
        self.assertEqual(result["http_status"], 200)

    def test_policy_is_exactly_one_path_without_default_deny(self):
        resource = policy(RUN_ID)
        self.assertEqual(resource["metadata"]["namespace"], "online-boutique")
        self.assertEqual(resource["kind"], "CiliumNetworkPolicy")
        self.assertEqual(resource["spec"], {
            "endpointSelector": {"matchLabels": {"app": "frontend"}},
            "enableDefaultDeny": {"ingress": False, "egress": False},
            "egressDeny": [{
                "toEndpoints": [{"matchLabels": {"app": "productcatalogservice"}}],
                "toPorts": [{"ports": [{"port": "3550", "protocol": "TCP"}]}],
            }],
        })

    def test_alert_does_not_opt_into_paid_agent_or_blind_diagnosis(self):
        payload = alert_payload(RUN_ID, "start", "end")[0]
        self.assertEqual(payload["labels"]["agent_rca_enabled"], "false")
        self.assertEqual(payload["labels"]["rca_enabled"], "true")
        self.assertNotIn("krca_profile", payload["labels"])
        self.assertNotIn("root_cause", payload)

    def test_invalid_ids_cannot_become_cleanup_commands(self):
        for value in ("", "../other", "hubble-evidence-;false", "hubble-evidence-" + "f" * 13):
            with self.assertRaises(ValueError):
                policy(value)
            with self.assertRaises(ValueError):
                cleanup_code(value)

    def test_cleanup_only_deletes_two_owned_exact_objects(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            document = {"metadata": {"labels": {"agent-rca.dev/verification-id": RUN_ID}}}
            return SimpleNamespace(stdout=json.dumps(document))

        with patch("subprocess.run", side_effect=run):
            exec(compile(cleanup_code(RUN_ID), "watchdog", "exec"), {})
        deletions = [args for args in calls if "delete" in args]
        self.assertEqual(len(deletions), 2)
        self.assertIn(RUN_ID, deletions[0])
        self.assertIn(LOCK, deletions[1])
        self.assertTrue(all("--all" not in args for args in calls))
        self.assertTrue(all(NAMESPACE in args for args in calls))

    def test_cleanup_does_not_delete_someone_elses_object(self):
        with patch("subprocess.run", return_value=SimpleNamespace(stdout=json.dumps({
            "metadata": {"labels": {"agent-rca.dev/verification-id": "someone-else"}},
        }))) as run:
            with self.assertRaisesRegex(RuntimeError, "ownership changed"):
                exec(compile(cleanup_code(RUN_ID), "watchdog", "exec"), {})
        self.assertEqual(run.call_count, 1)

    def test_cleanup_is_idempotent_when_objects_are_absent(self):
        with patch("subprocess.run", return_value=SimpleNamespace(stdout="")) as run:
            exec(compile(cleanup_code(RUN_ID), "watchdog", "exec"), {})
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all("get" in call.args[0] for call in run.call_args_list))

    def sample(self):
        facts = {"flow_signal": "POLICY_DENIAL_OBSERVED", "policy_denied_count": 3,
                 "retention_status": "UNKNOWN"}
        return {
            "status": "READY", "incident_status": "ANALYZING", "agent_run_count": 0,
            "hubble": [{"evidence_id": "ev-1", "facts": facts,
                        "subject": {"cluster_id": CLUSTER, "namespace": NAMESPACE, "name": "frontend"},
                        "quality": {"completeness": 0.5},
                        "in_frozen_context": True, "in_agent_catalog": True,
                        "tool_status": "SUCCEEDED", "tool_facts_equal": True}],
            "graph_events": [{"record": {"evidence_ids": ["ev-1"], "attributes": copy.deepcopy(facts)}}],
        }

    def test_evidence_must_reach_graph_context_catalog_and_tool(self):
        validate_audit(self.sample())
        for field in ("in_frozen_context", "in_agent_catalog", "tool_facts_equal"):
            item = self.sample()
            item["hubble"][0][field] = False
            with self.assertRaises(ValueError):
                validate_audit(item)
        item = self.sample()
        item["graph_events"][0]["record"]["attributes"]["policy_denied_count"] = 0
        with self.assertRaises(ValueError):
            validate_audit(item)

    def test_no_data_wrong_scope_or_overstated_completeness_cannot_pass(self):
        for change in ("no_data", "wrong_scope", "coverage", "llm"):
            item = self.sample()
            if change == "no_data":
                item["hubble"][0]["facts"]["flow_signal"] = "NO_FLOW_DATA"
            elif change == "wrong_scope":
                item["hubble"][0]["subject"]["cluster_id"] = "other"
            elif change == "coverage":
                item["hubble"][0]["quality"]["completeness"] = 1
            else:
                item["agent_run_count"] = 1
            with self.assertRaises(ValueError):
                validate_audit(item)

    def test_context_created_before_transition_is_not_a_completed_pipeline(self):
        item = self.sample()
        item["incident_status"] = "LOCALIZING"
        with self.assertRaises(ValueError):
            validate_audit(item)

    def test_observation_gaps_must_remain_visible(self):
        item = self.sample()
        item["hubble"][0]["facts"]["observation_gaps"] = ["CLI_RELAY_VERSION_MISMATCH"]
        with self.assertRaisesRegex(ValueError, "PARTIAL"):
            validate_audit(item, require_graph=False)
        item["hubble_collector_statuses"] = [{"status": "PARTIAL"}]
        with self.assertRaisesRegex(ValueError, "hidden"):
            validate_audit(item, require_graph=False)
        item["collector_failures"] = [{"collector": "hubble"}]
        validate_audit(item, require_graph=False)

    def test_empty_metric_series_is_not_zero_or_healthy_baseline(self):
        snapshot = {
            "scrape_up": {"status": "success", "data": {"result": [{"value": [0, "1"]}]}},
            "frontend_calls": {"status": "success", "data": {"result": []}},
        }
        self.assertFalse(metrics_ready(snapshot))
        snapshot["frontend_calls"]["data"]["result"] = [{"value": [0, "0"]}]
        self.assertTrue(metrics_ready(snapshot))
        snapshot["scrape_up"]["data"]["result"] = []
        with self.assertRaises(ValueError):
            metrics_ready(snapshot)


if __name__ == "__main__":
    unittest.main()
