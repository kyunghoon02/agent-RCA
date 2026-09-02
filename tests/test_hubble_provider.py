from __future__ import annotations

import json
import subprocess
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceWindow,
    ResourceScope,
    validate_provider_batch,
)
from incident_platform.errors import PermanentProviderError, RetryableProviderError
from incident_platform.projectors import HubbleNetworkFlowEvidenceProjector
from incident_platform.providers.hubble import (
    HubbleCLIClient,
    HubbleFlowResult,
    HubbleNetworkFlowProvider,
)


UTC = timezone.utc
CLUSTER_ID = "agent-rca-chaos-eval"
FLOW_TIME = "2026-09-02T05:30:11Z"


def request() -> CollectionRequest:
    return CollectionRequest(
        request_id="req-hubble-network-flow-0001",
        incident_id="inc-hubble-network-flow-0001",
        window=EvidenceWindow(
            start="2026-09-02T05:25:00Z",
            end="2026-09-02T05:35:00Z",
        ),
        scope=ResourceScope(
            namespace="online-boutique",
            resource_names=("checkoutservice",),
            resource_name_prefixes=("checkoutservice-",),
            max_items=8,
        ),
        timeout_seconds=5,
    )


def endpoint(name: str, *, namespace: str = "online-boutique") -> dict:
    return {
        "namespace": namespace,
        "pod_name": f"{name}-7d9f8-q1w2e",
        "workloads": [{"name": name, "kind": "Deployment"}],
        "labels": ["ignored=sensitive-and-unbounded"],
    }


def flow(
    uuid: str,
    *,
    verdict: str = "FORWARDED",
    source: str = "checkoutservice",
    destination: str = "paymentservice",
    drop_reason: str | None = None,
) -> dict:
    result = {
        "uuid": uuid,
        "time": FLOW_TIME,
        "verdict": verdict,
        "source": endpoint(source),
        "destination": endpoint(destination),
        "l4": {"TCP": {"source_port": 12345, "destination_port": 50051}},
        "IP": {"source": "10.244.0.10", "destination": "10.244.0.11"},
        "l7": {
            "http": {
                "url": "https://user:password@example.invalid/pay?token=secret",
                "headers": [{"key": "authorization", "value": "Bearer secret"}],
            }
        },
    }
    if drop_reason is not None:
        result["drop_reason_desc"] = drop_reason
    return result


class StaticHubbleClient:
    def __init__(self, results: dict[str, HubbleFlowResult]) -> None:
        self.results = results
        self.calls = []

    def observe(self, **kwargs):
        self.calls.append(kwargs)
        return self.results[kwargs["direction"]]


