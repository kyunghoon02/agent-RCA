"""Alert normalization and Incident ingestion orchestration."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .contracts import validate_contract
from .errors import InvalidAlert
from .repository import IncidentRepository


SUPPORTED_SEVERITIES = frozenset({"info", "warning", "critical"})
RESOURCE_LABELS: Sequence[Tuple[str, str, str]] = (
    ("pod", "Pod", "v1"),
    ("deployment", "Deployment", "apps/v1"),
    ("statefulset", "StatefulSet", "apps/v1"),
    ("daemonset", "DaemonSet", "apps/v1"),
    ("service", "Service", "v1"),
)


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise InvalidAlert(f"{field} must be a non-empty RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidAlert(f"{field} is not a valid RFC3339 timestamp") from error
    if parsed.tzinfo is None:
        raise InvalidAlert(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise InvalidAlert("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _string_map(value: Any, field: str) -> Dict[str, str]:
    if not isinstance(value, Mapping):
        raise InvalidAlert(f"{field} must be an object")
    result: Dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise InvalidAlert(f"{field} keys and values must be strings")
        result[key] = item
    return result


def _required_label(labels: Mapping[str, str], name: str) -> str:
    value = labels.get(name, "").strip()
    if not value:
        raise InvalidAlert(f"Alertmanager alert is missing required label: {name}")
    return value


@dataclass(frozen=True)
class NormalizedAlert:
    incident: Dict[str, Any]
    alert_status: str


@dataclass(frozen=True)
class IngestionResult:
    incident: Dict[str, Any]
    created: bool
    alert_status: str


class AlertmanagerNormalizer:
    """Convert Alertmanager webhook alerts into provider-neutral Incidents."""

    def __init__(self, baseline_duration: timedelta = timedelta(minutes=30)) -> None:
        if baseline_duration <= timedelta(0):
            raise ValueError("baseline_duration must be positive")
        self._baseline_duration = baseline_duration

    def normalize(
        self,
        payload: Mapping[str, Any],
        *,
        received_at: Optional[datetime] = None,
    ) -> List[NormalizedAlert]:
        if not isinstance(payload, Mapping):
            raise InvalidAlert("Alertmanager payload must be an object")
        alerts = payload.get("alerts")
        if not isinstance(alerts, list) or not alerts:
            raise InvalidAlert("Alertmanager payload must contain at least one alert")

        observed_at = received_at or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            raise InvalidAlert("received_at must be timezone-aware")
        return [self._normalize_one(alert, observed_at) for alert in alerts]

    def _normalize_one(
        self, alert: Any, received_at: datetime
    ) -> NormalizedAlert:
        if not isinstance(alert, Mapping):
            raise InvalidAlert("each Alertmanager alert must be an object")

        alert_status = alert.get("status")
        if alert_status not in {"firing", "resolved"}:
            raise InvalidAlert("Alertmanager alert status must be firing or resolved")

        labels = _string_map(alert.get("labels"), "labels")
        annotations = _string_map(alert.get("annotations", {}), "annotations")
        alert_name = _required_label(labels, "alertname")
        namespace = _required_label(labels, "namespace")
        severity = _required_label(labels, "severity")
        if severity not in SUPPORTED_SEVERITIES:
            raise InvalidAlert(
                f"unsupported severity {severity!r}; expected one of "
                f"{sorted(SUPPORTED_SEVERITIES)}"
            )

        fingerprint = alert.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise InvalidAlert("Alertmanager alert fingerprint is required")

        starts_at = _parse_time(alert.get("startsAt"), "startsAt")
        ends_at = self._resolve_end_time(alert, alert_status, starts_at)
        source_entity = self._source_entity(labels, namespace)
        deduplication_key, incident_id = self._incident_identity(
            fingerprint=fingerprint,
            starts_at=starts_at,
            alert_name=alert_name,
            source_entity=source_entity,
        )
        timestamp = _format_time(received_at)
        incident = {
            "schema_version": "1.0.0",
            "incident_id": incident_id,
            "deduplication_key": deduplication_key,
            "status": "RECEIVED",
            "severity": severity,
            "source": "alertmanager",
            "triggered_at": _format_time(starts_at),
            "window": {
                "baseline_start": _format_time(starts_at - self._baseline_duration),
                "incident_start": _format_time(starts_at),
                "incident_end": _format_time(ends_at) if ends_at else None,
                "recovery_end": None,
            },
            "alert": {
                "fingerprint": fingerprint,
                "name": alert_name,
                "labels": labels,
                "annotations": annotations,
            },
            "source_entity": source_entity,
            "collector_statuses": [],
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        validate_contract("incident.schema.json", incident)
        return NormalizedAlert(incident=incident, alert_status=alert_status)

    @staticmethod
    def _resolve_end_time(
        alert: Mapping[str, Any], alert_status: str, starts_at: datetime
    ) -> Optional[datetime]:
        if alert_status == "firing":
            return None
        ends_at = _parse_time(alert.get("endsAt"), "endsAt")
        if ends_at < starts_at:
            raise InvalidAlert("endsAt must not precede startsAt")
        return ends_at

    @staticmethod
    def _source_entity(
        labels: Mapping[str, str], namespace: str
    ) -> Dict[str, Any]:
        for label, kind, api_version in RESOURCE_LABELS:
            name = labels.get(label, "").strip()
            if name:
                return {
                    "api_version": api_version,
                    "kind": kind,
                    "namespace": namespace,
                    "name": name,
                    "uid": None,
                    "exists": True,
                }
        raise InvalidAlert(
            "Alertmanager alert must identify one of pod, deployment, "
            "statefulset, daemonset, or service"
        )

    @staticmethod
    def _incident_identity(
        *,
        fingerprint: str,
        starts_at: datetime,
        alert_name: str,
        source_entity: Mapping[str, Any],
    ) -> Tuple[str, str]:
        identity = [
            "alertmanager",
            fingerprint,
            _format_time(starts_at),
            alert_name,
            source_entity["kind"],
            source_entity["namespace"],
            source_entity["name"],
        ]
        canonical = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"alertmanager:sha256:{digest}", f"inc-{digest[:24]}"


class AlertmanagerIngestionService:
    """Normalize an Alertmanager webhook and persist it idempotently."""

    def __init__(
        self,
        repository: IncidentRepository,
        normalizer: Optional[AlertmanagerNormalizer] = None,
    ) -> None:
        self._repository = repository
        self._normalizer = normalizer or AlertmanagerNormalizer()

    def ingest(
        self,
        payload: Mapping[str, Any],
        *,
        received_at: Optional[datetime] = None,
    ) -> List[IngestionResult]:
        observed_at = received_at or datetime.now(timezone.utc)
        results = []
        for normalized in self._normalizer.normalize(
            payload, received_at=observed_at
        ):
            created = self._repository.create_or_get_by_deduplication_key(
                normalized.incident,
                occurred_at=observed_at,
            )
            stored = created.incident
            incident_end = normalized.incident["window"]["incident_end"]
            if not created.created and incident_end is not None:
                stored = self._repository.record_alert_resolution(
                    stored["incident_id"],
                    incident_end=incident_end,
                    occurred_at=observed_at,
                )
            results.append(
                IngestionResult(
                    incident=copy.deepcopy(stored),
                    created=created.created,
                    alert_status=normalized.alert_status,
                )
            )
        return results
