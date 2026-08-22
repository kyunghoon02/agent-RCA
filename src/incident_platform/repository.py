"""Incident persistence ports and an in-memory contract implementation."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Set

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

    def store_context(self, context: Mapping[str, Any]) -> None:
        ...

    def get_context(self, context_id: str) -> Dict[str, Any]:
        ...

    def store_report(self, report: Mapping[str, Any], markdown: str) -> None:
        ...

    def get_report(self, report_id: str) -> Dict[str, Any]:
        ...

    def get_report_markdown(self, report_id: str) -> str:
        ...

    def store_agent_run(self, audit: Mapping[str, Any]) -> None:
        ...

    def get_agent_run(self, agent_run_id: str) -> Dict[str, Any]:
        ...


def context_evidence_ids(context: Mapping[str, Any]) -> Set[str]:
    """Return every Evidence reference carried by one Context Package."""

    referenced = set(context["evidence_ids"])
    referenced.update(context["recent_change_evidence_ids"])
    for path in context["state_paths"]:
        referenced.update(path["evidence_ids"])
    return referenced


def report_evidence_ids(report: Mapping[str, Any]) -> Set[str]:
    """Return every Evidence reference cited by one RCA Report."""

    referenced: Set[str] = set()
    root_cause = report["root_cause"]
    if root_cause is not None:
        referenced.update(root_cause["supporting_evidence_ids"])
    for hypothesis in report["hypotheses"]:
        referenced.update(hypothesis["supporting_evidence_ids"])
        referenced.update(hypothesis["contradicting_evidence_ids"])
    return referenced


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
        self._contexts: Dict[str, Dict[str, Any]] = {}
        self._reports: Dict[str, Dict[str, Any]] = {}
        self._report_markdown: Dict[str, str] = {}
        self._agent_runs: Dict[str, Dict[str, Any]] = {}
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

    def store_context(self, context: Mapping[str, Any]) -> None:
        candidate = copy.deepcopy(dict(context))
        validate_contract("context-package.schema.json", candidate)
        incident_id = candidate["incident_id"]
        with self._lock:
            self._require_incident_locked(incident_id)
            available = set(self._evidence_by_incident[incident_id])
            unknown = sorted(context_evidence_ids(candidate) - available)
            if unknown:
                raise InvalidTransition(
                    f"Context Package references unstored Evidence: {unknown}"
                )
            context_id = candidate["context_id"]
            previous = self._contexts.get(context_id)
            if previous is not None and previous != candidate:
                raise InvalidTransition(
                    f"context_id collision with different content: {context_id}"
                )
            self._contexts[context_id] = candidate

    def get_context(self, context_id: str) -> Dict[str, Any]:
        with self._lock:
            try:
                return copy.deepcopy(self._contexts[context_id])
            except KeyError as error:
                raise KeyError(f"unknown context: {context_id}") from error

    def store_report(self, report: Mapping[str, Any], markdown: str) -> None:
        candidate = copy.deepcopy(dict(report))
        validate_contract("rca-report.schema.json", candidate)
        if not isinstance(markdown, str) or not markdown.strip():
            raise InvalidTransition("RCA Report markdown must be non-empty")
        incident_id = candidate["incident_id"]
        with self._lock:
            self._require_incident_locked(incident_id)
            try:
                context = self._contexts[candidate["context_id"]]
            except KeyError as error:
                raise InvalidTransition(
                    f"RCA Report references unknown Context Package: {candidate['context_id']}"
                ) from error
            if context["incident_id"] != incident_id:
                raise InvalidTransition(
                    "RCA Report and Context Package belong to different Incidents"
                )
            unknown = sorted(
                report_evidence_ids(candidate) - set(context["evidence_ids"])
            )
            if unknown:
                raise InvalidTransition(
                    f"RCA Report references Evidence outside Context Package: {unknown}"
                )
            report_id = candidate["report_id"]
            previous = self._reports.get(report_id)
            previous_markdown = self._report_markdown.get(report_id)
            if previous is not None and (
                previous != candidate or previous_markdown != markdown
            ):
                raise InvalidTransition(
                    f"report_id collision with different content: {report_id}"
                )
            self._reports[report_id] = candidate
            self._report_markdown[report_id] = markdown

    def get_report(self, report_id: str) -> Dict[str, Any]:
        with self._lock:
            try:
                return copy.deepcopy(self._reports[report_id])
            except KeyError as error:
                raise KeyError(f"unknown report: {report_id}") from error

    def get_report_markdown(self, report_id: str) -> str:
        with self._lock:
            try:
                return self._report_markdown[report_id]
            except KeyError as error:
                raise KeyError(f"unknown report: {report_id}") from error

    def store_agent_run(self, audit: Mapping[str, Any]) -> None:
        candidate = copy.deepcopy(dict(audit))
        validate_contract("agent-run-audit.schema.json", candidate)
        incident_id = candidate["incident_id"]
        with self._lock:
            self._require_incident_locked(incident_id)
            context = self._contexts.get(candidate["context_id"])
            if context is None or context["incident_id"] != incident_id:
                raise InvalidTransition(
                    "Agent Run references an unknown or foreign Context Package"
                )
            agent_run_id = candidate["agent_run_id"]
            previous = self._agent_runs.get(agent_run_id)
            if previous is not None and previous != candidate:
                raise InvalidTransition(
                    f"agent_run_id collision with different content: {agent_run_id}"
                )
            self._agent_runs[agent_run_id] = candidate

    def get_agent_run(self, agent_run_id: str) -> Dict[str, Any]:
        with self._lock:
            try:
                return copy.deepcopy(self._agent_runs[agent_run_id])
            except KeyError as error:
                raise KeyError(f"unknown Agent Run: {agent_run_id}") from error

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

    def query_incidents(
        self,
        *,
        statuses: Sequence[str],
        severities: Sequence[str],
        namespace: Optional[str],
        search: Optional[str],
        before_updated_at: Optional[str],
        before_incident_id: Optional[str],
        limit: int,
    ) -> List[Dict[str, Any]]:
        self._validate_query_limit(limit, 101)
        if (before_updated_at is None) != (before_incident_id is None):
            raise ValueError("Viewer cursor fields must be provided together")
        status_filter = set(statuses)
        severity_filter = set(severities)
        search_term = search.casefold() if search is not None else None
        with self._lock:
            rows = []
            for incident in self._incidents.values():
                if status_filter and incident["status"] not in status_filter:
                    continue
                if severity_filter and incident["severity"] not in severity_filter:
                    continue
                if namespace is not None and self._incident_namespace(incident) != namespace:
                    continue
                if search_term is not None:
                    searchable = (
                        incident["incident_id"],
                        incident["alert"]["name"],
                        incident["source_entity"]["name"],
                        *incident["alert"]["labels"].values(),
                    )
                    if not any(search_term in value.casefold() for value in searchable):
                        continue
                if before_updated_at is not None and (
                    incident["updated_at"], incident["incident_id"]
                ) >= (before_updated_at, before_incident_id):
                    continue
                rows.append(incident)
            rows.sort(
                key=lambda item: (item["updated_at"], item["incident_id"]),
                reverse=True,
            )
            return copy.deepcopy(rows[:limit])

    def query_evidence(
        self, incident_id: str, *, limit: int
    ) -> List[Dict[str, Any]]:
        self._validate_query_limit(limit, 501)
        with self._lock:
            self._require_incident_locked(incident_id)
            rows = sorted(
                self._evidence_by_incident[incident_id].values(),
                key=lambda item: (item["observed_at"], item["evidence_id"]),
            )
            return copy.deepcopy(rows[:limit])

    def query_contexts(
        self, incident_id: str, *, limit: int
    ) -> List[Dict[str, Any]]:
        self._validate_query_limit(limit, 51)
        with self._lock:
            self._require_incident_locked(incident_id)
            rows = sorted(
                (
                    item
                    for item in self._contexts.values()
                    if item["incident_id"] == incident_id
                ),
                key=lambda item: (item["frozen_at"], item["context_id"]),
                reverse=True,
            )
            return copy.deepcopy(rows[:limit])

    def query_reports(
        self, incident_id: str, *, limit: int
    ) -> List[tuple[Dict[str, Any], str]]:
        self._validate_query_limit(limit, 51)
        with self._lock:
            self._require_incident_locked(incident_id)
            rows = sorted(
                (
                    item
                    for item in self._reports.values()
                    if item["incident_id"] == incident_id
                ),
                key=lambda item: (item["generated_at"], item["report_id"]),
                reverse=True,
            )[:limit]
            return [
                (copy.deepcopy(item), self._report_markdown[item["report_id"]])
                for item in rows
            ]

    def query_agent_runs(
        self, incident_id: str, *, limit: int
    ) -> List[Dict[str, Any]]:
        self._validate_query_limit(limit, 51)
        with self._lock:
            self._require_incident_locked(incident_id)
            rows = sorted(
                (
                    item
                    for item in self._agent_runs.values()
                    if item["incident_id"] == incident_id
                ),
                key=lambda item: (item["started_at"], item["agent_run_id"]),
                reverse=True,
            )
            return copy.deepcopy(rows[:limit])

    def query_audit_events(
        self, incident_id: str, *, limit: int
    ) -> List[AuditEvent]:
        self._validate_query_limit(limit, 1001)
        with self._lock:
            self._require_incident_locked(incident_id)
            return copy.deepcopy(self._audit_events[incident_id][:limit])

    @staticmethod
    def _incident_namespace(incident: Mapping[str, Any]) -> Optional[str]:
        entity = incident["source_entity"]
        if "namespace" in entity:
            return entity["namespace"]
        scope = entity.get("scope", {})
        value = scope.get("namespace") if isinstance(scope, Mapping) else None
        return value if isinstance(value, str) else None

    @staticmethod
    def _validate_query_limit(limit: int, maximum: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
            raise ValueError(f"Viewer repository limit must be between 1 and {maximum}")

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
