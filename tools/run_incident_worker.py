#!/usr/bin/env python3
"""Claim durable Incidents and collect bounded in-cluster Evidence."""

from __future__ import annotations

import json
import os
import signal
import ssl
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from incident_platform.collectors import (
    CollectorOrchestrator,
    CollectorSpec,
    IncidentCollectionService,
)
from incident_platform.evidence import ResourceScope
from incident_platform.incident_work import (
    IncidentWorkRepository,
    validate_claim_request,
)
from incident_platform.postgresql import (
    PostgreSQLIncidentRepository,
    PostgreSQLIncidentWorkRepository,
    apply_migrations,
)
from incident_platform.providers.http import BoundedJSONTransport
from incident_platform.providers.kubernetes import (
    KubernetesHTTPAPI,
    KubernetesResourceSpec,
    KubernetesStateProvider,
)
from incident_platform.providers.prometheus import (
    PrometheusHTTPAPI,
    PrometheusMetricProvider,
    PrometheusQuerySpec,
)


UTC = timezone.utc


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


@dataclass(frozen=True)
class IncidentWorkerRuntimeConfig:
    worker_id: str
    cluster_id: str
    target_namespace: str
    poll_interval_seconds: float
    lease_seconds: int
    max_attempts: int
    provider_timeout_seconds: float
    max_evidence_items: int
    kubernetes_api_server: str
    kubernetes_token_file: str
    kubernetes_ca_file: str
    prometheus_base_url: str
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
            raise ValueError("worker poll interval must be between 0.5 and 60 seconds")
        if not 30 <= self.lease_seconds <= 1800:
            raise ValueError("worker lease must be between 30 and 1800 seconds")
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("worker max attempts must be between 1 and 10")
        if not 1 <= self.provider_timeout_seconds <= self.lease_seconds / 2:
            raise ValueError("provider timeout must fit safely inside the work lease")
        if not 8 <= self.max_evidence_items <= 100:
            raise ValueError("worker Evidence budget must be between 8 and 100")
        if not 1 <= self.postgres_port <= 65535:
            raise ValueError("PostgreSQL port is invalid")

    @classmethod
    def from_environment(cls) -> "IncidentWorkerRuntimeConfig":
        return cls(
            worker_id=os.environ.get("INCIDENT_WORKER_ID", os.environ.get("HOSTNAME", "")),
            cluster_id=_required_environment("INCIDENT_WORKER_CLUSTER_ID"),
            target_namespace=_required_environment("INCIDENT_WORKER_TARGET_NAMESPACE"),
            poll_interval_seconds=float(
                os.environ.get("INCIDENT_WORKER_POLL_INTERVAL_SECONDS", "2")
            ),
            lease_seconds=int(os.environ.get("INCIDENT_WORKER_LEASE_SECONDS", "120")),
            max_attempts=int(os.environ.get("INCIDENT_WORKER_MAX_ATTEMPTS", "3")),
            provider_timeout_seconds=float(
                os.environ.get("INCIDENT_WORKER_PROVIDER_TIMEOUT_SECONDS", "20")
            ),
            max_evidence_items=int(
                os.environ.get("INCIDENT_WORKER_MAX_EVIDENCE_ITEMS", "32")
            ),
            kubernetes_api_server=os.environ.get(
                "KUBERNETES_API_SERVER", "https://kubernetes.default.svc"
            ),
            kubernetes_token_file=os.environ.get(
                "KUBERNETES_TOKEN_FILE",
                "/var/run/secrets/kubernetes.io/serviceaccount/token",
            ),
            kubernetes_ca_file=os.environ.get(
                "KUBERNETES_CA_FILE",
                "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
            ),
            prometheus_base_url=_required_environment("PROMETHEUS_BASE_URL"),
            postgres_host=_required_environment("POSTGRES_HOST"),
            postgres_port=int(os.environ.get("POSTGRES_PORT", "5432")),
            postgres_database=_required_environment("POSTGRES_DATABASE"),
            postgres_username=_required_environment("POSTGRES_USERNAME"),
            postgres_password=_required_secret_environment("POSTGRES_PASSWORD"),
        )


def _postgres_connection_factory(
    config: IncidentWorkerRuntimeConfig,
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
            application_name="incident-worker",
        )

    return connect


