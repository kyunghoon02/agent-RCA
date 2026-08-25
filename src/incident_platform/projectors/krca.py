"""Project normalized KRCA API-edge Evidence into the temporal StateGraph."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from ..contracts import validate_contract
from ..errors import ContractViolation
from ..stategraph import (
    EntityIdentity,
    GraphProjection,
    stable_graph_id,
    validate_graph_record,
)


class KRCAPIEdgeEvidenceProjector:
    """Represent one allowlisted API dependency as a logical Service CALLS edge."""

    projector_name = "krca-api-edge-evidence-projector"
    feature_set = "krca-api-edge-v1"
    result_statuses = frozenset({"HAS_DATA", "INSUFFICIENT_DATA"})

    def supports(self, evidence: Mapping[str, Any]) -> bool:
        facts = evidence.get("facts")
        provenance = evidence.get("provenance")
        return (
            evidence.get("source") == "prometheus"
            and evidence.get("kind") == "metric-summary"
            and isinstance(facts, Mapping)
            and facts.get("feature_set") == self.feature_set
            and isinstance(provenance, Mapping)
            and provenance.get("provider")
            == "prometheus-krca-api-feature-provider"
        )

    def project(self, evidence: Mapping[str, Any]) -> GraphProjection:
        validate_contract("evidence-item.schema.json", evidence)
        if not self.supports(evidence):
            raise ContractViolation(
                "KRCAPIEdgeEvidenceProjector requires normalized KRCA API-edge Evidence"
            )
        subject = evidence["subject"]
        facts = evidence["facts"]
        cluster_id = subject.get("cluster_id")
        namespace = subject.get("namespace")
        if not isinstance(cluster_id, str) or not cluster_id:
            raise ContractViolation("KRCA API-edge Evidence requires trusted cluster_id")
        if not isinstance(namespace, str) or not namespace:
            raise ContractViolation("KRCA API-edge Evidence requires namespace")
        if facts.get("result_status") not in self.result_statuses:
            raise ContractViolation("KRCA API-edge result_status is unsupported")
        edge_id = facts.get("edge_id")
        if not isinstance(edge_id, str) or not edge_id:
            raise ContractViolation("KRCA API-edge Evidence requires edge_id")
        parent = self._api_ref(facts.get("parent"), "parent")
        child = self._api_ref(facts.get("child"), "child")
        if subject.get("kind") != "Service" or subject.get("name") != parent[0]:
            raise ContractViolation(
                "KRCA API-edge parent must match the Evidence Service subject"
            )
        if parent == child:
            raise ContractViolation("KRCA API-edge cannot be a self edge")

        parent_entity = self._service_entity(
            cluster_id, namespace, parent[0], evidence
        )
        child_entity = self._service_entity(
            cluster_id, namespace, child[0], evidence
        )
        identity = {
            "source_entity_id": parent_entity["entity_id"],
            "relation_type": "CALLS",
            "destination_entity_id": child_entity["entity_id"],
            "reference_key": f"{edge_id}@{evidence['incident_id']}",
            "projector": self.projector_name,
        }
        relation_key = stable_graph_id("relkey", identity)
        relation = {
            "record_type": "relation_interval",
            "relation_id": stable_graph_id(
                "rel",
                {
                    "relation_key": relation_key,
                    "valid_from": evidence["window"]["start"],
                },
            ),
            "relation_key": relation_key,
            **identity,
            "observed_at": evidence["observed_at"],
            "valid_from": evidence["window"]["start"],
            "valid_to": evidence["window"]["end"],
            "evidence_ids": [evidence["evidence_id"]],
        }
        records = (parent_entity, child_entity, relation)
        for record in records:
            validate_graph_record(record)
        return GraphProjection(records)

    @staticmethod
    def _api_ref(value: Any, field: str) -> Tuple[str, str]:
        if not isinstance(value, Mapping):
            raise ContractViolation(f"KRCA API-edge {field} is malformed")
        service = value.get("service")
        operation = value.get("operation")
        if not isinstance(service, str) or not service:
            raise ContractViolation(f"KRCA API-edge {field} service is malformed")
        if not isinstance(operation, str) or not operation:
            raise ContractViolation(f"KRCA API-edge {field} operation is malformed")
        return service, operation

    @staticmethod
    def _service_entity(
        cluster_id: str,
        namespace: str,
        service_name: str,
        evidence: Mapping[str, Any],
    ) -> Dict[str, Any]:
        identity = EntityIdentity.logical_service(
            cluster_id=cluster_id,
            namespace=namespace,
            service_name=service_name,
        )
        return {
            "record_type": "entity",
            "entity_id": identity.entity_id,
            "identity": identity.to_contract(),
            "entity_type": "Service",
            "domain": "web-service",
            "name": service_name,
            "scope": {"cluster_id": cluster_id, "namespace": namespace},
            "external_ref": f"service://{cluster_id}/{namespace}/{service_name}",
            "exists": True,
            "first_seen_at": evidence["window"]["start"],
            "last_seen_at": evidence["window"]["end"],
            "evidence_ids": [evidence["evidence_id"]],
        }
