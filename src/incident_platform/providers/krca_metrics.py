"""Bounded Prometheus feature extraction for KRCA API dependency edges."""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from ..errors import PermanentProviderError, RetryableProviderError
from ..evidence import CollectionRequest, EvidenceDraft, ProviderBatch
from ..krca import APIRef
from .prometheus import (
    PrometheusRangeClient,
    PrometheusRangeResult,
    _format_timestamp,
    _parse_timestamp,
)


_LABEL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class APIDependencySpec:
    """One allowlisted directed API edge from a versioned dependency source."""

    edge_id: str
    parent: APIRef
    child: APIRef

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{2,63}", self.edge_id):
            raise ValueError("invalid API dependency edge_id")
        if self.parent == self.child:
            raise ValueError("API dependency cannot be a self edge")


@dataclass(frozen=True)
class PrometheusAPIFeatureQuerySpec:
    """Allowlisted PromQL templates and hard limits for API feature extraction."""

    failure_rate_template: str
    latency_template: str
    qps_template: str
    latency_baseline_template: str
    namespace_label: str = "namespace"
    service_label: str = "service"
    operation_label: str = "operation"
    step_seconds: int = 30
    max_samples_per_query: int = 2_000
    minimum_aligned_samples: int = 4
    maximum_time_lag: int = 5
    epsilon: float = 1e-12

    def __post_init__(self) -> None:
        for template in (
            self.failure_rate_template,
            self.latency_template,
            self.qps_template,
            self.latency_baseline_template,
        ):
            if template.count("{scope}") != 1:
                raise ValueError(
                    "each KRCA PromQL template must contain exactly one {scope} slot"
                )
        for label in (
            self.namespace_label,
            self.service_label,
            self.operation_label,
        ):
            if not _LABEL_NAME.fullmatch(label):
                raise ValueError(f"invalid Prometheus label name: {label}")
        if self.step_seconds <= 0 or self.max_samples_per_query <= 0:
            raise ValueError("KRCA Prometheus query limits must be positive")
        if self.minimum_aligned_samples < 3:
            raise ValueError("minimum_aligned_samples must be at least 3")
        if self.maximum_time_lag < 0:
            raise ValueError("maximum_time_lag cannot be negative")
        if not math.isfinite(self.epsilon) or self.epsilon <= 0:
            raise ValueError("epsilon must be finite and positive")

    def scoped_expression(self, template: str, namespace: str, api: APIRef) -> str:
        scope = ",".join(
            (
                f"{self.namespace_label}={json.dumps(namespace)}",
                f"{self.service_label}={json.dumps(api.service)}",
                f"{self.operation_label}={json.dumps(api.operation)}",
            )
        )
        return template.replace("{scope}", scope)

    def template_for(self, metric: str) -> str:
        try:
            return {
                "failure_rate": self.failure_rate_template,
                "latency": self.latency_template,
                "qps": self.qps_template,
                "latency_baseline": self.latency_baseline_template,
            }[metric]
        except KeyError as error:
            raise ValueError(f"unsupported KRCA metric: {metric}") from error


@dataclass(frozen=True)
class _SeriesObservation:
    samples: Mapping[float, float]
    expression: str
    warnings: Tuple[str, ...]
    truncated: bool


