from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from incident_platform.collectors import CollectorSpec
from incident_platform.deterministic import DeterministicRCAEngine
from incident_platform.errors import (
    ContractViolation,
    InvalidTransition,
    PermanentProviderError,
)
from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceDraft,
    EvidenceWindow,
    ProviderBatch,
    ResourceScope,
    format_time,
)
from incident_platform.incident_work import (
    InMemoryIncidentLocalizationWorkRepository,
    InMemoryIncidentWorkRepository,
)
from incident_platform.incidents import AlertmanagerIngestionService
from incident_platform.krca_pipeline import KRCAGuidedIncidentLocalizationService
from incident_platform.localization import IncidentLocalizationService
from incident_platform.localization_collection import (
    LOCALIZATION_COLLECTION_EVENT,
    prepare_localization_collection,
)
from incident_platform.projectors import (
    HubbleNetworkFlowEvidenceProjector,
    KRCAPIEdgeEvidenceProjector,
    KubernetesEvidenceProjector,
    PrometheusMetricEvidenceProjector,
    PrometheusWorkloadMetricEvidenceProjector,
)
from incident_platform.repository import InMemoryIncidentRepository
from incident_platform.resolution import (
    EntityResolutionRequest,
    ResolvedIncidentLocalizationService,
    ServiceToEntityResolver,
)
from incident_platform.stategraph import EntityIdentity, InMemoryStateGraphRepository
from tools.run_incident_worker import (
    IncidentWorker,
    ProfileAwareIncidentCollectionService,
)

from tests.test_incident_worker_runtime import (
    NOW,
    KRCAInsufficientStaticProvider,
    config,
    krca_config,
    payload,
)


def subject(service, kind="Service"):
    return {
        "cluster_id": config().cluster_id,
        "api_version": "v1",
        "kind": kind,
        "namespace": config().target_namespace,
        "name": service if kind == "Service" else f"{service}-abc-pod",
        "uid": f"{service}-{kind}-uid",
        "exists": True,
    }


def state_draft(request, service, *, pod=False):
    facts = {"result_status": "FOUND"}
    if pod:
        facts["last_termination_reason"] = "OOMKilled"
        facts["restart_count"] = 1
    else:
        facts["relationships"] = [
            {
                **subject(service, "Pod"),
                "relation_type": "ROUTES_TO",
                "reference_key": "endpoints",
            }
        ]
    return EvidenceDraft(
        source="kubernetes",
        kind="resource-state",
        observed_at=request.window.end,
        subject=subject(service, "Pod" if pod else "Service"),
        summary="Scoped fixture workload state",
        facts=facts,
        provider="kubernetes-http-api",
        query="scoped Kubernetes fixture",
        locator=f"fixture://kubernetes/{service}/{'pod' if pod else 'service'}",
    )


class DownstreamProvider:
    def __init__(self, *, metric=False, service_metric=False, fail=False):
        self.metric = metric
        self.service_metric = service_metric
        self.fail = fail
        self.requests = []

    def collect(self, request):
        self.requests.append(request)
        if self.fail:
            raise PermanentProviderError("fixture telemetry unavailable")
        items = []
        for service in request.scope.resource_names:
            if self.metric or self.service_metric:
                items.append(
                    EvidenceDraft(
                        source="prometheus",
                        kind="metric-summary",
                        observed_at=request.window.end,
                        subject=subject(
                            service, "Service" if self.service_metric else "Pod"
                        ),
                        summary="Same Pod UID restarted",
                        provider="prometheus-http-api",
                        facts={
                            "metric": (
                                "request_error_ratio"
                                if self.service_metric
                                else "restart_count_delta"
                            ),
                            "result_status": "HAS_DATA",
                            "sample_count": 2,
                            "peak_delta": 1.0,
                        },
                        query="scoped restart metric",
                        locator=f"fixture://metric/{service}",
                    )
                )
            else:
                items.extend(
                    (
                        state_draft(request, service),
                        state_draft(request, service, pod=True),
                    )
                )
        return ProviderBatch(tuple(items))


