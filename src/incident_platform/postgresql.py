"""PostgreSQL persistence adapter for Incident RCA artifacts.

The adapter accepts a connection factory instead of a DSN. This keeps secrets
outside the domain layer and makes the database boundary contract-testable.
Each factory call must return a dedicated DB-API compatible connection.
"""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence

from .contracts import validate_contract
from .errors import InvalidTransition
from .repository import (
    ALLOWED_TRANSITIONS,
    AuditEvent,
    CreateResult,
    _format_time,
    _utc_now,
    context_evidence_ids,
    report_evidence_ids,
)


ConnectionFactory = Callable[[], Any]
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decode_document(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, Mapping):
            return dict(decoded)
    raise TypeError("PostgreSQL JSONB result is not an object")


@contextmanager
def _connection(connection_factory: ConnectionFactory) -> Iterator[Any]:
    connection = connection_factory()
    try:
        with connection:
            yield connection
    finally:
        close = getattr(connection, "close", None)
        if callable(close) and not getattr(connection, "closed", False):
            close()


def apply_migrations(
    connection_factory: ConnectionFactory,
    migrations_dir: Path = DEFAULT_MIGRATIONS_DIR,
) -> List[str]:
    """Apply ordered SQL files once and return versions applied this call."""

    paths = sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql"))
    if not paths:
        raise ValueError(f"no PostgreSQL migrations found in {migrations_dir}")

    applied: List[str] = []
    with _connection(connection_factory) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            for path in paths:
                version = path.name
                cursor.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = %s",
                    (version,),
                )
                if cursor.fetchone() is not None:
                    continue
                cursor.execute(path.read_text(encoding="utf-8"))
                cursor.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (version,),
                )
                applied.append(version)
    return applied


