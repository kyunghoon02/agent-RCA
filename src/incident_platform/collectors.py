"""Bounded parallel collector orchestration with failure isolation."""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from .errors import PermanentProviderError, RetryableProviderError
from .evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceWindow,
    ProviderBatch,
    ResourceScope,
    parse_time,
    validate_provider_batch,
)
from .repository import IncidentRepository


COLLECTOR_NAMES = frozenset(
    {
        "prometheus",
        "prometheus-api",
        "prometheus-workload",
        "loki-kernel-oom",
        "logs",
        "kubernetes",
        "deployment",
        "trace",
        "hubble",
    }
)


class EvidenceProvider(Protocol):
    """Cloud-specific adapter boundary used by one collector."""

    def collect(self, request: CollectionRequest) -> ProviderBatch:
        ...


@dataclass(frozen=True)
class CollectorSpec:
    name: str
    provider: EvidenceProvider
    timeout_seconds: float = 5.0
    max_attempts: int = 2
    request_scope: Optional[ResourceScope] = None
    lookback_seconds: Optional[int] = None

    def __post_init__(self) -> None:
        if self.name not in COLLECTOR_NAMES:
            raise ValueError(f"unsupported collector name: {self.name}")
        if self.timeout_seconds <= 0:
            raise ValueError("collector timeout must be positive")
        if self.max_attempts <= 0:
            raise ValueError("collector max_attempts must be positive")
        if self.lookback_seconds is not None and self.lookback_seconds <= 0:
            raise ValueError("collector lookback_seconds must be positive")


@dataclass(frozen=True)
class CollectorExecution:
    name: str
    status: str
    attempts: int
    started_at: datetime
    ended_at: datetime
    evidence: Tuple[Dict[str, Any], ...]
    error: Optional[str] = None

    def incident_status(self) -> Dict[str, Any]:
        return {
            "collector": self.name,
            "status": self.status,
            "attempts": self.attempts,
            "started_at": _format_time(self.started_at),
            "ended_at": _format_time(self.ended_at),
            "error": self.error,
        }


@dataclass(frozen=True)
class CollectionRun:
    status: str
    executions: Tuple[CollectorExecution, ...]

    @property
    def evidence(self) -> Tuple[Dict[str, Any], ...]:
        return tuple(
            evidence
            for execution in self.executions
            for evidence in execution.evidence
        )

    @property
    def collector_statuses(self) -> List[Dict[str, Any]]:
        return [execution.incident_status() for execution in self.executions]


@dataclass(frozen=True)
class _WorkerResult:
    batch: ProviderBatch
    attempts: int
    completed_monotonic: float


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _request_id(incident_id: str, collector_name: str, observed_at: datetime) -> str:
    raw = f"{incident_id}:{collector_name}:{_format_time(observed_at)}"
    return f"req-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def _bounded_window(
    window: EvidenceWindow,
    lookback_seconds: Optional[int],
) -> EvidenceWindow:
    if lookback_seconds is None:
        return window
    start = parse_time(window.start, "EvidenceWindow.start")
    end = parse_time(window.end, "EvidenceWindow.end")
    bounded_start = max(start, end - timedelta(seconds=lookback_seconds))
    return EvidenceWindow(start=_format_time(bounded_start), end=window.end)


