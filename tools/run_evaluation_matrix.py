#!/usr/bin/env python3
"""Plan or execute a registered evaluation matrix safely."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from incident_platform.contracts import validate_contract
from incident_platform.errors import ContractViolation


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "evaluation" / "matrix.yaml"
HOLDOUT_MATRIX_PATH = ROOT / "evaluation" / "holdout-v1-matrix.yaml"
PREREGISTRATION_PATH = ROOT / "evaluation" / "preregistration.yaml"
HOLDOUT_PREREGISTRATION_PATH = (
    ROOT / "evaluation" / "holdout-v1-preregistration.yaml"
)
VERSIONS_PATH = ROOT / "platform" / "versions.yaml"
PRIVATE_RUN_ROOT = ROOT / "evaluation" / "runs" / "private"
PRIVATE_MATRIX_ROOT = PRIVATE_RUN_ROOT / "matrix"
CONFIRMATION_VARIABLE = "CONFIRM_EVALUATION_MATRIX"
HOLDOUT_CONFIRMATION_VARIABLE = "CONFIRM_HOLDOUT_EVALUATION_MATRIX"
DEFAULT_MATRIX_NAME = "regression-v1"

REGISTERED_MATRICES = {
    DEFAULT_MATRIX_NAME: {
        "path": MATRIX_PATH,
        "matrix_id": "focused-four-scenario-v1",
        "schema_version": "1.0.0",
        "confirmation_variable": CONFIRMATION_VARIABLE,
    },
    "holdout-v1": {
        "path": HOLDOUT_MATRIX_PATH,
        "matrix_id": "focused-holdout-v1",
        "schema_version": "1.1.0",
        "confirmation_variable": HOLDOUT_CONFIRMATION_VARIABLE,
    },
}

ALLOWED_SCENARIOS = {
    "scenario-checkoutservice-oom-chaos-mesh-change-stress": {
        "scenario_path": "evaluation/scenarios/checkoutservice-oom.yaml",
        "make_target": "evaluate-checkout-oom",
        "confirmation_variable": "CONFIRM_CONTROLLED_FAULT",
        "expected_outcome": "ROOT_CAUSE",
    },
    "scenario-paymentservice-image-pull-change-normal": {
        "scenario_path": "evaluation/scenarios/paymentservice-image-pull.yaml",
        "make_target": "evaluate-payment-image-pull",
        "confirmation_variable": "CONFIRM_CONTROLLED_FAULT",
        "expected_outcome": "ROOT_CAUSE",
    },
    "scenario-checkoutservice-missing-configmap-normal": {
        "scenario_path": "evaluation/scenarios/checkoutservice-missing-configmap.yaml",
        "make_target": "evaluate-checkout-missing-configmap",
        "confirmation_variable": "CONFIRM_CONTROLLED_FAULT",
        "expected_outcome": "ROOT_CAUSE",
    },
    "scenario-frontend-no-fault-normal": {
        "scenario_path": "evaluation/scenarios/frontend-no-fault-normal.yaml",
        "make_target": "evaluate-no-fault-control",
        "confirmation_variable": "CONFIRM_NO_FAULT_CONTROL",
        "expected_outcome": "ABSTAIN",
    },
}

HOLDOUT_FAMILIES = {
    "kubernetes.container-oomkilled": {
        "scenario_prefix": "scenario-holdout-v1-checkout-oom-",
        "schema": "holdout-controlled-oom-scenario.schema.json",
        "make_target": "evaluate-checkout-oom",
        "scenario_path_variable": "CHECKOUT_OOM_SCENARIO_PATH",
        "confirmation_variable": "CONFIRM_CONTROLLED_FAULT",
        "expected_outcome": "ROOT_CAUSE",
    },
    "kubernetes.image-pull-failure": {
        "scenario_prefix": "scenario-holdout-v1-payment-image-pull-",
        "schema": "holdout-controlled-image-pull-scenario.schema.json",
        "make_target": "evaluate-payment-image-pull",
        "scenario_path_variable": "PAYMENT_IMAGE_PULL_SCENARIO_PATH",
        "confirmation_variable": "CONFIRM_CONTROLLED_FAULT",
        "expected_outcome": "ROOT_CAUSE",
    },
    "kubernetes.missing-configmap": {
        "scenario_prefix": "scenario-holdout-v1-checkout-missing-configmap-",
        "schema": "holdout-controlled-missing-configmap-scenario.schema.json",
        "make_target": "evaluate-checkout-missing-configmap",
        "scenario_path_variable": "CHECKOUT_MISSING_CONFIGMAP_SCENARIO_PATH",
        "confirmation_variable": "CONFIRM_CONTROLLED_FAULT",
        "expected_outcome": "ROOT_CAUSE",
    },
    "no-fault": {
        "scenario_prefix": "scenario-holdout-v1-frontend-no-fault-",
        "schema": "holdout-no-fault-control-scenario.schema.json",
        "make_target": "evaluate-no-fault-control",
        "scenario_path_variable": "NO_FAULT_CONTROL_SCENARIO_PATH",
        "confirmation_variable": "CONFIRM_NO_FAULT_CONTROL",
        "expected_outcome": "ABSTAIN",
    },
}


class MatrixError(ValueError):
    """Raised when the matrix or its execution boundary is invalid."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise MatrixError(f"cannot load {path.relative_to(ROOT)}") from error
    if not isinstance(value, dict):
        raise MatrixError(f"{path.relative_to(ROOT)} must contain one object")
    return value


