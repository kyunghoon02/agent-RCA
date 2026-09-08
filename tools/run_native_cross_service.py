#!/usr/bin/env python3
"""Opt-in single native frontend -> checkout ConfigMap verification.

No synthetic Alert, no prompt/Gate changes, no automatic reinjection. All raw
artifacts are private. The target's independent watchdog owns the hard deadline.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from tools.plan_native_cross_service import build_plan, deployment_patch_preview, load_plan
from tools.verify_hubble_evidence import Host, fingerprint, require_healthy, utc
from tools.verify_native_alert import ALERT_NAME, MISSING_CONFIGMAP_CAUSE, NativeAlertError, attest_downstream, capture, checked_rule, preflight
from tools.native_fault_remote import CLUSTER, CONFIGMAP, DEPLOYMENT, MARKER, MOUNT, NAMESPACE, VOLUME

REMOTE_HELPER = "/usr/local/lib/agent-rca-native-fault.py"
EXPECTED_APPS = {"adservice", "cartservice", "checkoutservice", "currencyservice", "emailservice",
                 "frontend", "paymentservice", "productcatalogservice", "recommendationservice",
                 "redis-cart", "shippingservice", "opentelemetrycollector"}


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def failure_details(error, phase):
    """Record only code-owned locations, never exception payloads or locals."""
    location = None
    frame = error.__traceback__
    while frame is not None:
        if frame.tb_frame.f_code.co_filename == __file__:
            location = {"function": frame.tb_frame.f_code.co_name, "line": frame.tb_lineno}
        frame = frame.tb_next
    return {"phase": phase, "error_type": type(error).__name__, "location": location}


def save(directory, name, value):
    path = directory / (name + ".json")
    temporary = directory / (name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def http_json(base, path):
    with urlopen(base + path, timeout=10) as response:
        data = response.read(16_000_001)
    require(len(data) <= 16_000_000, "telemetry_response_too_large")
    return json.loads(data)


class Tunnel:
    """One owned SSH process; directly forwards to the private Service IP."""
    def __init__(self, host, service_ip, port):
        ipaddress.IPv4Address(service_ip)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            local = sock.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{local}"
        self.process = subprocess.Popen(host.base[:-1] + ["-N", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-L",
            f"127.0.0.1:{local}:{service_ip}:{port}", host.base[-1]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def wait_until(check, deadline, *, label, tick=5):
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        print(f"{utc()} Waiting: {label}", flush=True)
        time.sleep(min(tick, max(0, deadline - time.monotonic())))
    raise RuntimeError(label + "_deadline_exceeded")


def database(control, sql):
    command = 'PGPASSWORD="$POSTGRES_PASSWORD" psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --tuples-only --no-align --command=' + shlex.quote(sql)
    raw = control.kube("exec", "-n", "incident-platform", "statefulset/postgresql", "--", "/bin/sh", "-ec", command)
    return json.loads(raw) if raw else None


def read_bundle(control, incident_id):
    require(bool(re.fullmatch(r"inc-[a-f0-9]{24}", incident_id)), "unsafe_incident_id")
    return database(control, """SELECT json_build_object(
      'incident', i.document,
      'context', (SELECT document FROM context_packages WHERE incident_id=i.incident_id ORDER BY frozen_at DESC LIMIT 1),
      'evidence', (SELECT COALESCE(json_agg(document ORDER BY observed_at,evidence_id),'[]'::json) FROM evidence_items WHERE incident_id=i.incident_id),
      'audit_events', (SELECT COALESCE(json_agg(json_build_object('event_type',event_type,'occurred_at',occurred_at,'details',details) ORDER BY event_id),'[]'::json) FROM incident_audit_events WHERE incident_id=i.incident_id AND event_type='LOCALIZATION_COLLECTION_COMPLETED'),
      'report', (SELECT document FROM rca_reports WHERE incident_id=i.incident_id ORDER BY generated_at DESC LIMIT 1),
      'agent_run', (SELECT document FROM agent_runs WHERE incident_id=i.incident_id ORDER BY started_at DESC LIMIT 1)
    ) FROM incidents i WHERE i.incident_id='""" + incident_id + "';")


