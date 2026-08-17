"""Incident persistence ports and an in-memory contract implementation."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence

from .contracts import validate_contract
from .errors import InvalidTransition


ALLOWED_TRANSITIONS = {
    "RECEIVED": frozenset({"COLLECTING", "FAILED"}),
    "COLLECTING": frozenset({"LOCALIZING", "PARTIAL", "FAILED"}),
    "LOCALIZING": frozenset({"ANALYZING", "PARTIAL", "FAILED"}),
    "ANALYZING": frozenset({"REPORTED", "PARTIAL", "FAILED"}),
    "REPORTED": frozenset(),
    "PARTIAL": frozenset(),
    "FAILED": frozenset(),
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("audit timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class CreateResult:
    incident: Dict[str, Any]
    created: bool


@dataclass(frozen=True)
class AuditEvent:
    incident_id: str
    event_type: str
    occurred_at: str
    details: Mapping[str, Any]


class IncidentRepository(Protocol):
    """Persistence port used by cloud-neutral Incident orchestration."""

    def create_or_get_by_deduplication_key(
        self,
        incident: Mapping[str, Any],
        *,
        occurred_at: Optional[datetime] = None,
    ) -> CreateResult:
        ...

    def record_alert_resolution(
        self,
        incident_id: str,
        *,
        incident_end: str,
        occurred_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        ...

    def get(self, incident_id: str) -> Dict[str, Any]:
        ...

    def transition(
        self,
        incident_id: str,
        *,
        expected_status: str,
        next_status: str,
        occurred_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        ...

    def replace_collector_statuses(
        self,
        incident_id: str,
        collector_statuses: List[Mapping[str, Any]],
        *,
        occurred_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        ...

    def store_evidence(
        self,
        incident_id: str,
        evidence_items: Sequence[Mapping[str, Any]],
    ) -> None:
        ...

    def list_evidence(self, incident_id: str) -> List[Dict[str, Any]]:
        ...


class InMemoryIncidentRepository:
    """Thread-safe test implementation of the IncidentRepository contract.

    It deliberately returns deep copies so callers cannot mutate stored Incident
    state without passing through lifecycle validation.
    """

    def __init__(self) -> None:
        self._incidents: Dict[str, Dict[str, Any]] = {}
        self._incident_id_by_deduplication_key: Dict[str, str] = {}
        self._audit_events: Dict[str, List[AuditEvent]] = {}
        self._evidence_by_incident: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._lock = RLock()

    def create_or_get_by_deduplication_key(
        self,
        incident: Mapping[str, Any],
        *,
        occurred_at: Optional[datetime] = None,
    ) -> CreateResult:
        candidate = copy.deepcopy(dict(incident))
        validate_contract("incident.schema.json", candidate)
        deduplication_key = candidate["deduplication_key"]

        with self._lock:
            existing_id = self._incident_id_by_deduplication_key.get(
                deduplication_key
            )
            if existing_id is not None:
                return CreateResult(copy.deepcopy(self._incidents[existing_id]), False)

            incident_id = candidate["incident_id"]
            if incident_id in self._incidents:
                raise InvalidTransition(
                    f"incident_id {incident_id} already belongs to another deduplication key"
                )

            self._incidents[incident_id] = candidate
            self._incident_id_by_deduplication_key[deduplication_key] = incident_id
            self._audit_events[incident_id] = []
            self._evidence_by_incident[incident_id] = {}
            self._append_audit_event_locked(
                incident_id,
                "INCIDENT_CREATED",
                occurred_at or _utc_now(),
                {"status": candidate["status"]},
            )
            return CreateResult(copy.deepcopy(candidate), True)

    def get(self, incident_id: str) -> Dict[str, Any]:
        with self._lock:
            try:
                return copy.deepcopy(self._incidents[incident_id])
            except KeyError as error:
                raise KeyError(f"unknown incident: {incident_id}") from error

    def transition(
        self,
        incident_id: str,
        *,
        expected_status: str,
        next_status: str,
        occurred_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        transition_time = occurred_at or _utc_now()
        with self._lock:
            incident = self._require_incident_locked(incident_id)
            current_status = incident["status"]
            if current_status != expected_status:
                raise InvalidTransition(
                    f"stale transition for {incident_id}: expected {expected_status}, "
                    f"found {current_status}"
                )
            if next_status not in ALLOWED_TRANSITIONS[current_status]:
                raise InvalidTransition(
                    f"transition {current_status} -> {next_status} is not allowed"
                )

            updated = copy.deepcopy(incident)
            updated["status"] = next_status
            updated["updated_at"] = _format_time(transition_time)
            validate_contract("incident.schema.json", updated)
            self._incidents[incident_id] = updated
            self._append_audit_event_locked(
                incident_id,
                "STATUS_TRANSITIONED",
                transition_time,
                {"from": current_status, "to": next_status},
            )
            return copy.deepcopy(updated)

    def replace_collector_statuses(
        self,
        incident_id: str,
        collector_statuses: List[Mapping[str, Any]],
        *,
        occurred_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        update_time = occurred_at or _utc_now()
        statuses = copy.deepcopy([dict(status) for status in collector_statuses])
        collector_names = [status.get("collector") for status in statuses]
        if len(collector_names) != len(set(collector_names)):
            raise InvalidTransition("collector status names must be unique")
        with self._lock:
            incident = self._require_incident_locked(incident_id)
            if incident["status"] != "COLLECTING":
                raise InvalidTransition(
                    "collector statuses may only be replaced while COLLECTING"
                )
            updated = copy.deepcopy(incident)
            updated["collector_statuses"] = statuses
            updated["updated_at"] = _format_time(update_time)
            validate_contract("incident.schema.json", updated)
            self._incidents[incident_id] = updated
            self._append_audit_event_locked(
                incident_id,
                "COLLECTION_COMPLETED",
                update_time,
                {
                    "collector_statuses": {
                        status["collector"]: status["status"] for status in statuses
                    }
                },
            )
            return copy.deepcopy(updated)

    def store_evidence(
        self,
        incident_id: str,
        evidence_items: Sequence[Mapping[str, Any]],
    ) -> None:
        candidates = [copy.deepcopy(dict(item)) for item in evidence_items]
        for candidate in candidates:
            validate_contract("evidence-item.schema.json", candidate)
            if candidate["incident_id"] != incident_id:
                raise InvalidTransition(
                    "Evidence incident_id does not match repository target"
                )
        with self._lock:
            self._require_incident_locked(incident_id)
            stored = self._evidence_by_incident[incident_id]
            for candidate in candidates:
                evidence_id = candidate["evidence_id"]
                previous = stored.get(evidence_id)
                if previous is not None and previous != candidate:
                    raise InvalidTransition(
                        f"evidence_id collision with different content: {evidence_id}"
                    )
                stored[evidence_id] = candidate

    def list_evidence(self, incident_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            self._require_incident_locked(incident_id)
            return copy.deepcopy(
                list(self._evidence_by_incident[incident_id].values())
            )

    def record_alert_resolution(
        self,
        incident_id: str,
        *,
        incident_end: str,
        occurred_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        resolution_time = occurred_at or _utc_now()
        with self._lock:
            incident = self._require_incident_locked(incident_id)
            updated = copy.deepcopy(incident)
            previous_end = updated["window"]["incident_end"]
            if previous_end is not None and previous_end != incident_end:
                raise InvalidTransition(
                    f"incident {incident_id} already has a different incident_end"
                )
            updated["window"]["incident_end"] = incident_end
            updated["updated_at"] = _format_time(resolution_time)
            validate_contract("incident.schema.json", updated)
            self._incidents[incident_id] = updated
            if previous_end is None:
                self._append_audit_event_locked(
                    incident_id,
                    "ALERT_RESOLVED",
                    resolution_time,
                    {"incident_end": incident_end},
                )
            return copy.deepcopy(updated)

    def list_audit_events(self, incident_id: str) -> List[AuditEvent]:
        with self._lock:
            self._require_incident_locked(incident_id)
            return copy.deepcopy(self._audit_events[incident_id])

    def count(self) -> int:
        with self._lock:
            return len(self._incidents)

    def _require_incident_locked(self, incident_id: str) -> Dict[str, Any]:
        try:
            return self._incidents[incident_id]
        except KeyError as error:
            raise KeyError(f"unknown incident: {incident_id}") from error

    def _append_audit_event_locked(
        self,
        incident_id: str,
        event_type: str,
        occurred_at: datetime,
        details: Mapping[str, Any],
    ) -> None:
        self._audit_events[incident_id].append(
            AuditEvent(
                incident_id=incident_id,
                event_type=event_type,
                occurred_at=_format_time(occurred_at),
                details=copy.deepcopy(dict(details)),
            )
        )
