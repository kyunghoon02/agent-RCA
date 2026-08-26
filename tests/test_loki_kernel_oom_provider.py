from __future__ import annotations

import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceWindow,
    ResourceScope,
    validate_provider_batch,
)
from incident_platform.errors import PermanentProviderError
from incident_platform.projectors import LokiKernelOOMEvidenceProjector
from incident_platform.providers.kubernetes import KubernetesResourcePage
from incident_platform.providers.loki import (
    LokiHTTPAPI,
    LokiKernelOOMProvider,
    LokiLogEntry,
    LokiLogStream,
    LokiRangeResult,
)


UTC = timezone.utc
INCIDENT_ID = "inc-loki-kernel-oom-0001"
CLUSTER_ID = "agent-rca-dev"
POD_NAME = "checkoutservice-7d9f8-q1w2e"
POD_UID = "7df6d266-40df-4fd6-942d-7ebc864c4061"
PAYMENT_UID = "03a09977-3d7c-4df3-b89c-c760ef8509f0"
MATCH_TIME = datetime(2026, 8, 12, 1, 4, 55, tzinfo=UTC)
MATCH_NS = int(MATCH_TIME.timestamp() * 1_000_000_000)


def kernel_line(uid: str = POD_UID) -> str:
    cgroup_uid = uid.replace("-", "_")
    return (
        "oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),"
        "cpuset=cri-containerd.scope,mems_allowed=0,global_oom,"
        "task_memcg=/kubepods.slice/kubepods-burstable.slice/"
        f"kubepods-burstable-pod{cgroup_uid}.slice/cri-containerd.scope,"
        "task=checkoutservice,pid=1234,uid=65532,"
        "oom_memcg=/kubepods.slice/kubepods-burstable.slice/"
        f"kubepods-burstable-pod{cgroup_uid}.slice"
    )


def request() -> CollectionRequest:
    return CollectionRequest(
        request_id="req-loki-kernel-oom-0001",
        incident_id=INCIDENT_ID,
        window=EvidenceWindow(
            start="2026-08-12T00:30:00Z",
            end="2026-08-12T01:05:00Z",
        ),
        scope=ResourceScope(
            namespace="online-boutique",
            resource_names=("checkoutservice",),
            resource_name_prefixes=("checkoutservice-",),
            max_items=10,
        ),
        timeout_seconds=5,
    )


class StaticKubernetesPods:
    def __init__(self) -> None:
        self.calls = []

    def list_resource_page(self, resource, **kwargs):
        self.calls.append((resource, kwargs))
        return KubernetesResourcePage(
            (
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {
                        "namespace": "online-boutique",
                        "name": POD_NAME,
                        "uid": POD_UID,
                    },
                },
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {
                        "namespace": "online-boutique",
                        "name": "paymentservice-6f6dd-abcde",
                        "uid": PAYMENT_UID,
                    },
                },
            )
        )


class StaticLoki:
    def __init__(self, result: LokiRangeResult) -> None:
        self.result = result
        self.calls = []

    def query_range(self, expression: str, **kwargs):
        self.calls.append((expression, kwargs))
        return self.result