class PrometheusAPIFeatureProvider:
    """Compute Evidence-backed KRCA features without retaining raw samples."""

    feature_set = "krca-api-edge-v1"

    def __init__(
        self,
        client: PrometheusRangeClient,
        dependencies: Sequence[APIDependencySpec],
        query_spec: PrometheusAPIFeatureQuerySpec,
        *,
        max_edges: int = 100,
        max_queries: int = 400,
    ) -> None:
        if not dependencies:
            raise ValueError("at least one APIDependencySpec is required")
        if max_edges <= 0 or max_queries <= 0:
            raise ValueError("KRCA provider limits must be positive")
        identities = [(item.parent, item.child) for item in dependencies]
        edge_ids = [item.edge_id for item in dependencies]
        if len(identities) != len(set(identities)):
            raise ValueError("API dependency edges must be unique")
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("API dependency edge_id values must be unique")
        self._client = client
        self._dependencies = tuple(dependencies)
        self._query_spec = query_spec
        self._max_edges = max_edges
        self._max_queries = max_queries

    def collect(self, request: CollectionRequest) -> ProviderBatch:
        allowed_services = set(request.scope.resource_names)
        selected = tuple(
            edge
            for edge in self._dependencies
            if edge.parent.service in allowed_services
            and edge.child.service in allowed_services
        )
        if not selected:
            return ProviderBatch(
                items=(self._no_dependency_draft(request),),
            )
        if len(selected) > self._max_edges:
            raise PermanentProviderError(
                "configured API dependency set exceeds the edge budget"
            )
        predicted_queries = self._predicted_query_count(selected)
        if predicted_queries > self._max_queries:
            raise PermanentProviderError(
                "configured API dependency set exceeds the query budget"
            )
        if len(selected) > request.scope.max_items:
            raise PermanentProviderError(
                "API edge feature set exceeds the Evidence item budget"
            )

        deadline = time.monotonic() + request.timeout_seconds
        cache: Dict[Tuple[str, APIRef], _SeriesObservation] = {}
        drafts = []
        partial_reasons = []
        for edge in selected:
            required = {
                "parent_failure": self._query(
                    "failure_rate", edge.parent, request, deadline, cache
                ),
                "child_failure": self._query(
                    "failure_rate", edge.child, request, deadline, cache
                ),
                "parent_latency": self._query(
                    "latency", edge.parent, request, deadline, cache
                ),
                "child_latency": self._query(
                    "latency", edge.child, request, deadline, cache
                ),
                "parent_qps": self._query("qps", edge.parent, request, deadline, cache),
                "child_qps": self._query("qps", edge.child, request, deadline, cache),
                "child_baseline": self._query(
                    "latency_baseline", edge.child, request, deadline, cache
                ),
            }
            draft, edge_partial = self._edge_draft(edge, required, request)
            drafts.append(draft)
            partial_reasons.extend(edge_partial)

        for observation in cache.values():
            if observation.warnings:
                partial_reasons.append(
                    f"Prometheus returned {len(observation.warnings)} warning(s)"
                )
            if observation.truncated:
                partial_reasons.append("Prometheus samples exceeded per-query limit")
        unique_reasons = tuple(dict.fromkeys(partial_reasons))
        if unique_reasons:
            return ProviderBatch(
                items=tuple(drafts),
                status="PARTIAL",
                error="; ".join(unique_reasons),
            )
        return ProviderBatch(items=tuple(drafts))

    @staticmethod
    def _predicted_query_count(edges: Sequence[APIDependencySpec]) -> int:
        apis = {edge.parent for edge in edges} | {edge.child for edge in edges}
        baseline_apis = {edge.child for edge in edges}
        return len(apis) * 3 + len(baseline_apis)

    def _query(
        self,
        metric: str,
        api: APIRef,
        request: CollectionRequest,
        deadline: float,
        cache: Dict[Tuple[str, APIRef], _SeriesObservation],
    ) -> _SeriesObservation:
        key = (metric, api)
        existing = cache.get(key)
        if existing is not None:
            return existing
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RetryableProviderError("KRCA feature collection deadline exhausted")
        template = self._query_spec.template_for(metric)
        expression = self._query_spec.scoped_expression(
            template, request.scope.namespace, api
        )
        result = self._client.query_range(
            expression,
            start=request.window.start,
            end=request.window.end,
            step_seconds=self._query_spec.step_seconds,
            timeout_seconds=remaining,
        )
        observation = self._read_series(result, expression, metric, api, request)
        cache[key] = observation
        return observation

    def _read_series(
        self,
        result: PrometheusRangeResult,
        expression: str,
        metric: str,
        api: APIRef,
        request: CollectionRequest,
    ) -> _SeriesObservation:
        if len(result.series) > 1:
            raise PermanentProviderError(
                "KRCA Prometheus query returned ambiguous duplicate API series"
            )
        if not result.series:
            return _SeriesObservation({}, expression, result.warnings, False)
        item = result.series[0]
        labels = item.get("metric")
        values = item.get("values")
        if not isinstance(labels, Mapping) or not isinstance(values, list):
            raise PermanentProviderError("KRCA Prometheus series is malformed")
        expected_labels = {
            self._query_spec.namespace_label: request.scope.namespace,
            self._query_spec.service_label: api.service,
            self._query_spec.operation_label: api.operation,
        }
        if any(labels.get(key) != value for key, value in expected_labels.items()):
            raise PermanentProviderError(
                "Prometheus returned an API series outside the requested scope"
            )
        start = _timestamp(request.window.start)
        end = _timestamp(request.window.end)
        samples: Dict[float, float] = {}
        truncated = len(values) > self._query_spec.max_samples_per_query
        for raw_sample in values[: self._query_spec.max_samples_per_query]:
            if not isinstance(raw_sample, list) or len(raw_sample) != 2:
                raise PermanentProviderError("KRCA Prometheus sample is malformed")
            try:
                timestamp = float(raw_sample[0])
                value = float(raw_sample[1])
            except (TypeError, ValueError) as error:
                raise PermanentProviderError(
                    "KRCA Prometheus float sample is malformed"
                ) from error
            if not math.isfinite(timestamp) or not math.isfinite(value):
                raise PermanentProviderError(
                    "KRCA Prometheus sample contains a non-finite value"
                )
            if value < 0 or (metric == "failure_rate" and value > 1):
                raise PermanentProviderError(
                    f"KRCA Prometheus {metric} sample is outside its valid range"
                )
            if timestamp < start or timestamp > end:
                raise PermanentProviderError(
                    "Prometheus returned an API sample outside the requested time window"
                )
            if timestamp in samples:
                raise PermanentProviderError(
                    "KRCA Prometheus series contains a duplicate timestamp"
                )
            samples[timestamp] = value
        return _SeriesObservation(samples, expression, result.warnings, truncated)

    def _edge_draft(
        self,
        edge: APIDependencySpec,
        observations: Mapping[str, _SeriesObservation],
        request: CollectionRequest,
    ) -> Tuple[EvidenceDraft, Tuple[str, ...]]:
        expressions = [item.expression for item in observations.values()]
        missing = sorted(
            key for key, item in observations.items() if not item.samples
        )
        truncated = sorted(
            key for key, item in observations.items() if item.truncated
        )
        if missing or truncated:
            reason_codes = [f"MISSING_{key.upper()}" for key in missing]
            reason_codes.extend(f"TRUNCATED_{key.upper()}" for key in truncated)
            facts = self._base_facts(edge)
            facts.update(
                {
                    "result_status": "INSUFFICIENT_DATA",
                    "reason_codes": reason_codes,
                }
            )
            draft = self._draft(
                edge,
                request,
                facts,
                expressions,
                observed_at=request.window.end,
                completeness=0.0,
            )
            return draft, tuple(
                f"{edge.edge_id}: {reason.lower()}" for reason in reason_codes
            )

        timestamps = sorted(
            set.intersection(
                *(set(item.samples) for item in observations.values())
            )
        )
        if len(timestamps) < self._query_spec.minimum_aligned_samples:
            facts = self._base_facts(edge)
            facts.update(
                {
                    "result_status": "INSUFFICIENT_DATA",
                    "reason_codes": ["TOO_FEW_ALIGNED_SAMPLES"],
                    "aligned_sample_count": len(timestamps),
                }
            )
            return (
                self._draft(
                    edge,
                    request,
                    facts,
                    expressions,
                    observed_at=request.window.end,
                    completeness=0.0,
                ),
                (f"{edge.edge_id}: too few aligned samples",),
            )

        values = {
            key: [item.samples[timestamp] for timestamp in timestamps]
            for key, item in observations.items()
        }
        start_index = _dynamic_window_start(values["parent_failure"])
        reason_codes = []
        if len(timestamps) - start_index < self._query_spec.minimum_aligned_samples:
            start_index = 0
            reason_codes.append("DYNAMIC_WINDOW_TOO_SHORT_FALLBACK_FULL")
        timestamps = timestamps[start_index:]
        values = {key: item[start_index:] for key, item in values.items()}

        failure_correlation, failure_p_value, failure_lag, failure_reason = (
            _maximum_lagged_correlation(
                values["parent_failure"],
                values["child_failure"],
                maximum_lag=self._query_spec.maximum_time_lag,
                minimum_samples=self._query_spec.minimum_aligned_samples,
                with_p_value=True,
            )
        )
        latency_correlation, _, latency_lag, latency_reason = (
            _maximum_lagged_correlation(
                values["parent_latency"],
                values["child_latency"],
                maximum_lag=self._query_spec.maximum_time_lag,
                minimum_samples=self._query_spec.minimum_aligned_samples,
                with_p_value=False,
            )
        )
        reason_codes.extend(failure_reason)
        reason_codes.extend(latency_reason)
        anomaly = _latency_anomaly(
            values["child_latency"],
            values["child_baseline"],
            self._query_spec.epsilon,
        )
        fluctuation, fluctuation_reason = _latency_fluctuation_contribution(
            values["parent_latency"],
            values["child_latency"],
            values["parent_qps"],
            values["child_qps"],
            self._query_spec.epsilon,
        )
        reason_codes.extend(fluctuation_reason)
        reason_codes = list(dict.fromkeys(reason_codes))
        facts = self._base_facts(edge)
        facts.update(
            {
                "result_status": "HAS_DATA",
                "failure_rate_correlation": failure_correlation,
                "failure_rate_p_value": failure_p_value,
                "latency_anomaly": anomaly,
                "latency_fluctuation_contribution": fluctuation,
                "latency_correlation": latency_correlation,
                "computation": {
                    "dynamic_window_start": _format_timestamp(timestamps[0]),
                    "dynamic_window_end": _format_timestamp(timestamps[-1]),
                    "aligned_sample_count": len(timestamps),
                    "maximum_time_lag": self._query_spec.maximum_time_lag,
                    "selected_failure_lag": failure_lag,
                    "selected_latency_lag": latency_lag,
                    "component_range": "0_to_1_except_signed_correlations",
                    "reason_codes": reason_codes,
                },
            }
        )
        completeness = 0.8 if reason_codes else 1.0
        return (
            self._draft(
                edge,
                request,
                facts,
                expressions,
                observed_at=_format_timestamp(timestamps[-1]),
                completeness=completeness,
            ),
            tuple(),
        )

    def _base_facts(self, edge: APIDependencySpec) -> Dict[str, Any]:
        return {
            "metric": "krca_api_edge_features",
            "feature_set": self.feature_set,
            "edge_id": edge.edge_id,
            "parent": {
                "service": edge.parent.service,
                "operation": edge.parent.operation,
            },
            "child": {
                "service": edge.child.service,
                "operation": edge.child.operation,
            },
        }

    def _draft(
        self,
        edge: APIDependencySpec,
        request: CollectionRequest,
        facts: Mapping[str, Any],
        expressions: Sequence[str],
        *,
        observed_at: str,
        completeness: float,
    ) -> EvidenceDraft:
        return EvidenceDraft(
            source="prometheus",
            kind="metric-summary",
            observed_at=observed_at,
            subject={
                "api_version": "v1",
                "kind": "Service",
                "namespace": request.scope.namespace,
                "name": edge.parent.service,
                "uid": None,
                "exists": True,
            },
            summary=(
                f"KRCA API edge features were computed for "
                f"{edge.parent.key} -> {edge.child.key}."
            ),
            facts=facts,
            provider="prometheus-krca-api-feature-provider",
            query=" ; ".join(expressions),
            locator=f"prometheus://krca/edge/{edge.edge_id}",
            completeness=completeness,
            confidence=1.0,
        )

    def _no_dependency_draft(self, request: CollectionRequest) -> EvidenceDraft:
        service = request.scope.resource_names[0]
        return EvidenceDraft(
            source="prometheus",
            kind="metric-summary",
            observed_at=request.window.end,
            subject={
                "api_version": "v1",
                "kind": "Service",
                "namespace": request.scope.namespace,
                "name": service,
                "uid": None,
                "exists": True,
            },
            summary="No configured API dependency edge matched the bounded scope.",
            facts={
                "metric": "krca_api_edge_features",
                "feature_set": self.feature_set,
                "result_status": "NO_CONFIGURED_EDGE",
                "reason_codes": ["NO_CONFIGURED_EDGE"],
            },
            provider="prometheus-krca-api-feature-provider",
            query="configured API dependency lookup",
            locator=f"prometheus://krca/scope/{request.scope.namespace}/{service}",
            completeness=0.0,
            confidence=1.0,
        )