def no_open_incident(control):
    count = database(control, """SELECT count(*) FROM incidents WHERE
      document->'alert'->>'name'='OnlineBoutiqueCheckoutHighFailureRate'
      AND document->'alert'->'labels'->>'cluster_id'='agent-rca-chaos-eval'
      AND document->'window'->>'incident_end' IS NULL;""")
    require(count == 0, "matching_incident_still_open")


def target_health(target, *, no_fault=True):
    deployments = target.get("deployments", NAMESPACE)["items"]
    require({d["metadata"]["name"] for d in deployments} == EXPECTED_APPS, "application_set_changed")
    require(all(d["status"].get("readyReplicas") == d["spec"].get("replicas") == 1 for d in deployments), "application_not_ready")
    pods = target.get("pods", NAMESPACE)
    require_healthy(pods)
    if no_fault:
        configmaps = target.get("configmaps", NAMESPACE)["items"]
        require(not any(c["metadata"]["name"].endswith("-lock") for c in configmaps), "existing_fault_lock")
        require(not any(MARKER in d["metadata"].get("annotations", {}) for d in deployments), "existing_fault_marker")
        for kind in ("stresschaos", "networkchaos", "podchaos", "ciliumnetworkpolicies"):
            require(not target.get(kind, NAMESPACE)["items"], "existing_fault_or_policy")
    return deployments, pods


def fault_postcondition(original_pod, deployment_uid, pods, replicasets, endpoints, events, configmap):
    """Return None until the exact non-serving FailedMount postcondition exists."""
    require(not configmap, "required_configmap_appeared")
    owned_rs = {r["metadata"]["uid"] for r in replicasets["items"] if any(
        owner.get("uid") == deployment_uid and owner.get("kind") == "Deployment" and owner.get("controller") is True
        for owner in r["metadata"].get("ownerReferences", []))}
    candidates = [p for p in pods["items"] if not p["metadata"].get("deletionTimestamp") and any(
        o.get("uid") in owned_rs and o.get("kind") == "ReplicaSet" and o.get("controller") is True
        for o in p["metadata"].get("ownerReferences", [])) and any(
        v.get("name") == VOLUME and v.get("configMap", {}).get("name") == CONFIGMAP
        and v["configMap"].get("optional") is False for v in p["spec"].get("volumes", []))]
    old_absent = all(p["metadata"]["uid"] != original_pod for p in pods["items"])
    # Kubernetes can serialize a zero-endpoint slice as endpoints: null.
    # Normalize only null, not arbitrary falsy/malformed responses. Unknown
    # endpoint readiness or serving state is not proof of no serving endpoints.
    serving = []
    for endpoint_slice in endpoints["items"]:
        if endpoint_slice["metadata"].get("labels", {}).get("kubernetes.io/service-name") != DEPLOYMENT:
            continue
        entries = endpoint_slice.get("endpoints")
        if entries is None:
            entries = []
        require(isinstance(entries, list), "invalid_endpoint_collection")
        for entry in entries:
            require(isinstance(entry, dict), "invalid_endpoint_entry")
            conditions = entry.get("conditions")
            if conditions is None:
                conditions = {}
            require(isinstance(conditions, dict), "invalid_endpoint_conditions")
            if conditions.get("ready") is not False or conditions.get("serving") is not False:
                serving.append(entry)
    if len(candidates) != 1 or not old_absent or serving:
        return None
    pod = candidates[0]
    if not any(c.get("name") == "server" and MOUNT in c.get("volumeMounts", []) for c in pod["spec"].get("containers", [])):
        return None
    if any(c.get("ready") for c in pod["status"].get("containerStatuses", [])):
        return None
    matches = [e for e in events["items"] if e.get("reason") == "FailedMount"
               and e.get("involvedObject", {}).get("uid") == pod["metadata"]["uid"]
               and e["involvedObject"].get("name") == pod["metadata"]["name"]
               and e["involvedObject"].get("namespace") == NAMESPACE
               and f'configmap "{CONFIGMAP}" not found' in e.get("message", "").lower()]
    if not matches:
        return None
    return {"pod_uid": pod["metadata"]["uid"], "pod_name": pod["metadata"]["name"],
            "configmap_name": CONFIGMAP, "configmap_absent": True, "required_reference": True,
            "old_pod_absent": True, "ready_endpoints": 0, "event": matches[-1]}


