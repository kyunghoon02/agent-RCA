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
CLUSTER_ID = "gcp-dev-01"


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


def real_kubelet_image_pull_event() -> dict:
    event = warning_event()
    event["reason"] = "BackOff"
    event["message"] = (
        'Back-off pulling image "registry.invalid/paymentservice:missing"'
    )
    return event


def real_kubelet_failed_pull_event() -> dict:
    event = warning_event()
    event["reason"] = "Failed"
    event["message"] = (
        'Failed to pull image "registry.invalid/paymentservice:missing": '
        "failed to pull and unpack image: failed to resolve image"
    )
    return event


class KubernetesStateProviderTests(unittest.TestCase):
    def test_cluster_identity_is_required_from_trusted_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "cluster_id"):
            KubernetesStateProvider(
                StaticKubernetesClient(resource=pod_resource()),
                KubernetesResourceSpec("v1", "Pod"),
                cluster_id="",
            )

    def test_pod_state_and_event_are_safe_and_contract_valid(self) -> None:
        client = StaticKubernetesClient(
            pod_resource(),
            [KubernetesEventPage((warning_event(),))],
        )
        provider = KubernetesStateProvider(
            client,
            KubernetesResourceSpec("v1", "Pod"),
            cluster_id=CLUSTER_ID,
        )
        request = contract_request(INCIDENT_ID, "checkoutservice")

        ProviderAdapterContract.verify(self, provider, request)

        batch = provider.collect(request)
        state, event = batch.items
        self.assertEqual(state.subject["cluster_id"], CLUSTER_ID)
        self.assertEqual(event.subject["cluster_id"], CLUSTER_ID)
        self.assertEqual(state.facts["waiting_reason"], "ImagePullBackOff")
        self.assertNotIn("must-not-be-copied", str(state.facts))
        self.assertEqual(event.facts["message_code"], "ImagePullBackOff")
        self.assertEqual(client.event_calls[0]["involved_object_kind"], "Pod")
        self.assertEqual(
            client.event_calls[0]["involved_object_uid"],
            "7df6d266-40df-4fd6-942d-7ebc864c4061",
        )

    def test_real_kubelet_backoff_event_gets_a_stable_image_pull_code(self) -> None:
        provider = KubernetesStateProvider(
            StaticKubernetesClient(
                pod_resource(),
                [KubernetesEventPage((real_kubelet_image_pull_event(),))],
            ),
            KubernetesResourceSpec("v1", "Pod"),
            cluster_id=CLUSTER_ID,
        )

        state, event = provider.collect(
            contract_request(INCIDENT_ID, "checkoutservice")
        ).items

        self.assertEqual(state.facts["waiting_reason"], "ImagePullBackOff")
        self.assertEqual(event.facts["message_code"], "BackOff")
        self.assertEqual(event.facts["image_pull_code"], "ImagePullBackOff")

    def test_real_kubelet_failed_pull_event_gets_err_image_pull_code(self) -> None:
        provider = KubernetesStateProvider(
            StaticKubernetesClient(
                pod_resource(),
                [KubernetesEventPage((real_kubelet_failed_pull_event(),))],
            ),
            KubernetesResourceSpec("v1", "Pod"),
            cluster_id=CLUSTER_ID,
        )

        _, event = provider.collect(
            contract_request(INCIDENT_ID, "checkoutservice")
        ).items

        self.assertEqual(event.facts["message_code"], "Failed")
        self.assertEqual(event.facts["image_pull_code"], "ErrImagePull")

    def test_missing_required_configmap_is_explicit_state(self) -> None:
        provider = KubernetesStateProvider(
            StaticKubernetesClient(resource=None),
            KubernetesResourceSpec("v1", "ConfigMap", required=True),
            cluster_id=CLUSTER_ID,
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
            cluster_id=CLUSTER_ID,
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
            cluster_id=CLUSTER_ID,
            event_page_size=1,
            max_events=1,
            max_raw_events=1,
        )

        batch = provider.collect(contract_request(INCIDENT_ID, "checkoutservice"))

        self.assertEqual(batch.status, "PARTIAL")
        self.assertIn("exceeded", batch.error)
        self.assertEqual(len(batch.items), 2)

    def test_old_events_do_not_consume_the_in_window_output_limit(self) -> None:
        old_event = warning_event()
        old_event["metadata"] = dict(
            old_event["metadata"], name="checkout-old-warning"
        )
        old_event["lastTimestamp"] = "2026-08-12T00:01:00Z"
        old_event["metadata"]["creationTimestamp"] = "2026-08-12T00:01:00Z"
        client = StaticKubernetesClient(
            pod_resource(),
            [
                KubernetesEventPage((old_event,), continue_token="next-page"),
                KubernetesEventPage((warning_event(),)),
            ],
        )
        provider = KubernetesStateProvider(
            client,
            KubernetesResourceSpec("v1", "Pod"),
            cluster_id=CLUSTER_ID,
            event_page_size=1,
            max_events=1,
            max_raw_events=2,
        )

        batch = provider.collect(contract_request(INCIDENT_ID, "checkoutservice"))

        self.assertEqual(batch.status, "SUCCEEDED")
        self.assertEqual(len(batch.items), 2)
        self.assertEqual(
            batch.items[1].locator,
            "k8s://online-boutique/Event/checkout-image-pull",
        )

    def test_repeated_event_series_are_aggregated_before_output_limit(self) -> None:
        repeated = warning_event()
        repeated["metadata"] = dict(
            repeated["metadata"], name="checkout-image-pull-second"
        )
        repeated["count"] = 2
        provider = KubernetesStateProvider(
            StaticKubernetesClient(
                pod_resource(),
                [KubernetesEventPage((warning_event(), repeated))],
            ),
            KubernetesResourceSpec("v1", "Pod"),
            cluster_id=CLUSTER_ID,
            event_page_size=2,
            max_events=1,
            max_raw_events=2,
        )

        batch = provider.collect(contract_request(INCIDENT_ID, "checkoutservice"))

        self.assertEqual(batch.status, "SUCCEEDED")
        event = batch.items[1]
        self.assertEqual(event.facts["count"], 5)
        self.assertEqual(event.facts["event_series_count"], 2)

    def test_event_with_different_resource_uid_is_not_admitted(self) -> None:
        event = warning_event()
        event["involvedObject"] = dict(
            event["involvedObject"], uid="different-pod-uid"
        )
        provider = KubernetesStateProvider(
            StaticKubernetesClient(
                pod_resource(), [KubernetesEventPage((event,))]
            ),
            KubernetesResourceSpec("v1", "Pod"),
            cluster_id=CLUSTER_ID,
        )

        batch = provider.collect(contract_request(INCIDENT_ID, "checkoutservice"))

        self.assertEqual(batch.status, "PARTIAL")
        self.assertIn("outside request scope", batch.error)
        self.assertEqual([item.kind for item in batch.items], ["resource-state"])

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
            cluster_id=CLUSTER_ID,
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
            cluster_id=CLUSTER_ID,
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
            cluster_id=CLUSTER_ID,
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
            involved_object_kind="Pod",
            involved_object_uid="7df6d266-40df-4fd6-942d-7ebc864c4061",
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
            parameters["fieldSelector"],
            [
                "involvedObject.name=checkoutservice,"
                "involvedObject.kind=Pod,"
                "involvedObject.uid=7df6d266-40df-4fd6-942d-7ebc864c4061"
            ],
        )
        self.assertEqual(resource_options["headers"]["Authorization"],
                         "Bearer service-account-test-token")

    def test_client_lists_namespaced_and_cluster_scoped_inventory(self) -> None:
        transport = RecordingTransport(
            [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSetList",
                    "metadata": {"continue": ""},
                    "items": [],
                },
                {
                    "apiVersion": "v1",
                    "kind": "NodeList",
                    "metadata": {"continue": ""},
                    "items": [],
                },
            ]
        )
        client = KubernetesHTTPAPI(
            "https://kubernetes.default.svc",
            bearer_token="service-account-test-token",
            transport=transport,
        )

        client.list_resource_page(
            KubernetesResourceSpec("apps/v1", "ReplicaSet"),
            namespace="online-boutique",
            limit=50,
            continue_token=None,
            timeout_seconds=2,
        )
        client.list_resource_page(
            KubernetesResourceSpec("v1", "Node"),
            namespace=None,
            limit=10,
            continue_token=None,
            timeout_seconds=2,
        )

        namespaced_url, _ = transport.calls[0]
        cluster_url, _ = transport.calls[1]
        self.assertEqual(
            urlsplit(namespaced_url).path,
            "/apis/apps/v1/namespaces/online-boutique/replicasets",
        )
        self.assertEqual(urlsplit(cluster_url).path, "/api/v1/nodes")
        self.assertEqual(parse_qs(urlsplit(namespaced_url).query)["limit"], ["50"])

    def test_inventory_accepts_items_without_repeated_type_metadata(self) -> None:
        service = pod_resource()
        service["metadata"]["name"] = "checkoutservice"
        service["metadata"]["namespace"] = "online-boutique"
        service.pop("apiVersion")
        service.pop("kind")
        transport = RecordingTransport(
            [
                {
                    "apiVersion": "v1",
                    "kind": "ServiceList",
                    "metadata": {"continue": ""},
                    "items": [service],
                }
            ]
        )
        client = KubernetesHTTPAPI(
            "https://kubernetes.default.svc",
            bearer_token="service-account-test-token",
            transport=transport,
        )

        page = client.list_resource_page(
            KubernetesResourceSpec("v1", "Service"),
            namespace="online-boutique",
            limit=50,
            continue_token=None,
            timeout_seconds=2,
        )

        self.assertEqual(page.items[0]["metadata"]["name"], "checkoutservice")
        self.assertEqual(page.items[0]["apiVersion"], "v1")
        self.assertEqual(page.items[0]["kind"], "Service")

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
