"""Project path-scoped network observations into caller/dependency context."""

from typing import Any, Mapping

from ..network_policy import NETWORK_PATH_FEATURE, validate_network_path_evidence
from ..stategraph import GraphProjection, stable_graph_id, validate_graph_record
from .krca import KRCAPIEdgeEvidenceProjector


class NetworkPolicyPathEvidenceProjector:
    def supports(self, evidence: Mapping[str, Any]) -> bool:
        return evidence.get("facts", {}).get("feature_set") == NETWORK_PATH_FEATURE \
            and evidence.get("provenance", {}).get("provider") in {
                "hubble-network-path-provider", "prometheus-network-path-provider"}

    def project(self, evidence: Mapping[str, Any]) -> GraphProjection:
        validate_network_path_evidence(evidence)
        facts, subject = evidence["facts"], evidence["subject"]
        caller = KRCAPIEdgeEvidenceProjector._service_entity(
            subject["cluster_id"], subject["namespace"], facts["source_service"], evidence)
        dependency = KRCAPIEdgeEvidenceProjector._service_entity(
            subject["cluster_id"], subject["namespace"], facts["destination_service"], evidence)
        # CALLS is a configured dependency, not an inference from any observed line.
        identity = {"source_entity_id": caller["entity_id"], "relation_type": "CALLS",
                    "destination_entity_id": dependency["entity_id"],
                    "reference_key": f"{facts['path_id']}@{evidence['incident_id']}:{evidence['source']}",
                    "projector": "network-policy-path-projector"}
        key = stable_graph_id("relkey", identity)
        relation = dict(identity, record_type="relation_interval", relation_key=key,
            relation_id=stable_graph_id("rel", {"key": key, "from": evidence["window"]["start"]}),
            observed_at=evidence["observed_at"], valid_from=evidence["window"]["start"],
            valid_to=evidence["window"]["end"], evidence_ids=[evidence["evidence_id"]])
        event = {"record_type": "event_aggregate", "event_id": stable_graph_id("evt", {
                    "projector": "network-policy-path-projector", "evidence_id": evidence["evidence_id"]}),
                 "entity_id": caller["entity_id"], "event_type": "NETWORK_DEPENDENCY_PATH_OBSERVATION",
                 "first_seen_at": evidence["window"]["start"], "last_seen_at": evidence["window"]["end"],
                 "count": max(1, facts.get("policy_denied_count", facts.get("sample_count", 0))),
                 "attributes": dict(facts), "evidence_ids": [evidence["evidence_id"]]}
        records = (caller, dependency, relation, event)
        for record in records:
            validate_graph_record(record)
        return GraphProjection(records)
