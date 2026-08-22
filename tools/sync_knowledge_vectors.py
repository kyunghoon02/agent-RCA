#!/usr/bin/env python3
"""Sync the approved, hash-pinned Git corpus into the opt-in pgvector index."""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

from incident_platform.knowledge import GitReferenceDocumentRepository
from incident_platform.vector_knowledge import (
    OpenAIEmbeddingProvider,
    PostgreSQLVectorKnowledgeIndex,
    apply_vector_migrations,
)


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    load_dotenv(ROOT / ".env")
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise SystemExit("POSTGRES_DSN is required for Knowledge vector sync")
    try:
        import psycopg
    except ImportError as error:
        raise SystemExit("psycopg is required for Knowledge vector sync") from error

    def connection_factory():
        return psycopg.connect(dsn)

    repository = GitReferenceDocumentRepository()
    documents = repository.list_documents(limit=500)
    embedding_provider = OpenAIEmbeddingProvider(
        model_name=os.environ.get(
            "KNOWLEDGE_EMBEDDING_MODEL",
            "text-embedding-3-small",
        )
    )
    index = PostgreSQLVectorKnowledgeIndex(connection_factory, embedding_provider)
    applied = apply_vector_migrations(connection_factory)
    result = index.sync_documents(documents)
    print(
        json.dumps(
            {
                "applied_vector_migrations": applied,
                "documents": result.documents,
                "chunks": result.chunks,
                "embedding_model": result.embedding_model,
                "dimensions": result.dimensions,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
