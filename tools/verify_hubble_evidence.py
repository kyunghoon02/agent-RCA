#!/usr/bin/env python3
"""One opt-in, development-only network Evidence drill; no LLM or RCA scoring.

Uses three existing SSH/Kubernetes trust boundaries. A target-side systemd
watchdog and a local finally block independently remove this run's policy.
Artifacts contain internal identifiers and must remain in the ignored tmp/ tree.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "online-boutique"
CLUSTER = "agent-rca-chaos-eval"
KUBECTL = ["sudo", "kubectl", "--kubeconfig=/etc/kubernetes/admin.conf", "--request-timeout=15s"]
LOCK = "agent-rca-network-evidence-lock"


def utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def policy(run_id: str) -> dict:
    if not re.fullmatch(r"hubble-evidence-[0-9a-f]{12}", run_id):
        raise ValueError("invalid verification id")
    return {
        "apiVersion": "cilium.io/v2", "kind": "CiliumNetworkPolicy",
        "metadata": {"name": run_id, "namespace": NAMESPACE,
                     "labels": {"agent-rca.dev/verification-id": run_id}},
        "spec": {
            "endpointSelector": {"matchLabels": {"app": "frontend"}},
            "enableDefaultDeny": {"ingress": False, "egress": False},
            "egressDeny": [{
                "toEndpoints": [{"matchLabels": {"app": "productcatalogservice"}}],
                "toPorts": [{"ports": [{"port": "3550", "protocol": "TCP"}]}],
            }],
        },
    }


def cleanup_code(run_id: str) -> str:
    policy(run_id)
    # Re-read ownership before deleting; never touch a pre-existing policy/lock.
    return "\n".join([
        "import json, subprocess",
        f"base = {KUBECTL[1:]!r} + ['-n', {NAMESPACE!r}]",
        f"for kind, name in [('ciliumnetworkpolicy', {run_id!r}), ('configmap', {LOCK!r})]:",
        "    found = subprocess.run(base + ['get', kind, name, '--ignore-not-found', '-o', 'json'], capture_output=True, text=True, timeout=20, check=True)",
        "    if not found.stdout.strip(): continue",
        "    obj = json.loads(found.stdout)",
        f"    if obj['metadata'].get('labels', {{}}).get('agent-rca.dev/verification-id') != {run_id!r}: raise RuntimeError('ownership changed')",
        "    subprocess.run(base + ['delete', kind, name, '--wait=true', '--timeout=20s'], check=True, timeout=25)",
    ])


class Host:
    def __init__(self, address: str, user: str, key: str):
        ipaddress.IPv4Address(address)
        if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}", user):
            raise ValueError("invalid SSH user")
        self.base = ["ssh", "-i", str(Path(key).expanduser()), "-o", "IdentitiesOnly=yes",
                     "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"{user}@{address}"]

    def run(self, argv: list[str], *, data: str | None = None, timeout: int = 35) -> str:
        completed = subprocess.run(self.base + [shlex.join(argv)], input=data,
                                   text=True, capture_output=True, timeout=timeout)
        if completed.returncode:
            # Do not put raw SSH/database output or addresses in public summaries.
            raise RuntimeError(f"remote command failed ({Path(argv[0]).name}, exit {completed.returncode})")
        return completed.stdout.strip()

    def kube(self, *args: str, data: str | None = None) -> str:
        return self.run(KUBECTL + list(args), data=data)

    def get(self, resource: str, namespace: str | None = None) -> dict:
        args = ["get", resource, "-o", "json"]
        if namespace:
            args += ["-n", namespace]
        return json.loads(self.kube(*args))


def fingerprint(pods: dict) -> str:
    records = sorted([
        {"name": p["metadata"]["name"], "uid": p["metadata"]["uid"],
         "containers": [{"name": c["name"], "restarts": c["restartCount"], "ready": c["ready"]}
                        for c in p["status"].get("containerStatuses", [])]}
        for p in pods["items"] if p["status"]["phase"] != "Succeeded"
    ], key=lambda item: item["name"])
    return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()


def require_healthy(pods: dict) -> None:
    for pod in pods["items"]:
        if pod["status"]["phase"] == "Succeeded":
            continue
        if (pod["status"]["phase"] != "Running" or pod["metadata"].get("deletionTimestamp")
            or not pod["status"].get("containerStatuses")
            or not all(c["ready"] for c in pod["status"]["containerStatuses"])):
            raise ValueError("starting or recovered workloads are not healthy")


def http_probe(host: Host, base: str, path: str) -> dict:
    started = utc()
    # Python handles expected HTTP failures without hiding SSH failures.
    code = "request_url = " + repr(base + path) + "\n" + """import json, time, urllib.request, urllib.error
