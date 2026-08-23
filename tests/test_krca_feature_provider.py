from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timezone

from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    ResourceScope,
    validate_provider_batch,
)
from incident_platform.errors import PermanentProviderError
from incident_platform.krca import APIEdgeSignal, APIRef, KRCADrilldownLocalizer
from incident_platform.krca_pipeline import (
    EvidenceBackedKRCADrilldownService,
    KRCAMetricLocalizationRun,
    KRCATopServiceLocalizationService,
    KRCATopServiceScopeResolver,
)
from incident_platform.localization import IncidentLocalizationService
from incident_platform.projectors import KubernetesEvidenceProjector
from incident_platform.providers.krca_metrics import (
    APIDependencySpec,
    PrometheusAPIFeatureProvider,
    PrometheusAPIFeatureQuerySpec,
)
from incident_platform.providers.prometheus import PrometheusRangeResult
from incident_platform.repository import InMemoryIncidentRepository
from incident_platform.resolution import ServiceToEntityResolver
from incident_platform.stategraph import InMemoryStateGraphRepository

from tests.test_entity_resolution import CLUSTER_ID, service_evidence
from tests.test_incident_localization_service import FROZEN_AT, incident_for
from tests.test_stategraph import WINDOW
from contract_suites import ProviderAdapterContract


UTC = timezone.utc
INCIDENT_ID = "inc-stategraph-fixture-0001"
FRONTEND = APIRef("frontend", "Checkout")
CHECKOUT = APIRef("checkoutservice", "PlaceOrder")
EDGE = APIDependencySpec("frontend-checkout", FRONTEND, CHECKOUT)
TIMESTAMPS = tuple(
    datetime(2026, 8, 12, 1, minute, tzinfo=UTC).timestamp()
    for minute in range(6)
)


def query_spec(**overrides) -> PrometheusAPIFeatureQuerySpec:
    values = {
        "failure_rate_template": "failure_ratio{{scope}}",
        "latency_template": "latency_seconds{{scope}}",
        "qps_template": "request_qps{{scope}}",
        "latency_baseline_template": "latency_baseline{{scope}}",
        "step_seconds": 60,
        "minimum_aligned_samples": 4,
        "maximum_time_lag": 2,
    }
    values.update(overrides)
    return PrometheusAPIFeatureQuerySpec(**values)


def feature_request() -> CollectionRequest:
    return CollectionRequest(
        request_id="req-krca-feature-0001",
        incident_id=INCIDENT_ID,
        window=WINDOW,
        scope=ResourceScope(
            namespace="online-boutique",
            resource_names=("frontend", "checkoutservice"),
            max_items=10,
        ),
        timeout_seconds=5,
    )


class StaticAPIFeatureClient:
    def __init__(self, values=None, *, label_override=None, warnings=tuple()) -> None:
        self.values = values or default_values()
        self.label_override = label_override or {}
        self.warnings = tuple(warnings)
        self.calls = []

    def query_range(self, expression: str, **kwargs) -> PrometheusRangeResult:
        self.calls.append((expression, kwargs))
        metric = expression.split("{", 1)[0]
        service = _label(expression, "service")
        operation = _label(expression, "operation")
        key = (metric, service, operation)
        samples = self.values.get(key)
        if samples is None:
            return PrometheusRangeResult(tuple(), self.warnings)
        labels = {
            "namespace": "online-boutique",
            "service": service,
            "operation": operation,
            **self.label_override,
        }
        return PrometheusRangeResult(
            (
                {
                    "metric": labels,
                    "values": [
                        [timestamp, str(value)]
                        for timestamp, value in zip(TIMESTAMPS, samples)
                    ],
                },
            ),
            self.warnings,
        )


def _label(expression: str, name: str) -> str:
    match = re.search(rf'{name}=("(?:[^"\\]|\\.)*")', expression)
    if match is None:
        raise AssertionError(f"missing {name} label in {expression}")
    return json.loads(match.group(1))


