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


def load_fixture(path: Path, mutate_drafts=None):
    with path.open(encoding="utf-8") as handle:
        fixture = json.load(handle)
    if mutate_drafts is not None:
        mutate_drafts(fixture["evidence_drafts"])
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

    def test_oom_memory_metric_from_a_different_pod_uid_cannot_prove_the_cause(self) -> None:
        _, evidence = load_fixture(
            FIXTURE_DIR / "oomkilled.json",
            lambda drafts: drafts[2]["subject"].update(
                {"uid": "03a09977-3d7c-4df3-b89c-c760ef8509f0"}
            ),
        )

        decision = DeterministicRCAEngine().evaluate(evidence)

        self.assertEqual(decision.status, "ABSTAIN")
        self.assertIn("Prometheus", decision.missing_requirements[0])

    def test_oom_restart_metric_from_a_different_pod_uid_cannot_prove_the_cause(self) -> None:
        _, evidence = load_fixture(
            FIXTURE_DIR / "oomkilled.json",
            lambda drafts: drafts[1]["subject"].update(
                {"uid": "03a09977-3d7c-4df3-b89c-c760ef8509f0"}
            ),
        )

        decision = DeterministicRCAEngine().evaluate(evidence)

        self.assertEqual(decision.status, "ABSTAIN")
        self.assertIn("restart_count_delta", decision.missing_requirements[0])

    def test_kernel_memcg_signal_recovers_oom_when_kubernetes_reason_is_error(self) -> None:
        def use_kernel_signal(drafts):
            for draft in drafts:
                draft["subject"]["cluster_id"] = "agent-rca-dev"
            drafts[0]["facts"] = {
                "last_termination_reason": "Error",
                "last_termination_exit_code": 137,
            }
            drafts.append(
                {
                    "source": "loki",
                    "kind": "log-pattern",
                    "observed_at": "2026-08-12T01:04:58Z",
                    "subject": dict(drafts[0]["subject"]),
                    "summary": "Kernel cgroup OOM signal matched the Pod once.",
                    "facts": {
                        "pattern_id": "kernel-cgroup-oom",
                        "kernel_constraint": "CONSTRAINT_MEMCG",
                        "match_count": 1,
                        "pod_uid": drafts[0]["subject"]["uid"],
                        "first_match_at": "2026-08-12T01:04:58Z",
                        "last_match_at": "2026-08-12T01:04:58Z",
                    },
                    "provider": "loki-kernel-oom-provider",
                    "query": "scoped kernel OOM fixture query",
                    "locator": "loki://kernel-journal/checkoutservice-abc",
                }
            )

        _, evidence = load_fixture(
            FIXTURE_DIR / "oomkilled.json",
            use_kernel_signal,
        )

        decision = DeterministicRCAEngine().evaluate(evidence)

        self.assertEqual(decision.status, "PROVEN")
        self.assertEqual(
            decision.root_cause_id, "kubernetes.container-oomkilled"
        )
        self.assertIn("kernel recorded", decision.statement)
        self.assertEqual(len(decision.supporting_evidence_ids), 3)

    def test_exit_137_without_independent_oom_signal_does_not_apply(self) -> None:
        def use_ambiguous_sigkill(drafts):
            drafts[0]["facts"] = {
                "last_termination_reason": "Error",
                "last_termination_exit_code": 137,
            }

        _, evidence = load_fixture(
            FIXTURE_DIR / "oomkilled.json",
            use_ambiguous_sigkill,
        )

        decision = DeterministicRCAEngine().evaluate(evidence)
        oom_evaluation = next(
            item
            for item in decision.evaluations
            if item.rule_id == "kubernetes.container-oomkilled"
        )

        self.assertEqual(decision.status, "ABSTAIN")
        self.assertEqual(oom_evaluation.status, "NOT_APPLICABLE")

    def test_kernel_signal_from_an_untrusted_provider_does_not_apply(self) -> None:
        def use_untrusted_kernel_signal(drafts):
            for draft in drafts:
                draft["subject"]["cluster_id"] = "agent-rca-dev"
            drafts[0]["facts"] = {
                "last_termination_reason": "Error",
                "last_termination_exit_code": 137,
            }
            drafts.append(
                {
                    "source": "loki",
                    "kind": "log-pattern",
                    "observed_at": "2026-08-12T01:04:58Z",
                    "subject": dict(drafts[0]["subject"]),
                    "summary": "Untrusted OOM-like signal.",
                    "facts": {
                        "pattern_id": "kernel-cgroup-oom",
                        "kernel_constraint": "CONSTRAINT_MEMCG",
                        "match_count": 1,
                        "pod_uid": drafts[0]["subject"]["uid"],
                        "first_match_at": "2026-08-12T01:04:58Z",
                        "last_match_at": "2026-08-12T01:04:58Z",
                    },
                    "provider": "generic-log-provider",
                    "query": "fixture query",
                    "locator": "loki://fixture",
                }
            )

        _, evidence = load_fixture(
            FIXTURE_DIR / "oomkilled.json",
            use_untrusted_kernel_signal,
        )

        decision = DeterministicRCAEngine().evaluate(evidence)

        self.assertEqual(decision.status, "ABSTAIN")


if __name__ == "__main__":
    unittest.main()
