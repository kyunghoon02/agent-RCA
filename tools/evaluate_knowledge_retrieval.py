#!/usr/bin/env python3
"""Compare lexical, vector, and hybrid retrieval on one frozen benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, Mapping, Sequence

import yaml
from dotenv import load_dotenv

from incident_platform.contracts import validate_contract
from incident_platform.knowledge import (
    BoundedKnowledgeRetriever,
    GitReferenceDocumentRepository,
)
from incident_platform.retrieval_evaluation import (
    RetrievalEvaluationCase,
    evaluate_rankings,
)
from incident_platform.stategraph import stable_graph_id
from incident_platform.vector_knowledge import (
    OpenAIEmbeddingProvider,
    PostgreSQLVectorKnowledgeIndex,
    apply_vector_migrations,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BENCHMARK = ROOT / "evaluation" / "knowledge-retrieval" / "benchmark.yaml"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _load_benchmark(path: Path) -> tuple[Dict[str, Any], Dict[str, Any]]:
    benchmark_path = path.resolve()
    raw = yaml.safe_load(benchmark_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("Knowledge retrieval benchmark must be an object")
    benchmark = dict(raw)
    required = {
        "schema_version",
        "benchmark_id",
        "context_path",
        "requested_at",
        "top_k",
        "allowed_document_types",
        "cases",
    }
    missing = sorted(required - set(benchmark))
    if missing:
        raise ValueError(f"Knowledge retrieval benchmark is missing {missing}")
    context_path = (benchmark_path.parent / benchmark["context_path"]).resolve()
    try:
        context_path.relative_to(benchmark_path.parent)
    except ValueError as error:
        raise ValueError("Knowledge retrieval context escapes its benchmark") from error
    context = json.loads(context_path.read_text(encoding="utf-8"))
    validate_contract("context-package.schema.json", context)
    return benchmark, context


def _p95(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _benchmark_fingerprint(
    benchmark_path: Path,
    context: Mapping[str, Any],
    documents: Sequence[Any],
    embedding_model: str,
    embedding_dimensions: int,
) -> str:
    digest = hashlib.sha256()
    digest.update(benchmark_path.read_bytes())
    digest.update(json.dumps(context, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest.update(embedding_model.encode("utf-8"))
    digest.update(str(embedding_dimensions).encode("ascii"))
    digest.update((ROOT / "src" / "incident_platform" / "knowledge.py").read_bytes())
    digest.update((ROOT / "src" / "incident_platform" / "vector_knowledge.py").read_bytes())
    for document in sorted(
        documents,
        key=lambda item: item.metadata["reference_document_id"],
    ):
        digest.update(document.metadata["reference_document_id"].encode("utf-8"))
        digest.update(document.metadata["content_hash"].encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def main() -> int:
    args = _arguments()
    load_dotenv(ROOT / ".env")
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise SystemExit("POSTGRES_DSN is required for Knowledge retrieval evaluation")
    try:
        import psycopg
    except ImportError as error:
        raise SystemExit("psycopg is required for Knowledge retrieval evaluation") from error

    def connection_factory():
        return psycopg.connect(dsn)

    benchmark_path = args.benchmark.resolve()
    benchmark, context = _load_benchmark(benchmark_path)
    requested_at = datetime.fromisoformat(benchmark["requested_at"].replace("Z", "+00:00"))
    top_k = int(benchmark["top_k"])
    cases = tuple(
        RetrievalEvaluationCase(
            case_id=item["case_id"],
            relevant_reference_ids=tuple(item["relevant_reference_ids"]),
        )
        for item in benchmark["cases"]
    )
    repository = GitReferenceDocumentRepository()
    documents = repository.list_documents(limit=500)
    embedding_provider = OpenAIEmbeddingProvider(
        model_name=os.environ.get(
            "KNOWLEDGE_EMBEDDING_MODEL",
            "text-embedding-3-small",
        )
    )
    vector_index = PostgreSQLVectorKnowledgeIndex(
        connection_factory,
        embedding_provider,
    )
    applied_migrations = []
    sync_result = None
    if args.sync:
        applied_migrations = apply_vector_migrations(connection_factory)
        sync_result = vector_index.sync_documents(documents)

    methods = {
        "lexical": BoundedKnowledgeRetriever(repository),
        "vector": BoundedKnowledgeRetriever(
            repository,
            semantic_index=vector_index,
            retrieval_method="entity-key+vector",
        ),
        "hybrid": BoundedKnowledgeRetriever(
            repository,
            semantic_index=vector_index,
            retrieval_method="entity-key+lexical+vector-rrf",
        ),
    }
    method_results: Dict[str, Dict[str, Any]] = {}
    for method_name, retriever in methods.items():
        rankings: Dict[str, Sequence[str]] = {}
        latencies_ms = []
        statuses: Dict[str, str] = {}
        for item in benchmark["cases"]:
            started = perf_counter()
            run = retriever.retrieve(
                context,
                request_id=stable_graph_id(
                    "kreq",
                    {
                        "benchmark_id": benchmark["benchmark_id"],
                        "method": method_name,
                        "case_id": item["case_id"],
                    },
                ),
                allowed_document_types=tuple(benchmark["allowed_document_types"]),
                query_terms=tuple(item["query_terms"]),
                top_k=top_k,
                timeout_seconds=5.0,
                requested_at=requested_at,
            )
            latencies_ms.append((perf_counter() - started) * 1_000)
            statuses[item["case_id"]] = run.audit["status"]
            rankings[item["case_id"]] = tuple(
                reference["reference_document_id"] for reference in run.references
            )
        failed = sorted(
            case_id
            for case_id, status in statuses.items()
            if status in {"FAILED", "TIMED_OUT"}
        )
        if failed:
            raise RuntimeError(
                f"{method_name} retrieval failed for benchmark cases: {failed}"
            )
        metrics = evaluate_rankings(cases, rankings, top_k=top_k)
        method_results[method_name] = {
            **metrics.as_dict(),
            "p95_latency_ms": round(_p95(latencies_ms), 3),
            "statuses": dict(sorted(statuses.items())),
            "rankings": {key: list(value) for key, value in sorted(rankings.items())},
        }

    metric_names = ("hit_rate_at_k", "recall_at_k", "mrr_at_k", "ndcg_at_k")
    deltas = {
        metric: round(
            method_results["hybrid"][metric] - method_results["lexical"][metric],
            6,
        )
        for metric in metric_names
    }
    claim_ready = len(documents) >= 20 and len(cases) >= 30
    report = {
        "schema_version": "1.0.0",
        "benchmark_id": benchmark["benchmark_id"],
        "benchmark_fingerprint": _benchmark_fingerprint(
            benchmark_path,
            context,
            documents,
            embedding_provider.model_name,
            embedding_provider.dimensions,
        ),
        "corpus_documents": len(documents),
        "queries": len(cases),
        "embedding_model": embedding_provider.model_name,
        "embedding_dimensions": embedding_provider.dimensions,
        "top_k": top_k,
        "methods": method_results,
        "hybrid_minus_lexical": deltas,
        "portfolio_claim_ready": claim_ready,
        "limitations": (
            []
            if claim_ready
            else [
                "Pilot corpus/result only; expand to at least 20 reviewed documents and 30 frozen queries before publishing an uplift claim."
            ]
        ),
        "applied_vector_migrations": applied_migrations,
        "synced_chunks": None if sync_result is None else sync_result.chunks,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
