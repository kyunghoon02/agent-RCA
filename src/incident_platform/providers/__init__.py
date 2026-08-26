"""Read-only telemetry provider adapters."""

from .change import DeploymentHistoryProvider
from .kubernetes import (
    KubernetesHTTPAPI,
    KubernetesIncidentProvider,
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
    "DeploymentHistoryProvider",
    "KubernetesHTTPAPI",
    "KubernetesIncidentProvider",
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