class CollectorOrchestrator:
    """Run independent collectors concurrently under per-collector budgets."""

    def __init__(
        self,
        specs: Sequence[CollectorSpec],
        evidence_builder: Optional[EvidenceBuilder] = None,
    ) -> None:
        if not specs:
            raise ValueError("at least one CollectorSpec is required")
        names = [spec.name for spec in specs]
        if len(names) != len(set(names)):
            raise ValueError("collector names must be unique")
        self._specs = tuple(specs)
        self._evidence_builder = evidence_builder or EvidenceBuilder()

    def collect(
        self,
        *,
        incident_id: str,
        window: EvidenceWindow,
        scope: ResourceScope,
        observed_at: Optional[datetime] = None,
    ) -> CollectionRun:
        started = observed_at or datetime.now(timezone.utc)
        if started.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")

        for spec in self._specs:
            if (
                spec.request_scope is not None
                and spec.request_scope.namespace != scope.namespace
            ):
                raise ValueError(
                    "collector request scope must stay in the Incident namespace"
                )

        executor = ThreadPoolExecutor(
            max_workers=len(self._specs),
            thread_name_prefix="evidence-collector",
        )
        futures: Dict[str, Future[_WorkerResult]] = {}
        deadlines: Dict[str, float] = {}
        starts: Dict[str, datetime] = {}
        requests: Dict[str, CollectionRequest] = {}
        for spec in self._specs:
            request_scope = spec.request_scope or scope
            request = CollectionRequest(
                request_id=_request_id(incident_id, spec.name, started),
                incident_id=incident_id,
                window=_bounded_window(window, spec.lookback_seconds),
                scope=request_scope,
                timeout_seconds=spec.timeout_seconds,
            )
            requests[spec.name] = request
            starts[spec.name] = datetime.now(timezone.utc)
            deadlines[spec.name] = time.monotonic() + spec.timeout_seconds
            futures[spec.name] = executor.submit(self._collect_with_retries, spec, request)

        executions: List[CollectorExecution] = []
        try:
            for spec in self._specs:
                future = futures[spec.name]
                remaining = max(0.0, deadlines[spec.name] - time.monotonic())
                try:
                    worker_result = future.result(timeout=remaining)
                    if worker_result.completed_monotonic > deadlines[spec.name]:
                        raise TimeoutError
                    ended = datetime.now(timezone.utc)
                    initial_request = requests[spec.name]
                    request = CollectionRequest(
                        request_id=initial_request.request_id,
                        incident_id=incident_id,
                        window=initial_request.window,
                        scope=initial_request.scope,
                        timeout_seconds=spec.timeout_seconds,
                        attempt=worker_result.attempts,
                    )
                    validate_provider_batch(worker_result.batch, request)
                    evidence = tuple(
                        self._evidence_builder.build(
                            draft,
                            request,
                            collected_at=ended,
                        )
                        for draft in worker_result.batch.items
                    )
                    executions.append(
                        CollectorExecution(
                            name=spec.name,
                            status=worker_result.batch.status,
                            attempts=worker_result.attempts,
                            started_at=starts[spec.name],
                            ended_at=ended,
                            evidence=evidence,
                            error=worker_result.batch.error,
                        )
                    )
                except TimeoutError:
                    future.cancel()
                    executions.append(
                        CollectorExecution(
                            name=spec.name,
                            status="TIMED_OUT",
                            attempts=1,
                            started_at=starts[spec.name],
                            ended_at=datetime.now(timezone.utc),
                            evidence=tuple(),
                            error=f"collector exceeded {spec.timeout_seconds:.3f}s budget",
                        )
                    )
                except (PermanentProviderError, RetryableProviderError) as error:
                    attempts = getattr(error, "attempts", spec.max_attempts)
                    executions.append(
                        CollectorExecution(
                            name=spec.name,
                            status="FAILED",
                            attempts=attempts,
                            started_at=starts[spec.name],
                            ended_at=datetime.now(timezone.utc),
                            evidence=tuple(),
                            error=str(error),
                        )
                    )
                except Exception as error:  # defensive adapter boundary
                    executions.append(
                        CollectorExecution(
                            name=spec.name,
                            status="FAILED",
                            attempts=1,
                            started_at=starts[spec.name],
                            ended_at=datetime.now(timezone.utc),
                            evidence=tuple(),
                            error=f"{type(error).__name__}: {error}",
                        )
                    )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        statuses = {execution.status for execution in executions}
        successful = statuses & {"SUCCEEDED", "PARTIAL"}
        failed = statuses & {"FAILED", "TIMED_OUT"}
        if not successful:
            run_status = "FAILED"
        elif failed or "PARTIAL" in statuses:
            run_status = "PARTIAL"
        else:
            run_status = "SUCCEEDED"
        return CollectionRun(status=run_status, executions=tuple(executions))

    @staticmethod
    def _collect_with_retries(
        spec: CollectorSpec,
        request: CollectionRequest,
    ) -> _WorkerResult:
        last_error: Optional[RetryableProviderError] = None
        for attempt in range(1, spec.max_attempts + 1):
            attempted_request = CollectionRequest(
                request_id=request.request_id,
                incident_id=request.incident_id,
                window=request.window,
                scope=request.scope,
                timeout_seconds=request.timeout_seconds,
                attempt=attempt,
            )
            try:
                return _WorkerResult(
                    spec.provider.collect(attempted_request),
                    attempt,
                    time.monotonic(),
                )
            except PermanentProviderError as error:
                setattr(error, "attempts", attempt)
                raise
            except RetryableProviderError as error:
                last_error = error
        assert last_error is not None
        setattr(last_error, "attempts", spec.max_attempts)
        raise last_error


