"""PostgreSQL/pgvector adapter for bounded Operational Knowledge retrieval."""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional, Protocol, Sequence, Tuple

from .errors import ContractViolation, KnowledgeRepositoryError
from .knowledge import (
    ReferenceDocument,
    SemanticSearchCandidate,
    SemanticSearchHit,
)


ConnectionFactory = Callable[[], Any]
DEFAULT_VECTOR_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[2] / "db" / "vector_migrations"
)


class EmbeddingProvider(Protocol):
    """Provider-neutral embedding boundary used by sync and query paths."""

    model_name: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> Tuple[Tuple[float, ...], ...]:
        ...


@dataclass(frozen=True)
class KnowledgeVectorPolicy:
    """Budgets that prevent unbounded chunking and embedding requests."""

    max_chunk_characters: int = 1_600
    chunk_overlap_characters: int = 200
    max_chunks_per_document: int = 64
    max_total_chunks: int = 4_000
    max_embedding_batch_size: int = 64

    def __post_init__(self) -> None:
        if not 200 <= self.max_chunk_characters <= 4_000:
            raise ValueError("Knowledge chunk size must be between 200 and 4000")
        if not 0 <= self.chunk_overlap_characters < self.max_chunk_characters:
            raise ValueError("Knowledge chunk overlap must be smaller than chunk size")
        if not 1 <= self.max_chunks_per_document <= 128:
            raise ValueError("Knowledge chunks per document must be between 1 and 128")
        if not 1 <= self.max_total_chunks <= 10_000:
            raise ValueError("Knowledge total chunk budget must be between 1 and 10000")
        if not 1 <= self.max_embedding_batch_size <= 256:
            raise ValueError("Knowledge embedding batch size must be between 1 and 256")


@dataclass(frozen=True)
class KnowledgeVectorSyncResult:
    documents: int
    chunks: int
    embedding_model: str
    dimensions: int


class OpenAIEmbeddingProvider:
    """Lazy OpenAI embeddings adapter; credentials remain environment-owned."""

    def __init__(
        self,
        *,
        client: Optional[Any] = None,
        model_name: str = "text-embedding-3-small",
        dimensions: int = 1_536,
    ) -> None:
        if not model_name.strip():
            raise ValueError("embedding model name must be non-empty")
        if dimensions != 1_536:
            raise ValueError("pgvector migration currently fixes embeddings at 1536")
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as error:
                raise RuntimeError("openai package is required for live embeddings") from error
            client = OpenAI()
        self._client = client
        self.model_name = model_name
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> Tuple[Tuple[float, ...], ...]:
        normalized = tuple(text for text in texts if isinstance(text, str) and text.strip())
        if len(normalized) != len(texts) or not normalized:
            raise ContractViolation("embedding input must contain non-empty strings")
        try:
            response = self._client.embeddings.create(
                model=self.model_name,
                input=list(normalized),
                dimensions=self.dimensions,
            )
        except Exception as error:
            raise KnowledgeRepositoryError("embedding provider request failed") from error
        rows = sorted(response.data, key=lambda item: item.index)
        if [row.index for row in rows] != list(range(len(normalized))):
            raise KnowledgeRepositoryError("embedding provider returned invalid indexes")
        vectors = tuple(tuple(float(value) for value in row.embedding) for row in rows)
        if len(vectors) != len(normalized):
            raise KnowledgeRepositoryError("embedding provider returned an incomplete batch")
        for vector in vectors:
            _validate_vector(vector, self.dimensions)
        return vectors


@contextmanager
def _connection(connection_factory: ConnectionFactory) -> Iterator[Any]:
    connection = connection_factory()
    try:
        with connection:
            yield connection
    finally:
        close = getattr(connection, "close", None)
        if callable(close) and not getattr(connection, "closed", False):
            close()


