"""Read-only telemetry provider adapters."""

from .kubernetes import (
    KubernetesHTTPAPI,
    KubernetesInventoryProvider,
    KubernetesResourceSpec,
    KubernetesResourcePage,
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
    "KubernetesInventoryProvider",
    "KubernetesResourcePage",
    "KubernetesResourceSpec",
    "KubernetesStateProvider",
    "APIDependencySpec",
    "PrometheusAPIFeatureProvider",
    "PrometheusAPIFeatureQuerySpec",
    "PrometheusHTTPAPI",
    "PrometheusMetricProvider",
    "PrometheusQuerySpec",
]
