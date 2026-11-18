from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch
from dataclasses import replace
from datetime import datetime, timezone

from incident_platform.agent_rca import AgentModelRun, AgentRCAService, OpenAIAgentsSDKRunner
from incident_platform.contracts import validate_contract
from incident_platform.deterministic import DeterministicRCAEngine
from incident_platform.errors import ContractViolation, PermanentProviderError, EvidenceGateViolation
from incident_platform.evidence import CollectionRequest, EvidenceBuilder, EvidenceWindow, ResourceScope
from incident_platform.incidents import AlertmanagerNormalizer
from incident_platform.knowledge import BoundedKnowledgeRetriever
from incident_platform.localization import IncidentLocalizationService
from incident_platform.network_policy import NETWORK_POLICY_CAUSE, validate_network_path_evidence
from incident_platform.projectors.network_policy import NetworkPolicyPathEvidenceProjector
from incident_platform.providers.hubble import HubbleFlowResult
from incident_platform.providers.network_policy import (
    NetworkDependencyPath, NetworkEvidenceCollector, NetworkPolicyPathProvider, load_network_paths,
)
from incident_platform.providers.prometheus import PrometheusRangeResult
from incident_platform.reporting import FastPathReportBuilder
from incident_platform.repository import InMemoryIncidentRepository, context_evidence_ids
from incident_platform.stategraph import EntityIdentity, InMemoryStateGraphRepository, InvestigationScope
from incident_platform.viewer import IncidentViewerQueryService

NOW = datetime(2026, 10, 13, 1, 10, tzinfo=timezone.utc)
CLUSTER = "network-fixture-cluster"
PATH = NetworkDependencyPath("frontend-catalog-get", "frontend", "productcatalogservice",
                             3550, "TCP", "hipstershop.ProductCatalogService/GetProduct")


def flow(**updates):
    def endpoint(service):
        return {"namespace": "online-boutique", "pod_name": service + "-abc-123",
                "workloads": [{"kind": "Deployment", "name": service}]}
    result = {"uuid": "flow-policy-0001", "time": "2026-10-13T01:05:11Z",
              "source": endpoint("frontend"), "destination": endpoint("productcatalogservice"),
              "verdict": "DROPPED", "drop_reason_desc": "POLICY_DENIED",
              "l4": {"TCP": {"destination_port": 3550, "source_port": 12345}},
              "IP": {"source": "10.0.0.1"}, "l7": {"http": {"url": "ignored-private-url"}}}
    result.update(updates)
    return result