def default_values() -> dict:
    child_failure = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    parent_failure = [0.0, 0.0, 0.2, 0.4, 0.6, 0.8]
    parent_latency = [0.5, 0.6, 0.8, 1.0, 1.2, 1.4]
    child_latency = [0.5, 0.7, 0.9, 1.1, 1.3, 1.5]
    return {
        ("failure_ratio", FRONTEND.service, FRONTEND.operation): parent_failure,
        ("failure_ratio", CHECKOUT.service, CHECKOUT.operation): child_failure,
        ("latency_seconds", FRONTEND.service, FRONTEND.operation): parent_latency,
        ("latency_seconds", CHECKOUT.service, CHECKOUT.operation): child_latency,
        ("request_qps", FRONTEND.service, FRONTEND.operation): [100.0] * 6,
        ("request_qps", CHECKOUT.service, CHECKOUT.operation): [80.0] * 6,
        ("latency_baseline", CHECKOUT.service, CHECKOUT.operation): [0.5] * 6,
    }


def build_feature_evidence(client=None) -> tuple[dict, PrometheusAPIFeatureProvider]:
    client = client or StaticAPIFeatureClient()
    provider = PrometheusAPIFeatureProvider(client, (EDGE,), query_spec())
    request = feature_request()
    batch = provider.collect(request)
    validate_provider_batch(batch, request)
    evidence = EvidenceBuilder().build(
        batch.items[0],
        request,
        collected_at=datetime(2026, 8, 12, 1, 6, tzinfo=UTC),
    )
    return evidence, provider


class PrometheusAPIFeatureProviderTests(unittest.TestCase):
    def test_adapter_passes_reusable_provider_contract(self) -> None:
        provider = PrometheusAPIFeatureProvider(
            StaticAPIFeatureClient(),
            (EDGE,),
            query_spec(),
        )

        ProviderAdapterContract.verify(self, provider, feature_request())

    def test_bounded_queries_compute_dynamic_lagged_features_without_raw_samples(self) -> None:
        client = StaticAPIFeatureClient()

        evidence, _ = build_feature_evidence(client)

        self.assertEqual(len(client.calls), 7)
        self.assertTrue(
            all("{{" not in expression for expression, _ in client.calls)
        )
        self.assertEqual(evidence["facts"]["result_status"], "HAS_DATA")
        self.assertEqual(evidence["facts"]["failure_rate_correlation"], 1.0)
        self.assertEqual(evidence["facts"]["failure_rate_p_value"], 0.0)
        self.assertEqual(
            evidence["facts"]["computation"]["selected_failure_lag"], 1
        )
        self.assertNotIn("samples", str(evidence["facts"]))

    def test_missing_required_series_is_explicit_and_not_projectable(self) -> None:
        values = default_values()
        del values[("latency_baseline", CHECKOUT.service, CHECKOUT.operation)]
        client = StaticAPIFeatureClient(values)
        provider = PrometheusAPIFeatureProvider(client, (EDGE,), query_spec())
        request = feature_request()

        batch = provider.collect(request)
        evidence = EvidenceBuilder().build(
            batch.items[0],
            request,
            collected_at=datetime(2026, 8, 12, 1, 6, tzinfo=UTC),
        )
        feature_run = EvidenceBackedKRCADrilldownService().localize(
            INCIDENT_ID,
            window=WINDOW,
            alerting_api=FRONTEND,
            evidence=(evidence,),
        )

        self.assertEqual(batch.status, "PARTIAL")
        self.assertEqual(evidence["facts"]["result_status"], "INSUFFICIENT_DATA")
        self.assertEqual(feature_run.drilldown.stop_reason, "NO_SUSPICIOUS_DOWNSTREAM")
        self.assertEqual(
            feature_run.unavailable_feature_evidence_ids,
            (evidence["evidence_id"],),
        )

    def test_out_of_scope_api_labels_are_rejected(self) -> None:
        provider = PrometheusAPIFeatureProvider(
            StaticAPIFeatureClient(label_override={"service": "paymentservice"}),
            (EDGE,),
            query_spec(),
        )

        with self.assertRaisesRegex(PermanentProviderError, "outside"):
            provider.collect(feature_request())

    def test_query_budget_is_checked_before_prometheus_calls(self) -> None:
        client = StaticAPIFeatureClient()
        provider = PrometheusAPIFeatureProvider(
            client,
            (EDGE,),
            query_spec(),
            max_queries=6,
        )

        with self.assertRaisesRegex(PermanentProviderError, "query budget"):
            provider.collect(feature_request())

        self.assertEqual(client.calls, [])

    def test_sample_truncation_never_creates_a_partial_feature_vector(self) -> None:
        client = StaticAPIFeatureClient()
        provider = PrometheusAPIFeatureProvider(
            client,
            (EDGE,),
            query_spec(max_samples_per_query=3),
        )

        batch = provider.collect(feature_request())

        self.assertEqual(batch.status, "PARTIAL")
        self.assertEqual(batch.items[0].facts["result_status"], "INSUFFICIENT_DATA")
        self.assertTrue(
            any(
                reason.startswith("TRUNCATED_")
                for reason in batch.items[0].facts["reason_codes"]
            )
        )


