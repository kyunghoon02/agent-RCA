from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from incident_platform.knowledge import ReferenceDocument, SemanticSearchCandidate
from incident_platform.vector_knowledge import (
    KnowledgeVectorPolicy,
    PostgreSQLVectorKnowledgeIndex,
    chunk_markdown,
)


ROOT = Path(__file__).resolve().parents[1]
VECTOR_MIGRATION = ROOT / "db" / "vector_migrations" / "001_pgvector_knowledge.sql"


class StaticEmbeddingProvider:
    model_name = "fixture-embedding-1536"
    dimensions = 1536

    def embed(self, texts):
        return tuple(
            tuple([float(index + 1)] + [0.0] * 1535)
            for index, _ in enumerate(texts)
        )


class RecordingCursor:
    def __init__(self, rows=()) -> None:
        self.calls = []
        self.rows = list(rows)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement, parameters=()):
        self.calls.append((statement, parameters))

    def fetchall(self):
        return list(self.rows)


class RecordingConnection:
    closed = False

    def __init__(self, cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def document(content: str) -> ReferenceDocument:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return ReferenceDocument(
        {
            "schema_version": "1.0.0",
            "reference_document_id": "ref-vector-test-document-0001",
            "document_type": "runbook",
            "title": "Vector Test Runbook",
            "source_class": "operational-knowledge",
            "source_kind": "git-path",
            "source_path_or_uri": "documents/vector-test.md",
            "version": "1.0.0",
            "valid_from": "2026-08-01T00:00:00Z",
            "valid_to": None,
            "entity_keys": ["domain:kubernetes"],
            "content_hash": f"sha256:{digest}",
            "review_status": "approved",
            "sensitivity": "internal",
        },
        content,
    )


class VectorKnowledgeTests(unittest.TestCase):
    def test_migration_keeps_pgvector_optional_from_core_schema(self) -> None:
        sql = VECTOR_MIGRATION.read_text(encoding="utf-8")
        self.assertIn("CREATE EXTENSION IF NOT EXISTS vector", sql)
        self.assertIn("embedding VECTOR(1536) NOT NULL", sql)
        self.assertIn("USING hnsw (embedding vector_cosine_ops)", sql)

    def test_markdown_chunking_is_bounded_and_overlapping(self) -> None:
        content = " ".join(f"token-{index}" for index in range(300))
        chunks = chunk_markdown(content, max_characters=300, overlap_characters=40)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(1 <= len(chunk) <= 300 for chunk in chunks))
        self.assertTrue(set(chunks[0].split()) & set(chunks[1].split()))

    def test_vector_search_uses_hash_pinned_candidates_and_sql_parameters(self) -> None:
        item = document("A bounded Kubernetes runbook")
        cursor = RecordingCursor(
            rows=((item.metadata["reference_document_id"], item.metadata["content_hash"], 0.87),)
        )
        index = PostgreSQLVectorKnowledgeIndex(
            lambda: RecordingConnection(cursor),
            StaticEmbeddingProvider(),
        )

        hits = index.search(
            "pod memory pressure",
            candidates=(
                SemanticSearchCandidate(
                    item.metadata["reference_document_id"],
                    item.metadata["content_hash"],
                ),
            ),
            limit=5,
        )

        self.assertEqual(hits[0].score, 0.87)
        statement, parameters = cursor.calls[0]
        self.assertIn("jsonb_to_recordset(%s::jsonb)", statement)
        self.assertIn("<=> %s::vector", statement)
        self.assertNotIn("pod memory pressure", statement)
        self.assertIn(item.metadata["reference_document_id"], parameters[0])
        self.assertEqual(parameters[2], StaticEmbeddingProvider.model_name)
        self.assertEqual(parameters[3], 5)

    def test_vector_sync_replaces_only_current_model_document_rows(self) -> None:
        item = document("A short bounded runbook")
        cursor = RecordingCursor()
        index = PostgreSQLVectorKnowledgeIndex(
            lambda: RecordingConnection(cursor),
            StaticEmbeddingProvider(),
            policy=KnowledgeVectorPolicy(
                max_chunk_characters=200,
                chunk_overlap_characters=20,
            ),
        )

        result = index.sync_documents((item,))

        self.assertEqual(result.documents, 1)
        self.assertEqual(result.chunks, 1)
        self.assertEqual(len(cursor.calls), 2)
        delete_statement, delete_parameters = cursor.calls[0]
        insert_statement, insert_parameters = cursor.calls[1]
        self.assertIn("DELETE FROM operational_knowledge_chunks", delete_statement)
        self.assertEqual(delete_parameters[0], item.metadata["reference_document_id"])
        self.assertIn("INSERT INTO operational_knowledge_chunks", insert_statement)
        self.assertEqual(insert_parameters[1], item.metadata["content_hash"])
        self.assertTrue(insert_parameters[-1].startswith("["))


if __name__ == "__main__":
    unittest.main()