def krca_baseline(base_url):
    from incident_platform.evidence import CollectionRequest, EvidenceBuilder, EvidenceWindow, ResourceScope, format_time, validate_provider_batch
    from incident_platform.krca_runtime import load_krca_runtime_config
    from incident_platform.providers.prometheus import PrometheusHTTPAPI
    config = replace(load_krca_runtime_config(ROOT / "config/online-boutique-krca.yaml"), cluster_id=CLUSTER)
    profile = config.profile("checkout-full")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    request = CollectionRequest("req-native-baseline", "inc-native-baseline", EvidenceWindow(
        format_time(now - timedelta(seconds=900)), format_time(now)),
        ResourceScope(config.namespace, profile.resource_names, max_items=config.collection.max_evidence_items), config.collection.timeout_seconds)
    batch = config.provider(PrometheusHTTPAPI(base_url), profile).collect(request)
    validate_provider_batch(batch, request)
    evidence = [EvidenceBuilder().build(item, request, collected_at=now) for item in batch.items]
    require(batch.status == "SUCCEEDED" and len(evidence) == len(profile.dependencies) == 11
            and all(e["facts"]["result_status"] == "HAS_DATA" for e in evidence), "krca_baseline_incomplete")
    return {"status": batch.status, "edges": [{"edge_id": e["facts"]["edge_id"], "result_status": e["facts"]["result_status"]} for e in evidence]}


def workload(stack, directory, base_url, *, duration, profile, marker):
    output = (directory / (profile + "-workload.log")).open("w")
    stack.callback(output.close)
    process = subprocess.Popen([str(ROOT / ".venv/bin/python"), str(ROOT / "tools/run_online_boutique_workload.py"),
        "--base-url", base_url, "--duration-seconds", str(duration), "--requests-per-second", "4",
        "--seed", "342", "--marker", marker, "--profile", profile, "--timeout-seconds", "5"],
        stdout=output, stderr=subprocess.DEVNULL)
    def stop():
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    stack.callback(stop)
    return process


