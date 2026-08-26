#!/usr/bin/env python3
"""Claim durable Incidents and collect bounded in-cluster Evidence."""

from __future__ import annotations

import json
import os
import signal
import ssl
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol

from incident_platform.collectors import (
    CollectionRun,
    CollectorOrchestrator,
    CollectorSpec,
    IncidentCollectionService,
)
from incident_platform.evidence import EvidenceWindow, ResourceScope, format_time, parse_time
from incident_platform.incident_work import (
    IncidentLocalizationWorkRepository,
    IncidentWorkRepository,
    validate_claim_request,
)
from incident_platform.localization import IncidentLocalizationService
from incident_platform.krca_pipeline import KRCAGuidedIncidentLocalizationService
from incident_platform.krca_runtime import (
    KRCARuntimeConfig,
    KRCARuntimeProfile,
    load_krca_runtime_config,
)
from incident_platform.neo4j_stategraph import (
    Neo4jStateGraphRepository,
    apply_neo4j_schema,
    create_neo4j_driver,
)
from incident_platform.postgresql import (
    PostgreSQLIncidentLocalizationWorkRepository,
    PostgreSQLIncidentRepository,
    PostgreSQLIncidentWorkRepository,
    apply_migrations,
)
from incident_platform.projectors import (
    DeploymentChangeEvidenceProjector,
    KRCAPIEdgeEvidenceProjector,
    KubernetesEvidenceProjector,
    LokiKernelOOMEvidenceProjector,
    PrometheusMetricEvidenceProjector,
    PrometheusWorkloadMetricEvidenceProjector,
)
from incident_platform.providers.change import DeploymentHistoryProvider
from incident_platform.providers.http import BoundedJSONTransport
from incident_platform.providers.kubernetes import (
    KubernetesHTTPAPI,
    KubernetesIncidentProvider,
    KubernetesInventoryProvider,
    KubernetesResourceSpec,
    KubernetesStateProvider,
)
from incident_platform.providers.loki import LokiHTTPAPI, LokiKernelOOMProvider
from incident_platform.providers.prometheus import (
    PrometheusHTTPAPI,
    PrometheusMetricProvider,
    PrometheusQuerySpec,
    PrometheusWorkloadMetricProvider,
)
from incident_platform.repository import IncidentRepository
from incident_platform.resolution import (
    EntityResolutionRequest,
    ResolvedIncidentLocalizationService,
    ServiceToEntityResolver,
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
    loki_base_url: str
    krca_config_path: str
    neo4j_uri: str
    neo4j_username: str
    neo4j_password: str
    neo4j_database: str
    localization_max_candidates: int
    localization_max_entities: int
    localization_max_depth: int
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
        if not 2 <= self.localization_max_candidates <= 99:
            raise ValueError("localization candidates must be between 2 and 99")
        if not 1 <= self.localization_max_entities <= 1000:
            raise ValueError("localization entities must be between 1 and 1000")
        if not 0 <= self.localization_max_depth <= 16:
            raise ValueError("localization depth must be between 0 and 16")
        if not 1 <= self.postgres_port <= 65535:
            raise ValueError("PostgreSQL port is invalid")
        if not self.krca_config_path.strip():
            raise ValueError("KRCA config path is required")

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
            loki_base_url=_required_environment("LOKI_BASE_URL"),
            krca_config_path=_required_environment("INCIDENT_WORKER_KRCA_CONFIG"),
            neo4j_uri=_required_environment("NEO4J_URI"),
            neo4j_username=_required_environment("NEO4J_USERNAME"),
            neo4j_password=_required_secret_environment("NEO4J_PASSWORD"),
            neo4j_database=os.environ.get("NEO4J_DATABASE", "neo4j"),
            localization_max_candidates=int(
                os.environ.get("INCIDENT_WORKER_LOCALIZATION_MAX_CANDIDATES", "10")
            ),
            localization_max_entities=int(
                os.environ.get("INCIDENT_WORKER_LOCALIZATION_MAX_ENTITIES", "40")
            ),
            localization_max_depth=int(
                os.environ.get("INCIDENT_WORKER_LOCALIZATION_MAX_DEPTH", "4")
            ),
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


def _prometheus_workload_query_specs() -> tuple[PrometheusQuerySpec, ...]:
    return (
        PrometheusQuerySpec(
            query_id="memory_working_set_ratio",
            expression_template="agent_rca_pod_memory_working_set_ratio{{scope}}",
            namespace_label="namespace",
            resource_label="pod",
            subject_kind="Pod",
            uid_label="uid",
            step_seconds=15,
            max_samples=4000,
            peak_fact="peak_ratio",
        ),
        PrometheusQuerySpec(
            query_id="restart_count_delta",
            expression_template="agent_rca_pod_restart_count_delta{{scope}}",
            namespace_label="namespace",
            resource_label="pod",
            subject_kind="Pod",
            uid_label="uid",
            step_seconds=15,
            max_samples=4000,
            peak_fact="peak_delta",
        ),
    )


class ClaimedIncidentCollectionService(Protocol):
    def collect_claimed_incident(
        self,
        incident_id: str,
        *,
        scope: ResourceScope,
        observed_at: Optional[datetime] = None,
    ) -> CollectionRun:
        ...


def _selected_krca_profile(
    incident: Mapping[str, Any],
    config: KRCARuntimeConfig,
) -> Optional[KRCARuntimeProfile]:
    profile_id = incident["alert"]["labels"].get("krca_profile", "").strip()
    if not profile_id:
        return None
    profile = config.profile(profile_id)
    source = incident["source_entity"]
    if source["namespace"] != config.namespace:
        raise ValueError("KRCA profile namespace does not match the Incident")
    if source["kind"] != "Service" or source["name"] != profile.alerting_api.service:
        raise ValueError("KRCA profile alerting Service does not match the Incident")
    return profile


class ProfileAwareIncidentCollectionService:
    """Add one isolated KRCA feature collector only for an explicit profile label."""

    def __init__(
        self,
        repository: PostgreSQLIncidentRepository,
        base_specs: tuple[CollectorSpec, ...],
        prometheus_client: PrometheusHTTPAPI,
        krca_config: KRCARuntimeConfig,
    ) -> None:
        self._repository = repository
        self._base_specs = base_specs
        self._prometheus_client = prometheus_client
        self._krca_config = krca_config

    def collect_claimed_incident(
        self,
        incident_id: str,
        *,
        scope: ResourceScope,
        observed_at: Optional[datetime] = None,
    ) -> CollectionRun:
        incident = self._repository.get(incident_id)
        profile = _selected_krca_profile(incident, self._krca_config)
        specs = []
        for spec in self._base_specs:
            if spec.name not in {
                "kubernetes",
                "prometheus-workload",
                "loki-kernel-oom",
            }:
                specs.append(spec)
                continue
            rooted_scope = ResourceScope(
                namespace=scope.namespace,
                resource_names=scope.resource_names,
                resource_name_prefixes=tuple(
                    f"{name}-" for name in scope.resource_names
                ),
                max_items=scope.max_items,
            )
            specs.append(replace(spec, request_scope=rooted_scope))
        if profile is not None:
            specs.append(
                CollectorSpec(
                    "prometheus-api",
                    self._krca_config.provider(self._prometheus_client, profile),
                    timeout_seconds=self._krca_config.collection.timeout_seconds,
                    max_attempts=2,
                    request_scope=ResourceScope(
                        namespace=self._krca_config.namespace,
                        resource_names=profile.resource_names,
                        max_items=self._krca_config.collection.max_evidence_items,
                    ),
                    lookback_seconds=self._krca_config.collection.window_seconds,
                )
            )
        service = IncidentCollectionService(
            self._repository,
            CollectorOrchestrator(tuple(specs)),
        )
        return service.collect_claimed_incident(
            incident_id,
            scope=scope,
            observed_at=observed_at,
        )


def build_collection_service(
    config: IncidentWorkerRuntimeConfig,
    incident_repository: PostgreSQLIncidentRepository,
    krca_config: KRCARuntimeConfig,
) -> ProfileAwareIncidentCollectionService:
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
    kubernetes_inventory = KubernetesInventoryProvider(
        kubernetes_client,
        cluster_id=config.cluster_id,
        page_size=100,
        max_raw_resources=500,
    )
    service_events = KubernetesStateProvider(
        kubernetes_client,
        KubernetesResourceSpec("v1", "Service", required=True),
        cluster_id=config.cluster_id,
        include_events=True,
        event_page_size=20,
        max_events=4,
    )
    pod_events = KubernetesStateProvider(
        kubernetes_client,
        KubernetesResourceSpec("v1", "Pod", required=True),
        cluster_id=config.cluster_id,
        include_events=True,
        event_page_size=20,
        max_events=8,
    )
    kubernetes_provider = KubernetesIncidentProvider(
        kubernetes_inventory,
        service_events,
        pod_events,
    )
    deployment_provider = DeploymentHistoryProvider(
        kubernetes_client,
        cluster_id=config.cluster_id,
        page_size=100,
        max_replica_sets=500,
    )
    prometheus_client = PrometheusHTTPAPI(config.prometheus_base_url)
    prometheus_provider = PrometheusMetricProvider(
        prometheus_client,
        _prometheus_query_specs(),
        cluster_id=config.cluster_id,
    )
    prometheus_workload_provider = PrometheusWorkloadMetricProvider(
        prometheus_client,
        _prometheus_workload_query_specs(),
        cluster_id=config.cluster_id,
    )
    loki_kernel_oom_provider = LokiKernelOOMProvider(
        LokiHTTPAPI(config.loki_base_url),
        kubernetes_client,
        cluster_id=config.cluster_id,
        pod_page_size=100,
        max_raw_pods=500,
        max_matches=50,
    )
    return ProfileAwareIncidentCollectionService(
        incident_repository,
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
            CollectorSpec(
                "prometheus-workload",
                prometheus_workload_provider,
                timeout_seconds=config.provider_timeout_seconds,
                max_attempts=2,
            ),
            CollectorSpec(
                "loki-kernel-oom",
                loki_kernel_oom_provider,
                timeout_seconds=config.provider_timeout_seconds,
                max_attempts=2,
            ),
            CollectorSpec(
                "deployment",
                deployment_provider,
                timeout_seconds=config.provider_timeout_seconds,
                max_attempts=2,
            ),
        ),
        prometheus_client,
        krca_config,
    )


