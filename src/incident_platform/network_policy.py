"""Versioned, path-scoped proof of policy enforcement on a failed dependency.

This proves an observed dependency failure, not that every frontend failure had
this cause or that a particular policy object was responsible.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any, Mapping, Sequence

from .contracts import validate_contract
from .errors import ContractViolation
from .evidence import parse_time


NETWORK_POLICY_CAUSE = "network.policy-denied-dependency"
NETWORK_PATH_FEATURE = "network-dependency-path-v1"
RPC_ERROR_LOOKBACK_SECONDS = 120
NETWORK_PATH_FIELDS = (
    "path_id", "source_service", "destination_service", "destination_port",
    "protocol", "client_operation",
)


def validate_network_path_evidence(item: Mapping[str, Any]) -> None:
    validate_contract("network-path-evidence.schema.json", item)
    facts, subject = item["facts"], item["subject"]
    if subject["name"] != facts["source_service"]:
        raise ContractViolation("Network path subject must be its caller Service")
    start = parse_time(item["window"]["start"], "network window start")
    end = parse_time(item["window"]["end"], "network window end")
    if start > end:
        raise ContractViolation("Network Evidence window is reversed")
    previous = None
    if item["source"] == "hubble":
        times = [parse_time(value, "observed denial") for value in facts["denied_at"]]
        if len(times) != facts["policy_denied_count"] or times != sorted(times) or any(
            not start <= value <= end for value in times
        ):
            raise ContractViolation("Observed denial timestamps contradict the count or window")
        if times and (parse_time(facts["first_denied_at"], "first denial") != times[0]
                      or parse_time(facts["last_denied_at"], "last denial") != times[-1]):
            raise ContractViolation("Observed denial bounds contradict their timestamps")
        for name in ("first_denied_at", "last_denied_at"):
            value = facts[name]
            if value is None:
                if facts["policy_denied_count"]:
                    raise ContractViolation("Policy denials require observed timestamps")
                continue
            timestamp = parse_time(value, name)
            if not start <= timestamp <= end or (previous and timestamp < previous):
                raise ContractViolation("Policy denial times are outside the Evidence window")
            previous = timestamp
        if not facts["policy_denied_count"] and (
            facts["first_denied_at"] is not None or facts["last_denied_at"] is not None
        ):
            raise ContractViolation("No observed denial cannot carry denial timestamps")
        if item["quality"]["completeness"] > 0.5:
            raise ContractViolation("Hubble retention remains unknown")
    else:
        if len(facts["error_samples"]) > facts["sample_count"]:
            raise ContractViolation("RPC error samples exceed the sample count")
        for sample in facts["error_samples"]:
            timestamp = parse_time(sample["at"], "RPC error sample")
            if not start <= timestamp <= end or (previous and timestamp <= previous):
                raise ContractViolation("RPC samples must be ordered inside the Evidence window")
            if not math.isfinite(sample["errors"]) or sample["errors"] <= 0:
                raise ContractViolation("RPC error samples must be finite and positive")
            previous = timestamp


def matching_path(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    return (
        first["incident_id"] == second["incident_id"]
        and first["subject"] == second["subject"]
        and all(first["facts"][key] == second["facts"][key] for key in NETWORK_PATH_FIELDS)
    )


class NetworkPolicyDeniedRule:
    rule_id = NETWORK_POLICY_CAUSE

    def evaluate(self, evidence: Sequence[Mapping[str, Any]]):
        # Avoid an import cycle with the common rule engine.
        from .deterministic import RuleEvaluation

        valid = []
        for item in evidence:
            if item.get("facts", {}).get("feature_set") != NETWORK_PATH_FEATURE:
                continue
            try:
                validate_network_path_evidence(item)
            except (ContractViolation, KeyError, TypeError, ValueError):
                continue
            valid.append(item)
        denials = [item for item in valid if item["source"] == "hubble"
                   and item["facts"]["policy_denied_count"] > 0]
        if not denials:
            return RuleEvaluation(self.rule_id, "NOT_APPLICABLE", "")
        for denied in denials:
            facts = denied["facts"]
            denial_times = [parse_time(value, "observed denial") for value in facts["denied_at"]]
            for rpc in valid:
                if rpc["source"] != "prometheus" or not matching_path(denied, rpc):
                    continue
                # Each sample is an increase over the fixed preceding 120 seconds.
                # Never correlate two whole-window maxima with unrelated times.
                overlap = any(
                    parse_time(sample["at"], "RPC sample") - timedelta(seconds=RPC_ERROR_LOOKBACK_SECONDS)
                    <= denied_at <= parse_time(sample["at"], "RPC sample")
                    for sample in rpc["facts"]["error_samples"]
                    for denied_at in denial_times
                )
                if overlap:
                    return RuleEvaluation(
                        self.rule_id, "PROVEN",
                        f"Policy enforcement dropped {facts['source_service']} to "
                        f"{facts['destination_service']}:{facts['destination_port']} "
                        f"{facts['protocol']} traffic while the registered caller RPC "
                        f"{facts['client_operation']} recorded errors. This establishes "
                        "an affected dependency, not a unique HTTP-request root cause "
                        "or the identity of the enforcing policy object.",
                        (denied["evidence_id"], rpc["evidence_id"]),
                    )
        return RuleEvaluation(
            self.rule_id, "INSUFFICIENT",
            "Policy denial was observed but matching caller RPC failure is unproven.",
            tuple(item["evidence_id"] for item in denials),
            ("Caller RPC errors on the same registered directed dependency and time interval",),
        )