def _matrix_registration(matrix_name: str) -> Mapping[str, Any]:
    try:
        return REGISTERED_MATRICES[matrix_name]
    except KeyError as error:
        raise MatrixError("evaluation matrix name is not registered") from error


def matrix_name_from_id(matrix_id: object) -> str:
    matches = [
        name
        for name, registration in REGISTERED_MATRICES.items()
        if registration["matrix_id"] == matrix_id
    ]
    if len(matches) != 1:
        raise MatrixError("matrix manifest has an unregistered matrix_id")
    return matches[0]


def _validate_execution_policy(matrix: Mapping[str, Any]) -> None:
    execution = matrix["execution"]
    if not isinstance(execution, dict) or execution != {
        "schedule": "rotated-round-robin",
        "max_parallel": 1,
        "failure_policy": "stop-and-preserve",
        "require_clean_worktree": True,
        "require_head_matches_origin_main": True,
    }:
        raise MatrixError("evaluation matrix execution policy is not frozen")


def _validate_regression_matrix(matrix: Mapping[str, Any]) -> None:
    preregistration = _load_yaml(PREREGISTRATION_PATH)
    registered_repetitions = preregistration.get("dataset", {}).get(
        "runtime_repetitions_per_scenario"
    )
    repetitions = matrix["repetitions_per_scenario"]
    if repetitions != 5 or repetitions != registered_repetitions:
        raise MatrixError("matrix repetitions differ from preregistration")

    scenarios = matrix["scenarios"]
    if not isinstance(scenarios, list) or len(scenarios) != len(ALLOWED_SCENARIOS):
        raise MatrixError("evaluation matrix must contain exactly four scenarios")
    seen: set[str] = set()
    required_scenario = {
        "scenario_id",
        "scenario_path",
        "make_target",
        "confirmation_variable",
        "expected_outcome",
    }
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != required_scenario:
            raise MatrixError("evaluation matrix scenario structure is invalid")
        scenario_id = scenario["scenario_id"]
        if scenario_id in seen or scenario_id not in ALLOWED_SCENARIOS:
            raise MatrixError("evaluation matrix scenario is duplicated or unregistered")
        if {key: scenario[key] for key in ALLOWED_SCENARIOS[scenario_id]} != (
            ALLOWED_SCENARIOS[scenario_id]
        ):
            raise MatrixError("evaluation matrix scenario boundary was modified")
        scenario_document = _load_yaml(ROOT / scenario["scenario_path"])
        if scenario_document.get("scenario_id") != scenario_id:
            raise MatrixError("evaluation matrix scenario_id does not match its file")
        seen.add(scenario_id)
    if seen != set(ALLOWED_SCENARIOS):
        raise MatrixError("evaluation matrix does not match the frozen scenario set")


