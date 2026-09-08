from __future__ import annotations

import copy
import json
import subprocess
import sys
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceWindow,
    ResourceScope,
    validate_provider_batch,
)
from incident_platform.errors import (
    ContractViolation,
    PermanentProviderError,
    RetryableProviderError,
)
from incident_platform.hubble_contract import FEATURE_SET, LEGACY_FEATURE_SET
from incident_platform.projectors import HubbleNetworkFlowEvidenceProjector
from incident_platform.providers.hubble import (
    HubbleCLIClient,
    HubbleFlowResult,
    HubbleNetworkFlowProvider,
    _run_bounded,
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
    def batch_for(self, flows, *, gaps=()):
        return HubbleNetworkFlowProvider(
            StaticHubbleClient(
                {
                    "from": HubbleFlowResult(tuple(flows), observation_gaps=gaps),
                    "to": HubbleFlowResult(()),
                }
            ),
            cluster_id=CLUSTER_ID,
        ).collect(request())

    def project(self, draft):
        evidence = EvidenceBuilder().build(
            draft,
            request(),
            collected_at=datetime(2026, 9, 2, 5, 35, tzinfo=UTC),
        )
        return HubbleNetworkFlowEvidenceProjector().project(evidence)

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
        self.assertEqual(draft.facts["verdict_counts"], {"DROPPED": 1, "FORWARDED": 1})
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

    def test_query_side_namespace_and_time_window_are_revalidated(self):
        for direction in ("from", "to"):
            for violation in ("namespace", "time", "malformed time"):
                with self.subTest(direction=direction, violation=violation):
                    item = flow("outside", destination="checkoutservice")
                    endpoint_name = "source" if direction == "from" else "destination"
                    if violation == "namespace":
                        item[endpoint_name]["namespace"] = "out-of-scope"
                    else:
                        item["time"] = (
                            "2026-09-02T05:24:59Z"
                            if violation == "time"
                            else "invalid-time"
                        )
                    results = {"from": HubbleFlowResult(()), "to": HubbleFlowResult(())}
                    results[direction] = HubbleFlowResult((item,))
                    with self.assertRaises(PermanentProviderError):
                        HubbleNetworkFlowProvider(
                            StaticHubbleClient(results), cluster_id=CLUSTER_ID
                        ).collect(request())

    def test_fractional_flow_time_is_not_rounded_outside_incident_window(self):
        item = flow("fractional")
        item["time"] = "2026-09-02T05:25:00.500000Z"
        scoped = replace(
            request(),
            window=EvidenceWindow(start=item["time"], end=request().window.end),
        )
        batch = HubbleNetworkFlowProvider(
            StaticHubbleClient(
                {"from": HubbleFlowResult((item,)), "to": HubbleFlowResult(())}
            ),
            cluster_id=CLUSTER_ID,
        ).collect(scoped)
        draft = batch.items[0]
        self.assertEqual(draft.facts["first_flow_at"], item["time"])
        evidence = EvidenceBuilder().build(
            draft, scoped, collected_at=datetime(2026, 9, 2, 5, 35, tzinfo=UTC)
        )
        HubbleNetworkFlowEvidenceProjector().project(evidence)

    def test_drop_signals_are_observations_not_cause_ids_or_health_claims(self):
        for verdict, reason, signal, policy, other, unknown in (
            ("DROPPED", "POLICY_DENIED", "POLICY_DENIAL_OBSERVED", 1, 0, 0),
            ("DROPPED", "POLICY_DENY", "POLICY_DENIAL_OBSERVED", 1, 0, 0),
            ("DROPPED", "DROP_REASON_UNKNOWN", "DROPS_OBSERVED", 0, 0, 1),
            ("DROPPED", "CT_MAP_INSERTION_FAILED", "DROPS_OBSERVED", 0, 1, 0),
            ("DROPPED", "untrusted reason token=secret", "DROPS_OBSERVED", 0, 0, 1),
            ("AUDIT", "POLICY_DENIED", "NO_DROPS_OBSERVED", 0, 0, 0),
            ("AUDIT", "POLICY_DENY", "NO_DROPS_OBSERVED", 0, 0, 0),
            ("FORWARDED", None, "NO_DROPS_OBSERVED", 0, 0, 0),
        ):
            with self.subTest(verdict=verdict, reason=reason):
                draft = self.batch_for(
                    [flow("signal", verdict=verdict, drop_reason=reason)]
                ).items[0]
                self.assertEqual(draft.facts["feature_set"], FEATURE_SET)
                self.assertEqual(draft.facts["flow_signal"], signal)
                self.assertEqual(draft.facts["policy_denied_count"], policy)
                self.assertEqual(draft.facts["other_drop_count"], other)
                self.assertEqual(draft.facts["unknown_drop_count"], unknown)
                self.assertEqual(draft.facts["retention_status"], "UNKNOWN")
                self.assertEqual(draft.completeness, 0.5)
                self.project(draft)

    def test_cross_namespace_names_are_not_counted_as_local_roots(self):
        item = flow("cross-namespace", destination="checkoutservice")
        item["destination"]["namespace"] = "another-namespace"
        draft = self.batch_for([item]).items[0]
        self.assertEqual(draft.facts["source_root_flow_count"], 1)
        self.assertEqual(draft.facts["destination_root_flow_count"], 0)

    def test_conflicting_duplicate_uuid_is_not_silently_overwritten(self):
        first = flow("conflict", destination="checkoutservice")
        second = copy.deepcopy(first)
        second["verdict"] = "DROPPED"
        client = StaticHubbleClient(
            {"from": HubbleFlowResult((first,)), "to": HubbleFlowResult((second,))}
        )
        with self.assertRaisesRegex(PermanentProviderError, "conflicting"):
            HubbleNetworkFlowProvider(client, cluster_id=CLUSTER_ID).collect(request())

    def test_observation_loss_is_partial_even_when_flows_are_present(self):
        batch = self.batch_for(
            [flow("present")], gaps=("FLOW_EVENTS_LOST", "RELAY_NODE_UNAVAILABLE")
        )
        self.assertEqual(batch.status, "PARTIAL")
        draft = batch.items[0]
        self.assertEqual(draft.facts["flow_count"], 1)
        self.assertEqual(draft.facts["verdict_counts"], {"FORWARDED": 1})
        self.assertIn("FLOW_EVENTS_LOST", draft.facts["observation_gaps"])
        self.assertEqual(draft.confidence, 0.5)
        self.project(draft)

    def test_one_failed_direction_keeps_other_direction_but_all_failed_is_retryable(
        self,
    ):
        class FailingClient:
            all_failed = False

            def observe(self, **kwargs):
                if self.all_failed or kwargs["direction"] == "to":
                    raise RetryableProviderError("token=do-not-copy")
                return HubbleFlowResult((flow("kept"),))

        client = FailingClient()
        batch = HubbleNetworkFlowProvider(client, cluster_id=CLUSTER_ID).collect(
            request()
        )
        self.assertEqual(batch.status, "PARTIAL")
        self.assertEqual(batch.items[0].facts["flow_count"], 1)
        self.assertIn("RELAY_QUERY_UNAVAILABLE", batch.error)
        self.assertNotIn("do-not-copy", batch.error)
        client.all_failed = True
        with self.assertRaisesRegex(RetryableProviderError, "All bounded"):
            HubbleNetworkFlowProvider(client, cluster_id=CLUSTER_ID).collect(request())

    def test_raw_flow_budget_is_shared_across_services_and_directions(self):
        client = StaticHubbleClient(
            {"from": HubbleFlowResult(()), "to": HubbleFlowResult(())}
        )
        scoped = replace(
            request(),
            scope=ResourceScope(
                namespace="online-boutique",
                resource_names=("frontend", "checkoutservice", "paymentservice"),
            ),
        )
        HubbleNetworkFlowProvider(
            client, cluster_id=CLUSTER_ID, max_raw_flows=500
        ).collect(scoped)
        self.assertEqual(len(client.calls), 6)
        self.assertLessEqual(sum(call["limit"] for call in client.calls), 500)
        self.assertEqual(
            {call["pod_prefix"] for call in client.calls},
            set(scoped.scope.resource_names),
        )

    def test_projector_rejects_tampered_signals_counts_quality_and_raw_fields(self):
        draft = self.batch_for(
            [flow("validate", verdict="DROPPED", drop_reason="POLICY_DENIED")]
        ).items[0]
        for fields in (
            {"flow_signal": "NO_DROPS_OBSERVED"},
            {"policy_denied_count": 0},
            {"drop_reason_counts": {"POLICY_DENIED": 2}},
            {
                "drop_reason_counts": {"DROP_REASON_UNKNOWN": 1},
                "policy_denied_count": 0,
                "other_drop_count": 1,
                "flow_signal": "DROPS_OBSERVED",
            },
            {"unknown_drop_count": 1},
            {"observation_gaps": ["secret-payload"]},
            {"raw_flow": {"IP": "secret"}},
        ):
            with self.subTest(fields=fields), self.assertRaises(ContractViolation):
                self.project(replace(draft, facts={**draft.facts, **fields}))
        with self.assertRaisesRegex(ContractViolation, "coverage"):
            self.project(replace(draft, completeness=1.0))

    def test_legacy_v1_evidence_remains_projectable_without_rewriting_history(self):
        draft = self.batch_for([flow("legacy")]).items[0]
        facts = {
            key: value
            for key, value in draft.facts.items()
            if key in HubbleNetworkFlowEvidenceProjector.fact_names
        }
        facts.update(feature_set=LEGACY_FEATURE_SET, retention_status="NOT_APPLICABLE")
        legacy = replace(draft, facts=facts, completeness=1.0)
        projection = self.project(legacy)
        self.assertEqual(
            projection.records[1]["attributes"]["feature_set"], LEGACY_FEATURE_SET
        )

    def test_hubble_policy_denial_does_not_prove_a_registered_application_cause(self):
        from incident_platform.deterministic import registered_rule_evaluations

        draft = self.batch_for(
            [flow("policy", verdict="DROPPED", drop_reason="POLICY_DENIED")]
        ).items[0]
        evidence = EvidenceBuilder().build(
            draft, request(), collected_at=datetime(2026, 9, 2, 5, 35, tzinfo=UTC)
        )
        self.assertTrue(
            all(
                rule.status != "PROVEN"
                for rule in registered_rule_evaluations((evidence,))
            )
        )


class HubbleCLIClientTests(unittest.TestCase):
    def observe_output(self, stdout=b"", stderr=b""):
        client = HubbleCLIClient("10.42.0.3:31234")
        with patch(
            "incident_platform.providers.hubble._run_bounded",
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=stdout, stderr=stderr
            ),
        ):
            return client.observe(
                namespace="online-boutique",
                pod_prefix="checkoutservice",
                direction="from",
                start=request().window.start,
                end=request().window.end,
                limit=10,
                timeout_seconds=1,
            )

    def test_cli_uses_argv_without_shell_and_parses_jsonpb(self) -> None:
        payload = json.dumps({"flow": flow("cli-flow")}).encode() + b"\n"
        completed = subprocess.CompletedProcess([], 0, stdout=payload, stderr=b"")
        client = HubbleCLIClient(
            "10.42.0.3:31234",
            binary="/usr/local/bin/hubble",
        )

        with patch("incident_platform.providers.hubble._run_bounded") as run:
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
        self.assertEqual(run.call_args.kwargs["timeout_seconds"], 4.5)
        self.assertIn("--field-mask", argv)
        mask = argv[argv.index("--field-mask") + 1]
        self.assertNotIn("IP", mask)
        self.assertNotIn("l7", mask)

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
            "incident_platform.providers.hubble._run_bounded",
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

    def test_jsonpb_control_messages_from_both_streams_are_sanitized(self):
        stdout = b"\n".join(
            json.dumps(item).encode()
            for item in (
                {"flow": flow("ok")},
                {
                    "lost_events": {
                        "source": "HUBBLE_RING_BUFFER",
                        "num_events_lost": "123",
                        "node_name": "private-node",
                    }
                },
            )
        )
        stderr = b"\n".join(
            json.dumps(item).encode()
            for item in (
                {
                    "node_status": {
                        "state_change": "NODE_CONNECTED",
                        "node_names": ["private-node"],
                    }
                },
                {
                    "node_status": {
                        "state_change": "NODE_UNAVAILABLE",
                        "message": "token=secret",
                    }
                },
            )
        )
        result = self.observe_output(stdout, stderr)
        self.assertEqual(len(result.flows), 1)
        self.assertEqual(
            result.observation_gaps, ("FLOW_EVENTS_LOST", "RELAY_NODE_UNAVAILABLE")
        )
        self.assertNotIn("secret", repr(result.observation_gaps))
        self.assertEqual(
            self.observe_output(
                stderr=json.dumps(
                    {"node_status": {"state_change": "NODE_CONNECTED"}}
                ).encode()
            ).observation_gaps,
            (),
        )

    def test_success_exit_with_unstructured_stderr_is_not_silently_complete(self):
        result = self.observe_output(
            json.dumps({"flow": flow("ok")}).encode(), b"warning token=secret"
        )
        self.assertEqual(result.observation_gaps, ("CLI_DIAGNOSTIC",))

    def test_cli_relay_version_warning_is_an_explicit_coverage_gap(self):
        result = self.observe_output(
            json.dumps({"flow": flow("ok")}).encode(),
            b'time=redacted level=WARN msg="Hubble CLI version is lower than Hubble Relay, API compatibility is not guaranteed, updating to a matching or higher version is recommended" hubble-cli-version=1.19.4 hubble-relay-version=1.20.1',
        )
        self.assertEqual(result.observation_gaps, ("CLI_RELAY_VERSION_MISMATCH",))
        draft = HubbleNetworkFlowProvider(
            StaticHubbleClient({"from": result, "to": HubbleFlowResult(())}),
            cluster_id=CLUSTER_ID,
        ).collect(request())
        self.assertEqual(draft.status, "PARTIAL")
        self.assertEqual(draft.items[0].facts["flow_count"], 1)

    def test_malformed_or_ambiguous_stdout_is_fail_closed(self):
        for value in (b"not-json", b"[]", b"{}", b'{"flow":{},"lost_events":{}}'):
            with self.subTest(value=value), self.assertRaises(PermanentProviderError):
                self.observe_output(value)

    def test_bounded_subprocess_reads_stdout_and_stderr(self):
        result = _run_bounded(
            [
                sys.executable,
                "-c",
                "import sys; print('out'); print('err',file=sys.stderr)",
            ],
            timeout_seconds=2,
            max_output_bytes=100,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"out\n")
        self.assertEqual(result.stderr, b"err\n")

    def test_bounded_subprocess_kills_oversized_or_stuck_children(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream), self.assertRaisesRegex(
                PermanentProviderError, "byte limit"
            ):
                _run_bounded(
                    [
                        sys.executable,
                        "-c",
                        f"import sys,time; sys.{stream}.write('x'*10000); sys.{stream}.flush(); time.sleep(30)",
                    ],
                    timeout_seconds=2,
                    max_output_bytes=100,
                )
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_bounded(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout_seconds=0.1,
                max_output_bytes=100,
            )
        self.assertLess(time.monotonic() - started, 2)


if __name__ == "__main__":
    unittest.main()
