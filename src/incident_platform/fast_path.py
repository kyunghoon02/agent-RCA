"""Deterministic Fast Path orchestration from stored Evidence to RCA Report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .deterministic import DeterministicDecision, DeterministicRCAEngine
from .errors import InvalidTransition
from .reporting import FastPathArtifacts, FastPathReportBuilder
from .repository import IncidentRepository


@dataclass(frozen=True)
class FastPathRun:
    """Persisted deterministic decision, artifacts, and final Incident state."""

    decision: DeterministicDecision
    artifacts: FastPathArtifacts
    incident: Dict[str, Any]


class IncidentFastPathService:
    """Advance one LOCALIZING Incident through deterministic reporting.

    The service is deliberately read-only toward Kubernetes and cloud systems.
    Its only writes are Incident lifecycle and RCA artifact persistence.
    """

    def __init__(
        self,
        repository: IncidentRepository,
        *,
        engine: Optional[DeterministicRCAEngine] = None,
        report_builder: Optional[FastPathReportBuilder] = None,
    ) -> None:
        self._repository = repository
        self._engine = engine or DeterministicRCAEngine()
        self._report_builder = report_builder or FastPathReportBuilder()

    def run(
        self,
        incident_id: str,
        *,
        generated_at: Optional[datetime] = None,
    ) -> FastPathRun:
        now = generated_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")

        incident = self._repository.get(incident_id)
        if incident["status"] != "LOCALIZING":
            raise InvalidTransition(
                f"Fast Path requires LOCALIZING, found {incident['status']}"
            )
        analyzing = self._repository.transition(
            incident_id,
            expected_status="LOCALIZING",
            next_status="ANALYZING",
            occurred_at=now,
        )

        try:
            evidence = self._repository.list_evidence(incident_id)
            decision = self._engine.evaluate(evidence)
            artifacts = self._report_builder.build(
                incident=analyzing,
                evidence=evidence,
                decision=decision,
                generated_at=now,
            )
            self._repository.store_context(artifacts.context)
            self._repository.store_report(artifacts.report, artifacts.markdown)
            reported = self._repository.transition(
                incident_id,
                expected_status="ANALYZING",
                next_status="REPORTED",
                occurred_at=now,
            )
        except Exception:
            self._mark_failed_without_masking(incident_id, now)
            raise

        return FastPathRun(
            decision=decision,
            artifacts=artifacts,
            incident=reported,
        )

    def _mark_failed_without_masking(
        self, incident_id: str, occurred_at: datetime
    ) -> None:
        try:
            self._repository.transition(
                incident_id,
                expected_status="ANALYZING",
                next_status="FAILED",
                occurred_at=occurred_at,
            )
        except Exception:
            # Preserve the analysis/persistence exception as the primary failure.
            pass
