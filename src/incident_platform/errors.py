"""Domain errors raised by the incident core."""


class IncidentPlatformError(RuntimeError):
    """Base class for incident-platform domain failures."""


class InvalidAlert(IncidentPlatformError):
    """An alert cannot be normalized into the Incident contract."""


class InvalidTransition(IncidentPlatformError):
    """An Incident status transition violates the lifecycle contract."""


class ContractViolation(IncidentPlatformError):
    """A runtime object does not satisfy a frozen JSON contract."""


class ProviderError(IncidentPlatformError):
    """Base class for an external evidence-provider failure."""


class RetryableProviderError(ProviderError):
    """A transient provider failure that may be retried within budget."""


class PermanentProviderError(ProviderError):
    """A provider request is invalid or cannot succeed by retrying."""