def _timestamp(value: str) -> float:
    return _parse_timestamp(value)


def _dynamic_window_start(values: Sequence[float]) -> int:
    previous_sign: Optional[int] = None
    last_change = 0
    for index in range(1, len(values)):
        difference = values[index] - values[index - 1]
        sign = 1 if difference > 0 else -1 if difference < 0 else 0
        if sign == 0:
            continue
        if previous_sign is not None and sign != previous_sign:
            last_change = index
        previous_sign = sign
    return last_change


def _maximum_lagged_correlation(
    parent: Sequence[float],
    child: Sequence[float],
    *,
    maximum_lag: int,
    minimum_samples: int,
    with_p_value: bool,
) -> Tuple[float, float, int, Tuple[str, ...]]:
    best: Optional[Tuple[float, float, int]] = None
    maximum = min(maximum_lag, len(parent) - minimum_samples)
    for lag in range(maximum + 1):
        parent_values = parent[lag:] if lag else parent
        child_values = child[:-lag] if lag else child
        correlation = _pearson(parent_values, child_values)
        if correlation is None:
            continue
        p_value = (
            _pearson_two_sided_p_value(correlation, len(parent_values))
            if with_p_value
            else 1.0
        )
        candidate = (correlation, p_value, lag)
        if best is None or correlation > best[0]:
            best = candidate
    if best is None:
        return 0.0, 1.0, 0, ("CONSTANT_SERIES_CORRELATION_ZEROED",)
    return best[0], best[1], best[2], tuple()


