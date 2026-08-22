"""Bounded, read-only Incident and RCA Viewer query service."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from .contracts import validate_contract
from .errors import ContractViolation
from .evidence import parse_time
from .repository import AuditEvent, report_evidence_ids


_PROBLEM_COLLECTOR_STATUSES = frozenset({"PARTIAL", "FAILED", "TIMED_OUT"})


class ViewerRepository(Protocol):
    """Read-only persistence capability exposed to the Viewer service."""

    def get(self, incident_id: str) -> Dict[str, Any]:
        ...

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
        ...

    def query_evidence(
        self, incident_id: str, *, limit: int
    ) -> List[Dict[str, Any]]:
        ...

    def query_contexts(
        self, incident_id: str, *, limit: int
    ) -> List[Dict[str, Any]]:
        ...

    def query_reports(
        self, incident_id: str, *, limit: int
    ) -> List[Tuple[Dict[str, Any], str]]:
        ...

    def query_agent_runs(
        self, incident_id: str, *, limit: int
    ) -> List[Dict[str, Any]]:
        ...

    def query_audit_events(
        self, incident_id: str, *, limit: int
    ) -> List[AuditEvent]:
        ...


@dataclass(frozen=True)
class ViewerQueryPolicy:
    max_page_size: int = 100
    max_evidence: int = 500
    max_contexts: int = 50
    max_reports: int = 50
    max_agent_runs: int = 50
    max_audit_events: int = 1000
    max_timeline_events: int = 2000

    def __post_init__(self) -> None:
        bounds = {
            "max_page_size": (self.max_page_size, 100),
            "max_evidence": (self.max_evidence, 500),
            "max_contexts": (self.max_contexts, 50),
            "max_reports": (self.max_reports, 50),
            "max_agent_runs": (self.max_agent_runs, 50),
            "max_audit_events": (self.max_audit_events, 1000),
            "max_timeline_events": (self.max_timeline_events, 2000),
        }
        for name, (value, maximum) in bounds.items():
            if not 1 <= value <= maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")


class IncidentViewerQueryService:
    """Expose schema-valid summaries and detail bundles without mutation methods."""

    def __init__(
        self,
        repository: ViewerRepository,
        *,
        policy: Optional[ViewerQueryPolicy] = None,
    ) -> None:
        self._repository = repository
        self._policy = policy or ViewerQueryPolicy()

    def list_incidents(self, query: Mapping[str, Any]) -> Dict[str, Any]:
        candidate = copy.deepcopy(dict(query))
        validate_contract("viewer-incident-query.schema.json", candidate)
        candidate["statuses"] = sorted(candidate["statuses"])
        candidate["severities"] = sorted(candidate["severities"])
        if candidate["namespace"] is not None:
            candidate["namespace"] = candidate["namespace"].strip()
            if not candidate["namespace"]:
                raise ContractViolation("Viewer namespace filter cannot be blank")
        if candidate["search"] is not None:
            candidate["search"] = " ".join(candidate["search"].split())
            if not candidate["search"]:
                raise ContractViolation("Viewer search filter cannot be blank")
        if candidate["limit"] > self._policy.max_page_size:
            raise ContractViolation("Viewer query exceeds configured page size")

        filter_hash = self._filter_hash(candidate)
        before_updated_at, before_incident_id = self._decode_cursor(
            candidate["cursor"], filter_hash
        )
        rows = self._repository.query_incidents(
            statuses=candidate["statuses"],
            severities=candidate["severities"],
            namespace=candidate["namespace"],
            search=candidate["search"],
            before_updated_at=before_updated_at,
            before_incident_id=before_incident_id,
            limit=candidate["limit"] + 1,
        )
        has_more = len(rows) > candidate["limit"]
        selected = rows[: candidate["limit"]]
        next_cursor = None
        if has_more and selected:
            last = selected[-1]
            next_cursor = self._encode_cursor(
                last["updated_at"], last["incident_id"], filter_hash
            )
        response = {
            "schema_version": "1.0.0",
            "items": [self._summary(item) for item in selected],
            "next_cursor": next_cursor,
        }
        validate_contract("viewer-incident-list.schema.json", response)
        return response

    def get_incident_detail(self, incident_id: str) -> Dict[str, Any]:
        incident = self._repository.get(incident_id)
        evidence_rows = self._repository.query_evidence(
            incident_id, limit=self._policy.max_evidence + 1
        )
        context_rows = self._repository.query_contexts(
            incident_id, limit=self._policy.max_contexts + 1
        )
        report_rows = self._repository.query_reports(
            incident_id, limit=self._policy.max_reports + 1
        )
        agent_rows = self._repository.query_agent_runs(
            incident_id, limit=self._policy.max_agent_runs + 1
        )
        audit_rows = self._repository.query_audit_events(
            incident_id, limit=self._policy.max_audit_events + 1
        )
        truncated = {
            "evidence": len(evidence_rows) > self._policy.max_evidence,
            "contexts": len(context_rows) > self._policy.max_contexts,
            "reports": len(report_rows) > self._policy.max_reports,
            "agent_runs": len(agent_rows) > self._policy.max_agent_runs,
            "audit_events": len(audit_rows) > self._policy.max_audit_events,
            "timeline": False,
        }
        evidence = evidence_rows[: self._policy.max_evidence]
        contexts = context_rows[: self._policy.max_contexts]
        reports = report_rows[: self._policy.max_reports]
        agent_runs = agent_rows[: self._policy.max_agent_runs]
        audits = audit_rows[: self._policy.max_audit_events]
        timeline = self._timeline(
            evidence=evidence,
            contexts=contexts,
            reports=reports,
            agent_runs=agent_runs,
            audit_events=audits,
        )
        if len(timeline) > self._policy.max_timeline_events:
            truncated["timeline"] = True
            timeline = timeline[: self._policy.max_timeline_events]
        response = {
            "schema_version": "1.0.0",
            "incident": incident,
            "evidence": evidence,
            "contexts": contexts,
            "reports": [
                {"report": report, "markdown": markdown}
                for report, markdown in reports
            ],
            "agent_runs": agent_runs,
            "timeline": timeline,
            "truncated": truncated,
        }
        validate_contract("viewer-incident-detail.schema.json", response)
        return response

    @staticmethod
    def _summary(incident: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "incident_id": incident["incident_id"],
            "status": incident["status"],
            "severity": incident["severity"],
            "source": incident["source"],
            "triggered_at": incident["triggered_at"],
            "updated_at": incident["updated_at"],
            "alert_name": incident["alert"]["name"],
            "source_entity": copy.deepcopy(dict(incident["source_entity"])),
            "collector_problem_count": sum(
                status["status"] in _PROBLEM_COLLECTOR_STATUSES
                for status in incident["collector_statuses"]
            ),
        }

    @staticmethod
    def _filter_hash(query: Mapping[str, Any]) -> str:
        filters = {
            "statuses": query["statuses"],
            "severities": query["severities"],
            "namespace": query["namespace"],
            "search": query["search"],
        }
        canonical = json.dumps(filters, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _encode_cursor(updated_at: str, incident_id: str, filter_hash: str) -> str:
        payload = json.dumps(
            {
                "updated_at": updated_at,
                "incident_id": incident_id,
                "filter_hash": filter_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(
        cursor: Optional[str], expected_filter_hash: str
    ) -> Tuple[Optional[str], Optional[str]]:
        if cursor is None:
            return None, None
        try:
            padding = "=" * (-len(cursor) % 4)
            raw = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError, binascii.Error) as error:
            raise ContractViolation("Viewer cursor is malformed") from error
        if not isinstance(payload, Mapping) or set(payload) != {
            "updated_at",
            "incident_id",
            "filter_hash",
        }:
            raise ContractViolation("Viewer cursor has an invalid shape")
        if payload["filter_hash"] != expected_filter_hash:
            raise ContractViolation("Viewer cursor does not match current filters")
        if not isinstance(payload["updated_at"], str) or not isinstance(
            payload["incident_id"], str
        ):
            raise ContractViolation("Viewer cursor fields must be strings")
        parse_time(payload["updated_at"], "ViewerCursor.updated_at")
        return payload["updated_at"], payload["incident_id"]

    @classmethod
    def _timeline(
        cls,
        *,
        evidence: Sequence[Mapping[str, Any]],
        contexts: Sequence[Mapping[str, Any]],
        reports: Sequence[Tuple[Mapping[str, Any], str]],
        agent_runs: Sequence[Mapping[str, Any]],
        audit_events: Sequence[AuditEvent],
    ) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for audit in audit_events:
            events.append(
                {
                    "occurred_at": audit.occurred_at,
                    "stage": cls._audit_stage(audit),
                    "event_type": audit.event_type,
                    "evidence_ids": [],
                    "details": copy.deepcopy(dict(audit.details)),
                }
            )
        for item in evidence:
            events.append(
                {
                    "occurred_at": item["observed_at"],
                    "stage": "COLLECTION",
                    "event_type": "EVIDENCE_OBSERVED",
                    "evidence_ids": [item["evidence_id"]],
                    "details": {
                        "source": item["source"],
                        "kind": item["kind"],
                        "subject": copy.deepcopy(dict(item["subject"])),
                    },
                }
            )
        for context in contexts:
            events.append(
                {
                    "occurred_at": context["frozen_at"],
                    "stage": "LOCALIZATION",
                    "event_type": "CONTEXT_FROZEN",
                    "evidence_ids": [],
                    "details": {
                        "context_id": context["context_id"],
                        "strategy": context["localization"]["strategy"],
                        "context_completeness": context["localization"][
                            "context_completeness"
                        ],
                        "evidence_count": len(context["evidence_ids"]),
                    },
                }
            )
        for run in agent_runs:
            events.append(
                {
                    "occurred_at": run["completed_at"],
                    "stage": "ANALYSIS",
                    "event_type": "AGENT_RUN_COMPLETED",
                    "evidence_ids": list(run["cited_evidence_ids"][:100]),
                    "details": {
                        "agent_run_id": run["agent_run_id"],
                        "status": run["status"],
                        "reason_code": run["reason_code"],
                        "model": run["model"],
                        "usage": copy.deepcopy(dict(run["usage"])),
                    },
                }
            )
        for report, _ in reports:
            cited = sorted(report_evidence_ids(report))
            events.append(
                {
                    "occurred_at": report["generated_at"],
                    "stage": "REPORT",
                    "event_type": "REPORT_GENERATED",
                    "evidence_ids": cited[:100],
                    "details": {
                        "report_id": report["report_id"],
                        "status": report["status"],
                        "path": report["path"],
                        "cited_evidence_count": len(cited),
                    },
                }
            )
        events.sort(
            key=lambda item: (
                item["occurred_at"],
                item["stage"],
                item["event_type"],
                json.dumps(item["details"], sort_keys=True, default=str),
            )
        )
        return events

    @staticmethod
    def _audit_stage(audit: AuditEvent) -> str:
        if audit.event_type in {"INCIDENT_CREATED", "ALERT_RESOLVED"}:
            return "DETECTION"
        if audit.event_type == "COLLECTION_COMPLETED":
            return "COLLECTION"
        if audit.event_type == "STATUS_TRANSITIONED":
            target = audit.details.get("to")
            if target == "COLLECTING":
                return "COLLECTION"
            if target == "LOCALIZING":
                return "LOCALIZATION"
            if target == "ANALYZING":
                return "ANALYSIS"
            if target == "REPORTED":
                return "REPORT"
        return "ANALYSIS"
