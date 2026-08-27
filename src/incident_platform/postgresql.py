"""PostgreSQL persistence adapter for Incident RCA artifacts.

The adapter accepts a connection factory instead of a DSN. This keeps secrets
outside the domain layer and makes the database boundary contract-testable.
Each factory call must return a dedicated DB-API compatible connection.
"""

from __future__ import annotations

import copy
import json
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from .contracts import validate_contract
from .evidence import parse_time
from .errors import InvalidTransition
from .incident_work import (
    IncidentAnalysisWorkClaim,
    IncidentWorkClaim,
    IncidentWorkQueueSnapshot,
    IncidentWorkQueueStageSnapshot,
    WORK_QUEUE_STAGES,
    WORK_OUTCOMES,
    validate_analysis_eligibility,
    validate_claim_request,
    validate_incident_id,
)
from .repository import (
    ALLOWED_TRANSITIONS,
    AuditEvent,
    CreateResult,
    _format_time,
    _utc_now,
    context_evidence_ids,
    report_evidence_ids,
)
from .stategraph import StateGraphReconciliationResult
from .stategraph_observations import (
    StateGraphObservationCycle,
    StateGraphObservationPruneResult,
    StateGraphObservationRetentionPolicy,
    validate_cycle_evidence,
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
        self._validate_viewer_limit(limit, 101)
        if (before_updated_at is None) != (before_incident_id is None):
            raise ValueError("Viewer cursor fields must be provided together")
        clauses = []
        parameters: List[Any] = []
        if statuses:
            clauses.append("status = ANY(%s)")
            parameters.append(list(statuses))
        if severities:
            clauses.append("document->>'severity' = ANY(%s)")
            parameters.append(list(severities))
        if namespace is not None:
            clauses.append(
                "COALESCE(document->'source_entity'->>'namespace', "
                "document->'source_entity'->'scope'->>'namespace') = %s"
            )
            parameters.append(namespace)
        if search is not None:
            clauses.append(
                "POSITION(lower(%s) IN lower("
                "incident_id || ' ' || document->'alert'->>'name' || ' ' || "
                "document->'source_entity'->>'name' || ' ' || "
                "(document->'alert'->'labels')::text)) > 0"
            )
            parameters.append(search)
        if before_updated_at is not None:
            clauses.append("(updated_at, incident_id) < (%s, %s)")
            parameters.extend((before_updated_at, before_incident_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(limit)
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT document FROM incidents"
                    + where
                    + " ORDER BY updated_at DESC, incident_id DESC LIMIT %s",
                    tuple(parameters),
                )
                return [_decode_document(row[0]) for row in cursor.fetchall()]

    def query_evidence(
        self, incident_id: str, *, limit: int
    ) -> List[Dict[str, Any]]:
        self._validate_viewer_limit(limit, 501)
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._require_incident(cursor, incident_id)
                cursor.execute(
                    """
                    SELECT document FROM evidence_items
                    WHERE incident_id = %s
                    ORDER BY observed_at, evidence_id
                    LIMIT %s
                    """,
                    (incident_id, limit),
                )
                return [_decode_document(row[0]) for row in cursor.fetchall()]

    def query_contexts(
        self, incident_id: str, *, limit: int
    ) -> List[Dict[str, Any]]:
        self._validate_viewer_limit(limit, 51)
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._require_incident(cursor, incident_id)
                cursor.execute(
                    """
                    SELECT document FROM context_packages
                    WHERE incident_id = %s
                    ORDER BY frozen_at DESC, context_id DESC
                    LIMIT %s
                    """,
                    (incident_id, limit),
                )
                return [_decode_document(row[0]) for row in cursor.fetchall()]

    def query_reports(
        self, incident_id: str, *, limit: int
    ) -> List[tuple[Dict[str, Any], str]]:
        self._validate_viewer_limit(limit, 51)
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._require_incident(cursor, incident_id)
                cursor.execute(
                    """
                    SELECT document, markdown FROM rca_reports
                    WHERE incident_id = %s
                    ORDER BY generated_at DESC, report_id DESC
                    LIMIT %s
                    """,
                    (incident_id, limit),
                )
                return [
                    (_decode_document(row[0]), row[1]) for row in cursor.fetchall()
                ]

    def query_agent_runs(
        self, incident_id: str, *, limit: int
    ) -> List[Dict[str, Any]]:
        self._validate_viewer_limit(limit, 51)
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._require_incident(cursor, incident_id)
                cursor.execute(
                    """
                    SELECT document FROM agent_runs
                    WHERE incident_id = %s
                    ORDER BY started_at DESC, agent_run_id DESC
                    LIMIT %s
                    """,
                    (incident_id, limit),
                )
                return [_decode_document(row[0]) for row in cursor.fetchall()]

    def query_audit_events(
        self, incident_id: str, *, limit: int
    ) -> List[AuditEvent]:
        self._validate_viewer_limit(limit, 1001)
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._require_incident(cursor, incident_id)
                cursor.execute(
                    """
                    SELECT event_type, occurred_at, details
                    FROM incident_audit_events
                    WHERE incident_id = %s
                    ORDER BY occurred_at, event_id
                    LIMIT %s
                    """,
                    (incident_id, limit),
                )
                return [
                    AuditEvent(
                        incident_id=incident_id,
                        event_type=row[0],
                        occurred_at=_format_time(row[1]),
                        details=_decode_document(row[2]),
                    )
                    for row in cursor.fetchall()
                ]

    def query_work_state(
        self, incident_id: str
    ) -> Dict[str, Optional[Dict[str, Any]]]:
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._require_incident(cursor, incident_id)
                cursor.execute(
                    """
                    SELECT 'collection', stage, state, available_at,
                           attempt_count, worker_id, lease_expires_at,
                           claimed_at, completed_at, last_error_code,
                           NULL::text AS context_id
                    FROM incident_work_items
                    WHERE incident_id = %s
                    UNION ALL
                    SELECT 'localization', stage, state, available_at,
                           attempt_count, worker_id, lease_expires_at,
                           claimed_at, completed_at, last_error_code,
                           NULL::text AS context_id
                    FROM incident_localization_work_items
                    WHERE incident_id = %s
                    UNION ALL
                    SELECT 'analysis', stage, state, available_at,
                           attempt_count, worker_id, lease_expires_at,
                           claimed_at, completed_at, last_error_code,
                           context_id
                    FROM incident_analysis_work_items
                    WHERE incident_id = %s
                    """,
                    (incident_id, incident_id, incident_id),
                )
                result: Dict[str, Optional[Dict[str, Any]]] = {
                    "collection": None,
                    "localization": None,
                    "analysis": None,
                }
                for row in cursor.fetchall():
                    result[row[0]] = {
                        "stage": row[1],
                        "state": row[2],
                        "available_at": _format_time(row[3]),
                        "attempt_count": int(row[4]),
                        "worker_id": row[5],
                        "lease_expires_at": (
                            _format_time(row[6]) if row[6] is not None else None
                        ),
                        "claimed_at": (
                            _format_time(row[7]) if row[7] is not None else None
                        ),
                        "completed_at": (
                            _format_time(row[8]) if row[8] is not None else None
                        ),
                        "last_error_code": row[9],
                        "context_id": row[10],
                    }
                return result

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

    @staticmethod
    def _validate_viewer_limit(limit: int, maximum: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
            raise ValueError(f"Viewer repository limit must be between 1 and {maximum}")


class PostgreSQLIncidentWorkRepository:
    """Fenced PostgreSQL lease queue for Incident collection workers."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> Optional[IncidentWorkClaim]:
        validate_claim_request(worker_id, now, lease_duration, max_attempts)
        claim_token = f"claim-{uuid.uuid4().hex}"
        lease_expires_at = now + lease_duration
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT work.incident_id, work.state, work.attempt_count,
                           incident.document
                    FROM incident_work_items AS work
                    JOIN incidents AS incident
                      ON incident.incident_id = work.incident_id
                    WHERE work.available_at <= %s
                      AND (
                        (work.state = 'READY' AND incident.status = 'RECEIVED')
                        OR
                        (work.state = 'RUNNING'
                         AND work.lease_expires_at <= %s
                         AND work.attempt_count < %s
                         AND incident.status = 'COLLECTING')
                      )
                    ORDER BY work.available_at, work.incident_id
                    FOR UPDATE OF work, incident SKIP LOCKED
                    LIMIT 1
                    """,
                    (now, now, max_attempts),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                incident_id, work_state, attempt_count, document = row
                incident = _decode_document(document)
                if work_state == "READY":
                    incident = self._transition_claimed_incident(
                        cursor,
                        incident,
                        now=now,
                    )
                    event_type = "INCIDENT_WORK_CLAIMED"
                else:
                    event_type = "INCIDENT_WORK_RECLAIMED"
                next_attempt = int(attempt_count) + 1
                cursor.execute(
                    """
                    UPDATE incident_work_items
                    SET state = 'RUNNING', claim_token = %s, worker_id = %s,
                        lease_expires_at = %s, attempt_count = %s,
                        claimed_at = %s, completed_at = NULL,
                        last_error_code = NULL
                    WHERE incident_id = %s
                    """,
                    (
                        claim_token,
                        worker_id,
                        lease_expires_at,
                        next_attempt,
                        now,
                        incident_id,
                    ),
                )
                PostgreSQLIncidentRepository._append_audit_event(
                    cursor,
                    incident_id,
                    event_type,
                    now,
                    {"attempt": next_attempt, "worker_id": worker_id},
                )
                return IncidentWorkClaim(
                    incident_id=incident_id,
                    claim_token=claim_token,
                    worker_id=worker_id,
                    lease_expires_at=lease_expires_at,
                    attempt_count=next_attempt,
                    incident=incident,
                )

    def renew(
        self,
        claim: IncidentWorkClaim,
        *,
        now: datetime,
        lease_duration: timedelta,
    ) -> IncidentWorkClaim:
        validate_claim_request(claim.worker_id, now, lease_duration, 1)
        lease_expires_at = now + lease_duration
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE incident_work_items
                    SET lease_expires_at = %s
                    WHERE incident_id = %s AND state = 'RUNNING'
                      AND claim_token = %s AND worker_id = %s
                    RETURNING attempt_count
                    """,
                    (
                        lease_expires_at,
                        claim.incident_id,
                        claim.claim_token,
                        claim.worker_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise InvalidTransition("Incident work claim is stale")
                incident = PostgreSQLIncidentRepository._locked_incident(
                    cursor, claim.incident_id
                )
                return IncidentWorkClaim(
                    incident_id=claim.incident_id,
                    claim_token=claim.claim_token,
                    worker_id=claim.worker_id,
                    lease_expires_at=lease_expires_at,
                    attempt_count=int(row[0]),
                    incident=incident,
                )

    def complete(
        self,
        claim: IncidentWorkClaim,
        *,
        now: datetime,
        outcome: str,
    ) -> None:
        if now.tzinfo is None or outcome not in WORK_OUTCOMES:
            raise ValueError("completion metadata is invalid")
        work_state = "FAILED" if outcome == "FAILED" else "SUCCEEDED"
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._lock_current_claim(cursor, claim)
                incident = PostgreSQLIncidentRepository._locked_incident(
                    cursor, claim.incident_id
                )
                if incident["status"] not in {
                    "LOCALIZING",
                    "ANALYZING",
                    "REPORTED",
                    "PARTIAL",
                    "FAILED",
                }:
                    raise InvalidTransition(
                        "collection work can complete only after Incident collection"
                    )
                cursor.execute(
                    """
                    UPDATE incident_work_items
                    SET state = %s, lease_expires_at = NULL, completed_at = %s,
                        last_error_code = %s
                    WHERE incident_id = %s
                    """,
                    (
                        work_state,
                        now,
                        "COLLECTORS_FAILED" if outcome == "FAILED" else None,
                        claim.incident_id,
                    ),
                )
                PostgreSQLIncidentRepository._append_audit_event(
                    cursor,
                    claim.incident_id,
                    "INCIDENT_WORK_COMPLETED",
                    now,
                    {"attempt": claim.attempt_count, "outcome": outcome},
                )

    def fail(
        self,
        claim: IncidentWorkClaim,
        *,
        now: datetime,
        error_code: str,
    ) -> None:
        if now.tzinfo is None or not error_code.strip():
            raise ValueError("failure metadata is invalid")
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._lock_current_claim(cursor, claim)
                incident = PostgreSQLIncidentRepository._locked_incident(
                    cursor, claim.incident_id
                )
                if incident["status"] == "COLLECTING":
                    incident = self._transition_to_failed(cursor, incident, now)
                elif incident["status"] != "FAILED":
                    raise InvalidTransition(
                        "work failure requires a COLLECTING Incident"
                    )
                cursor.execute(
                    """
                    UPDATE incident_work_items
                    SET state = 'FAILED', lease_expires_at = NULL,
                        completed_at = %s, last_error_code = %s
                    WHERE incident_id = %s
                    """,
                    (now, error_code, claim.incident_id),
                )
                PostgreSQLIncidentRepository._append_audit_event(
                    cursor,
                    claim.incident_id,
                    "INCIDENT_WORK_FAILED",
                    now,
                    {"attempt": claim.attempt_count, "error_code": error_code},
                )

    def reap_exhausted(self, *, now: datetime, max_attempts: int) -> int:
        if now.tzinfo is None or not 1 <= max_attempts <= 10:
            raise ValueError("reaper metadata is invalid")
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT work.incident_id, work.attempt_count, incident.document
                    FROM incident_work_items AS work
                    JOIN incidents AS incident
                      ON incident.incident_id = work.incident_id
                    WHERE work.state = 'RUNNING'
                      AND work.lease_expires_at <= %s
                      AND (
                        incident.status IN (
                            'LOCALIZING', 'ANALYZING', 'REPORTED', 'PARTIAL', 'FAILED'
                        )
                        OR (incident.status = 'COLLECTING'
                            AND work.attempt_count >= %s)
                      )
                    ORDER BY work.lease_expires_at, work.incident_id
                    FOR UPDATE OF work, incident SKIP LOCKED
                    LIMIT 50
                    """,
                    (now, max_attempts),
                )
                rows = cursor.fetchall()
                for incident_id, attempt_count, document in rows:
                    incident = _decode_document(document)
                    if incident["status"] in {
                        "LOCALIZING",
                        "ANALYZING",
                        "REPORTED",
                        "PARTIAL",
                    }:
                        work_state = "SUCCEEDED"
                        error_code = None
                        outcome = "RECOVERED_SUCCEEDED"
                    elif incident["status"] == "FAILED":
                        work_state = "FAILED"
                        error_code = "INCIDENT_ALREADY_FAILED"
                        outcome = "RECOVERED_FAILED"
                    else:
                        self._transition_to_failed(cursor, incident, now)
                        work_state = "FAILED"
                        error_code = "LEASE_ATTEMPTS_EXHAUSTED"
                        outcome = "LEASE_ATTEMPTS_EXHAUSTED"
                    cursor.execute(
                        """
                        UPDATE incident_work_items
                        SET state = %s, lease_expires_at = NULL,
                            completed_at = %s, last_error_code = %s
                        WHERE incident_id = %s
                        """,
                        (work_state, now, error_code, incident_id),
                    )
                    PostgreSQLIncidentRepository._append_audit_event(
                        cursor,
                        incident_id,
                        "INCIDENT_WORK_REAPED",
                        now,
                        {"attempt": int(attempt_count), "outcome": outcome},
                    )
                return len(rows)

    @staticmethod
    def _transition_claimed_incident(
        cursor: Any,
        incident: Mapping[str, Any],
        *,
        now: datetime,
    ) -> Dict[str, Any]:
        if incident["status"] != "RECEIVED":
            raise InvalidTransition("new work claim requires a RECEIVED Incident")
        updated = copy.deepcopy(dict(incident))
        updated["status"] = "COLLECTING"
        updated["updated_at"] = _format_time(now)
        validate_contract("incident.schema.json", updated)
        PostgreSQLIncidentRepository._update_incident(cursor, updated)
        PostgreSQLIncidentRepository._append_audit_event(
            cursor,
            updated["incident_id"],
            "STATUS_TRANSITIONED",
            now,
            {"from": "RECEIVED", "to": "COLLECTING"},
        )
        return updated

    @staticmethod
    def _transition_to_failed(
        cursor: Any,
        incident: Mapping[str, Any],
        now: datetime,
    ) -> Dict[str, Any]:
        if incident["status"] != "COLLECTING":
            raise InvalidTransition("exhausted work requires a COLLECTING Incident")
        updated = copy.deepcopy(dict(incident))
        updated["status"] = "FAILED"
        updated["updated_at"] = _format_time(now)
        validate_contract("incident.schema.json", updated)
        PostgreSQLIncidentRepository._update_incident(cursor, updated)
        PostgreSQLIncidentRepository._append_audit_event(
            cursor,
            updated["incident_id"],
            "STATUS_TRANSITIONED",
            now,
            {"from": "COLLECTING", "to": "FAILED"},
        )
        return updated

    @staticmethod
    def _lock_current_claim(cursor: Any, claim: IncidentWorkClaim) -> None:
        cursor.execute(
            """
            SELECT 1 FROM incident_work_items
            WHERE incident_id = %s AND state = 'RUNNING'
              AND claim_token = %s AND worker_id = %s
            FOR UPDATE
            """,
            (claim.incident_id, claim.claim_token, claim.worker_id),
        )
        if cursor.fetchone() is None:
            raise InvalidTransition("Incident work claim is stale")


class PostgreSQLIncidentLocalizationWorkRepository:
    """Fenced PostgreSQL lease queue for Incident localization workers."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> Optional[IncidentWorkClaim]:
        validate_claim_request(worker_id, now, lease_duration, max_attempts)
        claim_token = f"claim-{uuid.uuid4().hex}"
        lease_expires_at = now + lease_duration
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT work.incident_id, work.state, work.attempt_count,
                           incident.document
                    FROM incident_localization_work_items AS work
                    JOIN incidents AS incident
                      ON incident.incident_id = work.incident_id
                    WHERE work.available_at <= %s
                      AND incident.status = 'LOCALIZING'
                      AND (
                        work.state = 'READY'
                        OR (work.state = 'RUNNING'
                            AND work.lease_expires_at <= %s
                            AND work.attempt_count < %s)
                      )
                    ORDER BY work.available_at, work.incident_id
                    FOR UPDATE OF work, incident SKIP LOCKED
                    LIMIT 1
                    """,
                    (now, now, max_attempts),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                incident_id, work_state, attempt_count, document = row
                incident = _decode_document(document)
                next_attempt = int(attempt_count) + 1
                cursor.execute(
                    """
                    UPDATE incident_localization_work_items
                    SET state = 'RUNNING', claim_token = %s, worker_id = %s,
                        lease_expires_at = %s, attempt_count = %s,
                        claimed_at = %s, completed_at = NULL,
                        last_error_code = NULL
                    WHERE incident_id = %s
                    """,
                    (
                        claim_token,
                        worker_id,
                        lease_expires_at,
                        next_attempt,
                        now,
                        incident_id,
                    ),
                )
                PostgreSQLIncidentRepository._append_audit_event(
                    cursor,
                    incident_id,
                    (
                        "INCIDENT_LOCALIZATION_WORK_CLAIMED"
                        if work_state == "READY"
                        else "INCIDENT_LOCALIZATION_WORK_RECLAIMED"
                    ),
                    now,
                    {"attempt": next_attempt, "worker_id": worker_id},
                )
                return IncidentWorkClaim(
                    incident_id=incident_id,
                    claim_token=claim_token,
                    worker_id=worker_id,
                    lease_expires_at=lease_expires_at,
                    attempt_count=next_attempt,
                    incident=incident,
                )

    def renew(
        self,
        claim: IncidentWorkClaim,
        *,
        now: datetime,
        lease_duration: timedelta,
    ) -> IncidentWorkClaim:
        validate_claim_request(claim.worker_id, now, lease_duration, 1)
        lease_expires_at = now + lease_duration
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE incident_localization_work_items
                    SET lease_expires_at = %s
                    WHERE incident_id = %s AND state = 'RUNNING'
                      AND claim_token = %s AND worker_id = %s
                    RETURNING attempt_count
                    """,
                    (
                        lease_expires_at,
                        claim.incident_id,
                        claim.claim_token,
                        claim.worker_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise InvalidTransition(
                        "Incident localization work claim is stale"
                    )
                incident = PostgreSQLIncidentRepository._locked_incident(
                    cursor, claim.incident_id
                )
                return IncidentWorkClaim(
                    incident_id=claim.incident_id,
                    claim_token=claim.claim_token,
                    worker_id=claim.worker_id,
                    lease_expires_at=lease_expires_at,
                    attempt_count=int(row[0]),
                    incident=incident,
                )

    def complete(
        self,
        claim: IncidentWorkClaim,
        *,
        now: datetime,
        outcome: str,
    ) -> None:
        if now.tzinfo is None or outcome != "SUCCEEDED":
            raise ValueError("localization completion metadata is invalid")
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._lock_current_claim(cursor, claim)
                incident = PostgreSQLIncidentRepository._locked_incident(
                    cursor, claim.incident_id
                )
                if incident["status"] != "ANALYZING":
                    raise InvalidTransition(
                        "localization work can complete only after Incident localization"
                    )
                cursor.execute(
                    """
                    UPDATE incident_localization_work_items
                    SET state = 'SUCCEEDED', lease_expires_at = NULL,
                        completed_at = %s, last_error_code = NULL
                    WHERE incident_id = %s
                    """,
                    (now, claim.incident_id),
                )
                PostgreSQLIncidentRepository._append_audit_event(
                    cursor,
                    claim.incident_id,
                    "INCIDENT_LOCALIZATION_WORK_COMPLETED",
                    now,
                    {"attempt": claim.attempt_count, "outcome": outcome},
                )

    def fail(
        self,
        claim: IncidentWorkClaim,
        *,
        now: datetime,
        error_code: str,
    ) -> None:
        if now.tzinfo is None or not error_code.strip():
            raise ValueError("failure metadata is invalid")
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._lock_current_claim(cursor, claim)
                incident = PostgreSQLIncidentRepository._locked_incident(
                    cursor, claim.incident_id
                )
                if incident["status"] == "LOCALIZING":
                    self._transition_to_failed(cursor, incident, now)
                elif incident["status"] != "FAILED":
                    raise InvalidTransition(
                        "work failure requires a LOCALIZING Incident"
                    )
                cursor.execute(
                    """
                    UPDATE incident_localization_work_items
                    SET state = 'FAILED', lease_expires_at = NULL,
                        completed_at = %s, last_error_code = %s
                    WHERE incident_id = %s
                    """,
                    (now, error_code, claim.incident_id),
                )
                PostgreSQLIncidentRepository._append_audit_event(
                    cursor,
                    claim.incident_id,
                    "INCIDENT_LOCALIZATION_WORK_FAILED",
                    now,
                    {"attempt": claim.attempt_count, "error_code": error_code},
                )

    def reap_exhausted(self, *, now: datetime, max_attempts: int) -> int:
        if now.tzinfo is None or not 1 <= max_attempts <= 10:
            raise ValueError("reaper metadata is invalid")
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT work.incident_id, work.attempt_count, incident.document
                    FROM incident_localization_work_items AS work
                    JOIN incidents AS incident
                      ON incident.incident_id = work.incident_id
                    WHERE work.state = 'RUNNING'
                      AND work.lease_expires_at <= %s
                      AND (
                        incident.status IN ('ANALYZING', 'FAILED')
                        OR (incident.status = 'LOCALIZING'
                            AND work.attempt_count >= %s)
                      )
                    ORDER BY work.lease_expires_at, work.incident_id
                    FOR UPDATE OF work, incident SKIP LOCKED
                    LIMIT 50
                    """,
                    (now, max_attempts),
                )
                rows = cursor.fetchall()
                for incident_id, attempt_count, document in rows:
                    incident = _decode_document(document)
                    if incident["status"] == "ANALYZING":
                        work_state = "SUCCEEDED"
                        error_code = None
                        outcome = "RECOVERED_SUCCEEDED"
                    elif incident["status"] == "FAILED":
                        work_state = "FAILED"
                        error_code = "INCIDENT_ALREADY_FAILED"
                        outcome = "RECOVERED_FAILED"
                    else:
                        self._transition_to_failed(cursor, incident, now)
                        work_state = "FAILED"
                        error_code = "LEASE_ATTEMPTS_EXHAUSTED"
                        outcome = "LEASE_ATTEMPTS_EXHAUSTED"
                    cursor.execute(
                        """
                        UPDATE incident_localization_work_items
                        SET state = %s, lease_expires_at = NULL,
                            completed_at = %s, last_error_code = %s
                        WHERE incident_id = %s
                        """,
                        (work_state, now, error_code, incident_id),
                    )
                    PostgreSQLIncidentRepository._append_audit_event(
                        cursor,
                        incident_id,
                        "INCIDENT_LOCALIZATION_WORK_REAPED",
                        now,
                        {"attempt": int(attempt_count), "outcome": outcome},
                    )
                return len(rows)

    @staticmethod
    def _transition_to_failed(
        cursor: Any,
        incident: Mapping[str, Any],
        now: datetime,
    ) -> Dict[str, Any]:
        if incident["status"] != "LOCALIZING":
            raise InvalidTransition(
                "exhausted localization work requires a LOCALIZING Incident"
            )
        updated = copy.deepcopy(dict(incident))
        updated["status"] = "FAILED"
        updated["updated_at"] = _format_time(now)
        validate_contract("incident.schema.json", updated)
        PostgreSQLIncidentRepository._update_incident(cursor, updated)
        PostgreSQLIncidentRepository._append_audit_event(
            cursor,
            updated["incident_id"],
            "STATUS_TRANSITIONED",
            now,
            {"from": "LOCALIZING", "to": "FAILED"},
        )
        return updated

    @staticmethod
    def _lock_current_claim(cursor: Any, claim: IncidentWorkClaim) -> None:
        cursor.execute(
            """
            SELECT 1 FROM incident_localization_work_items
            WHERE incident_id = %s AND state = 'RUNNING'
              AND claim_token = %s AND worker_id = %s
            FOR UPDATE
            """,
            (claim.incident_id, claim.claim_token, claim.worker_id),
        )
        if cursor.fetchone() is None:
            raise InvalidTransition("Incident localization work claim is stale")


class PostgreSQLIncidentAnalysisWorkRepository:
    """Fenced PostgreSQL lease queue for bounded Agent RCA workers."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> Optional[IncidentAnalysisWorkClaim]:
        return self._claim(
            worker_id=worker_id,
            now=now,
            lease_duration=lease_duration,
            max_attempts=max_attempts,
            incident_id=None,
            eligibility_label=None,
            activated_at=None,
        )

    def claim_incident(
        self,
        incident_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> Optional[IncidentAnalysisWorkClaim]:
        validate_incident_id(incident_id)
        return self._claim(
            worker_id=worker_id,
            now=now,
            lease_duration=lease_duration,
            max_attempts=max_attempts,
            incident_id=incident_id,
            eligibility_label=None,
            activated_at=None,
        )

    def claim_next_eligible(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
        eligibility_label: str,
        activated_at: datetime,
    ) -> Optional[IncidentAnalysisWorkClaim]:
        validate_analysis_eligibility(eligibility_label, activated_at)
        return self._claim(
            worker_id=worker_id,
            now=now,
            lease_duration=lease_duration,
            max_attempts=max_attempts,
            incident_id=None,
            eligibility_label=eligibility_label,
            activated_at=activated_at,
        )

    def _claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
        incident_id: Optional[str],
        eligibility_label: Optional[str],
        activated_at: Optional[datetime],
    ) -> Optional[IncidentAnalysisWorkClaim]:
        validate_claim_request(worker_id, now, lease_duration, max_attempts)
        claim_token = f"claim-{uuid.uuid4().hex}"
        lease_expires_at = now + lease_duration
        target_clause = "" if incident_id is None else "AND work.incident_id = %s"
        parameters: List[Any] = [now, now, max_attempts]
        if incident_id is not None:
            parameters.append(incident_id)
        eligibility_clause = ""
        if eligibility_label is not None:
            if activated_at is None:
                raise ValueError("Agent activation time is required")
            validate_analysis_eligibility(eligibility_label, activated_at)
            eligibility_clause = """
              AND incident.created_at >= %s
              AND incident.document->'alert'->'labels'->>%s = 'true'
            """
            parameters.extend((activated_at, eligibility_label))
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT work.incident_id, work.context_id, work.state,
                           work.attempt_count, incident.document
                    FROM incident_analysis_work_items AS work
                    JOIN incidents AS incident
                      ON incident.incident_id = work.incident_id
                    WHERE work.available_at <= %s
                      AND incident.status = 'ANALYZING'
                      AND (
                        work.state = 'READY'
                        OR (work.state = 'RUNNING'
                            AND work.lease_expires_at <= %s
                            AND work.attempt_count < %s)
                      )
                      {target_clause}
                      {eligibility_clause}
                    ORDER BY work.available_at, work.incident_id
                    FOR UPDATE OF work, incident SKIP LOCKED
                    LIMIT 1
                    """,
                    parameters,
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                incident_id, context_id, work_state, attempt_count, document = row
                incident = _decode_document(document)
                next_attempt = int(attempt_count) + 1
                cursor.execute(
                    """
                    UPDATE incident_analysis_work_items
                    SET state = 'RUNNING', claim_token = %s, worker_id = %s,
                        lease_expires_at = %s, attempt_count = %s,
                        claimed_at = %s, completed_at = NULL,
                        last_error_code = NULL
                    WHERE incident_id = %s
                    """,
                    (
                        claim_token,
                        worker_id,
                        lease_expires_at,
                        next_attempt,
                        now,
                        incident_id,
                    ),
                )
                PostgreSQLIncidentRepository._append_audit_event(
                    cursor,
                    incident_id,
                    (
                        "INCIDENT_ANALYSIS_WORK_CLAIMED"
                        if work_state == "READY"
                        else "INCIDENT_ANALYSIS_WORK_RECLAIMED"
                    ),
                    now,
                    {
                        "attempt": next_attempt,
                        "worker_id": worker_id,
                        "context_id": context_id,
                    },
                )
                return IncidentAnalysisWorkClaim(
                    incident_id=incident_id,
                    context_id=context_id,
                    claim_token=claim_token,
                    worker_id=worker_id,
                    lease_expires_at=lease_expires_at,
                    attempt_count=next_attempt,
                    incident=incident,
                )

    def renew(
        self,
        claim: IncidentAnalysisWorkClaim,
        *,
        now: datetime,
        lease_duration: timedelta,
    ) -> IncidentAnalysisWorkClaim:
        validate_claim_request(claim.worker_id, now, lease_duration, 1)
        lease_expires_at = now + lease_duration
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE incident_analysis_work_items
                    SET lease_expires_at = %s
                    WHERE incident_id = %s AND context_id = %s
                      AND state = 'RUNNING'
                      AND claim_token = %s AND worker_id = %s
                    RETURNING attempt_count
                    """,
                    (
                        lease_expires_at,
                        claim.incident_id,
                        claim.context_id,
                        claim.claim_token,
                        claim.worker_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise InvalidTransition("Incident analysis work claim is stale")
                incident = PostgreSQLIncidentRepository._locked_incident(
                    cursor, claim.incident_id
                )
                return IncidentAnalysisWorkClaim(
                    incident_id=claim.incident_id,
                    context_id=claim.context_id,
                    claim_token=claim.claim_token,
                    worker_id=claim.worker_id,
                    lease_expires_at=lease_expires_at,
                    attempt_count=int(row[0]),
                    incident=incident,
                )

    def complete(
        self,
        claim: IncidentAnalysisWorkClaim,
        *,
        now: datetime,
        outcome: str,
    ) -> None:
        if now.tzinfo is None or outcome != "SUCCEEDED":
            raise ValueError("analysis completion metadata is invalid")
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._lock_current_claim(cursor, claim)
                incident = PostgreSQLIncidentRepository._locked_incident(
                    cursor, claim.incident_id
                )
                if incident["status"] != "REPORTED":
                    raise InvalidTransition(
                        "analysis work can complete only after Agent reporting"
                    )
                cursor.execute(
                    """
                    UPDATE incident_analysis_work_items
                    SET state = 'SUCCEEDED', lease_expires_at = NULL,
                        completed_at = %s, last_error_code = NULL
                    WHERE incident_id = %s
                    """,
                    (now, claim.incident_id),
                )
                PostgreSQLIncidentRepository._append_audit_event(
                    cursor,
                    claim.incident_id,
                    "INCIDENT_ANALYSIS_WORK_COMPLETED",
                    now,
                    {
                        "attempt": claim.attempt_count,
                        "outcome": outcome,
                        "context_id": claim.context_id,
                    },
                )

    def fail(
        self,
        claim: IncidentAnalysisWorkClaim,
        *,
        now: datetime,
        error_code: str,
    ) -> None:
        if now.tzinfo is None or not error_code.strip():
            raise ValueError("failure metadata is invalid")
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                self._lock_current_claim(cursor, claim)
                incident = PostgreSQLIncidentRepository._locked_incident(
                    cursor, claim.incident_id
                )
                if incident["status"] == "ANALYZING":
                    self._transition_to_failed(cursor, incident, now)
                elif incident["status"] != "FAILED":
                    raise InvalidTransition(
                        "work failure requires an ANALYZING Incident"
                    )
                cursor.execute(
                    """
                    UPDATE incident_analysis_work_items
                    SET state = 'FAILED', lease_expires_at = NULL,
                        completed_at = %s, last_error_code = %s
                    WHERE incident_id = %s
                    """,
                    (now, error_code, claim.incident_id),
                )
                PostgreSQLIncidentRepository._append_audit_event(
                    cursor,
                    claim.incident_id,
                    "INCIDENT_ANALYSIS_WORK_FAILED",
                    now,
                    {
                        "attempt": claim.attempt_count,
                        "error_code": error_code,
                        "context_id": claim.context_id,
                    },
                )

    def reap_exhausted(self, *, now: datetime, max_attempts: int) -> int:
        return self._reap_exhausted(
            now=now,
            max_attempts=max_attempts,
            eligibility_label=None,
            activated_at=None,
        )

    def reap_exhausted_eligible(
        self,
        *,
        now: datetime,
        max_attempts: int,
        eligibility_label: str,
        activated_at: datetime,
    ) -> int:
        validate_analysis_eligibility(eligibility_label, activated_at)
        return self._reap_exhausted(
            now=now,
            max_attempts=max_attempts,
            eligibility_label=eligibility_label,
            activated_at=activated_at,
        )

    def _reap_exhausted(
        self,
        *,
        now: datetime,
        max_attempts: int,
        eligibility_label: Optional[str],
        activated_at: Optional[datetime],
    ) -> int:
        if now.tzinfo is None or not 1 <= max_attempts <= 10:
            raise ValueError("reaper metadata is invalid")
        eligibility_clause = ""
        parameters: List[Any] = [now, max_attempts]
        if eligibility_label is not None:
            if activated_at is None:
                raise ValueError("Agent activation time is required")
            validate_analysis_eligibility(eligibility_label, activated_at)
            eligibility_clause = """
              AND incident.created_at >= %s
              AND incident.document->'alert'->'labels'->>%s = 'true'
            """
            parameters.extend((activated_at, eligibility_label))
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT work.incident_id, work.attempt_count, incident.document
                    FROM incident_analysis_work_items AS work
                    JOIN incidents AS incident
                      ON incident.incident_id = work.incident_id
                    WHERE work.state = 'RUNNING'
                      AND work.lease_expires_at <= %s
                      AND (
                        incident.status IN ('REPORTED', 'PARTIAL', 'FAILED')
                        OR (incident.status = 'ANALYZING'
                            AND work.attempt_count >= %s)
                      )
                      {eligibility_clause}
                    ORDER BY work.lease_expires_at, work.incident_id
                    FOR UPDATE OF work, incident SKIP LOCKED
                    LIMIT 50
                    """,
                    parameters,
                )
                rows = cursor.fetchall()
                for incident_id, attempt_count, document in rows:
                    incident = _decode_document(document)
                    if incident["status"] in {"REPORTED", "PARTIAL"}:
                        work_state = "SUCCEEDED"
                        error_code = None
                        outcome = "RECOVERED_SUCCEEDED"
                    elif incident["status"] == "FAILED":
                        work_state = "FAILED"
                        error_code = "INCIDENT_ALREADY_FAILED"
                        outcome = "RECOVERED_FAILED"
                    else:
                        self._transition_to_failed(cursor, incident, now)
                        work_state = "FAILED"
                        error_code = "LEASE_ATTEMPTS_EXHAUSTED"
                        outcome = "LEASE_ATTEMPTS_EXHAUSTED"
                    cursor.execute(
                        """
                        UPDATE incident_analysis_work_items
                        SET state = %s, lease_expires_at = NULL,
                            completed_at = %s, last_error_code = %s
                        WHERE incident_id = %s
                        """,
                        (work_state, now, error_code, incident_id),
                    )
                    PostgreSQLIncidentRepository._append_audit_event(
                        cursor,
                        incident_id,
                        "INCIDENT_ANALYSIS_WORK_REAPED",
                        now,
                        {"attempt": int(attempt_count), "outcome": outcome},
                    )
                return len(rows)

    @staticmethod
    def _transition_to_failed(
        cursor: Any,
        incident: Mapping[str, Any],
        now: datetime,
    ) -> Dict[str, Any]:
        if incident["status"] != "ANALYZING":
            raise InvalidTransition(
                "exhausted analysis work requires an ANALYZING Incident"
            )
        updated = copy.deepcopy(dict(incident))
        updated["status"] = "FAILED"
        updated["updated_at"] = _format_time(now)
        validate_contract("incident.schema.json", updated)
        PostgreSQLIncidentRepository._update_incident(cursor, updated)
        PostgreSQLIncidentRepository._append_audit_event(
            cursor,
            updated["incident_id"],
            "STATUS_TRANSITIONED",
            now,
            {"from": "ANALYZING", "to": "FAILED"},
        )
        return updated

    @staticmethod
    def _lock_current_claim(
        cursor: Any, claim: IncidentAnalysisWorkClaim
    ) -> None:
        cursor.execute(
            """
            SELECT 1 FROM incident_analysis_work_items
            WHERE incident_id = %s AND context_id = %s AND state = 'RUNNING'
              AND claim_token = %s AND worker_id = %s
            FOR UPDATE
            """,
            (
                claim.incident_id,
                claim.context_id,
                claim.claim_token,
                claim.worker_id,
            ),
        )
        if cursor.fetchone() is None:
            raise InvalidTransition("Incident analysis work claim is stale")


