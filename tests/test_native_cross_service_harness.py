from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import Mock, patch

import yaml

from tests.test_native_cross_service_plan import deployment
from tests.test_native_alert_verification import downstream_payload
from tools import native_fault_remote as remote
from tools import run_native_cross_service as runner
from tools.plan_native_cross_service import load_plan
from tools.verify_native_alert import MISSING_CONFIGMAP_CAUSE, attest_downstream

RUN = "native-cross-0123456789ab"


def apply_json_patch(document, operations):
    result = copy.deepcopy(document)
    for operation in operations:
        keys = [part.replace("~1", "/").replace("~0", "~") for part in operation["path"].strip("/").split("/")]
        parent = result
        for key in keys[:-1]:
            parent = parent[key]
        key = keys[-1]
        if operation["op"] == "test":
            if parent[key] != operation["value"]:
                raise ValueError("guard_failed")
        elif operation["op"] == "remove":
            del parent[key]
        elif operation["op"] in {"add", "replace"}:
            parent[key] = copy.deepcopy(operation["value"])
        else:
            raise AssertionError("Unsupported test patch")
    return result


def state(original=None):
    original = original or deployment()
    return {"run_id": RUN, "original": original, "expected_spec": remote.desired_spec(original),
            "apply_patch": remote.injection_patch(original, RUN), "boot_id": "boot-test", "deadline": 600}


def observation():
    pod = {"metadata": {"name": "checkout-new", "uid": "fault-pod-uid", "ownerReferences": [
        {"kind": "ReplicaSet", "uid": "rs-new", "controller": True}]},
        "spec": {"volumes": [copy.deepcopy(remote.VOLUME_SPEC)], "containers": [
            {"name": "server", "volumeMounts": [copy.deepcopy(remote.MOUNT)]}]},
        "status": {"phase": "Pending"}}
    rs = {"metadata": {"uid": "rs-new", "ownerReferences": [
        {"kind": "Deployment", "uid": "snapshot-uid", "controller": True}]}}
    event = {"metadata": {"uid": "event-uid"}, "reason": "FailedMount", "involvedObject": {
        "name": "checkout-new", "uid": "fault-pod-uid", "namespace": remote.NAMESPACE},
        "message": f'MountVolume.SetUp failed for volume "{remote.VOLUME}": configmap "{remote.CONFIGMAP}" not found'}
    return {"original_pod": "pod-old", "deployment_uid": "snapshot-uid", "pods": {"items": [pod]},
            "replicasets": {"items": [rs]}, "endpoints": {"items": []}, "events": {"items": [event]}, "configmap": {}}


