from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from incident_platform.contracts import validate_contract
from incident_platform.deterministic import DeterministicRCAEngine
from incident_platform.evidence import (
    CollectionRequest, EvidenceBuilder, EvidenceDraft, EvidenceWindow, ResourceScope,
)
from incident_platform.proof_binding import bound_root_proof
from incident_platform.stategraph import EntityIdentity
from tests.test_agent_rca import NOW, prepared_repository


OOM = "kubernetes.container-oomkilled"
IMAGE = "kubernetes.image-pull-failure"
CONFIG = "kubernetes.missing-configmap"


def subject(name="pod-a", *, uid="uid-a", kind="Pod", cluster="cluster-a", namespace="apps", **extra):
    return {
        "cluster_id": cluster, "api_version": "v1", "kind": kind,
        "namespace": namespace, "name": name, "uid": uid, "exists": True, **extra,
    }


def entity(resource):
    if resource["kind"] == "ConfigMap" and resource["exists"] is False:
        identity = EntityIdentity.kubernetes_placeholder(
            **{key: resource[key] for key in ("cluster_id", "api_version", "kind", "namespace", "name")}
        )
    else:
        identity = EntityIdentity.kubernetes_resource(
            cluster_id=resource["cluster_id"], uid=resource["uid"],
        )
    return {
        "entity_id": identity.entity_id, "entity_type": resource["kind"],
        "domain": "kubernetes", "name": resource["name"],
        "scope": {key: resource[key] for key in ("cluster_id", "namespace", "api_version")},
        "external_ref": resource.get("uid"), "exists": resource["exists"],
    }