def _validate_holdout_preregistration(matrix: Mapping[str, Any]) -> None:
    preregistration = _load_yaml(HOLDOUT_PREREGISTRATION_PATH)
    if (
        preregistration.get("schema_version") != "1.0.0"
        or preregistration.get("holdout_id") != matrix["matrix_id"]
        or preregistration.get("status") != "frozen-unexecuted"
    ):
        raise MatrixError("holdout preregistration identity is not frozen")
    if preregistration.get("isolation") != {
        "agent_runtime_receives_scenario_manifest": False,
        "agent_runtime_receives_ground_truth": False,
        "ground_truth_join": "post-run-only",
        "holdout_variants_used_for_agent_correction": False,
        "prompt_examples_overlap_allowed": False,
        "cause_revealing_alert_metadata_allowed": False,
    }:
        raise MatrixError("holdout Agent and Ground Truth isolation changed")

    scope = preregistration.get("scope", {})
    expected_families = [
        {
            "family_id": "kubernetes.container-oomkilled",
            "variants": 3,
            "varied_dimensions": [
                "memory-limit",
                "stress-duration",
                "workload-rate",
                "workload-seed",
                "observation-window",
            ],
        },
        {
            "family_id": "kubernetes.image-pull-failure",
            "variants": 3,
            "varied_dimensions": [
                "invalid-image-reference",
                "observation-window",
            ],
        },
        {
            "family_id": "kubernetes.missing-configmap",
            "variants": 3,
            "varied_dimensions": [
                "required-configmap-name",
                "volume-name",
                "mount-path",
                "observation-window",
            ],
        },
        {
            "family_id": "no-fault",
            "variants": 3,
            "varied_dimensions": [
                "workload-rate",
                "workload-seed",
                "baseline-window",
            ],
        },
    ]
    if (
        scope.get("new_root_cause_ids_allowed") is not False
        or scope.get("new_providers_allowed") is not False
        or scope.get("new_evidence_gate_rules_allowed") is not False
        or scope.get("registered_root_cause_ids")
        != [
            "kubernetes.container-oomkilled",
            "kubernetes.image-pull-failure",
            "kubernetes.missing-configmap",
        ]
        or scope.get("families") != expected_families
    ):
        raise MatrixError("holdout cause, Provider, or family scope changed")

    execution = preregistration.get("execution", {})
    if execution != {
        "matrix_path": "evaluation/holdout-v1-matrix.yaml",
        "unique_scenarios": 12,
        "repetitions_per_scenario": 1,
        "max_parallel": 1,
        "schedule": "rotated-round-robin",
        "failure_policy": "stop-and-preserve",
        "require_clean_worktree": True,
        "require_head_matches_origin_main": True,
        "explicit_confirmation_variable": HOLDOUT_CONFIRMATION_VARIABLE,
        "scenario_sha256_required": True,
        "agent_runtime_policy": (
            "reuse-pinned-corrected-regression-image-without-agent-code-change"
        ),
        "agent_runtime_image_source": "platform/versions.yaml",
    }:
        raise MatrixError("holdout execution preregistration changed")
    if preregistration.get("post_result_policy") != {
        "change_thresholds_after_first_attempt": False,
        "repair_agent_output": False,
        "continue_v1_after_agent_prompt_or_gate_change": False,
        "required_action_after_agent_prompt_or_gate_change": "register-holdout-v2",
        "combine_with_regression_v1_accuracy": False,
    }:
        raise MatrixError("holdout post-result policy changed")
    if preregistration.get("primary_metrics") != [
        "root_cause_top1_accuracy",
        "root_cause_top3_recall",
        "evidence_precision",
        "evidence_recall",
        "unsupported_evidence_citation_rate",
        "abstention_correctness",
    ] or preregistration.get("acceptance_reporting") != {
        "fault_top1_denominator": 9,
        "no_fault_abstention_denominator": 3,
        "publish_every_attempt": True,
        "publish_failures": True,
        "publish_harness_failures_separately": True,
        "unsupported_evidence_citations_must_be_zero": True,
        "cost_status_without_frozen_rate_card": "NOT_CALCULATED",
        "production_generalization_claim_allowed": False,
    }:
        raise MatrixError("holdout scoring and reporting boundary changed")


