"""Prepare validated initial collection results without publishing mutations."""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Mapping, Sequence

from .contracts import validate_contract
from .errors import ContractViolation, InvalidTransition
from .evidence import format_time


def prepare_collection_commit(
    incident: Mapping[str, Any],
    *,
    collector_statuses: Sequence[Mapping[str, Any]],
    evidence_items: Sequence[Mapping[str, Any]],
    outcome: str,
    now: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Validate and copy the whole result before either adapter writes it."""
    if now.tzinfo is None or outcome not in {"SUCCEEDED", "PARTIAL", "FAILED"}:
        raise ValueError("collection completion metadata is invalid")
    if incident["status"] != "COLLECTING":
        raise InvalidTransition("collection commit requires a COLLECTING Incident")
    updated = copy.deepcopy(dict(incident))
    statuses = copy.deepcopy([dict(item) for item in collector_statuses])
    candidates = copy.deepcopy([dict(item) for item in evidence_items])
    updated["collector_statuses"] = statuses
    updated["status"] = "FAILED" if outcome == "FAILED" else "LOCALIZING"
    updated["updated_at"] = format_time(now)
    try:
        validate_contract("incident.schema.json", updated)
        for candidate in candidates:
            validate_contract("evidence-item.schema.json", candidate)
    except ContractViolation:
        raise ContractViolation("collection result violates its frozen contract") from None
    names = [status["collector"] for status in statuses]
    if len(names) != len(set(names)):
        raise InvalidTransition("collector status names must be unique")
    if any(item["incident_id"] != incident["incident_id"] for item in candidates):
        raise InvalidTransition("Evidence incident_id does not match collection target")
    details = {"collector_statuses": {item["collector"]: item["status"] for item in statuses}}
    return updated, candidates, details
