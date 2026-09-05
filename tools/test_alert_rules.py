#!/usr/bin/env python3
"""Run the real PromQL evaluator on isolated samples; never contact a cluster."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CLUSTER = "alert-rule-test"
ALERT = "OnlineBoutiqueRecentOOMRestart"
MANIFESTS = (
    ROOT / "platform/observability/remote-online-boutique-alerts.yaml",
    ROOT / "platform/observability/remote-workload-alerts.yaml",
)


def load_groups() -> list:
    return [
        group
        for path in MANIFESTS
        for group in yaml.safe_load(
            path.read_text().replace("FAULT_TARGET_CLUSTER_ID", CLUSTER)
        )["spec"]["groups"]
    ]


def metric(name: str, labels: dict, values: str) -> dict:
    selector = ",".join(
        f"{key}={json.dumps(value)}" for key, value in sorted(labels.items())
    )
    return {"series": f"{name}{{{selector}}}", "values": values}


def oom_samples(
    *, pod="checkout-pod", uid="pod-uid", service="checkoutservice"
) -> list:
    scope = {"cluster_id": CLUSTER, "namespace": "online-boutique"}
    target = {**scope, "pod": pod, "uid": uid, "container": "server"}
    return [
        metric(
            "kube_pod_container_status_last_terminated_reason",
            {**target, "reason": "OOMKilled"},
            "_ _ 1+0x24",
        ),
        metric("kube_pod_container_status_restarts_total", target, "_ _ 1+0x24"),
        metric(
            "kube_pod_container_status_last_terminated_timestamp", target, "_ _ 30+0x24"
        ),
        metric(
            "kube_pod_owner",
            {
                **scope,
                "pod": pod,
                "uid": uid,
                "owner_kind": "ReplicaSet",
                "owner_name": "checkout-rs",
                "owner_is_controller": "true",
            },
            "1+0x26",
        ),
        metric(
            "kube_replicaset_owner",
            {
                **scope,
                "replicaset": "checkout-rs",
                "owner_kind": "Deployment",
                "owner_name": service,
                "owner_is_controller": "true",
            },
            "1+0x26",
        ),
    ]


def expected_alert(service: str = "checkoutservice") -> dict:
    definition = next(
        rule
        for group in load_groups()
        for rule in group["rules"]
        if rule.get("alert") == ALERT
    )
    return {
        "exp_labels": {
            "cluster_id": CLUSTER,
            "namespace": "online-boutique",
            "service": service,
            **definition["labels"],
        },
        "exp_annotations": definition["annotations"],
    }


def case(
    name: str,
    samples: list,
    expected: bool = False,
    *,
    at="30s",
    service="checkoutservice",
) -> dict:
    return {
        "name": name,
        "interval": "15s",
        "input_series": samples,
        "alert_rule_test": [
            {
                "eval_time": at,
                "alertname": ALERT,
                "exp_alerts": [expected_alert(service)] if expected else [],
            }
        ],
    }


def test_cases() -> list:
    tests = [
        case(
            "first sample already restarted needs no counter baseline or traffic",
            oom_samples(),
            True,
        )
    ]
    tests.append(
        case(
            "event remains visible inside its five-minute window",
            oom_samples(),
            True,
            at="5m",
        )
    )
    tests.append(
        case(
            "old OOM reason expires at exactly five minutes", oom_samples(), at="5m30s"
        )
    )
    tests.append(case("normal baseline has no telemetry event", []))
    tests.append(
        case(
            "mapping comes from Deployment owner not Pod name",
            oom_samples(service="paymentservice"),
            True,
            service="paymentservice",
        )
    )
    for name, index, old, new in (
        ("ordinary restart is not OOM", 0, 'reason="OOMKilled"', 'reason="Error"'),
        (
            "same Pod name with different reason UID cannot join",
            0,
            'uid="pod-uid"',
            'uid="old-uid"',
        ),
        ("restart UID cannot cross generations", 1, 'uid="pod-uid"', 'uid="old-uid"'),
        (
            "termination timestamp UID cannot cross generations",
            2,
            'uid="pod-uid"',
            'uid="old-uid"',
        ),
        ("owner UID cannot cross generations", 3, 'uid="pod-uid"', 'uid="old-uid"'),
        (
            "Pod must have a controller ReplicaSet",
            3,
            'owner_is_controller="true"',
            'owner_is_controller="false"',
        ),
        (
            "ReplicaSet must have a controller Deployment",
            4,
            'owner_kind="Deployment"',
            'owner_kind="StatefulSet"',
        ),
        (
            "unregistered service is excluded",
            4,
            'owner_name="checkoutservice"',
            'owner_name="unregistered"',
        ),
    ):
        samples = oom_samples()
        samples[index]["series"] = samples[index]["series"].replace(old, new)
        tests.append(case(name, samples))
    for field, old, new in (
        ("cluster", CLUSTER, "different-cluster"),
        ("namespace", "online-boutique", "different-namespace"),
    ):
        samples = oom_samples()
        for sample in samples:
            sample["series"] = sample["series"].replace(old, new)
        tests.append(case(f"other {field} is excluded", samples))
    for name, index, values in (
        ("zero reason is not OOM", 0, "0+0x26"),
        ("no restart is excluded", 1, "0+0x26"),
        ("future termination time is excluded", 2, "900+0x26"),
    ):
        samples = oom_samples()
        samples[index]["values"] = values
        tests.append(case(name, samples))
    for index, name in enumerate(
        ("reason", "restart", "timestamp", "Pod owner", "ReplicaSet owner")
    ):
        samples = oom_samples()
        samples.pop(index)
        tests.append(case(f"missing {name} fails closed", samples))
    samples = oom_samples()
    for sample in copy.deepcopy(samples):
        sample["series"] = sample["series"].replace("{", '{instance="second-exporter",')
        samples.append(sample)
    tests.append(
        case(
            "duplicate exporter samples do not duplicate alerts or break joins",
            samples,
            True,
        )
    )
    tests.append(
        case(
            "two OOM Pods coalesce into one Service alert",
            oom_samples() + oom_samples(pod="second-pod", uid="second-uid")[:4],
            True,
        )
    )
    samples = oom_samples()
    for index in (0, 1, 2):
        samples[index]["values"] = "_ _ 1 stale"
    tests.append(case("stale event series clears the alert", samples, at="1m"))
    scope = {
        "cluster_id": CLUSTER,
        "namespace": "online-boutique",
        "service_name": "frontend",
        "span_name": "POST /cart/checkout",
    }
    for sustained in (False, True):
        tests.append(
            {
                "name": (
                    "unchanged sustained service-impact rule"
                    if sustained
                    else "short error burst does not bypass two-minute service-impact hold"
                ),
                "interval": "15s",
                "input_series": [
                    metric(
                        "agent_rca_api_failure_rate",
                        scope,
                        "0.08+0x12" if sustained else "0.08+0x3 0+0x9",
                    ),
                    metric("agent_rca_api_request_rate", scope, "2+0x12"),
                ],
                "alert_rule_test": [
                    {
                        "eval_time": "2m",
                        "alertname": "OnlineBoutiqueCheckoutHighFailureRate",
                        "exp_alerts": (
                            [
                                {
                                    "exp_labels": {
                                        "cluster_id": CLUSTER,
                                        "namespace": "online-boutique",
                                        "service_name": "frontend",
                                        "service": "frontend",
                                        "severity": "critical",
                                        "rca_enabled": "true",
                                        "agent_rca_enabled": "true",
                                        "krca_profile": "checkout-full",
                                    },
                                    "exp_annotations": {
                                        "summary": "Online Boutique checkout failure rate is above 5 percent",
                                        "description": "The remote checkout route has sustained failures with active traffic.",
                                    },
                                }
                            ]
                            if sustained
                            else []
                        ),
                    }
                ],
            }
        )
    return tests


def main() -> None:
    versions = yaml.safe_load((ROOT / "platform/versions.yaml").read_text())
    image = versions["observability"]["kube_prometheus_stack"]["promtool_image"]
    cases = test_cases()
    with tempfile.TemporaryDirectory(prefix="agent-rca-promtool-") as temporary:
        directory = Path(temporary)
        (directory / "rules.yml").write_text(yaml.safe_dump({"groups": load_groups()}))
        (directory / "tests.yml").write_text(
            yaml.safe_dump(
                {
                    "rule_files": ["rules.yml"],
                    "evaluation_interval": "15s",
                    "tests": cases,
                }
            )
        )
        command = [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,size=128m,mode=1777",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--volume",
            f"{directory}:/rules:ro",
            "--workdir",
            "/rules",
            "--entrypoint",
            "/bin/promtool",
            image,
        ]
        subprocess.run(
            command + ["check", "rules", "rules.yml"], check=True, timeout=120
        )
        subprocess.run(
            command + ["test", "rules", "tests.yml"], check=True, timeout=120
        )
    print(
        f"Validated {len(cases)} isolated PromQL scenarios; no live alerts or faults submitted."
    )


if __name__ == "__main__":
    main()