def _pearson(left: Sequence[float], right: Sequence[float]) -> Optional[float]:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    left_delta = [value - left_mean for value in left]
    right_delta = [value - right_mean for value in right]
    denominator = math.sqrt(
        sum(value * value for value in left_delta)
        * sum(value * value for value in right_delta)
    )
    if denominator == 0:
        return None
    result = sum(
        left_value * right_value
        for left_value, right_value in zip(left_delta, right_delta)
    ) / denominator
    return max(-1.0, min(1.0, result))


def _pearson_two_sided_p_value(correlation: float, sample_count: int) -> float:
    if sample_count < 3:
        return 1.0
    absolute = abs(correlation)
    if absolute >= 1.0:
        return 0.0
    degrees = sample_count - 2
    t_squared = absolute * absolute * degrees / max(1e-300, 1 - absolute * absolute)
    x = degrees / (degrees + t_squared)
    return max(0.0, min(1.0, _regularized_beta(x, degrees / 2.0, 0.5)))


def _regularized_beta(x: float, a: float, b: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    if x < (a + 1) / (a + b + 2):
        return front * _beta_continued_fraction(a, b, x) / a
    return 1 - front * _beta_continued_fraction(b, a, 1 - x) / b


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    maximum_iterations = 200
    epsilon = 3e-14
    minimum = 1e-300
    qab = a + b
    qap = a + 1
    qam = a - 1
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < minimum:
        d = minimum
    d = 1.0 / d
    result = d
    for iteration in range(1, maximum_iterations + 1):
        even = 2 * iteration
        coefficient = iteration * (b - iteration) * x / (
            (qam + even) * (a + even)
        )
        d = 1.0 + coefficient * d
        if abs(d) < minimum:
            d = minimum
        c = 1.0 + coefficient / c
        if abs(c) < minimum:
            c = minimum
        d = 1.0 / d
        result *= d * c
        coefficient = -(a + iteration) * (qab + iteration) * x / (
            (a + even) * (qap + even)
        )
        d = 1.0 + coefficient * d
        if abs(d) < minimum:
            d = minimum
        c = 1.0 + coefficient / c
        if abs(c) < minimum:
            c = minimum
        d = 1.0 / d
        delta = d * c
        result *= delta
        if abs(delta - 1.0) < epsilon:
            break
    return result


def _latency_anomaly(
    current: Sequence[float], baseline: Sequence[float], epsilon: float
) -> float:
    values = [
        abs(current_value - baseline_value) / max(abs(current_value), epsilon)
        for current_value, baseline_value in zip(current, baseline)
    ]
    return min(1.0, max(0.0, sum(values) / len(values)))


def _latency_fluctuation_contribution(
    parent_latency: Sequence[float],
    child_latency: Sequence[float],
    parent_qps: Sequence[float],
    child_qps: Sequence[float],
    epsilon: float,
) -> Tuple[float, Tuple[str, ...]]:
    parent_qps_average = sum(parent_qps) / len(parent_qps)
    if parent_qps_average <= epsilon:
        return 0.0, ("ZERO_PARENT_QPS_FLUCTUATION_ZEROED",)
    parent_variation = sum(
        abs(parent_latency[index] - parent_latency[index - 1])
        for index in range(1, len(parent_latency))
    )
    if parent_variation <= epsilon:
        return 0.0, ("ZERO_PARENT_LATENCY_VARIATION_FLUCTUATION_ZEROED",)
    child_variation = sum(
        abs(child_latency[index] - child_latency[index - 1])
        for index in range(1, len(child_latency))
    )
    child_qps_average = max(0.0, sum(child_qps) / len(child_qps))
    contribution = (
        child_qps_average / max(parent_qps_average, epsilon)
    ) * (child_variation / parent_variation)
    return min(1.0, max(0.0, contribution)), tuple()
