from __future__ import annotations

import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from incident_platform.deterministic import DeterministicRCAEngine
from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceDraft,
    ResourceScope,
    validate_provider_batch,
)
from incident_platform.errors import PermanentProviderError
from incident_platform.providers.prometheus import (
    PrometheusHTTPAPI,
    PrometheusMetricProvider,
    PrometheusQuerySpec,
    PrometheusRangeResult,
    PrometheusWorkloadMetricProvider,
)

from contract_suites import ProviderAdapterContract, contract_request


INCIDENT_ID = "inc-prometheus-provider-0001"
CLUSTER_ID = "agent-rca-dev"
POD_NAME = "checkoutservice-7d9f8-q1w2e"
POD_UID = "7df6d266-40df-4fd6-942d-7ebc864c4061"


class StaticPrometheusClient:
    def __init__(self, result: PrometheusRangeResult) -> None:
        self.result = result
        self.calls = []

    def query_range(self, expression: str, **kwargs):
        self.calls.append((expression, kwargs))
        return self.result


def memory_query() -> PrometheusQuerySpec:
    return PrometheusQuerySpec(
        query_id="memory_working_set_ratio",
        expression_template=(
            "max by (namespace, service) "
            "(container_memory_working_set_ratio{{scope}})"
        ),
        namespace_label="namespace",
        resource_label="service",
        peak_fact="peak_ratio",
    )


def pod_memory_query() -> PrometheusQuerySpec:
    return PrometheusQuerySpec(
        query_id="memory_working_set_ratio",
        expression_template="agent_rca_pod_memory_working_set_ratio{{scope}}",
        namespace_label="namespace",
        resource_label="pod",
        subject_kind="Pod",
        uid_label="uid",
        step_seconds=15,
        peak_fact="peak_ratio",
    )


def pod_restart_query() -> PrometheusQuerySpec:
    return PrometheusQuerySpec(
        query_id="restart_count_delta",
        expression_template="agent_rca_pod_restart_count_delta{{scope}}",
        namespace_label="namespace",
        resource_label="pod",
        subject_kind="Pod",
        uid_label="uid",
        step_seconds=15,
        peak_fact="peak_delta",
    )


def workload_request() -> CollectionRequest:
    request = contract_request(INCIDENT_ID, "checkoutservice")
    return CollectionRequest(
        request_id=request.request_id,
        incident_id=request.incident_id,
        window=request.window,
        scope=ResourceScope(
            namespace=request.scope.namespace,
            resource_names=request.scope.resource_names,
            resource_name_prefixes=("checkoutservice-",),
            max_items=10,
        ),
        timeout_seconds=request.timeout_seconds,
    )


class PrometheusMetricProviderTests(unittest.TestCase):
    def test_scoped_series_becomes_summary_and_passes_provider_contract(self) -> None:
        client = StaticPrometheusClient(
            PrometheusRangeResult(
                series=(
                    {
                        "metric": {
                            "namespace": "online-boutique",
                            "service": "checkoutservice",
                        },
                        "values": [
                            [1786496640, "0.42"],
                            [1786496699, "0.97"],
                        ],
                    },
                )
            )
        )
        provider = PrometheusMetricProvider(client, [memory_query()])
        request = contract_request(INCIDENT_ID, "checkoutservice")

        ProviderAdapterContract.verify(self, provider, request)

        expression = client.calls[0][0]
        self.assertIn('namespace="online-boutique"', expression)
        self.assertIn('service=~"^(?:checkoutservice)$"', expression)
        self.assertNotIn("{{", expression)
        draft = provider.collect(request).items[0]
        self.assertEqual(draft.facts["result_status"], "HAS_DATA")
        self.assertEqual(draft.facts["sample_count"], 2)
        self.assertEqual(draft.facts["peak_ratio"], 0.97)

    def test_runtime_cluster_identity_is_copied_from_trusted_configuration(self) -> None:
        provider = PrometheusMetricProvider(
            StaticPrometheusClient(PrometheusRangeResult(series=tuple())),
            [memory_query()],
            cluster_id="agent-rca-dev",
        )

        draft = provider.collect(
            contract_request(INCIDENT_ID, "checkoutservice")
        ).items[0]

        self.assertEqual(draft.subject["cluster_id"], "agent-rca-dev")

    def test_no_series_is_explicit_no_data_evidence(self) -> None:
        provider = PrometheusMetricProvider(
            StaticPrometheusClient(PrometheusRangeResult(series=tuple())),
            [memory_query()],
        )

        batch = provider.collect(contract_request(INCIDENT_ID, "checkoutservice"))

        self.assertEqual(batch.status, "SUCCEEDED")
        self.assertEqual(len(batch.items), 1)
        self.assertEqual(batch.items[0].facts["result_status"], "NO_DATA")
        self.assertEqual(batch.items[0].facts["sample_count"], 0)

    def test_out_of_scope_series_is_rejected(self) -> None:
        client = StaticPrometheusClient(
            PrometheusRangeResult(
                series=(
                    {
                        "metric": {
                            "namespace": "online-boutique",
                            "service": "paymentservice",
                        },
                        "values": [[1786496699, "1"]],
                    },
                )
            )
        )
        provider = PrometheusMetricProvider(client, [memory_query()])

        with self.assertRaisesRegex(PermanentProviderError, "outside resource scope"):
            provider.collect(contract_request(INCIDENT_ID, "checkoutservice"))

    def test_sample_limit_returns_partial_summary(self) -> None:
        spec = PrometheusQuerySpec(
            query_id="request_error_ratio",
            expression_template="request_error_ratio{{scope}}",
            namespace_label="namespace",
            resource_label="service",
            max_samples=1,
        )
        client = StaticPrometheusClient(
            PrometheusRangeResult(
                series=(
                    {
                        "metric": {
                            "namespace": "online-boutique",
                            "service": "checkoutservice",
                        },
                        "values": [[1786496640, "0.1"], [1786496699, "0.2"]],
                    },
                )
            )
        )

        batch = PrometheusMetricProvider(client, [spec]).collect(
            contract_request(INCIDENT_ID, "checkoutservice")
        )

        self.assertEqual(batch.status, "PARTIAL")
        self.assertIn("samples exceeded", batch.error)
        self.assertEqual(batch.items[0].facts["sample_count"], 1)
        self.assertEqual(batch.items[0].completeness, 0.5)


