"""Project normalized Hubble summaries onto logical Service Entities."""

from __future__ import annotations

from typing import Any, Mapping

from ..contracts import validate_contract
from ..errors import ContractViolation
from ..evidence import parse_time
from ..stategraph import (
    EntityIdentity,
    GraphProjection,
    stable_graph_id,
    validate_graph_record,
)


class HubbleNetworkFlowEvidenceProjector:
    """Attach one allowlisted Hubble flow aggregate to a logical Service."""

    projector_name = "hubble-network-flow-evidence-projector"
    provider_name = "hubble-relay-network-flow-provider"
    feature_set = "hubble-network-flow-summary-v1"
    event_type = "HUBBLE_NETWORK_FLOW_SUMMARY"
    fact_names = frozenset(
        {
            "feature_set",
            "result_status",
            "flow_count",
            "verdict_counts",
            "protocol_counts",
            "drop_reason_counts",
            "source_root_flow_count",
            "destination_root_flow_count",
            "first_flow_at",
            "last_flow_at",
            "truncated",
            "retention_status",
            "reason_codes",
        }
    )

    def supports(self, evidence: Mapping[str, Any]) -> bool:
        subject = evidence.get("subject")
        facts = evidence.get("facts")
        provenance = evidence.get("provenance")
        return (
            evidence.get("source") == "hubble"
            and evidence.get("kind") == "network-flow-summary"
            and isinstance(provenance, Mapping)
            and provenance.get("provider") == self.provider_name
            and isinstance(subject, Mapping)
            and subject.get("kind") == "Service"
            and isinstance(subject.get("cluster_id"), str)
            and bool(subject.get("cluster_id"))
            and isinstance(facts, Mapping)
            and facts.get("feature_set") == self.feature_set
        )

    def project(self, evidence: Mapping[str, Any]) -> GraphProjection:
        validate_contract("evidence-item.schema.json", evidence)
        if not self.supports(evidence):
            raise ContractViolation(
                "HubbleNetworkFlowEvidenceProjector requires trusted Hubble Evidence"
            )
        subject = evidence["subject"]
        facts = evidence["facts"]
        unexpected = set(facts) - self.fact_names
        if unexpected:
            raise ContractViolation(
                "Hubble flow facts are not allowlisted: "
                + ", ".join(sorted(unexpected))
            )
        flow_count = self._non_negative_integer(facts.get("flow_count"), "flow_count")
        source_count = self._non_negative_integer(
            facts.get("source_root_flow_count"), "source_root_flow_count"
        )
        destination_count = self._non_negative_integer(
            facts.get("destination_root_flow_count"),
            "destination_root_flow_count",
        )
        if source_count > flow_count or destination_count > flow_count:
            raise ContractViolation("Hubble root flow count exceeds flow_count")
        verdict_counts = self._count_map(facts.get("verdict_counts"), "verdict_counts")
        protocol_counts = self._count_map(
            facts.get("protocol_counts"), "protocol_counts"
        )
        drop_reason_counts = self._count_map(
            facts.get("drop_reason_counts"), "drop_reason_counts"
        )
        if sum(verdict_counts.values()) != flow_count:
            raise ContractViolation("Hubble verdict counts contradict flow_count")
        if sum(protocol_counts.values()) != flow_count:
            raise ContractViolation("Hubble protocol counts contradict flow_count")
        if sum(drop_reason_counts.values()) > flow_count:
            raise ContractViolation("Hubble drop reason counts exceed flow_count")
        if not isinstance(facts.get("truncated"), bool):
            raise ContractViolation("Hubble truncated flag is malformed")
        reason_codes = facts.get("reason_codes")
        if not isinstance(reason_codes, list) or not all(
            isinstance(item, str) and item for item in reason_codes
        ):
            raise ContractViolation("Hubble reason_codes are malformed")

        result_status = facts.get("result_status")
        if result_status == "HAS_DATA":
            if flow_count <= 0 or facts.get("retention_status") != "NOT_APPLICABLE":
                raise ContractViolation("Hubble HAS_DATA facts are contradictory")
            if reason_codes:
                raise ContractViolation("Hubble HAS_DATA cannot have reason_codes")
            first_seen = parse_time(facts.get("first_flow_at"), "Hubble first_flow_at")
            last_seen = parse_time(facts.get("last_flow_at"), "Hubble last_flow_at")
            if first_seen > last_seen:
                raise ContractViolation("Hubble first_flow_at follows last_flow_at")
        elif result_status == "NO_DATA":
            if (
                flow_count != 0
                or facts.get("first_flow_at") is not None
                or facts.get("last_flow_at") is not None
                or facts.get("retention_status") != "UNKNOWN"
                or reason_codes != ["RETENTION_WINDOW_NOT_PROVABLE"]
                or facts["truncated"]
            ):
                raise ContractViolation("Hubble NO_DATA facts are contradictory")
            first_seen = parse_time(evidence["observed_at"], "Evidence observed_at")
            last_seen = first_seen
        else:
            raise ContractViolation("Hubble result_status is unsupported")

        window_start = parse_time(evidence["window"]["start"], "Evidence window start")
        window_end = parse_time(evidence["window"]["end"], "Evidence window end")
        if first_seen < window_start or last_seen > window_end:
            raise ContractViolation("Hubble flow aggregate is outside the Evidence window")

        identity = EntityIdentity.logical_service(
            cluster_id=subject["cluster_id"],
            namespace=subject["namespace"],
            service_name=subject["name"],
        )
        entity = {
            "record_type": "entity",
            "entity_id": identity.entity_id,
            "identity": identity.to_contract(),
            "entity_type": "Service",
            "domain": "web-service",
            "name": subject["name"],
            "scope": {
                "cluster_id": subject["cluster_id"],
                "namespace": subject["namespace"],
            },
            "external_ref": (
                f"service://{subject['cluster_id']}/{subject['namespace']}/"
                f"{subject['name']}"
            ),
            "exists": bool(subject["exists"]),
            "first_seen_at": evidence["window"]["start"],
            "last_seen_at": evidence["window"]["end"],
            "evidence_ids": [evidence["evidence_id"]],
        }
        event = {
            "record_type": "event_aggregate",
            "event_id": stable_graph_id(
                "evt",
                {
                    "projector": self.projector_name,
                    "evidence_id": evidence["evidence_id"],
                },
            ),
            "entity_id": identity.entity_id,
            "event_type": self.event_type,
            "first_seen_at": evidence["window"]["start"],
            "last_seen_at": evidence["window"]["end"],
            "count": max(1, flow_count),
            "attributes": {name: facts[name] for name in sorted(self.fact_names)},
            "evidence_ids": [evidence["evidence_id"]],
        }
        validate_graph_record(entity)
        validate_graph_record(event)
        return GraphProjection((entity, event))

    @staticmethod
    def _non_negative_integer(value: object, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContractViolation(f"Hubble {field} is malformed")
        return value

    @classmethod
    def _count_map(cls, value: object, field: str) -> Mapping[str, int]:
        if not isinstance(value, Mapping) or not all(
            isinstance(key, str)
            and bool(key)
            and cls._is_non_negative_integer(count)
            for key, count in value.items()
        ):
            raise ContractViolation(f"Hubble {field} is malformed")
        return value

    @staticmethod
    def _is_non_negative_integer(value: object) -> bool:
        return not isinstance(value, bool) and isinstance(value, int) and value >= 0