def _prometheus_query_specs() -> tuple[PrometheusQuerySpec, ...]:
    common = {
        "namespace_label": "namespace",
        "resource_label": "service_name",
        "step_seconds": 15,
        "max_samples": 4000,
    }
    return (
        PrometheusQuerySpec(
            query_id="api.request-rate",
            expression_template="agent_rca_api_request_rate{{scope}}",
            peak_fact="peak_request_rate",
            **common,
        ),
        PrometheusQuerySpec(
            query_id="api.failure-rate",
            expression_template="agent_rca_api_failure_rate{{scope}}",
            peak_fact="peak_failure_rate",
            **common,
        ),
        PrometheusQuerySpec(
            query_id="api.latency-p95-milliseconds",
            expression_template=(
                "agent_rca_api_latency_p95_milliseconds{{scope}} >= 0"
            ),
            peak_fact="peak_latency_milliseconds",
            **common,
        ),
        PrometheusQuerySpec(
            query_id="api.latency-baseline-p95-milliseconds",
            expression_template=(
                "agent_rca_api_latency_baseline_p95_milliseconds{{scope}} >= 0"
            ),
            peak_fact="peak_baseline_latency_milliseconds",
            **common,
        ),
    )


def build_collection_service(
    config: IncidentWorkerRuntimeConfig,
    incident_repository: PostgreSQLIncidentRepository,
) -> IncidentCollectionService:
    with open(config.kubernetes_token_file, encoding="utf-8") as token_file:
        bearer_token = token_file.read().strip()
    ssl_context = ssl.create_default_context(cafile=config.kubernetes_ca_file)
    kubernetes_client = KubernetesHTTPAPI(
        config.kubernetes_api_server,
        bearer_token=bearer_token,
        transport=BoundedJSONTransport(
            max_response_bytes=4 * 1024 * 1024,
            ssl_context=ssl_context,
        ),
    )
    kubernetes_provider = KubernetesStateProvider(
        kubernetes_client,
        KubernetesResourceSpec("v1", "Service", required=True),
        cluster_id=config.cluster_id,
        include_events=True,
        event_page_size=20,
        max_events=20,
    )
    prometheus_provider = PrometheusMetricProvider(
        PrometheusHTTPAPI(config.prometheus_base_url),
        _prometheus_query_specs(),
    )
    return IncidentCollectionService(
        incident_repository,
        CollectorOrchestrator(
            (
                CollectorSpec(
                    "kubernetes",
                    kubernetes_provider,
                    timeout_seconds=config.provider_timeout_seconds,
                    max_attempts=2,
                ),
                CollectorSpec(
                    "prometheus",
                    prometheus_provider,
                    timeout_seconds=config.provider_timeout_seconds,
                    max_attempts=2,
                ),
            )
        ),
    )


class IncidentWorker:
    def __init__(
        self,
        config: IncidentWorkerRuntimeConfig,
        incident_repository: PostgreSQLIncidentRepository,
        work_repository: IncidentWorkRepository,
        collection_service: IncidentCollectionService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._incidents = incident_repository
        self._work = work_repository
        self._collection = collection_service
        self._clock = clock

    def process_one(self) -> Mapping[str, Any]:
        now = self._clock()
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
            return {"status": "IDLE", "reaped": reaped}

        incident = claim.incident
        source = incident["source_entity"]
        try:
            if source["namespace"] != self._config.target_namespace:
                raise ValueError("Incident namespace is outside worker scope")
            if source["kind"] != "Service":
                raise ValueError("initial worker supports only Service-scoped Incidents")
            scope = ResourceScope(
                namespace=source["namespace"],
                resource_names=(source["name"],),
                max_items=self._config.max_evidence_items,
            )
            run = self._collection.collect_claimed_incident(
                claim.incident_id,
                scope=scope,
                observed_at=now,
            )
            self._work.complete(
                claim,
                now=self._clock(),
                outcome=run.status,
            )
            return {
                "status": "PROCESSED",
                "incident_id": claim.incident_id,
                "attempt": claim.attempt_count,
                "collection_status": run.status,
                "evidence_count": len(run.evidence),
                "collector_statuses": {
                    execution.name: execution.status for execution in run.executions
                },
                "reaped": reaped,
            }
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
                    "incident_id": claim.incident_id,
                    "attempt": claim.attempt_count,
                    "error_code": error_code,
                    "reaped": reaped,
                }
            return {
                "status": "FAILED",
                "incident_id": claim.incident_id,
                "attempt": claim.attempt_count,
                "error_code": error_code,
                "reaped": reaped,
            }


def build_worker(config: IncidentWorkerRuntimeConfig) -> IncidentWorker:
    connection_factory = _postgres_connection_factory(config)
    apply_migrations(connection_factory)
    incident_repository = PostgreSQLIncidentRepository(connection_factory)
    work_repository = PostgreSQLIncidentWorkRepository(connection_factory)
    collection_service = build_collection_service(config, incident_repository)
    return IncidentWorker(
        config,
        incident_repository,
        work_repository,
        collection_service,
    )


def main() -> int:
    stop = threading.Event()

    def request_stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        config = IncidentWorkerRuntimeConfig.from_environment()
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

    while not stop.is_set():
        result = worker.process_one()
        if result["status"] != "IDLE" or result.get("reaped"):
            print(json.dumps(result, sort_keys=True), flush=True)
        stop.wait(config.poll_interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
