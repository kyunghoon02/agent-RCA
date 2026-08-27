#!/usr/bin/env python3
"""Claim frozen ANALYZING Incidents and run bounded read-only Agent RCA."""

from __future__ import annotations

import json
import os
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol

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


@dataclass(frozen=True)
class AgentWorkerRuntimeConfig:
    worker_id: str
    run_once: bool
    target_incident_id: str | None
    poll_interval_seconds: float
    lease_seconds: int
    max_attempts: int
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
            reaped = self._work.reap_exhausted(
                now=now,
                max_attempts=self._config.max_attempts,
            )
            claim = self._work.claim_next(
                worker_id=self._config.worker_id,
                now=now,
                lease_duration=timedelta(seconds=self._config.lease_seconds),
                max_attempts=self._config.max_attempts,
            )
        if claim is None:
            if self._config.target_incident_id is not None:
                return {
                    "status": "TARGET_NOT_CLAIMABLE",
                    "incident_id": self._config.target_incident_id,
                    "reaped": 0,
                }
            return {"status": "IDLE", "reaped": reaped}
        return self._process_claim(claim, reaped=reaped)

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
            "reaped": reaped,
        }


def build_worker(config: AgentWorkerRuntimeConfig) -> AgentWorker:
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
    return AgentWorker(config, work_repository, service)


def main() -> int:
    load_dotenv(ROOT / ".env")
    stop = threading.Event()

    def request_stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        config = AgentWorkerRuntimeConfig.from_environment()
        worker = build_worker(config)
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
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0 if result["status"] == "PROCESSED" else 1

    while not stop.is_set():
        result = worker.process_one()
        if result["status"] != "IDLE" or result.get("reaped"):
            print(json.dumps(result, sort_keys=True), flush=True)
        stop.wait(config.poll_interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
