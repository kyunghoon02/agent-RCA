"""Scoped Prometheus range-query adapter producing metric-summary Evidence."""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import urlencode, urlsplit, urlunsplit

from ..errors import PermanentProviderError, RetryableProviderError
from ..evidence import CollectionRequest, EvidenceDraft, ProviderBatch
from .http import BoundedJSONTransport


_LABEL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _format_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _parse_timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise PermanentProviderError("Prometheus request timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise PermanentProviderError("Prometheus request timestamp has no timezone")
    return parsed.timestamp()


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Prometheus base_url must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Prometheus base_url must not contain credentials or query data")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class PrometheusQuerySpec:
    """Allowlisted PromQL template with one mandatory scoped selector slot."""

    query_id: str
    expression_template: str
    namespace_label: str
    resource_label: str
    subject_kind: str = "Service"
    subject_api_version: str = "v1"
    uid_label: Optional[str] = None
    step_seconds: int = 30
    max_samples: int = 10_000
    peak_fact: Optional[str] = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{2,63}", self.query_id):
            raise ValueError("invalid Prometheus query_id")
        if self.expression_template.count("{scope}") != 1:
            raise ValueError("PromQL template must contain exactly one {scope} slot")
        for label in (
            self.namespace_label,
            self.resource_label,
            self.uid_label,
        ):
            if label is not None and not _LABEL_NAME.fullmatch(label):
                raise ValueError(f"invalid Prometheus label name: {label}")
        if self.step_seconds <= 0 or self.max_samples <= 0:
            raise ValueError("Prometheus query limits must be positive")
        if self.peak_fact is not None and not re.fullmatch(
            r"[a-z][a-z0-9_]{1,63}", self.peak_fact
        ):
            raise ValueError("invalid peak_fact")

    def scoped_expression(self, namespace: str, resource_names: Sequence[str]) -> str:
        resource_pattern = "^(?:" + "|".join(
            re.escape(name) for name in resource_names
        ) + ")$"
        scope = ",".join(
            (
                f"{self.namespace_label}={json.dumps(namespace)}",
                f"{self.resource_label}=~{json.dumps(resource_pattern)}",
            )
        )
        return self.expression_template.replace("{scope}", scope)


@dataclass(frozen=True)
class PrometheusRangeResult:
    series: Tuple[Mapping[str, Any], ...]
    warnings: Tuple[str, ...] = tuple()


class PrometheusRangeClient(Protocol):
    def query_range(
        self,
        expression: str,
        *,
        start: str,
        end: str,
        step_seconds: int,
        timeout_seconds: float,
    ) -> PrometheusRangeResult:
        ...


class PrometheusHTTPAPI:
    """Minimal read-only client for Prometheus `/api/v1/query_range`."""

    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: Optional[str] = None,
        transport: Optional[BoundedJSONTransport] = None,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        if bearer_token is not None and not bearer_token.strip():
            raise ValueError("Prometheus bearer_token must not be empty")
        self._headers = (
            {"Authorization": f"Bearer {bearer_token}"}
            if bearer_token is not None
            else {}
        )
        self._transport = transport or BoundedJSONTransport()

    def query_range(
        self,
        expression: str,
        *,
        start: str,
        end: str,
        step_seconds: int,
        timeout_seconds: float,
    ) -> PrometheusRangeResult:
        parameters = urlencode(
            {
                "query": expression,
                "start": start,
                "end": end,
                "step": f"{step_seconds}s",
                "timeout": f"{max(1, math.ceil(timeout_seconds))}s",
            }
        )
        payload = self._transport.get_json(
            f"{self._base_url}/api/v1/query_range?{parameters}",
            timeout_seconds=timeout_seconds,
            headers=self._headers,
        )
        if payload.get("status") != "success":
            error_type = payload.get("errorType", "unknown")
            if error_type in {"timeout", "canceled"}:
                raise RetryableProviderError(
                    f"Prometheus query failed: {error_type}"
                )
            raise PermanentProviderError(
                f"Prometheus query failed: {error_type}"
            )
        data = payload.get("data")
        if not isinstance(data, Mapping) or data.get("resultType") != "matrix":
            raise PermanentProviderError(
                "Prometheus range query did not return a matrix"
            )
        result = data.get("result")
        if not isinstance(result, list) or not all(
            isinstance(item, Mapping) for item in result
        ):
            raise PermanentProviderError("Prometheus matrix result is malformed")
        warnings = payload.get("warnings", [])
        if not isinstance(warnings, list) or not all(
            isinstance(item, str) for item in warnings
        ):
            warnings = ["Prometheus returned malformed warnings metadata"]
        return PrometheusRangeResult(tuple(result), tuple(warnings))


class PrometheusMetricProvider:
    """Execute allowlisted scoped queries and aggregate samples per resource."""

    def __init__(
        self,
        client: PrometheusRangeClient,
        query_specs: Sequence[PrometheusQuerySpec],
        *,
        cluster_id: Optional[str] = None,
    ) -> None:
        if not query_specs:
            raise ValueError("at least one PrometheusQuerySpec is required")
        query_ids = [spec.query_id for spec in query_specs]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("Prometheus query_id values must be unique")
        if cluster_id is not None and not cluster_id.strip():
            raise ValueError("Prometheus cluster_id must not be empty")
        self._client = client
        self._query_specs = tuple(query_specs)
        self._cluster_id = cluster_id

    def collect(self, request: CollectionRequest) -> ProviderBatch:
        deadline = time.monotonic() + request.timeout_seconds
        drafts = []
        partial_reasons = []
        for spec in self._query_specs:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RetryableProviderError("Prometheus collection deadline exhausted")
            expression = spec.scoped_expression(
                request.scope.namespace, request.scope.resource_names
            )
            result = self._client.query_range(
                expression,
                start=request.window.start,
                end=request.window.end,
                step_seconds=spec.step_seconds,
                timeout_seconds=remaining,
            )
            if result.warnings:
                partial_reasons.append(
                    f"{spec.query_id}: Prometheus returned "
                    f"{len(result.warnings)} warning(s)"
                )
            spec_drafts, truncated = self._summarize(
                spec,
                result.series,
                expression,
                request,
                cluster_id=self._cluster_id,
            )
            drafts.extend(spec_drafts)
            if truncated:
                partial_reasons.append(
                    f"{spec.query_id}: samples exceeded {spec.max_samples} limit"
                )

        if len(drafts) > request.scope.max_items:
            raise PermanentProviderError(
                "Prometheus query set can exceed the Evidence item budget"
            )
        if partial_reasons:
            return ProviderBatch(
                items=tuple(drafts),
                status="PARTIAL",
                error="; ".join(partial_reasons),
            )
        return ProviderBatch(items=tuple(drafts))

    @staticmethod
    def _summarize(
        spec: PrometheusQuerySpec,
        series: Sequence[Mapping[str, Any]],
        expression: str,
        request: CollectionRequest,
        *,
        cluster_id: Optional[str],
    ) -> Tuple[Tuple[EvidenceDraft, ...], bool]:
        grouped: Dict[str, Dict[str, Any]] = {
            name: {"samples": [], "uids": set()}
            for name in request.scope.resource_names
        }
        sample_count = 0
        truncated = False
        window_start = _parse_timestamp(request.window.start)
        window_end = _parse_timestamp(request.window.end)
        for item in series:
            labels = item.get("metric")
            values = item.get("values")
            if not isinstance(labels, Mapping) or not isinstance(values, list):
                raise PermanentProviderError("Prometheus series is malformed")
            resource_name = labels.get(spec.resource_label)
            if not isinstance(resource_name, str):
                raise PermanentProviderError(
                    "Prometheus series has no string resource label"
                )
            if resource_name not in grouped:
                raise PermanentProviderError(
                    "Prometheus returned a series outside resource scope"
                )
            if labels.get(spec.namespace_label) != request.scope.namespace:
                raise PermanentProviderError(
                    "Prometheus returned a series outside namespace scope"
                )
            uid = labels.get(spec.uid_label) if spec.uid_label else None
            if uid is not None and not isinstance(uid, str):
                raise PermanentProviderError("Prometheus UID label is malformed")
            if uid is not None:
                grouped[resource_name]["uids"].add(uid)
            for sample in values:
                if sample_count >= spec.max_samples:
                    truncated = True
                    break
                if not isinstance(sample, list) or len(sample) != 2:
                    raise PermanentProviderError("Prometheus sample is malformed")
                try:
                    timestamp = float(sample[0])
                    value = float(sample[1])
                except (TypeError, ValueError) as error:
                    raise PermanentProviderError(
                        "Prometheus float sample is malformed"
                    ) from error
                if not math.isfinite(timestamp) or not math.isfinite(value):
                    raise PermanentProviderError(
                        "Prometheus sample contains a non-finite value"
                    )
                if timestamp < window_start or timestamp > window_end:
                    raise PermanentProviderError(
                        "Prometheus returned a sample outside the requested time window"
                    )
                grouped[resource_name]["samples"].append((timestamp, value))
                sample_count += 1

        drafts = []
        for resource_name in request.scope.resource_names:
            samples = sorted(grouped[resource_name]["samples"])
            uids = grouped[resource_name]["uids"]
            subject = {
                "api_version": spec.subject_api_version,
                "kind": spec.subject_kind,
                "namespace": request.scope.namespace,
                "name": resource_name,
                "uid": next(iter(uids)) if len(uids) == 1 else None,
                "exists": True,
            }
            if cluster_id is not None:
                subject["cluster_id"] = cluster_id
            if samples:
                values = [sample[1] for sample in samples]
                facts: Dict[str, Any] = {
                    "metric": spec.query_id,
                    "result_status": "HAS_DATA",
                    "sample_count": len(values),
                    "minimum": min(values),
                    "maximum": max(values),
                    "average": sum(values) / len(values),
                    "latest": values[-1],
                }
                if spec.peak_fact is not None:
                    facts[spec.peak_fact] = max(values)
                observed_at = _format_timestamp(samples[-1][0])
                summary = (
                    f"Prometheus {spec.query_id} returned {len(values)} scoped samples "
                    f"for {resource_name}."
                )
                completeness = 0.5 if truncated else 1.0
            else:
                facts = {
                    "metric": spec.query_id,
                    "result_status": (
                        "NO_DATA_WITHIN_LIMIT" if truncated else "NO_DATA"
                    ),
                    "sample_count": 0,
                }
                observed_at = request.window.end
                summary = (
                    f"Prometheus {spec.query_id} returned no scoped samples "
                    f"for {resource_name}."
                )
                completeness = 0.5 if truncated else 1.0
            drafts.append(
                EvidenceDraft(
                    source="prometheus",
                    kind="metric-summary",
                    observed_at=observed_at,
                    subject=subject,
                    summary=summary,
                    facts=facts,
                    provider="prometheus-http-api",
                    query=expression,
                    locator=(
                        f"prometheus://query/{spec.query_id}/"
                        f"{request.scope.namespace}/{resource_name}"
                    ),
                    completeness=completeness,
                    confidence=1.0,
                )
            )
        return tuple(drafts), truncated


class PrometheusWorkloadMetricProvider:
    """Collect Pod-scoped metric summaries under bounded workload prefixes.

    Service metric queries have a fixed one-to-one subject set. Workload metrics do
    not: a Deployment rollout changes Pod names over time. This adapter therefore
    accepts only explicitly rooted resource-name prefixes and requires Prometheus to
    return the Kubernetes Pod UID used by the StateGraph and deterministic rules.
    """

    def __init__(
        self,
        client: PrometheusRangeClient,
        query_specs: Sequence[PrometheusQuerySpec],
        *,
        cluster_id: str,
    ) -> None:
        if not query_specs:
            raise ValueError("at least one PrometheusQuerySpec is required")
        query_ids = [spec.query_id for spec in query_specs]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("Prometheus query_id values must be unique")
        if not cluster_id.strip():
            raise ValueError("Prometheus cluster_id must not be empty")
        for spec in query_specs:
            if spec.subject_kind != "Pod" or not spec.uid_label:
                raise ValueError(
                    "workload metric queries require Pod subjects and a UID label"
                )
        self._client = client
        self._query_specs = tuple(query_specs)
        self._cluster_id = cluster_id

    def collect(self, request: CollectionRequest) -> ProviderBatch:
        if not request.scope.resource_name_prefixes:
            raise PermanentProviderError(
                "workload metric collection requires rooted resource prefixes"
            )
        deadline = time.monotonic() + request.timeout_seconds
        drafts = []
        partial_reasons = []
        for spec in self._query_specs:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RetryableProviderError("Prometheus collection deadline exhausted")
            expression = self._scoped_expression(spec, request)
            result = self._client.query_range(
                expression,
                start=request.window.start,
                end=request.window.end,
                step_seconds=spec.step_seconds,
                timeout_seconds=remaining,
            )
            if result.warnings:
                partial_reasons.append(
                    f"{spec.query_id}: Prometheus returned "
                    f"{len(result.warnings)} warning(s)"
                )
            spec_drafts, truncated = self._summarize(
                spec,
                result.series,
                expression,
                request,
                cluster_id=self._cluster_id,
            )
            drafts.extend(spec_drafts)
            if truncated:
                partial_reasons.append(
                    f"{spec.query_id}: samples exceeded {spec.max_samples} limit"
                )

        if len(drafts) > request.scope.max_items:
            raise PermanentProviderError(
                "Prometheus workload query set exceeds the Evidence item budget"
            )
        if partial_reasons:
            return ProviderBatch(
                items=tuple(drafts),
                status="PARTIAL",
                error="; ".join(partial_reasons),
            )
        return ProviderBatch(items=tuple(drafts))

    @staticmethod
    def _scoped_expression(
        spec: PrometheusQuerySpec,
        request: CollectionRequest,
    ) -> str:
        alternatives = [re.escape(name) for name in request.scope.resource_names]
        alternatives.extend(
            f"{re.escape(prefix)}.*"
            for prefix in request.scope.resource_name_prefixes
        )
        resource_pattern = "^(?:" + "|".join(alternatives) + ")$"
        scope = ",".join(
            (
                f"{spec.namespace_label}={json.dumps(request.scope.namespace)}",
                f"{spec.resource_label}=~{json.dumps(resource_pattern)}",
            )
        )
        return spec.expression_template.replace("{scope}", scope)

    @staticmethod
    def _summarize(
        spec: PrometheusQuerySpec,
        series: Sequence[Mapping[str, Any]],
        expression: str,
        request: CollectionRequest,
        *,
        cluster_id: str,
    ) -> Tuple[Tuple[EvidenceDraft, ...], bool]:
        grouped: Dict[Tuple[str, str], list[Tuple[float, float]]] = {}
        sample_count = 0
        truncated = False
        window_start = _parse_timestamp(request.window.start)
        window_end = _parse_timestamp(request.window.end)
        assert spec.uid_label is not None

        for item in series:
            labels = item.get("metric")
            values = item.get("values")
            if not isinstance(labels, Mapping) or not isinstance(values, list):
                raise PermanentProviderError("Prometheus series is malformed")
            resource_name = labels.get(spec.resource_label)
            if not isinstance(resource_name, str) or not resource_name:
                raise PermanentProviderError(
                    "Prometheus series has no string workload label"
                )
            if not request.scope.contains_resource_name(resource_name):
                raise PermanentProviderError(
                    "Prometheus returned a workload outside resource scope"
                )
            if labels.get(spec.namespace_label) != request.scope.namespace:
                raise PermanentProviderError(
                    "Prometheus returned a series outside namespace scope"
                )
            uid = labels.get(spec.uid_label)
            if not isinstance(uid, str) or not uid:
                raise PermanentProviderError(
                    "Prometheus workload series has no Kubernetes Pod UID"
                )
            samples = grouped.setdefault((resource_name, uid), [])
            for sample in values:
                if sample_count >= spec.max_samples:
                    truncated = True
                    break
                if not isinstance(sample, list) or len(sample) != 2:
                    raise PermanentProviderError("Prometheus sample is malformed")
                try:
                    timestamp = float(sample[0])
                    value = float(sample[1])
                except (TypeError, ValueError) as error:
                    raise PermanentProviderError(
                        "Prometheus float sample is malformed"
                    ) from error
                if not math.isfinite(timestamp) or not math.isfinite(value):
                    raise PermanentProviderError(
                        "Prometheus sample contains a non-finite value"
                    )
                if timestamp < window_start or timestamp > window_end:
                    raise PermanentProviderError(
                        "Prometheus returned a sample outside the requested time window"
                    )
                samples.append((timestamp, value))
                sample_count += 1

        drafts = []
        for (resource_name, uid), samples in sorted(grouped.items()):
            ordered_samples = sorted(samples)
            if not ordered_samples:
                continue
            values = [sample[1] for sample in ordered_samples]
            facts: Dict[str, Any] = {
                "metric": spec.query_id,
                "result_status": "HAS_DATA",
                "sample_count": len(values),
                "minimum": min(values),
                "maximum": max(values),
                "average": sum(values) / len(values),
                "latest": values[-1],
            }
            if spec.peak_fact is not None:
                facts[spec.peak_fact] = max(values)
            drafts.append(
                EvidenceDraft(
                    source="prometheus",
                    kind="metric-summary",
                    observed_at=_format_timestamp(ordered_samples[-1][0]),
                    subject={
                        "api_version": spec.subject_api_version,
                        "kind": spec.subject_kind,
                        "namespace": request.scope.namespace,
                        "name": resource_name,
                        "uid": uid,
                        "cluster_id": cluster_id,
                        "exists": True,
                    },
                    summary=(
                        f"Prometheus {spec.query_id} returned {len(values)} "
                        f"scoped samples for Pod {resource_name}."
                    ),
                    facts=facts,
                    provider="prometheus-http-api",
                    query=expression,
                    locator=(
                        f"prometheus://query/{spec.query_id}/"
                        f"{request.scope.namespace}/Pod/{resource_name}/{uid}"
                    ),
                    completeness=0.5 if truncated else 1.0,
                    confidence=1.0,
                )
            )
        return tuple(drafts), truncated