t = time.monotonic()
try:
    with urllib.request.urlopen(request_url, timeout=5) as response:
        response.read(1024)
        value = {'http_status': response.status}
except urllib.error.HTTPError as error:
    value = {'http_status': error.code}
except (OSError, urllib.error.URLError) as error:
    value = {'transport_error': type(error).__name__}
value['elapsed_ms'] = round((time.monotonic() - t) * 1000)
print(json.dumps(value))
"""
    return {"started_at": started, "path": path,
            **json.loads(host.run(["python3", "-c", code], timeout=20))}


def alert_payload(run_id: str, started: str, ended: str) -> list[dict]:
    policy(run_id)
    return [{"labels": {
        "alertname": "AgentRCANetworkEvidenceVerification", "cluster_id": CLUSTER,
        "namespace": NAMESPACE, "service": "frontend", "severity": "warning",
        "rca_enabled": "true", "agent_rca_enabled": "false", "verification_id": run_id,
    }, "annotations": {"summary": "Controlled connectivity Evidence verification; not a root-cause evaluation"},
        "startsAt": started, "endsAt": ended, "generatorURL": "https://prometheus.invalid/network-evidence-verification"}]


def validate_audit(result: dict, *, require_graph: bool = True) -> None:
    if (result.get("status") != "READY" or result.get("incident_status") != "ANALYZING"
        or result["agent_run_count"] != 0):
        raise ValueError("Context not ready or an unexpected LLM run exists")
    items = result["hubble"]
    matching = [item for item in items if item["facts"].get("flow_signal") == "POLICY_DENIAL_OBSERVED"]
    if not matching:
        raise ValueError("no policy denial in stored Hubble Evidence")
    for item in matching:
        if (item["subject"].get("cluster_id") != CLUSTER
            or item["subject"].get("namespace") != NAMESPACE
            or item["subject"].get("name") != "frontend"
            or item["facts"].get("policy_denied_count", 0) <= 0):
            raise ValueError("policy denial is outside the intended subject")
        if not (item["in_frozen_context"] and item["in_agent_catalog"]
                and item["tool_status"] == "SUCCEEDED" and item["tool_facts_equal"]):
            raise ValueError("Hubble Evidence did not survive Context/catalog/tool boundaries")
        if item["facts"]["retention_status"] != "UNKNOWN" or item["quality"]["completeness"] > 0.5:
            raise ValueError("Hubble coverage was overstated")
        if item["facts"].get("observation_gaps"):
            if not any(s["status"] == "PARTIAL" for s in result.get("hubble_collector_statuses", [])):
                raise ValueError("observation gaps lost their PARTIAL status")
            if not any(f.get("collector") == "hubble" for f in result.get("collector_failures", [])):
                raise ValueError("Hubble collection failure was hidden from Context")
        if require_graph:
            events = [event["record"] for event in result["graph_events"]
                      if item["evidence_id"] in event["record"]["evidence_ids"]]
            if not events or not all(event["attributes"] == item["facts"] for event in events):
                raise ValueError("Neo4j event is missing or its facts differ")


def metrics_ready(snapshot: dict) -> bool:
    if any(item.get("status") != "success" for item in snapshot.values()):
        raise ValueError("Prometheus query failed")
    up = snapshot["scrape_up"]["data"]["result"]
    if not up or any(float(item["value"][1]) != 1 for item in up):
        raise ValueError("network exporter scrape is unhealthy or absent")
    # Empty API series means missing data, not an observed zero failure rate.
    return bool(snapshot["frontend_calls"]["data"]["result"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("target-host", "control-host", "observability-host", "ssh-user", "ssh-key"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-controlled-fault", choices=["development"])
    parser.add_argument("--audit-only", metavar="RUN_ID",
                        help="Recheck an existing private run without submitting alerts or applying a fault")
    args = parser.parse_args()
    if not args.audit_only and (not args.execute or args.confirm_controlled_fault != "development"):
        parser.error("requires --execute --confirm-controlled-fault development")
    if args.audit_only and (args.execute or args.confirm_controlled_fault):
        parser.error("audit-only cannot be combined with fault authorization")
    addresses = [args.target_host, args.control_host, args.observability_host]
    if len(set(addresses)) != 3:
        parser.error("three distinct domain hosts required")
    target, control, observability = [Host(a, args.ssh_user, args.ssh_key) for a in addresses]
    os.umask(0o077)
    run_id = args.audit_only or f"hubble-evidence-{uuid4().hex[:12]}"
    policy(run_id)
    directory = ROOT / "tmp" / run_id
    if not args.audit_only:
        directory.mkdir(parents=True, exist_ok=False)
    result = {"run_id": run_id, "started_at": utc(), "status": "RUNNING",
              "scope": "frontend -> productcatalogservice:3550/TCP",
              "trigger": "synthetic Alertmanager submission", "llm_requested": False,
              "policy": policy(run_id), "probes": {}}

    def save() -> None:
        (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")

    def emit(message: str) -> None:
        print(f"{utc()} {message}", flush=True)
        save()

    audit_source = (ROOT / "tools/hubble_evidence_audit.py").read_text()

    def audit(mode="graph") -> dict:
        deployment = "incident-worker" if mode == "graph" else "incident-agent-worker"
        return json.loads(control.run(KUBECTL + ["exec", "-i", "-n", "incident-platform",
                          f"deployment/{deployment}", "--", "python", "-", run_id, mode],
                          data=audit_source, timeout=40))

    if args.audit_only:
        original = json.loads((directory / "result.json").read_text())
        followup = {"run_id": run_id, "mode": "read-only-followup", "checked_at": utc()}
        try:
            for kind, name in (("ciliumnetworkpolicy", run_id), ("configmap", LOCK)):
                if target.kube("get", kind, name, "-n", NAMESPACE, "--ignore-not-found", "-o", "name"):
                    raise ValueError("verification policy or lock still present")
            pods = target.get("pods", NAMESPACE)
            require_healthy(pods)
            require_healthy(control.get("pods", "incident-platform"))
            if fingerprint(pods) != original["initial_pods_sha256"]:
                raise ValueError("target Pod identities or restart counts changed")
            deployments = target.get("deployments", NAMESPACE)["items"]
            digest = hashlib.sha256(json.dumps([d["spec"] for d in deployments], sort_keys=True).encode()).hexdigest()
            if digest != original["deployment_specs_sha256"]:
                raise ValueError("target Deployment specifications changed")
            followup["graph_audit"] = audit()
            followup["agent_audit"] = audit("agent")
            validate_audit(followup["graph_audit"])
            validate_audit(followup["agent_audit"], require_graph=False)
            for check in ("graph_audit", "agent_audit"):
                if followup[check]["context_sha256"] != original["audit"]["context_sha256"]:
                    raise ValueError("Frozen Context changed after recovery")
            followup.update(status="PASSED", cleanup_verified=True, pods_unchanged=True, deployments_unchanged=True)
        except Exception as error:
            followup.update(status="FAILED", error_type=type(error).__name__, error=str(error)[:200])
        (directory / "read-only-followup.json").write_text(json.dumps(followup, indent=2) + "\n")
        print(json.dumps({key: value for key, value in followup.items() if not key.endswith("audit")}))
        return 0 if followup["status"] == "PASSED" else 1

    def url(host, service, port):
        address = host.get(f"service/{service}", "observability")["spec"]["clusterIP"]
        ipaddress.IPv4Address(address)
        return f"http://{address}:{port}"

    def metrics() -> dict:
        queries = {
            "frontend_calls": f'sum by (span_name,status_code) (agent_rca_calls_total{{cluster_id="{CLUSTER}",service_name="frontend",span_kind="SPAN_KIND_SERVER"}})',
            "drops": f'sum by (reason) (hubble_drop_total{{cluster_id="{CLUSTER}"}})',
            "scrape_up": f'up{{cluster_id="{CLUSTER}",job=~".*(cilium|hubble).*"}}',
        }
        return {key: json.loads(observability.run(["curl", "--fail", "--silent", "--show-error", "--max-time", "10", "--get", prometheus + "/api/v1/query", "--data-urlencode", "query=" + query])) for key, query in queries.items()}

    def submit(ended: str) -> None:
        observability.run(["curl", "--fail", "--silent", "--show-error", "--max-time", "10", "-H", "Content-Type: application/json", "--data-binary", "@-", alertmanager + "/api/v2/alerts"], data=json.dumps(alert_payload(run_id, result["fault_started_at"], ended)))

    owned = False
    armed = False
    submitted = False
    timer = run_id + "-rollback"
    try:
        for host, expected in zip((target, control, observability), (
            "agent-rca-dev-chaos-eval-01", "agent-rca-dev-node-01", "agent-rca-dev-observability-01",
        )):
            nodes = host.get("nodes")["items"]
            if len(nodes) != 1 or nodes[0]["metadata"]["name"] != expected:
                raise ValueError("wrong reference cluster")
        initial = target.get("pods", NAMESPACE)
        require_healthy(initial)
        require_healthy(control.get("pods", "incident-platform"))
        result["initial_pods_sha256"] = fingerprint(initial)
        for resource in ("networkpolicies", "ciliumnetworkpolicies", "stresschaos", "podchaos", "networkchaos", "httpchaos", "dnschaos", "iochaos", "timechaos"):
            if target.get(resource, NAMESPACE)["items"]:
                raise ValueError("active fault or network policy exists")
        if target.get("ciliumclusterwidenetworkpolicies")["items"]:
            raise ValueError("clusterwide policy exists")
        cms = target.get("configmaps", NAMESPACE)["items"]
        if any(c["metadata"]["name"].startswith("agent-rca-") and c["metadata"]["name"].endswith("-lock") for c in cms):
            raise ValueError("another controlled verification is active")
        deployments = target.get("deployments", NAMESPACE)["items"]
        if any(d["metadata"].get("annotations", {}).get("agent-rca.dev/controlled-fault-id") for d in deployments):
            raise ValueError("active controlled-fault marker")
        for name in ("frontend", "productcatalogservice"):
            matches = [p for p in initial["items"] if p["metadata"].get("labels", {}).get("app") == name]
            if len(matches) != 1 or not matches[0]["metadata"]["name"].startswith(name + "-"):
                raise ValueError("unexpected endpoint selector")
        result["deployment_specs_sha256"] = hashlib.sha256(json.dumps([d["spec"] for d in deployments], sort_keys=True).encode()).hexdigest()
        front = target.get("service/frontend", NAMESPACE)["spec"]
        base = f"http://{front['clusterIP']}:80"
        prometheus = url(observability, "monitoring-kube-prometheus-prometheus", 9090)
        alertmanager = url(observability, "monitoring-kube-prometheus-alertmanager", 9093)
        observability.run(["curl", "--fail", "--silent", "--max-time", "5", prometheus + "/-/ready"])
        target.kube("create", "--dry-run=server", "-f", "-", data=json.dumps(policy(run_id)))
        emit("Preflight passed; collecting healthy baseline (no policy applied).")
        path = "/product/0PUK6V6EV0"
        result["probes"]["baseline"] = [http_probe(target, base, path) for _ in range(3)]
        if any(p.get("http_status") != 200 for p in result["probes"]["baseline"]):
            raise ValueError("baseline API not healthy")
        if http_probe(target, base, "/_healthz").get("http_status") != 200:
            raise ValueError("baseline frontend health endpoint unavailable")
        result["metrics_baseline"] = metrics()
        for _ in range(20):
            if metrics_ready(result["metrics_baseline"]):
                break
            time.sleep(3)
            result["metrics_baseline"] = metrics()
        if not metrics_ready(result["metrics_baseline"]):
            raise ValueError("baseline API metrics did not arrive; no fault applied")

        lock = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
            "name": LOCK, "namespace": NAMESPACE,
            "labels": {"agent-rca.dev/verification-id": run_id}}}
        target.kube("create", "-f", "-", data=json.dumps(lock))
        owned = True
        target.run(["sudo", "systemd-run", "--unit=" + timer, "--on-active=150s",
                    "--timer-property=AccuracySec=1s", "--property=Type=oneshot",
                    "--property=Restart=on-failure", "--property=RestartSec=5s",
                    "/usr/bin/python3", "-c", cleanup_code(run_id)])
        armed = True
        if target.run(["sudo", "systemctl", "is-active", timer + ".timer"]) != "active":
            raise ValueError("rollback timer not armed")
        target.kube("create", "-f", "-", data=json.dumps(policy(run_id)))
        result["fault_started_at"] = utc()
        deadline = time.monotonic() + 95
        emit("Scoped policy active; target-side rollback armed for 150 seconds.")
        time.sleep(4)
        result["probes"]["fault"] = [http_probe(target, base, path) for _ in range(3)]
        result["probes"]["control"] = [http_probe(target, base, "/_healthz")]
        if all(p.get("http_status") == 200 for p in result["probes"]["fault"]):
            raise ValueError("no API impact observed; refusing a success claim")
        if result["probes"]["control"][0].get("http_status") != 200:
            raise ValueError("frontend health endpoint affected")
        # Keep the fault's Incident independent of the taxonomy and paid Agent.
        submitted = True
        submit((datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
        emit("Evaluation Alert submitted with agent_rca_enabled=false; waiting for frozen Evidence.")
        while time.monotonic() < deadline:
            result["audit"] = audit()
            if result["audit"].get("status") == "READY":
                break
            if result["audit"].get("status") in {"FAILED", "AUDIT_ERROR"}:
                raise ValueError("Incident collection/localization failed")
            time.sleep(3)
        result["metrics_fault"] = metrics()
        metrics_ready(result["metrics_fault"])
        emit("Ending fault window; restoring policy before further analysis.")
    except Exception as error:
        result["status"] = "FAILED"
        result["error_type"] = type(error).__name__
        result["error"] = str(error)[:200]
    finally:
        if owned:
            try:
                target.run(["sudo", "python3", "-c", cleanup_code(run_id)], timeout=60)
                if target.kube("get", "ciliumnetworkpolicy", run_id, "-n", NAMESPACE, "--ignore-not-found", "-o", "name"):
                    raise ValueError("policy still exists")
                result["fault_ended_at"] = utc()
                result["cleanup_verified"] = True
                if armed:
                    target.run(["sudo", "systemctl", "stop", timer + ".timer"])
            except Exception as error:
                result["cleanup_verified"] = False
                result["cleanup_error_type"] = type(error).__name__
                result["status"] = "CLEANUP_REQUIRED"
        if submitted:
            try:
                submit(utc())
                result["alert_resolved_submission"] = True
            except Exception as error:
                result["alert_resolve_error_type"] = type(error).__name__
                if result["status"] != "CLEANUP_REQUIRED":
                    result["status"] = "FAILED"
        save()

    if result.get("cleanup_verified"):
        try:
            emit("Policy removed; checking application recovery and stored Evidence.")
            time.sleep(5)
            result["probes"]["recovery"] = [http_probe(target, base, path) for _ in range(3)]
            if any(p.get("http_status") != 200 for p in result["probes"]["recovery"]):
                raise ValueError("API recovery failed")
            recovered = target.get("pods", NAMESPACE)
            require_healthy(recovered)
            result["recovered_pods_sha256"] = fingerprint(recovered)
            if result["initial_pods_sha256"] != result["recovered_pods_sha256"]:
                raise ValueError("target Pod identities or restart counts changed")
            recovered_deployments = target.get("deployments", NAMESPACE)["items"]
            result["recovered_deployment_specs_sha256"] = hashlib.sha256(
                json.dumps([d["spec"] for d in recovered_deployments], sort_keys=True).encode()
            ).hexdigest()
            if result["deployment_specs_sha256"] != result["recovered_deployment_specs_sha256"]:
                raise ValueError("target Deployment specifications changed")
            require_healthy(control.get("pods", "incident-platform"))
            result["metrics_recovery"] = metrics()
            metrics_ready(result["metrics_recovery"])
            validate_audit(result.get("audit", {}))
            result["agent_audit"] = audit("agent")
            validate_audit(result["agent_audit"], require_graph=False)
            if result["audit"]["context_sha256"] != result["agent_audit"]["context_sha256"]:
                raise ValueError("Frozen Context changed after recovery")
            if result["status"] == "RUNNING":
                result["status"] = "PASSED"
        except Exception as error:
            result["status"] = "FAILED"
            result["verification_error"] = str(error)[:200]
    result["completed_at"] = utc()
    emit("Verification " + result["status"] + "; private artifact: " + str(directory / "result.json"))
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