class PostgreSQLIncidentWorkQueueTelemetryRepository:
    """Read-only queue snapshot for Prometheus without exposing claim tokens."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def snapshot(
        self,
        *,
        now: datetime,
        analysis_eligibility_label: str,
        analysis_activated_at: datetime,
    ) -> IncidentWorkQueueSnapshot:
        validate_analysis_eligibility(
            analysis_eligibility_label,
            analysis_activated_at,
        )
        if now.tzinfo is None:
            raise ValueError("queue observation time must be timezone-aware")
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH queue_rows AS (
                        SELECT 'collection'::text AS stage, state,
                               available_at, claimed_at
                        FROM incident_work_items
                        UNION ALL
                        SELECT 'localization'::text AS stage, state,
                               available_at, claimed_at
                        FROM incident_localization_work_items
                        UNION ALL
                        SELECT 'analysis'::text AS stage, work.state,
                               work.available_at, work.claimed_at
                        FROM incident_analysis_work_items AS work
                        JOIN incidents AS incident
                          ON incident.incident_id = work.incident_id
                        WHERE incident.created_at >= %s
                          AND incident.document->'alert'->'labels'->>%s = 'true'
                    )
                    SELECT stage,
                           COUNT(*) FILTER (WHERE state = 'READY') AS ready,
                           COUNT(*) FILTER (WHERE state = 'RUNNING') AS running,
                           COUNT(*) FILTER (WHERE state = 'SUCCEEDED') AS succeeded,
                           COUNT(*) FILTER (WHERE state = 'FAILED') AS failed,
                           GREATEST(0, COALESCE(EXTRACT(EPOCH FROM (
                               %s::timestamptz - MIN(available_at) FILTER (
                                   WHERE state = 'READY' AND available_at <= %s
                               )
                           )), 0)) AS oldest_ready_age_seconds,
                           GREATEST(0, COALESCE(EXTRACT(EPOCH FROM (
                               %s::timestamptz - MIN(claimed_at) FILTER (
                                   WHERE state = 'RUNNING' AND claimed_at IS NOT NULL
                               )
                           )), 0)) AS oldest_running_age_seconds
                    FROM queue_rows
                    GROUP BY stage
                    """,
                    (
                        analysis_activated_at,
                        analysis_eligibility_label,
                        now,
                        now,
                        now,
                    ),
                )
                observed = {
                    row[0]: IncidentWorkQueueStageSnapshot(
                        stage=row[0],
                        ready=int(row[1]),
                        running=int(row[2]),
                        succeeded=int(row[3]),
                        failed=int(row[4]),
                        oldest_ready_age_seconds=float(row[5]),
                        oldest_running_age_seconds=float(row[6]),
                    )
                    for row in cursor.fetchall()
                }
        stages = tuple(
            observed.get(
                stage,
                IncidentWorkQueueStageSnapshot(
                    stage=stage,
                    ready=0,
                    running=0,
                    succeeded=0,
                    failed=0,
                    oldest_ready_age_seconds=0,
                    oldest_running_age_seconds=0,
                ),
            )
            for stage in WORK_QUEUE_STAGES
        )
        return IncidentWorkQueueSnapshot(observed_at=now, stages=stages)


