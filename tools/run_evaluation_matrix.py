#!/usr/bin/env python3
"""Plan or execute the frozen four-scenario evaluation matrix safely."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "evaluation" / "matrix.yaml"
PREREGISTRATION_PATH = ROOT / "evaluation" / "preregistration.yaml"
VERSIONS_PATH = ROOT / "platform" / "versions.yaml"
PRIVATE_RUN_ROOT = ROOT / "evaluation" / "runs" / "private"
PRIVATE_MATRIX_ROOT = PRIVATE_RUN_ROOT / "matrix"
CONFIRMATION_VARIABLE = "CONFIRM_EVALUATION_MATRIX"

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


def load_matrix() -> Mapping[str, Any]:
    matrix = _load_yaml(MATRIX_PATH)
    required_top = {
        "schema_version",
        "matrix_id",
        "repetitions_per_scenario",
        "execution",
        "scenarios",
    }
    if set(matrix) != required_top or matrix["schema_version"] != "1.0.0":
        raise MatrixError("evaluation matrix has an unsupported structure")
    if matrix["matrix_id"] != "focused-four-scenario-v1":
        raise MatrixError("evaluation matrix_id is not frozen")

    preregistration = _load_yaml(PREREGISTRATION_PATH)
    registered_repetitions = preregistration.get("dataset", {}).get(
        "runtime_repetitions_per_scenario"
    )
    repetitions = matrix["repetitions_per_scenario"]
    if repetitions != 5 or repetitions != registered_repetitions:
        raise MatrixError("matrix repetitions differ from preregistration")

    execution = matrix["execution"]
    if not isinstance(execution, dict) or execution != {
        "schedule": "rotated-round-robin",
        "max_parallel": 1,
        "failure_policy": "stop-and-preserve",
        "require_clean_worktree": True,
        "require_head_matches_origin_main": True,
    }:
        raise MatrixError("evaluation matrix execution policy is not frozen")

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
            schedule.append(
                {
                    "attempt": attempt_number,
                    "repetition": repetition,
                    "scenario_id": scenario["scenario_id"],
                    "make_target": scenario["make_target"],
                    "expected_outcome": scenario["expected_outcome"],
                    "confirmation_variable": scenario["confirmation_variable"],
                }
            )
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


def _run_id(now: datetime, source_commit: str) -> str:
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    return f"matrix-{timestamp}-{source_commit[:8]}"


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
    if os.environ.get(CONFIRMATION_VARIABLE) != source["source_commit"]:
        raise MatrixError(
            f"set {CONFIRMATION_VARIABLE} to the full current HEAD commit"
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
            run_id = _run_id(started_at, source["source_commit"])
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
        description="Plan or execute the frozen four-scenario evaluation matrix."
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
        matrix = load_matrix()
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