class LokiKernelOOMProviderTests(unittest.TestCase):
    def test_exact_kernel_cgroup_signal_becomes_uid_backed_evidence(self) -> None:
        label_uid = POD_UID.replace("-", "_")
        loki = StaticLoki(
            LokiRangeResult(
                (
                    LokiLogStream(
                        {
                            "cluster_id": CLUSTER_ID,
                            "job": "kernel-journal",
                            "pod_uid": label_uid,
                        },
                        (LokiLogEntry(MATCH_NS, kernel_line()),),
                    ),
                )
            )
        )
        provider = LokiKernelOOMProvider(
            loki,
            StaticKubernetesPods(),
            cluster_id=CLUSTER_ID,
        )

        batch = provider.collect(request())
        validate_provider_batch(batch, request())
        self.assertEqual(batch.status, "SUCCEEDED")
        self.assertEqual(len(batch.items), 1)
        draft = batch.items[0]
        self.assertEqual(draft.subject["name"], POD_NAME)
        self.assertEqual(draft.subject["uid"], POD_UID)
        self.assertEqual(draft.facts["pattern_id"], "kernel-cgroup-oom")
        self.assertNotIn("pid=", draft.summary)
        self.assertNotIn("task_memcg", draft.facts)

        expression, kwargs = loki.calls[0]
        self.assertIn(POD_UID.replace("-", "_"), expression)
        self.assertNotIn(PAYMENT_UID.replace("-", "_"), expression)
        self.assertEqual(kwargs["limit"], 51)

        evidence = EvidenceBuilder().build(
            draft,
            request(),
            collected_at=datetime(2026, 8, 12, 1, 5, tzinfo=UTC),
        )
        projection = LokiKernelOOMEvidenceProjector().project(evidence)
        self.assertEqual(len(projection.records), 2)
        entity, event = projection.records
        self.assertEqual(entity["identity"]["keys"]["uid"], POD_UID)
        self.assertEqual(event["event_type"], "KERNEL_CGROUP_OOM")
        self.assertEqual(event["count"], 1)

    def test_stream_label_must_match_uid_parsed_from_kernel_line(self) -> None:
        loki = StaticLoki(
            LokiRangeResult(
                (
                    LokiLogStream(
                        {
                            "cluster_id": CLUSTER_ID,
                            "job": "kernel-journal",
                            "pod_uid": POD_UID.replace("-", "_"),
                        },
                        (LokiLogEntry(MATCH_NS, kernel_line(PAYMENT_UID)),),
                    ),
                )
            )
        )
        provider = LokiKernelOOMProvider(
            loki,
            StaticKubernetesPods(),
            cluster_id=CLUSTER_ID,
        )

        with self.assertRaisesRegex(PermanentProviderError, "disagrees"):
            provider.collect(request())

    def test_stream_must_come_from_the_trusted_cluster_and_job(self) -> None:
        loki = StaticLoki(
            LokiRangeResult(
                (
                    LokiLogStream(
                        {
                            "cluster_id": "different-cluster",
                            "job": "kernel-journal",
                            "pod_uid": POD_UID.replace("-", "_"),
                        },
                        (LokiLogEntry(MATCH_NS, kernel_line()),),
                    ),
                )
            )
        )
        provider = LokiKernelOOMProvider(
            loki,
            StaticKubernetesPods(),
            cluster_id=CLUSTER_ID,
        )

        with self.assertRaisesRegex(PermanentProviderError, "trusted source"):
            provider.collect(request())

    def test_exit_137_text_without_memcg_oom_signature_is_rejected(self) -> None:
        loki = StaticLoki(
            LokiRangeResult(
                (
                    LokiLogStream(
                        {
                            "cluster_id": CLUSTER_ID,
                            "job": "kernel-journal",
                            "pod_uid": POD_UID.replace("-", "_"),
                        },
                        (
                            LokiLogEntry(
                                MATCH_NS,
                                "container exited with status 137 after SIGKILL",
                            ),
                        ),
                    ),
                )
            )
        )
        provider = LokiKernelOOMProvider(
            loki,
            StaticKubernetesPods(),
            cluster_id=CLUSTER_ID,
        )

        with self.assertRaisesRegex(PermanentProviderError, "non-matching"):
            provider.collect(request())


class RecordingTransport:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = []

    def get_json(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.payload


class LokiHTTPAPITests(unittest.TestCase):
    def test_range_query_is_forward_bounded_and_uses_nanoseconds(self) -> None:
        transport = RecordingTransport(
            {
                "status": "success",
                "data": {"resultType": "streams", "result": []},
            }
        )
        client = LokiHTTPAPI("http://loki-gateway.observability", transport=transport)

        result = client.query_range(
            '{job="kernel-journal",pod_uid=~"^uid$"}',
            start="2026-08-12T00:30:00Z",
            end="2026-08-12T01:05:00Z",
            limit=51,
            timeout_seconds=2.5,
        )

        self.assertEqual(result.streams, tuple())
        url, kwargs = transport.calls[0]
        parsed = urlsplit(url)
        parameters = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/loki/api/v1/query_range")
        self.assertEqual(parameters["direction"], ["forward"])
        self.assertEqual(parameters["limit"], ["51"])
        self.assertTrue(parameters["start"][0].isdigit())
        self.assertTrue(parameters["end"][0].isdigit())
        self.assertEqual(kwargs["timeout_seconds"], 2.5)

    def test_base_url_rejects_embedded_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "credentials"):
            LokiHTTPAPI("https://user:password@loki.example")


if __name__ == "__main__":
    unittest.main()
