"""Read-only Hubble and Prometheus evidence for trusted dependency paths."""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from ..contracts import validate_contract
from ..errors import PermanentProviderError, RetryableProviderError
from ..evidence import CollectionRequest, EvidenceDraft, ProviderBatch, parse_time
from ..network_policy import NETWORK_PATH_FEATURE
from .hubble import HubbleFlowClient, HubbleNetworkFlowProvider
from .prometheus import PrometheusRangeClient


def _observation_time(value: datetime) -> str:
    # Preserve sub-second ordering when matching a drop to a counter interval.
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _exact_endpoint(endpoint: object, service: str, namespace: str) -> bool:
    if not isinstance(endpoint, Mapping):
        return False
    workloads = endpoint.get("workloads", [])
    return (
        endpoint.get("namespace") == namespace
        and isinstance(endpoint.get("pod_name"), str)
        and endpoint["pod_name"].startswith(service + "-")
        and isinstance(workloads, (list, tuple))
        and any(isinstance(workload, Mapping)
                and workload.get("kind") == "Deployment"
                and workload.get("name") == service for workload in workloads)
    )


@dataclass(frozen=True)
class NetworkDependencyPath:
    path_id: str
    source_service: str
    destination_service: str
    destination_port: int
    protocol: str
    client_operation: str

    def __post_init__(self):
        validate_contract("network-dependency-path.schema.json", asdict(self))
        if self.source_service == self.destination_service:
            raise ValueError("Network dependency cannot be a self edge")


