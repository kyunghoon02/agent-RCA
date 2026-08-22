from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

from incident_platform.contracts import validate_contract
from incident_platform.knowledge import GitReferenceDocumentRepository
from incident_platform.retrieval_evaluation import (
    RetrievalEvaluationCase,
    evaluate_rankings,
)


class RetrievalEvaluationTests(unittest.TestCase):
    def test_pilot_benchmark_is_frozen_against_the_approved_corpus(self) -> None:
        root = Path(__file__).resolve().parents[1]
        benchmark_path = root / "evaluation" / "knowledge-retrieval" / "benchmark.yaml"
        benchmark = yaml.safe_load(benchmark_path.read_text(encoding="utf-8"))
        context = json.loads(
            (benchmark_path.parent / benchmark["context_path"]).read_text(encoding="utf-8")
        )
        validate_contract("context-package.schema.json", context)
        known_ids = {
            item.metadata["reference_document_id"]
            for item in GitReferenceDocumentRepository().list_documents(limit=500)
        }

        self.assertEqual(benchmark["top_k"], 1)
        self.assertEqual(len(benchmark["cases"]), 12)
        for case in benchmark["cases"]:
            self.assertTrue(set(case["relevant_reference_ids"]) <= known_ids)

    def test_metrics_distinguish_first_rank_and_partial_recall(self) -> None:
        cases = (
            RetrievalEvaluationCase("case-a", ("ref-a",)),
            RetrievalEvaluationCase("case-b", ("ref-b", "ref-c")),
        )

        metrics = evaluate_rankings(
            cases,
            {
                "case-a": ("ref-x", "ref-a"),
                "case-b": ("ref-b", "ref-y"),
            },
            top_k=2,
        )

        self.assertEqual(metrics.hit_rate_at_k, 1.0)
        self.assertEqual(metrics.recall_at_k, 0.75)
        self.assertEqual(metrics.mrr_at_k, 0.75)
        self.assertGreater(metrics.ndcg_at_k, 0.0)
        self.assertLess(metrics.ndcg_at_k, 1.0)

    def test_metrics_require_the_same_frozen_case_set(self) -> None:
        with self.assertRaisesRegex(ValueError, "case mismatch"):
            evaluate_rankings(
                (RetrievalEvaluationCase("case-a", ("ref-a",)),),
                {"case-b": ("ref-a",)},
                top_k=1,
            )


if __name__ == "__main__":
    unittest.main()
