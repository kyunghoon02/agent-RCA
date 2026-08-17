from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from incident_platform.deterministic import DeterministicRCAEngine
from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceDraft,
    EvidenceWindow,
    ResourceScope,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "deterministic"
COLLECTED_AT = datetime(2026, 8, 12, 5, 0, tzinfo=timezone.utc)


def load_fixture(path: Path):
    with path.open(encoding="utf-8") as handle:
        fixture = json.load(handle)
    names = tuple(
        dict.fromkeys(draft["subject"]["name"] for draft in fixture["evidence_drafts"])
    )
    request = CollectionRequest(
        request_id=f"req-{path.stem}-fixture",
        incident_id=fixture["incident_id"],
        window=EvidenceWindow(
            start="2026-08-12T00:30:00Z",
            end="2026-08-12T05:00:00Z",
        ),
        scope=ResourceScope(
            namespace="online-boutique",
            resource_names=names,
        ),
        timeout_seconds=1,
    )
    builder = EvidenceBuilder()
    evidence = [
        builder.build(
            EvidenceDraft(**draft),
            request,
            collected_at=COLLECTED_AT,
        )
        for draft in fixture["evidence_drafts"]
    ]
    return fixture, evidence


class DeterministicRCAFixtureTests(unittest.TestCase):
    def test_registered_fixtures_match_expected_decisions(self) -> None:
        paths = sorted(FIXTURE_DIR.glob("*.json"))
        self.assertEqual(len(paths), 4)
        engine = DeterministicRCAEngine()

        for path in paths:
            with self.subTest(fixture=path.name):
                fixture, evidence = load_fixture(path)
                decision = engine.evaluate(evidence)
                self.assertEqual(decision.status, fixture["expected_status"])
                self.assertEqual(
                    decision.root_cause_id,
                    fixture["expected_root_cause_id"],
                )

    def test_oom_without_metric_corroboration_abstains_with_missing_reason(self) -> None:
        fixture, evidence = load_fixture(FIXTURE_DIR / "insufficient-oom.json")
        decision = DeterministicRCAEngine().evaluate(evidence)

        self.assertEqual(decision.status, fixture["expected_status"])
        self.assertTrue(decision.missing_requirements)
        self.assertIn("Prometheus", decision.missing_requirements[0])


if __name__ == "__main__":
    unittest.main()
