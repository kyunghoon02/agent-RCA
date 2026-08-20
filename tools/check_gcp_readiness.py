#!/usr/bin/env python3
"""Report GCP/GKE design and runtime readiness without reading credentials."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
READINESS_FILE = ROOT / "config" / "gcp-readiness.yaml"


def main() -> int:
    with READINESS_FILE.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    design = config["design_capabilities"]
    required_design = config["gates"]["terraform_design"][
        "required_capabilities"
    ]
    unresolved_design = [
        (capability, design[capability]["status"])
        for capability in required_design
        if design[capability]["status"] != "verified"
    ]

    runtime = config["runtime_inputs"]
    required_runtime = config["gates"]["terraform_plan_apply"][
        "required_runtime_inputs"
    ]
    unresolved_runtime = [
        (item, runtime[item]["status"])
        for item in required_runtime
        if runtime[item]["status"] != "verified"
    ]

    decision = config["decision"]
    print(
        "GCP target: "
        f"{decision['kubernetes_service']} / {decision['kubernetes_mode']} / "
        f"{decision['cluster_availability']}"
    )
    if unresolved_design:
        print("Terraform design readiness: BLOCKED")
        for capability, status in unresolved_design:
            print(f"- {capability}: {status}")
        return 2

    print("Terraform design readiness: READY")
    if unresolved_runtime:
        print("Terraform plan/apply readiness: BLOCKED")
        for item, status in unresolved_runtime:
            print(f"- {item}: {status}")
        print(f"Update evidence in: {READINESS_FILE.relative_to(ROOT)}")
        return 2

    print("Terraform plan/apply readiness: READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