class KRCAMetricPipelineTests(unittest.TestCase):
    def test_feature_evidence_drives_top_service_resolution_and_localization(self) -> None:
        feature_evidence, _ = build_feature_evidence()
        feature_run = EvidenceBackedKRCADrilldownService().localize(
            INCIDENT_ID,
            window=WINDOW,
            alerting_api=FRONTEND,
            evidence=(feature_evidence,),
        )
        kubernetes_evidence = service_evidence()
        projector = KubernetesEvidenceProjector()
        graph_repository = InMemoryStateGraphRepository()
        graph_repository.ingest(projector.project(kubernetes_evidence).records)
        incident_repository = InMemoryIncidentRepository()
        incident = incident_for((kubernetes_evidence, feature_evidence))
        incident_repository.create_or_get_by_deduplication_key(
            incident,
            occurred_at=FROZEN_AT,
        )
        incident_repository.store_evidence(
            incident["incident_id"],
            (kubernetes_evidence, feature_evidence),
        )
        service = KRCATopServiceLocalizationService(
            KRCATopServiceScopeResolver(ServiceToEntityResolver(graph_repository)),
            IncidentLocalizationService(
                incident_repository,
                graph_repository,
                (projector,),
            ),
        )

        run = service.localize(
            feature_run,
            cluster_id=CLUSTER_ID,
            namespace="online-boutique",
            frozen_at=FROZEN_AT,
            max_entities=4,
            max_depth=1,
        )

        self.assertEqual(
            [item.api.service for item in feature_run.drilldown.top_services],
            ["checkoutservice"],
        )
        self.assertTrue(run.resolution.complete)
        self.assertEqual(len(run.resolution.seed_entity_ids), 1)
        self.assertIsNotNone(run.localization)
        assert run.localization is not None
        self.assertEqual(run.localization.incident["status"], "ANALYZING")

    def test_one_unresolved_top_service_blocks_partial_scope_creation(self) -> None:
        checkout_signal = APIEdgeSignal(
            parent=FRONTEND,
            child=CHECKOUT,
            failure_rate_correlation=0.95,
            failure_rate_p_value=0.001,
            latency_anomaly=0.0,
            latency_fluctuation_contribution=0.0,
            latency_correlation=0.0,
            evidence_ids=("ev-krca-checkout-signal-0001",),
        )
        payment_signal = APIEdgeSignal(
            parent=FRONTEND,
            child=APIRef("paymentservice", "Charge"),
            failure_rate_correlation=0.90,
            failure_rate_p_value=0.001,
            latency_anomaly=0.0,
            latency_fluctuation_contribution=0.0,
            latency_correlation=0.0,
            evidence_ids=("ev-krca-payment-signal-0001",),
        )
        feature_run = KRCAMetricLocalizationRun(
            incident_id=INCIDENT_ID,
            window=WINDOW,
            drilldown=KRCADrilldownLocalizer().localize(
                FRONTEND,
                (checkout_signal, payment_signal),
            ),
            consumed_evidence_ids=(
                "ev-krca-checkout-signal-0001",
                "ev-krca-payment-signal-0001",
            ),
            unavailable_feature_evidence_ids=tuple(),
        )
        projector = KubernetesEvidenceProjector()
        repository = InMemoryStateGraphRepository()
        repository.ingest(projector.project(service_evidence()).records)

        resolution = KRCATopServiceScopeResolver(
            ServiceToEntityResolver(repository)
        ).resolve(
            feature_run,
            cluster_id=CLUSTER_ID,
            namespace="online-boutique",
        )

        self.assertFalse(resolution.complete)
        self.assertEqual(
            [item.status for item in resolution.resolutions],
            ["RESOLVED", "NOT_FOUND"],
        )
        self.assertIsNone(resolution.scope)


if __name__ == "__main__":
    unittest.main()