class PrometheusWorkloadMetricProviderTests(unittest.TestCase):
    def provider_for(self, series) -> PrometheusWorkloadMetricProvider:
        return PrometheusWorkloadMetricProvider(
            StaticPrometheusClient(PrometheusRangeResult(series=tuple(series))),
            (pod_memory_query(),),
            cluster_id=CLUSTER_ID,
        )

    def test_pod_series_becomes_uid_backed_workload_evidence(self) -> None:
        client = StaticPrometheusClient(
            PrometheusRangeResult(
                series=(
                    {
                        "metric": {
                            "namespace": "online-boutique",
                            "pod": POD_NAME,
                            "uid": POD_UID,
                        },
                        "values": [
                            [1786496640, "0.42"],
                            [1786496699, "0.99"],
                        ],
                    },
                )
            )
        )
        provider = PrometheusWorkloadMetricProvider(
            client,
            (pod_memory_query(),),
            cluster_id=CLUSTER_ID,
        )
        request = workload_request()

        batch = provider.collect(request)
        validate_provider_batch(batch, request)

        self.assertEqual(len(batch.items), 1)
        draft = batch.items[0]
        self.assertEqual(draft.subject["kind"], "Pod")
        self.assertEqual(draft.subject["name"], POD_NAME)
        self.assertEqual(draft.subject["uid"], POD_UID)
        self.assertEqual(draft.subject["cluster_id"], CLUSTER_ID)
        self.assertEqual(draft.facts["peak_ratio"], 0.99)
        expression = client.calls[0][0]
        self.assertIn('namespace="online-boutique"', expression)
        self.assertIn("checkoutservice", expression)
        self.assertIn(".*", expression)
        self.assertNotIn("{scope}", expression)

    def test_restart_delta_series_becomes_a_numeric_peak_fact(self) -> None:
        client = StaticPrometheusClient(
            PrometheusRangeResult(
                series=(
                    {
                        "metric": {
                            "namespace": "online-boutique",
                            "pod": POD_NAME,
                            "uid": POD_UID,
                        },
                        "values": [
                            [1786496640, "0"],
                            [1786496699, "1"],
                        ],
                    },
                )
            )
        )
        provider = PrometheusWorkloadMetricProvider(
            client,
            (pod_restart_query(),),
            cluster_id=CLUSTER_ID,
        )

        draft = provider.collect(workload_request()).items[0]

        self.assertEqual(draft.facts["metric"], "restart_count_delta")
        self.assertEqual(draft.facts["peak_delta"], 1.0)
        self.assertEqual(draft.subject["uid"], POD_UID)

    def test_workload_outside_rooted_prefix_is_rejected(self) -> None:
        provider = self.provider_for(
            (
                {
                    "metric": {
                        "namespace": "online-boutique",
                        "pod": "paymentservice-7d9f8-q1w2e",
                        "uid": POD_UID,
                    },
                    "values": [[1786496699, "0.99"]],
                },
            )
        )

        with self.assertRaisesRegex(PermanentProviderError, "outside resource scope"):
            provider.collect(workload_request())

    def test_workload_without_pod_uid_is_rejected(self) -> None:
        provider = self.provider_for(
            (
                {
                    "metric": {
                        "namespace": "online-boutique",
                        "pod": POD_NAME,
                    },
                    "values": [[1786496699, "0.99"]],
                },
            )
        )

        with self.assertRaisesRegex(PermanentProviderError, "Pod UID"):
            provider.collect(workload_request())

    def test_workload_collection_requires_rooted_prefixes(self) -> None:
        provider = self.provider_for(tuple())

        with self.assertRaisesRegex(PermanentProviderError, "rooted resource prefixes"):
            provider.collect(contract_request(INCIDENT_ID, "checkoutservice"))

    def test_no_matching_series_returns_no_fabricated_pod_evidence(self) -> None:
        batch = self.provider_for(tuple()).collect(workload_request())

        self.assertEqual(batch.status, "SUCCEEDED")
        self.assertEqual(batch.items, tuple())

    def test_uid_backed_workload_evidence_proves_matching_pod_oom(self) -> None:
        request = workload_request()
        metric_draft = self.provider_for(
            (
                {
                    "metric": {
                        "namespace": "online-boutique",
                        "pod": POD_NAME,
                        "uid": POD_UID,
                    },
                    "values": [[1786496699, "0.99"]],
                },
            )
        ).collect(request).items[0]
        restart_draft = EvidenceDraft(
            source="prometheus",
            kind="metric-summary",
            observed_at=request.window.end,
            subject=metric_draft.subject,
            summary="Pod restart counter increased in the bounded window.",
            facts={
                "metric": "restart_count_delta",
                "result_status": "HAS_DATA",
                "sample_count": 1,
                "minimum": 1.0,
                "maximum": 1.0,
                "average": 1.0,
                "latest": 1.0,
                "peak_delta": 1.0,
            },
            provider="prometheus-http-api",
            query="agent_rca_pod_restart_count_delta scoped-query",
            locator=f"prometheus://query/restarts/Pod/{POD_NAME}",
        )
        kubernetes_draft = EvidenceDraft(
            source="kubernetes",
            kind="resource-state",
            observed_at=request.window.end,
            subject={
                "api_version": "v1",
                "kind": "Pod",
                "namespace": request.scope.namespace,
                "name": POD_NAME,
                "uid": POD_UID,
                "cluster_id": CLUSTER_ID,
                "exists": True,
            },
            summary="Pod restarted after an OOMKilled termination.",
            facts={
                "last_termination_reason": "OOMKilled",
            },
            provider="kubernetes-api",
            query=f"get Pod {POD_NAME}",
            locator=f"kubernetes://{CLUSTER_ID}/online-boutique/Pod/{POD_NAME}",
        )
        builder = EvidenceBuilder()
        collected_at = datetime(2026, 8, 12, 1, 5, tzinfo=timezone.utc)
        evidence = (
            builder.build(kubernetes_draft, request, collected_at=collected_at),
            builder.build(restart_draft, request, collected_at=collected_at),
            builder.build(metric_draft, request, collected_at=collected_at),
        )

        decision = DeterministicRCAEngine().evaluate(evidence)

        self.assertEqual(decision.status, "PROVEN")
        self.assertEqual(decision.root_cause_id, "kubernetes.container-oomkilled")


