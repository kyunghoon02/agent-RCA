"""Domain errors raised by the incident core."""


class IncidentPlatformError(RuntimeError):
    """Base class for incident-platform domain failures."""


class InvalidAlert(IncidentPlatformError):
    """An alert cannot be normalized into the Incident contract."""


class InvalidTransition(IncidentPlatformError):
    """An Incident status transition violates the lifecycle contract."""


class ContractViolation(IncidentPlatformError):
    """A runtime object does not satisfy a frozen JSON contract."""


class EvidenceGateViolation(ContractViolation):
    """A safe, machine-readable Evidence Gate rejection.

    The reason code is suitable for audit storage. The human-readable message
    remains an exception detail and must not be copied into persisted audits.
    """

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class ProviderError(IncidentPlatformError):
    """Base class for an external evidence-provider failure."""


class RetryableProviderError(ProviderError):
    """A transient provider failure that may be retried within budget."""


class PermanentProviderError(ProviderError):
    """A provider request is invalid or cannot succeed by retrying."""


class KnowledgeRepositoryError(IncidentPlatformError):
    """An approved Operational Knowledge repository cannot serve a query."""
