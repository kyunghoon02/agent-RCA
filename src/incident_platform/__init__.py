"""Cloud-neutral core for the Kubernetes Incident Response Platform."""

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
from .repository import InMemoryIncidentRepository
from .postgresql import PostgreSQLIncidentRepository, apply_migrations
from .reporting import FastPathArtifacts, FastPathReportBuilder, render_markdown

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
    "InMemoryIncidentRepository",
    "IncidentCollectionService",
    "IngestionResult",
    "HTTPResponse",
    "InvalidAlert",
    "InvalidTransition",
    "ProviderBatch",
    "PostgreSQLIncidentRepository",
    "ResourceScope",
    "ReceiverConfig",
    "apply_migrations",
    "render_markdown",
    "verify_evidence_content_hash",
]
