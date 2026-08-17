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
from .repository import InMemoryIncidentRepository
from .reporting import FastPathArtifacts, FastPathReportBuilder, render_markdown

__all__ = [
    "AlertmanagerIngestionService",
    "AlertmanagerNormalizer",
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
    "InvalidAlert",
    "InvalidTransition",
    "ProviderBatch",
    "ResourceScope",
    "render_markdown",
    "verify_evidence_content_hash",
]
