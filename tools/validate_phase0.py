#!/usr/bin/env python3
"""Validate Phase 0 contracts, fixtures, manifests, and frozen decisions."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "contracts" / "schemas"
EXAMPLE_DIR = ROOT / "contracts" / "examples"

EXPECTED_NAMESPACES = {
    "online-boutique",
    "incident-platform",
    "observability",
    "graph-rca",
    "load-test",
}
FORBIDDEN_RBAC_VERBS = {
    "*",
    "bind",
    "create",
    "delete",
    "deletecollection",
    "escalate",
    "impersonate",
    "patch",
    "update",
}
FORBIDDEN_RBAC_RESOURCES = {
    "*",
    "pods/attach",
    "pods/exec",
    "pods/portforward",
    "secrets",
    "serviceaccounts/token",
}


class ValidationFailure(RuntimeError):
    pass


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_yaml_documents(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [document for document in yaml.safe_load_all(handle) if document]


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def schema_registry() -> tuple[dict[str, dict[str, Any]], Registry]:
    schemas: dict[str, dict[str, Any]] = {}
    resources: list[tuple[str, Resource[Any]]] = []

    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        schema = load_json(path)
        Draft202012Validator.check_schema(schema)
        schema_id = schema.get("$id")
        if not schema_id:
            raise ValidationFailure(f"{path} is missing $id")
        schemas[path.name] = schema
        resources.append((schema_id, Resource.from_contents(schema)))

    return schemas, Registry().with_resources(resources)


def validate_instance(
    schema: dict[str, Any],
    instance: Any,
    registry: Registry,
    label: str,
) -> None:
    validator = Draft202012Validator(
        schema,
        registry=registry,
        format_checker=FormatChecker(),
    )
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
    if errors:
        formatted = "\n".join(
            f"- {label}:{'/'.join(str(part) for part in error.path)}: {error.message}"
            for error in errors
        )
        raise ValidationFailure(formatted)


def validate_contracts() -> dict[str, Any]:
    schemas, registry = schema_registry()
    examples = {
        "incident": load_json(EXAMPLE_DIR / "incident.example.json"),
        "evidence": load_json(EXAMPLE_DIR / "evidence-item.example.json"),
        "context": load_json(EXAMPLE_DIR / "context-package.example.json"),
        "report": load_json(EXAMPLE_DIR / "rca-report.example.json"),
        "graph_records": load_json(EXAMPLE_DIR / "graph-records.example.json"),
        "investigation_scope": load_json(
            EXAMPLE_DIR / "investigation-scope.example.json"
        ),
    }

    validate_instance(
        schemas["incident.schema.json"],
        examples["incident"],
        registry,
        "incident.example.json",
    )
    validate_instance(
        schemas["evidence-item.schema.json"],
        examples["evidence"],
        registry,
        "evidence-item.example.json",
    )
    validate_instance(
        schemas["context-package.schema.json"],
        examples["context"],
        registry,
        "context-package.example.json",
    )
    validate_instance(
        schemas["rca-report.schema.json"],
        examples["report"],
        registry,
        "rca-report.example.json",
    )
    validate_instance(
        schemas["investigation-scope.schema.json"],
        examples["investigation_scope"],
        registry,
        "investigation-scope.example.json",
    )

    if not isinstance(examples["graph_records"], list) or not examples["graph_records"]:
        raise ValidationFailure("graph-records.example.json must be a non-empty array")
    for index, record in enumerate(examples["graph_records"]):
        validate_instance(
            schemas["graph-record.schema.json"],
            record,
            registry,
            f"graph-records.example.json[{index}]",
        )

    validate_cross_contract_references(examples)
    return examples


def report_evidence_ids(report: dict[str, Any]) -> set[str]:
    cited: set[str] = set()
    root_cause = report.get("root_cause")
    if root_cause:
        cited.update(root_cause["supporting_evidence_ids"])
    for hypothesis in report["hypotheses"]:
        cited.update(hypothesis["supporting_evidence_ids"])
        cited.update(hypothesis["contradicting_evidence_ids"])
    return cited


def validate_cross_contract_references(examples: dict[str, Any]) -> None:
    incident = examples["incident"]
    evidence = examples["evidence"]
    context = examples["context"]
    report = examples["report"]

    incident_ids = {
        incident["incident_id"],
        evidence["incident_id"],
        context["incident_id"],
        report["incident_id"],
    }
    if len(incident_ids) != 1:
        raise ValidationFailure(f"fixture incident IDs disagree: {sorted(incident_ids)}")
    if report["context_id"] != context["context_id"]:
        raise ValidationFailure("RCA report references a different Context Package")

    context_evidence = set(context["evidence_ids"])
    if evidence["evidence_id"] not in context_evidence:
        raise ValidationFailure("Evidence fixture is not included in Context Package")
    unknown_report_evidence = report_evidence_ids(report) - context_evidence
    if unknown_report_evidence:
        raise ValidationFailure(
            f"RCA report cites evidence outside Context Package: {sorted(unknown_report_evidence)}"
        )

    for path in context["state_paths"]:
        unknown_path_evidence = set(path["evidence_ids"]) - context_evidence
        if unknown_path_evidence:
            raise ValidationFailure(
                f"State path cites evidence outside Context Package: {sorted(unknown_path_evidence)}"
            )

    graph_records = examples["graph_records"]
    graph_evidence = {
        evidence_id
        for record in graph_records
        for evidence_id in record["evidence_ids"]
    }
    unknown_graph_evidence = graph_evidence - context_evidence
    if unknown_graph_evidence:
        raise ValidationFailure(
            "StateGraph cites evidence outside Context Package: "
            f"{sorted(unknown_graph_evidence)}"
        )
    entity_ids = {
        record["entity_id"]
        for record in graph_records
        if record["record_type"] == "entity"
    }
    for record in graph_records:
        references = []
        if record["record_type"] in {"snapshot_interval", "event_aggregate"}:
            references.append(record["entity_id"])
        elif record["record_type"] == "relation_interval":
            references.extend(
                [record["source_entity_id"], record["destination_entity_id"]]
            )
        unknown_entities = set(references) - entity_ids
        if unknown_entities:
            raise ValidationFailure(
                f"StateGraph record references unknown Entity: {sorted(unknown_entities)}"
            )

    window = incident["window"]
    if parse_time(window["baseline_start"]) >= parse_time(window["incident_start"]):
        raise ValidationFailure("Incident baseline must start before incident_start")
    if window["incident_end"] and parse_time(window["incident_start"]) > parse_time(
        window["incident_end"]
    ):
        raise ValidationFailure("incident_end precedes incident_start")


def validate_namespaces() -> None:
    documents = load_yaml_documents(ROOT / "platform" / "namespaces" / "namespaces.yml")
    names = {
        document["metadata"]["name"]
        for document in documents
        if document.get("kind") == "Namespace"
    }
    if names != EXPECTED_NAMESPACES:
        raise ValidationFailure(
            f"namespace boundary mismatch: expected {sorted(EXPECTED_NAMESPACES)}, got {sorted(names)}"
        )
    legacy = {"platform", "ai-serving", "aiops"} & names
    if legacy:
        raise ValidationFailure(f"legacy namespaces remain: {sorted(legacy)}")
def validate_rbac_documents(documents: list[dict[str, Any]]) -> None:
    service_accounts = {
        (document["metadata"].get("namespace"), document["metadata"]["name"])
        for document in documents
        if document.get("kind") == "ServiceAccount"
    }
    expected_sa = ("incident-platform", "incident-platform-reader")
    if expected_sa not in service_accounts:
        raise ValidationFailure("incident-platform-reader ServiceAccount is missing")

    rule_documents = [
        document
        for document in documents
        if document.get("kind") in {"Role", "ClusterRole"}
    ]
    if not rule_documents:
        raise ValidationFailure("read-only Role/ClusterRole is missing")

    for document in rule_documents:
        for rule in document.get("rules", []):
            verbs = set(rule.get("verbs", []))
            resources = set(rule.get("resources", []))
            forbidden_verbs = verbs & FORBIDDEN_RBAC_VERBS
            forbidden_resources = resources & FORBIDDEN_RBAC_RESOURCES
            if forbidden_verbs:
                raise ValidationFailure(
                    f"{document['kind']}/{document['metadata']['name']} has forbidden verbs: "
                    f"{sorted(forbidden_verbs)}"
                )
            if forbidden_resources:
                raise ValidationFailure(
                    f"{document['kind']}/{document['metadata']['name']} has forbidden resources: "
                    f"{sorted(forbidden_resources)}"
                )
            if verbs - {"get", "list", "watch"}:
                raise ValidationFailure(
                    f"{document['kind']}/{document['metadata']['name']} is not read-only: "
                    f"{sorted(verbs)}"
                )


def validate_rbac() -> None:
    documents = load_yaml_documents(
        ROOT / "platform" / "rbac" / "incident-platform-readonly.yaml"
    )
    validate_rbac_documents(documents)

    intentionally_broken = copy.deepcopy(documents)
    role = next(document for document in intentionally_broken if document["kind"] == "Role")
    role["rules"][0]["verbs"].append("delete")
    try:
        validate_rbac_documents(intentionally_broken)
    except ValidationFailure:
        pass
    else:
        raise ValidationFailure("negative RBAC self-test failed to reject delete permission")


def validate_versions_and_manifests() -> None:
    versions = load_yaml_documents(ROOT / "platform" / "versions.yaml")[0]
    expected_execution_target = {
        "cloud": "gcp",
        "location": "asia-northeast3",
    }
    if versions.get("execution_target") != expected_execution_target:
        raise ValidationFailure("platform version boundary must target GCP")
    expected_kubernetes_boundary = {
        "service": "self-managed",
        "distribution": "upstream",
        "bootstrap_tool": "kubeadm",
        "cluster_availability": "single-node-dev",
        "container_runtime": "containerd",
        "dataplane": "cilium",
        "network_observability": "hubble",
        "cloud_identity": "dedicated-vm-service-account",
    }
    if versions.get("kubernetes") != expected_kubernetes_boundary:
        raise ValidationFailure(
            "Kubernetes boundary must remain self-managed kubeadm with Cilium/Hubble"
        )
    if versions["terraform"].get("provider") != "hashicorp/google":
        raise ValidationFailure("Terraform provider must be hashicorp/google")

    expected_runtime_versions = {
        "host": {
            "distribution": "ubuntu",
            "release": "24.04",
            "architecture": "amd64",
        },
        "ansible": {"core_version": "2.20.7"},
        "containerd": {"package_version": "2.2.1-0ubuntu1~24.04.3"},
        "kubernetes_release": {
            "minor": "v1.36",
            "version": "v1.36.4",
            "deb_version": "1.36.4-1.1",
        },
        "cilium": {"chart_version": "1.20.1", "cli_version": "v0.19.7"},
        "hubble": {"cli_version": "v1.19.4"},
    }
    for boundary, expected in expected_runtime_versions.items():
        if versions.get(boundary) != expected:
            raise ValidationFailure(
                f"platform runtime version boundary drifted for {boundary}"
            )
    expected_chaos_evaluation = {
        "kubernetes_release": {
            "minor": "v1.35",
            "version": "v1.35.8",
            "deb_version": "1.35.8-1.1",
        },
        "chaos_mesh": {
            "namespace": "chaos-mesh",
            "target_namespace": "online-boutique",
            "chart_ref": "chaos-mesh/chaos-mesh",
            "chart_repository": "https://charts.chaos-mesh.org",
            "chart_version": "2.8.4",
            "cluster_scoped": False,
            "container_runtime": "containerd",
            "runtime_socket": "/run/containerd/containerd.sock",
        },
    }
    if versions.get("chaos_evaluation") != expected_chaos_evaluation:
        raise ValidationFailure(
            "Chaos evaluation Kubernetes and Chaos Mesh boundary drifted"
        )
    if versions.get("helm", {}).get("version") != "v3.21.4":
        raise ValidationFailure("platform Helm version boundary drifted")

    expected_observability = {
        "namespace": "observability",
        "local_path_provisioner": {
            "chart_ref": "oci://ghcr.io/rancher/local-path-provisioner/charts/local-path-provisioner",
            "chart_version": "0.0.36",
            "chart_digest": "sha256:ae31255346657674a47b99619a16fa12bf38e1e6ef51e2df12b0825fdb1fa80c",
            "storage_class": "agent-rca-local",
            "reclaim_policy": "Retain",
        },
        "kube_prometheus_stack": {
            "chart_ref": "oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack",
            "chart_version": "88.5.3",
            "retention": "7d",
            "retention_size": "12GiB",
        },
        "loki": {
            "chart_ref": "oci://ghcr.io/grafana-community/helm-charts/loki",
            "chart_version": "18.11.0",
            "deployment_mode": "Monolithic",
            "retention": "72h",
        },
        "tempo": {
            "manifest": "platform/observability/tempo",
            "app_version": "2.9.0",
            "image_digest": "sha256:65a5789759435f1ef696f1953258b9bbdb18eb571d5ce711ff812d2e128288a4",
            "deployment_mode": "monolithic",
            "retention": "72h",
            "storage": "5Gi",
        },
        "alloy": {
            "chart_ref": "grafana/alloy",
            "chart_repository": "https://grafana.github.io/helm-charts",
            "chart_version": "1.11.1",
            "controller_type": "daemonset",
        },
    }
    if versions.get("observability") != expected_observability:
        raise ValidationFailure("observability version and storage boundary drifted")

    ansible_versions = load_yaml_documents(
        ROOT / "automation" / "ansible" / "group_vars" / "all.yml"
    )[0]
    expected_ansible_versions = {
        "containerd_deb_version": versions["containerd"]["package_version"],
        "kubernetes_minor": versions["kubernetes_release"]["minor"],
        "kubernetes_version": versions["kubernetes_release"]["version"],
        "kubernetes_deb_version": versions["kubernetes_release"]["deb_version"],
        "helm_version": versions["helm"]["version"],
        "cilium_chart_version": versions["cilium"]["chart_version"],
        "cilium_cli_version": versions["cilium"]["cli_version"],
        "hubble_cli_version": versions["hubble"]["cli_version"],
        "observability_namespace": versions["observability"]["namespace"],
        "local_path_chart_ref": versions["observability"]["local_path_provisioner"]["chart_ref"],
        "local_path_chart_version": versions["observability"]["local_path_provisioner"]["chart_version"],
        "local_path_storage_class": versions["observability"]["local_path_provisioner"]["storage_class"],
        "kube_prometheus_chart_ref": versions["observability"]["kube_prometheus_stack"]["chart_ref"],
        "kube_prometheus_chart_version": versions["observability"]["kube_prometheus_stack"]["chart_version"],
        "loki_chart_ref": versions["observability"]["loki"]["chart_ref"],
        "loki_chart_version": versions["observability"]["loki"]["chart_version"],
        "alloy_chart_ref": versions["observability"]["alloy"]["chart_ref"],
        "alloy_chart_repository_url": versions["observability"]["alloy"]["chart_repository"],
        "alloy_chart_version": versions["observability"]["alloy"]["chart_version"],
        "chaos_mesh_namespace": versions["chaos_evaluation"]["chaos_mesh"]["namespace"],
        "chaos_mesh_target_namespace": versions["chaos_evaluation"]["chaos_mesh"]["target_namespace"],
        "chaos_mesh_chart_repository_url": versions["chaos_evaluation"]["chaos_mesh"]["chart_repository"],
        "chaos_mesh_chart_ref": versions["chaos_evaluation"]["chaos_mesh"]["chart_ref"],
        "chaos_mesh_chart_version": versions["chaos_evaluation"]["chaos_mesh"]["chart_version"],
    }
    for key, expected in expected_ansible_versions.items():
        if ansible_versions.get(key) != expected:
            raise ValidationFailure(
                f"Ansible runtime pin {key} must match platform/versions.yaml"
            )
    checksum_keys = (
        "kubernetes_package_key_sha256",
        "helm_linux_amd64_sha256",
        "cilium_cli_linux_amd64_sha256",
        "hubble_cli_linux_amd64_sha256",
    )
    for key in checksum_keys:
        if not re.fullmatch(r"[0-9a-f]{64}", ansible_versions.get(key, "")):
            raise ValidationFailure(f"Ansible download checksum is invalid for {key}")

    chaos_inventory = load_yaml_documents(
        ROOT
        / "automation"
        / "ansible"
        / "inventories"
        / "chaos-eval.example.yml"
    )[0]
    chaos_inventory_hosts = (
        chaos_inventory.get("all", {})
        .get("children", {})
        .get("kubernetes_nodes", {})
        .get("hosts", {})
    )
    chaos_inventory_host = chaos_inventory_hosts.get(
        "agent_rca_chaos_eval_node", {}
    )
    if chaos_inventory_host.get("deployment_profile_vars_file") != (
        "../group_vars/chaos-eval.yml"
    ):
        raise ValidationFailure(
            "Chaos evaluation inventory must load the reviewed evaluation profile"
        )
    if chaos_inventory_host.get("observability_remote_node_address_inventory") != (
        "OBSERVABILITY_PRIVATE_IP"
    ):
        raise ValidationFailure(
            "Chaos evaluation inventory must declare the private telemetry receiver input"
        )
    if "agent_rca_chaos_eval_node" not in (
        chaos_inventory.get("all", {})
        .get("children", {})
        .get("fault_target", {})
        .get("hosts", {})
    ):
        raise ValidationFailure("Chaos evaluation inventory must declare fault_target")
    chaos_profile = load_yaml_documents(
        ROOT / "automation" / "ansible" / "group_vars" / "chaos-eval.yml"
    )[0]
    if chaos_profile != {
        "cluster_name": "agent-rca-chaos-eval",
        "kubernetes_minor": expected_chaos_evaluation["kubernetes_release"]["minor"],
        "kubernetes_version": expected_chaos_evaluation["kubernetes_release"]["version"],
        "kubernetes_deb_version": expected_chaos_evaluation["kubernetes_release"]["deb_version"],
        "observability_domain_mode": "forwarder",
        "hubble_relay_service_type": "NodePort",
    }:
        raise ValidationFailure(
            "Chaos evaluation profile must select Kubernetes 1.35 and telemetry forwarding"
        )

    chaos_values = load_yaml_documents(
        ROOT / "platform" / "chaos-mesh" / "values.yaml"
    )[0]
    chaos_controller = chaos_values.get("controllerManager", {})
    chaos_daemon = chaos_values.get("chaosDaemon", {})
    chaos_dashboard = chaos_values.get("dashboard", {})
    if (
        chaos_values.get("clusterScoped") is not False
        or chaos_controller.get("targetNamespace") != "online-boutique"
        or chaos_controller.get("enabledControllers")
        != [
            "awschaos-records",
            "azurechaos-records",
            "dnschaos-records",
            "httpchaos-records",
            "iochaos-records",
            "kernelchaos-records",
            "networkchaos-records",
            "podchaos-records",
            "gcpchaos-records",
            "stresschaos-records",
            "jvmchaos-records",
            "timechaos-records",
            "physicalmachinechaos-records",
            "blockchaos-records",
            "stresschaos-initFinalizers",
            "stresschaos-desiredphase",
            "stresschaos-condition",
            "stresschaos-cleanFinalizers",
        ]
        or chaos_controller.get("enabledWebhooks") != ["stresschaos"]
        or chaos_controller.get("allowHostNetworkTesting") is not False
        or chaos_controller.get("replicaCount") != 1
        or chaos_controller.get("leaderElection", {}).get("enabled") is not False
        or chaos_daemon.get("runtime") != "containerd"
        or chaos_daemon.get("socketPath") != "/run/containerd/containerd.sock"
        or chaos_daemon.get("hostNetwork") is not False
        or chaos_daemon.get("mtls", {}).get("enabled") is not True
        or chaos_dashboard.get("securityMode") is not True
        or chaos_dashboard.get("service", {}).get("type") != "ClusterIP"
        or chaos_dashboard.get("persistentVolume", {}).get("enabled") is not False
    ):
        raise ValidationFailure("Chaos Mesh namespace or runtime safety boundary drifted")

    terraform_variables = (
        ROOT / "infra" / "terraform" / "environments" / "dev" / "variables.tf"
    ).read_text(encoding="utf-8")
    terraform_main = (
        ROOT / "infra" / "terraform" / "environments" / "dev" / "main.tf"
    ).read_text(encoding="utf-8")
    required_terraform_tokens = {
        'variable "enable_chaos_evaluation_node"',
        'variable "enable_observability_node"',
        'default     = false',
        'resource "google_compute_instance" "chaos_evaluation"',
        'resource "google_compute_instance" "observability"',
        'count = var.enable_chaos_evaluation_node ? 1 : 0',
        'count = var.enable_observability_node ? 1 : 0',
        'purpose            = "chaos-evaluation"',
        'purpose = "observability"',
        'resource "google_compute_firewall" "observability_ingest"',
        'resource "google_compute_firewall" "observability_query"',
        'resource "google_compute_firewall" "rca_control_webhook"',
        'resource "google_compute_firewall" "fault_target_kubernetes_api"',
        'resource "google_compute_firewall" "fault_target_hubble_relay"',
        'ports    = ["31234"]',
    }
    terraform_contract = terraform_variables + terraform_main
    missing_terraform_tokens = sorted(
        token for token in required_terraform_tokens if token not in terraform_contract
    )
    if missing_terraform_tokens:
        raise ValidationFailure(
            "Chaos evaluation Terraform opt-in boundary is incomplete: "
            f"{missing_terraform_tokens}"
        )

    chaos_role = (
        ROOT
        / "automation"
        / "ansible"
        / "roles"
        / "chaos_mesh_stack"
        / "tasks"
        / "main.yml"
    ).read_text(encoding="utf-8")
    required_chaos_role_tokens = {
        "Require the reviewed Chaos Mesh evaluation Kubernetes minor",
        "kubernetes_minor == 'v1.35'",
        "chaos_mesh_target_namespace == online_boutique_namespace",
        "--atomic --wait",
    }
    missing_chaos_role_tokens = sorted(
        token for token in required_chaos_role_tokens if token not in chaos_role
    )
    if missing_chaos_role_tokens:
        raise ValidationFailure(
            "Chaos Mesh deployment safety gate is incomplete: "
            f"{missing_chaos_role_tokens}"
        )

    online_boutique_role = (
        ROOT
        / "automation"
        / "ansible"
        / "roles"
        / "online_boutique"
        / "tasks"
        / "main.yml"
    ).read_text(encoding="utf-8")
    required_online_boutique_identity_tokens = {
        "behavior: merge",
        "| replace('agent-rca-dev', cluster_name)",
    }
    missing_online_boutique_identity_tokens = sorted(
        token
        for token in required_online_boutique_identity_tokens
        if token not in online_boutique_role
    )
    if missing_online_boutique_identity_tokens:
        raise ValidationFailure(
            "Online Boutique target-cluster telemetry identity overlay is incomplete: "
            f"{missing_online_boutique_identity_tokens}"
        )

    incident_platform_role = (
        ROOT
        / "automation"
        / "ansible"
        / "roles"
        / "incident_platform_stack"
        / "tasks"
        / "main.yml"
    ).read_text(encoding="utf-8")
    required_incident_identity_tokens = {
        "incident-worker-krca-config",
        "namespace: {{ incident_platform_namespace }}",
        "disableNameSuffixHash: true",
        "agent-rca.io/krca-config-sha256",
        "incident_platform_target_cluster_id",
        "INCIDENT_WORKER_CLUSTER_ID",
        "STATEGRAPH_CLUSTER_ID",
        "KUBERNETES_API_SERVER",
        "PROMETHEUS_BASE_URL",
        "LOKI_BASE_URL",
        "HUBBLE_SERVER",
        "remote-target-kubernetes",
    }
    missing_incident_identity_tokens = sorted(
        token
        for token in required_incident_identity_tokens
        if token not in incident_platform_role
    )
    if missing_incident_identity_tokens:
        raise ValidationFailure(
            "Incident Platform target-cluster identity overlay is incomplete: "
            f"{missing_incident_identity_tokens}"
        )

    scope = load_yaml_documents(ROOT / "config" / "project-scope.yaml")[0]
    expected_scope_target = {
        **expected_execution_target,
        "provisioning_status": "gcp-foundation-applied-kubernetes-observability-verified",
        "compute_service": "compute-engine",
        "kubernetes_service": "self-managed",
        "kubernetes_distribution": "upstream",
        "bootstrap_tool": "kubeadm",
        "cluster_availability": "single-node-dev",
        "container_runtime": "containerd",
        "dataplane": "cilium",
        "network_observability": "hubble",
        "cloud_identity": "dedicated-vm-service-account",
    }
    if scope.get("execution_target") != expected_scope_target:
        raise ValidationFailure("project scope and platform GCP boundaries disagree")

    readiness = load_yaml_documents(ROOT / "config" / "gcp-readiness.yaml")[0]
    expected_readiness_decision = {
        **expected_execution_target,
        "status": "architecture-confirmed",
        "confirmed_at": "2026-08-20",
        "compute_service": "compute-engine",
        "kubernetes_service": "self-managed",
        "kubernetes_distribution": "upstream",
        "bootstrap_tool": "kubeadm",
        "cluster_availability": "single-node-dev",
        "container_runtime": "containerd",
        "dataplane": "cilium",
        "network_observability": "hubble",
        "cloud_identity": "dedicated-vm-service-account",
        "remote_state_backend": "gcs",
    }
    if readiness.get("decision") != expected_readiness_decision:
        raise ValidationFailure("GCP readiness decision does not match the target")

    required_design_capabilities = {
        "terraform_google_provider",
        "compute_engine_vm",
        "kubeadm_bootstrap",
        "cilium_hubble",
        "dedicated_vm_service_account",
        "gcs_remote_state",
    }
    recorded_design = readiness.get("design_capabilities", {})
    missing_design = required_design_capabilities - set(recorded_design)
    if missing_design:
        raise ValidationFailure(
            f"GCP design capability matrix is incomplete: {sorted(missing_design)}"
        )
    unresolved_design = {
        capability
        for capability in required_design_capabilities
        if recorded_design[capability].get("status") != "verified"
    }
    if unresolved_design:
        raise ValidationFailure(
            f"GCP environment design capabilities are unresolved: {sorted(unresolved_design)}"
        )

    design_gate = readiness.get("gates", {}).get("environment_design", {})
    if design_gate.get("status") != "ready":
        raise ValidationFailure("GCP environment design gate must be ready")
    if set(design_gate.get("required_capabilities", [])) != required_design_capabilities:
        raise ValidationFailure(
            "GCP environment design gate does not cover every required capability"
        )

    required_runtime_inputs = {
        "target_project",
        "billing",
        "location_and_quota",
        "local_authentication",
        "required_apis",
        "remote_state_bucket",
    }
    recorded_runtime_inputs = readiness.get("runtime_inputs", {})
    missing_runtime_inputs = required_runtime_inputs - set(recorded_runtime_inputs)
    if missing_runtime_inputs:
        raise ValidationFailure(
            f"GCP runtime input matrix is incomplete: {sorted(missing_runtime_inputs)}"
        )
    runtime_gate = readiness.get("gates", {}).get("terraform_plan_apply", {})
    if set(runtime_gate.get("required_runtime_inputs", [])) != required_runtime_inputs:
        raise ValidationFailure("GCP runtime gate does not cover every required input")
    unresolved_runtime = {
        item
        for item in required_runtime_inputs
        if recorded_runtime_inputs[item].get("status") != "verified"
    }
    if unresolved_runtime and runtime_gate.get("status") != "blocked":
        raise ValidationFailure(
            "Terraform plan/apply gate must stay blocked while runtime inputs are unresolved"
        )

    boutique = load_yaml_documents(
        ROOT / "platform" / "online-boutique" / "kustomization.yaml"
    )[0]
    expected_remote = versions["online_boutique"]["remote_kustomize_base"]
    if boutique.get("resources") != [
        expected_remote,
        "otel-collector.yaml",
        "otel-rca-rules.yaml",
    ]:
        raise ValidationFailure("Online Boutique remote base is not pinned to the recorded SHA")
    if boutique.get("namespace") != "online-boutique":
        raise ValidationFailure("Online Boutique namespace is not fixed")

    expected_redis_digest = versions["online_boutique"]["redis_image_digest"]
    if boutique.get("images") != [
        {
            "name": "redis",
            "newName": "docker.io/library/redis",
            "digest": expected_redis_digest,
        }
    ]:
        raise ValidationFailure("Online Boutique Redis image is not digest-pinned")

    deleted_targets = {
        (patch["target"]["kind"], patch["target"]["name"])
        for patch in boutique.get("patches", [])
        if "target" in patch and "$patch: delete" in patch.get("patch", "")
    }
    expected_deletions = {
        ("Service", "frontend-external"),
        ("Deployment", "loadgenerator"),
        ("ServiceAccount", "loadgenerator"),
    }
    if deleted_targets != expected_deletions:
        raise ValidationFailure(
            f"Online Boutique deletion patches mismatch: {sorted(deleted_targets)}"
        )

    expected_instrumented_services = set(
        versions["online_boutique"]["directly_instrumented_services"]
    )
    required_application_services = {
        "adservice",
        "cartservice",
        "checkoutservice",
        "currencyservice",
        "emailservice",
        "frontend",
        "paymentservice",
        "productcatalogservice",
        "recommendationservice",
        "shippingservice",
    }
    if expected_instrumented_services != required_application_services:
        raise ValidationFailure(
            "Online Boutique direct server-span coverage is not complete"
        )
    instrumentation_paths = {
        patch["path"]
        for patch in boutique.get("patches", [])
        if "path" in patch
    }
    expected_instrumentation_paths = {
        f"patches/{service}-otel.yaml"
        for service in expected_instrumented_services
    }
    if instrumentation_paths != expected_instrumentation_paths:
        raise ValidationFailure("Online Boutique direct instrumentation patches drifted")
    for service in expected_instrumented_services:
        patch = load_yaml_documents(
            ROOT / "platform" / "online-boutique" / "patches" / f"{service}-otel.yaml"
        )[0]
        env = {
            item["name"]: item.get("value")
            for item in patch["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        required_env = {
            "COLLECTOR_SERVICE_ADDR": "opentelemetrycollector:4317",
            "OTEL_SERVICE_NAME": service,
            "ENABLE_TRACING": "1",
        }
        if any(env.get(name) != value for name, value in required_env.items()):
            raise ValidationFailure(
                f"Online Boutique OTel environment drifted for {service}"
            )

    custom_image_digests = versions["online_boutique"].get("custom_image_digests", {})
    custom_services = {"adservice", "cartservice", "shippingservice"}
    if set(custom_image_digests) != custom_services or any(
        re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        for digest in custom_image_digests.values()
    ):
        raise ValidationFailure("Online Boutique custom image digests drifted")
    for service in custom_services:
        patch = load_yaml_documents(
            ROOT / "platform" / "online-boutique" / "patches" / f"{service}-otel.yaml"
        )[0]
        pod_spec = patch["spec"]["template"]["spec"]
        strategy = patch["spec"].get("strategy", {}).get("rollingUpdate", {})
        if pod_spec.get("imagePullSecrets") != [{"name": "artifact-registry"}]:
            raise ValidationFailure(
                f"Online Boutique private image pull contract drifted for {service}"
            )
        if strategy != {"maxSurge": 0, "maxUnavailable": 1}:
            raise ValidationFailure(
                f"Online Boutique single-node rollout contract drifted for {service}"
            )
        source_patch = (
            ROOT
            / "platform"
            / "online-boutique"
            / "source-patches"
            / f"{service}-otel.patch"
        )
        if not source_patch.is_file() or f"a/src/{service}/" not in source_patch.read_text(
            encoding="utf-8"
        ):
            raise ValidationFailure(
                f"Online Boutique source instrumentation patch is missing for {service}"
            )

    source_patch_directory = (
        ROOT / "platform" / "online-boutique" / "source-patches"
    )
    fingerprint_input = "".join(
        sorted(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  ./{path.name}\n"
            for path in source_patch_directory.glob("*.patch")
        )
    ).encode()
    expected_custom_tag = (
        f"{versions['online_boutique']['release_tag']}-otel-"
        f"{hashlib.sha256(fingerprint_input).hexdigest()[:12]}"
    )
    if versions["online_boutique"].get("custom_image_tag") != expected_custom_tag:
        raise ValidationFailure("Online Boutique custom image tag fingerprint drifted")

    build_config = load_yaml_documents(
        ROOT / "platform" / "online-boutique" / "cloudbuild-otel.yaml"
    )[0]
    if len(build_config.get("steps", [])) != 3 or len(build_config.get("images", [])) != 3:
        raise ValidationFailure("Online Boutique Cloud Build image set drifted")
    build_script = ROOT / "tools" / "build_online_boutique_otel_images.sh"
    build_script_text = build_script.read_text(encoding="utf-8")
    if (
        versions["online_boutique"]["commit_sha"] not in build_script_text
        or "git -C \"$worktree/upstream\" apply --check" not in build_script_text
    ):
        raise ValidationFailure("Online Boutique exact-commit build gate drifted")

    collector_docs = load_yaml_documents(
        ROOT / "platform" / "online-boutique" / "otel-collector.yaml"
    )
    collector_by_kind = {document["kind"]: document for document in collector_docs}
    collector_container = collector_by_kind["Deployment"]["spec"]["template"]["spec"][
        "containers"
    ][0]
    expected_collector_image = (
        "otel/opentelemetry-collector-contrib:"
        f"{versions['online_boutique']['otel_collector_version']}@"
        f"{versions['online_boutique']['otel_collector_digest']}"
    )
    if collector_container.get("image") != expected_collector_image:
        raise ValidationFailure("Online Boutique OTel Collector image is not pinned")
    if collector_by_kind["Service"].get("spec", {}).get("type") != "ClusterIP":
        raise ValidationFailure("Online Boutique OTel Collector must remain internal")
    if (
        collector_by_kind["ServiceMonitor"].get("metadata", {}).get("labels", {}).get(
            "release"
        )
        != "monitoring"
    ):
        raise ValidationFailure("Online Boutique OTel ServiceMonitor is not selected")

    collector_config = load_yaml_documents(
        ROOT / "platform" / "online-boutique" / "otel-collector-config.yaml"
    )[0]
    connectors = collector_config.get("connectors", {})
    if (
        connectors.get("span_metrics", {}).get("aggregation_cardinality_limit")
        != 5000
        or connectors.get("span_metrics", {}).get("metrics_expiration") != "5m"
        or "service_graph" not in connectors
    ):
        raise ValidationFailure("Online Boutique span-derived metric boundary drifted")
    pipelines = collector_config.get("service", {}).get("pipelines", {})
    processors = collector_config.get("processors", {})
    route_statements = "\n".join(
        processors.get("transform/frontend_routes", {}).get("trace_statements", [])
    )
    health_conditions = "\n".join(
        processors.get("filter/krca_health", {}).get("trace_conditions", [])
    )
    expected_frontend_operations = {
        "GET /",
        "GET /product/{id}",
        "GET /cart",
        "POST /cart",
        "POST /cart/empty",
        "POST /cart/checkout",
    }
    if (
        not all(operation in route_statements for operation in expected_frontend_operations)
        or 'span.attributes["url.path"]' not in route_statements
        or "/_healthz" not in health_conditions
    ):
        raise ValidationFailure("Online Boutique frontend route normalization drifted")
    if (
        pipelines.get("traces", {}).get("exporters") != ["otlp/tempo"]
        or set(pipelines.get("traces/derived", {}).get("exporters", []))
        != {"span_metrics", "service_graph"}
        or set(pipelines.get("traces/derived", {}).get("processors", []))
        != {
            "memory_limiter",
            "resource",
            "transform/frontend_routes",
            "filter/krca_health",
            "batch",
        }
    ):
        raise ValidationFailure("Online Boutique trace pipeline isolation is incomplete")
    if set(pipelines.get("metrics/derived", {}).get("receivers", [])) != {
        "span_metrics",
        "service_graph",
    }:
        raise ValidationFailure("Online Boutique derived metric pipeline is incomplete")

    rca_rule = load_yaml_documents(
        ROOT / "platform" / "online-boutique" / "otel-rca-rules.yaml"
    )[0]
    if (
        rca_rule.get("kind") != "PrometheusRule"
        or rca_rule.get("metadata", {}).get("labels", {}).get("release")
        != "monitoring"
    ):
        raise ValidationFailure("Online Boutique KRCA recording rules are not selected")
    groups = rca_rule.get("spec", {}).get("groups", [])
    if len(groups) != 1 or groups[0].get("interval") != "15s":
        raise ValidationFailure("Online Boutique KRCA recording rule interval drifted")
    expected_recording_rules = {
        "agent_rca_api_request_rate",
        "agent_rca_api_failure_rate",
        "agent_rca_api_latency_p95_milliseconds",
        "agent_rca_api_latency_baseline_p95_milliseconds",
        "agent_rca_pod_memory_working_set_ratio",
        "agent_rca_pod_restart_count_delta",
    }
    actual_recording_rules = {
        rule["record"] for rule in groups[0].get("rules", []) if "record" in rule
    }
    if actual_recording_rules != expected_recording_rules:
        raise ValidationFailure("Online Boutique KRCA recording rule set drifted")
    alerting_rules = [
        rule for rule in groups[0].get("rules", []) if "alert" in rule
    ]
    expected_alerts = {
        "OnlineBoutiqueHomeHighFailureRate": ("browse-home", "GET /"),
        "OnlineBoutiqueProductDetailHighFailureRate": (
            "product-detail",
            "GET /product/{id}",
        ),
        "OnlineBoutiqueCartReadHighFailureRate": ("cart-read", "GET /cart"),
        "OnlineBoutiqueCartAddHighFailureRate": ("cart-add", "POST /cart"),
        "OnlineBoutiqueCartEmptyHighFailureRate": (
            "cart-empty",
            "POST /cart/empty",
        ),
        "OnlineBoutiqueCheckoutHighFailureRate": (
            "checkout-full",
            "POST /cart/checkout",
        ),
    }
    if {rule.get("alert") for rule in alerting_rules} != set(expected_alerts):
        raise ValidationFailure("Online Boutique RCA route alert set drifted")
    for opt_in_alert in alerting_rules:
        profile, operation = expected_alerts[opt_in_alert["alert"]]
        if (
            opt_in_alert.get("for") != "2m"
            or opt_in_alert.get("labels")
            != {
                "namespace": "online-boutique",
                "service": "frontend",
                "severity": "critical",
                "rca_enabled": "true",
                "agent_rca_enabled": "true",
                "krca_profile": profile,
            }
            or 'service_name="frontend"' not in opt_in_alert.get("expr", "")
            or f'span_name="{operation}"' not in opt_in_alert.get("expr", "")
            or "> 0.05" not in opt_in_alert.get("expr", "")
            or "> 0.1" not in opt_in_alert.get("expr", "")
        ):
            raise ValidationFailure("Online Boutique RCA opt-in alert boundary drifted")

    if scope["target"]["release_tag"] != versions["online_boutique"]["release_tag"]:
        raise ValidationFailure("project scope and version pin disagree on release tag")
    if scope["target"]["commit_sha"] != versions["online_boutique"]["commit_sha"]:
        raise ValidationFailure("project scope and version pin disagree on commit SHA")

    validate_observability_values()


def validate_krca_runtime_config() -> None:
    schemas, registry = schema_registry()
    runtime = load_yaml_documents(ROOT / "config" / "online-boutique-krca.yaml")[0]
    validate_instance(
        schemas["krca-runtime-config.schema.json"],
        runtime,
        registry,
        "online-boutique-krca.yaml",
    )

    labels = runtime["prometheus"]["labels"]
    if labels != {
        "namespace": "namespace",
        "service": "service_name",
        "operation": "span_name",
    }:
        raise ValidationFailure("Online Boutique live Prometheus labels drifted")
    expected_queries = {
        "failure_rate": "agent_rca_api_failure_rate{{scope}}",
        "latency": "agent_rca_api_latency_p95_milliseconds{{scope}} >= 0",
        "qps": "agent_rca_api_request_rate{{scope}}",
        "latency_baseline": (
            "agent_rca_api_latency_baseline_p95_milliseconds{{scope}} >= 0"
        ),
    }
    if runtime["prometheus"]["queries"] != expected_queries:
        raise ValidationFailure("Online Boutique live KRCA query allowlist drifted")
    expected_profiles = {
        "browse-home",
        "product-detail",
        "cart-read",
        "cart-add",
        "cart-empty",
        "checkout-full",
    }
    profile_ids = {profile["profile_id"] for profile in runtime["profiles"]}
    if profile_ids != expected_profiles:
        raise ValidationFailure("Online Boutique live KRCA profiles drifted")
    if sum(len(profile["dependencies"]) for profile in runtime["profiles"]) != 33:
        raise ValidationFailure("Online Boutique live KRCA edge coverage drifted")


def validate_stategraph_manifest() -> None:
    versions = load_yaml_documents(ROOT / "platform" / "versions.yaml")[0]
    expected_stategraph = {
        "namespace": "graph-rca",
        "neo4j": {
            "edition": "community",
            "version": "5.26.29",
            "image_digest": (
                "sha256:89d577f2e49606de76441eca8cf7a0fe88e594cbaac4d2a3d86c6e59676e2b1e"
            ),
            "database": "neo4j",
            "storage": "5Gi",
        },
    }
    if versions.get("stategraph") != expected_stategraph:
        raise ValidationFailure("StateGraph version and storage boundary drifted")

    kustomization = load_yaml_documents(
        ROOT / "platform" / "stategraph" / "kustomization.yaml"
    )[0]
    if kustomization.get("resources") != ["neo4j.yaml"]:
        raise ValidationFailure("StateGraph Kustomize resource set drifted")

    documents = load_yaml_documents(ROOT / "platform" / "stategraph" / "neo4j.yaml")
    if any(document.get("kind") == "Secret" for document in documents):
        raise ValidationFailure("Neo4j credentials must not be committed as a Secret")
    service_account = next(
        document for document in documents if document.get("kind") == "ServiceAccount"
    )
    if service_account.get("automountServiceAccountToken") is not False:
        raise ValidationFailure("Neo4j ServiceAccount token automount must stay disabled")

    services = [
        document for document in documents if document.get("kind") == "Service"
    ]
    if {service["metadata"]["name"] for service in services} != {
        "neo4j",
        "neo4j-headless",
    }:
        raise ValidationFailure("Neo4j private Service set drifted")
    for service in services:
        ports = service.get("spec", {}).get("ports", [])
        if (
            service.get("spec", {}).get("type") != "ClusterIP"
            or len(ports) != 1
            or ports[0].get("name") != "bolt"
            or ports[0].get("port") != 7687
        ):
            raise ValidationFailure("Neo4j must expose only internal Bolt")

    statefulset = next(
        document for document in documents if document.get("kind") == "StatefulSet"
    )
    pod_spec = statefulset["spec"]["template"]["spec"]
    if (
        statefulset["spec"].get("replicas") != 1
        or pod_spec.get("automountServiceAccountToken") is not False
        or pod_spec.get("enableServiceLinks") is not False
    ):
        raise ValidationFailure("Neo4j single-node Pod safety boundary drifted")
    container = pod_spec["containers"][0]
    expected_image = (
        "docker.io/library/neo4j:5.26.29-community@"
        f"{expected_stategraph['neo4j']['image_digest']}"
    )
    if container.get("image") != expected_image:
        raise ValidationFailure("Neo4j Community image is not digest-pinned")
    env = {item["name"]: item for item in container.get("env", [])}
    if env.get("NEO4J_AUTH", {}).get("valueFrom", {}).get("secretKeyRef") != {
        "name": "neo4j-auth",
        "key": "auth",
    }:
        raise ValidationFailure("Neo4j authentication must come from the runtime Secret")
    if (
        env.get("NEO4J_server_http_enabled", {}).get("value") != "false"
        or env.get("NEO4J_server_https_enabled", {}).get("value") != "false"
    ):
        raise ValidationFailure("Neo4j HTTP interfaces must stay disabled")
    claim = statefulset["spec"]["volumeClaimTemplates"][0]["spec"]
    if (
        claim.get("storageClassName") != "agent-rca-local"
        or claim.get("accessModes") != ["ReadWriteOnce"]
        or claim.get("resources", {}).get("requests", {}).get("storage") != "5Gi"
    ):
        raise ValidationFailure("Neo4j PVC boundary drifted")

    rbac = load_yaml_documents(
        ROOT / "platform" / "rbac" / "incident-platform-readonly.yaml"
    )
    reader = next(
        document
        for document in rbac
        if document.get("kind") == "ServiceAccount"
        and document["metadata"]["name"] == "incident-platform-reader"
    )
    if reader.get("automountServiceAccountToken") is not False:
        raise ValidationFailure(
            "Incident Platform reader token automount must stay disabled"
        )
    readable_resources = {
        resource
        for document in rbac
        if document.get("kind") in {"Role", "ClusterRole"}
        for rule in document.get("rules", [])
        for resource in rule.get("resources", [])
    }
    required_inventory_resources = {
        "services",
        "deployments",
        "replicasets",
        "pods",
        "endpointslices",
        "nodes",
    }
    if not required_inventory_resources.issubset(readable_resources):
        raise ValidationFailure("StateGraph inventory read-only RBAC is incomplete")


def validate_incident_platform_manifest() -> None:
    versions = load_yaml_documents(ROOT / "platform" / "versions.yaml")[0]
    expected_runtime = {
        "namespace": "incident-platform",
        "artifact_repository_id": "agent-rca-dev-workloads",
        "postgresql": {
            "version": 17.6,
            "image_digest": (
                "sha256:f3bd19c606e442c3d7bdfa8002e03fe260a1023351e0ea4598032022b68dd6e3"
            ),
            "database": "agent_rca",
            "storage": "5Gi",
        },
        "reconciler": {
            "schedule": "*/5 * * * *",
            "concurrency_policy": "Forbid",
            "image_tag": "runtime-8a4689d56f2f",
            "image_digest": (
                "sha256:e9e761576536d1c2737f769cc07eab7ffd9345e7d010781fbfb271e54e6b2899"
            ),
        },
        "viewer_frontend": {
            "node_version": "22.17.1",
            "image_tag": "viewer-8577a41a69be",
            "image_digest": (
                "sha256:f034ea3dd5a5b9afdb8d189ff260f4e39c637d4e5542b57534b72b2442630659"
            ),
            "service_type": "ClusterIP",
            "port": 3100,
        },
        "webhook": {
            "server": "gunicorn",
            "server_version": "26.0.0",
            "max_body_bytes": 1048576,
            "max_alerts_per_request": 100,
            "alert_matcher": "rca_enabled=true",
        },
        "worker": {
            "poll_interval_seconds": 2,
            "lease_seconds": 120,
            "max_attempts": 3,
            "provider_timeout_seconds": 20,
            "max_evidence_items": 32,
            "localization_max_candidates": 10,
            "localization_max_entities": 40,
            "localization_max_depth": 4,
        },
        "agent_worker": {
            "enabled": True,
            "model": "gpt-5.6-luna",
            "poll_interval_seconds": 2,
            "lease_seconds": 180,
            "max_attempts": 3,
            "eligibility_label": "agent_rca_enabled",
            "activated_at": "2026-08-27T06:30:00Z",
            "min_claim_interval_seconds": 60,
            "circuit_failure_threshold": 3,
            "circuit_cooldown_seconds": 300,
            "metrics_port": 9090,
            "max_turns": 6,
            "max_llm_calls": 6,
            "max_tool_calls": 12,
            "max_evidence_candidates": 8,
            "max_output_tokens": 2000,
            "max_wall_time_ms": 60000,
        },
    }
    if versions.get("incident_platform") != expected_runtime:
        raise ValidationFailure(
            "Incident Platform runtime version or storage boundary drifted"
        )

    directory = ROOT / "platform" / "incident-platform"
    kustomization = load_yaml_documents(directory / "kustomization.yaml")[0]
    if kustomization.get("resources") != [
        "postgresql.yaml",
        "incident-webhook.yaml",
        "incident-worker.yaml",
        "incident-viewer.yaml",
        "alertmanager-routing.yaml",
        "stategraph-reconciler.yaml",
    ]:
        raise ValidationFailure("Incident Platform Kustomize resource set drifted")

    documents = load_yaml_documents(directory / "postgresql.yaml")
    if any(document.get("kind") == "Secret" for document in documents):
        raise ValidationFailure("PostgreSQL credentials must not be committed")
    service_account = next(
        document for document in documents if document.get("kind") == "ServiceAccount"
    )
    if service_account.get("automountServiceAccountToken") is not False:
        raise ValidationFailure("PostgreSQL ServiceAccount token must stay disabled")
    services = [
        document for document in documents if document.get("kind") == "Service"
    ]
    if {service["metadata"]["name"] for service in services} != {
        "postgresql",
        "postgresql-headless",
    }:
        raise ValidationFailure("PostgreSQL private Service set drifted")
    for service in services:
        ports = service.get("spec", {}).get("ports", [])
        if (
            service.get("spec", {}).get("type") != "ClusterIP"
            or len(ports) != 1
            or ports[0].get("port") != 5432
        ):
            raise ValidationFailure("PostgreSQL must expose only internal port 5432")

    statefulset = next(
        document for document in documents if document.get("kind") == "StatefulSet"
    )
    pod_spec = statefulset["spec"]["template"]["spec"]
    if (
        statefulset["spec"].get("replicas") != 1
        or pod_spec.get("automountServiceAccountToken") is not False
        or pod_spec.get("enableServiceLinks") is not False
    ):
        raise ValidationFailure("PostgreSQL single-node Pod safety boundary drifted")
    container = pod_spec["containers"][0]
    expected_postgresql_image = (
        "docker.io/library/postgres:17.6-bookworm@"
        f"{expected_runtime['postgresql']['image_digest']}"
    )
    if container.get("image") != expected_postgresql_image:
        raise ValidationFailure("PostgreSQL image is not digest-pinned")
    env = {item["name"]: item for item in container.get("env", [])}
    for env_name, secret_key in (
        ("POSTGRES_DB", "database"),
        ("POSTGRES_USER", "username"),
        ("POSTGRES_PASSWORD", "password"),
    ):
        if env.get(env_name, {}).get("valueFrom", {}).get("secretKeyRef") != {
            "name": "postgresql-auth",
            "key": secret_key,
        }:
            raise ValidationFailure("PostgreSQL auth must come from the runtime Secret")
    claim = statefulset["spec"]["volumeClaimTemplates"][0]["spec"]
    if (
        claim.get("storageClassName") != "agent-rca-local"
        or claim.get("accessModes") != ["ReadWriteOnce"]
        or claim.get("resources", {}).get("requests", {}).get("storage") != "5Gi"
    ):
        raise ValidationFailure("PostgreSQL PVC boundary drifted")
    network_policy = next(
        document for document in documents if document.get("kind") == "NetworkPolicy"
    )
    ingress = network_policy.get("spec", {}).get("ingress", [])
    if (
        network_policy.get("spec", {}).get("podSelector", {}).get("matchLabels", {}).get(
            "app.kubernetes.io/name"
        )
        != "postgresql"
        or ingress[0]["ports"] != [{"protocol": "TCP", "port": 5432}]
    ):
        raise ValidationFailure("PostgreSQL ingress boundary drifted")
    allowed_postgresql_clients = {
        source.get("podSelector", {}).get("matchLabels", {}).get(
            "app.kubernetes.io/name"
        )
        for source in ingress[0].get("from", [])
    }
    if allowed_postgresql_clients != {
        "stategraph-reconciler",
        "incident-webhook",
        "incident-worker",
        "incident-agent-worker",
        "incident-viewer",
    }:
        raise ValidationFailure("PostgreSQL client allowlist drifted")

    webhook_documents = load_yaml_documents(directory / "incident-webhook.yaml")
    if any(document.get("kind") == "Secret" for document in webhook_documents):
        raise ValidationFailure("Incident webhook credentials must not be committed")
    webhook_service_account = next(
        document
        for document in webhook_documents
        if document.get("kind") == "ServiceAccount"
    )
    if webhook_service_account.get("automountServiceAccountToken") is not False:
        raise ValidationFailure("Incident webhook ServiceAccount token must stay disabled")
    webhook_service = next(
        document for document in webhook_documents if document.get("kind") == "Service"
    )
    if (
        webhook_service.get("spec", {}).get("type") != "ClusterIP"
        or webhook_service.get("spec", {}).get("ports") != [
            {"name": "http", "port": 8080, "targetPort": "http"}
        ]
    ):
        raise ValidationFailure("Incident webhook must expose only internal port 8080")
    webhook_deployment = next(
        document
        for document in webhook_documents
        if document.get("kind") == "Deployment"
    )
    webhook_pod_spec = webhook_deployment["spec"]["template"]["spec"]
    webhook_container = webhook_pod_spec["containers"][0]
    if (
        webhook_deployment["spec"].get("replicas") != 1
        or webhook_pod_spec.get("serviceAccountName") != "incident-webhook"
        or webhook_pod_spec.get("automountServiceAccountToken") is not False
        or webhook_container.get("image")
        != "agent-rca-runtime@sha256:" + "0" * 64
        or webhook_container.get("command") != ["gunicorn"]
        or "tools.run_incident_receiver:application"
        not in webhook_container.get("args", [])
        or webhook_container.get("resources", {})
        .get("requests", {})
        .get("cpu")
        != "0"
        or not webhook_container.get("securityContext", {}).get(
            "readOnlyRootFilesystem"
        )
    ):
        raise ValidationFailure("Incident webhook runtime boundary drifted")
    webhook_env = {
        item["name"]: item for item in webhook_container.get("env", [])
    }
    if (
        webhook_env.get("WEBHOOK_BEARER_TOKEN", {})
        .get("valueFrom", {})
        .get("secretKeyRef")
        != {"name": "incident-webhook-auth", "key": "token"}
        or webhook_env.get("POSTGRES_PASSWORD", {})
        .get("valueFrom", {})
        .get("secretKeyRef")
        != {"name": "postgresql-auth", "key": "password"}
        or webhook_env.get("WEBHOOK_MAX_BODY_BYTES", {}).get("value")
        != "1048576"
        or webhook_env.get("WEBHOOK_MAX_ALERTS_PER_REQUEST", {}).get("value")
        != "100"
    ):
        raise ValidationFailure("Incident webhook Secret or request bounds drifted")
    webhook_network_policy = next(
        document
        for document in webhook_documents
        if document.get("kind") == "NetworkPolicy"
    )
    webhook_ingress = webhook_network_policy["spec"]["ingress"]
    if (
        webhook_network_policy["spec"]["podSelector"]["matchLabels"].get(
            "app.kubernetes.io/name"
        )
        != "incident-webhook"
        or webhook_ingress[0]["from"] != [
            {
                "namespaceSelector": {
                    "matchLabels": {
                        "kubernetes.io/metadata.name": "observability"
                    }
                }
            }
        ]
        or webhook_ingress[0]["ports"] != [{"protocol": "TCP", "port": 8080}]
    ):
        raise ValidationFailure("Incident webhook ingress boundary drifted")

    worker_documents = load_yaml_documents(directory / "incident-worker.yaml")
    if any(document.get("kind") in {"Service", "Secret"} for document in worker_documents):
        raise ValidationFailure("Incident worker must not expose a Service or Secret")
    worker_deployment = next(
        document for document in worker_documents if document.get("kind") == "Deployment"
    )
    worker_pod_spec = worker_deployment["spec"]["template"]["spec"]
    worker_container = worker_pod_spec["containers"][0]
    worker_env = {item["name"]: item for item in worker_container.get("env", [])}
    if (
        worker_deployment["spec"].get("replicas") != 1
        or worker_pod_spec.get("serviceAccountName") != "incident-platform-reader"
        or worker_pod_spec.get("automountServiceAccountToken") is not True
        or worker_container.get("image") != "agent-rca-runtime@sha256:" + "0" * 64
        or worker_container.get("args") != ["/app/tools/run_incident_worker.py"]
        or worker_container.get("resources", {}).get("requests")
        != {"cpu": "100m", "memory": "192Mi"}
        or not worker_container.get("securityContext", {}).get(
            "readOnlyRootFilesystem"
        )
    ):
        raise ValidationFailure("Incident worker runtime boundary drifted")
    if (
        worker_env.get("INCIDENT_WORKER_LEASE_SECONDS", {}).get("value") != "120"
        or worker_env.get("INCIDENT_WORKER_MAX_ATTEMPTS", {}).get("value") != "3"
        or worker_env.get("INCIDENT_WORKER_PROVIDER_TIMEOUT_SECONDS", {}).get("value")
        != "20"
        or worker_env.get("POSTGRES_PASSWORD", {})
        .get("valueFrom", {})
        .get("secretKeyRef")
        != {"name": "postgresql-auth", "key": "password"}
        or worker_env.get("PROMETHEUS_BASE_URL", {}).get("value")
        != "http://monitoring-kube-prometheus-prometheus.observability.svc.cluster.local:9090"
        or worker_env.get("LOKI_BASE_URL", {}).get("value")
        != "http://loki-gateway.observability.svc.cluster.local"
        or worker_env.get("HUBBLE_SERVER", {}).get("value")
        != "hubble-relay.kube-system.svc.cluster.local:80"
        or worker_env.get("INCIDENT_WORKER_KRCA_CONFIG", {}).get("value")
        != "/app/config/online-boutique-krca.yaml"
        or worker_env.get("NEO4J_URI", {}).get("value")
        != "bolt://neo4j.graph-rca.svc.cluster.local:7687"
        or worker_env.get("NEO4J_USERNAME", {})
        .get("valueFrom", {})
        .get("secretKeyRef")
        != {"name": "stategraph-runtime-auth", "key": "neo4j-username"}
        or worker_env.get("NEO4J_PASSWORD", {})
        .get("valueFrom", {})
        .get("secretKeyRef")
        != {"name": "stategraph-runtime-auth", "key": "neo4j-password"}
        or worker_env.get("INCIDENT_WORKER_LOCALIZATION_MAX_CANDIDATES", {}).get(
            "value"
        )
        != "10"
        or worker_env.get("INCIDENT_WORKER_LOCALIZATION_MAX_ENTITIES", {}).get(
            "value"
        )
        != "60"
        or worker_env.get("INCIDENT_WORKER_LOCALIZATION_MAX_DEPTH", {}).get("value")
        != "4"
    ):
        raise ValidationFailure("Incident worker provider or lease boundary drifted")
    worker_network_policy = next(
        document
        for document in worker_documents
        if document.get("kind") == "NetworkPolicy"
    )
    if worker_network_policy.get("spec", {}).get("ingress") != []:
        raise ValidationFailure("Incident worker must deny all ingress")

    viewer_documents = load_yaml_documents(directory / "incident-viewer.yaml")
    if any(document.get("kind") == "Secret" for document in viewer_documents):
        raise ValidationFailure("Incident Viewer credentials must not be committed")
    viewer_service_account = next(
        document
        for document in viewer_documents
        if document.get("kind") == "ServiceAccount"
    )
    if viewer_service_account.get("automountServiceAccountToken") is not False:
        raise ValidationFailure("Incident Viewer ServiceAccount token must stay disabled")
    viewer_service = next(
        document for document in viewer_documents if document.get("kind") == "Service"
    )
    if (
        viewer_service.get("spec", {}).get("type") != "ClusterIP"
        or viewer_service.get("spec", {}).get("ports")
        != [{"name": "http", "port": 8080, "targetPort": "http"}]
    ):
        raise ValidationFailure("Incident Viewer must expose only internal port 8080")
    viewer_deployment = next(
        document
        for document in viewer_documents
        if document.get("kind") == "Deployment"
    )
    viewer_pod_spec = viewer_deployment["spec"]["template"]["spec"]
    viewer_container = viewer_pod_spec["containers"][0]
    viewer_env = {item["name"]: item for item in viewer_container.get("env", [])}
    if (
        viewer_deployment["spec"].get("replicas") != 1
        or viewer_pod_spec.get("serviceAccountName") != "incident-viewer"
        or viewer_pod_spec.get("automountServiceAccountToken") is not False
        or viewer_container.get("image")
        != "agent-rca-runtime@sha256:" + "0" * 64
        or viewer_container.get("command") != ["gunicorn"]
        or "tools.run_incident_viewer:application"
        not in viewer_container.get("args", [])
        or viewer_container.get("resources", {}).get("requests")
        != {"cpu": "25m", "memory": "128Mi"}
        or not viewer_container.get("securityContext", {}).get(
            "readOnlyRootFilesystem"
        )
    ):
        raise ValidationFailure("Incident Viewer runtime boundary drifted")
    if (
        viewer_env.get("VIEWER_BEARER_TOKEN", {})
        .get("valueFrom", {})
        .get("secretKeyRef")
        != {"name": "incident-viewer-auth", "key": "api-token"}
        or viewer_env.get("POSTGRES_USERNAME", {})
        .get("valueFrom", {})
        .get("secretKeyRef")
        != {"name": "incident-viewer-auth", "key": "postgres-username"}
        or viewer_env.get("POSTGRES_PASSWORD", {})
        .get("valueFrom", {})
        .get("secretKeyRef")
        != {"name": "incident-viewer-auth", "key": "postgres-password"}
        or viewer_env.get("VIEWER_MAX_RESPONSE_BYTES", {}).get("value")
        != "8388608"
    ):
        raise ValidationFailure("Incident Viewer Secret or response bound drifted")
    viewer_network_policy = next(
        document
        for document in viewer_documents
        if document.get("kind") == "NetworkPolicy"
    )
    viewer_policy_spec = viewer_network_policy.get("spec", {})
    if (
        set(viewer_policy_spec.get("policyTypes", [])) != {"Ingress", "Egress"}
        or len(viewer_policy_spec.get("ingress", [])) != 1
        or len(viewer_policy_spec.get("egress", [])) != 2
        or viewer_policy_spec["ingress"][0].get("ports")
        != [{"protocol": "TCP", "port": 8080}]
        or viewer_policy_spec["egress"][1].get("ports")
        != [{"protocol": "TCP", "port": 5432}]
    ):
        raise ValidationFailure("Incident Viewer network boundary drifted")

    agent_worker_documents = load_yaml_documents(directory / "agent-worker.yaml")
    if any(document.get("kind") == "Secret" for document in agent_worker_documents):
        raise ValidationFailure("Agent worker credentials must not be committed")
    if {document.get("kind") for document in agent_worker_documents} != {
        "ServiceAccount",
        "Deployment",
        "Service",
        "ServiceMonitor",
        "PrometheusRule",
        "ConfigMap",
        "NetworkPolicy",
    }:
        raise ValidationFailure("Agent worker runtime resource set drifted")
    agent_service_account = next(
        document
        for document in agent_worker_documents
        if document.get("kind") == "ServiceAccount"
    )
    if agent_service_account.get("automountServiceAccountToken") is not False:
        raise ValidationFailure("Agent worker ServiceAccount token must stay disabled")
    agent_deployment = next(
        document
        for document in agent_worker_documents
        if document.get("kind") == "Deployment"
    )
    agent_pod_spec = agent_deployment["spec"]["template"]["spec"]
    agent_container = agent_pod_spec["containers"][0]
    agent_env = {item["name"]: item for item in agent_container.get("env", [])}
    if (
        agent_deployment["spec"].get("replicas") != 1
        or agent_pod_spec.get("serviceAccountName") != "incident-agent-worker"
        or agent_pod_spec.get("automountServiceAccountToken") is not False
        or agent_container.get("image")
        != "agent-rca-runtime@sha256:" + "0" * 64
        or agent_container.get("args") != ["/app/tools/run_agent_worker.py"]
        or agent_container.get("ports")
        != [{"name": "metrics", "containerPort": 9090, "protocol": "TCP"}]
        or agent_container.get("resources", {}).get("requests")
        != {"cpu": "100m", "memory": "256Mi"}
        or agent_container.get("startupProbe", {}).get("httpGet")
        != {"path": "/healthz", "port": "metrics"}
        or agent_container.get("readinessProbe", {}).get("httpGet")
        != {"path": "/healthz", "port": "metrics"}
        or agent_container.get("livenessProbe", {}).get("httpGet")
        != {"path": "/healthz", "port": "metrics"}
        or not agent_container.get("securityContext", {}).get(
            "readOnlyRootFilesystem"
        )
    ):
        raise ValidationFailure("Agent worker runtime boundary drifted")
    if (
        agent_env.get("AGENT_WORKER_LEASE_SECONDS", {}).get("value") != "180"
        or agent_env.get("AGENT_WORKER_MAX_ATTEMPTS", {}).get("value") != "3"
        or agent_env.get("AGENT_WORKER_ELIGIBILITY_LABEL", {}).get("value")
        != "agent_rca_enabled"
        or agent_env.get("AGENT_WORKER_ACTIVATED_AT", {}).get("value")
        != "2026-08-27T06:30:00Z"
        or agent_env.get("AGENT_WORKER_MIN_CLAIM_INTERVAL_SECONDS", {}).get(
            "value"
        )
        != "60"
        or agent_env.get("AGENT_WORKER_CIRCUIT_FAILURE_THRESHOLD", {}).get(
            "value"
        )
        != "3"
        or agent_env.get("AGENT_WORKER_CIRCUIT_COOLDOWN_SECONDS", {}).get(
            "value"
        )
        != "300"
        or agent_env.get("AGENT_WORKER_METRICS_PORT", {}).get("value") != "9090"
        or agent_env.get("AGENT_RCA_MODEL", {}).get("value") != "gpt-5.6-luna"
        or agent_env.get("AGENT_RCA_MAX_TOOL_CALLS", {}).get("value") != "12"
        or agent_env.get("AGENT_RCA_MAX_EVIDENCE_CANDIDATES", {}).get("value")
        != "8"
        or agent_env.get("AGENT_RCA_MAX_WALL_TIME_MS", {}).get("value")
        != "60000"
        or agent_env.get("OPENAI_API_KEY", {})
        .get("valueFrom", {})
        .get("secretKeyRef")
        != {"name": "incident-agent-auth", "key": "api-key"}
        or agent_env.get("POSTGRES_PASSWORD", {})
        .get("valueFrom", {})
        .get("secretKeyRef")
        != {"name": "postgresql-auth", "key": "password"}
    ):
        raise ValidationFailure("Agent worker budget or Secret boundary drifted")
    agent_service = next(
        document for document in agent_worker_documents if document.get("kind") == "Service"
    )
    if (
        agent_service.get("spec", {}).get("type") != "ClusterIP"
        or agent_service.get("spec", {}).get("selector")
        != {"app.kubernetes.io/name": "incident-agent-worker"}
        or agent_service.get("spec", {}).get("ports")
        != [
            {
                "name": "metrics",
                "port": 9090,
                "targetPort": "metrics",
                "protocol": "TCP",
            }
        ]
    ):
        raise ValidationFailure("Agent worker metrics Service drifted")
    agent_service_monitor = next(
        document
        for document in agent_worker_documents
        if document.get("kind") == "ServiceMonitor"
    )
    if (
        agent_service_monitor.get("metadata", {}).get("labels", {}).get("release")
        != "monitoring"
        or agent_service_monitor.get("spec", {}).get("namespaceSelector")
        != {"matchNames": ["incident-platform"]}
        or agent_service_monitor.get("spec", {}).get("endpoints")
        != [
            {
                "port": "metrics",
                "path": "/metrics",
                "interval": "30s",
                "scrapeTimeout": "10s",
            }
        ]
    ):
        raise ValidationFailure("Agent worker ServiceMonitor drifted")
    agent_prometheus_rule = next(
        document
        for document in agent_worker_documents
        if document.get("kind") == "PrometheusRule"
    )
    agent_alert_names = {
        rule.get("alert")
        for group in agent_prometheus_rule.get("spec", {}).get("groups", [])
        for rule in group.get("rules", [])
    }
    if (
        agent_prometheus_rule.get("metadata", {}).get("labels", {}).get("release")
        != "monitoring"
        or agent_alert_names
        != {
            "AgentRCAWorkerUnavailable",
            "AgentRCAWorkerCircuitOpen",
            "AgentRCAWorkerHighFailureRatio",
            "AgentRCAWorkerRapidTokenBurn",
            "AgentRCAWorkerSlowRuns",
            "AgentRCAWorkQueueObservationFailed",
            "AgentRCAWorkQueueBacklog",
            "AgentRCAWorkQueueOldestReady",
            "AgentRCAWorkQueueStuckRunning",
        }
    ):
        raise ValidationFailure("Agent worker Prometheus alert boundary drifted")
    agent_dashboard_configmap = next(
        document
        for document in agent_worker_documents
        if document.get("kind") == "ConfigMap"
    )
    try:
        agent_dashboard = json.loads(
            agent_dashboard_configmap.get("data", {}).get(
                "agent-rca-operations.json", ""
            )
        )
    except json.JSONDecodeError as error:
        raise ValidationFailure("Agent RCA Grafana dashboard JSON is invalid") from error
    dashboard_expressions = {
        target.get("expr")
        for panel in agent_dashboard.get("panels", [])
        for target in panel.get("targets", [])
    }
    if (
        agent_dashboard_configmap.get("metadata", {}).get("labels", {}).get(
            "grafana_dashboard"
        )
        != "1"
        or agent_dashboard.get("uid") != "agent-rca-operations"
        or agent_dashboard.get("title") != "Agent RCA Operations"
        or "max by (stage, state) (agent_rca_work_items)"
        not in dashboard_expressions
        or "max by (stage) (agent_rca_work_oldest_ready_age_seconds)"
        not in dashboard_expressions
    ):
        raise ValidationFailure("Agent RCA Grafana dashboard boundary drifted")
    agent_network_policy = next(
        document
        for document in agent_worker_documents
        if document.get("kind") == "NetworkPolicy"
    )
    agent_policy_spec = agent_network_policy.get("spec", {})
    if (
        set(agent_policy_spec.get("policyTypes", [])) != {"Ingress", "Egress"}
        or len(agent_policy_spec.get("ingress", [])) != 1
        or agent_policy_spec["ingress"][0].get("ports")
        != [{"protocol": "TCP", "port": 9090}]
        or agent_policy_spec["ingress"][0].get("from", [])[0].get(
            "namespaceSelector"
        )
        != {"matchLabels": {"kubernetes.io/metadata.name": "observability"}}
        or len(agent_policy_spec.get("egress", [])) != 3
    ):
        raise ValidationFailure("Agent worker network boundary drifted")
    https_ip_block = agent_policy_spec["egress"][2]["to"][0]["ipBlock"]
    if (
        https_ip_block.get("cidr") != "0.0.0.0/0"
        or set(https_ip_block.get("except", []))
        != {
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "169.254.0.0/16",
        }
        or agent_policy_spec["egress"][2].get("ports")
        != [{"protocol": "TCP", "port": 443}]
    ):
        raise ValidationFailure("Agent worker external HTTPS boundary drifted")

    alertmanager_config = load_yaml_documents(
        directory / "alertmanager-routing.yaml"
    )[0]
    alert_route = alertmanager_config.get("spec", {}).get("route", {})
    webhook_config = alertmanager_config["spec"]["receivers"][0][
        "webhookConfigs"
    ][0]
    authorization = webhook_config["httpConfig"]["authorization"]
    if (
        alertmanager_config.get("kind") != "AlertmanagerConfig"
        or alertmanager_config.get("metadata", {}).get("namespace")
        != "online-boutique"
        or alert_route.get("matchers")
        != [{"name": "rca_enabled", "matchType": "=", "value": "true"}]
        or webhook_config.get("url")
        != "http://incident-webhook.incident-platform.svc.cluster.local:8080/v1/alertmanager/webhook"
        or webhook_config.get("sendResolved") is not True
        or webhook_config.get("maxAlerts") != 20
        or authorization
        != {
            "type": "Bearer",
            "credentials": {"name": "incident-webhook-auth", "key": "token"},
        }
    ):
        raise ValidationFailure("Alertmanager Incident routing boundary drifted")

    cronjob = load_yaml_documents(directory / "stategraph-reconciler.yaml")[0]
    job_pod_spec = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    if (
        cronjob["spec"].get("schedule") != expected_runtime["reconciler"]["schedule"]
        or cronjob["spec"].get("suspend") is not False
        or cronjob["spec"].get("concurrencyPolicy")
        != expected_runtime["reconciler"]["concurrency_policy"]
        or job_pod_spec.get("serviceAccountName") != "incident-platform-reader"
        or job_pod_spec.get("automountServiceAccountToken") is not True
        or job_pod_spec.get("restartPolicy") != "Never"
    ):
        raise ValidationFailure("StateGraph reconciler schedule or RBAC drifted")
    reconciler = job_pod_spec["containers"][0]
    if reconciler.get("image") != "agent-rca-runtime@sha256:" + "0" * 64:
        raise ValidationFailure(
            "StateGraph reconciler base image must require the Ansible digest overlay"
        )
    if not reconciler.get("securityContext", {}).get("readOnlyRootFilesystem"):
        raise ValidationFailure("StateGraph reconciler root filesystem must be read-only")
    reconciler_env = {item["name"]: item for item in reconciler.get("env", [])}
    if (
        reconciler_env.get("POSTGRES_PASSWORD", {})
        .get("valueFrom", {})
        .get("secretKeyRef")
        != {"name": "postgresql-auth", "key": "password"}
        or reconciler_env.get("NEO4J_PASSWORD", {})
        .get("valueFrom", {})
        .get("secretKeyRef")
        != {"name": "stategraph-runtime-auth", "key": "neo4j-password"}
    ):
        raise ValidationFailure("StateGraph runtime credentials must use Secret refs")

    dockerfile = (directory / "Dockerfile").read_text(encoding="utf-8")
    if (
        "python:3.12.11-slim-bookworm@sha256:" not in dockerfile
        or "run_incident_receiver.py" not in dockerfile
        or "run_incident_worker.py" not in dockerfile
        or "run_incident_viewer.py" not in dockerfile
        or "config/online-boutique-krca.yaml" not in dockerfile
        or "USER 65532:65532" not in dockerfile
    ):
        raise ValidationFailure("Incident Platform runtime image boundary drifted")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    if "gunicorn==26.0.0\n" not in requirements:
        raise ValidationFailure("Incident webhook WSGI server must remain pinned")


def validate_incident_viewer_frontend_manifest() -> None:
    directory = ROOT / "platform" / "incident-viewer-frontend"
    kustomization = load_yaml_documents(directory / "kustomization.yaml")[0]
    if kustomization.get("resources") != ["runtime.yaml"]:
        raise ValidationFailure("Viewer frontend Kustomize resource set drifted")

    documents = load_yaml_documents(directory / "runtime.yaml")
    if any(document.get("kind") in {"Ingress", "Secret"} for document in documents):
        raise ValidationFailure("Viewer frontend must not commit secrets or public ingress")
    if [document.get("kind") for document in documents] != [
        "ServiceAccount",
        "Service",
        "Deployment",
        "NetworkPolicy",
    ]:
        raise ValidationFailure("Viewer frontend private runtime resource set drifted")

    service_account, service, deployment, network_policy = documents
    if service_account.get("automountServiceAccountToken") is not False:
        raise ValidationFailure("Viewer frontend ServiceAccount token must stay disabled")

    service_spec = service.get("spec", {})
    service_ports = service_spec.get("ports", [])
    if (
        service_spec.get("type") != "ClusterIP"
        or service_spec.get("selector")
        != {"app.kubernetes.io/name": "incident-viewer-frontend"}
        or service_ports
        != [{"name": "http", "port": 3100, "targetPort": "http"}]
        or any("nodePort" in port for port in service_ports)
    ):
        raise ValidationFailure("Viewer frontend must expose only private port 3100")

    pod_spec = deployment["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    env = {item["name"]: item for item in container.get("env", [])}
    if (
        deployment["spec"].get("replicas") != 1
        or pod_spec.get("serviceAccountName") != "incident-viewer-frontend"
        or pod_spec.get("automountServiceAccountToken") is not False
        or pod_spec.get("imagePullSecrets") != [{"name": "artifact-registry"}]
        or container.get("image") != "agent-rca-viewer@sha256:" + "0" * 64
        or container.get("ports")
        != [{"name": "http", "containerPort": 3100, "protocol": "TCP"}]
        or container.get("startupProbe", {}).get("httpGet")
        != {"path": "/api/healthz", "port": "http"}
        or container.get("readinessProbe", {}).get("httpGet")
        != {"path": "/api/healthz", "port": "http"}
        or container.get("livenessProbe", {}).get("httpGet")
        != {"path": "/api/healthz", "port": "http"}
        or container.get("securityContext", {}).get("readOnlyRootFilesystem")
        is not True
    ):
        raise ValidationFailure("Viewer frontend Pod safety boundary drifted")
    if (
        env.get("NEXT_PUBLIC_VIEWER_API_BASE_URL", {}).get("value") != "/api/viewer"
        or env.get("VIEWER_API_ORIGIN", {}).get("value")
        != "http://incident-viewer.incident-platform.svc.cluster.local:8080"
        or env.get("VIEWER_API_TOKEN", {})
        .get("valueFrom", {})
        .get("secretKeyRef")
        != {"name": "incident-viewer-auth", "key": "api-token"}
    ):
        raise ValidationFailure("Viewer frontend BFF credential boundary drifted")

    policy_spec = network_policy.get("spec", {})
    if (
        set(policy_spec.get("policyTypes", [])) != {"Ingress", "Egress"}
        or policy_spec.get("ingress") != []
        or len(policy_spec.get("egress", [])) != 2
        or policy_spec["egress"][1].get("to", [])[0].get("podSelector")
        != {"matchLabels": {"app.kubernetes.io/name": "incident-viewer"}}
        or policy_spec["egress"][1].get("ports")
        != [{"protocol": "TCP", "port": 8080}]
    ):
        raise ValidationFailure("Viewer frontend private network boundary drifted")

    dockerfile = (ROOT / "frontend" / "viewer" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    next_config = (ROOT / "frontend" / "viewer" / "next.config.mjs").read_text(
        encoding="utf-8"
    )
    build_script = (ROOT / "tools" / "build_viewer_frontend_image.sh").read_text(
        encoding="utf-8"
    )
    stack_role = (
        ROOT
        / "automation"
        / "ansible"
        / "roles"
        / "incident_viewer_frontend_stack"
        / "tasks"
        / "main.yml"
    ).read_text(encoding="utf-8")
    verify_role = (
        ROOT
        / "automation"
        / "ansible"
        / "roles"
        / "incident_viewer_frontend_verify"
        / "tasks"
        / "main.yml"
    ).read_text(encoding="utf-8")
    required_tokens = {
        "node:22.17.1-bookworm-slim@sha256:": dockerfile,
        'output: "standalone"': next_config,
        "agent-rca-viewer": build_script,
        "digest: {{ incident_platform.viewer_frontend.image_digest }}": stack_role,
        "service_type == 'ClusterIP'": stack_role,
        "Reject Incident Platform Ingress resources": verify_role,
        "is not defined": verify_role,
        "incident-viewer.incident-platform.svc.cluster.local:8080": verify_role,
    }
    missing_tokens = sorted(
        token for token, source in required_tokens.items() if token not in source
    )
    if missing_tokens:
        raise ValidationFailure(
            f"Viewer frontend deployment boundary is incomplete: {missing_tokens}"
        )


def validate_observability_values() -> None:
    directory = ROOT / "platform" / "observability"
    local_path = load_yaml_documents(directory / "local-path-values.yaml")[0]
    prometheus = load_yaml_documents(
        directory / "kube-prometheus-stack-values.yaml"
    )[0]
    loki = load_yaml_documents(directory / "loki-values.yaml")[0]
    tempo_directory = directory / "tempo"
    tempo_config = load_yaml_documents(tempo_directory / "tempo-config.yaml")[0]
    tempo_documents = load_yaml_documents(tempo_directory / "tempo.yaml")
    alloy = load_yaml_documents(directory / "alloy-values.yaml")[0]

    storage_class = local_path.get("storageClass", {})
    if (
        storage_class.get("name") != "agent-rca-local"
        or storage_class.get("defaultClass") is not False
        or storage_class.get("reclaimPolicy") != "Retain"
        or storage_class.get("volumeBindingMode") != "WaitForFirstConsumer"
    ):
        raise ValidationFailure("local observability StorageClass boundary drifted")

    prometheus_spec = prometheus.get("prometheus", {}).get("prometheusSpec", {})
    if (
        prometheus_spec.get("retention") != "7d"
        or prometheus_spec.get("retentionSize") != "12GiB"
        or prometheus_spec.get("storageSpec", {})
        .get("volumeClaimTemplate", {})
        .get("spec", {})
        .get("resources", {})
        .get("requests", {})
        .get("storage")
        != "15Gi"
    ):
        raise ValidationFailure("Prometheus retention or storage boundary drifted")
    for component in ("prometheus", "alertmanager", "grafana"):
        values = prometheus.get(component, {})
        if values.get("ingress", {}).get("enabled") is not False:
            raise ValidationFailure(f"{component} Ingress must remain disabled")
        if values.get("service", {}).get("type") != "ClusterIP":
            raise ValidationFailure(f"{component} Service must remain ClusterIP")

    tempo_datasources = {
        datasource.get("uid"): datasource
        for datasource in prometheus.get("grafana", {}).get("additionalDataSources", [])
    }
    if (
        tempo_datasources.get("tempo", {}).get("type") != "tempo"
        or tempo_datasources.get("tempo", {}).get("url")
        != "http://tempo.observability.svc.cluster.local:3200"
    ):
        raise ValidationFailure("Grafana Tempo datasource is missing or unsafe")
    dashboard_sidecar = (
        prometheus.get("grafana", {}).get("sidecar", {}).get("dashboards", {})
    )
    if dashboard_sidecar != {
        "enabled": True,
        "label": "grafana_dashboard",
        "labelValue": "1",
        "searchNamespace": "ALL",
    }:
        raise ValidationFailure("Grafana dashboard sidecar boundary drifted")

    if (
        loki.get("deploymentMode") != "Monolithic"
        or loki.get("loki", {}).get("auth_enabled") is not False
        or loki.get("loki", {}).get("limits_config", {}).get("retention_period")
        != "72h"
        or loki.get("singleBinary", {}).get("replicas") != 1
        or loki.get("gateway", {}).get("service", {}).get("type") != "ClusterIP"
    ):
        raise ValidationFailure("Loki monolithic/private retention boundary drifted")

    tempo_version = load_yaml_documents(ROOT / "platform" / "versions.yaml")[0]["observability"]["tempo"]
    tempo_statefulset = next(
        document for document in tempo_documents if document["kind"] == "StatefulSet"
    )
    tempo_services = [
        document for document in tempo_documents if document["kind"] == "Service"
    ]
    tempo_container = tempo_statefulset["spec"]["template"]["spec"]["containers"][0]
    tempo_claim = tempo_statefulset["spec"]["volumeClaimTemplates"][0]["spec"]
    expected_tempo_image = (
        f"docker.io/grafana/tempo:{tempo_version['app_version']}@"
        f"{tempo_version['image_digest']}"
    )
    if (
        tempo_container.get("image") != expected_tempo_image
        or tempo_config.get("usage_report", {}).get("reporting_enabled") is not False
        or tempo_config.get("compactor", {}).get("compaction", {}).get(
            "block_retention"
        )
        != "72h"
        or tempo_config.get("storage", {}).get("trace", {}).get("backend")
        != "local"
        or len(tempo_services) != 2
        or any(service.get("spec", {}).get("type") != "ClusterIP" for service in tempo_services)
        or tempo_claim.get("storageClassName") != "agent-rca-local"
        or tempo_claim.get("resources", {}).get("requests", {}).get("storage")
        != "5Gi"
    ):
        raise ValidationFailure("Tempo monolithic/private retention boundary drifted")

    if (
        alloy.get("controller", {}).get("type") != "daemonset"
        or alloy.get("service", {}).get("type") != "ClusterIP"
    ):
        raise ValidationFailure("Alloy DaemonSet/private service boundary drifted")
    alloy_config = alloy.get("alloy", {}).get("configMap", {}).get("content", "")
    if (
        'loki.source.kubernetes "pods"' not in alloy_config
        or 'replacement  = "[[ cluster_name' not in alloy_config
        or 'loki.source.journal "kernel"' not in alloy_config
        or 'matches    = "_TRANSPORT=kernel"' not in alloy_config
        or 'loki.process "kernel_oom"' not in alloy_config
        or "CONSTRAINT_MEMCG" not in alloy_config
        or "pod_uid" not in alloy_config
    ):
        raise ValidationFailure("Alloy log normalization drifted")
    pod_security = alloy.get("global", {}).get("podSecurityContext", {})
    mounts = alloy.get("alloy", {}).get("mounts", {}).get("extra", [])
    volumes = alloy.get("controller", {}).get("volumes", {}).get("extra", [])
    if (
        pod_security.get("supplementalGroups") != [999]
        or not any(
            mount.get("name") == "host-journal"
            and mount.get("mountPath") == "/var/log/journal"
            and mount.get("readOnly") is True
            for mount in mounts
        )
        or not any(
            volume.get("name") == "host-journal"
            and volume.get("hostPath")
            == {"path": "/var/log/journal", "type": "Directory"}
            for volume in volumes
        )
    ):
        raise ValidationFailure("Alloy host journal read boundary drifted")
    for rule in alloy.get("rbac", {}).get("rules", []):
        resources = set(rule.get("resources", []))
        verbs = set(rule.get("verbs", []))
        if "secrets" in resources or verbs - {"get", "list", "watch"}:
            raise ValidationFailure("Alloy RBAC exceeds the read-only log boundary")

    cilium_template = (
        ROOT
        / "automation"
        / "ansible"
        / "roles"
        / "cilium"
        / "templates"
        / "cilium-values.yaml.j2"
    ).read_text(encoding="utf-8")
    if (
        cilium_template.count("serviceMonitor:") < 5
        or "prometheus_service_monitor_crd.rc == 0" not in cilium_template
    ):
        raise ValidationFailure("Cilium/Hubble ServiceMonitor gate drifted")


def validate_three_domain_runtime() -> None:
    observability_directory = ROOT / "platform" / "observability"
    scope = load_yaml_documents(ROOT / "config" / "project-scope.yaml")[0]
    topology = scope.get("runtime_topology", {})
    if (
        topology.get("model") != "three-independent-failure-domains"
        or set(topology.get("domains", {}))
        != {"rca-control", "fault-target", "observability"}
        or topology.get("transport") != "private-vpc-tag-scoped-firewall"
    ):
        raise ValidationFailure("project scope three-domain topology drifted")
    forwarder_values = (
        observability_directory / "kube-prometheus-stack-forwarder-values.yaml"
    ).read_text(encoding="utf-8")
    receiver_values = (
        observability_directory / "kube-prometheus-stack-receiver-values.yaml"
    ).read_text(encoding="utf-8")
    loki_receiver_values = (
        observability_directory / "loki-receiver-values.yaml"
    ).read_text(encoding="utf-8")
    tempo_receiver = load_yaml_documents(
        observability_directory / "tempo-receiver" / "service-nodeport.yaml"
    )[0]
    tempo_ports = {
        port["name"]: port.get("nodePort")
        for port in tempo_receiver.get("spec", {}).get("ports", [])
    }
    if (
        "remoteWrite:" not in forwarder_values
        or "observability_prometheus_remote_write_url" not in forwarder_values
        or "agent_rca_.+" not in forwarder_values
        or "enableRemoteWriteReceiver: true" not in receiver_values
        or "type: NodePort" not in receiver_values
        or "default(30090)" not in receiver_values
        or "type: NodePort" not in loki_receiver_values
        or "default(30100)" not in loki_receiver_values
        or tempo_receiver.get("spec", {}).get("type") != "NodePort"
        or tempo_ports != {"http": 30320, "otlp-grpc": 30317, "otlp-http": 30318}
    ):
        raise ValidationFailure("three-domain telemetry transport boundary drifted")

    observability_inventory = load_yaml_documents(
        ROOT
        / "automation"
        / "ansible"
        / "inventories"
        / "observability.example.yml"
    )[0]
    observability_children = observability_inventory.get("all", {}).get(
        "children", {}
    )
    observability_host = (
        observability_children.get("kubernetes_nodes", {})
        .get("hosts", {})
        .get("agent_rca_observability_node", {})
    )
    observability_profile = load_yaml_documents(
        ROOT / "automation" / "ansible" / "group_vars" / "observability.yml"
    )[0]
    if (
        observability_host.get("deployment_profile_vars_file")
        != "../group_vars/observability.yml"
        or "agent_rca_observability_node"
        not in observability_children.get("observability_domain", {}).get("hosts", {})
        or observability_profile
        != {
            "cluster_name": "agent-rca-observability",
            "observability_domain_mode": "receiver",
        }
    ):
        raise ValidationFailure("observability receiver inventory boundary drifted")

    playbook = (
        ROOT / "automation" / "ansible" / "playbooks" / "deploy-three-domain.yml"
    ).read_text(encoding="utf-8")
    stack_role = (
        ROOT
        / "automation"
        / "ansible"
        / "roles"
        / "incident_platform_stack"
        / "tasks"
        / "main.yml"
    ).read_text(encoding="utf-8")
    wiring_role = (
        ROOT
        / "automation"
        / "ansible"
        / "roles"
        / "three_domain_observability_wiring"
        / "tasks"
        / "main.yml"
    ).read_text(encoding="utf-8")
    quiesce_role = (
        ROOT
        / "automation"
        / "ansible"
        / "roles"
        / "three_domain_quiesce"
        / "tasks"
        / "main.yml"
    ).read_text(encoding="utf-8")
    required_tokens = {
        "fault_target": playbook,
        "rca_control": playbook,
        "observability_domain": playbook,
        "three_domain_target_access": playbook,
        "three_domain_observability_wiring": playbook,
        "three_domain_quiesce": playbook,
        "remote-target-kubernetes": stack_role,
        "externalTrafficPolicy: Local": stack_role,
        "KUBERNETES_TOKEN_FILE": stack_role,
        "rca_enabled": wiring_role,
        "remote_metric_series": wiring_role,
        "remote_log_streams": wiring_role,
        "remote_traces": wiring_role,
        "--replicas=0": quiesce_role,
        "--ignore-not-found": quiesce_role,
    }
    missing_tokens = sorted(
        token for token, source in required_tokens.items() if token not in source
    )
    if missing_tokens:
        raise ValidationFailure(
            f"three-domain orchestration boundary is incomplete: {missing_tokens}"
        )

    remote_route = (
        observability_directory / "remote-alertmanager-routing.yaml"
    ).read_text(encoding="utf-8")
    remote_alert = (
        observability_directory / "remote-online-boutique-alerts.yaml"
    ).read_text(encoding="utf-8")
    if (
        "RCA_CONTROL_PRIVATE_ADDRESS:30080" not in remote_route
        or "rca_enabled" not in remote_route
        or "authorization:" not in remote_route
        or 'cluster_id="FAULT_TARGET_CLUSTER_ID"' not in remote_alert
        or 'rca_enabled: "true"' not in remote_alert
    ):
        raise ValidationFailure("remote alert identity or authentication drifted")
    remote_alert_document = load_yaml_documents(
        observability_directory / "remote-online-boutique-alerts.yaml"
    )[0]
    remote_rules = remote_alert_document["spec"]["groups"][0]["rules"]
    expected_remote_profiles = {
        "OnlineBoutiqueHomeHighFailureRate": "browse-home",
        "OnlineBoutiqueProductDetailHighFailureRate": "product-detail",
        "OnlineBoutiqueCartReadHighFailureRate": "cart-read",
        "OnlineBoutiqueCartAddHighFailureRate": "cart-add",
        "OnlineBoutiqueCartEmptyHighFailureRate": "cart-empty",
        "OnlineBoutiqueCheckoutHighFailureRate": "checkout-full",
    }
    if {
        rule.get("alert"): rule.get("labels", {}).get("krca_profile")
        for rule in remote_rules
    } != expected_remote_profiles:
        raise ValidationFailure("remote route-to-KRCA profile mapping drifted")


def validate_policy_configs() -> None:
    routing = load_yaml_documents(ROOT / "config" / "rca-routing.yaml")[0]
    if routing["preconditions"]["ground_truth_access_allowed"]:
        raise ValidationFailure("RCA routing allows Ground Truth access")
    if routing["preconditions"]["write_tools_allowed"]:
        raise ValidationFailure("RCA routing allows write tools")
    if routing["fast_path"]["llm_calls"] != 0:
        raise ValidationFailure("Fast Path must not call an LLM")
    retrieval = routing["knowledge_retrieval"]
    if retrieval["bounds"]["max_documents"] > 5:
        raise ValidationFailure("Knowledge retrieval exceeds the MVP document cap")
    if retrieval["bounds"]["max_characters"] > 12000:
        raise ValidationFailure("Knowledge retrieval exceeds the MVP character cap")
    if retrieval["bounds"]["max_query_terms"] > 16:
        raise ValidationFailure("Knowledge retrieval exceeds the query-term cap")
    if retrieval["bounds"]["max_timeout_seconds"] > 5:
        raise ValidationFailure("Knowledge retrieval exceeds the timeout cap")
    if retrieval["bounds"]["max_index_documents"] > 500:
        raise ValidationFailure("Knowledge retrieval exceeds the index scan cap")
    if retrieval["evidence_separation"]["references_are_evidence"]:
        raise ValidationFailure("Operational references must not become runtime Evidence")
    if not retrieval["evidence_separation"]["require_evidence_id_for_root_cause"]:
        raise ValidationFailure("Root-cause conclusions must require Evidence IDs")
    if "evaluation-ground-truth" not in retrieval["prohibited_sources"]:
        raise ValidationFailure("Knowledge retrieval does not exclude Ground Truth")
    if retrieval["incident_memory"]["enabled"]:
        raise ValidationFailure("Incident Memory must remain disabled before validation")
    budget = routing["deep_path"]["budget"]
    if any(value <= 0 for value in budget.values()):
        raise ValidationFailure("Every Deep Path budget must be positive")

    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    ignored_private_labels = {
        "evaluation/ground-truth/private/*.json",
        "evaluation/ground-truth/private/*.yaml",
    }
    if not ignored_private_labels <= set(gitignore):
        raise ValidationFailure("private Ground Truth labels are not ignored by Git")
    runtime_dockerfile = (
        ROOT / "platform" / "incident-platform" / "Dockerfile"
    ).read_text(encoding="utf-8")
    if "COPY evaluation" in runtime_dockerfile or "ground-truth" in runtime_dockerfile:
        raise ValidationFailure("Incident runtime image includes evaluation Ground Truth")

    preregistration = load_yaml_documents(
        ROOT / "evaluation" / "preregistration.yaml"
    )[0]
    variant_ids = [variant["id"] for variant in preregistration["variants"]]
    if variant_ids != ["A", "B", "C", "D"]:
        raise ValidationFailure(f"evaluation variants changed: {variant_ids}")
    if preregistration["dataset"]["runtime_mount_allowed"]:
        raise ValidationFailure("Ground Truth may not be mounted into the RCA runtime")
    if preregistration["dataset"]["minimum_incidents"] < 15:
        raise ValidationFailure("evaluation dataset minimum dropped below 15 incidents")
    if preregistration["dataset"]["minimum_scenarios"] < 15:
        raise ValidationFailure("evaluation scenario minimum dropped below 15")
    if preregistration["dataset"]["runtime_repetitions_per_scenario"] < 5:
        raise ValidationFailure("runtime scenario repetitions dropped below 5")
    if not preregistration["dataset"]["no_fault_controls_required"]:
        raise ValidationFailure("evaluation must retain no-fault controls")
    if not preregistration["dataset"]["workload_seed_recorded"]:
        raise ValidationFailure("evaluation must record workload seeds")

    required_cells = {
        (False, "normal"),
        (True, "normal"),
        (False, "stress"),
        (True, "stress"),
    }
    recorded_cells = {
        (cell["change"], cell["workload"])
        for cell in preregistration["experiment_design"]["required_cells"]
    }
    if (
        recorded_cells != required_cells
        or len(preregistration["experiment_design"]["required_cells"]) != 4
    ):
        raise ValidationFailure("Change x Workload evaluation cells changed")
    required_workload_profiles = {"normal", "spike", "soak", "path-weighted"}
    if (
        set(preregistration["dataset"]["required_workload_profiles"])
        != required_workload_profiles
    ):
        raise ValidationFailure("required workload profiles changed")
    required_change_families = {
        "application-rollout",
        "configuration",
        "resource-policy",
        "network-policy",
    }
    if (
        set(preregistration["dataset"]["required_change_families"])
        != required_change_families
    ):
        raise ValidationFailure("required change families changed")
    if not preregistration["experiment_design"]["change_evidence_ablation"][
        "enabled"
    ]:
        raise ValidationFailure("Change Evidence ablation must remain enabled")
    load_generator = preregistration["experiment_design"]["load_generator"]
    if load_generator["failure_domain"] != "external-to-target-node":
        raise ValidationFailure("load generator must not distort target-node signals")
    if not load_generator["synthetic_marker_required"]:
        raise ValidationFailure("synthetic workload must remain identifiable")

    change_evidence = preregistration["change_evidence"]
    if change_evidence["secret_values_allowed"]:
        raise ValidationFailure("Change Evidence allows Secret values")
    if change_evidence["change_only_root_cause_allowed"]:
        raise ValidationFailure("Change history alone may not prove root cause")
    if not change_evidence["provenance_required"]:
        raise ValidationFailure("Change Evidence must preserve provenance")
    required_change_fields = {
        "change_id",
        "change_type",
        "observed_at",
        "affected_entity_keys",
        "source_reference",
        "before_hash",
        "after_hash",
    }
    if set(change_evidence["required_fields"]) != required_change_fields:
        raise ValidationFailure("Change Evidence required fields changed")
    evidence_ground_truth = preregistration["evidence_ground_truth"]
    if evidence_ground_truth["schema_version"] != "1.1.0":
        raise ValidationFailure("role-based Evidence Ground Truth is not registered")
    if evidence_ground_truth["scoring_unit"] != "causal-role":
        raise ValidationFailure("Evidence recall must be scored by causal role")
    if not evidence_ground_truth["alternative_proofs_allowed"]:
        raise ValidationFailure("equivalent causal Evidence alternatives were disabled")
    if evidence_ground_truth["auxiliary_observations_are_causal_labels"]:
        raise ValidationFailure("auxiliary observations may not become causal labels")
    controlled_oom_roles = {
        role["role"]: role
        for role in evidence_ground_truth["controlled_oom_roles"]
    }
    if set(controlled_oom_roles) != {
        "exact-oom-signature",
        "same-pod-restart-delta",
    }:
        raise ValidationFailure("controlled OOM causal Evidence roles changed")
    if any(
        int(role["minimum_matches"]) != 1
        for role in controlled_oom_roles.values()
    ):
        raise ValidationFailure("controlled OOM roles require one acceptable proof")
    reporting = preregistration["reporting"]
    required_records = (
        "record_platform_and_application_versions",
        "record_change_manifest_hash",
        "record_workload_profile_and_seed",
    )
    if not all(reporting[item] for item in required_records):
        raise ValidationFailure("evaluation reporting lost reproducibility metadata")

    graph_model = load_yaml_documents(
        ROOT / "contracts" / "graph" / "stategraph-model.yaml"
    )[0]
    if graph_model["canonicalization"]["secret_values_allowed"]:
        raise ValidationFailure("StateGraph model allows Secret values")
    if not graph_model["temporal_semantics"]["snapshot"][
        "merge_only_consecutive_equal_state"
    ]:
        raise ValidationFailure("StateGraph would merge non-consecutive equal states")
    reconciliation = graph_model["temporal_semantics"][
        "complete_set_reconciliation"
    ]
    if reconciliation["active_interval_evidence_ids"] != (
        "replace_with_latest_cycle"
    ):
        raise ValidationFailure(
            "StateGraph reconciliation would accumulate stale Evidence IDs"
        )
    observation_journal = graph_model["persistence"]["observation_journal"]
    if observation_journal["write_order"] != [
        "stage_cycle_and_normalized_evidence",
        "reconcile_graph_transaction",
        "mark_cycle_applied",
    ]:
        raise ValidationFailure(
            "StateGraph observation Evidence is not durable before Graph mutation"
        )
    if observation_journal["distributed_transaction"]:
        raise ValidationFailure(
            "StateGraph observation journal must expose its retry boundary"
        )
    if observation_journal["retry_contract"]["staged_cycle_retry_source"] != (
        "stored_normalized_evidence"
    ):
        raise ValidationFailure(
            "StateGraph observation retry would recollect mutable source state"
        )
    observation_retention = graph_model["retention"]["observation_journal"]
    if observation_retention != {
        "applied_cycle_and_evidence_hours": 72,
        "abandoned_staged_cycle_hours": 24,
    }:
        raise ValidationFailure("StateGraph observation retention changed")


def validate_negative_evidence_reference(examples: dict[str, Any]) -> None:
    intentionally_broken = copy.deepcopy(examples)
    intentionally_broken["report"]["hypotheses"][0][
        "supporting_evidence_ids"
    ].append("ev-invented-9999")
    try:
        validate_cross_contract_references(intentionally_broken)
    except ValidationFailure:
        pass
    else:
        raise ValidationFailure(
            "negative evidence self-test failed to reject an invented evidence_id"
        )


def validate_controlled_fault_scenarios() -> None:
    schemas, registry = schema_registry()
    scenario_path = ROOT / "evaluation" / "scenarios" / "checkoutservice-oom.yaml"
    scenario = load_yaml_documents(scenario_path)[0]
    validate_instance(
        schemas["controlled-fault-scenario.schema.json"],
        scenario,
        registry,
        "checkoutservice-oom.yaml",
    )
    if scenario["baseline"] != {
        "requests": {"cpu": "100m", "memory": "64Mi"},
        "limits": {"cpu": "200m", "memory": "128Mi"},
    }:
        raise ValidationFailure("checkout OOM baseline drifted from the pinned workload")
    if scenario["fault"]["resources"] != {
        "requests": {"cpu": "100m", "memory": "64Mi"},
        "limits": {"cpu": "200m", "memory": "96Mi"},
    }:
        raise ValidationFailure("checkout OOM fault resources changed")
    if scenario["fault"]["chaos_mesh"] != {
        "api_version": "chaos-mesh.org/v1alpha1",
        "kind": "StressChaos",
        "mode": "all",
        "duration_seconds": 120,
        "memory": {
            "workers": 1,
            "size": "128MiB",
            "oom_score_adj": -1000,
        },
    }:
        raise ValidationFailure("checkout OOM Chaos Mesh injection changed")
    if scenario["workload"]["runner"] != "external-controller":
        raise ValidationFailure("controlled load would contaminate the target node")

    harness = (
        ROOT
        / "automation"
        / "ansible"
        / "roles"
        / "checkout_oom_fault_harness"
        / "tasks"
        / "main.yml"
    ).read_text(encoding="utf-8")
    required_safety_tokens = {
        "confirm_controlled_fault",
        "controlled_fault_environment",
        "agent-rca-checkout-oom-lock",
        "remote fail-safe resource restoration watchdog",
        "Create the bounded checkoutservice StressChaos",
        "Delete the controlled StressChaos",
        "always:",
        "Restore the exact checkoutservice baseline resources",
        "Require successful automatic restoration",
    }
    missing_tokens = sorted(required_safety_tokens - set(
        token for token in required_safety_tokens if token in harness
    ))
    if missing_tokens:
        raise ValidationFailure(
            f"controlled-fault safety boundary is incomplete: {missing_tokens}"
        )

    image_pull_path = (
        ROOT
        / "evaluation"
        / "scenarios"
        / "paymentservice-image-pull.yaml"
    )
    image_pull = load_yaml_documents(image_pull_path)[0]
    validate_instance(
        schemas["controlled-image-pull-scenario.schema.json"],
        image_pull,
        registry,
        "paymentservice-image-pull.yaml",
    )
    if image_pull["baseline"]["image"] != (
        "us-central1-docker.pkg.dev/online-boutique-ci/microservices-demo/"
        "paymentservice:v0.10.6"
    ):
        raise ValidationFailure("payment image-pull baseline image changed")
    if image_pull["fault"] != {
        "type": "image-reference",
        "image": "registry.invalid/agent-rca/paymentservice:missing",
        "observation_seconds": 30,
        "maximum_active_seconds": 180,
    }:
        raise ValidationFailure("payment image-pull injection changed")
    image_pull_harness = (
        ROOT
        / "automation"
        / "ansible"
        / "roles"
        / "payment_image_pull_fault_harness"
        / "tasks"
        / "main.yml"
    ).read_text(encoding="utf-8")
    image_pull_safety_tokens = {
        "confirm_controlled_fault",
        "controlled_fault_environment",
        "agent-rca-payment-image-pull-lock",
        "remote fail-safe exact-image restoration watchdog",
        "Apply the preregistered invalid paymentservice image",
        "controlled_fault_evaluation",
        "always:",
        "Restore the exact paymentservice image",
        "Require successful exact-image automatic restoration",
        "payment_image_pull_original_restart_count",
        "no-increase-or-fresh-replacement",
    }
    missing_image_pull_tokens = sorted(
        token
        for token in image_pull_safety_tokens
        if token not in image_pull_harness
    )
    if missing_image_pull_tokens:
        raise ValidationFailure(
            "image-pull controlled-fault safety boundary is incomplete: "
            f"{missing_image_pull_tokens}"
        )

    missing_configmap_path = (
        ROOT
        / "evaluation"
        / "scenarios"
        / "checkoutservice-missing-configmap.yaml"
    )
    missing_configmap = load_yaml_documents(missing_configmap_path)[0]
    validate_instance(
        schemas["controlled-missing-configmap-scenario.schema.json"],
        missing_configmap,
        registry,
        "checkoutservice-missing-configmap.yaml",
    )
    if missing_configmap["fault"] != {
        "type": "required-configmap-reference",
        "configmap_name": "checkoutservice-agent-rca-missing",
        "volume_name": "agent-rca-required-config",
        "mount_path": "/var/run/agent-rca-required-config",
        "observation_seconds": 15,
        "maximum_active_seconds": 180,
    }:
        raise ValidationFailure("checkout missing-ConfigMap injection changed")
    missing_configmap_harness = (
        ROOT
        / "automation"
        / "ansible"
        / "roles"
        / "checkout_missing_configmap_fault_harness"
        / "tasks"
        / "main.yml"
    ).read_text(encoding="utf-8")
    missing_configmap_safety_tokens = {
        "confirm_controlled_fault",
        "controlled_fault_environment",
        "agent-rca-missing-configmap-lock",
        "remote fail-safe reference-removal watchdog",
        "Add the preregistered required ConfigMap volume reference",
        "controlled_fault_evaluation",
        "always:",
        "Remove the exact missing ConfigMap volume reference",
        "Require successful exact missing-ConfigMap restoration",
    }
    missing_configmap_safety_gaps = sorted(
        token
        for token in missing_configmap_safety_tokens
        if token not in missing_configmap_harness
    )
    if missing_configmap_safety_gaps:
        raise ValidationFailure(
            "missing-ConfigMap controlled-fault safety boundary is incomplete: "
            f"{missing_configmap_safety_gaps}"
        )

    common_evaluation = (
        ROOT
        / "automation"
        / "ansible"
        / "roles"
        / "controlled_fault_evaluation"
        / "tasks"
        / "main.yml"
    ).read_text(encoding="utf-8")
    common_evaluation_tokens = {
        "Submit the controlled fault through Alertmanager",
        "Wait for a unique controlled-fault Frozen Context Package",
        "Wait for the controlled-fault Agent work to reach a terminal state",
        "Export the completed controlled-fault Incident Evidence snapshot",
        "Build local-only controlled-fault Ground Truth and score artifacts",
        "Require an honestly represented controlled-fault Agent outcome",
    }
    common_evaluation_gaps = sorted(
        token
        for token in common_evaluation_tokens
        if token not in common_evaluation
    )
    if common_evaluation_gaps:
        raise ValidationFailure(
            "shared controlled-fault evaluation boundary is incomplete: "
            f"{common_evaluation_gaps}"
        )

    no_fault_path = (
        ROOT
        / "evaluation"
        / "scenarios"
        / "frontend-no-fault-normal.yaml"
    )
    no_fault = load_yaml_documents(no_fault_path)[0]
    validate_instance(
        schemas["no-fault-control-scenario.schema.json"],
        no_fault,
        registry,
        "frontend-no-fault-normal.yaml",
    )
    if no_fault["change"] != {"type": "none"}:
        raise ValidationFailure("no-fault control applies a runtime change")
    if no_fault["postconditions"] != {
        "all_workloads_ready": True,
        "active_fault_count": 0,
        "deployment_snapshot_unchanged": True,
        "pod_snapshot_unchanged": True,
        "restart_delta_maximum": 0,
        "successful_responses_minimum": 1,
        "successful_response_ratio_minimum": 0.99,
        "transport_error_ratio_maximum": 0.01,
    }:
        raise ValidationFailure("no-fault postcondition SLO changed")
    if no_fault["expected"] != {
        "outcome": "ABSTAIN",
        "root_cause_ids": [],
        "deterministic_rules_not_applicable": True,
        "detected_deployment_changes_maximum": 0,
        "deployment_no_change_evidence_minimum": 1,
        "collector_failures_maximum": 0,
        "minimum_context_completeness": 0.7,
    }:
        raise ValidationFailure("no-fault expected outcome or quality gate changed")
    if (
        no_fault["workload"]["profile"] != "normal"
        or int(no_fault["workload"]["baseline_seconds"]) < 900
        or int(no_fault["workload"]["maximum_duration_seconds"])
        <= int(no_fault["workload"]["baseline_seconds"])
    ):
        raise ValidationFailure(
            "no-fault workload does not cover a full normal baseline window"
        )
    no_fault_harness = (
        ROOT
        / "automation"
        / "ansible"
        / "roles"
        / "no_fault_control_harness"
        / "tasks"
        / "main.yml"
    ).read_text(encoding="utf-8")
    no_fault_safety_tokens = {
        "confirm_no_fault_control",
        "agent-rca-no-fault-control-lock",
        "Fill the complete normal-traffic baseline window",
        "Start the reconnecting controller-to-target SSH tunnel",
        "ConnectionAttempts=1",
        "Require an unchanged baseline before creating the synthetic alert",
        "Build the post-run no-fault control attestation",
        "Require the preregistered no-fault postconditions",
        "Require a correct no-fault Agent abstention",
        "always:",
        "Require every target workload to remain Ready after cleanup",
    }
    missing_no_fault_tokens = sorted(
        token
        for token in no_fault_safety_tokens
        if token not in no_fault_harness
    )
    if missing_no_fault_tokens:
        raise ValidationFailure(
            "no-fault control safety boundary is incomplete: "
            f"{missing_no_fault_tokens}"
        )
    if "agent-rca-no-fault-control-lock" not in harness or (
        "agent-rca-no-fault-control-lock" not in image_pull_harness
    ):
        raise ValidationFailure(
            "controlled faults do not honor the active no-fault control lock"
        )

    no_fault_preregistration = load_yaml_documents(
        ROOT / "evaluation" / "preregistration.yaml"
    )[0]
    no_fault_policy = no_fault_preregistration["evidence_ground_truth"].get(
        "controlled_no_fault", {}
    )
    if no_fault_policy != {
        "expected_outcome": "ABSTAIN",
        "causal_roles": [],
        "causal_precision_recall_applicable": False,
        "require_all_registered_rules_not_applicable": True,
        "require_explicit_deployment_no_changes": True,
        "require_post_run_attestation": True,
    }:
        raise ValidationFailure("no-fault Ground Truth policy changed")

    holdout_preregistration = load_yaml_documents(
        ROOT / "evaluation" / "holdout-v1-preregistration.yaml"
    )[0]
    if (
        holdout_preregistration.get("holdout_id") != "focused-holdout-v1"
        or holdout_preregistration.get("status") != "frozen-unexecuted"
        or holdout_preregistration.get("isolation")
        != {
            "agent_runtime_receives_scenario_manifest": False,
            "agent_runtime_receives_ground_truth": False,
            "ground_truth_join": "post-run-only",
            "holdout_variants_used_for_agent_correction": False,
            "prompt_examples_overlap_allowed": False,
            "cause_revealing_alert_metadata_allowed": False,
        }
        or holdout_preregistration.get("post_result_policy", {}).get(
            "continue_v1_after_agent_prompt_or_gate_change"
        )
        is not False
    ):
        raise ValidationFailure("holdout v1 preregistration or isolation changed")

    structured_output_v1_preregistration = load_yaml_documents(
        ROOT / "evaluation" / "structured-output-v1-preregistration.yaml"
    )[0]
    if (
        structured_output_v1_preregistration.get("evaluation_id")
        != "structured-output-reliability-v1"
        or structured_output_v1_preregistration.get("status")
        != "closed-incomplete"
        or structured_output_v1_preregistration.get("closure")
        != {
            "closed_at": "2026-09-03",
            "reason": "operator-reduced-duration-before-completing-the-matrix",
            "completed_attempts": 3,
            "interrupted_attempt": 4,
            "result_claim_allowed": False,
            "partial_results_used_for_threshold_selection": False,
        }
    ):
        raise ValidationFailure("structured-output v1 closure changed")

    structured_output_preregistration = load_yaml_documents(
        ROOT / "evaluation" / "structured-output-v2-preregistration.yaml"
    )[0]
    structured_boundary = structured_output_preregistration.get(
        "implementation_boundary", {}
    )
    structured_execution = structured_output_preregistration.get("execution", {})
    structured_acceptance = structured_output_preregistration.get("acceptance", {})
    if (
        structured_output_preregistration.get("schema_version") != "1.0.0"
        or structured_output_preregistration.get("evaluation_id")
        != "structured-output-reliability-v2"
        or structured_output_preregistration.get("status")
        != "frozen-unexecuted"
        or structured_output_preregistration.get("purpose", {}).get(
            "accuracy_or_generalization_claim_allowed"
        )
        is not False
        or structured_output_preregistration.get("reuse_disclosure", {}).get(
            "known_scenarios_reused"
        )
        is not True
        or structured_output_preregistration.get("reuse_disclosure", {}).get(
            "combine_with_holdout_accuracy"
        )
        is not False
        or structured_execution.get("planned_attempts") != 8
        or structured_execution.get("repetitions_per_scenario") != 2
        or structured_execution.get("max_parallel") != 1
        or structured_acceptance.get("model_execution_failure_count") != 0
        or structured_acceptance.get("draft_contract_rejection_count") != 0
        or structured_acceptance.get("unsupported_evidence_citation_rate") != 0
        or structured_acceptance.get("evidence_gate_bypass_allowed") is not False
        or structured_boundary.get("sdk_strict_json_schema") is not True
        or structured_boundary.get("evidence_gate_remains_independent") is not True
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(structured_boundary.get("agent_runtime_sha256", "")),
        )
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(structured_boundary.get("frozen_draft_contract_sha256", "")),
        )
    ):
        raise ValidationFailure("structured-output v2 preregistration changed")

    structured_matrix_path = ROOT / "evaluation" / "structured-output-v2-matrix.yaml"
    structured_matrix = load_yaml_documents(structured_matrix_path)[0]
    if (
        structured_matrix.get("matrix_id") != "structured-output-reliability-v2"
        or structured_matrix.get("repetitions_per_scenario") != 2
        or len(structured_matrix.get("scenarios", [])) != 4
        or hashlib.sha256(structured_matrix_path.read_bytes()).hexdigest()
        != structured_output_preregistration.get("reuse_disclosure", {}).get(
            "source_matrix_sha256"
        )
    ):
        raise ValidationFailure("structured-output v2 matrix changed")

    holdout_matrix = load_yaml_documents(
        ROOT / "evaluation" / "holdout-v1-matrix.yaml"
    )[0]
    holdout_scenarios = holdout_matrix.get("scenarios", [])
    if (
        holdout_matrix.get("matrix_id") != "focused-holdout-v1"
        or holdout_matrix.get("repetitions_per_scenario") != 1
        or len(holdout_scenarios) != 12
        or len({item.get("scenario_id") for item in holdout_scenarios}) != 12
    ):
        raise ValidationFailure("holdout v1 matrix is not twelve unique single runs")
    holdout_schema_by_family = {
        "kubernetes.container-oomkilled": "holdout-controlled-oom-scenario.schema.json",
        "kubernetes.image-pull-failure": (
            "holdout-controlled-image-pull-scenario.schema.json"
        ),
        "kubernetes.missing-configmap": (
            "holdout-controlled-missing-configmap-scenario.schema.json"
        ),
        "no-fault": "holdout-no-fault-control-scenario.schema.json",
    }
    variants_by_family = {family: set() for family in holdout_schema_by_family}
    holdout_scenario_root = (
        ROOT / "evaluation" / "scenarios" / "holdout-v1"
    ).resolve()
    for configured in holdout_scenarios:
        family = configured.get("scenario_family")
        if family not in holdout_schema_by_family:
            raise ValidationFailure("holdout v1 matrix contains an unknown family")
        scenario_file = (ROOT / configured["scenario_path"]).resolve()
        try:
            scenario_file.relative_to(holdout_scenario_root)
        except ValueError as error:
            raise ValidationFailure("holdout scenario path escapes its root") from error
        if hashlib.sha256(scenario_file.read_bytes()).hexdigest() != configured.get(
            "scenario_sha256"
        ):
            raise ValidationFailure("holdout scenario digest changed")
        holdout_scenario = load_yaml_documents(scenario_file)[0]
        validate_instance(
            schemas[holdout_schema_by_family[family]],
            holdout_scenario,
            registry,
            configured["scenario_path"],
        )
        if holdout_scenario.get("scenario_id") != configured.get("scenario_id"):
            raise ValidationFailure("holdout scenario identity differs from its matrix")
        variants_by_family[family].add(configured.get("variant_id"))
    if any(
        variants != {"a", "b", "c"} for variants in variants_by_family.values()
    ):
        raise ValidationFailure("holdout v1 family variants are incomplete")


def validate_host_and_chaos_command_boundaries() -> None:
    all_vars = load_yaml_documents(
        ROOT / "automation" / "ansible" / "group_vars" / "all.yml"
    )[0]
    if int(all_vars.get("kubernetes_inotify_max_user_instances", 0)) < 512:
        raise ValidationFailure("Kubernetes host inotify instance capacity is too small")

    host_tasks = (
        ROOT
        / "automation"
        / "ansible"
        / "roles"
        / "host_prerequisites"
        / "tasks"
        / "main.yml"
    ).read_text(encoding="utf-8")
    if (
        "fs.inotify.max_user_instances = "
        "{{ kubernetes_inotify_max_user_instances }}"
        not in host_tasks
    ):
        raise ValidationFailure("Kubernetes host inotify setting is not persisted")

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    for target in ("deploy-chaos-mesh", "verify-chaos-mesh"):
        match = re.search(
            rf"(?m)^{re.escape(target)}:\n(?P<body>(?:\t.*\n)+)",
            makefile,
        )
        if match is None:
            raise ValidationFailure(f"Make target {target} is missing")
        body = match.group("body")
        if (
            "-i $(ANSIBLE_TARGET_INVENTORY)" not in body
            or "-i $(ANSIBLE_INVENTORY)" in body
        ):
            raise ValidationFailure(
                f"Make target {target} can escape the fault-target inventory"
            )


def main() -> None:
    examples = validate_contracts()
    validate_namespaces()
    validate_rbac()
    validate_versions_and_manifests()
    validate_krca_runtime_config()
    validate_stategraph_manifest()
    validate_incident_platform_manifest()
    validate_incident_viewer_frontend_manifest()
    validate_three_domain_runtime()
    validate_policy_configs()
    validate_negative_evidence_reference(examples)
    validate_controlled_fault_scenarios()
    validate_host_and_chaos_command_boundaries()
    print("Phase 0 validation passed:")
    print(f"- {len(schema_registry()[0])} JSON Schemas are structurally valid")
    print("- 6 contract fixture groups are valid")
    print("- cross-contract evidence references are valid")
    print("- namespace and read-only RBAC boundaries are valid")
    print("- GCP self-managed Kubernetes target, readiness gates, and Kustomize pins are consistent")
    print("- opt-in Kubernetes 1.35 Chaos Mesh evaluation boundaries are consistent")
    print("- host inotify capacity and Chaos target inventory boundaries are consistent")
    print("- private three-domain telemetry, RCA control, and fault-target boundaries are consistent")
    print("- private Incident Viewer frontend and same-origin BFF boundaries are consistent")
    print("- routing, Knowledge retrieval, Graph, and Ground Truth policies are frozen")
    print("- the development-only fault scenarios and no-fault control gates are valid")
    print("- holdout v1 isolation, variants, and scenario digests are frozen")
    print("- structured-output v1 closure and shorter v2 boundaries are frozen")
    print("- negative RBAC and invented-evidence checks reject unsafe inputs")


if __name__ == "__main__":
    main()
