#!/usr/bin/env python3
"""Read-only attestation of Prometheus -> Alertmanager -> Incident identity.

This tool never sends alerts, changes rules, or invokes an Agent. Its output is
a private verification artifact, not a new accuracy matrix or a model input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from incident_platform.incidents import AlertmanagerNormalizer

ALERT_NAME = "OnlineBoutiqueCheckoutHighFailureRate"
OOM_ALERT_NAME = "OnlineBoutiqueRecentOOMRestart"
RULE_FILES = {
    ALERT_NAME: "remote-online-boutique-alerts.yaml",
    OOM_ALERT_NAME: "remote-workload-alerts.yaml",
}
EXPECTED_CAUSE = "kubernetes.container-oomkilled"


class NativeAlertError(ValueError):
    pass


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise NativeAlertError(reason)


def _time(value: str) -> datetime:
    # Prometheus uses RFC3339Nano; the controller may still use Python 3.9.
    value = re.sub(
        r"(?<=\d{2}:\d{2}:\d{2})\.(\d+)",
        lambda match: "." + (match.group(1) + "000000")[:6],
        value,
    )
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(result.tzinfo is not None, "timestamp_missing_timezone")
    return result


def _expression_tokens(expression: str) -> list:
    # Ignore formatting outside quoted labels without removing token boundaries.
    tokens = re.findall(
        r'"(?:\\.|[^"\\])*"|[A-Za-z_][A-Za-z0-9_:]*|[0-9]+(?:\.[0-9]+)?|[^\s]',
        expression,
    )
    # Prometheus renders selector matchers in sorted order. Ordering inside a
    # selector is immaterial, but every operator and quoted value must survive.
    result = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token != "{":
            result.append(token)
            index += 1
            continue
        end = tokens.index("}", index + 1)
        matchers = [[]]
        for item in tokens[index + 1 : end]:
            if item == ",":
                matchers.append([])
            else:
                matchers[-1].append(item)
        result.append("{")
        for position, matcher in enumerate(sorted(matchers)):
            if position:
                result.append(",")
            result.extend(matcher)
        result.append("}")
        index = end + 1
    return result


def expected_rule(cluster_id: str, alert_name: str = ALERT_NAME) -> dict:
    _require(alert_name in RULE_FILES, "unregistered_native_rule")
    document = yaml.safe_load(
        (ROOT / "platform/observability" / RULE_FILES[alert_name]).read_text(
            encoding="utf-8"
        )
    )
    rule = next(
        rule
        for group in document["spec"]["groups"]
        for rule in group["rules"]
        if rule.get("alert") == alert_name
    )
    rule["expr"] = rule["expr"].replace("FAULT_TARGET_CLUSTER_ID", cluster_id)
    return rule


def checked_rule(response: dict, cluster_id: str, alert_name: str = ALERT_NAME) -> dict:
    _require(response.get("status") == "success", "prometheus_request_failed")
    rules = [
        rule
        for group in response["data"]["groups"]
        for rule in group["rules"]
        if rule.get("name") == alert_name
    ]
    _require(len(rules) == 1, "native_rule_missing_or_ambiguous")
    actual = rules[0]
    expected = expected_rule(cluster_id, alert_name)
    _require(actual.get("health") == "ok", "native_rule_unhealthy")
    _require(not actual.get("lastError"), "native_rule_evaluation_error")
    _require(
        _expression_tokens(actual.get("query", ""))
        == _expression_tokens(expected["expr"]),
        "native_rule_expression_drift",
    )
    expected_seconds = {"2m": 120, "0s": 0}[expected["for"]]
    _require(
        actual.get("duration") == expected_seconds, "native_rule_hold_duration_drift"
    )
    _require(actual.get("labels") == expected["labels"], "native_rule_labels_drift")
    if alert_name == OOM_ALERT_NAME:
        document = yaml.safe_load(
            (ROOT / "platform/observability" / RULE_FILES[alert_name]).read_text()
        )
        dependency = next(
            rule
            for group in document["spec"]["groups"]
            for rule in group["rules"]
            if rule.get("record") == "agent_rca_target_pod_service"
        )
        dependencies = [
            rule
            for group in response["data"]["groups"]
            for rule in group["rules"]
            if rule.get("name") == dependency["record"]
        ]
        _require(len(dependencies) == 1, "native_ownership_rule_missing_or_ambiguous")
        loaded = dependencies[0]
        _require(
            loaded.get("health") == "ok" and not loaded.get("lastError"),
            "native_ownership_rule_unhealthy",
        )
        _require(
            _expression_tokens(loaded.get("query", ""))
            == _expression_tokens(
                dependency["expr"].replace("FAULT_TARGET_CLUSTER_ID", cluster_id)
            ),
            "native_ownership_rule_expression_drift",
        )
    return actual


def preflight(payload: dict, cluster_id: str, alert_name: str = ALERT_NAME) -> dict:
    rule = checked_rule(payload["prometheus"], cluster_id, alert_name)
    _require(rule.get("state") == "inactive", "native_rule_already_active")
    _require(not rule.get("alerts"), "native_rule_has_existing_alerts")
    return {
        "alert_name": alert_name,
        "rule_hold_seconds": rule["duration"],
        "rule_expression_sha256": hashlib.sha256(
            " ".join(rule["query"].split()).encode()
        ).hexdigest(),
        "state_before_fault": rule["state"],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def capture(payload: dict, cluster_id: str, alert_name: str = ALERT_NAME) -> dict:
    rule = checked_rule(payload["prometheus"], cluster_id, alert_name)
    _require(rule.get("state") == "firing", "native_rule_not_firing")
    floor = _time(payload["not_before"])
    # This verifier runs the registered checkout fault, even though the event
    # rule supports multiple Services. Never bind another workload's alert.
    required_labels = {
        **rule["labels"],
        "cluster_id": cluster_id,
        "alertname": alert_name,
        "namespace": "online-boutique",
        "service": "checkoutservice" if alert_name == OOM_ALERT_NAME else "frontend",
    }

    def matches(item: dict) -> bool:
        labels = item.get("labels", {})
        return all(labels.get(k) == v for k, v in required_labels.items())

    prometheus_alerts = [item for item in rule.get("alerts", []) if matches(item)]
    _require(len(prometheus_alerts) == 1, "prometheus_alert_missing_or_ambiguous")
    prometheus_alert = prometheus_alerts[0]
    _require(prometheus_alert.get("state") == "firing", "prometheus_alert_not_firing")
    _require(
        _time(prometheus_alert["activeAt"]) >= floor, "prometheus_alert_predates_fault"
    )
    alerts = [item for item in payload["alertmanager"] if matches(item)]
    _require(len(alerts) == 1, "alertmanager_alert_missing_or_ambiguous")
    alert = alerts[0]
    _require(
        alert.get("status", {}).get("state") == "active",
        "alertmanager_alert_not_active",
    )
    _require(_time(alert["startsAt"]) >= floor, "alertmanager_alert_predates_fault")
    _require(
        all(alert["labels"].get(k) == v for k, v in prometheus_alert["labels"].items()),
        "prometheus_alertmanager_label_mismatch",
    )
    _require(
        "verification_id" not in alert["labels"], "synthetic_verification_label_present"
    )
    normalized = (
        AlertmanagerNormalizer()
        .normalize(
            {
                "alerts": [
                    {
                        **alert,
                        "status": "firing",
                        "startsAt": _time(alert["startsAt"]).isoformat(),
                    }
                ]
            }
        )[0]
        .incident
    )
    return {
        "trigger": "prometheus-rule",
        "alert_name": alert_name,
        "rule_hold_seconds": rule["duration"],
        "rule_expression_sha256": hashlib.sha256(
            " ".join(rule["query"].split()).encode()
        ).hexdigest(),
        "change_started_at": payload["not_before"],
        "prometheus_active_at": prometheus_alert["activeAt"],
        "firing_observed_at": datetime.now(timezone.utc).isoformat(),
        "alertmanager_starts_at": alert["startsAt"],
        "alertmanager_fingerprint": alert["fingerprint"],
        "incident_id": normalized["incident_id"],
        "alert_labels": alert["labels"],
        "synthetic_alert_submitted": False,
    }


def attest(payload: dict) -> dict:
    captured, bundle = payload["capture"], payload["bundle"]
    incident = bundle["incident"]
    _require(captured["trigger"] == "prometheus-rule", "wrong_trigger")
    _require(
        captured["synthetic_alert_submitted"] is False, "synthetic_alert_submitted"
    )
    _require(
        incident["incident_id"] == captured["incident_id"], "incident_identity_mismatch"
    )
    _require(
        incident["alert"]["fingerprint"] == captured["alertmanager_fingerprint"],
        "fingerprint_mismatch",
    )
    _require(
        incident["alert"]["name"] == captured["alert_name"]
        and captured["alert_name"] in RULE_FILES,
        "alert_name_mismatch",
    )
    _require("verification_id" not in incident["alert"]["labels"], "synthetic_incident")
    _require(
        all(
            incident["alert"]["labels"].get(k) == v
            for k, v in captured["alert_labels"].items()
        ),
        "incident_label_mismatch",
    )
    _require(
        _time(incident["triggered_at"])
        == _time(captured["alertmanager_starts_at"]).replace(microsecond=0),
        "incident_start_mismatch",
    )
    _require(
        _time(incident["created_at"]) >= _time(captured["change_started_at"]),
        "incident_predates_fault",
    )
    context, run, report = (
        bundle.get("context"),
        bundle.get("agent_run"),
        bundle.get("report"),
    )
    _require(context is not None, "context_missing")
    _require(
        context["incident_id"] == incident["incident_id"], "context_incident_mismatch"
    )
    _require(run is not None, "agent_run_missing")
    _require(run["incident_id"] == incident["incident_id"], "agent_incident_mismatch")
    _require(run["context_id"] == context["context_id"], "agent_context_mismatch")
    if report is not None:
        _require(
            report["incident_id"] == incident["incident_id"], "report_incident_mismatch"
        )
        _require(
            report["context_id"] == context["context_id"], "report_context_mismatch"
        )
    cause = (report or {}).get("root_cause") or {}
    accepted = (
        run["status"] == "SUCCEEDED"
        and run["reason_code"] == "REPORT_ACCEPTED"
        and report is not None
    )
    return {
        "boundary": "native-alert-connectivity-single-run-not-accuracy-matrix",
        "native_detection_verified": True,
        "incident_received": True,
        "synthetic_alert_submitted": False,
        "agent_status": run["status"],
        "agent_reason_code": run["reason_code"],
        "report_accepted": accepted,
        "report_status": (report or {}).get("status"),
        "reported_cause_id": cause.get("cause_id"),
        "expected_cause_match": accepted and cause.get("cause_id") == EXPECTED_CAUSE,
        "usage": run.get("usage", {}),
        "ingest_to_report_seconds": (
            (
                _time(report["generated_at"]) - _time(incident["created_at"])
            ).total_seconds()
            if report is not None
            else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase", required=True, choices=("preflight", "capture", "attest")
    )
    parser.add_argument("--cluster-id", required=True)
    parser.add_argument("--alert-name", choices=tuple(RULE_FILES), default=ALERT_NAME)
    arguments = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        result = (
            attest(payload)
            if arguments.phase == "attest"
            else {
                "preflight": preflight,
                "capture": capture,
            }[
                arguments.phase
            ](payload, arguments.cluster_id, arguments.alert_name)
        )
    except (ValueError, KeyError, TypeError, StopIteration) as error:
        reason = (
            str(error)
            if isinstance(error, NativeAlertError)
            else "invalid_attestation_input"
        )
        print(json.dumps({"status": "FAILED", "reason": reason}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
