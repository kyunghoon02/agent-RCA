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
from .krca_metrics import (
    APIDependencySpec,
    PrometheusAPIFeatureProvider,
    PrometheusAPIFeatureQuerySpec,
)

__all__ = [
    "KubernetesHTTPAPI",
    "KubernetesResourceSpec",
    "KubernetesStateProvider",
    "APIDependencySpec",
    "PrometheusAPIFeatureProvider",
    "PrometheusAPIFeatureQuerySpec",
    "PrometheusHTTPAPI",
    "PrometheusMetricProvider",
    "PrometheusQuerySpec",
]
