"""Evidence normalization, redaction, hashing, and contract validation."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .contracts import validate_contract
from .errors import ContractViolation


SENSITIVE_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "credential",
    "password",
    "private_key",
    "secret_value",
    "token",
)
INLINE_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)\b(password|passwd|token|api[_-]?key|secret)\s*[=:]\s*[^\s,;]+"
    ),
)


def format_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ContractViolation("Evidence timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ContractViolation(f"{field} must be an RFC3339 timestamp") from error
    if parsed.tzinfo is None:
        raise ContractViolation(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class EvidenceWindow:
    start: str
    end: str


@dataclass(frozen=True)
class ResourceScope:
    namespace: str
    resource_names: Tuple[str, ...]
    max_items: int = 100

    def __post_init__(self) -> None:
        if not self.namespace.strip():
            raise ContractViolation("ResourceScope.namespace is required")
        if not self.resource_names:
            raise ContractViolation("ResourceScope requires at least one resource name")
        if any(not name.strip() for name in self.resource_names):
            raise ContractViolation("ResourceScope resource names must not be empty")
        if len(self.resource_names) != len(set(self.resource_names)):
            raise ContractViolation("ResourceScope resource names must be unique")
        if self.max_items <= 0:
            raise ContractViolation("ResourceScope.max_items must be positive")


@dataclass(frozen=True)
class CollectionRequest:
    request_id: str
    incident_id: str
    window: EvidenceWindow
    scope: ResourceScope
    timeout_seconds: float
    attempt: int = 1

    def __post_init__(self) -> None:
        if not self.request_id.strip():
            raise ContractViolation("CollectionRequest.request_id is required")
        if not self.incident_id.startswith("inc-"):
            raise ContractViolation("CollectionRequest.incident_id is invalid")
        if self.timeout_seconds <= 0:
            raise ContractViolation("CollectionRequest.timeout_seconds must be positive")
        if self.attempt <= 0:
            raise ContractViolation("CollectionRequest.attempt must be positive")
        start = parse_time(self.window.start, "EvidenceWindow.start")
        end = parse_time(self.window.end, "EvidenceWindow.end")
        if start > end:
            raise ContractViolation("EvidenceWindow.start must not follow end")


@dataclass(frozen=True)
class EvidenceDraft:
    source: str
    kind: str
    observed_at: str
    subject: Mapping[str, Any]
    summary: str
    facts: Mapping[str, Any]
    provider: str
    query: str
    locator: str
    freshness: str = "live"
    completeness: float = 1.0
    confidence: float = 1.0


@dataclass(frozen=True)
class ProviderBatch:
    items: Tuple[EvidenceDraft, ...] = field(default_factory=tuple)
    status: str = "SUCCEEDED"
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in {"SUCCEEDED", "PARTIAL"}:
            raise ContractViolation(
                "ProviderBatch.status must be SUCCEEDED or PARTIAL; failures use exceptions"
            )
        if self.status == "PARTIAL" and not self.error:
            raise ContractViolation("PARTIAL ProviderBatch requires an error description")


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _redact_string(value: str, path: str) -> Tuple[str, List[str]]:
    redacted = value
    changed = False
    for pattern in INLINE_SECRET_PATTERNS:
        replacement = (
            lambda match: f"{match.group(1)}=[REDACTED]"
            if match.lastindex
            else "Bearer [REDACTED]"
        )
        redacted, count = pattern.subn(replacement, redacted)
        changed = changed or count > 0
    return redacted, [path] if changed else []


def redact(value: Any, path: str = "$") -> Tuple[Any, List[str]]:
    """Return a deep-redacted JSON-compatible value and affected field paths."""

    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        paths: List[str] = []
        for raw_key, item in value.items():
            key = str(raw_key)
            item_path = f"{path}.{key}"
            if _is_sensitive_key(key):
                result[key] = "[REDACTED]"
                paths.append(item_path)
                continue
            result[key], item_paths = redact(item, item_path)
            paths.extend(item_paths)
        return result, paths
    if isinstance(value, (list, tuple)):
        result_list = []
        paths = []
        for index, item in enumerate(value):
            redacted_item, item_paths = redact(item, f"{path}[{index}]")
            result_list.append(redacted_item)
            paths.extend(item_paths)
        return result_list, paths
    if isinstance(value, str):
        return _redact_string(value, path)
    return copy.deepcopy(value), []


class EvidenceBuilder:
    """Turn provider drafts into immutable, traceable EvidenceItem dictionaries."""

    def build(
        self,
        draft: EvidenceDraft,
        request: CollectionRequest,
        *,
        collected_at: datetime,
    ) -> Dict[str, Any]:
        observed_at = parse_time(draft.observed_at, "EvidenceDraft.observed_at")
        window_start = parse_time(request.window.start, "EvidenceWindow.start")
        window_end = parse_time(request.window.end, "EvidenceWindow.end")
        if observed_at < window_start or observed_at > window_end:
            raise ContractViolation(
                "EvidenceDraft.observed_at is outside the requested time window"
            )
        subject = copy.deepcopy(dict(draft.subject))
        subject_namespace = subject.get("namespace")
        if subject_namespace != request.scope.namespace:
            raise ContractViolation(
                f"Evidence subject namespace {subject_namespace!r} is outside "
                f"scope {request.scope.namespace!r}"
            )
        subject_name = subject.get("name")
        if subject_name not in request.scope.resource_names:
            raise ContractViolation(
                f"Evidence subject {subject_name!r} is outside the requested resource scope"
            )

        summary, summary_redactions = redact(draft.summary, "$.summary")
        facts, fact_redactions = redact(draft.facts, "$.facts")
        query, query_redactions = redact(draft.query, "$.provenance.query")
        locator, locator_redactions = redact(
            draft.locator, "$.provenance.locator"
        )
        redactions = sorted(
            set(
                summary_redactions
                + fact_redactions
                + query_redactions
                + locator_redactions
            )
        )
        collected_at_text = format_time(collected_at)
        hash_input = {
            "schema_version": "1.0.0",
            "incident_id": request.incident_id,
            "source": draft.source,
            "kind": draft.kind,
            "observed_at": draft.observed_at,
            "window": {"start": request.window.start, "end": request.window.end},
            "subject": subject,
            "summary": summary,
            "facts": facts,
            "provider": draft.provider,
            "query": query,
            "locator": locator,
            "collected_at": collected_at_text,
            "quality": {
                "freshness": draft.freshness,
                "completeness": draft.completeness,
                "confidence": draft.confidence,
            },
            "redactions": redactions,
        }
        canonical = json.dumps(
            hash_input,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        evidence = {
            "schema_version": "1.0.0",
            "evidence_id": f"ev-{digest[:24]}",
            "incident_id": request.incident_id,
            "source": draft.source,
            "kind": draft.kind,
            "observed_at": draft.observed_at,
            "window": {"start": request.window.start, "end": request.window.end},
            "subject": subject,
            "summary": summary,
            "facts": facts,
            "provenance": {
                "provider": draft.provider,
                "query": query,
                "locator": locator,
                "collected_at": collected_at_text,
                "content_hash": f"sha256:{digest}",
            },
            "quality": {
                "freshness": draft.freshness,
                "completeness": draft.completeness,
                "confidence": draft.confidence,
            },
            "redactions": redactions,
        }
        validate_contract("evidence-item.schema.json", evidence)
        return evidence


def verify_evidence_content_hash(evidence: Mapping[str, Any]) -> bool:
    """Recompute the digest over the normalized stored Evidence content."""

    provenance = evidence.get("provenance", {})
    hash_input = {
        "schema_version": evidence.get("schema_version"),
        "incident_id": evidence.get("incident_id"),
        "source": evidence.get("source"),
        "kind": evidence.get("kind"),
        "observed_at": evidence.get("observed_at"),
        "window": evidence.get("window"),
        "subject": evidence.get("subject"),
        "summary": evidence.get("summary"),
        "facts": evidence.get("facts"),
        "provider": provenance.get("provider"),
        "query": provenance.get("query"),
        "locator": provenance.get("locator"),
        "collected_at": provenance.get("collected_at"),
        "quality": evidence.get("quality"),
        "redactions": evidence.get("redactions"),
    }
    canonical = json.dumps(
        hash_input,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    actual = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    return provenance.get("content_hash") == actual


def validate_provider_batch(
    batch: ProviderBatch,
    request: CollectionRequest,
) -> None:
    """Enforce limits before provider output reaches Evidence storage."""

    if len(batch.items) > request.scope.max_items:
        raise ContractViolation(
            f"provider returned {len(batch.items)} items; limit is "
            f"{request.scope.max_items}"
        )
    for draft in batch.items:
        namespace = draft.subject.get("namespace")
        name = draft.subject.get("name")
        if namespace != request.scope.namespace:
            raise ContractViolation("provider returned evidence outside namespace scope")
        if name not in request.scope.resource_names:
            raise ContractViolation("provider returned evidence outside resource scope")
