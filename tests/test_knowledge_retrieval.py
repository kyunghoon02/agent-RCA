from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from incident_platform.contracts import validate_contract
from incident_platform.errors import ContractViolation, KnowledgeRepositoryError
from incident_platform.knowledge import (
    BoundedKnowledgeRetriever,
    GitReferenceDocumentRepository,
    KnowledgeRetrievalPolicy,
    ReferenceDocument,
)


UTC = timezone.utc
REQUESTED_AT = datetime(2026, 8, 22, 1, 0, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 8, 22, 1, 0, 1, tzinfo=UTC)


def localized_context() -> dict:
    pod = {
        "entity_id": "ent-pod-checkoutservice-0001",
        "entity_type": "Pod",
        "domain": "kubernetes",
        "name": "checkoutservice-7b8f6",
        "scope": {
            "cluster_id": "gcp-dev-01",
            "namespace": "online-boutique",
        },
        "external_ref": "k8s://online-boutique/Pod/checkoutservice-7b8f6",
        "exists": True,
    }
    config_map = {
        "entity_id": "ent-configmap-checkout-settings-0001",
        "entity_type": "ConfigMap",
        "domain": "kubernetes",
        "name": "checkout-settings",
        "scope": {
            "cluster_id": "gcp-dev-01",
            "namespace": "online-boutique",
        },
        "external_ref": "k8s://online-boutique/ConfigMap/checkout-settings",
        "exists": False,
    }
    return {
        "schema_version": "1.0.0",
        "context_id": "ctx-knowledge-fixture-0001",
        "incident_id": "inc-knowledge-fixture-0001",
        "frozen_at": "2026-08-22T00:59:00Z",
        "source_entity": pod,
        "scope": {
            "incident_id": "inc-knowledge-fixture-0001",
            "seed_entity_ids": [pod["entity_id"]],
            "domains": ["kubernetes"],
            "correlation_keys": {},
            "relation_types": ["REFERENCES"],
            "time_window": {
                "start": "2026-08-22T00:50:00Z",
                "end": "2026-08-22T00:59:00Z",
            },
            "max_entities": 2,
            "max_depth": 1,
        },
        "state_paths": [
            {
                "path_id": "path-knowledge-fixture-0001",
                "entities": [pod, config_map],
                "relations": ["REFERENCES"],
                "evidence_ids": ["ev-knowledge-fixture-0001"],
            }
        ],
        "evidence_ids": ["ev-knowledge-fixture-0001"],
        "recent_change_evidence_ids": [],
        "missing_evidence": [],
        "collector_failures": [],
        "localization": {
            "strategy": "stategraph",
            "candidate_entities_before": 2,
            "candidate_entities_after": 2,
            "context_completeness": 1.0,
        },
    }


def reference_metadata(
    content: str,
    *,
    review_status: str = "approved",
    valid_from: str = "2026-08-01T00:00:00Z",
    valid_to: str | None = None,
    source_path: str = "documents/test-runbook.md",
) -> dict:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "schema_version": "1.0.0",
        "reference_document_id": "ref-test-runbook-document-0001",
        "document_type": "runbook",
        "title": "Test runbook",
        "source_class": "operational-knowledge",
        "source_kind": "git-path",
        "source_path_or_uri": source_path,
        "version": "1.0.0",
        "valid_from": valid_from,
        "valid_to": valid_to,
        "entity_keys": ["domain:kubernetes"],
        "content_hash": f"sha256:{digest}",
        "review_status": review_status,
        "sensitivity": "internal",
    }


class StaticRepository:
    def __init__(self, documents: tuple[ReferenceDocument, ...]) -> None:
        self.documents = documents

    def list_documents(self, *, limit: int) -> tuple[ReferenceDocument, ...]:
        return self.documents


class FailingRepository:
    def __init__(self, message: str = "repository offline") -> None:
        self.message = message

    def list_documents(self, *, limit: int) -> tuple[ReferenceDocument, ...]:
        raise KnowledgeRepositoryError(self.message)


class AdvancingClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 10.0
        return self.value


def retriever(repository: object, **kwargs: object) -> BoundedKnowledgeRetriever:
    return BoundedKnowledgeRetriever(
        repository,  # type: ignore[arg-type]
        utc_now=lambda: COMPLETED_AT,
        **kwargs,
    )


