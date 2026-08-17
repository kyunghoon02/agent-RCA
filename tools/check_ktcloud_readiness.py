#!/usr/bin/env python3
"""Report whether the KT Cloud capability gate is ready for implementation."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
CAPABILITY_FILE = ROOT / "config" / "kt-cloud-capabilities.yaml"


def main() -> int:
    with CAPABILITY_FILE.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    capabilities = config["capabilities"]
    required = config["gates"]["terraform_implementation"][
        "required_capabilities"
    ]
    unresolved = [
        (capability, capabilities[capability]["status"])
        for capability in required
        if capabilities[capability]["status"] != "verified"
    ]

    decision = config["decision"]
    print(
        "KT Cloud target: "
        f"{decision['cloud']} / {decision['zone']} / {decision['kubernetes_mode']}"
    )
    if unresolved:
        print("Terraform implementation readiness: BLOCKED")
        for capability, status in unresolved:
            print(f"- {capability}: {status}")
        print(f"Update evidence in: {CAPABILITY_FILE.relative_to(ROOT)}")
        return 2

    print("Terraform implementation readiness: READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
