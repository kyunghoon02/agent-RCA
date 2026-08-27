#!/usr/bin/env python3
"""Claim frozen ANALYZING Incidents and run bounded read-only Agent RCA."""

from __future__ import annotations

import json
import os
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from dotenv import load_dotenv

from incident_platform.agent_rca import (
    AgentRCAPolicy,
    AgentRCAService,
    AgentRCAServiceRun,
    OpenAIAgentsSDKRunner,
)
from incident_platform.incident_work import (
    IncidentAnalysisWorkClaim,
    IncidentAnalysisWorkRepository,
    IncidentWorkQueueSnapshot,
    IncidentWorkQueueTelemetryRepository,
    validate_analysis_eligibility,
    validate_claim_request,
    validate_incident_id,
)
from incident_platform.knowledge import (
    BoundedKnowledgeRetriever,
    GitReferenceDocumentRepository,
)
from incident_platform.postgresql import (
    PostgreSQLIncidentAnalysisWorkRepository,
    PostgreSQLIncidentRepository,
    PostgreSQLIncidentWorkQueueTelemetryRepository,
    apply_migrations,
)


UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _required_secret_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _boolean_environment(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{name} must be true or false")


def _optional_datetime_environment(name: str) -> datetime | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an RFC3339 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class AgentWorkerRuntimeConfig:
    worker_id: str
    run_once: bool
    target_incident_id: str | None
    poll_interval_seconds: float
    lease_seconds: int
    max_attempts: int
    eligibility_label: str
    activated_at: datetime | None
    min_claim_interval_seconds: float
    circuit_failure_threshold: int
    circuit_cooldown_seconds: int
    metrics_host: str
    metrics_port: int
    model_name: str
    max_turns: int
    max_llm_calls: int
    max_tool_calls: int
    max_evidence_candidates: int
    max_output_tokens: int
    max_wall_time_ms: int
    knowledge_root: str
    knowledge_index_path: str
    postgres_host: str
    postgres_port: int
    postgres_database: str
    postgres_username: str
    postgres_password: str

    def __post_init__(self) -> None:
        validate_claim_request(
            self.worker_id,
            datetime(2000, 1, 1, tzinfo=UTC),
            timedelta(seconds=self.lease_seconds),
            self.max_attempts,
        )
        if not 0.5 <= self.poll_interval_seconds <= 60:
            raise ValueError("Agent worker poll interval must be between 0.5 and 60")
        if self.run_once != (self.target_incident_id is not None):
            raise ValueError(
                "AGENT_WORKER_RUN_ONCE and AGENT_WORKER_TARGET_INCIDENT_ID "
                "must be configured together"
            )
        if self.target_incident_id is not None:
            validate_incident_id(self.target_incident_id)
        if not self.run_once and self.activated_at is None:
            raise ValueError(
                "AGENT_WORKER_ACTIVATED_AT is required for continuous operation"
            )
        if not self.eligibility_label.strip():
            raise ValueError("Agent eligibility label is required")
        if self.activated_at is not None:
            validate_analysis_eligibility(
                self.eligibility_label,
                self.activated_at,
            )
        if not 1 <= self.min_claim_interval_seconds <= 3600:
            raise ValueError("Agent minimum claim interval must be between 1 and 3600")
        if not 1 <= self.circuit_failure_threshold <= 20:
            raise ValueError("Agent circuit failure threshold must be between 1 and 20")
        if not 10 <= self.circuit_cooldown_seconds <= 3600:
            raise ValueError("Agent circuit cooldown must be between 10 and 3600")
        if not self.metrics_host.strip() or not 1 <= self.metrics_port <= 65535:
            raise ValueError("Agent metrics endpoint is invalid")
        if not self.model_name.strip():
            raise ValueError("Agent model name is required")
        if not 1 <= self.postgres_port <= 65535:
            raise ValueError("PostgreSQL port is invalid")
        policy = self.policy()
        lease_budget_ms = max(0, (self.lease_seconds - 30) * 1000)
        if policy.max_wall_time_ms > lease_budget_ms:
            raise ValueError(
                "Agent wall-time budget must leave at least 30 seconds in the lease"
            )
        root = Path(self.knowledge_root)
        index = Path(self.knowledge_index_path)
        try:
            index.relative_to(root)
        except ValueError as error:
            raise ValueError("Knowledge index must stay inside the corpus root") from error

    def policy(self) -> AgentRCAPolicy:
        return AgentRCAPolicy(
            max_turns=self.max_turns,
            max_llm_calls=self.max_llm_calls,
            max_tool_calls=self.max_tool_calls,
            max_evidence_candidates=self.max_evidence_candidates,
            max_output_tokens=self.max_output_tokens,
            max_wall_time_ms=self.max_wall_time_ms,
        )

    @classmethod
    def from_environment(cls) -> "AgentWorkerRuntimeConfig":
        knowledge_root = os.environ.get("AGENT_RCA_KNOWLEDGE_ROOT", "/app/knowledge")
        return cls(
            worker_id=os.environ.get(
                "AGENT_WORKER_ID", os.environ.get("HOSTNAME", "")
            ),
            run_once=_boolean_environment("AGENT_WORKER_RUN_ONCE"),
            target_incident_id=(
                os.environ.get("AGENT_WORKER_TARGET_INCIDENT_ID", "").strip() or None
            ),
            poll_interval_seconds=float(
                os.environ.get("AGENT_WORKER_POLL_INTERVAL_SECONDS", "2")
            ),
            lease_seconds=int(os.environ.get("AGENT_WORKER_LEASE_SECONDS", "180")),
            max_attempts=int(os.environ.get("AGENT_WORKER_MAX_ATTEMPTS", "3")),
            eligibility_label=os.environ.get(
                "AGENT_WORKER_ELIGIBILITY_LABEL", "agent_rca_enabled"
            ),
            activated_at=_optional_datetime_environment("AGENT_WORKER_ACTIVATED_AT"),
            min_claim_interval_seconds=float(
                os.environ.get("AGENT_WORKER_MIN_CLAIM_INTERVAL_SECONDS", "60")
            ),
            circuit_failure_threshold=int(
                os.environ.get("AGENT_WORKER_CIRCUIT_FAILURE_THRESHOLD", "3")
            ),
            circuit_cooldown_seconds=int(
                os.environ.get("AGENT_WORKER_CIRCUIT_COOLDOWN_SECONDS", "300")
            ),
            metrics_host=os.environ.get("AGENT_WORKER_METRICS_HOST", "0.0.0.0"),
            metrics_port=int(os.environ.get("AGENT_WORKER_METRICS_PORT", "9090")),
            model_name=os.environ.get("AGENT_RCA_MODEL", "gpt-5.6-luna"),
            max_turns=int(os.environ.get("AGENT_RCA_MAX_TURNS", "6")),
            max_llm_calls=int(os.environ.get("AGENT_RCA_MAX_LLM_CALLS", "6")),
            max_tool_calls=int(os.environ.get("AGENT_RCA_MAX_TOOL_CALLS", "12")),
            max_evidence_candidates=int(
                os.environ.get("AGENT_RCA_MAX_EVIDENCE_CANDIDATES", "8")
            ),
            max_output_tokens=int(
                os.environ.get("AGENT_RCA_MAX_OUTPUT_TOKENS", "2000")
            ),
            max_wall_time_ms=int(
                os.environ.get("AGENT_RCA_MAX_WALL_TIME_MS", "60000")
            ),
            knowledge_root=knowledge_root,
            knowledge_index_path=os.environ.get(
                "AGENT_RCA_KNOWLEDGE_INDEX",
                str(Path(knowledge_root) / "index.yaml"),
            ),
            postgres_host=_required_environment("POSTGRES_HOST"),
            postgres_port=int(os.environ.get("POSTGRES_PORT", "5432")),
            postgres_database=_required_environment("POSTGRES_DATABASE"),
            postgres_username=_required_environment("POSTGRES_USERNAME"),
            postgres_password=_required_secret_environment("POSTGRES_PASSWORD"),
        )


def _postgres_connection_factory(
    config: AgentWorkerRuntimeConfig,
) -> Callable[[], object]:
    def connect() -> object:
        import psycopg

        return psycopg.connect(
            host=config.postgres_host,
            port=config.postgres_port,
            dbname=config.postgres_database,
            user=config.postgres_username,
            password=config.postgres_password,
            connect_timeout=5,
            application_name="agent-rca-worker",
        )

    return connect


class ClaimedAgentRCAService(Protocol):
    def run(
        self,
        incident_id: str,
        *,
        context_id: str,
        generated_at: datetime,
    ) -> AgentRCAServiceRun:
        ...


class AgentWorker:
    """Run one Context-pinned Agent claim without exposing mutating tools."""

    def __init__(
        self,
        config: AgentWorkerRuntimeConfig,
        work_repository: IncidentAnalysisWorkRepository,
        agent_service: ClaimedAgentRCAService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._work = work_repository
        self._agent = agent_service
        self._clock = clock
        self._next_claim_at: datetime | None = None
        self._consecutive_failures = 0
        self._circuit_open_until: datetime | None = None

    def runtime_state(self) -> Mapping[str, object]:
        now = self._clock()
        circuit_open = (
            self._circuit_open_until is not None
            and now < self._circuit_open_until
        )
        return {
            "circuit_open": circuit_open,
            "consecutive_failures": self._consecutive_failures,
        }

    def process_one(self) -> Mapping[str, object]:
        now = self._clock()
        if self._config.target_incident_id is not None:
            reaped = 0
            claim = self._work.claim_incident(
                self._config.target_incident_id,
                worker_id=self._config.worker_id,
                now=now,
                lease_duration=timedelta(seconds=self._config.lease_seconds),
                max_attempts=self._config.max_attempts,
            )
        else:
            if (
                self._circuit_open_until is not None
                and now < self._circuit_open_until
            ):
                return {
                    "status": "CIRCUIT_OPEN",
                    "retry_after_seconds": max(
                        1, int((self._circuit_open_until - now).total_seconds())
                    ),
                    "reaped": 0,
                }
            if self._circuit_open_until is not None:
                self._circuit_open_until = None
                self._consecutive_failures = 0
            if self._next_claim_at is not None and now < self._next_claim_at:
                return {
                    "status": "RATE_LIMITED",
                    "retry_after_seconds": max(
                        1, int((self._next_claim_at - now).total_seconds())
                    ),
                    "reaped": 0,
                }
            activated_at = self._config.activated_at
            if activated_at is None:  # Protected by runtime configuration validation.
                raise RuntimeError("continuous Agent activation time is missing")
            reaped = self._work.reap_exhausted_eligible(
                now=now,
                max_attempts=self._config.max_attempts,
                eligibility_label=self._config.eligibility_label,
                activated_at=activated_at,
            )
            claim = self._work.claim_next_eligible(
                worker_id=self._config.worker_id,
                now=now,
                lease_duration=timedelta(seconds=self._config.lease_seconds),
                max_attempts=self._config.max_attempts,
                eligibility_label=self._config.eligibility_label,
                activated_at=activated_at,
            )
        if claim is None:
            if self._config.target_incident_id is not None:
                return {
                    "status": "TARGET_NOT_CLAIMABLE",
                    "incident_id": self._config.target_incident_id,
                    "reaped": 0,
                }
            return {"status": "IDLE", "reaped": reaped}
        if self._config.target_incident_id is None:
            self._next_claim_at = now + timedelta(
                seconds=self._config.min_claim_interval_seconds
            )
        result = self._process_claim(claim, reaped=reaped)
        if self._config.target_incident_id is None:
            self._update_circuit(result)
        return result

    def _update_circuit(self, result: Mapping[str, object]) -> None:
        if result["status"] == "PROCESSED":
            self._consecutive_failures = 0
            self._circuit_open_until = None
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._config.circuit_failure_threshold:
            self._circuit_open_until = self._clock() + timedelta(
                seconds=self._config.circuit_cooldown_seconds
            )

    def _process_claim(
        self,
        claim: IncidentAnalysisWorkClaim,
        *,
        reaped: int,
    ) -> Mapping[str, object]:
        try:
            run = self._agent.run(
                claim.incident_id,
                context_id=claim.context_id,
                generated_at=self._clock(),
            )
        except Exception as error:
            error_code = type(error).__name__.upper()
            try:
                self._work.fail(
                    claim,
                    now=self._clock(),
                    error_code=error_code,
                )
            except Exception:
                return {
                    "status": "FAILURE_PERSISTENCE_FAILED",
                    "stage": "ANALYSIS",
                    "incident_id": claim.incident_id,
                    "context_id": claim.context_id,
                    "attempt": claim.attempt_count,
                    "error_code": error_code,
                    "reaped": reaped,
                }
            return {
                "status": "FAILED",
                "stage": "ANALYSIS",
                "incident_id": claim.incident_id,
                "context_id": claim.context_id,
                "attempt": claim.attempt_count,
                "error_code": error_code,
                "reaped": reaped,
            }

        try:
            self._work.complete(
                claim,
                now=self._clock(),
                outcome="SUCCEEDED",
            )
        except Exception as error:
            # AgentRCAService has already committed REPORTED. Do not attempt to
            # move the Incident backwards; the reaper will recover this work row.
            return {
                "status": "WORK_COMPLETION_FAILED",
                "stage": "ANALYSIS",
                "incident_id": claim.incident_id,
                "context_id": claim.context_id,
                "attempt": claim.attempt_count,
                "error_code": type(error).__name__.upper(),
                "reaped": reaped,
            }
        return {
            "status": "PROCESSED",
            "stage": "ANALYSIS",
            "incident_id": claim.incident_id,
            "context_id": claim.context_id,
            "attempt": claim.attempt_count,
            "report_id": run.report["report_id"],
            "report_status": run.report["status"],
            "agent_run_id": run.audit["agent_run_id"],
            "model": run.audit["model"],
            "llm_calls": run.audit["usage"]["llm_calls"],
            "tool_calls": run.audit["usage"]["tool_calls"],
            "input_tokens": run.audit["usage"]["input_tokens"],
            "output_tokens": run.audit["usage"]["output_tokens"],
            "total_tokens": run.audit["usage"]["total_tokens"],
            "wall_time_ms": run.audit["usage"]["wall_time_ms"],
            "reaped": reaped,
        }


class AgentWorkerMetrics:
    """Small dependency-free Prometheus registry for the continuous worker."""

    _OUTCOMES: Sequence[str] = (
        "processed",
        "failed",
        "failure_persistence_failed",
        "work_completion_failed",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs = {outcome: 0 for outcome in self._OUTCOMES}
        self._llm_calls = 0
        self._tool_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._duration_seconds_sum = 0.0
        self._duration_seconds_count = 0
        self._last_success_timestamp = 0.0

    def observe(self, result: Mapping[str, object], *, observed_at: datetime) -> None:
        outcome = str(result.get("status", "")).lower()
        if outcome not in self._runs:
            return
        with self._lock:
            self._runs[outcome] += 1
            if outcome != "processed":
                return
            self._llm_calls += int(result.get("llm_calls", 0))
            self._tool_calls += int(result.get("tool_calls", 0))
            self._input_tokens += int(result.get("input_tokens", 0))
            self._output_tokens += int(result.get("output_tokens", 0))
            self._total_tokens += int(result.get("total_tokens", 0))
            self._duration_seconds_sum += float(result.get("wall_time_ms", 0)) / 1000
            self._duration_seconds_count += 1
            self._last_success_timestamp = observed_at.timestamp()

    def render(
        self,
        runtime_state: Mapping[str, object],
        *,
        queue_snapshot: IncidentWorkQueueSnapshot | None = None,
        queue_observation_success: bool = False,
    ) -> bytes:
        with self._lock:
            lines = [
                "# HELP agent_rca_worker_up Whether the worker metrics endpoint is serving.",
                "# TYPE agent_rca_worker_up gauge",
                "agent_rca_worker_up 1",
                "# HELP agent_rca_worker_runs_total Agent runs by terminal outcome.",
                "# TYPE agent_rca_worker_runs_total counter",
            ]
            lines.extend(
                f'agent_rca_worker_runs_total{{outcome="{outcome}"}} {count}'
                for outcome, count in self._runs.items()
            )
            lines.extend(
                [
                    "# TYPE agent_rca_worker_llm_calls_total counter",
                    f"agent_rca_worker_llm_calls_total {self._llm_calls}",
                    "# TYPE agent_rca_worker_tool_calls_total counter",
                    f"agent_rca_worker_tool_calls_total {self._tool_calls}",
                    "# TYPE agent_rca_worker_tokens_total counter",
                    f'agent_rca_worker_tokens_total{{type="input"}} {self._input_tokens}',
                    f'agent_rca_worker_tokens_total{{type="output"}} {self._output_tokens}',
                    f'agent_rca_worker_tokens_total{{type="total"}} {self._total_tokens}',
                    "# TYPE agent_rca_worker_run_duration_seconds summary",
                    "agent_rca_worker_run_duration_seconds_sum "
                    f"{self._duration_seconds_sum:.6f}",
                    "agent_rca_worker_run_duration_seconds_count "
                    f"{self._duration_seconds_count}",
                    "# TYPE agent_rca_worker_circuit_open gauge",
                    "agent_rca_worker_circuit_open "
                    f"{1 if runtime_state.get('circuit_open') else 0}",
                    "# TYPE agent_rca_worker_consecutive_failures gauge",
                    "agent_rca_worker_consecutive_failures "
                    f"{int(runtime_state.get('consecutive_failures', 0))}",
                    "# TYPE agent_rca_worker_last_success_timestamp_seconds gauge",
                    "agent_rca_worker_last_success_timestamp_seconds "
                    f"{self._last_success_timestamp:.3f}",
                    "# HELP agent_rca_work_queue_observation_success Whether the latest PostgreSQL queue observation succeeded.",
                    "# TYPE agent_rca_work_queue_observation_success gauge",
                    "agent_rca_work_queue_observation_success "
                    f"{1 if queue_observation_success else 0}",
                ]
            )
            if queue_snapshot is not None:
                lines.extend(
                    [
                        "# HELP agent_rca_work_queue_last_observed_timestamp_seconds Unix timestamp of the latest successful queue observation.",
                        "# TYPE agent_rca_work_queue_last_observed_timestamp_seconds gauge",
                        "agent_rca_work_queue_last_observed_timestamp_seconds "
                        f"{queue_snapshot.observed_at.timestamp():.3f}",
                        "# HELP agent_rca_work_items Current durable work items by pipeline stage and state. Analysis includes only continuous-Agent-eligible Incidents.",
                        "# TYPE agent_rca_work_items gauge",
                    ]
                )
                for stage in queue_snapshot.stages:
                    for state, count in (
                        ("ready", stage.ready),
                        ("running", stage.running),
                        ("succeeded", stage.succeeded),
                        ("failed", stage.failed),
                    ):
                        lines.append(
                            "agent_rca_work_items"
                            f'{{stage="{stage.stage}",state="{state}"}} {count}'
                        )
                lines.extend(
                    [
                        "# HELP agent_rca_work_oldest_ready_age_seconds Age of the oldest available READY item by stage.",
                        "# TYPE agent_rca_work_oldest_ready_age_seconds gauge",
                    ]
                )
                lines.extend(
                    "agent_rca_work_oldest_ready_age_seconds"
                    f'{{stage="{stage.stage}"}} '
                    f"{stage.oldest_ready_age_seconds:.3f}"
                    for stage in queue_snapshot.stages
                )
                lines.extend(
                    [
                        "# HELP agent_rca_work_oldest_running_age_seconds Age of the oldest RUNNING item by stage.",
                        "# TYPE agent_rca_work_oldest_running_age_seconds gauge",
                    ]
                )
                lines.extend(
                    "agent_rca_work_oldest_running_age_seconds"
                    f'{{stage="{stage.stage}"}} '
                    f"{stage.oldest_running_age_seconds:.3f}"
                    for stage in queue_snapshot.stages
                )
        return ("\n".join(lines) + "\n").encode("utf-8")


def start_metrics_server(
    config: AgentWorkerRuntimeConfig,
    metrics: AgentWorkerMetrics,
    worker: AgentWorker,
    queue_telemetry: IncidentWorkQueueTelemetryRepository,
) -> ThreadingHTTPServer:
    class MetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            if self.path == "/healthz":
                body = b"ok\n"
                content_type = "text/plain; charset=utf-8"
            elif self.path == "/metrics":
                queue_snapshot = None
                queue_observation_success = False
                if config.activated_at is not None:
                    try:
                        queue_snapshot = queue_telemetry.snapshot(
                            now=datetime.now(UTC),
                            analysis_eligibility_label=config.eligibility_label,
                            analysis_activated_at=config.activated_at,
                        )
                        queue_observation_success = True
                    except Exception:
                        # Preserve worker-local metrics while exposing the failed
                        # PostgreSQL observation as a dedicated fail-closed gauge.
                        pass
                body = metrics.render(
                    worker.runtime_state(),
                    queue_snapshot=queue_snapshot,
                    queue_observation_success=queue_observation_success,
                )
                content_type = "text/plain; version=0.0.4; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(
        (config.metrics_host, config.metrics_port), MetricsHandler
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def build_worker(
    config: AgentWorkerRuntimeConfig,
) -> tuple[AgentWorker, IncidentWorkQueueTelemetryRepository]:
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise ValueError("OPENAI_API_KEY is required")
    connection_factory = _postgres_connection_factory(config)
    apply_migrations(connection_factory)
    incident_repository = PostgreSQLIncidentRepository(connection_factory)
    work_repository = PostgreSQLIncidentAnalysisWorkRepository(connection_factory)
    knowledge_repository = GitReferenceDocumentRepository(
        Path(config.knowledge_root),
        Path(config.knowledge_index_path),
    )
    service = AgentRCAService(
        incident_repository,
        BoundedKnowledgeRetriever(knowledge_repository),
        OpenAIAgentsSDKRunner(config.model_name),
        policy=config.policy(),
    )
    queue_telemetry = PostgreSQLIncidentWorkQueueTelemetryRepository(
        connection_factory
    )
    return AgentWorker(config, work_repository, service), queue_telemetry


def main() -> int:
    load_dotenv(ROOT / ".env")
    stop = threading.Event()

    def request_stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        config = AgentWorkerRuntimeConfig.from_environment()
        worker, queue_telemetry = build_worker(config)
        metrics = AgentWorkerMetrics()
        metrics_server = start_metrics_server(
            config,
            metrics,
            worker,
            queue_telemetry,
        )
    except Exception as error:
        print(
            json.dumps(
                {"status": "STARTUP_FAILED", "error_code": type(error).__name__.upper()},
                sort_keys=True,
            ),
            flush=True,
        )
        return 1

    if config.run_once:
        result = worker.process_one()
        metrics.observe(result, observed_at=datetime.now(UTC))
        print(json.dumps(result, sort_keys=True), flush=True)
        metrics_server.shutdown()
        metrics_server.server_close()
        return 0 if result["status"] == "PROCESSED" else 1

    try:
        while not stop.is_set():
            result = worker.process_one()
            metrics.observe(result, observed_at=datetime.now(UTC))
            if result["status"] not in {"IDLE", "RATE_LIMITED", "CIRCUIT_OPEN"} or result.get("reaped"):
                print(json.dumps(result, sort_keys=True), flush=True)
            stop.wait(config.poll_interval_seconds)
    finally:
        metrics_server.shutdown()
        metrics_server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
