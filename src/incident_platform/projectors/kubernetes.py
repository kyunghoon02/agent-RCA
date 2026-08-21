"""Project normalized Kubernetes Evidence into domain-neutral Graph records."""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Mapping

from ..contracts import validate_contract
from ..errors import ContractViolation
from ..stategraph import (
    EntityIdentity,
    GraphProjection,
    stable_graph_id,
    state_content_hash,
    validate_graph_record,
)


class KubernetesEvidenceProjector:
    """Translate safe EvidenceItems; never inspect raw Kubernetes responses."""

    projector_name = "kubernetes-evidence-projector"
    supported_kinds = frozenset({"resource-state", "kubernetes-event"})
    event_attribute_allowlist = frozenset(
        {
            "type",
            "reason",
            "message_code",
            "message_truncated",
            "source_component",
            "missing_kind",
            "missing_name",
        }
    )

    def supports(self, evidence: Mapping[str, Any]) -> bool:
        """Return whether this projector owns the normalized Evidence kind."""

        return (
            evidence.get("source") == "kubernetes"
            and evidence.get("kind") in self.supported_kinds
        )

    def project(self, evidence: Mapping[str, Any]) -> GraphProjection:
        validate_contract("evidence-item.schema.json", evidence)
        if evidence["source"] != "kubernetes":
            raise ContractViolation(
                "KubernetesEvidenceProjector only accepts Kubernetes Evidence"
            )
        if evidence["kind"] not in self.supported_kinds:
            raise ContractViolation(
                f"unsupported Kubernetes Evidence kind: {evidence['kind']}"
            )

        subject = evidence["subject"]
        entity = self._entity(subject, evidence)
        records: List[Mapping[str, Any]] = [entity]
        if entity["entity_type"] == "Service" and entity["exists"]:
            logical_service = self._logical_service(entity, evidence)
            records.extend(
                [logical_service, self._represented_by(logical_service, entity, evidence)]
            )
        if evidence["kind"] == "resource-state":
            records.append(self._snapshot(entity["entity_id"], evidence))
        else:
            records.append(self._event(entity["entity_id"], evidence))
            records.extend(self._missing_reference_records(entity, evidence))
        for record in records:
            validate_graph_record(record)
        return GraphProjection(tuple(records))

    def _entity(
        self, subject: Mapping[str, Any], evidence: Mapping[str, Any]
    ) -> Dict[str, Any]:
        kind = subject.get("kind")
        name = subject.get("name")
        namespace = subject.get("namespace")
        api_version = subject.get("api_version")
        uid = subject.get("uid")
        cluster_id = subject.get("cluster_id")
        if not isinstance(cluster_id, str) or not cluster_id:
            raise ContractViolation("Kubernetes Evidence subject cluster_id is required")
        if not isinstance(kind, str) or not kind:
            raise ContractViolation("Kubernetes Evidence subject kind is required")
        if not isinstance(name, str) or not name:
            raise ContractViolation("Kubernetes Evidence subject name is required")
        if not isinstance(namespace, str) or not namespace:
            raise ContractViolation("Kubernetes Evidence subject namespace is required")
        if not isinstance(api_version, str) or not api_version:
            raise ContractViolation("Kubernetes Evidence subject api_version is required")
        identity = (
            EntityIdentity.kubernetes_resource(cluster_id=cluster_id, uid=uid)
            if isinstance(uid, str) and uid
            else EntityIdentity.kubernetes_placeholder(
                cluster_id=cluster_id,
                api_version=api_version,
                kind=kind,
                namespace=namespace,
                name=name,
            )
        )
        return {
            "record_type": "entity",
            "entity_id": identity.entity_id,
            "identity": identity.to_contract(),
            "entity_type": kind,
            "domain": "kubernetes",
            "name": name,
            "scope": {
                "cluster_id": cluster_id,
                "namespace": namespace,
                "api_version": api_version,
            },
            "external_ref": uid or f"k8s://{cluster_id}/{namespace}/{kind}/{name}",
            "exists": bool(subject.get("exists")),
            "first_seen_at": evidence["observed_at"],
            "last_seen_at": evidence["observed_at"],
            "evidence_ids": [evidence["evidence_id"]],
        }

    def _logical_service(
        self, resource: Mapping[str, Any], evidence: Mapping[str, Any]
    ) -> Dict[str, Any]:
        cluster_id = resource["scope"]["cluster_id"]
        namespace = resource["scope"]["namespace"]
        identity = EntityIdentity.logical_service(
            cluster_id=cluster_id,
            namespace=namespace,
            service_name=resource["name"],
        )
        return {
            "record_type": "entity",
            "entity_id": identity.entity_id,
            "identity": identity.to_contract(),
            "entity_type": "Service",
            "domain": "web-service",
            "name": resource["name"],
            "scope": {"cluster_id": cluster_id, "namespace": namespace},
            "external_ref": f"service://{cluster_id}/{namespace}/{resource['name']}",
            "exists": True,
            "first_seen_at": evidence["observed_at"],
            "last_seen_at": evidence["observed_at"],
            "evidence_ids": [evidence["evidence_id"]],
        }

    def _represented_by(
        self,
        logical_service: Mapping[str, Any],
        resource: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> Dict[str, Any]:
        identity = {
            "source_entity_id": logical_service["entity_id"],
            "relation_type": "REPRESENTED_BY",
            "destination_entity_id": resource["entity_id"],
            "reference_key": "kubernetes-service",
            "projector": self.projector_name,
        }
        relation_key = stable_graph_id("relkey", identity)
        return {
            "record_type": "relation_interval",
            "relation_id": stable_graph_id(
                "rel",
                {"relation_key": relation_key, "valid_from": evidence["observed_at"]},
            ),
            "relation_key": relation_key,
            **identity,
            "observed_at": evidence["observed_at"],
            "valid_from": evidence["observed_at"],
            "valid_to": None,
            "evidence_ids": [evidence["evidence_id"]],
        }

    def _snapshot(self, entity_id: str, evidence: Mapping[str, Any]) -> Dict[str, Any]:
        state = {
            "exists": bool(evidence["subject"].get("exists")),
            "facts": copy.deepcopy(evidence["facts"]),
        }
        digest = state_content_hash(state)
        return {
            "record_type": "snapshot_interval",
            "snapshot_id": stable_graph_id(
                "snap",
                {
                    "entity_id": entity_id,
                    "valid_from": evidence["observed_at"],
                    "state_hash": digest,
                },
            ),
            "entity_id": entity_id,
            "observed_at": evidence["observed_at"],
            "valid_from": evidence["observed_at"],
            "valid_to": None,
            "state_hash": digest,
            "state": state,
            "evidence_ids": [evidence["evidence_id"]],
        }

    def _event(self, entity_id: str, evidence: Mapping[str, Any]) -> Dict[str, Any]:
        facts = evidence["facts"]
        event_type = facts.get("reason") or facts.get("message_code") or "Unknown"
        if not isinstance(event_type, str):
            event_type = "Unknown"
        count = facts.get("count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            count = 1
        attributes = {
            key: copy.deepcopy(value)
            for key, value in facts.items()
            if key in self.event_attribute_allowlist
        }
        return {
            "record_type": "event_aggregate",
            "event_id": stable_graph_id(
                "evt",
                {
                    "entity_id": entity_id,
                    "event_type": event_type,
                    "locator": evidence["provenance"]["locator"],
                },
            ),
            "entity_id": entity_id,
            "event_type": event_type,
            "first_seen_at": evidence["observed_at"],
            "last_seen_at": evidence["observed_at"],
            "count": count,
            "attributes": attributes,
            "evidence_ids": [evidence["evidence_id"]],
        }

    def _missing_reference_records(
        self,
        source_entity: Mapping[str, Any],
        evidence: Mapping[str, Any],
    ) -> List[Mapping[str, Any]]:
        missing_kind = evidence["facts"].get("missing_kind")
        missing_name = evidence["facts"].get("missing_name")
        if not isinstance(missing_kind, str) or not isinstance(missing_name, str):
            return []
        namespace = evidence["subject"].get("namespace")
        cluster_id = evidence["subject"].get("cluster_id")
        api_version = "v1" if missing_kind in {"ConfigMap", "Secret"} else None
        destination = self._entity(
            {
                "cluster_id": cluster_id,
                "api_version": api_version,
                "kind": missing_kind,
                "namespace": namespace,
                "name": missing_name,
                "uid": None,
                "exists": False,
            },
            evidence,
        )
        missing_state = {
            "exists": False,
            "facts": {
                "result_status": "NOT_FOUND",
                "reported_by_event": True,
            },
        }
        missing_hash = state_content_hash(missing_state)
        snapshot = {
            "record_type": "snapshot_interval",
            "snapshot_id": stable_graph_id(
                "snap",
                {
                    "entity_id": destination["entity_id"],
                    "valid_from": evidence["observed_at"],
                    "state_hash": missing_hash,
                },
            ),
            "entity_id": destination["entity_id"],
            "observed_at": evidence["observed_at"],
            "valid_from": evidence["observed_at"],
            "valid_to": None,
            "state_hash": missing_hash,
            "state": missing_state,
            "evidence_ids": [evidence["evidence_id"]],
        }
        relation_identity = {
            "source_entity_id": source_entity["entity_id"],
            "relation_type": "REFERENCES",
            "destination_entity_id": destination["entity_id"],
            "reference_key": "event-reported-reference",
            "projector": self.projector_name,
        }
        relation_key = stable_graph_id("relkey", relation_identity)
        relation = {
            "record_type": "relation_interval",
            "relation_id": stable_graph_id(
                "rel",
                {
                    "relation_key": relation_key,
                    "valid_from": evidence["observed_at"],
                },
            ),
            "relation_key": relation_key,
            **relation_identity,
            "observed_at": evidence["observed_at"],
            "valid_from": evidence["observed_at"],
            "valid_to": None,
            "evidence_ids": [evidence["evidence_id"]],
        }
        return [destination, snapshot, relation]