def apply_vector_migrations(
    connection_factory: ConnectionFactory,
    migrations_dir: Path = DEFAULT_VECTOR_MIGRATIONS_DIR,
) -> List[str]:
    """Apply opt-in pgvector migrations without changing core PostgreSQL setup."""

    paths = sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql"))
    if not paths:
        raise ValueError(f"no pgvector migrations found in {migrations_dir}")
    applied: List[str] = []
    with _connection(connection_factory) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS vector_schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            for path in paths:
                version = path.name
                cursor.execute(
                    "SELECT 1 FROM vector_schema_migrations WHERE version = %s",
                    (version,),
                )
                if cursor.fetchone() is not None:
                    continue
                cursor.execute(path.read_text(encoding="utf-8"))
                cursor.execute(
                    "INSERT INTO vector_schema_migrations (version) VALUES (%s)",
                    (version,),
                )
                applied.append(version)
    return applied


def chunk_markdown(
    content: str,
    *,
    max_characters: int = 1_600,
    overlap_characters: int = 200,
) -> Tuple[str, ...]:
    """Split a hash-verified document into deterministic overlapping text windows."""

    if not content.strip():
        raise ContractViolation("Operational Knowledge document must be non-empty")
    if max_characters < 200:
        raise ValueError("chunk size must be at least 200 characters")
    if not 0 <= overlap_characters < max_characters:
        raise ValueError("chunk overlap must be smaller than chunk size")
    normalized = content.strip()
    chunks: List[str] = []
    start = 0
    while start < len(normalized):
        hard_end = min(len(normalized), start + max_characters)
        end = hard_end
        if hard_end < len(normalized):
            search_floor = start + max_characters // 2
            newline = normalized.rfind("\n", search_floor, hard_end)
            space = normalized.rfind(" ", search_floor, hard_end)
            boundary = max(newline, space)
            if boundary > start:
                end = boundary
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(normalized):
            break
        next_start = max(0, end - overlap_characters)
        if next_start <= start:
            next_start = end
        start = next_start
    return tuple(chunks)