def prepare_incident(incidents):
    incident = (
        AlertmanagerIngestionService(incidents)
        .ingest(
            payload(krca_profile="checkout-fixture"),
            received_at=NOW,
        )[0]
        .incident
    )
    incident_id = incident["incident_id"]
    window = EvidenceWindow(incident["window"]["baseline_start"], format_time(NOW))
    request = CollectionRequest(
        request_id="req-downstream-fixture-0001",
        incident_id=incident_id,
        window=window,
        scope=ResourceScope(
            namespace=config().target_namespace,
            resource_names=("frontend",),
        ),
        timeout_seconds=2,
    )
    incomplete = KRCAInsufficientStaticProvider().collect(request).items[0]
    feature = replace(
        incomplete,
        completeness=1.0,
        facts={
            **incomplete.facts,
            "result_status": "HAS_DATA",
            "failure_rate_correlation": 0.99,
            "failure_rate_p_value": 0.001,
            "latency_anomaly": 0.9,
            "latency_fluctuation_contribution": 0.9,
            "latency_correlation": 0.99,
        },
    )
    frontend = replace(
        state_draft(request, "frontend"), facts={"result_status": "FOUND"}
    )
    metric = DownstreamProvider(service_metric=True).collect(request).items[0]
    evidence = tuple(
        EvidenceBuilder().build(draft, request, collected_at=NOW)
        for draft in (frontend, feature, metric)
    )
    incidents.transition(
        incident_id,
        expected_status="RECEIVED",
        next_status="COLLECTING",
        occurred_at=NOW,
    )
    statuses = [
        {
            "collector": name,
            "status": "SUCCEEDED",
            "attempts": 1,
            "started_at": format_time(NOW),
            "ended_at": format_time(NOW),
            "error": None,
        }
        for name in (
            "kubernetes",
            "prometheus",
            "prometheus-workload",
            "prometheus-api",
        )
    ]
    incidents.replace_collector_statuses(incident_id, statuses, occurred_at=NOW)
    incidents.store_evidence(incident_id, evidence)
    incidents.transition(
        incident_id,
        expected_status="COLLECTING",
        next_status="LOCALIZING",
        occurred_at=NOW,
    )
    return incidents.get(incident_id), request, evidence


