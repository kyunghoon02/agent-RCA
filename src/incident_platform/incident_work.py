"""Durable Incident collection work claims with lease fencing."""

from __future__ import annotations

import copy
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from typing import Any, Dict, Mapping, Optional, Protocol

from .errors import InvalidTransition
from .repository import IncidentRepository


_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_INCIDENT_ID = re.compile(r"^inc-[a-z0-9][a-z0-9-]{7,63}$")
WORK_OUTCOMES = frozenset({"SUCCEEDED", "PARTIAL", "FAILED"})


def validate_claim_request(
    worker_id: str,
    now: datetime,
    lease_duration: timedelta,
    max_attempts: int,
) -> None:
    if not _WORKER_ID.fullmatch(worker_id):
        raise ValueError("worker_id is invalid")
    if now.tzinfo is None:
        raise ValueError("claim time must be timezone-aware")
    if not timedelta(seconds=10) <= lease_duration <= timedelta(minutes=30):
        raise ValueError("lease duration must be between 10 seconds and 30 minutes")
    if not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts must be between 1 and 10")


def validate_incident_id(incident_id: str) -> None:
    """Validate the shared Incident contract identity before targeted claims."""

    if not _INCIDENT_ID.fullmatch(incident_id):
        raise ValueError("incident_id is invalid")


@dataclass(frozen=True)
class IncidentWorkClaim:
    incident_id: str
    claim_token: str
    worker_id: str
    lease_expires_at: datetime
    attempt_count: int
    incident: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.incident_id or self.incident.get("incident_id") != self.incident_id:
            raise ValueError("claim Incident identity is invalid")
        if not self.claim_token.startswith("claim-"):
            raise ValueError("claim token is invalid")
        if not _WORKER_ID.fullmatch(self.worker_id):
            raise ValueError("claim worker_id is invalid")
        if self.lease_expires_at.tzinfo is None:
            raise ValueError("claim lease must be timezone-aware")
        if self.attempt_count <= 0:
            raise ValueError("claim attempt_count must be positive")


@dataclass(frozen=True)
class IncidentAnalysisWorkClaim:
    """One fenced ANALYZING claim pinned to an immutable Context Package."""

    incident_id: str
    context_id: str
    claim_token: str
    worker_id: str
    lease_expires_at: datetime
    attempt_count: int
    incident: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.incident_id or self.incident.get("incident_id") != self.incident_id:
            raise ValueError("analysis claim Incident identity is invalid")
        if not self.context_id.startswith("ctx-"):
            raise ValueError("analysis claim Context identity is invalid")
        if not self.claim_token.startswith("claim-"):
            raise ValueError("analysis claim token is invalid")
        if not _WORKER_ID.fullmatch(self.worker_id):
            raise ValueError("analysis claim worker_id is invalid")
        if self.lease_expires_at.tzinfo is None:
            raise ValueError("analysis claim lease must be timezone-aware")
        if self.attempt_count <= 0:
            raise ValueError("analysis claim attempt_count must be positive")


class IncidentWorkRepository(Protocol):
    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> Optional[IncidentWorkClaim]:
        ...

    def renew(
        self,
        claim: IncidentWorkClaim,
        *,
        now: datetime,
        lease_duration: timedelta,
    ) -> IncidentWorkClaim:
        ...

    def complete(
        self,
        claim: IncidentWorkClaim,
        *,
        now: datetime,
        outcome: str,
    ) -> None:
        ...

    def fail(
        self,
        claim: IncidentWorkClaim,
        *,
        now: datetime,
        error_code: str,
    ) -> None:
        ...

    def reap_exhausted(self, *, now: datetime, max_attempts: int) -> int:
        ...


class IncidentLocalizationWorkRepository(IncidentWorkRepository, Protocol):
    """Durable LOCALIZING work boundary using the shared fenced claim contract."""