def _validate_holdout_matrix(matrix: Mapping[str, Any]) -> None:
    _validate_holdout_preregistration(matrix)
    if matrix["repetitions_per_scenario"] != 1:
        raise MatrixError("holdout matrix scenarios must each run exactly once")
    scenarios = matrix["scenarios"]
    if not isinstance(scenarios, list) or len(scenarios) != 12:
        raise MatrixError("holdout matrix must contain exactly twelve scenarios")
    required_scenario = {
        "scenario_id",
        "scenario_family",
        "variant_id",
        "scenario_path",
        "scenario_sha256",
        "make_target",
        "scenario_path_variable",
        "confirmation_variable",
        "expected_outcome",
    }
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    variants_by_family = {family: set() for family in HOLDOUT_FAMILIES}
    documents_by_family: dict[str, list[Mapping[str, Any]]] = {
        family: [] for family in HOLDOUT_FAMILIES
    }
    alert_names: set[str] = set()
    holdout_root = (ROOT / "evaluation" / "scenarios" / "holdout-v1").resolve()
    for scenario in scenarios:
        if not isinstance(scenario, dict) or set(scenario) != required_scenario:
            raise MatrixError("holdout matrix scenario structure is invalid")
        family = scenario["scenario_family"]
        boundary = HOLDOUT_FAMILIES.get(family)
        if boundary is None:
            raise MatrixError("holdout matrix contains an unregistered family")
        variant = scenario["variant_id"]
        if variant not in {"a", "b", "c"}:
            raise MatrixError("holdout variant_id must be a, b, or c")
        scenario_id = scenario["scenario_id"]
        if scenario_id != f"{boundary['scenario_prefix']}{variant}":
            raise MatrixError("holdout scenario_id does not match family and variant")
        for key in (
            "make_target",
            "scenario_path_variable",
            "confirmation_variable",
            "expected_outcome",
        ):
            if scenario[key] != boundary[key]:
                raise MatrixError("holdout scenario execution boundary was modified")
        try:
            scenario_path = (ROOT / scenario["scenario_path"]).resolve(strict=True)
            scenario_path.relative_to(holdout_root)
        except (OSError, ValueError) as error:
            raise MatrixError("holdout scenario path escapes its frozen root") from error
        if scenario_id in seen_ids or scenario["scenario_path"] in seen_paths:
            raise MatrixError("holdout scenario id or path is duplicated")
        content = scenario_path.read_bytes()
        if hashlib.sha256(content).hexdigest() != scenario["scenario_sha256"]:
            raise MatrixError("holdout scenario content differs from its frozen digest")
        scenario_document = _load_yaml(scenario_path)
        if scenario_document.get("scenario_id") != scenario_id:
            raise MatrixError("holdout scenario_id does not match its file")
        try:
            validate_contract(str(boundary["schema"]), scenario_document)
        except ContractViolation as error:
            raise MatrixError("holdout scenario contract validation failed") from error
        if family == "kubernetes.missing-configmap" and (
            scenario_document["fault"]["configmap_name"]
            != scenario_document["expected"]["evidence_predicates"][
                "configmap_name"
            ]
        ):
            raise MatrixError("holdout ConfigMap predicate differs from the injection")
        if family == "no-fault" and (
            scenario_document["workload"]["maximum_duration_seconds"]
            <= scenario_document["workload"]["baseline_seconds"]
        ):
            raise MatrixError("holdout no-fault watchdog does not exceed its baseline")
        alert_metadata = " ".join(
            str(scenario_document["alert"][key])
            for key in (
                "name",
                "summary",
                "generator_url",
                "verification_prefix",
            )
        ).lower()
        alert_metadata = f"{alert_metadata} {scenario_document['workload']['synthetic_marker']}"
        if any(
            term in alert_metadata
            for term in ("oom", "image", "pull", "missing", "configmap", "no-fault")
        ):
            raise MatrixError("holdout alert metadata reveals its expected cause")
        alert_name = str(scenario_document["alert"]["name"])
        if alert_name in alert_names:
            raise MatrixError("holdout alert names must be unique neutral case IDs")
        seen_ids.add(scenario_id)
        seen_paths.add(scenario["scenario_path"])
        variants_by_family[family].add(variant)
        documents_by_family[family].append(scenario_document)
        alert_names.add(alert_name)
    if any(variants != {"a", "b", "c"} for variants in variants_by_family.values()):
        raise MatrixError("holdout matrix must freeze variants a, b, and c per family")

    varied_values = {
        "kubernetes.container-oomkilled": [
            (
                document["fault"]["resources"]["limits"]["memory"],
                document["fault"]["chaos_mesh"]["duration_seconds"],
                document["workload"]["requests_per_second"],
                document["workload"]["seed"],
                document["fault"]["observation_seconds"],
            )
            for document in documents_by_family["kubernetes.container-oomkilled"]
        ],
        "kubernetes.image-pull-failure": [
            (
                document["fault"]["image"],
                document["fault"]["observation_seconds"],
            )
            for document in documents_by_family["kubernetes.image-pull-failure"]
        ],
        "kubernetes.missing-configmap": [
            (
                document["fault"]["configmap_name"],
                document["fault"]["volume_name"],
                document["fault"]["mount_path"],
                document["fault"]["observation_seconds"],
            )
            for document in documents_by_family["kubernetes.missing-configmap"]
        ],
        "no-fault": [
            (
                document["workload"]["requests_per_second"],
                document["workload"]["seed"],
                document["workload"]["baseline_seconds"],
            )
            for document in documents_by_family["no-fault"]
        ],
    }
    if any(
        len({values[index] for values in family_values}) != 3
        for family_values in varied_values.values()
        for index in range(len(family_values[0]))
    ):
        raise MatrixError("holdout declared variation dimensions are not distinct")


