from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlsplit

from incident_platform.errors import PermanentProviderError
from incident_platform.providers.prometheus import (
    PrometheusHTTPAPI,
    PrometheusMetricProvider,
    PrometheusQuerySpec,
    PrometheusRangeResult,
)

from contract_suites import ProviderAdapterContract, contract_request


INCIDENT_ID = "inc-prometheus-provider-0001"


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
