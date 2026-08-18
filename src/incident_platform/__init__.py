"""Cloud-neutral core for the Incident Response and Agent RCA Platform."""

from .errors import ContractViolation, InvalidAlert, InvalidTransition
from .collectors import CollectorOrchestrator, CollectorSpec, IncidentCollectionService
from .deterministic import DeterministicDecision, DeterministicRCAEngine
from .evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceDraft,
    EvidenceWindow,
    ProviderBatch,
    ResourceScope,
    verify_evidence_content_hash,
)
from .incidents import (
    AlertmanagerIngestionService,
    AlertmanagerNormalizer,
    IngestionResult,
)
from .http_receiver import (
    AlertmanagerHTTPReceiver,
    AlertmanagerWebhookWSGI,
    HTTPResponse,
    ReceiverConfig,
)
from .fast_path import FastPathRun, IncidentFastPathService
from .repository import InMemoryIncidentRepository
from .postgresql import PostgreSQLIncidentRepository, apply_migrations
from .providers import (
    KubernetesHTTPAPI,
    KubernetesResourceSpec,
    KubernetesStateProvider,
    PrometheusHTTPAPI,
    PrometheusMetricProvider,
    PrometheusQuerySpec,
)
from .reporting import FastPathArtifacts, FastPathReportBuilder, render_markdown
from .stategraph import (
    GraphLocalizer,
    GraphProjection,
    InMemoryStateGraphRepository,
    InvestigationScope,
    stable_graph_id,
    state_content_hash,
    validate_graph_record,
)
from .projectors import KubernetesEvidenceProjector

__all__ = [
    "AlertmanagerIngestionService",
    "AlertmanagerHTTPReceiver",
    "AlertmanagerNormalizer",
    "AlertmanagerWebhookWSGI",
    "CollectionRequest",
    "CollectorOrchestrator",
    "CollectorSpec",
    "ContractViolation",
    "DeterministicDecision",
    "DeterministicRCAEngine",
    "EvidenceBuilder",
    "EvidenceDraft",
    "EvidenceWindow",
    "FastPathArtifacts",
    "FastPathReportBuilder",
    "FastPathRun",
    "GraphLocalizer",
    "GraphProjection",
    "InMemoryStateGraphRepository",
    "InMemoryIncidentRepository",
    "IncidentCollectionService",
    "IncidentFastPathService",
    "IngestionResult",
    "HTTPResponse",
    "InvalidAlert",
    "InvalidTransition",
    "InvestigationScope",
    "KubernetesHTTPAPI",
    "KubernetesEvidenceProjector",
    "KubernetesResourceSpec",
    "KubernetesStateProvider",
    "ProviderBatch",
    "PostgreSQLIncidentRepository",
    "PrometheusHTTPAPI",
    "PrometheusMetricProvider",
    "PrometheusQuerySpec",
    "ResourceScope",
    "ReceiverConfig",
    "apply_migrations",
    "render_markdown",
    "stable_graph_id",
    "state_content_hash",
    "validate_graph_record",
    "verify_evidence_content_hash",
]
