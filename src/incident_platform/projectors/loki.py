"""Project normalized Loki kernel OOM Evidence onto UID-backed Pod Entities."""

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


class LokiKernelOOMEvidenceProjector:
    """Attach one allowlisted kernel cgroup OOM aggregate to its exact Pod UID."""

    projector_name = "loki-kernel-oom-evidence-projector"
    event_type = "KERNEL_CGROUP_OOM"
    provider_name = "loki-kernel-oom-provider"
    pattern_id = "kernel-cgroup-oom"
    fact_names = frozenset(
        {
            "pattern_id",
            "kernel_constraint",
            "match_count",
            "pod_uid",
            "first_match_at",
            "last_match_at",
        }
    )

    def supports(self, evidence: Mapping[str, Any]) -> bool:
        subject = evidence.get("subject")
        facts = evidence.get("facts")
        provenance = evidence.get("provenance")
        return (
            evidence.get("source") == "loki"
            and evidence.get("kind") == "log-pattern"
            and isinstance(provenance, Mapping)
            and provenance.get("provider") == self.provider_name
            and isinstance(subject, Mapping)
            and subject.get("kind") == "Pod"
            and isinstance(subject.get("cluster_id"), str)
            and bool(subject.get("cluster_id"))
            and isinstance(subject.get("uid"), str)
            and bool(subject.get("uid"))
            and isinstance(facts, Mapping)
            and facts.get("pattern_id") == self.pattern_id
        )

    def project(self, evidence: Mapping[str, Any]) -> GraphProjection:
        validate_contract("evidence-item.schema.json", evidence)
        if not self.supports(evidence):
            raise ContractViolation(
                "LokiKernelOOMEvidenceProjector requires trusted UID-backed "
                "kernel cgroup OOM Evidence"
            )
        subject = evidence["subject"]
        facts = evidence["facts"]
        unexpected = set(facts) - self.fact_names
        if unexpected:
            raise ContractViolation(
                "Loki kernel OOM facts are not allowlisted: "
                + ", ".join(sorted(unexpected))
            )
        if facts.get("kernel_constraint") != "CONSTRAINT_MEMCG":
            raise ContractViolation("Loki kernel OOM constraint is unsupported")
        if facts.get("pod_uid") != subject["uid"]:
            raise ContractViolation(
                "Loki kernel OOM fact UID disagrees with the Evidence subject"
            )
        match_count = facts.get("match_count")
        if (
            isinstance(match_count, bool)
            or not isinstance(match_count, int)
            or match_count < 1
        ):
            raise ContractViolation("Loki kernel OOM match_count is malformed")
        first_seen = parse_time(
            facts.get("first_match_at"), "Loki kernel OOM first_match_at"
        )
        last_seen = parse_time(
            facts.get("last_match_at"), "Loki kernel OOM last_match_at"
        )
        if first_seen > last_seen:
            raise ContractViolation(
                "Loki kernel OOM first_match_at follows last_match_at"
            )
        window_start = parse_time(
            evidence["window"]["start"], "Evidence window start"
        )
        window_end = parse_time(evidence["window"]["end"], "Evidence window end")
        if first_seen < window_start or last_seen > window_end:
            raise ContractViolation(
                "Loki kernel OOM aggregate is outside the Evidence window"
            )

        identity = EntityIdentity.kubernetes_resource(
            cluster_id=subject["cluster_id"],
            uid=subject["uid"],
        )
        entity = {
            "record_type": "entity",
            "entity_id": identity.entity_id,
            "identity": identity.to_contract(),
            "entity_type": "Pod",
            "domain": "kubernetes",
            "name": subject["name"],
            "scope": {
                "cluster_id": subject["cluster_id"],
                "namespace": subject["namespace"],
                "api_version": subject["api_version"],
            },
            "external_ref": subject["uid"],
            "exists": bool(subject["exists"]),
            "first_seen_at": facts["first_match_at"],
            "last_seen_at": facts["last_match_at"],
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
            "first_seen_at": facts["first_match_at"],
            "last_seen_at": facts["last_match_at"],
            "count": match_count,
            "attributes": {
                "pattern_id": facts["pattern_id"],
                "kernel_constraint": facts["kernel_constraint"],
            },
            "evidence_ids": [evidence["evidence_id"]],
        }
        validate_graph_record(entity)
        validate_graph_record(event)
        return GraphProjection((entity, event))
