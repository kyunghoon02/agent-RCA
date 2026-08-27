#!/usr/bin/env python3
"""Summarize local-only controlled-fault observations without leaking IDs."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from incident_platform.errors import ContractViolation
from incident_platform.fault_evaluation import (
    summarize_controlled_fault_observations,
)


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_RUN_ROOT = ROOT / "evaluation" / "runs" / "private"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read private observation artifacts for one scenario and print an "
            "ID-free distribution summary."
        )
    )
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument("--evidence-gate-policy", required=True)
    parser.add_argument("--minimum-runs", type=int, default=5)
    return parser


def _load_observations(
    scenario_id: str, evidence_gate_policy: str
) -> list[Mapping[str, Any]]:
    observations: list[Mapping[str, Any]] = []
    for path in sorted(PRIVATE_RUN_ROOT.glob("*.observation.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SystemExit(f"invalid observation artifact: {path}") from error
        if not isinstance(value, dict):
            raise SystemExit(f"observation artifact is not an object: {path}")
        if (
            value.get("scenario_id") == scenario_id
            and value.get("evidence_gate_policy") == evidence_gate_policy
        ):
            observations.append(value)
    return observations


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.minimum_runs < 1:
        raise SystemExit("--minimum-runs must be at least 1")
    observations = _load_observations(
        arguments.scenario_id, arguments.evidence_gate_policy
    )
    if len(observations) < arguments.minimum_runs:
        raise SystemExit(
            f"scenario has {len(observations)} observation(s); "
            f"{arguments.minimum_runs} required"
        )
    try:
        summary = summarize_controlled_fault_observations(
            observations,
            generated_at=datetime.now(timezone.utc),
        )
    except ContractViolation as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
