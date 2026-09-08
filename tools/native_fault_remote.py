#!/usr/bin/env python3
"""Root-owned, target-local restoration for one native ConfigMap drill.

Standard library only: this file is copied to the fault VM by Ansible. No LLM,
Alert submission, or credentials are accepted. Private snapshots stay on the VM.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

NAMESPACE = "online-boutique"
DEPLOYMENT = "checkoutservice"
CLUSTER = "agent-rca-chaos-eval"
LOCK = "agent-rca-missing-configmap-lock"
MARKER = "agent-rca.dev/controlled-fault-id"
VOLUME = "agent-rca-native-required-config"
CONFIGMAP = "checkoutservice-agent-rca-native-missing"
MOUNT = {"name": VOLUME, "mountPath": "/var/run/agent-rca-native-required-config", "readOnly": True}
VOLUME_SPEC = {"name": VOLUME, "configMap": {"name": CONFIGMAP, "optional": False, "defaultMode": 420}}
BASE = ["kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "--request-timeout=10s", "-n", NAMESPACE]
STATE_ROOT = Path("/var/lib/agent-rca-native")


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def run(argv, *, data=None, timeout=20):
    result = subprocess.run(argv, input=data, text=True, capture_output=True, timeout=timeout)
    require(result.returncode == 0, "remote_operation_failed")
    return result.stdout.strip()


def kube(*args, data=None):
    return run(BASE + list(args), data=data)


def get(kind, name=None):
    # Only NotFound becomes an empty object; authorization/transport errors fail.
    value = kube("get", kind, *([name] if name else []), "--ignore-not-found", "-o", "json")
    return json.loads(value) if value else {}


def save(directory, name, value):
    temporary = directory / (name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    temporary.replace(directory / name)


def state_dir(run_id):
    require(bool(re.fullmatch(r"native-cross-[a-f0-9]{12}", run_id)), "invalid_run_id")
    directory = STATE_ROOT / run_id
    require(not STATE_ROOT.is_symlink() and not directory.is_symlink(), "unsafe_state_path")
    return directory


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def desired_spec(original):
    spec = copy.deepcopy(original["spec"])
    require(spec.get("replicas") == 1 and spec.get("strategy", {}).get("type") == "RollingUpdate", "invalid_baseline")
    pod = spec["template"]["spec"]
    containers = [c for c in pod["containers"] if c["name"] == "server"]
    require(len(containers) == 1, "ambiguous_container")
    require(not any(v["name"] == VOLUME for v in pod.get("volumes", [])), "existing_fault_volume")
    require(not any(m.get("name") == VOLUME or m.get("mountPath") == MOUNT["mountPath"]
                    for c in pod["containers"] for m in c.get("volumeMounts", [])), "existing_fault_mount")
    pod.setdefault("volumes", []).append(copy.deepcopy(VOLUME_SPEC))
    containers[0].setdefault("volumeMounts", []).append(copy.deepcopy(MOUNT))
    spec["strategy"] = {"type": "Recreate"}
    return spec


def injection_patch(original, run_id):
    desired = desired_spec(original)
    annotations = original["metadata"].get("annotations", {})
    require(MARKER not in annotations, "existing_fault_marker")
    return [
        {"op": "test", "path": "/metadata/uid", "value": original["metadata"]["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": original["metadata"]["resourceVersion"]},
        {"op": "test", "path": "/spec", "value": original["spec"]},
        {"op": "replace", "path": "/spec/strategy", "value": desired["strategy"]},
        {"op": "replace", "path": "/spec/template", "value": desired["template"]},
        {"op": "add", "path": "/metadata/annotations", "value": {**annotations, MARKER: run_id}},
    ]


def cleanup_patch(state, current):
    """Remove only our exact reference and strategy; preserve concurrent edits."""
    original, expected = state["original"], state["expected_spec"]
    require(current["metadata"]["uid"] == original["metadata"]["uid"], "deployment_uid_changed")
    annotations = current["metadata"].get("annotations", {})
    if MARKER not in annotations:
        require(current["spec"] == original["spec"], "unowned_spec_drift")
        return [], False
    require(annotations[MARKER] == state["run_id"], "deployment_ownership_changed")
    modified = copy.deepcopy(current["spec"])
    if modified == expected:
        modified = copy.deepcopy(original["spec"])
    else:
        # A concurrent deployment must not be rolled back wholesale.
        pod, old_pod = modified["template"]["spec"], original["spec"]["template"]["spec"]
        for key, field in (("volumes", VOLUME_SPEC),):
            found = [v for v in pod.get(key, []) if v.get("name") == VOLUME]
            require(not found or found == [field], "owned_volume_modified")
            pod[key] = [v for v in pod.get(key, []) if v.get("name") != VOLUME]
            if not pod[key] and key not in old_pod:
                pod.pop(key)
        for container in pod["containers"]:
            found = [m for m in container.get("volumeMounts", []) if m.get("name") == VOLUME]
            require(not found or (container["name"] == "server" and found == [MOUNT]), "owned_mount_modified")
            if found:
                container["volumeMounts"] = [m for m in container["volumeMounts"] if m.get("name") != VOLUME]
                old = next(c for c in old_pod["containers"] if c["name"] == container["name"])
                if not container["volumeMounts"] and "volumeMounts" not in old:
                    container.pop("volumeMounts")
        if modified.get("strategy") == expected["strategy"]:
            modified["strategy"] = copy.deepcopy(original["spec"]["strategy"])
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": original["metadata"]["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
        {"op": "test", "path": "/spec", "value": current["spec"]},
        {"op": "replace", "path": "/spec/template", "value": modified["template"]},
        {"op": "replace", "path": "/spec/strategy", "value": modified["strategy"]},
        {"op": "remove", "path": "/metadata/annotations/agent-rca.dev~1controlled-fault-id"},
    ]
    return patch, modified != original["spec"]


def checkout_ready():
    pods = get("pods").get("items", [])
    pods = [p for p in pods if p["metadata"].get("labels", {}).get("app") == DEPLOYMENT]
    return len(pods) == 1 and all(
        not p["metadata"].get("deletionTimestamp") and p["status"].get("phase") == "Running"
        and p["status"].get("containerStatuses")
        and all(c.get("ready") and c.get("restartCount") == 0 for c in p["status"]["containerStatuses"])
        for p in pods)


def restore(directory):
    state = json.loads((directory / "state.json").read_text())
    lock = get("configmap", LOCK)
    require(not lock or lock.get("data", {}).get("run_id") == state["run_id"], "fault_lock_ownership_changed")
    current = get("deployment", DEPLOYMENT)
    patch, drift = cleanup_patch(state, current)
    if patch:
        kube("patch", "deployment", DEPLOYMENT, "--type=json", "-p", json.dumps(patch))
    current = get("deployment", DEPLOYMENT)
    exact = current["spec"] == state["original"]["spec"] and MARKER not in current["metadata"].get("annotations", {})
    ready = exact and checkout_ready() and not get("configmap", CONFIGMAP)
    result = {"exact_restoration": exact, "ready": ready, "concurrent_drift": drift,
              "checked_at": time.time(), "watchdog_retained": not ready}
    save(directory, "recovery.json", result)
    if ready:
        if lock:
            # UID+resourceVersion preconditions prevent deleting a replaced lock.
            manifest = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {
                "uid": lock["metadata"]["uid"], "resourceVersion": lock["metadata"]["resourceVersion"]}}
            kube("delete", "--raw", f"/api/v1/namespaces/{NAMESPACE}/configmaps/{LOCK}", "-f", "-", data=json.dumps(manifest))
        save(directory, "complete.json", result)
    return result


def prepare(directory, run_id, request):
    require(request.get("maximum_active_seconds") == 600, "invalid_deadline")
    require(request.get("cluster_id") == CLUSTER, "cluster_scope_mismatch")
    configuration = json.loads(run(BASE[:3] + ["-n", "kube-system", "get", "configmap", "kubeadm-config", "-o", "json"]))
    require(bool(re.search(r"(?m)^clusterName:\s*" + re.escape(CLUSTER) + r"\s*$",
                          configuration["data"]["ClusterConfiguration"])), "wrong_kubeadm_cluster")
    require(not get("configmap", CONFIGMAP), "required_configmap_exists")
    existing = get("configmaps").get("items", [])
    require(not any(c["metadata"]["name"].endswith("-lock") for c in existing), "existing_fault_lock")
    original = get("deployment", DEPLOYMENT)
    require(checkout_ready() and original == request["original"], "baseline_changed_before_prepare")
    patch = injection_patch(original, run_id)
    admitted = json.loads(kube("patch", "deployment", DEPLOYMENT, "--type=json", "--dry-run=server", "-o", "json", "-p", json.dumps(patch)))
    require(admitted["spec"] == desired_spec(original), "unexpected_admission_mutation")
    lock_doc = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": LOCK, "namespace": NAMESPACE}, "data": {"run_id": run_id}}
    # Persist recovery before any lock or workload mutation.
    state = {"run_id": run_id, "original": original, "expected_spec": admitted["spec"],
             "apply_patch": patch, "deadline": time.monotonic() + 600,
             "boot_id": boot_id()}
    save(directory, "state.json", state)
    kube("create", "-f", "-", data=json.dumps(lock_doc))
    unit = run_id + "-restore"
    # Freeze this run's helper so a later install cannot change its recovery code.
    helper_file = directory / "watchdog.py"
    helper_file.write_text(Path(__file__).read_text())
    helper_file.chmod(0o700)
    # Start immediately; the process itself uses the persisted absolute deadline.
    run(["systemd-run", "--unit=" + unit, "--property=Type=exec", "--property=Restart=on-failure", "--property=RestartSec=5s",
         "/usr/bin/python3", str(helper_file), "watchdog", "--run-id", run_id])
    run(["systemctl", "is-active", unit])
    handshake_deadline = time.monotonic() + 10
    while not (directory / "armed.json").exists() and time.monotonic() < handshake_deadline:
        time.sleep(0.1)
    require((directory / "armed.json").exists(), "watchdog_handshake_missing")
    return {"armed": True, "maximum_active_seconds": 600}


def execute_action(action, run_id, request=None):
    directory = state_dir(run_id)
    if action == "prepare":
        STATE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.mkdir(mode=0o700, exist_ok=False)
    require(directory.is_dir(), "state_directory_missing")
    with (directory / "mutex").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if action == "prepare":
            return prepare(directory, run_id, request)
        state = json.loads((directory / "state.json").read_text())
        if action == "apply":
            require(not (directory / "apply-attempted.json").exists(), "repeat_injection_forbidden")
            require(time.monotonic() < state["deadline"] and state["boot_id"] == boot_id(), "fault_deadline_expired")
            require((directory / "armed.json").exists(), "watchdog_not_armed")
            require(get("configmap", LOCK).get("data", {}).get("run_id") == run_id, "fault_lock_not_owned")
            run(["systemctl", "is-active", run_id + "-restore"])
            save(directory, "apply-attempted.json", {"at": time.time()})
            kube("patch", "deployment", DEPLOYMENT, "--type=json", "-p", json.dumps(state["apply_patch"]))
            current = get("deployment", DEPLOYMENT)
            require(current["spec"] == state["expected_spec"], "post_apply_spec_drift")
            return {"applied": True, "remaining_seconds": max(0, state["deadline"] - time.monotonic())}
        if action == "restore":
            return restore(directory)
        raise RuntimeError("unknown_action")


def watchdog(run_id):
    directory = state_dir(run_id)
    state = json.loads((directory / "state.json").read_text())
    save(directory, "armed.json", {"deadline": state["deadline"], "boot_id": state["boot_id"]})
    while not (directory / "complete.json").exists():
        same_boot = state["boot_id"] == boot_id()
        remaining = state["deadline"] - time.monotonic() if same_boot else 0
        if remaining > 0:
            time.sleep(min(5, remaining))
            continue
        result = execute_action("restore", run_id)
        if result["ready"]:
            break
        time.sleep(5)
    return {"watchdog_completed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "apply", "restore", "watchdog"))
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    require(os.geteuid() == 0, "requires_root")
    os.umask(0o077)
    try:
        result = watchdog(args.run_id) if args.action == "watchdog" else execute_action(
            args.action, args.run_id, json.load(sys.stdin) if args.action == "prepare" else None)
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__,
                          "reason": str(error) if type(error) is RuntimeError else "remote_helper_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
