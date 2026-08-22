"""Bounded Operational Knowledge indexing and retrieval.

Runtime Evidence proves the current Incident. This module only returns versioned
references that may guide investigation; it never creates EvidenceItems.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import quote

import yaml

from .contracts import validate_contract
from .errors import ContractViolation, KnowledgeRepositoryError
from .evidence import format_time, parse_time
from .stategraph import stable_graph_id


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KNOWLEDGE_ROOT = ROOT / "knowledge"
DEFAULT_INDEX_PATH = DEFAULT_KNOWLEDGE_ROOT / "index.yaml"

DOCUMENT_TYPES = frozenset(
    {"architecture", "service-catalog", "runbook", "slo", "tool-guide"}
)
RETRIEVAL_METHODS = frozenset(
    {
        "entity-key+lexical",
        "entity-key+vector",
        "entity-key+lexical+vector-rrf",
    }
)
PROHIBITED_SOURCE_PATH_PARTS = frozenset(
    {
        "evaluation",
        "ground-truth",
        "ground_truth",
        "fault-injection",
        "fault_injection",
        "grader",
        "unverified-agent-output",
        "agent-output",
        "reasoning-trace",
        "private",
    }
)
SENSITIVE_CONTENT_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:password|passwd|api[_-]?key|client[_-]?secret|access[_-]?token)"
        r"\s*[:=]\s*[^\s`]{4,}",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class ReferenceDocument:
    """Validated index metadata plus hash-verified source content."""

    metadata: Mapping[str, Any]
    content: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        validate_contract("reference-document.schema.json", self.metadata)
        if not isinstance(self.content, str) or not self.content.strip():
            raise ContractViolation("Operational Knowledge document must be non-empty")


class ReferenceDocumentRepository(Protocol):
    """Storage-neutral source of versioned Operational Knowledge documents."""

    def list_documents(self, *, limit: int) -> Tuple[ReferenceDocument, ...]:
        ...


@dataclass(frozen=True)
class SemanticSearchCandidate:
    """Hash-pinned document identity allowed to enter semantic ranking."""

    reference_document_id: str
    content_hash: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"ref-[a-z0-9][a-z0-9-]{7,63}", self.reference_document_id):
            raise ContractViolation("semantic candidate has an invalid ReferenceDocument ID")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", self.content_hash):
            raise ContractViolation("semantic candidate has an invalid content hash")


@dataclass(frozen=True)
class SemanticSearchHit:
    """One semantic rank returned by a bounded vector index."""

    reference_document_id: str
    content_hash: str
    score: float

    def __post_init__(self) -> None:
        SemanticSearchCandidate(self.reference_document_id, self.content_hash)
        if not math.isfinite(float(self.score)):
            raise ContractViolation("semantic search score must be finite")


class SemanticKnowledgeIndex(Protocol):
    """Vector-search port; implementations never decide document eligibility."""

    def search(
        self,
        query_text: str,
        *,
        candidates: Sequence[SemanticSearchCandidate],
        limit: int,
    ) -> Tuple[SemanticSearchHit, ...]:
        ...


@dataclass(frozen=True)
class KnowledgeRetrievalPolicy:
    """Hard budgets for one retrieval request and one Git index scan."""

    max_documents: int = 5
    max_characters: int = 12_000
    max_query_terms: int = 16
    max_timeout_seconds: float = 5.0
    max_index_documents: int = 500
    max_document_characters: int = 100_000
    max_excerpt_characters: int = 4_000

    def __post_init__(self) -> None:
        if not 1 <= self.max_documents <= 5:
            raise ValueError("Knowledge max_documents must be between 1 and 5")
        if not 100 <= self.max_characters <= 12_000:
            raise ValueError("Knowledge max_characters must be between 100 and 12000")
        if not 1 <= self.max_query_terms <= 16:
            raise ValueError("Knowledge max_query_terms must be between 1 and 16")
        if not 0 < self.max_timeout_seconds <= 5:
            raise ValueError("Knowledge max_timeout_seconds must be in (0, 5]")
        if not 1 <= self.max_index_documents <= 500:
            raise ValueError("Knowledge max_index_documents must be between 1 and 500")
        if self.max_document_characters < self.max_excerpt_characters:
            raise ValueError("Knowledge source limit must cover the excerpt limit")
        if not 1 <= self.max_excerpt_characters <= self.max_characters:
            raise ValueError("Knowledge excerpt limit exceeds the request character cap")


@dataclass(frozen=True)
class KnowledgeRetrievalRun:
    """One validated query, its bounded references, and a non-secret audit record."""

    query: Mapping[str, Any]
    references: Tuple[Mapping[str, Any], ...]
    audit: Mapping[str, Any]


class GitReferenceDocumentRepository:
    """Load a hash-pinned Markdown/YAML corpus from one bounded Git directory."""

    def __init__(
        self,
        corpus_root: Path = DEFAULT_KNOWLEDGE_ROOT,
        index_path: Path = DEFAULT_INDEX_PATH,
        *,
        max_document_characters: int = 100_000,
    ) -> None:
        if max_document_characters < 1:
            raise ValueError("max_document_characters must be positive")
        self._corpus_root = corpus_root.resolve()
        self._index_path = index_path.resolve()
        self._max_document_characters = max_document_characters
        self._require_within_root(self._index_path, "Knowledge index")

    def list_documents(self, *, limit: int) -> Tuple[ReferenceDocument, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("Knowledge index limit must be between 1 and 500")
        try:
            raw = yaml.safe_load(self._index_path.read_text(encoding="utf-8"))
        except OSError as error:
            raise KnowledgeRepositoryError("knowledge index is unavailable") from error
        except yaml.YAMLError as error:
            raise ContractViolation("Knowledge index YAML is malformed") from error
        if not isinstance(raw, Mapping):
            raise ContractViolation("Knowledge index must be an object")
        index = copy.deepcopy(dict(raw))
        validate_contract("reference-index.schema.json", index)
        entries = index["documents"]
        if len(entries) > limit:
            raise KnowledgeRepositoryError(
                "INDEX_BUDGET_EXCEEDED: Knowledge index exceeds scan budget"
            )

        seen_ids: set[str] = set()
        documents = []
        for entry in entries:
            metadata = copy.deepcopy(dict(entry))
            validate_contract("reference-document.schema.json", metadata)
            reference_id = metadata["reference_document_id"]
            if reference_id in seen_ids:
                raise ContractViolation(
                    f"duplicate ReferenceDocument ID in index: {reference_id}"
                )
            seen_ids.add(reference_id)
            self._validate_reference_window(metadata)
            source_path = self._resolve_source_path(metadata["source_path_or_uri"])
            content = self._read_source(source_path)
            self._validate_content(metadata, content)
            documents.append(ReferenceDocument(metadata=metadata, content=content))
        return tuple(documents)

    def _resolve_source_path(self, raw_path: str) -> Path:
        posix_path = PurePosixPath(raw_path)
        if posix_path.is_absolute() or ".." in posix_path.parts:
            raise ContractViolation("Knowledge source path must stay inside its corpus")
        normalized_parts = {part.casefold() for part in posix_path.parts}
        prohibited = sorted(normalized_parts & PROHIBITED_SOURCE_PATH_PARTS)
        if prohibited:
            raise ContractViolation(
                "Knowledge source path belongs to a prohibited corpus: "
                + ", ".join(prohibited)
            )
        source_path = (self._corpus_root / Path(*posix_path.parts)).resolve()
        self._require_within_root(source_path, "Knowledge source")
        if source_path.suffix.casefold() not in {".md", ".yaml", ".yml"}:
            raise ContractViolation("Knowledge source must be Markdown or YAML")
        if not source_path.is_file():
            raise ContractViolation(f"Knowledge source does not exist: {raw_path}")
        return source_path

    def _read_source(self, path: Path) -> str:
        try:
            size = path.stat().st_size
            if size > self._max_document_characters * 4:
                raise ContractViolation("Knowledge source exceeds its byte budget")
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ContractViolation("Knowledge source must be UTF-8 text") from error
        except OSError as error:
            raise KnowledgeRepositoryError("knowledge source is unavailable") from error
        if len(content) > self._max_document_characters:
            raise ContractViolation("Knowledge source exceeds its character budget")
        return content

    @staticmethod
    def _validate_reference_window(metadata: Mapping[str, Any]) -> None:
        valid_from = parse_time(metadata["valid_from"], "ReferenceDocument.valid_from")
        valid_to_value = metadata["valid_to"]
        if valid_to_value is None:
            return
        valid_to = parse_time(valid_to_value, "ReferenceDocument.valid_to")
        if valid_from > valid_to:
            raise ContractViolation("ReferenceDocument valid_from follows valid_to")

    @staticmethod
    def _validate_content(metadata: Mapping[str, Any], content: str) -> None:
        if not content.strip():
            raise ContractViolation("Operational Knowledge document must be non-empty")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        actual_hash = f"sha256:{digest}"
        if actual_hash != metadata["content_hash"]:
            raise ContractViolation(
                f"ReferenceDocument content hash mismatch: "
                f"{metadata['reference_document_id']}"
            )
        for pattern in SENSITIVE_CONTENT_PATTERNS:
            if pattern.search(content):
                raise ContractViolation(
                    "Operational Knowledge contains credential-like content"
                )

    def _require_within_root(self, path: Path, label: str) -> None:
        try:
            path.relative_to(self._corpus_root)
        except ValueError as error:
            raise ContractViolation(f"{label} escapes the Knowledge corpus") from error


class BoundedKnowledgeRetriever:
    """Retrieve only approved references scoped by one frozen StateGraph Context."""

    def __init__(
        self,
        repository: ReferenceDocumentRepository,
        *,
        semantic_index: Optional[SemanticKnowledgeIndex] = None,
        retrieval_method: str = "entity-key+lexical",
        policy: Optional[KnowledgeRetrievalPolicy] = None,
        monotonic_clock: Callable[[], float] = monotonic,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._repository = repository
        if retrieval_method not in RETRIEVAL_METHODS:
            raise ValueError(f"unsupported Knowledge retrieval method: {retrieval_method}")
        if retrieval_method != "entity-key+lexical" and semantic_index is None:
            raise ValueError("vector and hybrid retrieval require a semantic index")
        self._semantic_index = semantic_index
        self._retrieval_method = retrieval_method
        self._policy = policy or KnowledgeRetrievalPolicy()
        self._monotonic = monotonic_clock
        self._utc_now = utc_now

    def retrieve(
        self,
        context: Mapping[str, Any],
        *,
        request_id: str,
        allowed_document_types: Sequence[str],
        query_terms: Sequence[str],
        top_k: int = 5,
        character_budget: int = 12_000,
        timeout_seconds: float = 2.0,
        requested_at: Optional[datetime] = None,
    ) -> KnowledgeRetrievalRun:
        query = self._build_query(
            context,
            request_id=request_id,
            allowed_document_types=allowed_document_types,
            query_terms=query_terms,
            top_k=top_k,
            character_budget=character_budget,
            timeout_seconds=timeout_seconds,
            requested_at=requested_at or self._utc_now(),
        )
        deadline = self._monotonic() + query["timeout_seconds"]
        try:
            documents = self._repository.list_documents(
                limit=self._policy.max_index_documents
            )
        except KnowledgeRepositoryError as error:
            reason = (
                "INDEX_BUDGET_EXCEEDED"
                if str(error).startswith("INDEX_BUDGET_EXCEEDED:")
                else "REPOSITORY_UNAVAILABLE"
            )
            return self._run(
                query,
                references=(),
                status="FAILED",
                reason_code=reason,
                scanned_documents=0,
                excluded_counts={},
            )
        if self._monotonic() > deadline:
            return self._run(
                query,
                references=(),
                status="TIMED_OUT",
                reason_code="TIME_BUDGET_EXHAUSTED",
                scanned_documents=0,
                excluded_counts={},
            )

        eligible: List[Tuple[ReferenceDocument, Tuple[str, ...], int, int]] = []
        excluded: Dict[str, int] = {}
        stale_matches = 0
        localized_keys = set(query["localized_entity_keys"])
        context_time = parse_time(context["frozen_at"], "Context.frozen_at")
        for scanned, document in enumerate(documents, start=1):
            if self._monotonic() > deadline:
                return self._run(
                    query,
                    references=(),
                    status="TIMED_OUT",
                    reason_code="TIME_BUDGET_EXHAUSTED",
                    scanned_documents=scanned - 1,
                    excluded_counts=excluded,
                )
            metadata = document.metadata
            if metadata["document_type"] not in query["allowed_document_types"]:
                self._increment(excluded, "document_type")
                continue
            matched_keys = tuple(
                sorted(localized_keys & set(metadata["entity_keys"]))
            )
            if not matched_keys:
                self._increment(excluded, "entity_scope")
                continue
            lexical_score = self._lexical_score(document, query["query_terms"])
            if metadata["review_status"] != "approved":
                self._increment(excluded, metadata["review_status"])
                continue
            valid_from = parse_time(
                metadata["valid_from"], "ReferenceDocument.valid_from"
            )
            if valid_from > context_time:
                self._increment(excluded, "not_yet_valid")
                continue
            valid_to_value = metadata["valid_to"]
            if valid_to_value is not None and parse_time(
                valid_to_value, "ReferenceDocument.valid_to"
            ) < context_time:
                if lexical_score > 0:
                    stale_matches += 1
                self._increment(excluded, "expired")
                continue
            entity_score = sum(
                self._entity_key_weight(key) for key in matched_keys
            )
            eligible.append((document, matched_keys, lexical_score, entity_score))

        try:
            candidates = self._rank_candidates(query, eligible, excluded)
        except KnowledgeRepositoryError:
            return self._run(
                query,
                references=(),
                status="FAILED",
                reason_code="REPOSITORY_UNAVAILABLE",
                scanned_documents=len(documents),
                excluded_counts=excluded,
            )
        if self._monotonic() > deadline:
            return self._run(
                query,
                references=(),
                status="TIMED_OUT",
                reason_code="TIME_BUDGET_EXHAUSTED",
                scanned_documents=len(documents),
                excluded_counts=excluded,
            )

        if not candidates:
            status = "STALE_ONLY" if stale_matches else "NO_MATCH"
            reason = "ONLY_STALE_MATCHES" if stale_matches else "NO_SCOPED_MATCH"
            return self._run(
                query,
                references=(),
                status=status,
                reason_code=reason,
                scanned_documents=len(documents),
                excluded_counts=excluded,
            )

        selected = candidates[: query["top_k"]]
        references = self._build_references(
            query,
            selected,
            retrieval_method=self._retrieval_method,
        )
        return self._run(
            query,
            references=references,
            status="SUCCEEDED",
            reason_code="MATCHES_FOUND",
            scanned_documents=len(documents),
            excluded_counts=excluded,
        )

    def _build_query(
        self,
        context: Mapping[str, Any],
        *,
        request_id: str,
        allowed_document_types: Sequence[str],
        query_terms: Sequence[str],
        top_k: int,
        character_budget: int,
        timeout_seconds: float,
        requested_at: datetime,
    ) -> Dict[str, Any]:
        validate_contract("context-package.schema.json", context)
        if context["localization"]["strategy"] != "stategraph":
            raise ContractViolation(
                "Knowledge retrieval requires a StateGraph-localized Context"
            )
        if requested_at.tzinfo is None:
            raise ContractViolation("Knowledge retrieval requested_at needs a timezone")
        requested_at_text = format_time(requested_at)
        if parse_time(requested_at_text, "KnowledgeQuery.requested_at") < parse_time(
            context["frozen_at"], "Context.frozen_at"
        ):
            raise ContractViolation(
                "Knowledge retrieval cannot precede the Context freeze time"
            )
        if any(not isinstance(value, str) for value in allowed_document_types):
            raise ContractViolation("Knowledge query document types must be strings")
        document_types = tuple(dict.fromkeys(allowed_document_types))
        if not document_types or not set(document_types) <= DOCUMENT_TYPES:
            raise ContractViolation("Knowledge query contains unsupported document types")
        if any(not isinstance(value, str) for value in query_terms):
            raise ContractViolation("Knowledge query terms must be strings")
        terms = tuple(
            dict.fromkeys(
                normalized
                for value in query_terms
                if (normalized := " ".join(value.split()).casefold())
            )
        )
        if not terms:
            raise ContractViolation("Knowledge query requires at least one term")
        if len(terms) > self._policy.max_query_terms:
            raise ContractViolation("Knowledge query exceeds its term budget")
        if not 1 <= top_k <= self._policy.max_documents:
            raise ContractViolation("Knowledge query exceeds its Top-K budget")
        if not 100 <= character_budget <= self._policy.max_characters:
            raise ContractViolation("Knowledge query exceeds its character budget")
        if not 0 < timeout_seconds <= self._policy.max_timeout_seconds:
            raise ContractViolation("Knowledge query exceeds its timeout budget")
        entity_keys = self.localized_entity_keys(context)
        query = {
            "schema_version": "1.0.0",
            "request_id": request_id,
            "incident_id": context["incident_id"],
            "context_id": context["context_id"],
            "investigation_scope": copy.deepcopy(context["scope"]),
            "localized_entity_keys": list(entity_keys),
            "allowed_document_types": list(document_types),
            "query_terms": list(terms),
            "retrieval_method": self._retrieval_method,
            "top_k": top_k,
            "character_budget": character_budget,
            "timeout_seconds": timeout_seconds,
            "requested_at": requested_at_text,
        }
        validate_contract("knowledge-retrieval-query.schema.json", query)
        return query

    @classmethod
    def localized_entity_keys(
        cls, context: Mapping[str, Any]
    ) -> Tuple[str, ...]:
        """Derive keys from frozen Graph Entity refs; callers cannot inject them."""

        entities: Dict[str, Mapping[str, Any]] = {}
        for path in context["state_paths"]:
            for entity in path["entities"]:
                entity_id = entity.get("entity_id")
                if not isinstance(entity_id, str):
                    raise ContractViolation(
                        "Knowledge retrieval found a non-Graph Entity reference"
                    )
                entities[entity_id] = entity
        if not entities:
            raise ContractViolation("Knowledge retrieval found no localized Entities")
        keys: set[str] = set()
        for entity_id in sorted(entities):
            entity = entities[entity_id]
            cls._add_entity_key(keys, "entity-id", entity_id)
            cls._add_entity_key(keys, "domain", entity["domain"])
            cls._add_entity_key(keys, "entity-type", entity["entity_type"])
            cls._add_entity_key(keys, "name", entity["name"])
            for scope_key, scope_value in sorted(entity["scope"].items()):
                if not isinstance(scope_value, (str, int, float, bool)):
                    continue
                normalized_scope_key = re.sub(
                    r"[^a-z0-9-]", "-", scope_key.casefold().replace("_", "-")
                ).strip("-")
                if not normalized_scope_key:
                    continue
                cls._add_entity_key(
                    keys,
                    f"scope-{normalized_scope_key}"[:32],
                    str(scope_value),
                )
        return tuple(sorted(keys))

    @staticmethod
    def _add_entity_key(keys: set[str], prefix: str, value: str) -> None:
        encoded = quote(value.casefold(), safe="._-/=")
        if encoded and len(prefix) >= 2 and len(encoded) <= 128:
            keys.add(f"{prefix}:{encoded}")

    @staticmethod
    def _lexical_score(
        document: ReferenceDocument, query_terms: Sequence[str]
    ) -> int:
        title = document.metadata["title"].casefold()
        content = document.content.casefold()
        return sum(
            title.count(term) * 8 + content.count(term)
            for term in query_terms
        )

    @staticmethod
    def _entity_key_weight(key: str) -> int:
        if key.startswith("entity-id:"):
            return 100
        if key.startswith("name:"):
            return 40
        if key.startswith("scope-"):
            return 20
        if key.startswith("entity-type:"):
            return 10
        return 5

    def _rank_candidates(
        self,
        query: Mapping[str, Any],
        eligible: Sequence[Tuple[ReferenceDocument, Tuple[str, ...], int, int]],
        excluded: Dict[str, int],
    ) -> List[Tuple[float, ReferenceDocument, Tuple[str, ...]]]:
        lexical = [
            (lexical_score + entity_score, document, matched_keys)
            for document, matched_keys, lexical_score, entity_score in eligible
            if lexical_score > 0
        ]
        lexical.sort(
            key=lambda item: (
                -item[0],
                item[1].metadata["reference_document_id"],
            )
        )
        if self._retrieval_method == "entity-key+lexical":
            for _, _, lexical_score, _ in eligible:
                if lexical_score == 0:
                    self._increment(excluded, "lexical")
            return lexical

        semantic = self._semantic_candidates(query, eligible)
        if self._retrieval_method == "entity-key+vector":
            return semantic
        return self._reciprocal_rank_fusion(lexical, semantic)

    def _semantic_candidates(
        self,
        query: Mapping[str, Any],
        eligible: Sequence[Tuple[ReferenceDocument, Tuple[str, ...], int, int]],
    ) -> List[Tuple[float, ReferenceDocument, Tuple[str, ...]]]:
        if not eligible:
            return []
        if self._semantic_index is None:
            raise KnowledgeRepositoryError("semantic Knowledge index is unavailable")
        allowed = {
            document.metadata["reference_document_id"]: (
                document.metadata["content_hash"],
                document,
                matched_keys,
            )
            for document, matched_keys, _, _ in eligible
        }
        hits = self._semantic_index.search(
            " ".join(query["query_terms"]),
            candidates=tuple(
                SemanticSearchCandidate(
                    reference_document_id=reference_id,
                    content_hash=value[0],
                )
                for reference_id, value in sorted(allowed.items())
            ),
            limit=min(self._policy.max_index_documents, max(query["top_k"] * 4, 20)),
        )
        seen: set[str] = set()
        ranked: List[Tuple[float, ReferenceDocument, Tuple[str, ...]]] = []
        for hit in hits:
            allowed_value = allowed.get(hit.reference_document_id)
            if allowed_value is None or allowed_value[0] != hit.content_hash:
                raise KnowledgeRepositoryError(
                    "semantic Knowledge index returned an out-of-scope or stale document"
                )
            if hit.reference_document_id in seen:
                raise KnowledgeRepositoryError(
                    "semantic Knowledge index returned a duplicate document"
                )
            seen.add(hit.reference_document_id)
            ranked.append((float(hit.score), allowed_value[1], allowed_value[2]))
        ranked.sort(
            key=lambda item: (
                -item[0],
                item[1].metadata["reference_document_id"],
            )
        )
        return ranked

    @staticmethod
    def _reciprocal_rank_fusion(
        lexical: Sequence[Tuple[float, ReferenceDocument, Tuple[str, ...]]],
        semantic: Sequence[Tuple[float, ReferenceDocument, Tuple[str, ...]]],
        *,
        rank_constant: int = 60,
    ) -> List[Tuple[float, ReferenceDocument, Tuple[str, ...]]]:
        fused: Dict[str, Tuple[float, ReferenceDocument, Tuple[str, ...]]] = {}
        for ranking in (lexical, semantic):
            for rank, (_, document, matched_keys) in enumerate(ranking, start=1):
                reference_id = document.metadata["reference_document_id"]
                previous = fused.get(reference_id)
                score = 1.0 / (rank_constant + rank)
                if previous is not None:
                    score += previous[0]
                fused[reference_id] = (score, document, matched_keys)
        return sorted(
            fused.values(),
            key=lambda item: (
                -item[0],
                item[1].metadata["reference_document_id"],
            ),
        )

    def _build_references(
        self,
        query: Mapping[str, Any],
        selected: Sequence[Tuple[float, ReferenceDocument, Tuple[str, ...]]],
        *,
        retrieval_method: str,
    ) -> Tuple[Mapping[str, Any], ...]:
        remaining = query["character_budget"]
        references = []
        for offset, (_, document, matched_keys) in enumerate(selected):
            slots_left = len(selected) - offset
            allocation = min(
                self._policy.max_excerpt_characters,
                max(1, remaining // slots_left),
            )
            excerpt = self._excerpt(
                document.content,
                query["query_terms"],
                allocation,
            )
            remaining -= len(excerpt)
            metadata = document.metadata
            reference = {
                "schema_version": "1.0.0",
                "retrieval_id": stable_graph_id(
                    "kret",
                    {
                        "request_id": query["request_id"],
                        "reference_document_id": metadata["reference_document_id"],
                        "version": metadata["version"],
                        "content_hash": metadata["content_hash"],
                        "retrieval_method": retrieval_method,
                    },
                ),
                "request_id": query["request_id"],
                "reference_document_id": metadata["reference_document_id"],
                "source_class": "operational-knowledge",
                "document_type": metadata["document_type"],
                "title": metadata["title"],
                "source_path_or_uri": metadata["source_path_or_uri"],
                "document_version": metadata["version"],
                "content_hash": metadata["content_hash"],
                "matched_entity_keys": list(matched_keys),
                "retrieval_method": retrieval_method,
                "rank": offset + 1,
                "bounded_excerpt": excerpt,
            }
            validate_contract("retrieved-reference.schema.json", reference)
            references.append(reference)
        return tuple(references)

    @staticmethod
    def _excerpt(content: str, query_terms: Sequence[str], limit: int) -> str:
        if len(content) <= limit:
            return content
        lowered = content.casefold()
        positions = [
            position
            for term in query_terms
            if (position := lowered.find(term)) >= 0
        ]
        center = min(positions) if positions else 0
        prefix = "…" if center > 0 else ""
        start = max(0, center - max(0, (limit - len(prefix)) // 3))
        suffix = "…" if start + limit < len(content) else ""
        available = max(1, limit - len(prefix) - len(suffix))
        return f"{prefix}{content[start:start + available]}{suffix}"

    def _run(
        self,
        query: Mapping[str, Any],
        *,
        references: Sequence[Mapping[str, Any]],
        status: str,
        reason_code: str,
        scanned_documents: int,
        excluded_counts: Mapping[str, int],
    ) -> KnowledgeRetrievalRun:
        completed_at = format_time(self._utc_now())
        retrieval_ids = [reference["retrieval_id"] for reference in references]
        audit = {
            "schema_version": "1.0.0",
            "audit_id": stable_graph_id(
                "kaud",
                {
                    "request_id": query["request_id"],
                    "status": status,
                    "reason_code": reason_code,
                    "retrieval_method": query["retrieval_method"],
                    "retrieval_ids": retrieval_ids,
                },
            ),
            "request_id": query["request_id"],
            "incident_id": query["incident_id"],
            "context_id": query["context_id"],
            "retrieval_method": query["retrieval_method"],
            "status": status,
            "reason_code": reason_code,
            "requested_at": query["requested_at"],
            "completed_at": completed_at,
            "scanned_documents": scanned_documents,
            "returned_reference_ids": retrieval_ids,
            "excluded_counts": dict(sorted(excluded_counts.items())),
            "budget": {
                "top_k": query["top_k"],
                "character_limit": query["character_budget"],
                "characters_used": sum(
                    len(reference["bounded_excerpt"])
                    for reference in references
                ),
                "timeout_seconds": query["timeout_seconds"],
            },
        }
        validate_contract("knowledge-retrieval-audit.schema.json", audit)
        return KnowledgeRetrievalRun(
            query=copy.deepcopy(dict(query)),
            references=tuple(copy.deepcopy(dict(item)) for item in references),
            audit=audit,
        )

    @staticmethod
    def _increment(counts: Dict[str, int], key: str) -> None:
        counts[key] = counts.get(key, 0) + 1