class NativeFaultRemoteTests(unittest.TestCase):
    def test_reference_and_strategy_are_exactly_restorable(self):
        snapshot = state()
        changed = apply_json_patch(snapshot["original"], snapshot["apply_patch"])
        patch_doc, drift = remote.cleanup_patch(snapshot, changed)
        self.assertFalse(drift)
        restored = apply_json_patch(changed, patch_doc)
        self.assertEqual(restored["spec"], snapshot["original"]["spec"])
        self.assertNotIn(remote.MARKER, restored["metadata"]["annotations"])
        self.assertEqual(changed["spec"]["strategy"], {"type": "Recreate"})

    def test_cleanup_preserves_concurrent_image_and_annotations(self):
        snapshot = state()
        changed = apply_json_patch(snapshot["original"], snapshot["apply_patch"])
        changed["spec"]["template"]["spec"]["containers"][0]["image"] = "user-new-image"
        changed["metadata"]["annotations"]["user-note"] = "keep"
        patch_doc, drift = remote.cleanup_patch(snapshot, changed)
        self.assertTrue(drift)
        restored = apply_json_patch(changed, patch_doc)
        self.assertEqual(restored["spec"]["template"]["spec"]["containers"][0]["image"], "user-new-image")
        self.assertEqual(restored["metadata"]["annotations"]["user-note"], "keep")
        self.assertNotIn("volumes", restored["spec"]["template"]["spec"])
        self.assertEqual(restored["spec"]["strategy"], snapshot["original"]["spec"]["strategy"])

    def test_cleanup_does_not_replace_a_user_changed_strategy(self):
        snapshot = state()
        changed = apply_json_patch(snapshot["original"], snapshot["apply_patch"])
        custom = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 2, "maxUnavailable": 0}}
        changed["spec"]["strategy"] = custom
        patch_doc, drift = remote.cleanup_patch(snapshot, changed)
        self.assertTrue(drift)
        self.assertEqual(apply_json_patch(changed, patch_doc)["spec"]["strategy"], custom)

    def test_cleanup_refuses_wrong_uid_marker_or_modified_owned_volume(self):
        for case in ("uid", "marker", "volume", "mount"):
            with self.subTest(case=case):
                snapshot = state()
                changed = apply_json_patch(snapshot["original"], snapshot["apply_patch"])
                if case == "uid":
                    changed["metadata"]["uid"] = "replacement"
                elif case == "marker":
                    changed["metadata"]["annotations"][remote.MARKER] = "other-run"
                elif case == "volume":
                    changed["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"] = "user-config"
                else:
                    changed["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][0]["mountPath"] = "/user"
                with self.assertRaises(RuntimeError):
                    remote.cleanup_patch(snapshot, changed)

    def test_get_only_ignores_actual_notfound(self):
        with patch.object(remote, "kube", return_value=""):
            self.assertEqual(remote.get("configmap", remote.CONFIGMAP), {})
        with patch.object(remote, "kube", side_effect=RuntimeError("Forbidden")):
            with self.assertRaises(RuntimeError):
                remote.get("configmap", remote.CONFIGMAP)

    def test_run_id_cannot_escape_private_state_directory(self):
        for run_id in ("../anything", "/", "native-cross-xyz", "native-cross-0123456789ab/other"):
            with self.subTest(run_id=run_id), self.assertRaises(RuntimeError):
                remote.state_dir(run_id)

    def test_prepare_saves_restore_state_and_arms_watchdog_without_injection(self):
        original = deployment()
        calls = []
        def kube(*args, data=None):
            calls.append(args)
            if args[0] == "patch":
                self.assertIn("--dry-run=server", args)
                admitted = copy.deepcopy(original)
                admitted["spec"] = remote.desired_spec(original)
                return json.dumps(admitted)
            return ""
        def run(args, **kwargs):
            calls.append(tuple(args))
            if args[0] == "kubectl":
                return json.dumps({"data": {"ClusterConfiguration": "clusterName: agent-rca-chaos-eval\n"}})
            if args[0] == "systemd-run":
                remote.save(root, "armed.json", {"deadline": 700})
            return "active"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(remote, "get", side_effect=lambda kind, name=None: original if kind == "deployment" else {}), \
                 patch.object(remote, "checkout_ready", return_value=True), patch.object(remote, "kube", side_effect=kube), \
                 patch.object(remote, "run", side_effect=run), patch.object(remote, "boot_id", return_value="boot-test"), \
                 patch.object(remote.time, "monotonic", return_value=100):
                result = remote.prepare(root, RUN, {"original": original, "cluster_id": remote.CLUSTER, "maximum_active_seconds": 600})
            saved = json.loads((root / "state.json").read_text())
            self.assertEqual(saved["deadline"], 700)
            self.assertEqual(saved["original"], original)
            self.assertTrue(result["armed"])
            self.assertLess(next(i for i, c in enumerate(calls) if c[0] == "create"), next(i for i, c in enumerate(calls) if c[0] == "systemd-run"))
            self.assertFalse((root / "apply-attempted.json").exists())

    def test_apply_cannot_repeat_after_uncertain_command_result(self):
        snapshot = state()
        with tempfile.TemporaryDirectory() as directory, patch.object(remote, "STATE_ROOT", Path(directory)), \
             patch.object(remote, "boot_id", return_value="boot-test"), patch.object(remote.time, "monotonic", return_value=1), \
             patch.object(remote, "run", return_value="active"), patch.object(remote, "get", return_value={"data": {"run_id": RUN}}), \
             patch.object(remote, "kube", side_effect=TimeoutError("response lost")) as mutation:
            folder = remote.state_dir(RUN)
            folder.mkdir()
            remote.save(folder, "state.json", snapshot)
            remote.save(folder, "armed.json", {"deadline": 600})
            with self.assertRaises(TimeoutError):
                remote.execute_action("apply", RUN)
            with self.assertRaisesRegex(RuntimeError, "repeat_injection"):
                remote.execute_action("apply", RUN)
            self.assertEqual(mutation.call_count, 1)

    def test_expired_deadline_or_reboot_forbids_apply(self):
        for now, boot in ((601, "boot-test"), (1, "different-boot")):
            with self.subTest(now=now, boot=boot), tempfile.TemporaryDirectory() as directory, \
                 patch.object(remote, "STATE_ROOT", Path(directory)), patch.object(remote, "boot_id", return_value=boot), \
                 patch.object(remote.time, "monotonic", return_value=now), patch.object(remote, "kube") as mutation:
                folder = remote.state_dir(RUN)
                folder.mkdir()
                remote.save(folder, "state.json", state())
                with self.assertRaisesRegex(RuntimeError, "deadline_expired"):
                    remote.execute_action("apply", RUN)
                mutation.assert_not_called()

    def test_watchdog_uses_persisted_deadline_without_local_controller(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(remote, "STATE_ROOT", Path(directory)), \
             patch.object(remote, "boot_id", return_value="boot-test"), patch.object(remote.time, "monotonic", side_effect=[598, 601]), \
             patch.object(remote.time, "sleep") as sleeper, patch.object(remote, "execute_action", return_value={"ready": True}) as action:
            folder = remote.state_dir(RUN)
            folder.mkdir()
            remote.save(folder, "state.json", state())
            remote.watchdog(RUN)
            sleeper.assert_called_once_with(2)
            action.assert_called_once_with("restore", RUN)

    def test_restore_retains_lock_when_readiness_not_recovered(self):
        snapshot = state()
        with tempfile.TemporaryDirectory() as directory, patch.object(remote, "checkout_ready", return_value=False), \
             patch.object(remote, "get", side_effect=lambda kind, name=None: snapshot["original"] if kind == "deployment" else {"data": {"run_id": RUN}}), \
             patch.object(remote, "kube") as kube:
            folder = Path(directory)
            remote.save(folder, "state.json", snapshot)
            result = remote.restore(folder)
            self.assertFalse(result["ready"])
            self.assertTrue(result["watchdog_retained"])
            self.assertFalse((folder / "complete.json").exists())
            kube.assert_not_called()

    def test_lock_delete_has_uid_and_resource_version_preconditions(self):
        snapshot = state()
        lock = {"metadata": {"uid": "lock-uid", "resourceVersion": "9"}, "data": {"run_id": RUN}}
        def get(kind, name=None):
            return snapshot["original"] if kind == "deployment" else lock if name == remote.LOCK else {}
        with tempfile.TemporaryDirectory() as directory, patch.object(remote, "checkout_ready", return_value=True), \
             patch.object(remote, "get", side_effect=get), patch.object(remote, "kube") as kube:
            folder = Path(directory)
            remote.save(folder, "state.json", snapshot)
            self.assertTrue(remote.restore(folder)["ready"])
            self.assertEqual(json.loads(kube.call_args.kwargs["data"])["preconditions"], {"uid": "lock-uid", "resourceVersion": "9"})
            self.assertTrue((folder / "complete.json").exists())


class NativeCrossServiceObservationTests(unittest.TestCase):
    def test_controller_restores_after_remote_or_observation_failure_without_reinjection(self):
        for failed_action in ("prepare", "apply", "observe"):
            with self.subTest(failed_action=failed_action), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                original = deployment()
                pods = {"items": [{"metadata": {"name": "old", "uid": "pod-old", "labels": {"app": remote.DEPLOYMENT}},
                    "status": {"phase": "Running", "containerStatuses": [{"name": "server", "ready": True, "restartCount": 0}]}}]}
                target, control, observability = Mock(), Mock(), Mock()
                actions = []
                def helper(args, **kwargs):
                    if args[:3] == ["sudo", "python3", runner.REMOTE_HELPER]:
                        action = args[3]
                        actions.append(action)
                        if action == failed_action:
                            raise RuntimeError("uncertain_remote_response")
                        return json.dumps({"ready": True, "exact_restoration": True})
                    actions.append("stop-watchdog")
                    return ""
                target.run.side_effect = helper
                target.kube.side_effect = lambda *args: json.dumps(original) if args[:2] == ("get", "deployment") else "" if args[:2] == ("get", "configmap") else json.dumps({"spec": {"clusterIP": "192.0.2.5"}})
                target.get.return_value = {"items": []}
                observability.kube.return_value = json.dumps({"spec": {"clusterIP": "192.0.2.6"}})
                def work(_stack, destination, base, *, duration, profile, marker):
                    if profile == "normal":
                        (destination / "normal-workload.log").write_text(json.dumps({"request_attempts": 10, "transport_errors": 0, "status_families": {"2xx": 10}}))
                    return Mock(returncode=0, poll=Mock(return_value=0 if profile == "normal" else None))
                for name, value in {
                    "ROOT": Path(directory), "target_health": Mock(return_value=([original], pods)),
                    "require_healthy": Mock(), "no_open_incident": Mock(),
                    "Tunnel": Mock(return_value=Mock(base_url="http://127.0.0.1:12345", process=Mock(poll=Mock(return_value=None)))),
                    "http_json": Mock(return_value={"status": "success"}),
                    "preflight": Mock(return_value={"observed_at": "2026-09-08T00:00:00Z"}),
                    "krca_baseline": Mock(return_value={"status": "SUCCEEDED"}),
                    "fault_postcondition": Mock(side_effect=TypeError("private-input-example")),
                    "checked_rule": Mock(return_value={"state": "inactive"}), "workload": work,
                }.items():
                    stack.enter_context(patch.object(runner, name, value))
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.execute(target, control, observability), 1)
                self.assertEqual(actions.count("prepare"), 1)
                self.assertEqual(actions.count("apply"), int(failed_action != "prepare"))
                self.assertIn("restore", actions)
                self.assertGreater(actions.index("stop-watchdog"), actions.index("restore"))
                result = json.loads(next(Path(directory).rglob("result.json")).read_text())
                self.assertEqual(result["status"], "FAILED")
                self.assertTrue(result["cleanup_verified"])
                self.assertEqual(result["reason"], "verification_failed" if failed_action == "observe" else "uncertain_remote_response")
                self.assertEqual(result["failure"]["phase"], {"prepare": "watchdog_prepare", "apply": "fault_apply", "observe": "fault_observation"}[failed_action])
                self.assertEqual(result["phase"], "finished")
                self.assertEqual(result["failure"]["error_type"], "TypeError" if failed_action == "observe" else "RuntimeError")
                self.assertEqual(result["failure"]["location"]["function"], "observe_fault" if failed_action == "observe" else "helper")
                self.assertNotIn("private-input-example", json.dumps(result))
                if failed_action == "observe":
                    observed = json.loads(next(Path(directory).rglob("fault-observation.json")).read_text())
                    self.assertEqual(observed["endpoints"], {"items": []})

    def test_exact_failedmount_is_accepted(self):
        result = runner.fault_postcondition(**observation())
        self.assertEqual(result["pod_uid"], "fault-pod-uid")
        self.assertTrue(result["required_reference"])

    def test_empty_endpoint_slice_collection_accepts_null_or_empty_array(self):
        # During the live Recreate gap Kubernetes returned endpoints: null.
        for endpoints in (None, []):
            with self.subTest(endpoints=endpoints):
                value = observation()
                value["endpoints"]["items"] = [{
                    "metadata": {"labels": {"kubernetes.io/service-name": remote.DEPLOYMENT}},
                    "endpoints": endpoints,
                }]
                self.assertEqual(runner.fault_postcondition(**value)["ready_endpoints"], 0)

    def test_null_slice_does_not_hide_another_serving_or_unknown_slice(self):
        for conditions in ({"ready": True}, {}, None, {"ready": False}, {"ready": False, "serving": True}):
            with self.subTest(conditions=conditions):
                value = observation()
                metadata = {"labels": {"kubernetes.io/service-name": remote.DEPLOYMENT}}
                value["endpoints"]["items"] = [
                    {"metadata": metadata, "endpoints": None},
                    {"metadata": metadata, "endpoints": [{"conditions": conditions}]},
                ]
                self.assertIsNone(runner.fault_postcondition(**value))

    def test_explicitly_not_ready_and_not_serving_endpoint_is_not_serving(self):
        value = observation()
        value["endpoints"]["items"] = [{
            "metadata": {"labels": {"kubernetes.io/service-name": remote.DEPLOYMENT}},
            "endpoints": [{"conditions": {"ready": False, "serving": False}}],
        }]
        self.assertEqual(runner.fault_postcondition(**value)["ready_endpoints"], 0)

    def test_failure_details_omit_messages_locals_and_external_frames(self):
        private_value = "private-observation-example"
        try:
            runner.require(False, private_value)
        except RuntimeError as error:
            details = runner.failure_details(error, "fault_observation")
        self.assertEqual(details["phase"], "fault_observation")
        self.assertEqual(details["location"]["function"], "require")
        self.assertIsInstance(details["location"]["line"], int)
        self.assertNotIn(private_value, json.dumps(details))
        self.assertEqual(set(details["location"]), {"function", "line"})

    def test_invalid_endpoint_collection_is_not_absence_proof(self):
        for endpoints in (False, 0, "", {}, "invalid", [None]):
            with self.subTest(endpoints=endpoints):
                value = observation()
                value["endpoints"]["items"] = [{
                    "metadata": {"labels": {"kubernetes.io/service-name": remote.DEPLOYMENT}},
                    "endpoints": endpoints,
                }]
                with self.assertRaisesRegex(RuntimeError, "invalid_endpoint"):
                    runner.fault_postcondition(**value)

    def test_ambiguous_or_still_serving_workload_is_not_fault_proof(self):
        for case in ("old-pod", "endpoint", "unknown-endpoint", "wrong-owner", "wrong-event", "optional", "missing-mount", "two-pods"):
            with self.subTest(case=case):
                value = observation()
                pod = value["pods"]["items"][0]
                if case == "old-pod":
                    value["pods"]["items"].append({"metadata": {"uid": "pod-old"}, "spec": {}, "status": {}})
                elif case in {"endpoint", "unknown-endpoint"}:
                    value["endpoints"]["items"] = [{"metadata": {"labels": {"kubernetes.io/service-name": remote.DEPLOYMENT}},
                        "endpoints": [{"conditions": {"ready": True} if case == "endpoint" else {}}]}]
                elif case == "wrong-owner":
                    pod["metadata"]["ownerReferences"][0]["uid"] = "other-rs"
                elif case == "wrong-event":
                    value["events"]["items"][0]["involvedObject"]["uid"] = "different-pod"
                elif case == "optional":
                    pod["spec"]["volumes"][0]["configMap"]["optional"] = True
                elif case == "missing-mount":
                    pod["spec"]["containers"][0]["volumeMounts"] = []
                else:
                    value["pods"]["items"] *= 2
                self.assertIsNone(runner.fault_postcondition(**value))

    def test_existing_configmap_is_failure_not_missing_proof(self):
        value = observation()
        value["configmap"] = {"metadata": {"name": remote.CONFIGMAP}}
        with self.assertRaises(RuntimeError):
            runner.fault_postcondition(**value)

    def test_configmap_attestation_preserves_same_downstream_proof_chain(self):
        value = downstream_payload()
        value["plan_id"] = load_plan()["plan_id"]
        value["fault_postcondition"] = runner.fault_postcondition(**observation())
        value["bundle"]["report"]["root_cause"]["cause_id"] = MISSING_CONFIGMAP_CAUSE
        expected = copy.deepcopy(value)
        result = attest_downstream(value, fault_family=MISSING_CONFIGMAP_CAUSE)
        self.assertTrue(result["cross_service_verified"], result)
        self.assertEqual(value, expected)
        for key, changed in (("configmap_absent", False), ("required_reference", False), ("old_pod_absent", False), ("ready_endpoints", 1), ("configmap_name", "other")):
            with self.subTest(key=key):
                bad = copy.deepcopy(value)
                bad["fault_postcondition"][key] = changed
                self.assertFalse(attest_downstream(bad, fault_family=MISSING_CONFIGMAP_CAUSE)["cross_service_verified"])
        value["bundle"]["agent_run"]["inspected_evidence_ids"] = []
        self.assertFalse(attest_downstream(value, fault_family=MISSING_CONFIGMAP_CAUSE)["cross_service_verified"])

    def test_missing_authorization_never_constructs_hosts(self):
        args = []
        for index, domain in enumerate(("target", "control", "observability")):
            args += [f"--{domain}-host", f"192.0.2.{index+1}", f"--{domain}-user", "test", f"--{domain}-key", "/nonexistent"]
        with patch.object(runner, "Host") as host, self.assertRaisesRegex(RuntimeError, "authorization_required"):
            runner.main(args)
        host.assert_not_called()

    def test_expired_local_wait_does_not_start_a_new_operation(self):
        check = Mock()
        with patch.object(runner.time, "monotonic", return_value=10), self.assertRaisesRegex(RuntimeError, "deadline_exceeded"):
            runner.wait_until(check, 9, label="bounded")
        check.assert_not_called()

    def test_playbook_and_make_are_opt_in_and_do_not_reuse_synthetic_harness(self):
        root = Path(__file__).resolve().parents[1]
        role = (root / "automation/ansible/roles/native_cross_service_harness/tasks/main.yml").read_text()
        yaml.safe_load(role)
        self.assertIn("confirm_controlled_fault | default('') == 'yes'", role)
        self.assertIn("no_log: true", role)
        self.assertNotIn("controlled_fault_evaluation", role)
        self.assertNotIn("krca_coverage_smoke", role)
        make = (root / "Makefile").read_text().split("verify-native-cross-service:")[1].split("\n\n")[0]
        self.assertIn('test "$(CONFIRM_CONTROLLED_FAULT)" = "yes"', make)
        self.assertIn("$(ANSIBLE_TARGET_INVENTORY)", make)


if __name__ == "__main__":
    unittest.main()
