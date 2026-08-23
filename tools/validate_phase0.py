#!/usr/bin/env python3
"""Validate Phase 0 contracts, fixtures, manifests, and frozen decisions."""

from __future__ import annotations

import copy
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
        "hubble": {"cli_version": "v1.19.3"},
    }
    for boundary, expected in expected_runtime_versions.items():
        if versions.get(boundary) != expected:
            raise ValidationFailure(
                f"platform runtime version boundary drifted for {boundary}"
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
            "controller_type": "deployment",
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
        if env != {
            "COLLECTOR_SERVICE_ADDR": "opentelemetrycollector:4317",
            "OTEL_SERVICE_NAME": service,
            "ENABLE_TRACING": "1",
        }:
            raise ValidationFailure(
                f"Online Boutique OTel environment drifted for {service}"
            )

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
    if set(pipelines.get("traces", {}).get("exporters", [])) != {
        "otlp/tempo",
        "span_metrics",
        "service_graph",
    }:
        raise ValidationFailure("Online Boutique trace pipeline is incomplete")
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
    }
    actual_recording_rules = {
        rule.get("record") for rule in groups[0].get("rules", [])
    }
    if actual_recording_rules != expected_recording_rules:
        raise ValidationFailure("Online Boutique KRCA recording rule set drifted")

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
        "checkout-payment",
        "recommendation-catalog",
    }
    profile_ids = {profile["profile_id"] for profile in runtime["profiles"]}
    if profile_ids != expected_profiles:
        raise ValidationFailure("Online Boutique live KRCA profiles drifted")


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
        alloy.get("controller", {}).get("type") != "deployment"
        or alloy.get("controller", {}).get("replicas") != 1
        or alloy.get("service", {}).get("type") != "ClusterIP"
    ):
        raise ValidationFailure("Alloy deployment/private service boundary drifted")
    alloy_config = alloy.get("alloy", {}).get("configMap", {}).get("content", "")
    if (
        'loki.source.kubernetes "pods"' not in alloy_config
        or 'replacement  = "agent-rca-dev"' not in alloy_config
    ):
        raise ValidationFailure("Alloy Kubernetes log normalization drifted")
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


def main() -> None:
    examples = validate_contracts()
    validate_namespaces()
    validate_rbac()
    validate_versions_and_manifests()
    validate_krca_runtime_config()
    validate_policy_configs()
    validate_negative_evidence_reference(examples)
    print("Phase 0 validation passed:")
    print(f"- {len(schema_registry()[0])} JSON Schemas are structurally valid")
    print("- 6 contract fixture groups are valid")
    print("- cross-contract evidence references are valid")
    print("- namespace and read-only RBAC boundaries are valid")
    print("- GCP self-managed Kubernetes target, readiness gates, and Kustomize pins are consistent")
    print("- private observability and live KRCA metric profiles are consistent")
    print("- routing, Knowledge retrieval, Graph, and Ground Truth policies are frozen")
    print("- negative RBAC and invented-evidence checks reject unsafe inputs")


if __name__ == "__main__":
    main()