class IncidentAnalysisWorkRepository(Protocol):
    """Durable ANALYZING work boundary pinned to one frozen Context."""

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> Optional[IncidentAnalysisWorkClaim]:
        ...

    def claim_incident(
        self,
        incident_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> Optional[IncidentAnalysisWorkClaim]:
        """Claim only the requested Incident, without falling back to another."""

        ...

    def renew(
        self,
        claim: IncidentAnalysisWorkClaim,
        *,
        now: datetime,
        lease_duration: timedelta,
    ) -> IncidentAnalysisWorkClaim:
        ...

    def complete(
        self,
        claim: IncidentAnalysisWorkClaim,
        *,
        now: datetime,
        outcome: str,
    ) -> None:
        ...

    def fail(
        self,
        claim: IncidentAnalysisWorkClaim,
        *,
        now: datetime,
        error_code: str,
    ) -> None:
        ...

    def reap_exhausted(self, *, now: datetime, max_attempts: int) -> int:
        ...


class InMemoryIncidentWorkRepository:
    """Thread-safe claim adapter used by deterministic worker contract tests."""

    def __init__(self, incident_repository: IncidentRepository) -> None:
        self._incidents = incident_repository
        self._items: Dict[str, Dict[str, Any]] = {}
        self._lock = RLock()

    def enqueue(self, incident_id: str, *, available_at: datetime) -> None:
        if available_at.tzinfo is None:
            raise ValueError("available_at must be timezone-aware")
        incident = self._incidents.get(incident_id)
        if incident["status"] != "RECEIVED":
            raise InvalidTransition("only RECEIVED Incidents can be enqueued")
        with self._lock:
            self._items.setdefault(
                incident_id,
                {
                    "state": "READY",
                    "available_at": available_at,
                    "claim_token": None,
                    "worker_id": None,
                    "lease_expires_at": None,
                    "attempt_count": 0,
                },
            )

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> Optional[IncidentWorkClaim]:
        validate_claim_request(worker_id, now, lease_duration, max_attempts)
        with self._lock:
            for incident_id, item in sorted(
                self._items.items(),
                key=lambda pair: (pair[1]["available_at"], pair[0]),
            ):
                ready = item["state"] == "READY" and item["available_at"] <= now
                expired = (
                    item["state"] == "RUNNING"
                    and item["lease_expires_at"] <= now
                    and item["attempt_count"] < max_attempts
                )
                if not ready and not expired:
                    continue
                incident = self._incidents.get(incident_id)
                if ready:
                    incident = self._incidents.transition(
                        incident_id,
                        expected_status="RECEIVED",
                        next_status="COLLECTING",
                        occurred_at=now,
                    )
                elif incident["status"] != "COLLECTING":
                    raise InvalidTransition(
                        "expired collection work requires a COLLECTING Incident"
                    )
                token = f"claim-{uuid.uuid4().hex}"
                item.update(
                    {
                        "state": "RUNNING",
                        "claim_token": token,
                        "worker_id": worker_id,
                        "lease_expires_at": now + lease_duration,
                        "attempt_count": item["attempt_count"] + 1,
                    }
                )
                return IncidentWorkClaim(
                    incident_id=incident_id,
                    claim_token=token,
                    worker_id=worker_id,
                    lease_expires_at=item["lease_expires_at"],
                    attempt_count=item["attempt_count"],
                    incident=copy.deepcopy(incident),
                )
        return None

    def renew(
        self,
        claim: IncidentWorkClaim,
        *,
        now: datetime,
        lease_duration: timedelta,
    ) -> IncidentWorkClaim:
        validate_claim_request(claim.worker_id, now, lease_duration, 1)
        with self._lock:
            item = self._current_item(claim)
            item["lease_expires_at"] = now + lease_duration
            return IncidentWorkClaim(
                incident_id=claim.incident_id,
                claim_token=claim.claim_token,
                worker_id=claim.worker_id,
                lease_expires_at=item["lease_expires_at"],
                attempt_count=item["attempt_count"],
                incident=self._incidents.get(claim.incident_id),
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
        with self._lock:
            item = self._current_item(claim)
            incident_status = self._incidents.get(claim.incident_id)["status"]
            if incident_status not in {
                "LOCALIZING",
                "ANALYZING",
                "REPORTED",
                "PARTIAL",
                "FAILED",
            }:
                raise InvalidTransition(
                    "collection work can complete only after Incident collection"
                )
            item["state"] = "FAILED" if outcome == "FAILED" else "SUCCEEDED"
            item["lease_expires_at"] = None

    def fail(
        self,
        claim: IncidentWorkClaim,
        *,
        now: datetime,
        error_code: str,
    ) -> None:
        if now.tzinfo is None or not error_code.strip():
            raise ValueError("failure metadata is invalid")
        with self._lock:
            item = self._current_item(claim)
            incident = self._incidents.get(claim.incident_id)
            if incident["status"] == "COLLECTING":
                self._incidents.transition(
                    claim.incident_id,
                    expected_status="COLLECTING",
                    next_status="FAILED",
                    occurred_at=now,
                )
            elif incident["status"] != "FAILED":
                raise InvalidTransition("work failure requires a COLLECTING Incident")
            item["state"] = "FAILED"
            item["lease_expires_at"] = None
            item["error_code"] = error_code

    def reap_exhausted(self, *, now: datetime, max_attempts: int) -> int:
        if now.tzinfo is None or not 1 <= max_attempts <= 10:
            raise ValueError("reaper metadata is invalid")
        reaped = 0
        with self._lock:
            for incident_id, item in self._items.items():
                if not (
                    item["state"] == "RUNNING"
                    and item["lease_expires_at"] <= now
                ):
                    continue
                incident = self._incidents.get(incident_id)
                if incident["status"] in {
                    "LOCALIZING",
                    "ANALYZING",
                    "REPORTED",
                    "PARTIAL",
                }:
                    item["state"] = "SUCCEEDED"
                    item["error_code"] = None
                elif incident["status"] == "FAILED":
                    item["state"] = "FAILED"
                    item["error_code"] = "INCIDENT_ALREADY_FAILED"
                elif (
                    incident["status"] == "COLLECTING"
                    and item["attempt_count"] >= max_attempts
                ):
                    self._incidents.transition(
                        incident_id,
                        expected_status="COLLECTING",
                        next_status="FAILED",
                        occurred_at=now,
                    )
                    item["state"] = "FAILED"
                    item["error_code"] = "LEASE_ATTEMPTS_EXHAUSTED"
                else:
                    continue
                item["lease_expires_at"] = None
                reaped += 1
        return reaped

    def _current_item(self, claim: IncidentWorkClaim) -> Dict[str, Any]:
        item = self._items.get(claim.incident_id)
        if (
            item is None
            or item["state"] != "RUNNING"
            or item["claim_token"] != claim.claim_token
            or item["worker_id"] != claim.worker_id
        ):
            raise InvalidTransition("Incident work claim is stale")
        return item


class InMemoryIncidentLocalizationWorkRepository:
    """Thread-safe localization claim adapter used by runtime contract tests."""

    def __init__(self, incident_repository: IncidentRepository) -> None:
        self._incidents = incident_repository
        self._items: Dict[str, Dict[str, Any]] = {}
        self._lock = RLock()

    def enqueue(self, incident_id: str, *, available_at: datetime) -> None:
        if available_at.tzinfo is None:
            raise ValueError("available_at must be timezone-aware")
        incident = self._incidents.get(incident_id)
        if incident["status"] != "LOCALIZING":
            raise InvalidTransition("only LOCALIZING Incidents can be enqueued")
        with self._lock:
            self._items.setdefault(
                incident_id,
                {
                    "state": "READY",
                    "available_at": available_at,
                    "claim_token": None,
                    "worker_id": None,
                    "lease_expires_at": None,
                    "attempt_count": 0,
                },
            )

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> Optional[IncidentWorkClaim]:
        validate_claim_request(worker_id, now, lease_duration, max_attempts)
        with self._lock:
            for incident_id, item in sorted(
                self._items.items(),
                key=lambda pair: (pair[1]["available_at"], pair[0]),
            ):
                ready = item["state"] == "READY" and item["available_at"] <= now
                expired = (
                    item["state"] == "RUNNING"
                    and item["lease_expires_at"] <= now
                    and item["attempt_count"] < max_attempts
                )
                if not ready and not expired:
                    continue
                incident = self._incidents.get(incident_id)
                if incident["status"] != "LOCALIZING":
                    continue
                token = f"claim-{uuid.uuid4().hex}"
                item.update(
                    {
                        "state": "RUNNING",
                        "claim_token": token,
                        "worker_id": worker_id,
                        "lease_expires_at": now + lease_duration,
                        "attempt_count": item["attempt_count"] + 1,
                    }
                )
                return IncidentWorkClaim(
                    incident_id=incident_id,
                    claim_token=token,
                    worker_id=worker_id,
                    lease_expires_at=item["lease_expires_at"],
                    attempt_count=item["attempt_count"],
                    incident=copy.deepcopy(incident),
                )
        return None

    def renew(
        self,
        claim: IncidentWorkClaim,
        *,
        now: datetime,
        lease_duration: timedelta,
    ) -> IncidentWorkClaim:
        validate_claim_request(claim.worker_id, now, lease_duration, 1)
        with self._lock:
            item = self._current_item(claim)
            item["lease_expires_at"] = now + lease_duration
            return IncidentWorkClaim(
                incident_id=claim.incident_id,
                claim_token=claim.claim_token,
                worker_id=claim.worker_id,
                lease_expires_at=item["lease_expires_at"],
                attempt_count=item["attempt_count"],
                incident=self._incidents.get(claim.incident_id),
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
        with self._lock:
            item = self._current_item(claim)
            if self._incidents.get(claim.incident_id)["status"] != "ANALYZING":
                raise InvalidTransition(
                    "localization work can complete only after Incident localization"
                )
            item["state"] = "SUCCEEDED"
            item["lease_expires_at"] = None

    def fail(
        self,
        claim: IncidentWorkClaim,
        *,
        now: datetime,
        error_code: str,
    ) -> None:
        if now.tzinfo is None or not error_code.strip():
            raise ValueError("failure metadata is invalid")
        with self._lock:
            item = self._current_item(claim)
            incident = self._incidents.get(claim.incident_id)
            if incident["status"] == "LOCALIZING":
                self._incidents.transition(
                    claim.incident_id,
                    expected_status="LOCALIZING",
                    next_status="FAILED",
                    occurred_at=now,
                )
            elif incident["status"] != "FAILED":
                raise InvalidTransition("work failure requires a LOCALIZING Incident")
            item["state"] = "FAILED"
            item["lease_expires_at"] = None
            item["error_code"] = error_code

    def reap_exhausted(self, *, now: datetime, max_attempts: int) -> int:
        if now.tzinfo is None or not 1 <= max_attempts <= 10:
            raise ValueError("reaper metadata is invalid")
        reaped = 0
        with self._lock:
            for incident_id, item in self._items.items():
                if not (
                    item["state"] == "RUNNING"
                    and item["lease_expires_at"] <= now
                ):
                    continue
                incident = self._incidents.get(incident_id)
                if incident["status"] == "ANALYZING":
                    item["state"] = "SUCCEEDED"
                    item["error_code"] = None
                elif incident["status"] == "FAILED":
                    item["state"] = "FAILED"
                    item["error_code"] = "INCIDENT_ALREADY_FAILED"
                elif (
                    incident["status"] == "LOCALIZING"
                    and item["attempt_count"] >= max_attempts
                ):
                    self._incidents.transition(
                        incident_id,
                        expected_status="LOCALIZING",
                        next_status="FAILED",
                        occurred_at=now,
                    )
                    item["state"] = "FAILED"
                    item["error_code"] = "LEASE_ATTEMPTS_EXHAUSTED"
                else:
                    continue
                item["lease_expires_at"] = None
                reaped += 1
        return reaped

    def _current_item(self, claim: IncidentWorkClaim) -> Dict[str, Any]:
        item = self._items.get(claim.incident_id)
        if (
            item is None
            or item["state"] != "RUNNING"
            or item["claim_token"] != claim.claim_token
            or item["worker_id"] != claim.worker_id
        ):
            raise InvalidTransition("Incident localization work claim is stale")
        return item


class InMemoryIncidentAnalysisWorkRepository:
    """Thread-safe ANALYZING claim adapter used by Agent worker tests."""

    def __init__(self, incident_repository: IncidentRepository) -> None:
        self._incidents = incident_repository
        self._items: Dict[str, Dict[str, Any]] = {}
        self._lock = RLock()

    def enqueue(
        self,
        incident_id: str,
        *,
        context_id: str,
        available_at: datetime,
    ) -> None:
        if available_at.tzinfo is None:
            raise ValueError("available_at must be timezone-aware")
        incident = self._incidents.get(incident_id)
        if incident["status"] != "ANALYZING":
            raise InvalidTransition("only ANALYZING Incidents can be enqueued")
        context = self._incidents.get_context(context_id)
        if context["incident_id"] != incident_id:
            raise InvalidTransition("analysis work Context belongs to another Incident")
        with self._lock:
            existing = self._items.get(incident_id)
            if existing is not None and existing["context_id"] != context_id:
                raise InvalidTransition(
                    "analysis work already references a different Context"
                )
            self._items.setdefault(
                incident_id,
                {
                    "context_id": context_id,
                    "state": "READY",
                    "available_at": available_at,
                    "claim_token": None,
                    "worker_id": None,
                    "lease_expires_at": None,
                    "attempt_count": 0,
                },
            )

    def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> Optional[IncidentAnalysisWorkClaim]:
        validate_claim_request(worker_id, now, lease_duration, max_attempts)
        with self._lock:
            for incident_id, item in sorted(
                self._items.items(),
                key=lambda pair: (pair[1]["available_at"], pair[0]),
            ):
                claim = self._claim_available(
                    incident_id,
                    item,
                    worker_id=worker_id,
                    now=now,
                    lease_duration=lease_duration,
                    max_attempts=max_attempts,
                )
                if claim is not None:
                    return claim
        return None

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
        validate_claim_request(worker_id, now, lease_duration, max_attempts)
        with self._lock:
            item = self._items.get(incident_id)
            if item is None:
                return None
            return self._claim_available(
                incident_id,
                item,
                worker_id=worker_id,
                now=now,
                lease_duration=lease_duration,
                max_attempts=max_attempts,
            )

    def _claim_available(
        self,
        incident_id: str,
        item: Dict[str, Any],
        *,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
        max_attempts: int,
    ) -> Optional[IncidentAnalysisWorkClaim]:
        ready = item["state"] == "READY" and item["available_at"] <= now
        expired = (
            item["state"] == "RUNNING"
            and item["lease_expires_at"] <= now
            and item["attempt_count"] < max_attempts
        )
        if not ready and not expired:
            return None
        incident = self._incidents.get(incident_id)
        if incident["status"] != "ANALYZING":
            return None
        token = f"claim-{uuid.uuid4().hex}"
        item.update(
            {
                "state": "RUNNING",
                "claim_token": token,
                "worker_id": worker_id,
                "lease_expires_at": now + lease_duration,
                "attempt_count": item["attempt_count"] + 1,
            }
        )
        return IncidentAnalysisWorkClaim(
            incident_id=incident_id,
            context_id=item["context_id"],
            claim_token=token,
            worker_id=worker_id,
            lease_expires_at=item["lease_expires_at"],
            attempt_count=item["attempt_count"],
            incident=copy.deepcopy(incident),
        )

    def renew(
        self,
        claim: IncidentAnalysisWorkClaim,
        *,
        now: datetime,
        lease_duration: timedelta,
    ) -> IncidentAnalysisWorkClaim:
        validate_claim_request(claim.worker_id, now, lease_duration, 1)
        with self._lock:
            item = self._current_item(claim)
            item["lease_expires_at"] = now + lease_duration
            return IncidentAnalysisWorkClaim(
                incident_id=claim.incident_id,
                context_id=claim.context_id,
                claim_token=claim.claim_token,
                worker_id=claim.worker_id,
                lease_expires_at=item["lease_expires_at"],
                attempt_count=item["attempt_count"],
                incident=self._incidents.get(claim.incident_id),
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
        with self._lock:
            item = self._current_item(claim)
            if self._incidents.get(claim.incident_id)["status"] != "REPORTED":
                raise InvalidTransition(
                    "analysis work can complete only after Agent reporting"
                )
            item["state"] = "SUCCEEDED"
            item["lease_expires_at"] = None

    def fail(
        self,
        claim: IncidentAnalysisWorkClaim,
        *,
        now: datetime,
        error_code: str,
    ) -> None:
        if now.tzinfo is None or not error_code.strip():
            raise ValueError("failure metadata is invalid")
        with self._lock:
            item = self._current_item(claim)
            incident = self._incidents.get(claim.incident_id)
            if incident["status"] == "ANALYZING":
                self._incidents.transition(
                    claim.incident_id,
                    expected_status="ANALYZING",
                    next_status="FAILED",
                    occurred_at=now,
                )
            elif incident["status"] != "FAILED":
                raise InvalidTransition("work failure requires an ANALYZING Incident")
            item["state"] = "FAILED"
            item["lease_expires_at"] = None
            item["error_code"] = error_code

    def reap_exhausted(self, *, now: datetime, max_attempts: int) -> int:
        if now.tzinfo is None or not 1 <= max_attempts <= 10:
            raise ValueError("reaper metadata is invalid")
        reaped = 0
        with self._lock:
            for incident_id, item in self._items.items():
                if not (
                    item["state"] == "RUNNING"
                    and item["lease_expires_at"] <= now
                ):
                    continue
                incident = self._incidents.get(incident_id)
                if incident["status"] in {"REPORTED", "PARTIAL"}:
                    item["state"] = "SUCCEEDED"
                    item["error_code"] = None
                elif incident["status"] == "FAILED":
                    item["state"] = "FAILED"
                    item["error_code"] = "INCIDENT_ALREADY_FAILED"
                elif (
                    incident["status"] == "ANALYZING"
                    and item["attempt_count"] >= max_attempts
                ):
                    self._incidents.transition(
                        incident_id,
                        expected_status="ANALYZING",
                        next_status="FAILED",
                        occurred_at=now,
                    )
                    item["state"] = "FAILED"
                    item["error_code"] = "LEASE_ATTEMPTS_EXHAUSTED"
                else:
                    continue
                item["lease_expires_at"] = None
                reaped += 1
        return reaped

    def _current_item(
        self, claim: IncidentAnalysisWorkClaim
    ) -> Dict[str, Any]:
        item = self._items.get(claim.incident_id)
        if (
            item is None
            or item["context_id"] != claim.context_id
            or item["state"] != "RUNNING"
            or item["claim_token"] != claim.claim_token
            or item["worker_id"] != claim.worker_id
        ):
            raise InvalidTransition("Incident analysis work claim is stale")
        return item