class PostgreSQLVectorKnowledgeIndex:
    """Hash-pinned pgvector chunk index behind the SemanticKnowledgeIndex port."""

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        embedding_provider: EmbeddingProvider,
        *,
        policy: Optional[KnowledgeVectorPolicy] = None,
    ) -> None:
        if embedding_provider.dimensions != 1_536:
            raise ValueError("pgvector migration currently fixes embeddings at 1536")
        self._connection_factory = connection_factory
        self._embedding_provider = embedding_provider
        self._policy = policy or KnowledgeVectorPolicy()

    def sync_documents(
        self,
        documents: Sequence[ReferenceDocument],
    ) -> KnowledgeVectorSyncResult:
        """Atomically replace current-model chunks for each hash-verified document."""

        prepared: List[Tuple[ReferenceDocument, int, str, str]] = []
        for document in documents:
            chunks = chunk_markdown(
                document.content,
                max_characters=self._policy.max_chunk_characters,
                overlap_characters=self._policy.chunk_overlap_characters,
            )
            if len(chunks) > self._policy.max_chunks_per_document:
                raise KnowledgeRepositoryError(
                    "Knowledge document exceeds its chunk budget"
                )
            title = document.metadata["title"]
            for index, chunk in enumerate(chunks):
                prepared.append((document, index, chunk, f"{title}\n\n{chunk}"))
        if len(prepared) > self._policy.max_total_chunks:
            raise KnowledgeRepositoryError("Knowledge corpus exceeds its chunk budget")

        vectors: List[Tuple[float, ...]] = []
        batch_size = self._policy.max_embedding_batch_size
        for offset in range(0, len(prepared), batch_size):
            texts = [item[3] for item in prepared[offset : offset + batch_size]]
            vectors.extend(self._embedding_provider.embed(texts))
        if len(vectors) != len(prepared):
            raise KnowledgeRepositoryError("embedding count does not match chunk count")

        try:
            with _connection(self._connection_factory) as connection:
                with connection.cursor() as cursor:
                    for document in documents:
                        metadata = document.metadata
                        cursor.execute(
                            """
                            DELETE FROM operational_knowledge_chunks
                            WHERE reference_document_id = %s
                              AND embedding_model = %s
                            """,
                            (
                                metadata["reference_document_id"],
                                self._embedding_provider.model_name,
                            ),
                        )
                    for (document, chunk_index, chunk, _), vector in zip(
                        prepared, vectors
                    ):
                        metadata = document.metadata
                        cursor.execute(
                            """
                            INSERT INTO operational_knowledge_chunks (
                                reference_document_id, content_hash, document_version,
                                chunk_index, chunk_text, embedding_model, embedding
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s::vector)
                            """,
                            (
                                metadata["reference_document_id"],
                                metadata["content_hash"],
                                metadata["version"],
                                chunk_index,
                                chunk,
                                self._embedding_provider.model_name,
                                _vector_literal(vector, self._embedding_provider.dimensions),
                            ),
                        )
        except KnowledgeRepositoryError:
            raise
        except Exception as error:
            raise KnowledgeRepositoryError("pgvector Knowledge sync failed") from error
        return KnowledgeVectorSyncResult(
            documents=len(documents),
            chunks=len(prepared),
            embedding_model=self._embedding_provider.model_name,
            dimensions=self._embedding_provider.dimensions,
        )

    def search(
        self,
        query_text: str,
        *,
        candidates: Sequence[SemanticSearchCandidate],
        limit: int,
    ) -> Tuple[SemanticSearchHit, ...]:
        if not query_text.strip():
            raise ContractViolation("semantic Knowledge query must be non-empty")
        if not 1 <= limit <= 500:
            raise ContractViolation("semantic Knowledge limit must be between 1 and 500")
        if not candidates:
            return ()
        if len(candidates) > 500:
            raise ContractViolation("semantic Knowledge candidate budget exceeded")
        identities = [
            {
                "reference_document_id": item.reference_document_id,
                "content_hash": item.content_hash,
            }
            for item in candidates
        ]
        if len({item["reference_document_id"] for item in identities}) != len(identities):
            raise ContractViolation("semantic Knowledge candidates must be unique")
        query_vector = self._embedding_provider.embed((query_text,))[0]
        vector_literal = _vector_literal(
            query_vector,
            self._embedding_provider.dimensions,
        )
        try:
            with _connection(self._connection_factory) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        WITH candidate_documents AS (
                            SELECT reference_document_id, content_hash
                            FROM jsonb_to_recordset(%s::jsonb) AS candidate(
                                reference_document_id TEXT,
                                content_hash TEXT
                            )
                        )
                        SELECT chunk.reference_document_id,
                               chunk.content_hash,
                               MAX(1 - (chunk.embedding <=> %s::vector)) AS score
                        FROM operational_knowledge_chunks AS chunk
                        JOIN candidate_documents AS candidate
                          ON candidate.reference_document_id = chunk.reference_document_id
                         AND candidate.content_hash = chunk.content_hash
                        WHERE chunk.embedding_model = %s
                        GROUP BY chunk.reference_document_id, chunk.content_hash
                        ORDER BY score DESC, chunk.reference_document_id
                        LIMIT %s
                        """,
                        (
                            json.dumps(identities, sort_keys=True),
                            vector_literal,
                            self._embedding_provider.model_name,
                            limit,
                        ),
                    )
                    rows = cursor.fetchall()
        except Exception as error:
            raise KnowledgeRepositoryError("pgvector Knowledge search failed") from error
        return tuple(
            SemanticSearchHit(
                reference_document_id=str(row[0]),
                content_hash=str(row[1]),
                score=float(row[2]),
            )
            for row in rows
        )


def _validate_vector(vector: Sequence[float], dimensions: int) -> None:
    if len(vector) != dimensions:
        raise KnowledgeRepositoryError(
            f"embedding dimension mismatch: expected {dimensions}, got {len(vector)}"
        )
    if any(not math.isfinite(float(value)) for value in vector):
        raise KnowledgeRepositoryError("embedding contains a non-finite value")


def _vector_literal(vector: Sequence[float], dimensions: int) -> str:
    _validate_vector(vector, dimensions)
    return "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"
