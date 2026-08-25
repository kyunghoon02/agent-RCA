"""Project normalized Prometheus summaries onto logical Service Entities."""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping

from ..contracts import validate_contract
from ..errors import ContractViolation
from ..stategraph import (
    EntityIdentity,
    GraphProjection,
    stable_graph_id,
    validate_graph_record,
)


class PrometheusMetricEvidenceProjector:
    """Attach one bounded metric summary to its logical Service as an Event."""

    projector_name = "prometheus-metric-evidence-projector"
    event_type = "PROMETHEUS_METRIC_SUMMARY"
    result_statuses = frozenset(
        {"HAS_DATA", "NO_DATA", "NO_DATA_WITHIN_LIMIT"}
    )
    scalar_fact_names = frozenset(
        {"minimum", "maximum", "average", "latest"}
    )
    peak_fact_name = re.compile(r"^peak_[a-z][a-z0-9_]{1,63}$")

    def supports(self, evidence: Mapping[str, Any]) -> bool:
        subject = evidence.get("subject")
        facts = evidence.get("facts")
        provenance = evidence.get("provenance")
        return (
            evidence.get("source") == "prometheus"
            and evidence.get("kind") == "metric-summary"
            and isinstance(provenance, Mapping)
            and provenance.get("provider") == "prometheus-http-api"
            and isinstance(subject, Mapping)
            and isinstance(subject.get("cluster_id"), str)
            and bool(subject.get("cluster_id"))
            and subject.get("kind") == "Service"
            and isinstance(facts, Mapping)
            and isinstance(facts.get("metric"), str)
            and bool(facts.get("metric"))
            and "feature_set" not in facts
        )

    def project(self, evidence: Mapping[str, Any]) -> GraphProjection:
        validate_contract("evidence-item.schema.json", evidence)
        if not self.supports(evidence):
            raise ContractViolation(
                "PrometheusMetricEvidenceProjector requires a trusted "
                "cluster-scoped Service metric summary"
            )
        subject = evidence["subject"]
        facts = evidence["facts"]
        result_status = facts.get("result_status")
        if result_status not in self.result_statuses:
            raise ContractViolation(
                "Prometheus metric summary result_status is unsupported"
            )
        sample_count = facts.get("sample_count")
        if (
            isinstance(sample_count, bool)
            or not isinstance(sample_count, int)
            or sample_count < 0
        ):
            raise ContractViolation(
                "Prometheus metric summary sample_count is malformed"
            )
        if (result_status == "HAS_DATA") != (sample_count > 0):
            raise ContractViolation(
                "Prometheus metric summary sample_count contradicts result_status"
            )

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
                f"service://{subject['cluster_id']}/"
                f"{subject['namespace']}/{subject['name']}"
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
            "count": max(1, sample_count),
            "attributes": self._attributes(facts),
            "evidence_ids": [evidence["evidence_id"]],
        }
        validate_graph_record(entity)
        validate_graph_record(event)
        return GraphProjection((entity, event))

    def _attributes(self, facts: Mapping[str, Any]) -> Dict[str, Any]:
        attributes: Dict[str, Any] = {
            "metric": facts["metric"],
            "result_status": facts["result_status"],
            "sample_count": facts["sample_count"],
        }
        for name, value in facts.items():
            if name in {"metric", "result_status", "sample_count"}:
                continue
            if name not in self.scalar_fact_names and not self.peak_fact_name.fullmatch(
                name
            ):
                raise ContractViolation(
                    f"Prometheus metric summary fact is not allowlisted: {name}"
                )
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ContractViolation(
                    f"Prometheus metric summary {name} is malformed"
                )
            attributes[name] = value
        return attributes
