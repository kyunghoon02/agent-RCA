from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlsplit

from incident_platform.errors import PermanentProviderError
from incident_platform.providers.kubernetes import (
    KubernetesEventPage,
    KubernetesHTTPAPI,
    KubernetesResourceSpec,
    KubernetesStateProvider,
)
from incident_platform.providers.http import ProviderPageExpired

from contract_suites import ProviderAdapterContract, contract_request


INCIDENT_ID = "inc-kubernetes-provider-0001"


class StaticKubernetesClient:
    def __init__(self, resource=None, pages=None) -> None:
        self.resource = resource
        self.pages = list(pages or [KubernetesEventPage(tuple())])
        self.get_calls = []
        self.event_calls = []

    def get_resource(self, resource, **kwargs):
        self.get_calls.append((resource, kwargs))
        return self.resource

    def list_event_page(self, **kwargs):
        self.event_calls.append(kwargs)
        if len(self.pages) > 1:
            return self.pages.pop(0)
        return self.pages[0]


def pod_resource() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "checkoutservice",
            "namespace": "online-boutique",
            "uid": "7df6d266-40df-4fd6-942d-7ebc864c4061",
            "resourceVersion": "421",
            "generation": 1,
        },
        "spec": {
            "containers": [
                {
                    "name": "server",
                    "env": [{"name": "PASSWORD", "value": "must-not-be-copied"}],
                }
            ]
        },
        "status": {
            "phase": "Running",
            "qosClass": "Burstable",
            "containerStatuses": [
                {
                    "name": "server",
                    "ready": False,
                    "restartCount": 2,
                    "state": {"waiting": {"reason": "ImagePullBackOff"}},
                    "lastState": {"terminated": {"reason": "Error", "exitCode": 1}},
                }
            ],
        },
    }


def warning_event() -> dict:
    return {
        "metadata": {
            "name": "checkout-image-pull",
            "namespace": "online-boutique",
            "creationTimestamp": "2026-08-12T01:04:58Z",
        },
        "involvedObject": {
            "apiVersion": "v1",
            "kind": "Pod",
            "namespace": "online-boutique",
            "name": "checkoutservice",
            "uid": "7df6d266-40df-4fd6-942d-7ebc864c4061",
        },
        "type": "Warning",
        "reason": "ImagePullBackOff",
        "message": "Back-off pulling image token=must-be-redacted",
        "count": 3,
        "lastTimestamp": "2026-08-12T01:04:58Z",
        "source": {"component": "kubelet"},
    }


