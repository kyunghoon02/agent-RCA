#!/usr/bin/env python3
"""Verify the frozen Agent output boundary or one completed reliability run."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from incident_platform.agent_rca import OpenAIAgentsSDKRunner
from incident_platform.errors import ContractViolation
from tools.run_evaluation_matrix import MatrixError, load_matrix
from tools.summarize_evaluation_matrix import build_summary, load_matrix_records


ROOT = Path(__file__).resolve().parents[1]
PREREGISTRATION_PATH = (
    ROOT / "evaluation" / "structured-output-v2-preregistration.yaml"
)
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _load_preregistration() -> Mapping[str, Any]:
    try:
        value = yaml.safe_load(PREREGISTRATION_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise MatrixError("cannot load structured-output preregistration") from error
    if not isinstance(value, dict):
        raise MatrixError("structured-output preregistration must be one object")
    if (
        value.get("schema_version") != "1.0.0"
        or value.get("evaluation_id") != "structured-output-reliability-v2"
        or value.get("status") != "frozen-unexecuted"
    ):
        raise MatrixError("structured-output preregistration identity changed")
    if value.get("reuse_disclosure") != {
        "source_matrix": "evaluation/structured-output-v2-matrix.yaml",
        "source_matrix_id": "structured-output-reliability-v2",
        "source_matrix_sha256": (
            "236baf25491c9033f73c439c925d1767ffda0a0dc10729ad33c94e9a1aaea4d1"
        ),
        "known_scenarios_reused": True,
        "reason": (
            "reduce runtime while retaining two temporal passes per registered case"
        ),
        "combine_with_holdout_accuracy": False,
    }:
        raise MatrixError("structured-output reuse disclosure changed")
    if value.get("execution") != {
        "planned_attempts": 8,
        "unique_scenarios": 4,
        "repetitions_per_scenario": 2,
        "max_parallel": 1,
        "schedule": "rotated-round-robin",
        "failure_policy": "stop-and-preserve",
        "require_clean_worktree": True,
        "require_head_matches_origin_main": True,
        "explicit_confirmation_variable": "CONFIRM_STRUCTURED_OUTPUT_EVALUATION",
        "runtime_image_source": "platform/versions.yaml",
        "plan_command": "make plan-structured-output-evaluation",
        "execute_command": "make evaluate-structured-output-evaluation",
        "summarize_command": "make summarize-evaluation-matrix",
    }:
        raise MatrixError("structured-output execution boundary changed")
    if value.get("acceptance") != {
        "completed_attempts": 8,
        "scored_attempts": 8,
        "model_execution_failure_count": 0,
        "draft_contract_rejection_count": 0,
        "unsupported_evidence_citation_rate": 0,
        "evidence_gate_bypass_allowed": False,
    }:
        raise MatrixError("structured-output acceptance boundary changed")
    return value


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _tracked_content(source_commit: str, relative_path: str) -> bytes:
    if not _COMMIT_PATTERN.fullmatch(source_commit):
        raise MatrixError("evaluation source commit is invalid")
    completed = subprocess.run(
        ["git", "show", f"{source_commit}:{relative_path}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise MatrixError("cannot read frozen file from evaluation source commit")
    return completed.stdout


def _contains_keyword(value: Any, keyword: str) -> bool:
    if isinstance(value, Mapping):
        return keyword in value or any(
            _contains_keyword(child, keyword) for child in value.values()
        )
    if isinstance(value, list):
        return any(_contains_keyword(child, keyword) for child in value)
    return False


def verify_current_boundary(registration: Mapping[str, Any]) -> Mapping[str, Any]:
    reuse = registration["reuse_disclosure"]
    matrix_path = (ROOT / str(reuse["source_matrix"])).resolve()
    if (
        not matrix_path.is_file()
        or _sha256(matrix_path.read_bytes()) != reuse["source_matrix_sha256"]
    ):
        raise MatrixError("structured-output matrix changed after preregistration")
    boundary = registration.get("implementation_boundary", {})
    for path_key, digest_key in (
        ("agent_runtime_path", "agent_runtime_sha256"),
        ("frozen_draft_contract_path", "frozen_draft_contract_sha256"),
    ):
        relative_path = boundary.get(path_key)
        expected_digest = boundary.get(digest_key)
        if not isinstance(relative_path, str) or not isinstance(expected_digest, str):
            raise MatrixError("structured-output implementation boundary is incomplete")
        path = (ROOT / relative_path).resolve()
        try:
            path.relative_to(ROOT.resolve())
        except ValueError as error:
            raise MatrixError("structured-output boundary path escapes repository") from error
        if not path.is_file() or _sha256(path.read_bytes()) != expected_digest:
            raise MatrixError(f"structured-output boundary changed: {relative_path}")

    installed_versions = {
        "agents_sdk_version": importlib.metadata.version("openai-agents"),
        "openai_sdk_version": importlib.metadata.version("openai"),
    }
    for key, actual in installed_versions.items():
        if boundary.get(key) != actual:
            raise MatrixError(f"structured-output dependency changed: {key}")

    output_schema = OpenAIAgentsSDKRunner("boundary-validation").output_schema
    if not output_schema.is_strict_json_schema():
        raise MatrixError("Agent output schema is not strict")
    schema = output_schema.json_schema()
    for keyword in boundary.get("unsupported_contract_keywords_enforced_after_model_output", []):
        if _contains_keyword(schema, str(keyword)):
            raise MatrixError(f"unsupported keyword reached API schema: {keyword}")
    if boundary.get("evidence_gate_remains_independent") is not True:
        raise MatrixError("independent Evidence Gate boundary changed")
    return {
        "evaluation_id": registration["evaluation_id"],
        "status": "BOUNDARY_VERIFIED",
        "strict_json_schema": True,
        "post_output_evidence_gate": True,
    }


def _verify_source_boundary(
    registration: Mapping[str, Any], source_commit: str
) -> None:
    boundary = registration["implementation_boundary"]
    for path_key, digest_key in (
        ("agent_runtime_path", "agent_runtime_sha256"),
        ("frozen_draft_contract_path", "frozen_draft_contract_sha256"),
    ):
        relative_path = str(boundary[path_key])
        actual_digest = _sha256(_tracked_content(source_commit, relative_path))
        if actual_digest != boundary[digest_key]:
            raise MatrixError(
                f"evaluation source does not match preregistered {relative_path}"
            )


def evaluate_completed_run(
    registration: Mapping[str, Any], manifest_path: str
) -> Mapping[str, Any]:
    manifest, records = load_matrix_records(manifest_path, allow_incomplete=False)
    matrix = load_matrix("structured-output-v2")
    if manifest.get("matrix_id") != registration["reuse_disclosure"][
        "source_matrix_id"
    ]:
        raise MatrixError("manifest is not the preregistered source matrix")
    source_commit = str(manifest.get("source", {}).get("source_commit", ""))
    _verify_source_boundary(registration, source_commit)

    reason_codes: dict[str, int] = {}
    unsupported_citation_values: list[float] = []
    for record in records:
        reason_code = str(record["runtime"]["reason_code"])
        reason_codes[reason_code] = reason_codes.get(reason_code, 0) + 1
        value = record["result"]["metrics"][
            "unsupported_evidence_citation_rate"
        ]
        if value is not None:
            unsupported_citation_values.append(float(value))

    observed = {
        "completed_attempts": len(manifest["attempts"]),
        "scored_attempts": len(records),
        "model_execution_failure_count": reason_codes.get(
            "MODEL_EXECUTION_FAILED", 0
        ),
        "draft_contract_rejection_count": reason_codes.get(
            "GATE_DRAFT_CONTRACT_INVALID", 0
        ),
        "unsupported_evidence_citation_rate": (
            max(unsupported_citation_values)
            if unsupported_citation_values
            else None
        ),
    }
    acceptance = registration["acceptance"]
    passed = all(
        observed[key] == acceptance[key]
        for key in (
            "completed_attempts",
            "scored_attempts",
            "model_execution_failure_count",
            "draft_contract_rejection_count",
            "unsupported_evidence_citation_rate",
        )
    )
    aggregate = build_summary(
        matrix,
        manifest,
        records,
        generated_at=datetime.now(timezone.utc),
    )
    return {
        "schema_version": "1.0.0",
        "evaluation_id": registration["evaluation_id"],
        "status": "PASSED" if passed else "FAILED",
        "observed": observed,
        "agent_reason_codes": reason_codes,
        "secondary_regression": {
            "expected_outcome_rate": aggregate["expected_outcome_rate"],
        },
        "claim_boundary": "structured-output-contract-reliability-only",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify Agent RCA structured-output preregistration."
    )
    parser.add_argument(
        "--manifest",
        help="completed private regression matrix manifest to evaluate",
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        registration = _load_preregistration()
        result = (
            evaluate_completed_run(registration, arguments.manifest)
            if arguments.manifest
            else verify_current_boundary(registration)
        )
    except (
        ContractViolation,
        MatrixError,
        importlib.metadata.PackageNotFoundError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] != "FAILED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
