from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from incident_platform.contracts import validate_contract
from incident_platform.deterministic import DeterministicRCAEngine
from incident_platform.errors import ContractViolation
from incident_platform.reporting import FastPathReportBuilder

from tests.test_deterministic_rca import FIXTURE_DIR, load_fixture


GENERATED_AT = datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc)


def incident_for(evidence: list[dict], *, failed_collector: bool = False) -> dict:
    incident_id = evidence[0]["incident_id"]
    statuses = [
        {
            "collector": "kubernetes",
            "status": "SUCCEEDED",
            "attempts": 1,
            "started_at": "2026-08-13T01:59:00Z",
            "ended_at": "2026-08-13T01:59:01Z",
            "error": None,
        }
    ]
    if any(item["source"] == "prometheus" for item in evidence):
        statuses.append(
            {
                "collector": "prometheus",
                "status": "SUCCEEDED",
                "attempts": 1,
                "started_at": "2026-08-13T01:59:00Z",
                "ended_at": "2026-08-13T01:59:01Z",
                "error": None,
            }
        )
    if failed_collector:
        statuses.append(
            {
                "collector": "logs",
                "status": "TIMED_OUT",
                "attempts": 2,
                "started_at": "2026-08-13T01:59:00Z",
                "ended_at": "2026-08-13T01:59:03Z",
                "error": "collector exceeded 3.000s budget",
            }
        )
    incident = {
        "schema_version": "1.0.0",
        "incident_id": incident_id,
        "deduplication_key": f"fixture:dedup:{incident_id}",
        "status": "LOCALIZING",
        "severity": "critical",
        "source": "alertmanager",
        "triggered_at": "2026-08-13T01:30:00Z",
        "window": {
            "baseline_start": "2026-08-13T01:00:00Z",
            "incident_start": "2026-08-13T01:30:00Z",
            "incident_end": None,
            "recovery_end": None,
        },
        "alert": {
            "fingerprint": "fixture-report-fingerprint",
            "name": "FixtureFailure",
            "labels": {
                "alertname": "FixtureFailure",
                "namespace": "online-boutique",
                "severity": "critical",
            },
            "annotations": {},
        },
        "source_entity": copy.deepcopy(evidence[0]["subject"]),
        "collector_statuses": statuses,
        "created_at": "2026-08-13T01:35:00Z",
        "updated_at": "2026-08-13T01:59:03Z",
    }
    validate_contract("incident.schema.json", incident)
    return incident


class FastPathReportTests(unittest.TestCase):
    def test_proven_decision_generates_context_json_and_markdown(self) -> None:
        _, evidence = load_fixture(FIXTURE_DIR / "oomkilled.json")
        decision = DeterministicRCAEngine().evaluate(evidence)

        artifacts = FastPathReportBuilder().build(
            incident=incident_for(evidence),
            evidence=evidence,
            decision=decision,
            generated_at=GENERATED_AT,
        )

        validate_contract("context-package.schema.json", artifacts.context)
        validate_contract("rca-report.schema.json", artifacts.report)
        self.assertEqual(artifacts.report["status"], "conclusive")
        self.assertEqual(artifacts.report["path"], "fast")
        self.assertEqual(artifacts.report["budget"]["llm_calls"], 0)
        self.assertEqual(artifacts.report["budget"]["tool_calls"], 0)
        self.assertEqual(
            artifacts.report["root_cause"]["supporting_evidence_ids"],
            list(decision.supporting_evidence_ids),
        )
        self.assertEqual(
            artifacts.context["localization"]["strategy"],
            "namespace-fallback",
        )
        for evidence_id in decision.supporting_evidence_ids:
            self.assertIn(evidence_id, artifacts.context["evidence_ids"])
            self.assertIn(evidence_id, artifacts.markdown)

    def test_abstain_report_has_no_root_cause_and_names_missing_evidence(self) -> None:
        _, evidence = load_fixture(FIXTURE_DIR / "insufficient-oom.json")
        decision = DeterministicRCAEngine().evaluate(evidence)

        artifacts = FastPathReportBuilder().build(
            incident=incident_for(evidence),
            evidence=evidence,
            decision=decision,
            generated_at=GENERATED_AT,
        )

        self.assertEqual(artifacts.report["status"], "inconclusive")
        self.assertIsNone(artifacts.report["root_cause"])
        self.assertEqual(artifacts.report["hypotheses"][0]["status"], "unresolved")
        self.assertIn(
            "Prometheus",
            artifacts.report["hypotheses"][0]["missing_evidence"][0],
        )
        self.assertEqual(artifacts.report["remediation"]["suggestions"], [])

    def test_collector_failure_marks_otherwise_proven_report_partial(self) -> None:
        _, evidence = load_fixture(FIXTURE_DIR / "missing-configmap.json")
        decision = DeterministicRCAEngine().evaluate(evidence)

        artifacts = FastPathReportBuilder().build(
            incident=incident_for(evidence, failed_collector=True),
            evidence=evidence,
            decision=decision,
            generated_at=GENERATED_AT,
        )

        self.assertEqual(artifacts.report["status"], "partial")
        self.assertEqual(
            artifacts.context["collector_failures"][0]["collector"], "logs"
        )
        self.assertTrue(
            any("Collector logs" in item for item in artifacts.report["limitations"])
        )

    def test_unknown_decision_evidence_reference_is_rejected(self) -> None:
        _, evidence = load_fixture(FIXTURE_DIR / "oomkilled.json")
        decision = DeterministicRCAEngine().evaluate(evidence)
        invalid = replace(
            decision,
            supporting_evidence_ids=("ev-invented-evidence-0001",),
        )

        with self.assertRaisesRegex(ContractViolation, "unknown Evidence"):
            FastPathReportBuilder().build(
                incident=incident_for(evidence),
                evidence=evidence,
                decision=invalid,
                generated_at=GENERATED_AT,
            )


if __name__ == "__main__":
    unittest.main()