class PostgreSQLIncidentRepository:
    """Transaction-safe PostgreSQL implementation of IncidentRepository."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def create_or_get_by_deduplication_key(
        self,
        incident: Mapping[str, Any],
        *,
        occurred_at: Optional[datetime] = None,
    ) -> CreateResult:
        candidate = copy.deepcopy(dict(incident))
        validate_contract("incident.schema.json", candidate)
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO incidents (
                        incident_id, deduplication_key, status, triggered_at,
                        created_at, updated_at, document
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT DO NOTHING
                    RETURNING document
                    """,
                    (
                        candidate["incident_id"],
                        candidate["deduplication_key"],
                        candidate["status"],
                        candidate["triggered_at"],
                        candidate["created_at"],
                        candidate["updated_at"],
                        _json(candidate),
                    ),
                )
                row = cursor.fetchone()
                if row is not None:
                    self._append_audit_event(
                        cursor,
                        candidate["incident_id"],
                        "INCIDENT_CREATED",
                        occurred_at or _utc_now(),
                        {"status": candidate["status"]},
                    )
                    return CreateResult(_decode_document(row[0]), True)

                cursor.execute(
                    "SELECT document FROM incidents WHERE deduplication_key = %s",
                    (candidate["deduplication_key"],),
                )
                row = cursor.fetchone()
                if row is not None:
                    return CreateResult(_decode_document(row[0]), False)

                cursor.execute(
                    "SELECT deduplication_key FROM incidents WHERE incident_id = %s",
                    (candidate["incident_id"],),
                )
                if cursor.fetchone() is not None:
                    raise InvalidTransition(
                        f"incident_id {candidate['incident_id']} already belongs to "
                        "another deduplication key"
                    )
                raise RuntimeError("incident insert conflicted without a visible owner")

    def get(self, incident_id: str) -> Dict[str, Any]:
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT document FROM incidents WHERE incident_id = %s",
                    (incident_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown incident: {incident_id}")
                return _decode_document(row[0])

    def transition(
        self,
        incident_id: str,
        *,
        expected_status: str,
        next_status: str,
        occurred_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        transition_time = occurred_at or _utc_now()
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                incident = self._locked_incident(cursor, incident_id)
                current_status = incident["status"]
                if current_status != expected_status:
                    raise InvalidTransition(
                        f"stale transition for {incident_id}: expected "
                        f"{expected_status}, found {current_status}"
                    )
                if next_status not in ALLOWED_TRANSITIONS[current_status]:
                    raise InvalidTransition(
                        f"transition {current_status} -> {next_status} is not allowed"
                    )
                updated = copy.deepcopy(incident)
                updated["status"] = next_status
                updated["updated_at"] = _format_time(transition_time)
                validate_contract("incident.schema.json", updated)
                self._update_incident(cursor, updated)
                self._append_audit_event(
                    cursor,
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
        names = [status.get("collector") for status in statuses]
        if len(names) != len(set(names)):
            raise InvalidTransition("collector status names must be unique")
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                incident = self._locked_incident(cursor, incident_id)
                if incident["status"] != "COLLECTING":
                    raise InvalidTransition(
                        "collector statuses may only be replaced while COLLECTING"
                    )
                updated = copy.deepcopy(incident)
                updated["collector_statuses"] = statuses
                updated["updated_at"] = _format_time(update_time)
                validate_contract("incident.schema.json", updated)
                self._update_incident(cursor, updated)
                self._append_audit_event(
                    cursor,
                    incident_id,
                    "COLLECTION_COMPLETED",
                    update_time,
                    {
                        "collector_statuses": {
                            status["collector"]: status["status"]
                            for status in statuses
                        }
                    },
                )
                return copy.deepcopy(updated)

    def record_alert_resolution(
        self,
        incident_id: str,
        *,
        incident_end: str,
        occurred_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        resolution_time = occurred_at or _utc_now()
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                incident = self._locked_incident(cursor, incident_id)
                previous_end = incident["window"]["incident_end"]
                if previous_end is not None and previous_end != incident_end:
                    raise InvalidTransition(
                        f"incident {incident_id} already has a different incident_end"
                    )
                updated = copy.deepcopy(incident)
                updated["window"]["incident_end"] = incident_end
                updated["updated_at"] = _format_time(resolution_time)
                validate_contract("incident.schema.json", updated)
                self._update_incident(cursor, updated)
                if previous_end is None:
                    self._append_audit_event(
                        cursor,
                        incident_id,
                        "ALERT_RESOLVED",
                        resolution_time,
                        {"incident_end": incident_end},
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
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._locked_incident(cursor, incident_id)
                for candidate in candidates:
                    cursor.execute(
                        """
                        INSERT INTO evidence_items (
                            evidence_id, incident_id, content_hash, observed_at, document
                        ) VALUES (%s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (evidence_id) DO NOTHING
                        RETURNING evidence_id
                        """,
                        (
                            candidate["evidence_id"],
                            incident_id,
                            candidate["provenance"]["content_hash"],
                            candidate["observed_at"],
                            _json(candidate),
                        ),
                    )
                    if cursor.fetchone() is not None:
                        continue
                    cursor.execute(
                        "SELECT document FROM evidence_items WHERE evidence_id = %s",
                        (candidate["evidence_id"],),
                    )
                    row = cursor.fetchone()
                    if row is None or _decode_document(row[0]) != candidate:
                        raise InvalidTransition(
                            "evidence_id collision with different content: "
                            f"{candidate['evidence_id']}"
                        )

    def list_evidence(self, incident_id: str) -> List[Dict[str, Any]]:
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._require_incident(cursor, incident_id)
                cursor.execute(
                    """
                    SELECT document FROM evidence_items
                    WHERE incident_id = %s
                    ORDER BY observed_at, evidence_id
                    """,
                    (incident_id,),
                )
                return [_decode_document(row[0]) for row in cursor.fetchall()]

    def store_context(self, context: Mapping[str, Any]) -> None:
        candidate = copy.deepcopy(dict(context))
        validate_contract("context-package.schema.json", candidate)
        incident_id = candidate["incident_id"]
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._locked_incident(cursor, incident_id)
                available = self._evidence_ids(cursor, incident_id)
                unknown = sorted(context_evidence_ids(candidate) - available)
                if unknown:
                    raise InvalidTransition(
                        f"Context Package references unstored Evidence: {unknown}"
                    )
                cursor.execute(
                    """
                    INSERT INTO context_packages (
                        context_id, incident_id, frozen_at, document
                    ) VALUES (%s, %s, %s, %s::jsonb)
                    ON CONFLICT (context_id) DO NOTHING
                    RETURNING context_id
                    """,
                    (
                        candidate["context_id"],
                        incident_id,
                        candidate["frozen_at"],
                        _json(candidate),
                    ),
                )
                if cursor.fetchone() is not None:
                    return
                cursor.execute(
                    "SELECT document FROM context_packages WHERE context_id = %s",
                    (candidate["context_id"],),
                )
                row = cursor.fetchone()
                if row is None or _decode_document(row[0]) != candidate:
                    raise InvalidTransition(
                        "context_id collision with different content: "
                        f"{candidate['context_id']}"
                    )

    def get_context(self, context_id: str) -> Dict[str, Any]:
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT document FROM context_packages WHERE context_id = %s",
                    (context_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown context: {context_id}")
                return _decode_document(row[0])

    def store_report(self, report: Mapping[str, Any], markdown: str) -> None:
        candidate = copy.deepcopy(dict(report))
        validate_contract("rca-report.schema.json", candidate)
        if not isinstance(markdown, str) or not markdown.strip():
            raise InvalidTransition("RCA Report markdown must be non-empty")
        incident_id = candidate["incident_id"]
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._locked_incident(cursor, incident_id)
                cursor.execute(
                    """
                    SELECT incident_id, document FROM context_packages
                    WHERE context_id = %s
                    """,
                    (candidate["context_id"],),
                )
                row = cursor.fetchone()
                if row is None:
                    raise InvalidTransition(
                        "RCA Report references unknown Context Package: "
                        f"{candidate['context_id']}"
                    )
                context = _decode_document(row[1])
                if row[0] != incident_id:
                    raise InvalidTransition(
                        "RCA Report and Context Package belong to different Incidents"
                    )
                unknown = sorted(
                    report_evidence_ids(candidate) - set(context["evidence_ids"])
                )
                if unknown:
                    raise InvalidTransition(
                        "RCA Report references Evidence outside Context Package: "
                        f"{unknown}"
                    )
                cursor.execute(
                    """
                    INSERT INTO rca_reports (
                        report_id, incident_id, context_id, status,
                        generated_at, document, markdown
                    ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (report_id) DO NOTHING
                    RETURNING report_id
                    """,
                    (
                        candidate["report_id"],
                        incident_id,
                        candidate["context_id"],
                        candidate["status"],
                        candidate["generated_at"],
                        _json(candidate),
                        markdown,
                    ),
                )
                if cursor.fetchone() is not None:
                    return
                cursor.execute(
                    """
                    SELECT document, markdown FROM rca_reports WHERE report_id = %s
                    """,
                    (candidate["report_id"],),
                )
                row = cursor.fetchone()
                if (
                    row is None
                    or _decode_document(row[0]) != candidate
                    or row[1] != markdown
                ):
                    raise InvalidTransition(
                        "report_id collision with different content: "
                        f"{candidate['report_id']}"
                    )

    def get_report(self, report_id: str) -> Dict[str, Any]:
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT document FROM rca_reports WHERE report_id = %s",
                    (report_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown report: {report_id}")
                return _decode_document(row[0])

    def get_report_markdown(self, report_id: str) -> str:
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT markdown FROM rca_reports WHERE report_id = %s",
                    (report_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown report: {report_id}")
                return row[0]

    def store_agent_run(self, audit: Mapping[str, Any]) -> None:
        candidate = copy.deepcopy(dict(audit))
        validate_contract("agent-run-audit.schema.json", candidate)
        incident_id = candidate["incident_id"]
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._locked_incident(cursor, incident_id)
                cursor.execute(
                    "SELECT incident_id FROM context_packages WHERE context_id = %s",
                    (candidate["context_id"],),
                )
                row = cursor.fetchone()
                if row is None or row[0] != incident_id:
                    raise InvalidTransition(
                        "Agent Run references an unknown or foreign Context Package"
                    )
                cursor.execute(
                    """
                    INSERT INTO agent_runs (
                        agent_run_id, incident_id, context_id, status,
                        started_at, completed_at, document
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (agent_run_id) DO NOTHING
                    RETURNING agent_run_id
                    """,
                    (
                        candidate["agent_run_id"],
                        incident_id,
                        candidate["context_id"],
                        candidate["status"],
                        candidate["started_at"],
                        candidate["completed_at"],
                        _json(candidate),
                    ),
                )
                if cursor.fetchone() is not None:
                    return
                cursor.execute(
                    "SELECT document FROM agent_runs WHERE agent_run_id = %s",
                    (candidate["agent_run_id"],),
                )
                row = cursor.fetchone()
                if row is None or _decode_document(row[0]) != candidate:
                    raise InvalidTransition(
                        "agent_run_id collision with different content: "
                        f"{candidate['agent_run_id']}"
                    )

    def get_agent_run(self, agent_run_id: str) -> Dict[str, Any]:
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT document FROM agent_runs WHERE agent_run_id = %s",
                    (agent_run_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown Agent Run: {agent_run_id}")
                return _decode_document(row[0])

    def list_audit_events(self, incident_id: str) -> List[AuditEvent]:
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._require_incident(cursor, incident_id)
                cursor.execute(
                    """
                    SELECT event_type, occurred_at, details
                    FROM incident_audit_events
                    WHERE incident_id = %s
                    ORDER BY occurred_at, event_id
                    """,
                    (incident_id,),
                )
                events = []
                for event_type, occurred_at, details in cursor.fetchall():
                    events.append(
                        AuditEvent(
                            incident_id=incident_id,
                            event_type=event_type,
                            occurred_at=_format_time(occurred_at),
                            details=_decode_document(details),
                        )
                    )
                return events

    @staticmethod
    def _require_incident(cursor: Any, incident_id: str) -> None:
        cursor.execute(
            "SELECT 1 FROM incidents WHERE incident_id = %s", (incident_id,)
        )
        if cursor.fetchone() is None:
            raise KeyError(f"unknown incident: {incident_id}")

    @staticmethod
    def _locked_incident(cursor: Any, incident_id: str) -> Dict[str, Any]:
        cursor.execute(
            "SELECT document FROM incidents WHERE incident_id = %s FOR UPDATE",
            (incident_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(f"unknown incident: {incident_id}")
        return _decode_document(row[0])

    @staticmethod
    def _update_incident(cursor: Any, incident: Mapping[str, Any]) -> None:
        cursor.execute(
            """
            UPDATE incidents
            SET status = %s, updated_at = %s, document = %s::jsonb
            WHERE incident_id = %s
            """,
            (
                incident["status"],
                incident["updated_at"],
                _json(incident),
                incident["incident_id"],
            ),
        )

    @staticmethod
    def _append_audit_event(
        cursor: Any,
        incident_id: str,
        event_type: str,
        occurred_at: datetime,
        details: Mapping[str, Any],
    ) -> None:
        _format_time(occurred_at)
        cursor.execute(
            """
            INSERT INTO incident_audit_events (
                incident_id, event_type, occurred_at, details
            ) VALUES (%s, %s, %s, %s::jsonb)
            """,
            (incident_id, event_type, occurred_at, _json(details)),
        )

    @staticmethod
    def _evidence_ids(cursor: Any, incident_id: str) -> set[str]:
        cursor.execute(
            "SELECT evidence_id FROM evidence_items WHERE incident_id = %s",
            (incident_id,),
        )
        return {row[0] for row in cursor.fetchall()}
