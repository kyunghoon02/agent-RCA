"""Read-only telemetry provider adapters."""

from .kubernetes import (
    KubernetesHTTPAPI,
    KubernetesResourceSpec,
    KubernetesStateProvider,
)
from .prometheus import (
    PrometheusHTTPAPI,
    PrometheusMetricProvider,
    PrometheusQuerySpec,
)

__all__ = [
    "KubernetesHTTPAPI",
    "KubernetesResourceSpec",
    "KubernetesStateProvider",
    "PrometheusHTTPAPI",
    "PrometheusMetricProvider",
    "PrometheusQuerySpec",
]