class Hubble:
    def __init__(self, flows=None, *, truncated=False, gaps=()):
        self.result = HubbleFlowResult(tuple([flow()] if flows is None else flows),
                                      truncated=truncated, observation_gaps=gaps)
        self.calls = []

    def observe(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class Prometheus:
    def __init__(self, *, value="1", at="2026-10-13T01:05:15Z", no_data=False, labels=None, warnings=()):
        labels = labels or {"namespace": "online-boutique", "service_name": "frontend",
                            "span_name": PATH.client_operation}
        series = () if no_data else ({"metric": labels,
                    "values": [[datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp(), value]]},)
        self.result = PrometheusRangeResult(series, warnings)
        self.calls = []

    def query_range(self, expression, **kwargs):
        self.calls.append((expression, kwargs))
        return self.result


def collect(hubble=None, prometheus=None, incident_id="inc-network-fixture-0001"):
    request = CollectionRequest("req-network-fixture-0001", incident_id,
        EvidenceWindow("2026-10-13T01:00:00Z", "2026-10-13T01:10:00Z"),
        ResourceScope("online-boutique", ("frontend",), max_items=8), 5)
    provider = NetworkPolicyPathProvider(hubble or Hubble(), prometheus or Prometheus(),
                                        (PATH,), cluster_id=CLUSTER)
    batch = provider.collect(request)
    evidence = [EvidenceBuilder().build(item, request, collected_at=NOW) for item in batch.items]
    return request, batch, evidence


class EmptyKnowledge:
    def list_documents(self, *, limit):
        return ()


class NetworkFakeRunner:
    model_name = "offline-network-fixture"

    def __init__(self, *, decision="PARTIAL", inspected=True, wrong_entity=False):
        self.decision, self.inspected, self.wrong_entity = decision, inspected, wrong_entity

    def run(self, invocation):
        ids = list(invocation.investigation_view.candidate_evidence_ids)
        if self.inspected:
            for key in ids:
                invocation.tool_runtime.inspect_evidence(key)
        caller = "productcatalogservice" if self.wrong_entity else "frontend"
        entity_id = EntityIdentity.logical_service(cluster_id=CLUSTER,
            namespace="online-boutique", service_name=caller).entity_id
        root = {"cause_id": NETWORK_POLICY_CAUSE,
                "summary": "Policy denial on the registered failed dependency; HTTP-request attribution is incomplete.",
                "entity_id": entity_id, "supporting_evidence_ids": ids,
                "contradicting_evidence_ids": [], "reference_document_ids": []}
        draft = {"schema_version": "1.2.0", "incident_id": invocation.context["incident_id"],
                 "context_id": invocation.context["context_id"], "decision": self.decision,
                 "root_cause": root, "hypotheses": [dict(root, rank=1, confidence=0.8,
                                                          status="supported", missing_evidence=[])],
                 "remediation": {"suggestions": ["Review relevant policies through source control."],
                                 "verification_conditions": ["The registered RPC succeeds."]},
                 "limitations": ["Unknown retention; aggregate RPC errors; enforcing policy UID is unknown."],
                 "read_only": True}
        return AgentModelRun(draft, 1, 100, 100, 200)


def prepared():
    incident = AlertmanagerNormalizer().normalize({"alerts": [{"status": "firing",
        "labels": {"alertname": "NeutralNetworkFixture", "namespace": "online-boutique", "service": "frontend", "severity": "warning"},
        "annotations": {}, "startsAt": "2026-10-13T01:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z", "fingerprint": "neutral-network-fixture"}]}, received_at=NOW)[0].incident
    repository = InMemoryIncidentRepository()
    repository.create_or_get_by_deduplication_key(incident, occurred_at=NOW)
    incident_id = incident["incident_id"]
    request, _, evidence = collect(incident_id=incident_id)
    repository.transition(incident_id, expected_status="RECEIVED", next_status="COLLECTING", occurred_at=NOW)
    repository.store_evidence(incident_id, evidence)
    repository.transition(incident_id, expected_status="COLLECTING", next_status="LOCALIZING", occurred_at=NOW)
    seed = EntityIdentity.logical_service(cluster_id=CLUSTER, namespace="online-boutique", service_name="frontend")
    graph = InMemoryStateGraphRepository()
    result = IncidentLocalizationService(repository, graph, (NetworkPolicyPathEvidenceProjector(),)).localize_incident(
        incident_id, scope=InvestigationScope(incident_id, (seed.entity_id,), request.window,
            domains=("web-service",), relation_types=("CALLS",), max_entities=4, max_depth=1), frozen_at=NOW)
    return repository, result, evidence


class NetworkPolicyRCATests(unittest.TestCase):
    def test_exact_dependency_becomes_two_private_safe_evidence_channels(self):
        hubble, metrics = Hubble(), Prometheus()
        _, _, evidence = collect(hubble, metrics)
        for item in evidence:
            validate_network_path_evidence(item)
        self.assertEqual(DeterministicRCAEngine().evaluate(evidence).root_cause_id, NETWORK_POLICY_CAUSE)
        serialized = json.dumps(evidence)
        self.assertNotIn("10.0.0.1", serialized)
        self.assertNotIn("ignored-private-url", serialized)
        self.assertNotIn("source_port", serialized)
        self.assertIn('span_kind="SPAN_KIND_CLIENT"', metrics.calls[0][0])
        self.assertEqual(hubble.calls[0]["direction"], "from")

    def test_wrong_destination_port_direction_or_reason_cannot_prove(self):
        wrong_destination = flow()
        wrong_destination["destination"]["workloads"][0]["name"] = "paymentservice"
        for candidate in (wrong_destination, flow(l4={"TCP": {"destination_port": 50051}}),
                          flow(verdict="FORWARDED"), flow(verdict="AUDIT"),
                          flow(drop_reason_desc="UNKNOWN"), flow(drop_reason_desc="CT_TRUNCATED_OR_INVALID_HEADER")):
            with self.subTest(flow=candidate):
                _, _, evidence = collect(Hubble([candidate]))
                self.assertNotEqual(DeterministicRCAEngine().evaluate(evidence).status, "PROVEN")

    def test_wrong_source_or_namespace_is_rejected(self):
        for endpoint in ({"pod_name": "frontend-other-123", "namespace": "other"},
                         {"pod_name": "paymentservice-123", "namespace": "online-boutique"}):
            with self.subTest(endpoint=endpoint), self.assertRaises(PermanentProviderError):
                collect(Hubble([flow(source=endpoint)]))

    def test_rpc_error_at_another_time_is_insufficient(self):
        _, _, evidence = collect(prometheus=Prometheus(at="2026-10-13T01:09:00Z"))
        self.assertEqual(DeterministicRCAEngine().evaluate_rule(NETWORK_POLICY_CAUSE, evidence).status, "INSUFFICIENT")

    def test_rpc_between_disjoint_denial_times_cannot_match_whole_window_bounds(self):
        first = flow(time="2026-10-13T01:01:00Z")
        last = flow(uuid="second-denial", time="2026-10-13T01:09:00Z")
        _, _, evidence = collect(Hubble([first, last]))
        self.assertEqual(DeterministicRCAEngine().evaluate_rule(NETWORK_POLICY_CAUSE, evidence).status, "INSUFFICIENT")

    def test_subsecond_denial_after_rpc_interval_is_not_rounded_into_it(self):
        _, _, evidence = collect(Hubble([flow(time="2026-10-13T01:05:11.500000Z")]),
                                 Prometheus(at="2026-10-13T01:05:11Z"))
        self.assertEqual(DeterministicRCAEngine().evaluate_rule(NETWORK_POLICY_CAUSE, evidence).status, "INSUFFICIENT")

    def test_stale_and_inconsistent_evidence_are_not_accepted(self):
        _, _, evidence = collect()
        changed = copy.deepcopy(evidence)
        changed[0]["quality"]["freshness"] = "stale"
        self.assertNotEqual(DeterministicRCAEngine().evaluate(changed).status, "PROVEN")
        changed = copy.deepcopy(evidence)
        changed[0]["facts"]["denied_at"] = []
        self.assertNotEqual(DeterministicRCAEngine().evaluate(changed).status, "PROVEN")
        changed = copy.deepcopy(evidence)
        changed[1]["facts"]["sample_count"] = 0
        self.assertNotEqual(DeterministicRCAEngine().evaluate(changed).status, "PROVEN")

    def test_zero_errors_or_missing_rpc_cannot_prove(self):
        for metrics in (Prometheus(value="0"), Prometheus(no_data=True)):
            _, _, evidence = collect(prometheus=metrics)
            self.assertNotEqual(DeterministicRCAEngine().evaluate(evidence).status, "PROVEN")

    def test_cross_incident_cluster_path_or_caller_cannot_prove(self):
        _, _, evidence = collect()
        for location, key, value in (("subject", "cluster_id", "other"), ("facts", "path_id", "other-path"),
                                     ("facts", "destination_port", 80), ("facts", "client_operation", "OtherRPC")):
            changed = copy.deepcopy(evidence)
            changed[1][location][key] = value
            self.assertNotEqual(DeterministicRCAEngine().evaluate(changed).status, "PROVEN")
        changed = copy.deepcopy(evidence)
        changed[1]["incident_id"] = "inc-other-fixture-0001"
        self.assertNotEqual(DeterministicRCAEngine().evaluate(changed).status, "PROVEN")

    def test_untrusted_provider_and_legacy_service_summary_are_not_proof(self):
        _, _, evidence = collect()
        evidence[0]["provenance"]["provider"] = "other-provider"
        self.assertNotEqual(DeterministicRCAEngine().evaluate(evidence).status, "PROVEN")
        evidence[0]["facts"]["feature_set"] = "hubble-network-flow-summary-v2"
        self.assertNotEqual(DeterministicRCAEngine().evaluate(evidence).status, "PROVEN")

    def test_truncated_or_lost_observation_preserves_positive_evidence_and_partial(self):
        for hubble in (Hubble(truncated=True), Hubble(gaps=("FLOW_EVENTS_LOST",))):
            _, batch, evidence = collect(hubble)
            self.assertEqual(batch.status, "PARTIAL")
            self.assertEqual(evidence[0]["quality"]["completeness"], 0.5)
            self.assertEqual(evidence[0]["facts"]["retention_status"], "UNKNOWN")

    def test_no_flows_does_not_assert_network_health(self):
        _, batch, evidence = collect(Hubble([]))
        self.assertEqual(batch.status, "PARTIAL")
        self.assertEqual(evidence[0]["quality"]["completeness"], 0)
        self.assertEqual(DeterministicRCAEngine().evaluate(evidence).status, "ABSTAIN")

    def test_rpc_labels_nonfinite_out_of_window_or_oversize_are_rejected(self):
        for metrics in (Prometheus(labels={"namespace": "other"}), Prometheus(value="NaN"),
                        Prometheus(at="2026-10-13T02:00:00Z")):
            with self.subTest(metrics=metrics), self.assertRaises(PermanentProviderError):
                collect(prometheus=metrics)
        metrics = Prometheus()
        metrics.result.series[0]["values"] *= 1001
        with self.assertRaises(PermanentProviderError):
            collect(prometheus=metrics)

    def test_duplicate_flow_conflict_is_rejected(self):
        with self.assertRaises(PermanentProviderError):
            collect(Hubble([flow(), flow(verdict="FORWARDED")]))

    def test_no_scope_expansion_and_item_budget_preflight(self):
        request, _, _ = collect()
        hubble = Hubble()
        provider = NetworkPolicyPathProvider(hubble, Prometheus(), (PATH,), cluster_id=CLUSTER)
        batch = provider.collect(replace(request, scope=ResourceScope("online-boutique", ("paymentservice",))))
        self.assertEqual(batch.items, ())
        self.assertEqual(hubble.calls, [])
        with self.assertRaises(PermanentProviderError):
            provider.collect(replace(request, scope=replace(request.scope, max_items=1)))

    def test_paths_loaded_from_trusted_config_and_ambiguous_operation_rejected(self):
        self.assertEqual(load_network_paths("config/network-dependency-paths.json"), (PATH,))
        with self.assertRaises(ValueError):
            NetworkPolicyPathProvider(Hubble(), Prometheus(), (PATH, replace(PATH, path_id="other-path")), cluster_id=CLUSTER)

    def test_worker_keeps_network_path_collection_opt_in(self):
        from test_incident_worker_runtime import config
        from incident_platform.krca_runtime import load_krca_runtime_config
        from tools.run_incident_worker import build_collection_service
        settings = config()
        runtime = load_krca_runtime_config(Path(settings.krca_config_path))
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                selected = replace(settings, network_paths_config_path="config/network-dependency-paths.json" if enabled else "")
                with patch("builtins.open", mock_open(read_data="test-only-token")), \
                        patch("tools.run_incident_worker.ssl.create_default_context"):
                    service = build_collection_service(selected, InMemoryIncidentRepository(), runtime)
                provider = next(item.provider for item in service._base_specs if item.name == "hubble")
                self.assertEqual(isinstance(provider, NetworkEvidenceCollector), enabled)

    def test_graph_frozen_context_inspection_gate_report_end_to_end(self):
        repository, localization, evidence = prepared()
        context = localization.context
        self.assertEqual(set(context_evidence_ids(context)), {item["evidence_id"] for item in evidence})
        frozen_before = json.dumps(context, sort_keys=True)
        run = AgentRCAService(repository, BoundedKnowledgeRetriever(EmptyKnowledge()), NetworkFakeRunner()).run(
            context["incident_id"], context_id=context["context_id"], generated_at=NOW)
        self.assertEqual(run.report["status"], "partial")
        self.assertEqual(run.report["root_cause"]["cause_id"], NETWORK_POLICY_CAUSE)
        self.assertEqual(run.report["schema_version"], "1.2.0")
        self.assertEqual(run.incident["status"], "REPORTED")
        self.assertEqual(len(run.audit["inspected_evidence_ids"]), 2)
        self.assertEqual(json.dumps(repository.get_context(context["context_id"]), sort_keys=True), frozen_before)
        validate_contract("rca-report.schema.json", run.report)
        detail = IncidentViewerQueryService(repository).get_incident_detail(context["incident_id"])
        self.assertEqual(detail["reports"][0]["report"]["status"], "partial")
        self.assertEqual(detail["reports"][0]["report"]["schema_version"], "1.2.0")

    def test_evaluation_prediction_keeps_new_registered_cause(self):
        from incident_platform.rca_evaluation import prediction_from_agent_report
        repository, localized, evidence = prepared()
        run = AgentRCAService(repository, BoundedKnowledgeRetriever(EmptyKnowledge()), NetworkFakeRunner()).run(
            localized.context["incident_id"], context_id=localized.context["context_id"], generated_at=NOW)
        prediction = prediction_from_agent_report(evaluation_case_id="eval-network-fixture-0001",
            scenario_id="scenario-network-fixture-0001", incident_id=run.incident["incident_id"],
            report=run.report, available_evidence_ids=[item["evidence_id"] for item in evidence], completed_at=NOW)
        self.assertEqual(prediction["predicted_root_cause_ids"], [NETWORK_POLICY_CAUSE])

    def test_gate_rejects_conclusive_wrong_entity_or_uninspected_citation(self):
        for runner, reason in ((NetworkFakeRunner(decision="CONCLUSIVE"), "GATE_NETWORK_COVERAGE_UNPROVEN"),
                               (NetworkFakeRunner(wrong_entity=True), "GATE_PROOF_ENTITY_MISMATCH"),
                               (NetworkFakeRunner(inspected=False), "GATE_UNINSPECTED_EVIDENCE_CITATION")):
            with self.subTest(reason=reason):
                repository, localized, _ = prepared()
                with self.assertRaises(EvidenceGateViolation) as error:
                    AgentRCAService(repository, BoundedKnowledgeRetriever(EmptyKnowledge()), runner).run(
                        localized.context["incident_id"], context_id=localized.context["context_id"], generated_at=NOW)
                self.assertEqual(error.exception.reason_code, reason)
                self.assertEqual(repository.get(localized.context["incident_id"])["status"], "FAILED")

    def test_fast_path_cannot_overstate_unknown_network_coverage(self):
        repository, localized, evidence = prepared()
        artifacts = FastPathReportBuilder().build(incident=repository.get(localized.context["incident_id"]),
            evidence=evidence, decision=DeterministicRCAEngine().evaluate(evidence), generated_at=NOW)
        self.assertEqual(artifacts.report["status"], "partial")

    def test_old_output_contract_rejects_new_cause(self):
        repository, localized, _ = prepared()
        run = AgentRCAService(repository, BoundedKnowledgeRetriever(EmptyKnowledge()), NetworkFakeRunner()).run(
            localized.context["incident_id"], context_id=localized.context["context_id"], generated_at=NOW)
        legacy = dict(run.report, schema_version="1.1.0")
        with self.assertRaises(ContractViolation):
            validate_contract("rca-report.schema.json", legacy)

    def test_sdk_strict_schema_and_v12_contract_share_the_new_taxonomy(self):
        from incident_platform.root_cause_taxonomy import ROOT_CAUSE_IDS
        output = OpenAIAgentsSDKRunner("offline-schema-check").output_schema
        schema = output.json_schema()
        self.assertTrue(output.is_strict_json_schema())
        self.assertEqual(schema["properties"]["schema_version"]["const"], "1.2.0")
        self.assertEqual(schema["$defs"]["DraftRootCause"]["properties"]["cause_id"]["enum"], list(ROOT_CAUSE_IDS))

    def test_failed_path_read_retains_legacy_hubble_evidence(self):
        from incident_platform.evidence import ProviderBatch
        class Summaries:
            def collect(self, request):
                return ProviderBatch((collect()[1].items[0],))
        class Failed:
            def collect(self, request):
                raise RetryableProviderError("unavailable")
        from incident_platform.errors import RetryableProviderError
        request, _, _ = collect()
        batch = NetworkEvidenceCollector(Summaries(), Failed()).collect(request)
        self.assertEqual(len(batch.items), 1)
        self.assertEqual(batch.status, "PARTIAL")


if __name__ == "__main__":
    unittest.main()
