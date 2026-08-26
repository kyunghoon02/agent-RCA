#!/usr/bin/env python3
"""Score one completed RCA run against a local-only Ground Truth label."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

from incident_platform.rca_evaluation import evaluate_rca_case


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_GROUND_TRUTH_ROOT = ROOT / "evaluation" / "ground-truth" / "private"
MAX_INPUT_BYTES = 1_048_576


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Join one private Ground Truth label with one post-run RCA prediction "
            "and emit a sanitized evaluation result."
        )
    )
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--prediction", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser


def _load_object(path: Path, label: str) -> Dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise SystemExit(f"{label} file cannot be read: {path}") from error
    if size > MAX_INPUT_BYTES:
        raise SystemExit(f"{label} file exceeds {MAX_INPUT_BYTES} bytes")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"{label} file is not valid UTF-8 JSON: {path}") from error
    if not isinstance(value, dict):
        raise SystemExit(f"{label} file must contain one JSON object")
    return value


def _require_private_ground_truth(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(PRIVATE_GROUND_TRUTH_ROOT.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise SystemExit(
            "Ground Truth must be an existing file below "
            "evaluation/ground-truth/private"
        ) from error
    return resolved


def main() -> int:
    arguments = _parser().parse_args()
    ground_truth_path = _require_private_ground_truth(arguments.ground_truth)
    try:
        prediction_path = arguments.prediction.resolve(strict=True)
    except OSError as error:
        raise SystemExit(
            f"prediction file cannot be read: {arguments.prediction}"
        ) from error
    result = evaluate_rca_case(
        _load_object(ground_truth_path, "Ground Truth"),
        _load_object(prediction_path, "prediction"),
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output is None:
        print(rendered, end="")
    else:
        output_path = arguments.output.resolve(strict=False)
        if output_path in {ground_truth_path, prediction_path}:
            raise SystemExit("evaluation output must not overwrite an input file")
        try:
            output_path.relative_to(PRIVATE_GROUND_TRUTH_ROOT.resolve(strict=True))
        except ValueError:
            pass
        else:
            raise SystemExit(
                "sanitized evaluation output must stay outside the private "
                "Ground Truth directory"
            )
        try:
            with output_path.open("x", encoding="utf-8") as handle:
                handle.write(rendered)
        except OSError as error:
            raise SystemExit(
                f"evaluation output could not be created: {output_path}"
            ) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