class DownstreamCollectionTests(unittest.TestCase):
    def setUp(self):
        self.incidents = InMemoryIncidentRepository()
        self.incident, self.initial_request, self.initial_evidence = prepare_incident(
            self.incidents
        )
        self.incident_id = self.incident["incident_id"]
        self.now = NOW + timedelta(seconds=1)
        self.work = InMemoryIncidentLocalizationWorkRepository(self.incidents)
        self.work.enqueue(self.incident_id, available_at=NOW)
        self.graph = InMemoryStateGraphRepository()
        self.projector = KubernetesEvidenceProjector()
        # Background inventory resolves the seed but is not Incident cause proof.
        observer_request = replace(
            self.initial_request,
            scope=ResourceScope(
                namespace=config().target_namespace,
                resource_names=("checkoutservice",),
            ),
        )
        observer = EvidenceBuilder().build(
            state_draft(observer_request, "checkoutservice"),
            observer_request,
            collected_at=NOW,
        )
        for item in (self.initial_evidence[0], observer):
            self.graph.ingest(self.projector.project(item).records)
        resolver = ServiceToEntityResolver(self.graph)
        core = IncidentLocalizationService(
            self.incidents,
            self.graph,
            (
                self.projector,
                KRCAPIEdgeEvidenceProjector(),
                PrometheusMetricEvidenceProjector(),
                PrometheusWorkloadMetricEvidenceProjector(),
            ),
        )
        self.guided = KRCAGuidedIncidentLocalizationService(resolver, core)
        self.kubernetes = DownstreamProvider()
        self.metric = DownstreamProvider(metric=True)
        self.collection = ProfileAwareIncidentCollectionService(
            self.incidents,
            (
                CollectorSpec("kubernetes", self.kubernetes, timeout_seconds=1),
                CollectorSpec(
                    "prometheus",
                    DownstreamProvider(service_metric=True),
                    timeout_seconds=1,
                ),
                CollectorSpec("prometheus-workload", self.metric, timeout_seconds=1),
            ),
            None,
            krca_config(),
        )
        self.worker = IncidentWorker(
            config(),
            self.incidents,
            InMemoryIncidentWorkRepository(self.incidents),
            self.collection,
            self.work,
            ResolvedIncidentLocalizationService(resolver, core),
            krca_config(),
            self.guided,
            clock=lambda: self.now,
        )

    def plan(self, **overrides):
        profile = krca_config().profile("checkout-fixture")
        request = EntityResolutionRequest(
            incident_id=self.incident_id,
            cluster_id=config().cluster_id,
            namespace=config().target_namespace,
            service_name="frontend",
            window=EvidenceWindow(
                self.initial_request.window.start, format_time(self.now)
            ),
        )
        kwargs = dict(
            profile_id=profile.profile_id,
            alerting_api=profile.alerting_api,
            expected_edges={
                edge.edge_id: (edge.parent, edge.child) for edge in profile.dependencies
            },
            evidence=self.incidents.list_evidence(self.incident_id),
        )
        kwargs.update(overrides)
        return self.guided.plan(request, **kwargs)

    def claim(self):
        return self.work.claim_next(
            worker_id="downstream-test",
            now=self.now,
            lease_duration=timedelta(seconds=120),
            max_attempts=3,
        )

    def test_planning_is_read_only_and_keeps_original_incident_source(self):
        plan = self.plan()
        self.assertEqual(plan.additional_services, ("checkoutservice",))
        self.assertEqual(self.incidents.get(self.incident_id), self.incident)
        self.assertEqual(len(self.incidents.list_evidence(self.incident_id)), 3)
        self.assertEqual(self.kubernetes.requests, [])
        self.assertEqual(plan.scope.window.end, format_time(self.now))

    def test_worker_collects_downstream_proof_then_freezes(self):
        result = self.worker.process_one()
        self.assertEqual(result["status"], "PROCESSED", result)
        self.assertEqual(result["resolution_method"], "krca-top-services")
        self.assertEqual(
            self.incidents.get(self.incident_id)["source_entity"]["name"], "frontend"
        )
        context = self.incidents.get_context(result["context_id"])
        checkpoint = self.incidents.get_localization_collection(self.incident_id)
        self.assertEqual(checkpoint["selection"]["services"], ["checkoutservice"])
        self.assertTrue(set(checkpoint["evidence_ids"]) <= set(context["evidence_ids"]))
        evidence = [
            item
            for item in self.incidents.list_evidence(self.incident_id)
            if item["evidence_id"] in context["evidence_ids"]
        ]
        proof = DeterministicRCAEngine().evaluate_rule(
            "kubernetes.container-oomkilled", evidence
        )
        self.assertEqual(proof.status, "PROVEN")
        for provider in (self.kubernetes, self.metric):
            self.assertEqual(len(provider.requests), 1)
            request = provider.requests[0]
            self.assertEqual(request.scope.resource_names, ("checkoutservice",))
            self.assertEqual(
                request.scope.resource_name_prefixes, ("checkoutservice-",)
            )
            self.assertEqual(request.scope.max_items, config().max_evidence_items)
        events = self.incidents.list_audit_events(self.incident_id)
        types = [event.event_type for event in events]
        self.assertLess(types.index(LOCALIZATION_COLLECTION_EVENT), len(types) - 1)
        self.assertEqual(events[-1].details["to"], "ANALYZING")

    def test_downstream_context_reaches_agent_and_gate_with_fake_model(self):
        from tests.test_agent_rca import SuccessfulFakeRunner, service

        class ProofSelectingFakeRunner(SuccessfulFakeRunner):
            def run(self, invocation):
                model_run = super().run(invocation)
                proof = DeterministicRCAEngine().evaluate_rule(
                    "kubernetes.container-oomkilled",
                    invocation.evidence,
                )
                ids = list(proof.supporting_evidence_ids)
                for evidence_id in ids:
                    invocation.tool_runtime.inspect_evidence(evidence_id)
                draft = copy.deepcopy(model_run.draft)
                draft["root_cause"]["supporting_evidence_ids"] = ids
                draft["hypotheses"][0]["supporting_evidence_ids"] = ids
                return replace(model_run, draft=draft)

        result = self.worker.process_one()
        entity_id = EntityIdentity.kubernetes_resource(
            cluster_id=config().cluster_id,
            uid=subject("checkoutservice", "Pod")["uid"],
        ).entity_id
        with patch("tests.test_agent_rca.ENTITY_ID", entity_id):
            run = service(self.incidents, ProofSelectingFakeRunner()).run(
                self.incident_id,
                context_id=result["context_id"],
                generated_at=self.now,
            )
        self.assertEqual(run.report["status"], "conclusive")
        self.assertEqual(self.incidents.get(self.incident_id)["status"], "REPORTED")

    def test_provider_failure_is_preserved_and_cannot_prove_oom(self):
        self.metric.fail = True
        result = self.worker.process_one()
        self.assertEqual(result["status"], "PROCESSED", result)
        context = self.incidents.get_context(result["context_id"])
        failures = {
            item["collector"]: item["error"] for item in context["collector_failures"]
        }
        self.assertIn("downstream", failures["prometheus-workload"])
        proof = DeterministicRCAEngine().evaluate_rule(
            "kubernetes.container-oomkilled",
            self.incidents.list_evidence(self.incident_id),
        )
        self.assertEqual(proof.status, "INSUFFICIENT")

    def test_completed_checkpoint_survives_worker_reclaim_without_recollection(self):
        claim = self.claim()
        self.worker._collect_krca_candidates(
            claim, self.plan(), krca_config().profiles[0]
        )
        saved = self.incidents.get_localization_collection(self.incident_id)
        self.now += timedelta(seconds=121)
        result = self.worker.process_one()
        self.assertEqual(result["status"], "PROCESSED", result)
        self.assertEqual(len(self.kubernetes.requests), 1)
        self.assertEqual(len(self.metric.requests), 1)
        self.assertEqual(
            self.incidents.get_localization_collection(self.incident_id), saved
        )

    def test_unresolved_top_service_falls_back_without_extra_reads(self):
        self.guided._top_scope_resolver._resolver = ServiceToEntityResolver(
            InMemoryStateGraphRepository()
        )
        result = self.worker.process_one()
        self.assertEqual(result["status"], "PROCESSED", result)
        self.assertEqual(result["resolution_method"], "source-entity-krca-fallback")
        self.assertEqual(self.kubernetes.requests, [])

    def test_feature_cluster_and_namespace_and_window_escape_are_rejected(self):
        for mutate in (
            lambda item: item["subject"].update(cluster_id="another-cluster"),
            lambda item: item["subject"].update(namespace="another-namespace"),
            lambda item: item["window"].update(
                end=format_time(self.now + timedelta(seconds=1))
            ),
        ):
            with self.subTest(mutate=mutate):
                evidence = copy.deepcopy(self.initial_evidence)
                mutate(evidence[1])
                with self.assertRaises(ContractViolation):
                    self.plan(evidence=evidence)
        self.assertEqual(self.kubernetes.requests, [])

    def test_additional_scope_cannot_escape_profile_or_service_budget(self):
        for services in (("unrelated",), ("frontend",), (), ("checkoutservice",) * 4):
            with self.subTest(services=services), self.assertRaises(ValueError):
                self.collection.collect_localization_candidates(
                    self.plan().request,
                    profile_id="checkout-fixture",
                    service_names=services,
                    max_items=8,
                    observed_at=self.now,
                )
        self.assertEqual(self.kubernetes.requests, [])

    def test_expired_or_reclaimed_worker_cannot_persist_a_collection(self):
        claim = self.claim()
        plan = self.plan()
        run = self.collection.collect_localization_candidates(
            plan.request,
            profile_id="checkout-fixture",
            service_names=plan.additional_services,
            max_items=8,
            observed_at=self.now,
        )
        self.now += timedelta(seconds=121)
        for reclaim in (False, True):
            if reclaim:
                self.claim()
            with self.subTest(reclaim=reclaim), self.assertRaises(InvalidTransition):
                self.work.store_collection(
                    claim,
                    selection={},
                    window=plan.request.window,
                    collector_statuses=run.collector_statuses,
                    evidence_items=run.evidence,
                    now=self.now,
                )
        self.assertEqual(len(self.incidents.list_evidence(self.incident_id)), 3)
        self.assertIsNone(self.incidents.get_localization_collection(self.incident_id))

    def test_invalid_batch_is_atomic_and_does_not_replace_initial_statuses(self):
        claim = self.claim()
        self.worker._collect_krca_candidates(
            claim, self.plan(), krca_config().profiles[0]
        )
        checkpoint = self.incidents.get_localization_collection(self.incident_id)
        evidence = self.incidents.list_evidence(self.incident_id)
        invalid = copy.deepcopy(evidence[-1])
        invalid["subject"]["namespace"] = "unrelated"
        before = self.incidents.get(self.incident_id)
        with self.assertRaises(InvalidTransition):
            self.work.store_collection(
                claim,
                selection=checkpoint["selection"],
                window=EvidenceWindow(**checkpoint["window"]),
                collector_statuses=checkpoint["collector_statuses"],
                evidence_items=(invalid,),
                now=self.now,
            )
        self.assertEqual(self.incidents.get(self.incident_id), before)
        self.assertEqual(self.incidents.list_evidence(self.incident_id), evidence)

    def test_supplemental_collection_cannot_run_after_context_freeze(self):
        result = self.worker.process_one()
        self.assertEqual(result["status"], "PROCESSED", result)
        with self.assertRaises(ValueError):
            self.collection.collect_localization_candidates(
                self.plan().request,
                profile_id="checkout-fixture",
                service_names=("checkoutservice",),
                max_items=8,
                observed_at=self.now,
            )

    def test_successful_downstream_read_does_not_erase_initial_failure(self):
        claim = self.claim()
        self.worker._collect_krca_candidates(
            claim, self.plan(), krca_config().profiles[0]
        )
        checkpoint = self.incidents.get_localization_collection(self.incident_id)
        incident = copy.deepcopy(self.incident)
        initial = next(
            item
            for item in incident["collector_statuses"]
            if item["collector"] == "prometheus-workload"
        )
        initial.update(status="TIMED_OUT", error="initial timeout")
        updated, _, _ = prepare_localization_collection(
            incident,
            selection=checkpoint["selection"],
            window=EvidenceWindow(**checkpoint["window"]),
            collector_statuses=checkpoint["collector_statuses"],
            evidence_items=(),
            now=self.now,
        )
        merged = next(
            item
            for item in updated["collector_statuses"]
            if item["collector"] == "prometheus-workload"
        )
        self.assertEqual(merged["status"], "PARTIAL")
        self.assertIn("initial timeout", merged["error"])

    def test_checkpoint_store_is_idempotent(self):
        claim = self.claim()
        self.worker._collect_krca_candidates(
            claim, self.plan(), krca_config().profiles[0]
        )
        checkpoint = self.incidents.get_localization_collection(self.incident_id)
        evidence = [
            item
            for item in self.incidents.list_evidence(self.incident_id)
            if item["evidence_id"] in checkpoint["evidence_ids"]
        ]
        replay = self.work.store_collection(
            claim,
            selection=checkpoint["selection"],
            window=EvidenceWindow(**checkpoint["window"]),
            collector_statuses=checkpoint["collector_statuses"],
            evidence_items=evidence,
            now=self.now,
        )
        self.assertEqual(replay, checkpoint)
        events = [
            event
            for event in self.incidents.list_audit_events(self.incident_id)
            if event.event_type == LOCALIZATION_COLLECTION_EVENT
        ]
        self.assertEqual(len(events), 1)

    def test_item_budget_is_shared_across_selected_workloads_per_provider(self):
        plan = self.plan()
        run = self.collection.collect_localization_candidates(
            plan.request,
            profile_id="checkout-fixture",
            service_names=plan.additional_services,
            max_items=1,
            observed_at=self.now,
        )
        kube = next(item for item in run.executions if item.name == "kubernetes")
        self.assertEqual(kube.status, "FAILED")
        self.assertEqual(kube.evidence, ())
        self.assertEqual(run.status, "PARTIAL")

    def install_hubble_fixture(self, *, gaps=()):
        from incident_platform.providers.hubble import (
            HubbleNetworkFlowProvider,
            HubbleFlowResult,
        )
        from tests.test_hubble_provider import StaticHubbleClient, flow

        item = flow(
            "downstream-network", verdict="DROPPED", drop_reason="POLICY_DENIED"
        )
        item["time"] = format_time(self.now)
        client = StaticHubbleClient(
            {
                "from": HubbleFlowResult((item,), observation_gaps=gaps),
                "to": HubbleFlowResult(()),
            }
        )
        self.collection._base_specs += (
            CollectorSpec(
                "hubble",
                HubbleNetworkFlowProvider(client, cluster_id=config().cluster_id),
                timeout_seconds=1,
                lookback_seconds=900,
            ),
        )
        core = self.guided._localization_service
        core._projectors += (HubbleNetworkFlowEvidenceProjector(),)
        return client

    def test_hubble_reaches_downstream_frozen_context_and_agent_catalog(self):
        from incident_platform.agent_rca import EvidenceCandidateSelector

        client = self.install_hubble_fixture()
        result = self.worker.process_one()
        self.assertEqual(result["status"], "PROCESSED", result)
        context = self.incidents.get_context(result["context_id"])
        evidence = self.incidents.list_evidence(self.incident_id)
        hubble = next(item for item in evidence if item["source"] == "hubble")
        self.assertEqual(hubble["subject"]["name"], "checkoutservice")
        self.assertEqual(hubble["facts"]["flow_signal"], "POLICY_DENIAL_OBSERVED")
        self.assertEqual(hubble["facts"]["retention_status"], "UNKNOWN")
        self.assertIn(hubble["evidence_id"], context["evidence_ids"])
        catalog = EvidenceCandidateSelector().select(
            context, evidence, max_candidates=12
        )
        self.assertIn(hubble["evidence_id"], catalog)
        self.assertEqual(
            {call["pod_prefix"] for call in client.calls}, {"checkoutservice"}
        )
        self.assertEqual(
            {call["namespace"] for call in client.calls}, {config().target_namespace}
        )
        self.assertEqual(
            self.incidents.get(self.incident_id)["source_entity"]["name"], "frontend"
        )

    def test_hubble_loss_reaches_context_without_discarding_observed_flows(self):
        self.install_hubble_fixture(gaps=("FLOW_EVENTS_LOST",))
        result = self.worker.process_one()
        self.assertEqual(result["status"], "PROCESSED", result)
        context = self.incidents.get_context(result["context_id"])
        failure = next(
            item
            for item in context["collector_failures"]
            if item["collector"] == "hubble"
        )
        self.assertIn("FLOW_EVENTS_LOST", failure["error"])
        evidence = self.incidents.list_evidence(self.incident_id)
        hubble = next(item for item in evidence if item["source"] == "hubble")
        self.assertIn(hubble["evidence_id"], context["evidence_ids"])
        self.assertEqual(hubble["facts"]["flow_count"], 1)