def execute(target, control, observability):
    plan = load_plan()
    build_plan(plan)
    run_id = "native-cross-" + uuid4().hex[:12]
    directory = ROOT / "evaluation/runs/private/native-prometheus" / run_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    result = {"run_id": run_id, "status": "RUNNING", "plan_id": plan["plan_id"], "synthetic_alert_submitted": False}
    prepared = False
    captured = None
    fault = None

    def progress(phase):
        result["phase"] = phase
        save(directory, "progress", {"phase": phase, "observed_at": utc()})

    def helper(action, data=None):
        return json.loads(target.run(["sudo", "python3", REMOTE_HELPER, action, "--run-id", run_id],
                                      data=json.dumps(data) if data else None, timeout=55))

    with ExitStack() as stack:
        try:
            progress("preflight")
            deployments, initial_pods = target_health(target)
            require_healthy(control.get("pods", "incident-platform"))
            no_open_incident(control)
            original = next(d for d in deployments if d["metadata"]["name"] == DEPLOYMENT)
            deployment_patch_preview(plan, original)
            old_pods = [p for p in initial_pods["items"] if p["metadata"].get("labels", {}).get("app") == DEPLOYMENT]
            require(len(old_pods) == 1 and all(c["restartCount"] == 0 for c in old_pods[0]["status"]["containerStatuses"]), "checkout_not_restart_zero")
            tunnels = []
            for host, service, namespace, port, label in (
                (target, "frontend", NAMESPACE, 80, "frontend"),
                (observability, "monitoring-kube-prometheus-prometheus", "observability", 9090, "prometheus"),
                (observability, "monitoring-kube-prometheus-alertmanager", "observability", 9093, "alertmanager"),
            ):
                service_ip = json.loads(host.kube("get", "service", service, "-n", namespace, "-o", "json"))["spec"]["clusterIP"]
                tunnel = Tunnel(host, service_ip, port)
                tunnels.append(tunnel)
                stack.callback(tunnel.close)
                result[label + "_url"] = tunnel.base_url
            prometheus, alertmanager, frontend = [result.pop(key + "_url") for key in ("prometheus", "alertmanager", "frontend")]
            # Connection failures are retryable only during tunnel startup, before mutation.
            def ready():
                require(all(t.process.poll() is None for t in tunnels), "private_tunnel_process_failed")
                try:
                    return http_json(prometheus, "/api/v1/rules").get("status") == "success"
                except OSError:
                    return False
            wait_until(ready, time.monotonic() + 30, label="private_tunnels")
            preflight({"prometheus": http_json(prometheus, "/api/v1/rules")}, CLUSTER)
            progress("normal_baseline")
            normal = workload(stack, directory, frontend, duration=900, profile="normal", marker=run_id)
            wait_until(lambda: normal.poll() is not None, time.monotonic() + 920, label="900_second_normal_baseline", tick=30)
            progress("baseline_validation")
            require(normal.returncode == 0, "baseline_workload_failed")
            baseline = json.loads((directory / "normal-workload.log").read_text())
            require(baseline["request_attempts"] > 0 and baseline["transport_errors"] == 0
                    and baseline["status_families"] == {"2xx": baseline["request_attempts"]}, "baseline_requests_not_healthy")
            after, pods_after = target_health(target)
            require(fingerprint(initial_pods) == fingerprint(pods_after), "baseline_pod_drift")
            require({d["metadata"]["name"]: d["spec"] for d in after} == {d["metadata"]["name"]: d["spec"] for d in deployments}, "baseline_deployment_drift")
            save(directory, "baseline", {"workload": baseline, "krca": krca_baseline(prometheus)})
            native_before = preflight({"prometheus": http_json(prometheus, "/api/v1/rules")}, CLUSTER)
            no_open_incident(control)
            original = json.loads(target.kube("get", "deployment", DEPLOYMENT, "-n", NAMESPACE, "-o", "json"))
            save(directory, "original", original)
            started = utc()
            # Mark attempted before SSH: an uncertain prepare/apply response must still restore.
            prepared = True
            deadline = time.monotonic() + 600
            progress("watchdog_prepare")
            save(directory, "watchdog", helper("prepare", {"original": original, "cluster_id": CLUSTER, "maximum_active_seconds": 600}))
            traffic = workload(stack, directory, frontend, duration=600, profile="path-weighted", marker=run_id)
            progress("fault_apply")
            save(directory, "applied", helper("apply"))
            save(directory, "injection", {"started_at": started, "preflight": native_before, "plan": plan})
            progress("fault_observation")
            def observe_fault():
                require(traffic.poll() is None, "fault_workload_stopped")
                configmap = target.kube("get", "configmap", CONFIGMAP, "-n", NAMESPACE, "--ignore-not-found", "-o", "json")
                observation = {"original_pod": old_pods[0]["metadata"]["uid"], "deployment_uid": original["metadata"]["uid"],
                    "pods": target.get("pods", NAMESPACE), "replicasets": target.get("replicasets", NAMESPACE),
                    "endpoints": target.get("endpointslices", NAMESPACE), "events": target.get("events", NAMESPACE),
                    "configmap": json.loads(configmap) if configmap else {}}
                # Retain the last input before interpreting it; private artifact only.
                save(directory, "fault-observation", observation)
                return fault_postcondition(**observation)
            fault = wait_until(observe_fault, min(deadline, time.monotonic() + 90), label="exact_FailedMount_and_endpoint_loss")
            save(directory, "fault-postcondition", fault)
            progress("native_detection")
            def detect():
                require(traffic.poll() is None, "fault_workload_stopped")
                rules = http_json(prometheus, "/api/v1/rules")
                save(directory, "detection", {"prometheus": rules, "preflight": native_before, "fault_postcondition": fault})
                rule = checked_rule(rules, CLUSTER)
                if rule["state"] != "firing":
                    return None
                alerts = http_json(alertmanager, "/api/v2/alerts")
                if not any(a.get("labels", {}).get("alertname") == ALERT_NAME for a in alerts):
                    return None
                value = capture({"prometheus": rules, "alertmanager": alerts, "not_before": started}, CLUSTER)
                save(directory, "capture", value)
                return value
            captured = wait_until(detect, min(deadline - 110, time.monotonic() + 300), label="unchanged_native_frontend_rule")
            progress("agent_result")
            def terminal():
                bundle = read_bundle(control, captured["incident_id"])
                if bundle:
                    save(directory, "bundle", bundle)
                return bundle if bundle and (bundle["incident"]["status"] == "FAILED" or bundle.get("report")) else None
            bundle = wait_until(terminal, min(deadline - 20, time.monotonic() + 90), label="native_agent_result")
            progress("downstream_attestation")
            attestation = attest_downstream({"plan_id": plan["plan_id"], "capture": captured,
                "fault_postcondition": fault, "bundle": bundle}, fault_family=MISSING_CONFIGMAP_CAUSE)
            save(directory, "attestation", attestation)
            require(attestation["cross_service_verified"], "cross_service_attestation_failed")
            result.update(status="PASSED", attestation=attestation)
        except (Exception, KeyboardInterrupt) as error:
            result.update(status="FAILED", error_type=type(error).__name__,
                          failure=failure_details(error, result["phase"]),
                          reason=str(error) if type(error) in {RuntimeError, NativeAlertError} else "verification_failed")
        finally:
            if prepared:
                try:
                    progress("recovery")
                    recovery = wait_until(lambda: (value if (value := helper("restore"))["ready"] else None),
                                          time.monotonic() + 300, label="exact_restore")
                    save(directory, "recovery", recovery)
                    # Stop only after exact restoration is verified; otherwise leave watchdog armed.
                    target.run(["sudo", "systemctl", "stop", run_id + "-restore"])
                    target_health(target)
                    recovered_rule = wait_until(lambda: (rules if checked_rule(rules := http_json(prometheus, "/api/v1/rules"), CLUSTER)["state"] == "inactive" else None),
                                                time.monotonic() + 240, label="natural_alert_resolution")
                    save(directory, "recovered-rule", preflight({"prometheus": recovered_rule}, CLUSTER))
                    if captured:
                        def resolved():
                            bundle = read_bundle(control, captured["incident_id"])
                            return bundle if bundle and bundle["incident"].get("window", {}).get("incident_end") else None
                        closed = wait_until(resolved, time.monotonic() + 100, label="resolved_webhook")
                        save(directory, "closed-bundle", closed)
                    result["cleanup_verified"] = True
                except Exception as error:
                    result.update(status="FAILED", cleanup_verified=False, cleanup_error_type=type(error).__name__,
                                  cleanup_failure=failure_details(error, "recovery"),
                                  cleanup_reason=str(error) if type(error) is RuntimeError else "cleanup_check_failed")
            progress("finished")
            save(directory, "result", result)
    print(json.dumps({k: v for k, v in result.items() if k not in {"run_id", "attestation"}}, sort_keys=True))
    return 0 if result["status"] == "PASSED" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-controlled-fault", choices=["development"])
    for domain in ("target", "control", "observability"):
        for field in ("host", "user", "key"):
            parser.add_argument(f"--{domain}-{field}", required=True)
    args = parser.parse_args(argv)
    require(args.execute and args.confirm_controlled_fault == "development", "explicit_development_authorization_required")
    addresses = [getattr(args, d + "_host") for d in ("target", "control", "observability")]
    require(len(set(addresses)) == 3, "three_independent_hosts_required")
    os.umask(0o077)
    return execute(*(Host(getattr(args, d + "_host"), getattr(args, d + "_user"), getattr(args, d + "_key"))
                     for d in ("target", "control", "observability")))


if __name__ == "__main__":
    raise SystemExit(main())
