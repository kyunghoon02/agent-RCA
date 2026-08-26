"""Project normalized Deployment revision Evidence into StateGraph events."""

from __future__ import annotations

from typing import Any, Dict, Mapping

from ..contracts import validate_contract
from ..errors import ContractViolation
from ..stategraph import (
    EntityIdentity,
    GraphProjection,
    stable_graph_id,
    validate_graph_record,
)


class DeploymentChangeEvidenceProjector:
    """Attach a bounded Deployment change or absence event to its K8s Entity."""

    projector_name = "deployment-change-evidence-projector"
    supported_statuses = frozenset(
        {
            "CHANGE_DETECTED",
            "NO_CHANGES",
            "HISTORY_INCOMPLETE",
            "DEPLOYMENT_NOT_FOUND",
        }
    )
    attribute_allowlist = frozenset(
        {
            "result_status",
            "revision",
            "previous_revision",
            "current_revision",
            "replica_set",
            "changed_fields",
            "retained_revision_count",
            "window_change_count",
            "history_source",
        }
    )

    def supports(self, evidence: Mapping[str, Any]) -> bool:
        subject = evidence.get("subject")
        provenance = evidence.get("provenance")
        return (
            evidence.get("source") == "deployment"
            and evidence.get("kind") == "deployment-change"
            and isinstance(provenance, Mapping)
            and provenance.get("provider") == "kubernetes-deployment-history"
            and isinstance(subject, Mapping)
            and subject.get("api_version") == "apps/v1"
            and subject.get("kind") == "Deployment"
            and isinstance(subject.get("cluster_id"), str)
            and bool(subject.get("cluster_id"))
        )

    def project(self, evidence: Mapping[str, Any]) -> GraphProjection:
        validate_contract("evidence-item.schema.json", evidence)
        if not self.supports(evidence):
            raise ContractViolation(
                "DeploymentChangeEvidenceProjector requires trusted Kubernetes "
                "Deployment history Evidence"
            )
        subject = evidence["subject"]
        facts = evidence["facts"]
        status = facts.get("result_status")
        if status not in self.supported_statuses:
            raise ContractViolation("Deployment change result_status is unsupported")
        self._validate_facts(status, facts, subject)
        changed_fields = facts.get("changed_fields")
        if changed_fields is not None and (
            not isinstance(changed_fields, list)
            or not all(isinstance(item, str) and item for item in changed_fields)
        ):
            raise ContractViolation("Deployment changed_fields is malformed")
        if status == "CHANGE_DETECTED" and not changed_fields:
            raise ContractViolation("Deployment change requires changed_fields")
        if status != "CHANGE_DETECTED" and changed_fields is not None:
            raise ContractViolation("Deployment absence cannot declare changed_fields")

        entity = self._entity(subject, evidence)
        event_type = (
            "DEPLOYMENT_CHANGE"
            if status == "CHANGE_DETECTED"
            else "DEPLOYMENT_CHANGE_ABSENCE"
        )
        event = {
            "record_type": "event_aggregate",
            "event_id": stable_graph_id(
                "evt",
                {
                    "projector": self.projector_name,
                    "evidence_id": evidence["evidence_id"],
                },
            ),
            "entity_id": entity["entity_id"],
            "event_type": event_type,
            "first_seen_at": evidence["observed_at"],
            "last_seen_at": evidence["observed_at"],
            "count": 1,
            "attributes": self._attributes(facts),
            "evidence_ids": [evidence["evidence_id"]],
        }
        validate_graph_record(entity)
        validate_graph_record(event)
        return GraphProjection((entity, event))

    @staticmethod
    def _validate_facts(
        status: str,
        facts: Mapping[str, Any],
        subject: Mapping[str, Any],
    ) -> None:
        if facts.get("history_source") != "kubernetes-replicaset":
            raise ContractViolation("Deployment history_source is unsupported")
        retained_count = facts.get("retained_revision_count")
        if (
            isinstance(retained_count, bool)
            or not isinstance(retained_count, int)
            or retained_count < 0
        ):
            raise ContractViolation("Deployment retained revision count is malformed")
        if status == "CHANGE_DETECTED":
            revision = facts.get("revision")
            previous_revision = facts.get("previous_revision")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision <= 0
            ):
                raise ContractViolation("Deployment revision is malformed")
            if previous_revision is not None and (
                isinstance(previous_revision, bool)
                or not isinstance(previous_revision, int)
                or previous_revision <= 0
                or previous_revision >= revision
            ):
                raise ContractViolation("Deployment previous revision is malformed")
            if not isinstance(facts.get("replica_set"), str) or not facts.get(
                "replica_set"
            ):
                raise ContractViolation("Deployment ReplicaSet name is malformed")
            if subject.get("exists") is not True:
                raise ContractViolation("Deployment change requires an existing subject")
            return
        if facts.get("window_change_count") != 0:
            raise ContractViolation("Deployment absence must declare zero window changes")
        current_revision = facts.get("current_revision")
        if current_revision is not None and (
            isinstance(current_revision, bool)
            or not isinstance(current_revision, int)
            or current_revision <= 0
        ):
            raise ContractViolation("Deployment current revision is malformed")
        if status == "DEPLOYMENT_NOT_FOUND" and subject.get("exists") is not False:
            raise ContractViolation("Missing Deployment subject must not exist")

    @staticmethod
    def _entity(
        subject: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> Dict[str, Any]:
        cluster_id = subject["cluster_id"]
        namespace = subject.get("namespace")
        name = subject.get("name")
        uid = subject.get("uid")
        if not isinstance(namespace, str) or not namespace:
            raise ContractViolation("Deployment namespace is required")
        if not isinstance(name, str) or not name:
            raise ContractViolation("Deployment name is required")
        identity = (
            EntityIdentity.kubernetes_resource(cluster_id=cluster_id, uid=uid)
            if isinstance(uid, str) and uid
            else EntityIdentity.kubernetes_placeholder(
                cluster_id=cluster_id,
                api_version="apps/v1",
                kind="Deployment",
                namespace=namespace,
                name=name,
            )
        )
        return {
            "record_type": "entity",
            "entity_id": identity.entity_id,
            "identity": identity.to_contract(),
            "entity_type": "Deployment",
            "domain": "kubernetes",
            "name": name,
            "scope": {
                "cluster_id": cluster_id,
                "namespace": namespace,
                "api_version": "apps/v1",
            },
            "external_ref": uid or f"k8s://{cluster_id}/{namespace}/Deployment/{name}",
            "exists": bool(subject.get("exists")),
            "first_seen_at": evidence["observed_at"],
            "last_seen_at": evidence["observed_at"],
            "evidence_ids": [evidence["evidence_id"]],
        }

    def _attributes(self, facts: Mapping[str, Any]) -> Dict[str, Any]:
        attributes: Dict[str, Any] = {}
        for name, value in facts.items():
            if name in {"before", "after", "occurred_at"}:
                continue
            if name not in self.attribute_allowlist:
                raise ContractViolation(
                    f"Deployment change fact is not allowlisted: {name}"
                )
            attributes[name] = value
        return attributes