class KubernetesStateProviderTests(unittest.TestCase):
    def test_pod_state_and_event_are_safe_and_contract_valid(self) -> None:
        client = StaticKubernetesClient(
            pod_resource(),
            [KubernetesEventPage((warning_event(),))],
        )
        provider = KubernetesStateProvider(
            client,
            KubernetesResourceSpec("v1", "Pod"),
        )
        request = contract_request(INCIDENT_ID, "checkoutservice")

        ProviderAdapterContract.verify(self, provider, request)

        batch = provider.collect(request)
        state, event = batch.items
        self.assertEqual(state.facts["waiting_reason"], "ImagePullBackOff")
        self.assertNotIn("must-not-be-copied", str(state.facts))
        self.assertEqual(event.facts["message_code"], "ImagePullBackOff")

    def test_missing_required_configmap_is_explicit_state(self) -> None:
        provider = KubernetesStateProvider(
            StaticKubernetesClient(resource=None),
            KubernetesResourceSpec("v1", "ConfigMap", required=True),
            include_events=False,
        )

        batch = provider.collect(
            contract_request(INCIDENT_ID, "checkout-settings")
        )

        self.assertEqual(batch.items[0].subject["exists"], False)
        self.assertEqual(batch.items[0].facts["result_status"], "NOT_FOUND")
        self.assertEqual(batch.items[0].facts["required"], True)

    def test_configmap_values_are_never_copied_to_evidence(self) -> None:
        resource = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "checkout-settings",
                "namespace": "online-boutique",
                "uid": "8df6d266-40df-4fd6-942d-7ebc864c4061",
            },
            "spec": {},
            "status": {},
            "data": {"password": "must-not-be-copied", "mode": "prod"},
            "binaryData": {"certificate": "must-not-be-copied"},
        }
        provider = KubernetesStateProvider(
            StaticKubernetesClient(resource=resource),
            KubernetesResourceSpec("v1", "ConfigMap"),
            include_events=False,
        )

        facts = provider.collect(
            contract_request(INCIDENT_ID, "checkout-settings")
        ).items[0].facts

        self.assertEqual(facts["data_key_count"], 2)
        self.assertEqual(facts["binary_data_key_count"], 1)
        self.assertNotIn("must-not-be-copied", str(facts))

    def test_event_limit_is_partial_instead_of_silent_truncation(self) -> None:
        client = StaticKubernetesClient(
            pod_resource(),
            [KubernetesEventPage((warning_event(),), continue_token="next-page")],
        )
        provider = KubernetesStateProvider(
            client,
            KubernetesResourceSpec("v1", "Pod"),
            event_page_size=1,
            max_events=1,
        )

        batch = provider.collect(contract_request(INCIDENT_ID, "checkoutservice"))

        self.assertEqual(batch.status, "PARTIAL")
        self.assertIn("result exceeded", batch.error)
        self.assertEqual(len(batch.items), 2)

    def test_expired_event_page_restarts_once(self) -> None:
        first_event = warning_event()
        second_event = warning_event()
        second_event["metadata"] = dict(
            second_event["metadata"], name="checkout-image-pull-restarted"
        )

        class ExpiringClient(StaticKubernetesClient):
            def __init__(self):
                super().__init__(pod_resource())
                self.step = 0

            def list_event_page(self, **kwargs):
                self.event_calls.append(kwargs)
                self.step += 1
                if self.step == 1:
                    return KubernetesEventPage((first_event,), "expired-token")
                if self.step == 2:
                    raise ProviderPageExpired("snapshot expired")
                return KubernetesEventPage((second_event,))

        client = ExpiringClient()
        provider = KubernetesStateProvider(
            client,
            KubernetesResourceSpec("v1", "Pod"),
            event_page_size=1,
            max_events=2,
        )

        batch = provider.collect(contract_request(INCIDENT_ID, "checkoutservice"))

        event_names = [
            item.locator for item in batch.items if item.kind == "kubernetes-event"
        ]
        self.assertEqual(
            event_names,
            ["k8s://online-boutique/Event/checkout-image-pull-restarted"],
        )
        self.assertIsNone(client.event_calls[2]["continue_token"])

    def test_secret_resource_is_not_supported(self) -> None:
        with self.assertRaisesRegex(ValueError, "sensitive"):
            KubernetesResourceSpec("v1", "Secret")

    def test_field_selector_injection_name_is_rejected(self) -> None:
        provider = KubernetesStateProvider(
            StaticKubernetesClient(resource=pod_resource()),
            KubernetesResourceSpec("v1", "Pod"),
        )
        request = contract_request(INCIDENT_ID, "pod,metadata.namespace=default")

        with self.assertRaisesRegex(PermanentProviderError, "DNS subdomain"):
            provider.collect(request)

    def test_resource_outside_scope_is_rejected(self) -> None:
        resource = pod_resource()
        resource["metadata"]["name"] = "paymentservice"
        provider = KubernetesStateProvider(
            StaticKubernetesClient(resource=resource),
            KubernetesResourceSpec("v1", "Pod"),
            include_events=False,
        )

        with self.assertRaisesRegex(PermanentProviderError, "outside request scope"):
            provider.collect(contract_request(INCIDENT_ID, "checkoutservice"))


class RecordingTransport:
    def __init__(self, payloads) -> None:
        self.payloads = list(payloads)
        self.calls = []

    def get_json(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.payloads.pop(0)


class KubernetesHTTPAPITests(unittest.TestCase):
    def test_client_uses_get_only_scoped_resource_and_event_paths(self) -> None:
        transport = RecordingTransport(
            [pod_resource(), {"metadata": {"continue": ""}, "items": []}]
        )
        client = KubernetesHTTPAPI(
            "https://kubernetes.default.svc",
            bearer_token="service-account-test-token",
            transport=transport,
        )

        resource = client.get_resource(
            KubernetesResourceSpec("v1", "Pod"),
            namespace="online-boutique",
            name="checkoutservice",
            timeout_seconds=2,
        )
        page = client.list_event_page(
            namespace="online-boutique",
            involved_object_name="checkoutservice",
            limit=50,
            continue_token=None,
            timeout_seconds=2,
        )

        self.assertEqual(resource["kind"], "Pod")
        self.assertEqual(page.items, tuple())
        resource_url, resource_options = transport.calls[0]
        self.assertEqual(
            urlsplit(resource_url).path,
            "/api/v1/namespaces/online-boutique/pods/checkoutservice",
        )
        event_url, _ = transport.calls[1]
        parameters = parse_qs(urlsplit(event_url).query)
        self.assertEqual(
            parameters["fieldSelector"], ["involvedObject.name=checkoutservice"]
        )
        self.assertEqual(resource_options["headers"]["Authorization"],
                         "Bearer service-account-test-token")

    def test_api_server_requires_https_and_no_embedded_credentials(self) -> None:
        transport = RecordingTransport([])
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            KubernetesHTTPAPI(
                "http://kubernetes.default.svc",
                bearer_token="token",
                transport=transport,
            )
        with self.assertRaisesRegex(ValueError, "credentials"):
            KubernetesHTTPAPI(
                "https://user:password@kubernetes.default.svc",
                bearer_token="token",
                transport=transport,
            )


if __name__ == "__main__":
    unittest.main()
