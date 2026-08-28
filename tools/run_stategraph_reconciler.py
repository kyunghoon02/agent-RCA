#!/usr/bin/env python3
"""Run one durable Kubernetes inventory reconciliation cycle in-cluster."""

from __future__ import annotations

import json
import os
import ssl
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from incident_platform.evidence import (
    CollectionRequest,
    EvidenceWindow,
    ResourceScope,
    format_time,
)
from incident_platform.neo4j_stategraph import (
    Neo4jStateGraphRepository,
    apply_neo4j_schema,
    create_neo4j_driver,
)
from incident_platform.postgresql import (
    PostgreSQLStateGraphObservationRepository,
    apply_migrations,
)
from incident_platform.projectors.kubernetes import KubernetesEvidenceProjector
from incident_platform.providers.http import BoundedJSONTransport
from incident_platform.providers.kubernetes import (
    KubernetesHTTPAPI,
    KubernetesInventoryProvider,
)
from incident_platform.reconciliation import KubernetesStateGraphReconciler
from incident_platform.stategraph import stable_graph_id


UTC = timezone.utc
_CURRENT_STAGE = "configuration"


def _stage(name: str) -> None:
    global _CURRENT_STAGE
    _CURRENT_STAGE = name


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
class RuntimeConfig:
    cluster_id: str
    target_namespace: str
    application_services: tuple[str, ...]
    schedule_interval_seconds: int
    kubernetes_api_server: str
    kubernetes_token_file: str
    kubernetes_ca_file: str
    neo4j_uri: str
    neo4j_username: str
    neo4j_password: str
    neo4j_database: str
    postgres_host: str
    postgres_port: int
    postgres_database: str
    postgres_username: str
    postgres_password: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "application_services",
            tuple(sorted(self.application_services)),
        )
        if not self.application_services or len(self.application_services) != len(
            set(self.application_services)
        ):
            raise ValueError("application services must be non-empty and unique")
        if not 60 <= self.schedule_interval_seconds <= 3600:
            raise ValueError("schedule interval must be between 60 and 3600 seconds")
        if 3600 % self.schedule_interval_seconds != 0:
            raise ValueError("schedule interval must divide one hour")
        if not 1 <= self.postgres_port <= 65535:
            raise ValueError("PostgreSQL port is invalid")

    @classmethod
    def from_environment(cls) -> "RuntimeConfig":
        services = tuple(
            value.strip()
            for value in _required_environment(
                "STATEGRAPH_APPLICATION_SERVICES"
            ).split(",")
            if value.strip()
        )
        return cls(
            cluster_id=_required_environment("STATEGRAPH_CLUSTER_ID"),
            target_namespace=_required_environment(
                "STATEGRAPH_TARGET_NAMESPACE"
            ),
            application_services=services,
            schedule_interval_seconds=int(
                os.environ.get("STATEGRAPH_SCHEDULE_INTERVAL_SECONDS", "300")
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
            neo4j_uri=_required_environment("NEO4J_URI"),
            neo4j_username=_required_environment("NEO4J_USERNAME"),
            neo4j_password=_required_secret_environment("NEO4J_PASSWORD"),
            neo4j_database=os.environ.get("NEO4J_DATABASE", "neo4j"),
            postgres_host=_required_environment("POSTGRES_HOST"),
            postgres_port=int(os.environ.get("POSTGRES_PORT", "5432")),
            postgres_database=_required_environment("POSTGRES_DATABASE"),
            postgres_username=_required_environment("POSTGRES_USERNAME"),
            postgres_password=_required_secret_environment("POSTGRES_PASSWORD"),
        )


def scheduled_observation_time(
    now: datetime,
    interval_seconds: int,
) -> datetime:
    if now.tzinfo is None:
        raise ValueError("runtime time must include a timezone")
    current = now.astimezone(UTC).replace(microsecond=0)
    epoch_seconds = int(current.timestamp())
    return datetime.fromtimestamp(
        epoch_seconds - (epoch_seconds % interval_seconds),
        tz=UTC,
    )


def build_collection_request(
    config: RuntimeConfig,
    observed_at: datetime,
) -> CollectionRequest:
    observed_text = format_time(observed_at)
    request_identity = {
        "purpose": "stategraph-inventory",
        "cluster_id": config.cluster_id,
        "namespace": config.target_namespace,
        "application_services": config.application_services,
        "observed_at": observed_text,
    }
    return CollectionRequest(
        request_id=stable_graph_id("req", request_identity),
        incident_id=stable_graph_id("inc", request_identity),
        window=EvidenceWindow(
            start=format_time(observed_at - timedelta(minutes=5)),
            end=observed_text,
        ),
        scope=ResourceScope(
            namespace=config.target_namespace,
            resource_names=config.application_services,
            resource_name_prefixes=tuple(
                f"{name}-" for name in config.application_services
            ),
            max_items=100,
        ),
        timeout_seconds=45,
    )


def _postgres_connection_factory(config: RuntimeConfig) -> Callable[[], object]:
    def connect() -> object:
        import psycopg

        return psycopg.connect(
            host=config.postgres_host,
            port=config.postgres_port,
            dbname=config.postgres_database,
            user=config.postgres_username,
            password=config.postgres_password,
            connect_timeout=5,
            application_name="stategraph-reconciler",
        )

    return connect


def run_once(config: RuntimeConfig, *, now: datetime) -> dict[str, object]:
    collected_at = now.astimezone(UTC).replace(microsecond=0)
    observed_at = scheduled_observation_time(
        collected_at,
        config.schedule_interval_seconds,
    )
    request = build_collection_request(config, observed_at)

    _stage("postgresql_migrations")
    connection_factory = _postgres_connection_factory(config)
    applied_migrations = apply_migrations(connection_factory)
    observation_repository = PostgreSQLStateGraphObservationRepository(
        connection_factory
    )

    _stage("kubernetes_client")
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
    inventory_provider = KubernetesInventoryProvider(
        kubernetes_client,
        cluster_id=config.cluster_id,
        page_size=100,
        max_raw_resources=500,
    )

    _stage("neo4j_connectivity")
    driver = create_neo4j_driver(
        config.neo4j_uri,
        config.neo4j_username,
        config.neo4j_password,
    )
    try:
        driver.verify_connectivity()
        apply_neo4j_schema(driver, database=config.neo4j_database)
        graph_repository = Neo4jStateGraphRepository(
            driver,
            database=config.neo4j_database,
        )
        reconciler = KubernetesStateGraphReconciler(
            inventory_provider,
            graph_repository,
            observation_repository,
            cluster_id=config.cluster_id,
            projector=KubernetesEvidenceProjector(),
        )

        _stage("stategraph_reconciliation")
        run = reconciler.reconcile(request, collected_at=collected_at)

        _stage("graph_history_gc")
        graph_prune = graph_repository.prune_history(now=collected_at)
        _stage("observation_journal_gc")
        observation_prune = observation_repository.prune_observations(
            now=collected_at
        )
    finally:
        driver.close()

    return {
        "status": run.cycle.status,
        "cycle_id": run.cycle.cycle_id,
        "observed_at": run.cycle.observed_at,
        "applied_at": run.cycle.applied_at,
        "applied_migrations": applied_migrations,
        "evidence_count": len(run.evidence),
        "projected_record_count": run.projected_record_count,
        "reconciliation": asdict(run.result),
        "graph_prune": asdict(graph_prune),
        "observation_prune": asdict(observation_prune),
    }


def main() -> int:
    try:
        config = RuntimeConfig.from_environment()
        result = run_once(config, now=datetime.now(UTC))
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "error_stage": _CURRENT_STAGE,
                    "error_type": type(error).__name__,
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