class GitReferenceDocumentRepositoryTests(unittest.TestCase):
    def test_repository_loads_the_hash_pinned_operational_corpus(self) -> None:
        documents = GitReferenceDocumentRepository().list_documents(limit=500)

        self.assertEqual(len(documents), 2)
        self.assertTrue(all(item.metadata["review_status"] == "approved" for item in documents))

    def test_repository_rejects_a_content_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "documents").mkdir()
            content = "bounded Kubernetes runbook"
            (root / "documents" / "test-runbook.md").write_text(content, encoding="utf-8")
            metadata = reference_metadata(content)
            metadata["content_hash"] = f"sha256:{'0' * 64}"
            (root / "index.yaml").write_text(
                yaml.safe_dump({"schema_version": "1.0.0", "documents": [metadata]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ContractViolation, "content hash mismatch"):
                GitReferenceDocumentRepository(root, root / "index.yaml").list_documents(
                    limit=10
                )

    def test_repository_rejects_a_prohibited_corpus_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = reference_metadata(
                "fault answer", source_path="ground-truth/answer.md"
            )
            (root / "index.yaml").write_text(
                yaml.safe_dump({"schema_version": "1.0.0", "documents": [metadata]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ContractViolation, "prohibited corpus"):
                GitReferenceDocumentRepository(root, root / "index.yaml").list_documents(
                    limit=10
                )


class BoundedKnowledgeRetrieverTests(unittest.TestCase):
    def test_retrieval_is_derived_from_localized_entities_and_never_becomes_evidence(self) -> None:
        run = retriever(GitReferenceDocumentRepository()).retrieve(
            localized_context(),
            request_id="kreq-knowledge-fixture-0001",
            allowed_document_types=("runbook", "tool-guide"),
            query_terms=("FailedMount", "ConfigMap"),
            top_k=2,
            character_budget=500,
            requested_at=REQUESTED_AT,
        )

        validate_contract("knowledge-retrieval-query.schema.json", run.query)
        validate_contract("knowledge-retrieval-audit.schema.json", run.audit)
        for reference in run.references:
            validate_contract("retrieved-reference.schema.json", reference)
        self.assertEqual(run.audit["status"], "SUCCEEDED")
        self.assertEqual(len(run.references), 1)
        self.assertEqual(
            run.references[0]["reference_document_id"],
            "ref-kubernetes-workload-triage-0001",
        )
        self.assertIn("entity-type:pod", run.query["localized_entity_keys"])
        self.assertIn("scope-cluster-id:gcp-dev-01", run.query["localized_entity_keys"])
        self.assertEqual(run.references[0]["source_class"], "operational-knowledge")
        self.assertNotIn("evidence_id", run.references[0])
        self.assertLessEqual(run.audit["budget"]["characters_used"], 500)

    def test_retrieval_rejects_namespace_fallback_context(self) -> None:
        context = localized_context()
        context["localization"]["strategy"] = "namespace-fallback"

        with self.assertRaisesRegex(ContractViolation, "StateGraph-localized"):
            retriever(GitReferenceDocumentRepository()).retrieve(
                context,
                request_id="kreq-knowledge-fixture-0002",
                allowed_document_types=("runbook",),
                query_terms=("ConfigMap",),
                requested_at=REQUESTED_AT,
            )

    def test_retrieval_reports_only_stale_matching_documents(self) -> None:
        content = "Kubernetes runbook for ConfigMap checks"
        document = ReferenceDocument(
            reference_metadata(content, valid_to="2026-08-20T00:00:00Z"),
            content,
        )

        run = retriever(StaticRepository((document,))).retrieve(
            localized_context(),
            request_id="kreq-knowledge-fixture-0003",
            allowed_document_types=("runbook",),
            query_terms=("ConfigMap",),
            requested_at=REQUESTED_AT,
        )

        self.assertEqual(run.references, ())
        self.assertEqual(run.audit["status"], "STALE_ONLY")
        self.assertEqual(run.audit["reason_code"], "ONLY_STALE_MATCHES")

    def test_retrieval_distinguishes_timeout_and_repository_failure(self) -> None:
        timeout_run = retriever(
            StaticRepository(()), monotonic_clock=AdvancingClock()
        ).retrieve(
            localized_context(),
            request_id="kreq-knowledge-fixture-0004",
            allowed_document_types=("runbook",),
            query_terms=("ConfigMap",),
            timeout_seconds=1,
            requested_at=REQUESTED_AT,
        )
        failed_run = retriever(FailingRepository()).retrieve(
            localized_context(),
            request_id="kreq-knowledge-fixture-0005",
            allowed_document_types=("runbook",),
            query_terms=("ConfigMap",),
            requested_at=REQUESTED_AT,
        )

        self.assertEqual(timeout_run.audit["status"], "TIMED_OUT")
        self.assertEqual(failed_run.audit["status"], "FAILED")
        self.assertEqual(failed_run.audit["reason_code"], "REPOSITORY_UNAVAILABLE")

    def test_retrieval_enforces_request_and_index_budgets(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "Top-K"):
            retriever(StaticRepository(())).retrieve(
                localized_context(),
                request_id="kreq-knowledge-fixture-0006",
                allowed_document_types=("runbook",),
                query_terms=("ConfigMap",),
                top_k=6,
                requested_at=REQUESTED_AT,
            )

        index_run = retriever(
            FailingRepository("INDEX_BUDGET_EXCEEDED: too many documents")
        ).retrieve(
            localized_context(),
            request_id="kreq-knowledge-fixture-0007",
            allowed_document_types=("runbook",),
            query_terms=("ConfigMap",),
            requested_at=REQUESTED_AT,
        )
        self.assertEqual(index_run.audit["reason_code"], "INDEX_BUDGET_EXCEEDED")

    def test_draft_reference_is_not_returned(self) -> None:
        content = "Kubernetes runbook for ConfigMap checks"
        document = ReferenceDocument(
            reference_metadata(content, review_status="draft"), content
        )

        run = retriever(StaticRepository((document,))).retrieve(
            localized_context(),
            request_id="kreq-knowledge-fixture-0008",
            allowed_document_types=("runbook",),
            query_terms=("ConfigMap",),
            requested_at=REQUESTED_AT,
        )

        self.assertEqual(run.audit["status"], "NO_MATCH")
        self.assertEqual(run.audit["excluded_counts"], {"draft": 1})


class KnowledgeRetrievalPolicyTests(unittest.TestCase):
    def test_policy_cannot_expand_beyond_contract_caps(self) -> None:
        with self.assertRaises(ValueError):
            KnowledgeRetrievalPolicy(max_documents=6)


if __name__ == "__main__":
    unittest.main()
