#!/usr/bin/env python3
"""Ingest bounded live Kubernetes inventory and freeze one Neo4j Context."""

from __future__ import annotations

import json
import os
import ssl
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from incident_platform.evidence import (
    CollectionRequest,
    EvidenceWindow,
    ResourceScope,
    format_time,
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
from incident_platform.reconciliation import KubernetesStateGraphReconciler
from incident_platform.repository import InMemoryIncidentRepository
from incident_platform.resolution import (
    EntityResolutionRequest,
    ResolvedIncidentLocalizationService,
    ServiceToEntityResolver,
)
from incident_platform.stategraph import (
    EntityIdentity,
    StateGraphReconciliationScope,
    stable_graph_id,
    state_content_hash,
)
from incident_platform.stategraph_observations import (
    InMemoryStateGraphObservationRepository,
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


def _synthetic_entity(
    *,
    cluster_id: str,
    namespace: str,
    name: str,
    observed_at: str,
    evidence_id: str,
) -> dict:
    identity = EntityIdentity.logical_service(
        cluster_id=cluster_id,
        namespace=namespace,
        service_name=name,
    )
    return {
        "record_type": "entity",
        "entity_id": identity.entity_id,
        "identity": identity.to_contract(),
        "entity_type": "Service",
        "domain": "web-service",
        "name": name,
        "scope": {"cluster_id": cluster_id, "namespace": namespace},
        "external_ref": f"service://{cluster_id}/{namespace}/{name}",
        "exists": True,
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "evidence_ids": [evidence_id],
    }


def _synthetic_snapshot(entity_id: str, observed_at: str, evidence_id: str) -> dict:
    state = {"exists": True, "facts": {"health": "ready"}}
    state_hash = state_content_hash(state)
    return {
        "record_type": "snapshot_interval",
        "snapshot_id": stable_graph_id(
            "snap",
            {
                "entity_id": entity_id,
                "valid_from": observed_at,
                "state_hash": state_hash,
            },
        ),
        "entity_id": entity_id,
        "observed_at": observed_at,
        "valid_from": observed_at,
        "valid_to": None,
        "state_hash": state_hash,
        "state": state,
        "evidence_ids": [evidence_id],
    }


def synthetic_reconciliation_smoke(
    repository,
    driver,
    *,
    database: str,
    now: datetime,
) -> dict:
    """Verify close/reopen behavior without changing the target workload."""

    suffix = uuid4().hex[:12]
    cluster_id = f"stategraph-reconcile-smoke-{suffix}"
    namespace = "stategraph-reconcile-smoke"
    source_name = f"source-{suffix}"
    destination_name = f"destination-{suffix}"
    projector = "stategraph-reconciliation-smoke"
    evidence_ids = (
        f"ev-stategraph-smoke-{suffix}-a",
        f"ev-stategraph-smoke-{suffix}-b",
        f"ev-stategraph-smoke-{suffix}-c",
    )
    scope = StateGraphReconciliationScope(
        cluster_id=cluster_id,
        namespace=namespace,
        resource_names=(source_name, destination_name),
        resource_name_prefixes=(f"{source_name}-", f"{destination_name}-"),
        projector=projector,
        managed_entity_types=("Service",),
        managed_relation_types=("CALLS",),
    )

    def cycle(at: datetime, evidence_id: str, *, include_destination: bool) -> tuple:
        observed_at = format_time(at)
        source = _synthetic_entity(
            cluster_id=cluster_id,
            namespace=namespace,
            name=source_name,
            observed_at=observed_at,
            evidence_id=evidence_id,
        )
        records = [
            source,
            _synthetic_snapshot(source["entity_id"], observed_at, evidence_id),
        ]
        if not include_destination:
            return tuple(records)
        destination = _synthetic_entity(
            cluster_id=cluster_id,
            namespace=namespace,
            name=destination_name,
            observed_at=observed_at,
            evidence_id=evidence_id,
        )
        relation_identity = {
            "source_entity_id": source["entity_id"],
            "relation_type": "CALLS",
            "destination_entity_id": destination["entity_id"],
            "reference_key": "synthetic-api",
            "projector": projector,
        }
        relation_key = stable_graph_id("relkey", relation_identity)
        records.extend(
            [
                destination,
                _synthetic_snapshot(
                    destination["entity_id"], observed_at, evidence_id
                ),
                {
                    "record_type": "relation_interval",
                    "relation_id": stable_graph_id(
                        "rel",
                        {
                            "relation_key": relation_key,
                            "valid_from": observed_at,
                        },
                    ),
                    "relation_key": relation_key,
                    **relation_identity,
                    "observed_at": observed_at,
                    "valid_from": observed_at,
                    "valid_to": None,
                    "evidence_ids": [evidence_id],
                },
            ]
        )
        return tuple(records)

    first_at = now
    second_at = now + timedelta(seconds=1)
    third_at = now + timedelta(seconds=2)
    try:
        repository.reconcile_projection(
            cycle(first_at, evidence_ids[0], include_destination=True),
            scope=scope,
            observed_at=first_at,
        )
        disappeared = repository.reconcile_projection(
            cycle(second_at, evidence_ids[1], include_destination=False),
            scope=scope,
            observed_at=second_at,
        )
        reappeared = repository.reconcile_projection(
            cycle(third_at, evidence_ids[2], include_destination=True),
            scope=scope,
            observed_at=third_at,
        )
        with driver.session(database=database) as session:
            row = session.run(
                """
                MATCH (source:StateGraphEntity)-[relation:STATEGRAPH_RELATION]->
                      (destination:StateGraphEntity)
                WHERE source.cluster_id = $cluster_id
                  AND destination.cluster_id = $cluster_id
                WITH source, destination, collect(relation) AS relations
                MATCH (destination)-[:HAS_SNAPSHOT]->(snapshot:StateGraphSnapshot)
                RETURN size(relations) AS relation_intervals,
                       count(snapshot) AS snapshot_intervals,
                       size([relation IN relations
                             WHERE relation.valid_to IS NULL]) AS active_relations
                """,
                cluster_id=cluster_id,
            ).single()
        result = {
            "disappeared_entities": disappeared.retired_entities,
            "closed_snapshots": disappeared.closed_snapshot_intervals,
            "closed_relations": disappeared.closed_relation_intervals,
            "reopened_current_entities": reappeared.current_entities,
            "relation_intervals": int(row["relation_intervals"]),
            "snapshot_intervals": int(row["snapshot_intervals"]),
            "active_relations": int(row["active_relations"]),
        }
        if result != {
            "disappeared_entities": 1,
            "closed_snapshots": 1,
            "closed_relations": 1,
            "reopened_current_entities": 2,
            "relation_intervals": 2,
            "snapshot_intervals": 2,
            "active_relations": 1,
        }:
            raise RuntimeError("synthetic StateGraph reconciliation contract failed")
        return result
    finally:
        with driver.session(database=database) as session:
            session.run(
                """
                MATCH (entity:StateGraphEntity {cluster_id: $cluster_id})
                OPTIONAL MATCH (entity)-[:HAS_SNAPSHOT|HAS_EVENT]->(artifact)
                WITH collect(DISTINCT artifact) + collect(DISTINCT entity) AS nodes
                UNWIND nodes AS node
                DETACH DELETE node
                """,
                cluster_id=cluster_id,
            ).consume()


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
    inventory_provider = KubernetesInventoryProvider(
        kubernetes_client,
        cluster_id=cluster_id,
        page_size=100,
        max_raw_resources=500,
    )
    projector = KubernetesEvidenceProjector()

    _stage("neo4j_connectivity")
    driver = create_neo4j_driver(neo4j_uri, neo4j_username, neo4j_password)
    try:
        driver.verify_connectivity()
        _stage("neo4j_schema")
        schema_objects = apply_neo4j_schema(driver, database=neo4j_database)
        repository = Neo4jStateGraphRepository(driver, database=neo4j_database)
        reconciler = KubernetesStateGraphReconciler(
            inventory_provider,
            repository,
            InMemoryStateGraphObservationRepository(),
            cluster_id=cluster_id,
            projector=projector,
        )
        _stage("stategraph_reconciliation")
        first_reconciliation = reconciler.reconcile(
            collection_request,
            collected_at=now,
        )
        evidence = first_reconciliation.evidence
        source_evidence = next(
            item
            for item in evidence
            if item["subject"]["kind"] == "Service"
            and item["subject"]["name"] == seed_service
        )
        first_counts = graph_counts(
            driver,
            database=neo4j_database,
            cluster_id=cluster_id,
            namespace=namespace,
        )
        second_reconciliation = reconciler.reconcile(
            collection_request,
            collected_at=now,
        )
        second_counts = graph_counts(
            driver,
            database=neo4j_database,
            cluster_id=cluster_id,
            namespace=namespace,
        )
        if first_counts != second_counts:
            raise RuntimeError("repeated StateGraph ingestion created duplicate records")
        if any(
            (
                second_reconciliation.result.retired_entities,
                second_reconciliation.result.closed_snapshot_intervals,
                second_reconciliation.result.closed_relation_intervals,
            )
        ):
            raise RuntimeError(
                "repeated complete StateGraph reconciliation was not idempotent"
            )

        _stage("synthetic_reconciliation")
        reconciliation_contract = synthetic_reconciliation_smoke(
            repository,
            driver,
            database=neo4j_database,
            now=now,
        )

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
            "projected_record_count": first_reconciliation.projected_record_count,
            "idempotent_reingest": first_counts == second_counts,
            "reconciliation": {
                "current_entities": first_reconciliation.result.current_entities,
                "current_relations": first_reconciliation.result.current_relations,
                "retired_entities": first_reconciliation.result.retired_entities,
                "closed_snapshot_intervals": (
                    first_reconciliation.result.closed_snapshot_intervals
                ),
                "closed_relation_intervals": (
                    first_reconciliation.result.closed_relation_intervals
                ),
            },
            "reconciliation_contract": reconciliation_contract,
            "observation_cycle": {
                "status": first_reconciliation.cycle.status,
                "evidence_count": len(first_reconciliation.cycle.evidence_ids),
            },
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
