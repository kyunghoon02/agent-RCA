"""Checkpoint and completeness rules for one bounded downstream collection."""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Mapping, Sequence

from .contracts import validate_contract
from .errors import InvalidTransition
from .evidence import EvidenceWindow, ResourceScope, format_time, parse_time, redact

LOCALIZATION_COLLECTION_EVENT = "LOCALIZATION_COLLECTION_COMPLETED"
LOCALIZATION_COLLECTORS = frozenset(
    {
        "kubernetes",
        "prometheus",
        "prometheus-workload",
        "loki-kernel-oom",
        "deployment",
        "hubble",
    }
)
MAX_ADDITIONAL_SERVICES = 3


def prepare_localization_collection(
    incident: Mapping[str, Any],
    *,
    selection: Mapping[str, Any],
    window: EvidenceWindow,
    collector_statuses: Sequence[Mapping[str, Any]],
    evidence_items: Sequence[Mapping[str, Any]],
    now: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Validate the whole batch before any write; never erase initial failures."""
    if incident["status"] != "LOCALIZING":
        raise InvalidTransition("supplemental collection requires LOCALIZING")
    services = tuple(selection["services"])
    source = incident["source_entity"]
    if (
        not 1 <= len(services) <= MAX_ADDITIONAL_SERVICES
        or len(set(services)) != len(services)
        or source["name"] in services
        or selection["namespace"] != source["namespace"]
        or not selection["cluster_id"]
        or not selection["profile_id"]
        or not selection["feature_evidence_ids"]
    ):
        raise InvalidTransition("supplemental collection selection is invalid")
    end = parse_time(window.end, "supplemental window end")
    known_end = incident["window"]["recovery_end"] or incident["window"]["incident_end"]
    if (
        parse_time(window.start, "supplemental window start")
        < parse_time(incident["window"]["baseline_start"], "baseline start")
        or end > now
        or (known_end is not None and end > parse_time(known_end, "Incident end"))
    ):
        raise InvalidTransition(
            "supplemental collection window is outside the Incident"
        )
    scope = ResourceScope(
        namespace=source["namespace"],
        resource_names=services,
        resource_name_prefixes=tuple(f"{name}-" for name in services),
        related_resource_kinds=("ConfigMap",),
    )
    candidates = copy.deepcopy([dict(item) for item in evidence_items])
    for item in candidates:
        validate_contract("evidence-item.schema.json", item)
        if (
            item["incident_id"] != incident["incident_id"]
            or item["subject"].get("cluster_id") != selection["cluster_id"]
            or item["subject"].get("namespace") != scope.namespace
            or not scope.contains_evidence_subject(item["subject"], item["facts"])
            or parse_time(item["window"]["start"], "Evidence start")
            < parse_time(window.start, "collection start")
            or parse_time(item["window"]["end"], "Evidence end") > end
        ):
            raise InvalidTransition(
                "supplemental Evidence is outside the selected scope"
            )

    statuses, _ = redact([dict(status) for status in collector_statuses])
    names = [status["collector"] for status in statuses]
    if (
        not names
        or len(names) != len(set(names))
        or not set(names) <= LOCALIZATION_COLLECTORS
        or any(
            status["status"] not in {"SUCCEEDED", "PARTIAL", "FAILED", "TIMED_OUT"}
            for status in statuses
        )
    ):
        raise InvalidTransition("supplemental collector statuses are invalid")
    # Validate each original status too, before computing the aggregate.
    probe = dict(incident, collector_statuses=statuses)
    validate_contract("incident.schema.json", probe)
    merged = {
        item["collector"]: copy.deepcopy(item)
        for item in incident["collector_statuses"]
    }
    for status in statuses:
        name = status["collector"]
        old = merged.get(name)
        if old is None:
            merged[name] = status
            continue
        failures = []
        for stage, item in (("initial", old), ("downstream", status)):
            if item["status"] != "SUCCEEDED":
                failures.append(f"{stage}: {item.get('error') or item['status']}")
        successful = any(
            item["status"] in {"SUCCEEDED", "PARTIAL"} for item in (old, status)
        )
        merged[name] = {
            "collector": name,
            "status": (
                "SUCCEEDED" if not failures else "PARTIAL" if successful else "FAILED"
            ),
            "attempts": old["attempts"] + status["attempts"],
            "started_at": old["started_at"] or status["started_at"],
            "ended_at": status["ended_at"],
            "error": "; ".join(failures) if failures else None,
        }
    updated = copy.deepcopy(dict(incident))
    updated["collector_statuses"] = list(merged.values())
    updated["updated_at"] = format_time(now)
    validate_contract("incident.schema.json", updated)
    details = {
        "selection": copy.deepcopy(dict(selection)),
        "window": {"start": window.start, "end": window.end},
        "collector_statuses": statuses,
        "evidence_ids": sorted(item["evidence_id"] for item in candidates),
    }
    return updated, candidates, details
