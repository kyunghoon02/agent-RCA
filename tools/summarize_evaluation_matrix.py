#!/usr/bin/env python3
"""Build an ID-free aggregate from one private evaluation matrix run."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from incident_platform.contracts import validate_contract
from incident_platform.errors import ContractViolation
from tools.run_evaluation_matrix import (
    PRIVATE_MATRIX_ROOT,
    PRIVATE_RUN_ROOT,
    ROOT,
    MatrixError,
    build_schedule,
    load_matrix,
)


METRIC_NAMES = (
    "root_cause_top1_accuracy",
    "root_cause_top3_recall",
    "multi_factor_exact_match",
    "multi_factor_partial_match",
    "evidence_precision",
    "evidence_recall",
    "unsupported_evidence_citation_rate",
    "abstention_correctness",
)
USAGE_NAMES = (
    "llm_calls",
    "tool_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _confined_path(value: str, *, root: Path, suffix: str) -> Path:
    candidate = (ROOT / value).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise MatrixError("matrix input path escapes its private root") from error
    if not candidate.is_file() or not candidate.name.endswith(suffix):
        raise MatrixError("matrix input path is missing or has an unexpected suffix")
    return candidate


def _load_object(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MatrixError("matrix artifact is not valid JSON") from error
    if not isinstance(value, dict):
        raise MatrixError("matrix artifact must contain one object")
    return value


def load_matrix_records(
    manifest_value: str, *, allow_incomplete: bool
) -> tuple[Mapping[str, Any], list[dict[str, Mapping[str, Any]]]]:
    manifest_path = _confined_path(
        manifest_value, root=PRIVATE_MATRIX_ROOT, suffix="manifest.json"
    )
    manifest = _load_object(manifest_path)
    matrix = load_matrix()
    expected_schedule = build_schedule(matrix)
    if manifest.get("schema_version") != "1.0.0":
        raise MatrixError("matrix manifest schema is unsupported")
    if manifest.get("matrix_id") != matrix["matrix_id"]:
        raise MatrixError("matrix manifest belongs to another matrix")
    if manifest.get("schedule") != expected_schedule:
        raise MatrixError("matrix manifest schedule differs from the frozen matrix")
    attempts = manifest.get("attempts")
    if not isinstance(attempts, list) or len(attempts) > len(expected_schedule):
        raise MatrixError("matrix manifest attempt list is invalid")
    if not allow_incomplete and (
        manifest.get("state") not in {"COMPLETED", "COMPLETED_WITH_FAILURES"}
        or len(attempts) != len(expected_schedule)
    ):
        raise MatrixError("matrix is incomplete; use --allow-incomplete to inspect it")

    records: list[dict[str, Mapping[str, Any]]] = []
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, dict):
            raise MatrixError("matrix attempt is not an object")
        planned = expected_schedule[index]
        if any(attempt.get(key) != planned[key] for key in (
            "attempt", "repetition", "scenario_id", "expected_outcome"
        )):
            raise MatrixError("matrix attempt order differs from its frozen schedule")
        paths = {
            "prediction": attempt.get("agent_prediction_path"),
            "result": attempt.get("agent_result_path"),
            "runtime": attempt.get("agent_runtime_path"),
        }
        has_all_paths = all(isinstance(value, str) for value in paths.values())
        has_any_path = any(isinstance(value, str) for value in paths.values())
        if attempt.get("state") == "PASSED" and not has_all_paths:
            raise MatrixError("passed matrix attempt omits an artifact path")
        if has_any_path and not has_all_paths:
            raise MatrixError("matrix attempt contains an incomplete artifact set")
        if not has_all_paths:
            continue
        prediction = _load_object(
            _confined_path(
                paths["prediction"],
                root=PRIVATE_RUN_ROOT,
                suffix=".agent.prediction.json",
            )
        )
        result = _load_object(
            _confined_path(
                paths["result"],
                root=PRIVATE_RUN_ROOT,
                suffix=".agent.result.json",
            )
        )
        runtime = _load_object(
            _confined_path(
                paths["runtime"],
                root=PRIVATE_RUN_ROOT,
                suffix=".agent.runtime.json",
            )
        )
        validate_contract("rca-evaluation-prediction.schema.json", prediction)
        validate_contract("rca-evaluation-result.schema.json", result)
        validate_contract("rca-evaluation-agent-runtime.schema.json", runtime)
        identities = {
            (item["evaluation_case_id"], item["scenario_id"], item["incident_id"])
            for item in (prediction, result, runtime)
        }
        if len(identities) != 1 or result["scenario_id"] != attempt["scenario_id"]:
            raise MatrixError("matrix artifacts do not describe the same evaluation case")
        records.append(
            {
                "attempt": attempt,
                "prediction": prediction,
                "result": result,
                "runtime": runtime,
            }
        )
    return manifest, records


def _bootstrap_95(values: Sequence[float], *, seed: str) -> list[float] | None:
    if not values:
        return None
    generator = random.Random(
        int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16], 16)
    )
    sample_means = sorted(
        statistics.fmean(generator.choice(values) for _ in values)
        for _ in range(10_000)
    )
    return [round(sample_means[249], 6), round(sample_means[9749], 6)]


def _numeric_summary(values: Sequence[float]) -> Mapping[str, float] | None:
    if not values:
        return None
    return {
        "minimum": round(min(values), 3),
        "median": round(statistics.median(values), 3),
        "mean": round(statistics.fmean(values), 3),
        "maximum": round(max(values), 3),
    }


def build_summary(
    matrix: Mapping[str, Any],
    manifest: Mapping[str, Any],
    records: Sequence[Mapping[str, Mapping[str, Any]]],
    *,
    generated_at: datetime,
) -> Mapping[str, Any]:
    attempts = manifest["attempts"]
    summaries: list[dict[str, Any]] = []
    for configured in matrix["scenarios"]:
        scenario_id = configured["scenario_id"]
        scenario_attempts = [
            attempt for attempt in attempts if attempt["scenario_id"] == scenario_id
        ]
        scenario_records = [
            record
            for record in records
            if record["attempt"]["scenario_id"] == scenario_id
        ]
        prediction_outcomes: dict[str, int] = {}
        agent_statuses: dict[str, int] = {}
        for record in scenario_records:
            outcome = str(record["prediction"]["outcome"])
            prediction_outcomes[outcome] = prediction_outcomes.get(outcome, 0) + 1
            status = str(record["runtime"]["agent_status"])
            agent_statuses[status] = agent_statuses.get(status, 0) + 1

        metric_summaries: dict[str, Any] = {}
        for metric_name in METRIC_NAMES:
            values = [
                float(record["result"]["metrics"][metric_name])
                for record in scenario_records
                if record["result"]["metrics"][metric_name] is not None
            ]
            metric_summaries[metric_name] = (
                {
                    "applicable_runs": len(values),
                    "mean": round(statistics.fmean(values), 6),
                    "bootstrap_95": _bootstrap_95(
                        values, seed=f"{scenario_id}:{metric_name}"
                    ),
                }
                if values
                else {"applicable_runs": 0, "mean": None, "bootstrap_95": None}
            )

        usage = {
            name: {
                "total": sum(int(record["runtime"]["usage"][name]) for record in scenario_records),
                "mean": (
                    round(
                        statistics.fmean(
                            int(record["runtime"]["usage"][name])
                            for record in scenario_records
                        ),
                        3,
                    )
                    if scenario_records
                    else None
                ),
            }
            for name in USAGE_NAMES
        }
        latencies = {
            "ingest_to_agent_start_ms": _numeric_summary(
                [float(record["runtime"]["ingest_to_agent_start_ms"]) for record in scenario_records]
            ),
            "agent_wall_time_ms": _numeric_summary(
                [float(record["runtime"]["usage"]["wall_time_ms"]) for record in scenario_records]
            ),
            "ingest_to_terminal_ms": _numeric_summary(
                [float(record["runtime"]["ingest_to_terminal_ms"]) for record in scenario_records]
            ),
            "ingest_to_report_ms": _numeric_summary(
                [
                    float(record["runtime"]["ingest_to_report_ms"])
                    for record in scenario_records
                    if record["runtime"]["ingest_to_report_ms"] is not None
                ]
            ),
        }
        expected_matches = sum(
            record["prediction"]["outcome"] == configured["expected_outcome"]
            for record in scenario_records
        )
        summaries.append(
            {
                "scenario_id": scenario_id,
                "expected_outcome": configured["expected_outcome"],
                "planned_runs": matrix["repetitions_per_scenario"],
                "attempted_runs": len(scenario_attempts),
                "harness_passed_runs": sum(
                    attempt["state"] == "PASSED" for attempt in scenario_attempts
                ),
                "harness_failed_runs": sum(
                    attempt["state"] == "FAILED" for attempt in scenario_attempts
                ),
                "scored_runs": len(scenario_records),
                "expected_outcome_matches": expected_matches,
                "expected_outcome_rate": (
                    round(expected_matches / len(scenario_records), 6)
                    if scenario_records
                    else None
                ),
                "prediction_outcomes": prediction_outcomes,
                "agent_statuses": agent_statuses,
                "metrics": metric_summaries,
                "latency_ms": latencies,
                "usage": usage,
            }
        )

    return {
        "schema_version": "1.0.0",
        "matrix_id": matrix["matrix_id"],
        "generated_at": _format_time(generated_at),
        "matrix_state": manifest["state"],
        "source_commit": manifest["source"]["source_commit"],
        "runtime_image_tag": manifest["source"]["runtime_image_tag"],
        "runtime_image_digest": manifest["source"]["runtime_image_digest"],
        "planned_attempts": len(manifest["schedule"]),
        "attempted_runs": len(attempts),
        "harness_passed_runs": sum(item["state"] == "PASSED" for item in attempts),
        "harness_failed_runs": sum(item["state"] == "FAILED" for item in attempts),
        "scored_runs": len(records),
        "confidence_interval": "deterministic-bootstrap-95-10000-resamples",
        "cost": {
            "status": "NOT_CALCULATED",
            "reason": "no-frozen-model-rate-card",
        },
        "scenarios": summaries,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build an ID-free summary from a private matrix manifest."
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--allow-incomplete", action="store_true")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        matrix = load_matrix()
        manifest, records = load_matrix_records(
            arguments.manifest, allow_incomplete=arguments.allow_incomplete
        )
        summary = build_summary(
            matrix,
            manifest,
            records,
            generated_at=datetime.now(timezone.utc),
        )
    except (MatrixError, ContractViolation) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