def load_matrix(matrix_name: str = DEFAULT_MATRIX_NAME) -> Mapping[str, Any]:
    registration = _matrix_registration(matrix_name)
    matrix = _load_yaml(Path(registration["path"]))
    required_top = {
        "schema_version",
        "matrix_id",
        "repetitions_per_scenario",
        "execution",
        "scenarios",
    }
    if set(matrix) != required_top or matrix["schema_version"] != registration[
        "schema_version"
    ]:
        raise MatrixError("evaluation matrix has an unsupported structure")
    if matrix["matrix_id"] != registration["matrix_id"]:
        raise MatrixError("evaluation matrix_id is not frozen")
    _validate_execution_policy(matrix)
    if matrix_name == DEFAULT_MATRIX_NAME:
        _validate_regression_matrix(matrix)
    else:
        _validate_holdout_matrix(matrix)
    return matrix


def build_schedule(matrix: Mapping[str, Any]) -> list[dict[str, Any]]:
    scenarios = list(matrix["scenarios"])
    schedule: list[dict[str, Any]] = []
    attempt_number = 0
    for repetition in range(1, int(matrix["repetitions_per_scenario"]) + 1):
        offset = (repetition - 1) % len(scenarios)
        rotated = scenarios[offset:] + scenarios[:offset]
        for scenario in rotated:
            attempt_number += 1
            planned = {
                "attempt": attempt_number,
                "repetition": repetition,
                "scenario_id": scenario["scenario_id"],
                "make_target": scenario["make_target"],
                "expected_outcome": scenario["expected_outcome"],
                "confirmation_variable": scenario["confirmation_variable"],
            }
            for key in (
                "scenario_family",
                "variant_id",
                "scenario_path",
                "scenario_sha256",
                "scenario_path_variable",
            ):
                if key in scenario:
                    planned[key] = scenario[key]
            schedule.append(planned)
    return schedule


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise MatrixError(f"git {' '.join(arguments)} failed")
    return completed.stdout.strip()


