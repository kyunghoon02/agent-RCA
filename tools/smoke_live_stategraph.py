#!/usr/bin/env python3
"""Ingest bounded live Kubernetes inventory and freeze one Neo4j Context."""

from __future__ import annotations

import json
import os
import ssl
from datetime import datetime, timedelta, timezone

from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceWindow,
    ResourceScope,
    format_time,
    validate_provider_batch,
)
from incident_platform.localization import IncidentLocalizationService
from incident_platform.neo4j_stategraph import (
    Neo4jStateGraphRepository,
    apply_neo4j_schema,
    create_neo4j_driver,
)
from incident_platform.projectors import KubernetesEvidenceProjector
from incident_platform.providers.http import BoundedJSONTransport
from incident_platform.providers.kubernetes import (
    KubernetesHTTPAPI,
    KubernetesInventoryProvider,
)
from incident_platform.repository import InMemoryIncidentRepository
from incident_platform.resolution import (
    EntityResolutionRequest,
    ResolvedIncidentLocalizationService,
    ServiceToEntityResolver,
)

_CURRENT_STAGE = "environment"


def _stage(name: str) -> None:
    global _CURRENT_STAGE
    _CURRENT_STAGE = name


def _safe_error(error: Exception) -> str:
    message = " ".join(str(error).split())
    sensitive_values = {
        os.environ.get("KUBERNETES_BEARER_TOKEN", ""),
        os.environ.get("NEO4J_AUTH", ""),
    }
    neo4j_auth = os.environ.get("NEO4J_AUTH", "")
    if "/" in neo4j_auth:
        sensitive_values.update(neo4j_auth.split("/", 1))
    for value in sorted(sensitive_values, key=len, reverse=True):
        if value:
            message = message.replace(value, "[redacted]")
    return message[:500]


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def required_secret_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def graph_counts(driver, *, database: str, cluster_id: str, namespace: str) -> dict:
    queries = {
        "entities": """
            MATCH (entity:StateGraphEntity)
            WHERE entity.cluster_id = $cluster_id
              AND entity.namespace = $namespace
            RETURN count(entity) AS count
        """,
        "snapshots": """
            MATCH (entity:StateGraphEntity)-[:HAS_SNAPSHOT]->(snapshot)
            WHERE entity.cluster_id = $cluster_id
              AND entity.namespace = $namespace
            RETURN count(snapshot) AS count
        """,
        "relations": """
            MATCH (source:StateGraphEntity)-[relation:STATEGRAPH_RELATION]->
                  (destination:StateGraphEntity)
            WHERE source.cluster_id = $cluster_id
              AND destination.cluster_id = $cluster_id
              AND (
                source.namespace = $namespace
                OR destination.namespace = $namespace
              )
            RETURN count(relation) AS count
        """,
    }
    counts = {}
    with driver.session(database=database) as session:
        for label, query in queries.items():
            row = session.run(
                query,
                cluster_id=cluster_id,
                namespace=namespace,
            ).single()
            counts[label] = int(row["count"])
    return counts


def logical_service_names(
    driver,
    *,
    database: str,
    cluster_id: str,
    namespace: str,
) -> tuple[str, ...]:
    with driver.session(database=database) as session:
        rows = session.run(
            """
            MATCH (entity:StateGraphEntity)
            WHERE entity.cluster_id = $cluster_id
              AND entity.namespace = $namespace
              AND entity.domain = 'web-service'
              AND entity.identity_type = 'logical-service'
            RETURN entity.name AS name
            ORDER BY name
            """,
            cluster_id=cluster_id,
            namespace=namespace,
        )
        return tuple(row["name"] for row in rows)


def relation_types(
    driver,
    *,
    database: str,
    cluster_id: str,
    namespace: str,
) -> tuple[str, ...]:
    with driver.session(database=database) as session:
        rows = session.run(
            """
            MATCH (source:StateGraphEntity)-[relation:STATEGRAPH_RELATION]->
                  (destination:StateGraphEntity)
            WHERE source.cluster_id = $cluster_id
              AND destination.cluster_id = $cluster_id
              AND (
                source.namespace = $namespace
                OR destination.namespace = $namespace
              )
            RETURN DISTINCT relation.relation_type AS relation_type
            ORDER BY relation_type
            """,
            cluster_id=cluster_id,
            namespace=namespace,
        )
        return tuple(row["relation_type"] for row in rows)