def load_network_paths(path: str) -> tuple[NetworkDependencyPath, ...]:
    """Read a trusted local config; Alert/Agent input never supplies paths."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_contract("network-dependency-config.schema.json", document)
    paths = tuple(NetworkDependencyPath(**item) for item in document["paths"])
    if len({item.path_id for item in paths}) != len(paths):
        raise ValueError("Network path IDs must be unique")
    if len({(item.source_service, item.client_operation) for item in paths}) != len(paths):
        raise ValueError("Caller operation must resolve to exactly one dependency")
    return paths


class NetworkPolicyPathProvider:
    """Bounded pair observations; no arbitrary PromQL or raw flow persistence."""

    def __init__(self, hubble: HubbleFlowClient, prometheus: PrometheusRangeClient,
                 paths: Sequence[NetworkDependencyPath], *, cluster_id: str):
        if not cluster_id or not 1 <= len(paths) <= 8:
            raise ValueError("Network provider requires a cluster and 1..8 paths")
        if len({item.path_id for item in paths}) != len(paths) or len({
            (item.source_service, item.client_operation) for item in paths
        }) != len(paths):
            raise ValueError("Network path identity or caller operation is ambiguous")
        self._hubble, self._prometheus = hubble, prometheus
        self._paths, self._cluster_id = tuple(paths), cluster_id

    def collect(self, request: CollectionRequest) -> ProviderBatch:
        selected = [item for item in self._paths if item.source_service in request.scope.resource_names]
        if 2 * len(selected) > request.scope.max_items:
            raise PermanentProviderError("Network paths exceed the Evidence budget")
        deadline = time.monotonic() + request.timeout_seconds
        items, reasons = [], []
        for path in selected:
            subject = {"cluster_id": self._cluster_id, "api_version": "v1", "kind": "Service",
                       "namespace": request.scope.namespace, "name": path.source_service,
                       "uid": None, "exists": True}
            facts = dict(asdict(path), feature_set=NETWORK_PATH_FEATURE)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RetryableProviderError("Network path deadline exhausted")
            result = self._hubble.observe(namespace=request.scope.namespace,
                pod_prefix=path.source_service, direction="from", start=request.window.start,
                end=request.window.end, limit=100, timeout_seconds=remaining)
            if len(result.flows) > 100:
                raise PermanentProviderError("Network path flow cap exceeded")
            matches = {}
            flow_validator = HubbleNetworkFlowProvider(self._hubble, cluster_id=self._cluster_id)
            for flow in result.flows:
                if not isinstance(flow, Mapping):
                    raise PermanentProviderError("Malformed network path flow")
                flow_validator._validate_scoped_flow(
                    flow, request=request, resource_name=path.source_service, direction="from")
                uid = flow.get("uuid")
                if not isinstance(uid, str) or not uid:
                    raise PermanentProviderError("Network path flow UUID missing")
                if uid in matches and matches[uid] != flow:
                    raise PermanentProviderError("Network path flow UUID conflict")
                matches[uid] = flow
            denied_times = []
            for flow in matches.values():
                l4 = flow.get("l4")
                l4 = l4.get(path.protocol, {}) if isinstance(l4, Mapping) else {}
                if not isinstance(l4, Mapping):
                    raise PermanentProviderError("Malformed network path transport")
                if _exact_endpoint(flow.get("source"), path.source_service, request.scope.namespace) \
                        and _exact_endpoint(flow.get("destination"), path.destination_service, request.scope.namespace) \
                        and l4.get("destination_port") == path.destination_port \
                        and flow.get("verdict") == "DROPPED" \
                        and flow.get("drop_reason_desc") in {"POLICY_DENIED", "POLICY_DENY"}:
                    denied_times.append(parse_time(flow["time"], "denied flow"))
            if result.truncated or result.observation_gaps or not denied_times:
                reasons.append(f"{path.path_id}: Hubble coverage incomplete or denial unobserved")
            items.append(EvidenceDraft(source="hubble", kind="network-flow-summary",
                subject=subject, observed_at=_observation_time(max(denied_times)) if denied_times else request.window.end,
                summary=f"Registered dependency {path.path_id}: {len(denied_times)} observed policy denials.",
                facts=dict(facts, policy_denied_count=len(denied_times),
                           denied_at=[_observation_time(value) for value in sorted(denied_times)],
                           first_denied_at=_observation_time(min(denied_times)) if denied_times else None,
                           last_denied_at=_observation_time(max(denied_times)) if denied_times else None,
                           truncated=result.truncated, observation_gaps=list(result.observation_gaps),
                           retention_status="UNKNOWN"),
                provider="hubble-network-path-provider", query=f"bounded from-Pod query; path={path.path_id}",
                locator=f"hubble://network-path/{path.path_id}", completeness=0.5 if denied_times else 0.0))
            labels = {"namespace": request.scope.namespace, "service_name": path.source_service,
                      "span_name": path.client_operation}
            selector = ",".join(f"{key}={json.dumps(value)}" for key, value in labels.items())
            expression = "sum by (namespace, service_name, span_name) (increase(agent_rca_calls_total{" \
                + selector + ',span_kind="SPAN_KIND_CLIENT",status_code="STATUS_CODE_ERROR"}[2m]))'
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RetryableProviderError("Network path deadline exhausted")
            metric = self._prometheus.query_range(expression, start=request.window.start,
                end=request.window.end, step_seconds=15, timeout_seconds=remaining)
            if len(metric.series) > 1:
                raise PermanentProviderError("Ambiguous caller RPC series")
            samples = []
            count = 0
            previous = None
            for series in metric.series:
                if not isinstance(series, Mapping):
                    raise PermanentProviderError("Malformed caller RPC series")
                if series.get("metric") != labels:
                    raise PermanentProviderError("Caller RPC labels outside registered scope")
                values = series.get("values")
                if not isinstance(values, list) or len(values) > 1000:
                    raise PermanentProviderError("Caller RPC sample cap or shape invalid")
                for value in values:
                    if not isinstance(value, (list, tuple)) or len(value) != 2:
                        raise PermanentProviderError("Malformed caller RPC sample")
                    if any(isinstance(part, bool) for part in value):
                        raise PermanentProviderError("Boolean caller RPC sample")
                    try:
                        timestamp, errors = float(value[0]), float(value[1])
                    except (TypeError, ValueError) as error:
                        raise PermanentProviderError("Malformed caller RPC sample") from error
                    if not math.isfinite(timestamp) or not math.isfinite(errors) or errors < 0 \
                            or not parse_time(request.window.start, "start").timestamp() <= timestamp <= parse_time(request.window.end, "end").timestamp() \
                            or (previous is not None and timestamp <= previous):
                        raise PermanentProviderError("Caller RPC sample out of bounds")
                    previous = timestamp
                    count += 1
                    if errors > 0:
                        samples.append({"at": _observation_time(datetime.fromtimestamp(timestamp, timezone.utc)), "errors": errors})
            if metric.warnings or not count:
                reasons.append(f"{path.path_id}: caller RPC telemetry incomplete")
            items.append(EvidenceDraft(source="prometheus", kind="metric-summary", subject=subject,
                observed_at=request.window.end, summary=f"Registered caller RPC {path.client_operation}: error observations.",
                facts=dict(facts, sample_count=count, error_samples=samples, telemetry_warnings=bool(metric.warnings)),
                provider="prometheus-network-path-provider", query=expression,
                locator=f"prometheus://network-path/{path.path_id}", completeness=0.5 if metric.warnings else 1.0 if count else 0.0))
        return ProviderBatch(tuple(items), status="PARTIAL" if reasons else "SUCCEEDED",
                             error="; ".join(reasons) if reasons else None)


class NetworkEvidenceCollector:
    """Share one deadline/item cap with the existing Hubble summary collector."""

    def __init__(self, summaries, paths):
        self._summaries, self._paths = summaries, paths

    def collect(self, request):
        deadline = time.monotonic() + request.timeout_seconds
        initial = self._summaries.collect(request)
        remaining = deadline - time.monotonic()
        cap = request.scope.max_items - len(initial.items)
        if remaining <= 0 or cap <= 0:
            return ProviderBatch(initial.items, "PARTIAL", "Network path budget exhausted")
        try:
            extra = self._paths.collect(replace(request, timeout_seconds=remaining,
                scope=replace(request.scope, max_items=cap)))
        except (PermanentProviderError, RetryableProviderError):
            return ProviderBatch(initial.items, "PARTIAL", "Network path collection failed; summaries retained")
        errors = [batch.error for batch in (initial, extra) if batch.status == "PARTIAL"]
        return ProviderBatch(initial.items + extra.items, "PARTIAL" if errors else "SUCCEEDED",
                             "; ".join(errors) if errors else None)