def _source_snapshot(*, require_clean: bool) -> dict[str, str]:
    head = _git("rev-parse", "HEAD")
    origin_main = _git("rev-parse", "origin/main")
    if require_clean and _git("status", "--porcelain", "--untracked-files=all"):
        raise MatrixError("actual evaluation requires a clean worktree")
    if require_clean and head != origin_main:
        raise MatrixError("actual evaluation requires HEAD to match origin/main")

    versions = _load_yaml(VERSIONS_PATH)
    runtime = versions.get("incident_platform", {}).get("reconciler", {})
    image_tag = runtime.get("image_tag")
    image_digest = runtime.get("image_digest")
    if not isinstance(image_tag, str) or not image_tag.startswith("runtime-"):
        raise MatrixError("Incident runtime image tag is not pinned")
    if not isinstance(image_digest, str) or not image_digest.startswith("sha256:"):
        raise MatrixError("Incident runtime image digest is not pinned")
    return {
        "source_commit": head,
        "origin_main_commit": origin_main,
        "runtime_image_tag": image_tag,
        "runtime_image_digest": image_digest,
    }


def _atomic_private_write(path: Path, value: Mapping[str, Any]) -> None:
    rendered = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _private_path(value: str, *, suffix: str | None = None) -> Path:
    candidate = (ROOT / value).resolve()
    try:
        candidate.relative_to(PRIVATE_RUN_ROOT.resolve())
    except ValueError as error:
        raise MatrixError("matrix artifact path escapes the private run root") from error
    if suffix is not None and not candidate.name.endswith(suffix):
        raise MatrixError("matrix artifact has an unexpected suffix")
    return candidate


def _new_artifact(
    before: set[Path], suffix: str, *, required: bool
) -> str | None:
    after = set(PRIVATE_RUN_ROOT.glob(f"*{suffix}"))
    created = sorted(after - before)
    if len(created) == 1:
        return str(created[0].relative_to(ROOT))
    if len(created) > 1:
        raise MatrixError(
            f"attempt created {len(created)} {suffix} artifacts; at most one allowed"
        )
    if required:
        raise MatrixError(
            f"attempt created no {suffix} artifact; exactly one required"
        )
    return None


def _run_id(now: datetime, source_commit: str, matrix_id: str) -> str:
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    prefix = "matrix" if matrix_id == "focused-four-scenario-v1" else matrix_id
    return f"{prefix}-{timestamp}-{source_commit[:8]}"


def _manifest_path_for_resume(value: str) -> Path:
    candidate = _private_path(value, suffix="manifest.json")
    try:
        candidate.relative_to(PRIVATE_MATRIX_ROOT.resolve())
    except ValueError as error:
        raise MatrixError("resume manifest must be below the private matrix root") from error
    if not candidate.is_file():
        raise MatrixError("resume manifest does not exist")
    return candidate


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MatrixError("resume manifest is invalid") from error
    if not isinstance(value, dict) or value.get("schema_version") != "1.0.0":
        raise MatrixError("resume manifest has an unsupported structure")
    return value


