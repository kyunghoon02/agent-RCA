#!/usr/bin/env python3
"""Create local-only controlled-fault evaluation artifacts from stdin."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from incident_platform.fault_evaluation import build_controlled_fault_evaluation


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = ROOT / "evaluation" / "scenarios"
GROUND_TRUTH_ROOT = ROOT / "evaluation" / "ground-truth" / "private"
RUN_ROOT = ROOT / "evaluation" / "runs" / "private"
MAX_STDIN_BYTES = 8_388_608


def _load_scenario(path: Path) -> tuple[Mapping[str, Any], str]:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(SCENARIO_ROOT.resolve(strict=True))
        content = resolved.read_bytes()
    except (OSError, ValueError) as error:
        raise SystemExit(
            "scenario must be an existing YAML file below evaluation/scenarios"
        ) from error
    try:
        scenario = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise SystemExit("scenario is not valid YAML") from error
    if not isinstance(scenario, dict):
        raise SystemExit("scenario must contain one YAML object")
    return scenario, hashlib.sha256(content).hexdigest()


def _load_bundle() -> Mapping[str, Any]:
    content = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(content) > MAX_STDIN_BYTES:
        raise SystemExit(f"fault evaluation bundle exceeds {MAX_STDIN_BYTES} bytes")
    try:
        value = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit("fault evaluation stdin must be UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise SystemExit("fault evaluation stdin must contain one JSON object")
    return value


def _exclusive_write(path: Path, value: Mapping[str, Any]) -> None:
    rendered = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read an Incident/Context/Evidence bundle from stdin and create "
            "local-only controlled-fault evaluation artifacts."
        )
    )
    parser.add_argument("--scenario", type=Path, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    scenario, scenario_sha256 = _load_scenario(arguments.scenario)
    artifacts = build_controlled_fault_evaluation(
        _load_bundle(),
        scenario,
        scenario_sha256=scenario_sha256,
        evaluated_at=datetime.now(timezone.utc),
    )
    evaluation_case_id = artifacts["result"]["evaluation_case_id"]
    GROUND_TRUTH_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    RUN_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = {
        "ground_truth": GROUND_TRUTH_ROOT / f"{evaluation_case_id}.json",
        "prediction": RUN_ROOT / f"{evaluation_case_id}.prediction.json",
        "result": RUN_ROOT / f"{evaluation_case_id}.result.json",
        "observation": RUN_ROOT / f"{evaluation_case_id}.observation.json",
    }
    existing = [str(path) for path in paths.values() if path.exists()]
    if existing:
        raise SystemExit(
            "evaluation artifacts already exist; refusing to overwrite: "
            + ", ".join(existing)
        )
    created: list[Path] = []
    try:
        for name in ("ground_truth", "prediction", "result", "observation"):
            _exclusive_write(paths[name], artifacts[name])
            created.append(paths[name])
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise

    summary = {
        "schema_version": "1.0.0",
        "evaluation_case_id": evaluation_case_id,
        "prediction_outcome": artifacts["prediction"]["outcome"],
        "metrics": artifacts["result"]["metrics"],
        "artifacts": {
            name: str(path.relative_to(ROOT)) for name, path in paths.items()
        },
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
