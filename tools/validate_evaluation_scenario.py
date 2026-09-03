#!/usr/bin/env python3
"""Reject evaluation scenario paths outside the registered matrices."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from incident_platform.contracts import validate_contract
from incident_platform.errors import ContractViolation
from tools.run_evaluation_matrix import MatrixError, load_matrix


SCENARIO_ROOT = (ROOT / "evaluation" / "scenarios").resolve()
REGRESSION_FAMILY_BY_ID = {
    "scenario-checkoutservice-oom-chaos-mesh-change-stress": (
        "kubernetes.container-oomkilled"
    ),
    "scenario-paymentservice-image-pull-change-normal": (
        "kubernetes.image-pull-failure"
    ),
    "scenario-checkoutservice-missing-configmap-normal": (
        "kubernetes.missing-configmap"
    ),
    "scenario-frontend-no-fault-normal": "no-fault",
}
SCHEMA_BY_FAMILY = {
    "kubernetes.container-oomkilled": "controlled-fault-scenario.schema.json",
    "kubernetes.image-pull-failure": "controlled-image-pull-scenario.schema.json",
    "kubernetes.missing-configmap": (
        "controlled-missing-configmap-scenario.schema.json"
    ),
    "no-fault": "no-fault-control-scenario.schema.json",
}
HOLDOUT_SCHEMA_BY_FAMILY = {
    "kubernetes.container-oomkilled": (
        "holdout-controlled-oom-scenario.schema.json"
    ),
    "kubernetes.image-pull-failure": (
        "holdout-controlled-image-pull-scenario.schema.json"
    ),
    "kubernetes.missing-configmap": (
        "holdout-controlled-missing-configmap-scenario.schema.json"
    ),
    "no-fault": "holdout-no-fault-control-scenario.schema.json",
}


def _load_scenario(path: Path) -> Mapping[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise MatrixError("registered scenario cannot be loaded") from error
    if not isinstance(value, dict):
        raise MatrixError("registered scenario must contain one object")
    return value


def validate_registered_scenario(path_value: str, family: str) -> Path:
    try:
        path = (ROOT / path_value).resolve(strict=True)
        relative_path = path.relative_to(ROOT)
        path.relative_to(SCENARIO_ROOT)
    except (OSError, ValueError) as error:
        raise MatrixError("scenario path escapes the registered scenario root") from error

    registrations: dict[str, tuple[str, str]] = {}
    for configured in load_matrix()["scenarios"]:
        scenario_id = str(configured["scenario_id"])
        registrations[str(configured["scenario_path"])] = (
            REGRESSION_FAMILY_BY_ID[scenario_id],
            SCHEMA_BY_FAMILY[REGRESSION_FAMILY_BY_ID[scenario_id]],
        )
    for configured in load_matrix("holdout-v1")["scenarios"]:
        registrations[str(configured["scenario_path"])] = (
            str(configured["scenario_family"]),
            HOLDOUT_SCHEMA_BY_FAMILY[str(configured["scenario_family"])],
        )

    registration = registrations.get(str(relative_path))
    if registration is None or registration[0] != family:
        raise MatrixError("scenario is not registered for the requested fault family")
    scenario = _load_scenario(path)
    try:
        validate_contract(registration[1], scenario)
    except ContractViolation as error:
        raise MatrixError("registered scenario contract validation failed") from error
    if family == "kubernetes.missing-configmap" and (
        scenario["fault"]["configmap_name"]
        != scenario["expected"]["evidence_predicates"]["configmap_name"]
    ):
        raise MatrixError("registered ConfigMap predicate differs from the injection")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate an evaluation scenario against its registered matrix."
    )
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--family", choices=tuple(SCHEMA_BY_FAMILY), required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        path = validate_registered_scenario(arguments.scenario, arguments.family)
    except MatrixError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