class IncidentWorker:
    def __init__(
        self,
        config: IncidentWorkerRuntimeConfig,
        incident_repository: IncidentRepository,
        work_repository: IncidentWorkRepository,
        collection_service: ClaimedIncidentCollectionService,
        localization_work_repository: IncidentLocalizationWorkRepository | None = None,
        localization_service: ResolvedIncidentLocalizationService | None = None,
        krca_config: KRCARuntimeConfig | None = None,
        krca_localization_service: KRCAGuidedIncidentLocalizationService | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._incidents = incident_repository
        self._work = work_repository
        self._collection = collection_service
        if (localization_work_repository is None) != (localization_service is None):
            raise ValueError(
                "localization work repository and service must be configured together"
            )
        self._localization_work = localization_work_repository
        self._localization = localization_service
        if (krca_config is None) != (krca_localization_service is None):
            raise ValueError(
                "KRCA runtime config and localization service must be configured together"
            )
        self._krca_config = krca_config
        self._krca_localization = krca_localization_service
        self._clock = clock

    def process_one(self) -> Mapping[str, Any]:
        now = self._clock()
        collection_reaped = self._work.reap_exhausted(
            now=now,
            max_attempts=self._config.max_attempts,
        )
        localization_reaped = 0
        if self._localization_work is not None:
            localization_reaped = self._localization_work.reap_exhausted(
                now=now,
                max_attempts=self._config.max_attempts,
            )
            localization_claim = self._localization_work.claim_next(
                worker_id=self._config.worker_id,
                now=now,
                lease_duration=timedelta(seconds=self._config.lease_seconds),
                max_attempts=self._config.max_attempts,
            )
            if localization_claim is not None:
                return self._process_localization(
                    localization_claim,
                    reaped=collection_reaped + localization_reaped,
                )
        claim = self._work.claim_next(
            worker_id=self._config.worker_id,
            now=now,
            lease_duration=timedelta(seconds=self._config.lease_seconds),
            max_attempts=self._config.max_attempts,
        )
        if claim is None:
            return {
                "status": "IDLE",
                "reaped": collection_reaped + localization_reaped,
            }

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
                "stage": "COLLECTION",
                "incident_id": claim.incident_id,
                "attempt": claim.attempt_count,
                "collection_status": run.status,
                "evidence_count": len(run.evidence),
                "collector_statuses": {
                    execution.name: execution.status for execution in run.executions
                },
                "reaped": collection_reaped + localization_reaped,
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
                    "stage": "COLLECTION",
                    "incident_id": claim.incident_id,
                    "attempt": claim.attempt_count,
                    "error_code": error_code,
                    "reaped": collection_reaped + localization_reaped,
                }
            return {
                "status": "FAILED",
                "stage": "COLLECTION",
                "incident_id": claim.incident_id,
                "attempt": claim.attempt_count,
                "error_code": error_code,
                "reaped": collection_reaped + localization_reaped,
            }

    def _process_localization(
        self,
        claim: Any,
        *,
        reaped: int,
    ) -> Mapping[str, Any]:
        assert self._localization_work is not None
        assert self._localization is not None
        incident = claim.incident
        source = incident["source_entity"]
        try:
            if source["namespace"] != self._config.target_namespace:
                raise ValueError("Incident namespace is outside worker scope")
            if source["kind"] != "Service":
                raise ValueError("initial worker supports only Service-scoped Incidents")
            frozen_at = self._clock()
            known_end = (
                incident["window"]["recovery_end"]
                or incident["window"]["incident_end"]
            )
            if known_end is not None and parse_time(
                known_end, "Incident.window.end"
            ) <= frozen_at:
                window_end = known_end
            else:
                window_end = format_time(frozen_at)
            resolution_request = EntityResolutionRequest(
                incident_id=claim.incident_id,
                cluster_id=self._config.cluster_id,
                namespace=source["namespace"],
                service_name=source["name"],
                window=EvidenceWindow(
                    start=incident["window"]["baseline_start"],
                    end=window_end,
                ),
                max_candidates=self._config.localization_max_candidates,
            )
            profile = (
                _selected_krca_profile(incident, self._krca_config)
                if self._krca_config is not None
                else None
            )
            krca_run = None
            if profile is not None:
                assert self._krca_localization is not None
                krca_run = self._krca_localization.localize(
                    resolution_request,
                    profile_id=profile.profile_id,
                    alerting_api=profile.alerting_api,
                    expected_edges={
                        edge.edge_id: (edge.parent, edge.child)
                        for edge in profile.dependencies
                    },
                    evidence=self._incidents.list_evidence(claim.incident_id),
                    frozen_at=frozen_at,
                    max_entities=self._config.localization_max_entities,
                    max_depth=self._config.localization_max_depth,
                )
                localization = krca_run.localization
                resolution = (
                    krca_run.source_resolution
                    if krca_run.source_resolution is not None
                    else None
                )
                if resolution is None and krca_run.top_resolution is not None:
                    candidate_count = len(krca_run.top_resolution.resolutions)
                    resolution_status = (
                        "RESOLVED" if krca_run.top_resolution.complete else "NOT_FOUND"
                    )
                else:
                    candidate_count = len(resolution.candidates) if resolution else 0
                    resolution_status = resolution.status if resolution else "NOT_FOUND"
            else:
                run = self._localization.localize_service(
                    resolution_request,
                    frozen_at=frozen_at,
                    max_entities=self._config.localization_max_entities,
                    max_depth=self._config.localization_max_depth,
                )
                localization = run.localization
                candidate_count = len(run.resolution.candidates)
                resolution_status = run.resolution.status
            if localization is None:
                error_code = f"ENTITY_{resolution_status}"
                self._localization_work.fail(
                    claim,
                    now=self._clock(),
                    error_code=error_code,
                )
                return {
                    "status": "FAILED",
                    "stage": "LOCALIZATION",
                    "incident_id": claim.incident_id,
                    "attempt": claim.attempt_count,
                    "error_code": error_code,
                    "candidate_count": candidate_count,
                    "reaped": reaped,
                }
            self._localization_work.complete(
                claim,
                now=self._clock(),
                outcome="SUCCEEDED",
            )
            return {
                "status": "PROCESSED",
                "stage": "LOCALIZATION",
                "incident_id": claim.incident_id,
                "attempt": claim.attempt_count,
                "resolution_method": (
                    krca_run.seed_source
                    if krca_run is not None
                    else run.resolution.method
                ),
                "krca_profile": profile.profile_id if profile is not None else None,
                "krca_stop_reason": (
                    krca_run.feature_run.drilldown.stop_reason
                    if krca_run is not None
                    else None
                ),
                "context_id": localization.context["context_id"],
                "entity_count": localization.context["localization"][
                    "candidate_entities_after"
                ],
                "path_count": len(localization.context["state_paths"]),
                "evidence_count": len(localization.context["evidence_ids"]),
                "reaped": reaped,
            }
        except Exception as error:
            error_code = type(error).__name__.upper()
            try:
                self._localization_work.fail(
                    claim,
                    now=self._clock(),
                    error_code=error_code,
                )
            except Exception:
                return {
                    "status": "FAILURE_PERSISTENCE_FAILED",
                    "stage": "LOCALIZATION",
                    "incident_id": claim.incident_id,
                    "attempt": claim.attempt_count,
                    "error_code": error_code,
                    "reaped": reaped,
                }
            return {
                "status": "FAILED",
                "stage": "LOCALIZATION",
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
    localization_work_repository = PostgreSQLIncidentLocalizationWorkRepository(
        connection_factory
    )
    krca_config = load_krca_runtime_config(Path(config.krca_config_path))
    if krca_config.cluster_id != config.cluster_id:
        raise ValueError("KRCA config cluster_id does not match the worker")
    if krca_config.namespace != config.target_namespace:
        raise ValueError("KRCA config namespace does not match the worker")
    if krca_config.collection.timeout_seconds > config.lease_seconds / 2:
        raise ValueError("KRCA collection timeout does not fit inside the work lease")
    collection_service = build_collection_service(
        config,
        incident_repository,
        krca_config,
    )
    driver = create_neo4j_driver(
        config.neo4j_uri,
        config.neo4j_username,
        config.neo4j_password,
    )
    driver.verify_connectivity()
    apply_neo4j_schema(driver, database=config.neo4j_database)
    graph_repository = Neo4jStateGraphRepository(
        driver,
        database=config.neo4j_database,
    )
    resolver = ServiceToEntityResolver(graph_repository)
    incident_localization_service = IncidentLocalizationService(
        incident_repository,
        graph_repository,
        (
            DeploymentChangeEvidenceProjector(),
            KubernetesEvidenceProjector(),
            LokiKernelOOMEvidenceProjector(),
            PrometheusMetricEvidenceProjector(),
            PrometheusWorkloadMetricEvidenceProjector(),
            KRCAPIEdgeEvidenceProjector(),
        ),
    )
    localization_service = ResolvedIncidentLocalizationService(
        resolver,
        incident_localization_service,
    )
    krca_localization_service = KRCAGuidedIncidentLocalizationService(
        resolver,
        incident_localization_service,
    )
    return IncidentWorker(
        config,
        incident_repository,
        work_repository,
        collection_service,
        localization_work_repository,
        localization_service,
        krca_config,
        krca_localization_service,
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