def incident(
    *,
    incident_id: str,
    namespace: str,
    seed_service: str,
    source_entity: dict,
    window: EvidenceWindow,
    now: datetime,
) -> dict:
    incident_start = format_time(now - timedelta(minutes=1))
    return {
        "schema_version": "1.0.0",
        "incident_id": incident_id,
        "deduplication_key": f"live-stategraph:{incident_id}",
        "status": "LOCALIZING",
        "severity": "info",
        "source": "alertmanager",
        "triggered_at": incident_start,
        "window": {
            "baseline_start": window.start,
            "incident_start": incident_start,
            "incident_end": window.end,
            "recovery_end": None,
        },
        "alert": {
            "fingerprint": incident_id,
            "name": "LiveStateGraphSmoke",
            "labels": {
                "alertname": "LiveStateGraphSmoke",
                "namespace": namespace,
                "service": seed_service,
                "severity": "info",
            },
            "annotations": {},
        },
        "source_entity": source_entity,
        "collector_statuses": [
            {
                "collector": "kubernetes",
                "status": "SUCCEEDED",
                "attempts": 1,
                "started_at": window.end,
                "ended_at": window.end,
                "error": None,
            }
        ],
        "created_at": window.end,
        "updated_at": window.end,
    }


def main() -> int:
    _stage("environment")
    cluster_id = required_environment("STATEGRAPH_CLUSTER_ID")
    namespace = required_environment("STATEGRAPH_TARGET_NAMESPACE")
    seed_service = required_environment("STATEGRAPH_SEED_SERVICE")
    service_names = tuple(
        sorted(
            {
                value.strip()
                for value in required_environment(
                    "STATEGRAPH_APPLICATION_SERVICES"
                ).split(",")
                if value.strip()
            }
        )
    )
    if seed_service not in service_names:
        raise SystemExit("STATEGRAPH_SEED_SERVICE is outside the application service set")

    api_server = required_environment("KUBERNETES_API_SERVER")
    bearer_token = required_secret_environment("KUBERNETES_BEARER_TOKEN")
    ca_file = required_environment("KUBERNETES_CA_FILE")
    neo4j_uri = required_environment("NEO4J_URI")
    neo4j_auth = required_secret_environment("NEO4J_AUTH")
    if "/" not in neo4j_auth:
        raise SystemExit("NEO4J_AUTH must contain username/password")
    neo4j_username, neo4j_password = neo4j_auth.split("/", 1)
    neo4j_database = os.environ.get("NEO4J_DATABASE", "neo4j")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    window = EvidenceWindow(
        start=format_time(now - timedelta(minutes=5)),
        end=format_time(now),
    )
    incident_id = f"inc-live-stategraph-{now.strftime('%Y%m%dt%H%M%S').lower()}"
    collection_request = CollectionRequest(
        request_id=f"req-{incident_id[4:]}",
        incident_id=incident_id,
        window=window,
        scope=ResourceScope(
            namespace=namespace,
            resource_names=service_names,
            resource_name_prefixes=tuple(f"{name}-" for name in service_names),
            max_items=100,
        ),
        timeout_seconds=20,
    )
    ssl_context = ssl.create_default_context(cafile=ca_file)
    kubernetes_client = KubernetesHTTPAPI(
        api_server,
        bearer_token=bearer_token,
        transport=BoundedJSONTransport(
            max_response_bytes=4 * 1024 * 1024,
            ssl_context=ssl_context,
        ),
    )
    _stage("kubernetes_inventory")
    batch = KubernetesInventoryProvider(
        kubernetes_client,
        cluster_id=cluster_id,
        page_size=100,
        max_raw_resources=500,
    ).collect(collection_request)
    validate_provider_batch(batch, collection_request)
    _stage("evidence_projection")
    evidence = tuple(
        EvidenceBuilder().build(item, collection_request, collected_at=now)
        for item in batch.items
    )
    projector = KubernetesEvidenceProjector()
    records = tuple(
        record
        for item in evidence
        for record in projector.project(item).records
    )
    source_evidence = next(
        item
        for item in evidence
        if item["subject"]["kind"] == "Service"
        and item["subject"]["name"] == seed_service
    )

    _stage("neo4j_connectivity")
    driver = create_neo4j_driver(neo4j_uri, neo4j_username, neo4j_password)
    try:
        driver.verify_connectivity()
        _stage("neo4j_schema")
        schema_objects = apply_neo4j_schema(driver, database=neo4j_database)
        repository = Neo4jStateGraphRepository(driver, database=neo4j_database)
        _stage("neo4j_ingest")
        repository.ingest(records)
        first_counts = graph_counts(
            driver,
            database=neo4j_database,
            cluster_id=cluster_id,
            namespace=namespace,
        )
        repository.ingest(records)
        second_counts = graph_counts(
            driver,
            database=neo4j_database,
            cluster_id=cluster_id,
            namespace=namespace,
        )
        if first_counts != second_counts:
            raise RuntimeError("repeated StateGraph ingestion created duplicate records")

        _stage("incident_localization")
        incident_repository = InMemoryIncidentRepository()
        incident_repository.create_or_get_by_deduplication_key(
            incident(
                incident_id=incident_id,
                namespace=namespace,
                seed_service=seed_service,
                source_entity=dict(source_evidence["subject"]),
                window=window,
                now=now,
            ),
            occurred_at=now,
        )
        incident_repository.store_evidence(incident_id, evidence)
        localization = ResolvedIncidentLocalizationService(
            ServiceToEntityResolver(repository),
            IncidentLocalizationService(
                incident_repository,
                repository,
                (projector,),
            ),
        ).localize_service(
            EntityResolutionRequest(
                incident_id=incident_id,
                cluster_id=cluster_id,
                namespace=namespace,
                service_name=seed_service,
                window=window,
            ),
            frozen_at=now,
            max_entities=40,
            max_depth=4,
        )
        if localization.resolution.status != "RESOLVED":
            raise RuntimeError(
                f"live Entity resolution returned {localization.resolution.status}"
            )
        if localization.localization is None:
            raise RuntimeError("live StateGraph localization did not freeze a Context")
        context = localization.localization.context
        _stage("graph_assertions")
        live_services = logical_service_names(
            driver,
            database=neo4j_database,
            cluster_id=cluster_id,
            namespace=namespace,
        )
        missing_services = sorted(set(service_names) - set(live_services))
        if missing_services:
            raise RuntimeError(
                "StateGraph is missing logical services: " + ", ".join(missing_services)
            )
        live_relation_types = relation_types(
            driver,
            database=neo4j_database,
            cluster_id=cluster_id,
            namespace=namespace,
        )
        required_relations = {
            "OWNS",
            "REPRESENTED_BY",
            "ROUTES_TO",
            "SCHEDULED_ON",
            "SELECTS",
        }
        missing_relations = sorted(required_relations - set(live_relation_types))
        if missing_relations:
            raise RuntimeError(
                "StateGraph is missing topology relationships: "
                + ", ".join(missing_relations)
            )
        output = {
            "status": "CONNECTED",
            "cluster_id": cluster_id,
            "namespace": namespace,
            "inventory_evidence_count": len(evidence),
            "projected_record_count": len(records),
            "idempotent_reingest": first_counts == second_counts,
            "graph_counts": second_counts,
            "application_service_count": len(
                set(service_names) & set(live_services)
            ),
            "application_services_present": sorted(
                set(service_names) & set(live_services)
            ),
            "relation_types": list(live_relation_types),
            "resolution": {
                "service": seed_service,
                "status": localization.resolution.status,
                "method": localization.resolution.method,
                "candidate_count": len(localization.resolution.candidates),
            },
            "context": {
                "status": localization.localization.incident["status"],
                "entity_count": localization.localization.context["localization"][
                    "candidate_entities_after"
                ],
                "path_count": len(context["state_paths"]),
                "evidence_count": len(context["evidence_ids"]),
                "completeness": context["localization"]["context_completeness"],
            },
            "schema_object_count": len(schema_objects),
        }
        print(json.dumps(output, ensure_ascii=True, sort_keys=True))
    finally:
        driver.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "error_stage": _CURRENT_STAGE,
                    "error_type": type(error).__name__,
                    "error": _safe_error(error),
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        raise SystemExit(1)