class ProofBindingTests(unittest.TestCase):
    def setUp(self):
        repository, incident_id, context_id = prepared_repository()
        self.base = repository.get_context(context_id)
        self.request = CollectionRequest(
            request_id="req-proof-binding-0001", incident_id=incident_id,
            window=EvidenceWindow(start="2026-08-22T01:45:00Z", end="2026-08-22T01:59:00Z"),
            scope=ResourceScope(namespace="apps", resource_names=("pod-a",)), timeout_seconds=1.0,
        )
        self.pod_a = subject()
        self.pod_b = subject("pod-b", uid="uid-b")

    def evidence(self, resource, facts, *, kind="resource-state", source="kubernetes"):
        return EvidenceBuilder().build(EvidenceDraft(
            source=source, kind=kind, observed_at="2026-08-22T01:58:30Z",
            subject=resource, summary="Proof binding fixture Evidence.", facts=facts,
            provider="proof-binding-test", query="bounded fixture query",
            locator="fixture://proof-binding",
        ), replace(self.request, scope=ResourceScope(
            namespace=resource["namespace"], resource_names=(resource["name"],),
        )), collected_at=NOW)

    def pair(self, resource, cause=OOM):
        if cause == IMAGE:
            return [
                self.evidence(resource, {"waiting_reason": "ImagePullBackOff"}),
                self.evidence(resource, {"message_code": "ImagePullBackOff"}, kind="kubernetes-event"),
            ]
        return [
            self.evidence(resource, {"last_termination_reason": "OOMKilled"}),
            self.evidence(resource, {"metric": "restart_count_delta", "peak_delta": 1},
                          source="prometheus", kind="metric-summary"),
        ]

    def bind(self, target, evidence, *, cause=OOM, paths=(), extra=(), source_entity=None):
        context = copy.deepcopy(self.base)
        context["source_entity"] = source_entity if source_entity is not None else target
        context["evidence_ids"] = [item["evidence_id"] for item in evidence]
        context["state_paths"] = [
            {"path_id": f"path-binding-{index:04d}", "entities": list(entities),
             "relations": list(relations), "evidence_ids": context["evidence_ids"]}
            for index, (entities, relations) in enumerate([
                ([target], []), *paths, *(([item], []) for item in extra),
            ])
        ]
        context["scope"].update(seed_entity_ids=[target["entity_id"]], max_entities=40, max_depth=5)
        validate_contract("context-package.schema.json", context)
        before = copy.deepcopy((context, evidence))
        proof = bound_root_proof(cause_id=cause, context=context, entity=target,
                                 evidence=evidence, proof_engine=DeterministicRCAEngine())
        self.assertEqual((context, evidence), before)
        return proof

    def test_exact_pod_proof_and_unrelated_pod_for_each_cause(self):
        for cause in (OOM, IMAGE):
            with self.subTest(cause=cause):
                evidence = self.pair(self.pod_a, cause)
                proof = self.bind(entity(self.pod_a), evidence, cause=cause)
                self.assertEqual(set(proof.supporting_evidence_ids), {item["evidence_id"] for item in evidence})
                self.assertIsNone(self.bind(entity(self.pod_b), evidence, cause=cause, extra=[entity(self.pod_a)]))

    def test_same_name_different_uid_or_cluster_rejected(self):
        for cause in (OOM, IMAGE):
            for replacement in (subject(uid="different-uid"), subject(cluster="other-cluster")):
                with self.subTest(cause=cause, replacement=replacement):
                    self.assertIsNone(self.bind(entity(replacement), self.pair(self.pod_a, cause), cause=cause))

    def test_missing_identity_and_inconsistent_kind_or_scope_fail_closed(self):
        for overrides in ({"uid": None}, {"kind": "Service"}):
            with self.subTest(overrides=overrides):
                self.assertIsNone(self.bind(entity(self.pod_a), self.pair({**self.pod_a, **overrides})))
        without_cluster = {key: value for key, value in self.pod_a.items() if key != "cluster_id"}
        self.assertIsNone(self.bind(entity(self.pod_a), self.pair(without_cluster)))
        without_namespace = {**entity(self.pod_a), "scope": {"cluster_id": "cluster-a"}}
        self.assertIsNone(self.bind(without_namespace, self.pair(self.pod_a)))
        bad_scope = {**entity(self.pod_a), "scope": {"cluster_id": "other", "namespace": "apps"}}
        self.assertIsNone(self.bind(bad_scope, self.pair(self.pod_a)))

    def test_extra_harmless_target_citation_does_not_authorize_target(self):
        for cause in (OOM, IMAGE):
            evidence = [*self.pair(self.pod_a, cause), self.evidence(self.pod_b, {"ready": True})]
            self.assertIsNone(self.bind(entity(self.pod_b), evidence, cause=cause, extra=[entity(self.pod_a)]))

    def test_multiple_proven_pods_are_target_and_evidence_order_independent(self):
        for cause in (OOM, IMAGE):
            a, b = self.pair(self.pod_a, cause), self.pair(self.pod_b, cause)
            for target, expected in ((self.pod_a, a), (self.pod_b, b)):
                for evidence in (a + b, b + a, list(reversed(a + b))):
                    with self.subTest(cause=cause, target=target["name"]):
                        proof = self.bind(entity(target), evidence, cause=cause, extra=[entity(self.pod_a), entity(self.pod_b)])
                        self.assertEqual(set(proof.supporting_evidence_ids), {item["evidence_id"] for item in expected})

    def test_service_and_logical_service_selection_forward_and_reverse(self):
        pod = entity(self.pod_a)
        service = entity(subject("service-a", uid="svc-a", kind="Service"))
        logical = {**service, "domain": "web-service", "entity_id": EntityIdentity.logical_service(
            cluster_id="cluster-a", namespace="apps", service_name="service-a").entity_id}
        for cause in (OOM, IMAGE):
            for reverse in (False, True):
                entities, relations = [logical, service, pod], ["REPRESENTED_BY", "SELECTS"]
                if reverse:
                    entities, relations = list(reversed(entities)), list(reversed(relations))
                for target in (service, logical):
                    self.assertIsNotNone(self.bind(target, self.pair(self.pod_a, cause), cause=cause, paths=[(entities, relations)]))

    def test_deployment_ownership_accepts_both_path_directions(self):
        deployment = entity(subject("deployment-a", uid="deploy-a", kind="Deployment"))
        replica = entity(subject("replica-a", uid="rs-a", kind="ReplicaSet"))
        pod = entity(self.pod_a)
        for reverse in (False, True):
            entities = [deployment, replica, pod]
            if reverse:
                entities.reverse()
            for target in (deployment, replica):
                self.assertIsNotNone(self.bind(target, self.pair(self.pod_a), paths=[(entities, ["OWNS", "OWNS"])]))

    def test_sibling_and_nonownership_relations_do_not_authorize(self):
        a, b = entity(self.pod_a), entity(self.pod_b)
        replica = entity(subject("replica-a", uid="rs-a", kind="ReplicaSet"))
        node = entity(subject("node-a", uid="node-a", kind="Node"))
        service = entity(subject("service-a", uid="svc-a", kind="Service"))
        cases = [
            (b, [([a, replica, b], ["OWNS", "OWNS"])]),
            (b, [([a, b], ["CALLS"])]),
            (b, [([a, node, b], ["SCHEDULED_ON", "SCHEDULED_ON"])]),
            (service, [([service, a], ["CALLS"])]),
            (service, [([service, a], ["SCHEDULED_ON"])]),
        ]
        for target, paths in cases:
            with self.subTest(paths=paths):
                self.assertIsNone(self.bind(target, self.pair(self.pod_a), paths=paths))

    def test_aggregate_requires_matching_scope_and_consecutive_allowed_pairs(self):
        a = entity(self.pod_a)
        for overrides in ({"cluster": "other"}, {"namespace": "other"}):
            service = entity(subject("service-a", uid="svc-a", kind="Service", **overrides))
            self.assertIsNone(self.bind(service, self.pair(self.pod_a), paths=[([service, a], ["SELECTS"])]))
        service = entity(subject("service-a", uid="svc-a", kind="Service"))
        intermediate = entity(self.pod_b)
        self.assertIsNone(self.bind(service, self.pair(self.pod_a), paths=[([service, intermediate, a], ["SELECTS", "CALLS"])]))
        deployment = entity(subject("deployment-a", uid="deploy-a", kind="Deployment"))
        self.assertIsNone(self.bind(deployment, self.pair(self.pod_a), paths=[([deployment, a], ["OWNS"])]))

    def missing_pair(self, *, resource=None, pod=None, facts=None):
        configmap = resource or subject("config-a", uid=None, kind="ConfigMap", exists=False)
        return [
            self.evidence(configmap, {"required": True}),
            self.evidence(pod or self.pod_a, facts or {
                "reason": "FailedMount", "missing_kind": "ConfigMap", "missing_name": configmap["name"],
            }, kind="kubernetes-event"),
        ]

    def test_missing_configmap_binds_absence_affected_pod_and_aggregate(self):
        cm = entity(subject("config-a", uid=None, kind="ConfigMap", exists=False))
        pod = entity(self.pod_a)
        service = entity(subject("service-a", uid="svc-a", kind="Service"))
        for target in (cm, pod, service):
            proof = self.bind(target, self.missing_pair(), cause=CONFIG,
                              paths=[([service, pod], ["SELECTS"])], extra=[cm])
            self.assertEqual(len(proof.supporting_evidence_ids), 2)

    def test_missing_configmap_wrong_target_and_event_rejected(self):
        cm = subject("config-a", uid=None, kind="ConfigMap", exists=False)
        for wrong in (
            {**cm, "name": "config-b"}, {**cm, "namespace": "other"},
            {**cm, "cluster_id": "other"}, self.pod_b,
        ):
            self.assertIsNone(self.bind(entity(wrong), self.missing_pair(), cause=CONFIG, extra=[entity(self.pod_a)]))
        for event_pod in (subject(namespace="other"), subject(cluster="other"), subject(uid=None)):
            self.assertIsNone(self.bind(entity(cm), self.missing_pair(pod=event_pod), cause=CONFIG, extra=[entity(self.pod_a)]))
        for facts in (
            {"reason": "FailedMount", "missing_kind": "ConfigMap", "missing_name": "wrong"},
            {"reason": "FailedMount", "missing_kind": "Secret", "missing_name": "config-a"},
            {"reason": "Unrelated", "missing_kind": "ConfigMap", "missing_name": "config-a"},
        ):
            self.assertIsNone(self.bind(entity(cm), self.missing_pair(facts=facts), cause=CONFIG, extra=[entity(self.pod_a)]))

    def test_missing_configmap_pair_cannot_mix_clusters_even_if_both_pods_in_context(self):
        cm = subject("config-a", uid=None, kind="ConfigMap", exists=False)
        other_pod = subject(cluster="other-cluster")
        for target in (entity(cm), entity(other_pod)):
            self.assertIsNone(self.bind(target, self.missing_pair(pod=other_pod), cause=CONFIG, extra=[entity(self.pod_a), entity(other_pod)]))

    def test_missing_configmap_extra_event_does_not_authorize_wrong_pod(self):
        evidence = [*self.missing_pair(), self.evidence(self.pod_b, {
            "reason": "FailedMount", "missing_kind": "ConfigMap", "missing_name": "other-config",
        }, kind="kubernetes-event")]
        self.assertIsNone(self.bind(entity(self.pod_b), evidence, cause=CONFIG, extra=[entity(self.pod_a)]))

    def test_resource_shaped_source_uses_frozen_graph_path_identity(self):
        proof = self.bind(entity(self.pod_a), self.pair(self.pod_a), source_entity=self.pod_a)
        self.assertIsNotNone(proof)

    def test_future_cause_has_no_implicit_binding(self):
        self.assertIsNone(self.bind(entity(self.pod_a), self.pair(self.pod_a), cause="future.cause"))
