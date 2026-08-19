from __future__ import annotations

import unittest

from incident_platform.errors import ContractViolation
from incident_platform.krca import (
    APIEdgeSignal,
    APIRef,
    KRCADrilldownLocalizer,
    KRCADrilldownPolicy,
    KRCADrilldownScorer,
)


ALERT = APIRef("frontend", "POST /checkout")


def edge(
    parent: APIRef,
    child: APIRef,
    *,
    failure_correlation: float,
    significant: bool = True,
    latency_anomaly: float = 0.1,
    latency_fluctuation: float = 0.1,
    latency_correlation: float = 0.1,
    evidence_suffix: str,
) -> APIEdgeSignal:
    return APIEdgeSignal(
        parent=parent,
        child=child,
        failure_rate_correlation=failure_correlation,
        failure_rate_p_value=0.01 if significant else 0.20,
        latency_anomaly=latency_anomaly,
        latency_fluctuation_contribution=latency_fluctuation,
        latency_correlation=latency_correlation,
        evidence_ids=(f"ev-krca-{evidence_suffix}-0001",),
    )


class KRCADrilldownScorerTests(unittest.TestCase):
    def test_score_uses_maximum_of_significant_failure_and_composite_latency(self) -> None:
        policy = KRCADrilldownPolicy(propagation_threshold=0.8)
        signal = edge(
            ALERT,
            APIRef("payment", "Charge"),
            failure_correlation=0.91,
            latency_anomaly=0.5,
            latency_fluctuation=0.7,
            latency_correlation=0.6,
            evidence_suffix="score",
        )

        scored = KRCADrilldownScorer(policy).score(signal)

        self.assertEqual(scored.failure_rate_score, 0.91)
        self.assertEqual(scored.latency_score, 0.63)
        self.assertEqual(scored.score, 0.91)
        self.assertTrue(scored.retained)

    def test_insignificant_failure_correlation_is_zeroed(self) -> None:
        policy = KRCADrilldownPolicy(propagation_threshold=0.8)
        signal = edge(
            ALERT,
            APIRef("payment", "Charge"),
            failure_correlation=0.99,
            significant=False,
            latency_anomaly=0.2,
            latency_fluctuation=0.2,
            latency_correlation=0.2,
            evidence_suffix="pvalue",
        )

        scored = KRCADrilldownScorer(policy).score(signal)

        self.assertEqual(scored.failure_rate_score, 0.0)
        self.assertEqual(scored.score, 0.2)
        self.assertFalse(scored.retained)


class KRCADrilldownLocalizerTests(unittest.TestCase):
    def test_recursive_drilldown_keeps_only_high_scoring_paths(self) -> None:
        payment = APIRef("payment", "Charge")
        ledger = APIRef("ledger", "Commit")
        recommendation = APIRef("recommendation", "List")
        email = APIRef("email", "Send")
        signals = (
            edge(
                ALERT,
                payment,
                failure_correlation=0.90,
                evidence_suffix="frontend-payment",
            ),
            edge(
                ALERT,
                recommendation,
                failure_correlation=0.30,
                evidence_suffix="frontend-recommendation",
            ),
            edge(
                payment,
                ledger,
                failure_correlation=0.86,
                evidence_suffix="payment-ledger",
            ),
            edge(
                payment,
                email,
                failure_correlation=0.79,
                evidence_suffix="payment-email",
            ),
        )
        localizer = KRCADrilldownLocalizer(
            policy=KRCADrilldownPolicy(top_n_services=3)
        )

        run = localizer.localize(ALERT, signals)

        self.assertEqual(run.stop_reason, "TOP_N_READY")
        self.assertFalse(run.budget_exhausted)
        self.assertFalse(run.requires_fallback)
        self.assertEqual(
            [candidate.api.service for candidate in run.top_services],
            ["payment", "ledger"],
        )
        ledger_candidate = next(
            candidate for candidate in run.candidates if candidate.api == ledger
        )
        self.assertEqual(
            [api.service for api in ledger_candidate.path],
            ["frontend", "payment", "ledger"],
        )
        self.assertEqual(len(ledger_candidate.evidence_ids), 2)
        retained = {
            (item.signal.parent.key, item.signal.child.key): item.retained
            for item in run.scored_edges
        }
        self.assertFalse(retained[(ALERT.key, recommendation.key)])
        self.assertFalse(retained[(payment.key, email.key)])

    def test_top_n_retains_next_ranked_services_for_bounded_fallback(self) -> None:
        services = (
            ("payment", 0.95),
            ("ledger", 0.90),
            ("inventory", 0.85),
            ("database", 0.83),
        )
        signals = tuple(
            edge(
                ALERT,
                APIRef(service, "Handle"),
                failure_correlation=score,
                evidence_suffix=service,
            )
            for service, score in services
        )
        localizer = KRCADrilldownLocalizer(
            policy=KRCADrilldownPolicy(top_n_services=3)
        )

        run = localizer.localize(ALERT, signals)

        self.assertEqual(
            [candidate.api.service for candidate in run.top_services],
            ["payment", "ledger", "inventory"],
        )
        self.assertEqual(
            [candidate.api.service for candidate in run.next_ranked_candidates],
            ["database"],
        )

    def test_missing_observability_requires_fallback_instead_of_a_root_cause(self) -> None:
        run = KRCADrilldownLocalizer().localize(ALERT, ())

        self.assertEqual(run.stop_reason, "NO_SUSPICIOUS_DOWNSTREAM")
        self.assertEqual(run.top_services, ())
        self.assertTrue(run.requires_fallback)

    def test_depth_budget_marks_partial_drilldown_for_fallback(self) -> None:
        payment = APIRef("payment", "Charge")
        ledger = APIRef("ledger", "Commit")
        signals = (
            edge(
                ALERT,
                payment,
                failure_correlation=0.90,
                evidence_suffix="depth-payment",
            ),
            edge(
                payment,
                ledger,
                failure_correlation=0.90,
                evidence_suffix="depth-ledger",
            ),
        )
        localizer = KRCADrilldownLocalizer(
            policy=KRCADrilldownPolicy(max_depth=1)
        )

        run = localizer.localize(ALERT, signals)

        self.assertEqual(run.stop_reason, "DRILLDOWN_BUDGET_EXHAUSTED")
        self.assertTrue(run.budget_exhausted)
        self.assertTrue(run.requires_fallback)
        self.assertEqual(
            [candidate.api.service for candidate in run.top_services],
            ["payment"],
        )

    def test_duplicate_edge_signal_is_rejected(self) -> None:
        signal = edge(
            ALERT,
            APIRef("payment", "Charge"),
            failure_correlation=0.90,
            evidence_suffix="duplicate",
        )

        with self.assertRaisesRegex(ContractViolation, "duplicate API edge"):
            KRCADrilldownLocalizer().localize(ALERT, (signal, signal))


if __name__ == "__main__":
    unittest.main()
