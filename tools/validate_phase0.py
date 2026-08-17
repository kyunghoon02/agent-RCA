#!/usr/bin/env python3
"""Validate Phase 0 contracts, fixtures, manifests, and frozen decisions."""

from __future__ import annotations

import copy
import json
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
    for document in documents:
        annotations = document.get("metadata", {}).get("annotations", {})
        gke_annotations = [
            key for key in annotations if key.startswith("policy.network.gke.io/")
        ]
        if gke_annotations:
            raise ValidationFailure(
                f"active namespace contains GKE-only annotations: {gke_annotations}"
            )


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
        "cloud": "kt-cloud",
        "zone": "DX-M1",
    }
    if versions.get("execution_target") != expected_execution_target:
        raise ValidationFailure("platform version boundary must target KT Cloud DX-M1")
    expected_kubernetes_boundary = {
        "mode": "self-managed-on-vm",
        "distribution": "pending-verification",
        "bootstrap_owner": "ansible",
        "cni": "cilium",
        "cilium_version": "pending-verification",
        "network_observability": "hubble",
    }
    if versions.get("kubernetes") != expected_kubernetes_boundary:
        raise ValidationFailure(
            "Kubernetes boundary must remain self-managed with Ansible, Cilium, and Hubble"
        )
    if "gke" in versions or "google_provider" in versions.get("terraform", {}):
        raise ValidationFailure("active platform versions still contain a GKE boundary")
    if versions["terraform"].get("provider_selection") != (
        "pending-capability-verification"
    ):
        raise ValidationFailure(
            "Terraform provider must remain capability-gated until KT Cloud verification"
        )

    scope = load_yaml_documents(ROOT / "config" / "project-scope.yaml")[0]
    expected_scope_target = {
        **expected_execution_target,
        "provisioning_gate": "capability-verification-required",
        "kubernetes_mode": "self-managed-on-vm",
        "bootstrap_owner": "ansible",
        "cni": "cilium",
        "network_observability": "hubble",
    }
    if scope.get("execution_target") != expected_scope_target:
        raise ValidationFailure("project scope and platform KT Cloud boundaries disagree")
    forbidden_gke_scope_values = {
        "gke-network-policy-logs",
        "always-on-gke",
        "regional-gke",
    }
    scope_values = set(scope.get("secondary_evidence", [])) | set(
        scope.get("excluded_from_mvp", [])
    )
    stale_scope_values = forbidden_gke_scope_values & scope_values
    if stale_scope_values:
        raise ValidationFailure(
            f"active project scope still contains GKE-only values: {sorted(stale_scope_values)}"
        )

    capabilities = load_yaml_documents(
        ROOT / "config" / "kt-cloud-capabilities.yaml"
    )[0]
    expected_capability_decision = {
        **expected_execution_target,
        "status": "project-confirmed",
        "confirmed_at": "2026-08-14",
        "kubernetes_mode": "self-managed-on-vm",
        "bootstrap_owner": "ansible",
        "cni": "cilium",
        "network_observability": "hubble",
    }
    if capabilities.get("decision") != expected_capability_decision:
        raise ValidationFailure("KT Cloud capability decision does not match the target")

    required_capabilities = {
        "tenant_openstack_identity_api",
        "terraform_openstack_provider_compatibility",
        "compute_api",
        "network_api",
        "block_storage_api",
        "remote_state_backend",
    }
    recorded_capabilities = capabilities.get("capabilities", {})
    missing_capabilities = required_capabilities - set(recorded_capabilities)
    if missing_capabilities:
        raise ValidationFailure(
            f"KT Cloud capability matrix is incomplete: {sorted(missing_capabilities)}"
        )
    unresolved_capabilities = {
        capability
        for capability in required_capabilities
        if recorded_capabilities[capability].get("status") != "verified"
    }
    terraform_gate = capabilities.get("gates", {}).get(
        "terraform_implementation", {}
    )
    if unresolved_capabilities and terraform_gate.get("status") != "blocked":
        raise ValidationFailure(
            "Terraform implementation gate must stay blocked while capabilities are unresolved"
        )
    if set(terraform_gate.get("required_capabilities", [])) != required_capabilities:
        raise ValidationFailure(
            "Terraform implementation gate does not cover every required capability"
        )

    automation_requirements = {
        name: version
        for name, version in (
            line.strip().split("==", maxsplit=1)
            for line in (
                ROOT / "automation" / "ansible" / "requirements.txt"
            ).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    }
    expected_automation_requirements = {
        "ansible-core": versions["automation"]["ansible_core"],
        "kubernetes": versions["automation"]["kubernetes_python_client"],
    }
    for package, expected_version in expected_automation_requirements.items():
        if automation_requirements.get(package) != expected_version:
            raise ValidationFailure(
                f"{package} version mismatch: expected {expected_version}, "
                f"got {automation_requirements.get(package)}"
            )

    collection_requirements = load_yaml_documents(
        ROOT / "automation" / "ansible" / "collections" / "requirements.yml"
    )[0]
    kubernetes_collection = next(
        collection
        for collection in collection_requirements["collections"]
        if collection["name"] == "kubernetes.core"
    )
    if (
        kubernetes_collection["version"]
        != versions["automation"]["kubernetes_collection"]
    ):
        raise ValidationFailure(
            "kubernetes.core collection version does not match platform/versions.yaml"
        )

    boutique = load_yaml_documents(
        ROOT / "platform" / "online-boutique" / "kustomization.yaml"
    )[0]
    expected_remote = versions["online_boutique"]["remote_kustomize_base"]
    if boutique.get("resources") != [expected_remote]:
        raise ValidationFailure("Online Boutique remote base is not pinned to the recorded SHA")
    if boutique.get("namespace") != "online-boutique":
        raise ValidationFailure("Online Boutique namespace is not fixed")

    deleted_targets = {
        (patch["target"]["kind"], patch["target"]["name"])
        for patch in boutique.get("patches", [])
        if "$patch: delete" in patch.get("patch", "")
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

    if scope["target"]["release_tag"] != versions["online_boutique"]["release_tag"]:
        raise ValidationFailure("project scope and version pin disagree on release tag")
    if scope["target"]["commit_sha"] != versions["online_boutique"]["commit_sha"]:
        raise ValidationFailure("project scope and version pin disagree on commit SHA")


def validate_policy_configs() -> None:
    routing = load_yaml_documents(ROOT / "config" / "rca-routing.yaml")[0]
    if routing["preconditions"]["ground_truth_access_allowed"]:
        raise ValidationFailure("RCA routing allows Ground Truth access")
    if routing["preconditions"]["write_tools_allowed"]:
        raise ValidationFailure("RCA routing allows write tools")
    if routing["fast_path"]["llm_calls"] != 0:
        raise ValidationFailure("Fast Path must not call an LLM")
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
    validate_policy_configs()
    validate_negative_evidence_reference(examples)
    print("Phase 0 validation passed:")
    print("- 6 JSON Schemas are structurally valid")
    print("- 5 contract fixture groups are valid")
    print("- cross-contract evidence references are valid")
    print("- namespace and read-only RBAC boundaries are valid")
    print("- KT Cloud target/capability gate, automation dependencies, and Kustomize pins are consistent")
    print("- routing, evaluation, Graph, and Ground Truth policies are frozen")
    print("- negative RBAC and invented-evidence checks reject unsafe inputs")


if __name__ == "__main__":
    main()