class RecordingTransport:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = []

    def get_json(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.payload


class PrometheusHTTPAPITests(unittest.TestCase):
    def test_range_query_uses_v1_endpoint_and_bounded_parameters(self) -> None:
        transport = RecordingTransport(
            {
                "status": "success",
                "data": {"resultType": "matrix", "result": []},
            }
        )
        client = PrometheusHTTPAPI(
            "http://prometheus.observability.svc:9090",
            bearer_token="test-bearer-value",
            transport=transport,
        )

        result = client.query_range(
            "up{namespace=\"online-boutique\"}",
            start="2026-08-12T00:30:00Z",
            end="2026-08-12T01:05:00Z",
            step_seconds=30,
            timeout_seconds=2.5,
        )

        self.assertEqual(result.series, tuple())
        url, kwargs = transport.calls[0]
        parsed = urlsplit(url)
        parameters = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/api/v1/query_range")
        self.assertEqual(parameters["step"], ["30s"])
        self.assertEqual(parameters["timeout"], ["3s"])
        self.assertEqual(kwargs["timeout_seconds"], 2.5)
        self.assertEqual(
            kwargs["headers"]["Authorization"], "Bearer test-bearer-value"
        )

    def test_base_url_rejects_embedded_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "credentials"):
            PrometheusHTTPAPI("https://user:password@prometheus.example")


if __name__ == "__main__":
    unittest.main()