class IncidentCollectionService:
    """Connect Incident lifecycle, collection, and repository persistence."""

    def __init__(
        self,
        repository: IncidentRepository,
        orchestrator: CollectorOrchestrator,
    ) -> None:
        self._repository = repository
        self._orchestrator = orchestrator

    def collect_incident(
        self,
        incident_id: str,
        *,
        scope: ResourceScope,
        observed_at: Optional[datetime] = None,
    ) -> CollectionRun:
        now = observed_at or datetime.now(timezone.utc)
        incident = self._validated_incident(incident_id, scope)
        self._repository.transition(
            incident_id,
            expected_status="RECEIVED",
            next_status="COLLECTING",
            occurred_at=now,
        )
        return self._collect_started(
            incident_id,
            incident=incident,
            scope=scope,
            observed_at=now,
        )

    def collect_claimed_incident(
        self,
        incident_id: str,
        *,
        scope: ResourceScope,
        observed_at: Optional[datetime] = None,
    ) -> CollectionRun:
        """Collect an Incident already moved to COLLECTING by a fenced claim."""

        now = observed_at or datetime.now(timezone.utc)
        incident = self._validated_incident(incident_id, scope)
        if incident["status"] != "COLLECTING":
            raise ValueError("claimed Incident must be COLLECTING")
        return self._collect_started(
            incident_id,
            incident=incident,
            scope=scope,
            observed_at=now,
        )

    def _validated_incident(
        self,
        incident_id: str,
        scope: ResourceScope,
    ) -> Dict[str, Any]:
        incident = self._repository.get(incident_id)
        expected_namespace = incident["source_entity"]["namespace"]
        if scope.namespace != expected_namespace:
            raise ValueError("collection scope does not match Incident namespace")
        if incident["source_entity"]["name"] not in scope.resource_names:
            raise ValueError("collection scope does not include the Incident source entity")
        return incident

    def _collect_started(
        self,
        incident_id: str,
        *,
        incident: Dict[str, Any],
        scope: ResourceScope,
        observed_at: datetime,
    ) -> CollectionRun:
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        end = incident["window"]["incident_end"] or _format_time(observed_at)
        run = self._orchestrator.collect(
            incident_id=incident_id,
            window=EvidenceWindow(
                start=incident["window"]["baseline_start"],
                end=end,
            ),
            scope=scope,
            observed_at=observed_at,
        )
        self._repository.replace_collector_statuses(
            incident_id,
            run.collector_statuses,
            occurred_at=observed_at,
        )
        self._repository.store_evidence(incident_id, run.evidence)
        self._repository.transition(
            incident_id,
            expected_status="COLLECTING",
            next_status="FAILED" if run.status == "FAILED" else "LOCALIZING",
            occurred_at=observed_at,
        )
        return run
