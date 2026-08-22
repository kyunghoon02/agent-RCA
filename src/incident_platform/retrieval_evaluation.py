"""Deterministic ranking metrics for Operational Knowledge retrieval."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Mapping, Sequence, Tuple


@dataclass(frozen=True)
class RetrievalEvaluationCase:
    case_id: str
    relevant_reference_ids: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("retrieval evaluation case_id must be non-empty")
        if not self.relevant_reference_ids:
            raise ValueError("retrieval evaluation needs at least one relevant reference")
        if len(set(self.relevant_reference_ids)) != len(self.relevant_reference_ids):
            raise ValueError("relevant reference IDs must be unique")


@dataclass(frozen=True)
class RetrievalMetrics:
    queries: int
    top_k: int
    hit_rate_at_k: float
    recall_at_k: float
    mrr_at_k: float
    ndcg_at_k: float

    def as_dict(self) -> Dict[str, float | int]:
        return {
            "queries": self.queries,
            "top_k": self.top_k,
            "hit_rate_at_k": round(self.hit_rate_at_k, 6),
            "recall_at_k": round(self.recall_at_k, 6),
            "mrr_at_k": round(self.mrr_at_k, 6),
            "ndcg_at_k": round(self.ndcg_at_k, 6),
        }


def evaluate_rankings(
    cases: Sequence[RetrievalEvaluationCase],
    rankings: Mapping[str, Sequence[str]],
    *,
    top_k: int,
) -> RetrievalMetrics:
    """Calculate macro metrics from one frozen case set and one retrieval method."""

    if not cases:
        raise ValueError("retrieval evaluation requires at least one case")
    if not 1 <= top_k <= 100:
        raise ValueError("retrieval evaluation top_k must be between 1 and 100")
    expected_case_ids = {case.case_id for case in cases}
    if set(rankings) != expected_case_ids:
        missing = sorted(expected_case_ids - set(rankings))
        unexpected = sorted(set(rankings) - expected_case_ids)
        raise ValueError(
            f"retrieval ranking case mismatch: missing={missing}, unexpected={unexpected}"
        )

    hits = 0.0
    recall = 0.0
    reciprocal_rank = 0.0
    ndcg = 0.0
    for case in cases:
        relevant = set(case.relevant_reference_ids)
        ranking = tuple(rankings[case.case_id][:top_k])
        if len(set(ranking)) != len(ranking):
            raise ValueError(f"duplicate result in retrieval case {case.case_id}")
        matched = relevant & set(ranking)
        hits += 1.0 if matched else 0.0
        recall += len(matched) / len(relevant)
        first_rank = next(
            (rank for rank, reference_id in enumerate(ranking, start=1) if reference_id in relevant),
            None,
        )
        reciprocal_rank += 0.0 if first_rank is None else 1.0 / first_rank
        dcg = sum(
            1.0 / math.log2(rank + 1)
            for rank, reference_id in enumerate(ranking, start=1)
            if reference_id in relevant
        )
        ideal_count = min(len(relevant), top_k)
        ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
        ndcg += dcg / ideal_dcg

    count = len(cases)
    return RetrievalMetrics(
        queries=count,
        top_k=top_k,
        hit_rate_at_k=hits / count,
        recall_at_k=recall / count,
        mrr_at_k=reciprocal_rank / count,
        ndcg_at_k=ndcg / count,
    )