def execute_matrix(
    matrix: Mapping[str, Any],
    schedule: Sequence[Mapping[str, Any]],
    *,
    resume: str | None,
) -> Path:
    source = _source_snapshot(require_clean=True)
    confirmation_variable = str(
        _matrix_registration(matrix_name_from_id(matrix["matrix_id"]))[
            "confirmation_variable"
        ]
    )
    if os.environ.get(confirmation_variable) != source["source_commit"]:
        raise MatrixError(
            f"set {confirmation_variable} to the full current HEAD commit"
        )

    PRIVATE_MATRIX_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = PRIVATE_MATRIX_ROOT / ".execution.lock"
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_descriptor, "w") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MatrixError("another evaluation matrix is already running") from error

        if resume is not None:
            manifest_path = _manifest_path_for_resume(resume)
            manifest = _load_manifest(manifest_path)
            if manifest.get("matrix_id") != matrix["matrix_id"]:
                raise MatrixError("resume manifest belongs to another matrix")
            if manifest.get("source") != source:
                raise MatrixError("resume requires the original source and runtime snapshot")
            if manifest.get("state") in {"COMPLETED", "COMPLETED_WITH_FAILURES"}:
                raise MatrixError("completed matrix cannot be resumed")
            if manifest.get("schedule") != list(schedule):
                raise MatrixError("resume schedule differs from the frozen matrix")
            attempts = manifest.get("attempts")
            if not isinstance(attempts, list) or len(attempts) >= len(schedule):
                raise MatrixError("resume manifest has no pending attempt")
            run_directory = manifest_path.parent
            manifest["state"] = "RUNNING"
            manifest["resumed_at"] = _format_time(_utc_now())
        else:
            started_at = _utc_now()
            run_id = _run_id(
                started_at, source["source_commit"], str(matrix["matrix_id"])
            )
            run_directory = PRIVATE_MATRIX_ROOT / run_id
            try:
                run_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
            except FileExistsError as error:
                raise MatrixError("matrix run directory already exists") from error
            manifest_path = run_directory / "manifest.json"
            manifest = {
                "schema_version": "1.0.0",
                "matrix_id": matrix["matrix_id"],
                "run_id": run_id,
                "state": "RUNNING",
                "started_at": _format_time(started_at),
                "completed_at": None,
                "source": source,
                "schedule": list(schedule),
                "preflight_checks": [],
                "attempts": [],
            }
        _atomic_private_write(manifest_path, manifest)

        preflight_number = len(manifest.get("preflight_checks", [])) + 1
        preflight_started = _utc_now()
        preflight_log = run_directory / f"preflight-{preflight_number:02d}.log"
        descriptor = os.open(
            preflight_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        preflight_return_code: int | None = None
        preflight_error_code: str | None = None
        try:
            with os.fdopen(descriptor, "wb") as log_handle:
                preflight = subprocess.run(
                    ["make", "verify-evaluation-runtime"],
                    cwd=ROOT,
                    env=os.environ.copy(),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    timeout=300,
                    check=False,
                )
            preflight_return_code = preflight.returncode
        except subprocess.TimeoutExpired:
            preflight_error_code = "PREFLIGHT_TIMEOUT"
        manifest.setdefault("preflight_checks", []).append(
            {
                "sequence": preflight_number,
                "started_at": _format_time(preflight_started),
                "completed_at": _format_time(_utc_now()),
                "return_code": preflight_return_code,
                "error_code": preflight_error_code,
                "log_path": str(preflight_log.relative_to(ROOT)),
            }
        )
        if preflight_return_code != 0:
            manifest["state"] = "PREFLIGHT_FAILED"
            _atomic_private_write(manifest_path, manifest)
            return manifest_path
        _atomic_private_write(manifest_path, manifest)

        start_index = len(manifest["attempts"])
        for planned in schedule[start_index:]:
            attempt_started = _utc_now()
            attempt_number = int(planned["attempt"])
            log_path = run_directory / f"attempt-{attempt_number:02d}.log"
            before_result = set(PRIVATE_RUN_ROOT.glob("*.agent.result.json"))
            before_prediction = set(PRIVATE_RUN_ROOT.glob("*.agent.prediction.json"))
            before_runtime = set(PRIVATE_RUN_ROOT.glob("*.agent.runtime.json"))
            environment = os.environ.copy()
            environment[str(planned["confirmation_variable"])] = "yes"
            if "scenario_path_variable" in planned:
                environment[str(planned["scenario_path_variable"])] = str(
                    planned["scenario_path"]
                )
            return_code: int | None = None
            error_code: str | None = None
            descriptor = os.open(
                log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            try:
                with os.fdopen(descriptor, "wb") as log_handle:
                    completed = subprocess.run(
                        ["make", str(planned["make_target"])],
                        cwd=ROOT,
                        env=environment,
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                return_code = completed.returncode
            except BaseException as error:
                error_code = type(error).__name__.upper()

            artifact_errors: list[str] = []
            artifact_specs = (
                ("result_path", before_result, ".agent.result.json"),
                ("prediction_path", before_prediction, ".agent.prediction.json"),
                ("runtime_path", before_runtime, ".agent.runtime.json"),
            )
            artifact_paths: dict[str, str | None] = {}
            for name, before, suffix in artifact_specs:
                try:
                    artifact_paths[name] = _new_artifact(
                        before, suffix, required=return_code == 0
                    )
                except MatrixError as error:
                    artifact_paths[name] = None
                    artifact_errors.append(str(error))
            result_path = artifact_paths["result_path"]
            prediction_path = artifact_paths["prediction_path"]
            runtime_path = artifact_paths["runtime_path"]
            if all((result_path, prediction_path, runtime_path)):
                prefixes = {
                    Path(result_path).name.removesuffix(".agent.result.json"),
                    Path(prediction_path).name.removesuffix(
                        ".agent.prediction.json"
                    ),
                    Path(runtime_path).name.removesuffix(".agent.runtime.json"),
                }
                if len(prefixes) != 1:
                    artifact_errors.append(
                        "attempt artifacts belong to different evaluation cases"
                    )
            artifact_error = "; ".join(artifact_errors) or None

            passed = return_code == 0 and artifact_error is None
            attempt = {
                "attempt": attempt_number,
                "repetition": planned["repetition"],
                "scenario_id": planned["scenario_id"],
                "expected_outcome": planned["expected_outcome"],
                "state": "PASSED" if passed else "FAILED",
                "started_at": _format_time(attempt_started),
                "completed_at": _format_time(_utc_now()),
                "return_code": return_code,
                "error_code": error_code,
                "artifact_error": artifact_error,
                "log_path": str(log_path.relative_to(ROOT)),
                "agent_prediction_path": prediction_path,
                "agent_result_path": result_path,
                "agent_runtime_path": runtime_path,
            }
            manifest["attempts"].append(attempt)
            manifest["state"] = "RUNNING" if passed else "STOPPED_ON_FAILURE"
            _atomic_private_write(manifest_path, manifest)
            if not passed:
                break

        if len(manifest["attempts"]) == len(schedule):
            manifest["state"] = (
                "COMPLETED"
                if all(item["state"] == "PASSED" for item in manifest["attempts"])
                else "COMPLETED_WITH_FAILURES"
            )
            manifest["completed_at"] = _format_time(_utc_now())
            _atomic_private_write(manifest_path, manifest)
        return manifest_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or execute a registered evaluation matrix."
    )
    parser.add_argument(
        "--matrix",
        choices=tuple(REGISTERED_MATRICES),
        default=DEFAULT_MATRIX_NAME,
        help="registered matrix to plan or execute",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="run the matrix; otherwise print a mutation-free plan",
    )
    parser.add_argument(
        "--resume",
        help="private manifest path to resume after an operator-reviewed failure",
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        if arguments.resume and not arguments.execute:
            raise MatrixError("--resume requires --execute")
        matrix = load_matrix(arguments.matrix)
        schedule = build_schedule(matrix)
        source = _source_snapshot(require_clean=False)
        if not arguments.execute:
            print(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "mode": "DRY_RUN",
                        "matrix_id": matrix["matrix_id"],
                        "repetitions_per_scenario": matrix[
                            "repetitions_per_scenario"
                        ],
                        "total_attempts": len(schedule),
                        "source": source,
                        "schedule": schedule,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        manifest_path = execute_matrix(
            matrix,
            schedule,
            resume=arguments.resume,
        )
        manifest = _load_manifest(manifest_path)
        print(
            json.dumps(
                {
                    "state": manifest["state"],
                    "attempted": len(manifest["attempts"]),
                    "planned": len(manifest["schedule"]),
                    "manifest": str(manifest_path.relative_to(ROOT)),
                },
                sort_keys=True,
            )
        )
        return 0 if manifest["state"] == "COMPLETED" else 1
    except MatrixError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