class HubbleNetworkFlowProviderTests(unittest.TestCase):
    def test_scoped_flows_are_deduplicated_aggregated_and_projected(self) -> None:
        forwarded = flow("flow-forwarded")
        dropped = flow(
            "flow-dropped",
            verdict="DROPPED",
            destination="checkoutservice",
            source="paymentservice",
            drop_reason="POLICY_DENIED",
        )
        client = StaticHubbleClient(
            {
                "from": HubbleFlowResult((forwarded,)),
                "to": HubbleFlowResult((dropped,)),
            }
        )
        provider = HubbleNetworkFlowProvider(client, cluster_id=CLUSTER_ID)

        batch = provider.collect(request())

        validate_provider_batch(batch, request())
        self.assertEqual(batch.status, "SUCCEEDED")
        self.assertEqual(len(batch.items), 1)
        draft = batch.items[0]
        self.assertEqual(draft.source, "hubble")
        self.assertEqual(draft.kind, "network-flow-summary")
        self.assertEqual(draft.subject["name"], "checkoutservice")
        self.assertEqual(draft.facts["flow_count"], 2)
        self.assertEqual(
            draft.facts["verdict_counts"], {"DROPPED": 1, "FORWARDED": 1}
        )
        self.assertEqual(draft.facts["drop_reason_counts"], {"POLICY_DENIED": 1})
        self.assertEqual(draft.facts["protocol_counts"], {"TCP": 2})
        serialized = json.dumps(draft.facts, sort_keys=True)
        self.assertNotIn("10.244", serialized)
        self.assertNotIn("authorization", serialized)
        self.assertNotIn("password", serialized)
        self.assertEqual({call["direction"] for call in client.calls}, {"from", "to"})
        self.assertTrue(
            all(call["pod_prefix"] == "checkoutservice" for call in client.calls)
        )

        evidence = EvidenceBuilder().build(
            draft,
            request(),
            collected_at=datetime(2026, 9, 2, 5, 35, tzinfo=UTC),
        )
        projection = HubbleNetworkFlowEvidenceProjector().project(evidence)
        self.assertEqual(len(projection.records), 2)
        entity, event = projection.records
        self.assertEqual(entity["identity"]["identity_type"], "logical-service")
        self.assertEqual(event["event_type"], "HUBBLE_NETWORK_FLOW_SUMMARY")
        self.assertEqual(event["count"], 2)

    def test_same_flow_returned_by_both_direction_queries_is_counted_once(self) -> None:
        self_flow = flow(
            "same-flow",
            source="checkoutservice",
            destination="checkoutservice",
        )
        client = StaticHubbleClient(
            {
                "from": HubbleFlowResult((self_flow,)),
                "to": HubbleFlowResult((self_flow,)),
            }
        )

        batch = HubbleNetworkFlowProvider(client, cluster_id=CLUSTER_ID).collect(
            request()
        )

        self.assertEqual(batch.items[0].facts["flow_count"], 1)
        self.assertEqual(batch.items[0].facts["source_root_flow_count"], 1)
        self.assertEqual(batch.items[0].facts["destination_root_flow_count"], 1)

    def test_no_data_is_explicit_partial_when_retention_is_not_provable(self) -> None:
        empty = HubbleFlowResult(tuple())
        client = StaticHubbleClient({"from": empty, "to": empty})

        batch = HubbleNetworkFlowProvider(client, cluster_id=CLUSTER_ID).collect(
            request()
        )

        self.assertEqual(batch.status, "PARTIAL")
        self.assertIn("retention coverage unknown", batch.error)
        draft = batch.items[0]
        self.assertEqual(draft.facts["result_status"], "NO_DATA")
        self.assertEqual(draft.facts["retention_status"], "UNKNOWN")
        self.assertEqual(draft.completeness, 0.0)
        evidence = EvidenceBuilder().build(
            draft,
            request(),
            collected_at=datetime(2026, 9, 2, 5, 35, tzinfo=UTC),
        )
        event = HubbleNetworkFlowEvidenceProjector().project(evidence).records[1]
        self.assertEqual(event["count"], 1)

    def test_flow_outside_exact_query_side_is_rejected(self) -> None:
        client = StaticHubbleClient(
            {
                "from": HubbleFlowResult((flow("wrong-root", source="frontend"),)),
                "to": HubbleFlowResult(tuple()),
            }
        )

        with self.assertRaisesRegex(PermanentProviderError, "Pod prefix"):
            HubbleNetworkFlowProvider(client, cluster_id=CLUSTER_ID).collect(request())

    def test_truncation_is_preserved_as_partial_quality(self) -> None:
        client = StaticHubbleClient(
            {
                "from": HubbleFlowResult((flow("bounded"),), truncated=True),
                "to": HubbleFlowResult(tuple()),
            }
        )

        batch = HubbleNetworkFlowProvider(client, cluster_id=CLUSTER_ID).collect(
            request()
        )

        self.assertEqual(batch.status, "PARTIAL")
        self.assertTrue(batch.items[0].facts["truncated"])
        self.assertEqual(batch.items[0].completeness, 0.5)


class HubbleCLIClientTests(unittest.TestCase):
    def test_cli_uses_argv_without_shell_and_parses_jsonpb(self) -> None:
        payload = json.dumps({"flow": flow("cli-flow")}).encode() + b"\n"
        completed = subprocess.CompletedProcess([], 0, stdout=payload, stderr=b"")
        client = HubbleCLIClient(
            "10.42.0.3:31234",
            binary="/usr/local/bin/hubble",
        )

        with patch("incident_platform.providers.hubble.subprocess.run") as run:
            run.return_value = completed
            result = client.observe(
                namespace="online-boutique",
                pod_prefix="checkoutservice",
                direction="from",
                start="2026-09-02T05:25:00Z",
                end="2026-09-02T05:35:00Z",
                limit=50,
                timeout_seconds=4.5,
            )

        self.assertEqual(len(result.flows), 1)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0:2], ["/usr/local/bin/hubble", "observe"])
        self.assertIn("online-boutique/checkoutservice", argv)
        self.assertNotIn("--namespace", argv)
        self.assertIn("51", argv)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["timeout"], 4.5)

    def test_public_relay_endpoint_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "private IPv4"):
            HubbleCLIClient("8.8.8.8:4245")

    def test_connection_failure_is_retryable_without_leaking_stderr(self) -> None:
        completed = subprocess.CompletedProcess(
            [],
            1,
            stdout=b"",
            stderr=b"connection refused token=do-not-leak",
        )
        client = HubbleCLIClient("10.42.0.3:31234")
        with patch(
            "incident_platform.providers.hubble.subprocess.run",
            return_value=completed,
        ):
            with self.assertRaisesRegex(
                RetryableProviderError, "Hubble Relay is unavailable"
            ) as raised:
                client.observe(
                    namespace="online-boutique",
                    pod_prefix="checkoutservice",
                    direction="to",
                    start="2026-09-02T05:25:00Z",
                    end="2026-09-02T05:35:00Z",
                    limit=10,
                    timeout_seconds=2,
                )
        self.assertNotIn("do-not-leak", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