class PostgreSQLStateGraphObservationRepository:
    """PostgreSQL journal for durable background Evidence and retry state."""

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        retention_policy: Optional[StateGraphObservationRetentionPolicy] = None,
    ) -> None:
        self._connection_factory = connection_factory
        self._retention_policy = (
            retention_policy or StateGraphObservationRetentionPolicy()
        )

    def stage_cycle(
        self,
        cycle: StateGraphObservationCycle,
        evidence: Sequence[Mapping[str, Any]],
    ) -> StateGraphObservationCycle:
        if cycle.status != "STAGED":
            raise InvalidTransition("only a STAGED observation cycle can be staged")
        candidates = validate_cycle_evidence(cycle, evidence)
        document = cycle.to_document()
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO stategraph_observation_cycles (
                        cycle_id, request_id, evidence_scope_id, cluster_id,
                        namespace, status, observed_at, staged_at, applied_at,
                        evidence_count, result, document
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, NULL,
                        %s::jsonb
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING cycle_id
                    """,
                    (
                        cycle.cycle_id,
                        cycle.request_id,
                        cycle.evidence_scope_id,
                        cycle.cluster_id,
                        cycle.namespace,
                        cycle.status,
                        cycle.observed_at,
                        cycle.staged_at,
                        len(candidates),
                        _json(document),
                    ),
                )
                if cursor.fetchone() is None:
                    return self._verify_existing_cycle(cursor, cycle, candidates)
                for candidate in candidates:
                    cursor.execute(
                        """
                        INSERT INTO stategraph_observation_evidence (
                            evidence_id, cycle_id, content_hash, observed_at,
                            document
                        ) VALUES (%s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (evidence_id) DO NOTHING
                        RETURNING evidence_id
                        """,
                        (
                            candidate["evidence_id"],
                            cycle.cycle_id,
                            candidate["provenance"]["content_hash"],
                            candidate["observed_at"],
                            _json(candidate),
                        ),
                    )
                    if cursor.fetchone() is None:
                        raise InvalidTransition(
                            "observation evidence_id belongs to another cycle: "
                            f"{candidate['evidence_id']}"
                        )
                return cycle

    def mark_cycle_applied(
        self,
        cycle_id: str,
        result: StateGraphReconciliationResult,
        *,
        applied_at: datetime,
    ) -> StateGraphObservationCycle:
        applied_text = _format_time(applied_at)
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT document FROM stategraph_observation_cycles
                    WHERE cycle_id = %s FOR UPDATE
                    """,
                    (cycle_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown observation cycle: {cycle_id}")
                existing = StateGraphObservationCycle.from_document(
                    _decode_document(row[0])
                )
                updated = replace(
                    existing,
                    status="APPLIED",
                    applied_at=applied_text,
                    result=result,
                )
                if existing.status == "APPLIED":
                    if existing != updated:
                        raise InvalidTransition(
                            f"observation cycle result collision: {cycle_id}"
                        )
                    return existing
                cursor.execute(
                    """
                    UPDATE stategraph_observation_cycles
                    SET status = 'APPLIED', applied_at = %s,
                        result = %s::jsonb, document = %s::jsonb
                    WHERE cycle_id = %s
                    """,
                    (
                        applied_text,
                        _json(_result_document(result)),
                        _json(updated.to_document()),
                        cycle_id,
                    ),
                )
                return updated

    def get_cycle(self, cycle_id: str) -> StateGraphObservationCycle:
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT document FROM stategraph_observation_cycles
                    WHERE cycle_id = %s
                    """,
                    (cycle_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise KeyError(f"unknown observation cycle: {cycle_id}")
                return StateGraphObservationCycle.from_document(
                    _decode_document(row[0])
                )

    def list_cycle_evidence(self, cycle_id: str) -> Tuple[Mapping[str, Any], ...]:
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM stategraph_observation_cycles WHERE cycle_id = %s",
                    (cycle_id,),
                )
                if cursor.fetchone() is None:
                    raise KeyError(f"unknown observation cycle: {cycle_id}")
                cursor.execute(
                    """
                    SELECT document FROM stategraph_observation_evidence
                    WHERE cycle_id = %s
                    ORDER BY evidence_id
                    """,
                    (cycle_id,),
                )
                return tuple(
                    _decode_document(row[0]) for row in cursor.fetchall()
                )

    def prune_observations(
        self,
        *,
        now: datetime,
        batch_size: int = 1000,
    ) -> StateGraphObservationPruneResult:
        if not 1 <= batch_size <= 10_000:
            raise ValueError("observation prune batch_size must be between 1 and 10000")
        now_text = _format_time(now)
        now_utc = parse_time(now_text, "ObservationPrune.now")
        applied_cutoff = now_utc - self._retention_policy.applied_history
        staged_cutoff = now_utc - self._retention_policy.abandoned_staged_history
        with _connection(self._connection_factory) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT cycle_id, evidence_count
                    FROM stategraph_observation_cycles
                    WHERE (
                        status = 'APPLIED' AND applied_at <= %s
                    ) OR (
                        status = 'STAGED' AND staged_at <= %s
                    )
                    ORDER BY COALESCE(applied_at, staged_at), cycle_id
                    LIMIT %s
                    FOR UPDATE
                    """,
                    (applied_cutoff, staged_cutoff, batch_size),
                )
                rows = cursor.fetchall()
                if not rows:
                    return StateGraphObservationPruneResult()
                cycle_ids = [row[0] for row in rows]
                cursor.execute(
                    """
                    DELETE FROM stategraph_observation_cycles
                    WHERE cycle_id = ANY(%s)
                    """,
                    (cycle_ids,),
                )
                return StateGraphObservationPruneResult(
                    cycles=len(rows),
                    evidence_items=sum(int(row[1]) for row in rows),
                )

    @staticmethod
    def _verify_existing_cycle(
        cursor: Any,
        candidate: StateGraphObservationCycle,
        evidence: Sequence[Mapping[str, Any]],
    ) -> StateGraphObservationCycle:
        cursor.execute(
            """
            SELECT document FROM stategraph_observation_cycles
            WHERE cycle_id = %s OR request_id = %s
            FOR UPDATE
            """,
            (candidate.cycle_id, candidate.request_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise InvalidTransition(
                "observation cycle insert conflicted without a visible owner"
            )
        existing = StateGraphObservationCycle.from_document(
            _decode_document(row[0])
        )
        if existing.staging_identity() != candidate.staging_identity():
            raise InvalidTransition(
                f"observation cycle collision: {candidate.cycle_id}"
            )
        cursor.execute(
            """
            SELECT document FROM stategraph_observation_evidence
            WHERE cycle_id = %s ORDER BY evidence_id
            """,
            (existing.cycle_id,),
        )
        stored = tuple(_decode_document(row[0]) for row in cursor.fetchall())
        if stored != tuple(evidence):
            raise InvalidTransition(
                f"observation cycle Evidence collision: {candidate.cycle_id}"
            )
        return existing


def _result_document(result: StateGraphReconciliationResult) -> Dict[str, Any]:
    return {
        "ingested_records": result.ingested_records,
        "current_entities": result.current_entities,
        "current_relations": result.current_relations,
        "retired_entities": result.retired_entities,
        "closed_snapshot_intervals": result.closed_snapshot_intervals,
        "closed_relation_intervals": result.closed_relation_intervals,
    }
