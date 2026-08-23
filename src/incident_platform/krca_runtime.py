"""Strict runtime wiring for versioned KRCA dependency and PromQL profiles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, Tuple

import yaml

from .contracts import validate_contract
from .errors import ContractViolation
from .krca import APIRef
from .providers.krca_metrics import (
    APIDependencySpec,
    PrometheusAPIFeatureProvider,
    PrometheusAPIFeatureQuerySpec,
)
from .providers.prometheus import PrometheusRangeClient


@dataclass(frozen=True)
class KRCARuntimeCollectionPolicy:
    window_seconds: int
    timeout_seconds: float
    max_evidence_items: int
    max_edges: int
    max_queries: int


@dataclass(frozen=True)
class KRCARuntimeProfile:
    profile_id: str
    alerting_api: APIRef
    resource_names: Tuple[str, ...]
    dependencies: Tuple[APIDependencySpec, ...]


@dataclass(frozen=True)
class KRCARuntimeConfig:
    cluster_id: str
    namespace: str
    query_spec: PrometheusAPIFeatureQuerySpec
    collection: KRCARuntimeCollectionPolicy
    profiles: Tuple[KRCARuntimeProfile, ...]

    def profile(self, profile_id: str) -> KRCARuntimeProfile:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        raise ContractViolation(f"unknown KRCA runtime profile: {profile_id}")

    def provider(
        self,
        client: PrometheusRangeClient,
        profile: KRCARuntimeProfile,
    ) -> PrometheusAPIFeatureProvider:
        return PrometheusAPIFeatureProvider(
            client,
            profile.dependencies,
            self.query_spec,
            max_edges=self.collection.max_edges,
            max_queries=self.collection.max_queries,
        )


def load_krca_runtime_config(path: Path) -> KRCARuntimeConfig:
    """Load schema-valid profiles without accepting endpoint or credential data."""

    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    validate_contract("krca-runtime-config.schema.json", raw)
    if not isinstance(raw, Mapping):
        raise ContractViolation("KRCA runtime configuration must be an object")

    prometheus = _mapping(raw["prometheus"], "prometheus")
    labels = _mapping(prometheus["labels"], "prometheus.labels")
    queries = _mapping(prometheus["queries"], "prometheus.queries")
    query_spec = PrometheusAPIFeatureQuerySpec(
        failure_rate_template=str(queries["failure_rate"]),
        latency_template=str(queries["latency"]),
        qps_template=str(queries["qps"]),
        latency_baseline_template=str(queries["latency_baseline"]),
        namespace_label=str(labels["namespace"]),
        service_label=str(labels["service"]),
        operation_label=str(labels["operation"]),
        step_seconds=int(prometheus["step_seconds"]),
        max_samples_per_query=int(prometheus["max_samples_per_query"]),
        minimum_aligned_samples=int(prometheus["minimum_aligned_samples"]),
        maximum_time_lag=int(prometheus["maximum_time_lag"]),
    )

    collection_raw = _mapping(raw["collection"], "collection")
    collection = KRCARuntimeCollectionPolicy(
        window_seconds=int(collection_raw["window_seconds"]),
        timeout_seconds=float(collection_raw["timeout_seconds"]),
        max_evidence_items=int(collection_raw["max_evidence_items"]),
        max_edges=int(collection_raw["max_edges"]),
        max_queries=int(collection_raw["max_queries"]),
    )
    profiles = tuple(_profile(item) for item in raw["profiles"])
    profile_ids = [item.profile_id for item in profiles]
    if len(profile_ids) != len(set(profile_ids)):
        raise ContractViolation("KRCA runtime profile_id values must be unique")
    for profile in profiles:
        _validate_profile(profile, collection)

    return KRCARuntimeConfig(
        cluster_id=str(raw["cluster_id"]),
        namespace=str(raw["namespace"]),
        query_spec=query_spec,
        collection=collection,
        profiles=profiles,
    )


def _profile(raw: Any) -> KRCARuntimeProfile:
    value = _mapping(raw, "profiles[]")
    return KRCARuntimeProfile(
        profile_id=str(value["profile_id"]),
        alerting_api=_api_ref(value["alerting_api"], "alerting_api"),
        resource_names=tuple(str(item) for item in value["resource_names"]),
        dependencies=tuple(
            APIDependencySpec(
                edge_id=str(_mapping(item, "dependencies[]")["edge_id"]),
                parent=_api_ref(
                    _mapping(item, "dependencies[]")["parent"],
                    "dependencies[].parent",
                ),
                child=_api_ref(
                    _mapping(item, "dependencies[]")["child"],
                    "dependencies[].child",
                ),
            )
            for item in value["dependencies"]
        ),
    )


def _api_ref(raw: Any, field: str) -> APIRef:
    value = _mapping(raw, field)
    return APIRef(str(value["service"]), str(value["operation"]))


def _mapping(raw: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ContractViolation(f"{field} must be an object")
    return raw


def _validate_profile(
    profile: KRCARuntimeProfile,
    collection: KRCARuntimeCollectionPolicy,
) -> None:
    allowed_services = set(profile.resource_names)
    apis = {
        api
        for edge in profile.dependencies
        for api in (edge.parent, edge.child)
    }
    if profile.alerting_api not in apis:
        raise ContractViolation(
            f"KRCA profile {profile.profile_id} alerting API is not in its dependency graph"
        )
    if any(api.service not in allowed_services for api in apis):
        raise ContractViolation(
            f"KRCA profile {profile.profile_id} dependency escapes resource scope"
        )
    if len(profile.dependencies) > collection.max_edges:
        raise ContractViolation(
            f"KRCA profile {profile.profile_id} exceeds the edge budget"
        )
    predicted_queries = PrometheusAPIFeatureProvider._predicted_query_count(
        profile.dependencies
    )
    if predicted_queries > collection.max_queries:
        raise ContractViolation(
            f"KRCA profile {profile.profile_id} exceeds the query budget"
        )
    if len(profile.dependencies) > collection.max_evidence_items:
        raise ContractViolation(
            f"KRCA profile {profile.profile_id} exceeds the Evidence item budget"
        )

    reachable = {profile.alerting_api}
    pending: Sequence[APIDependencySpec] = profile.dependencies
    while pending:
        next_pending = tuple(edge for edge in pending if edge.parent not in reachable)
        for edge in pending:
            if edge.parent in reachable:
                reachable.add(edge.child)
        if len(next_pending) == len(pending):
            break
        pending = next_pending
    if pending:
        raise ContractViolation(
            f"KRCA profile {profile.profile_id} contains a disconnected dependency edge"
        )
