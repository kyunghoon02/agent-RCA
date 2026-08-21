from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone

from incident_platform.contracts import validate_contract
from incident_platform.errors import ContractViolation
from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceDraft,
    EvidenceWindow,
    ResourceScope,
)
from incident_platform.projectors import KubernetesEvidenceProjector
from incident_platform.stategraph import (
    EntityIdentity,
    GraphLocalizer,
    InMemoryStateGraphRepository,
    InvestigationScope,
    StateGraphRepository,
    stable_graph_id,
    state_content_hash,
)


UTC = timezone.utc
WINDOW = EvidenceWindow(
    start="2026-08-12T01:00:00Z",
    end="2026-08-12T01:10:00Z",
)


def graph_entity(
    entity_id: str,
    *,
    name: str,
    evidence_id: str,
    observed_at: str = "2026-08-12T01:01:00Z",
) -> dict:
    identity = EntityIdentity.external(
        domain="web-service", external_key=f"fixture:{name}"
    )
    return {
        "record_type": "entity",
        "entity_id": entity_id,
        "identity": identity.to_contract(),
        "entity_type": "Service",
        "domain": "web-service",
        "name": name,
        "scope": {"environment": "test"},
        "external_ref": f"service://{name}",
        "exists": True,
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "evidence_ids": [evidence_id],
    }


def graph_entity_id(name: str) -> str:
    return EntityIdentity.external(
        domain="web-service", external_key=f"fixture:{name}"
    ).entity_id


def graph_snapshot(
    entity_id: str,
    *,
    state: dict,
    observed_at: str,
    evidence_id: str,
) -> dict:
    digest = state_content_hash(state)
    return {
        "record_type": "snapshot_interval",
        "snapshot_id": stable_graph_id(
            "snap",
            {
                "entity_id": entity_id,
                "state_hash": digest,
                "valid_from": observed_at,
            },
        ),
        "entity_id": entity_id,
        "observed_at": observed_at,
        "valid_from": observed_at,
        "valid_to": None,
        "state_hash": digest,
        "state": copy.deepcopy(state),
        "evidence_ids": [evidence_id],
    }


def build_evidence(draft: EvidenceDraft) -> dict:
    request = CollectionRequest(
        request_id="req-stategraph-fixture-0001",
        incident_id="inc-stategraph-fixture-0001",
        window=WINDOW,
        scope=ResourceScope(
            namespace="online-boutique",
            resource_names=("checkoutservice", "checkout-settings"),
            max_items=10,
        ),
        timeout_seconds=5,
    )
    return EvidenceBuilder().build(
        draft,
        request,
        collected_at=datetime(2026, 8, 12, 1, 6, tzinfo=UTC),
    )


def kubernetes_evidence() -> tuple[dict, dict]:
    event = build_evidence(
        EvidenceDraft(
            source="kubernetes",
            kind="kubernetes-event",
            observed_at="2026-08-12T01:05:00Z",
            subject={
                "cluster_id": "gcp-dev-01",
                "api_version": "v1",
                "kind": "Pod",
                "namespace": "online-boutique",
                "name": "checkoutservice",
                "uid": "7df6d266-40df-4fd6-942d-7ebc864c4061",
                "exists": True,
            },
            summary="Pod reported a missing ConfigMap.",
            facts={
                "type": "Warning",
                "reason": "FailedMount",
                "message_code": "FailedMount",
                "count": 2,
                "source_component": "kubelet",
                "missing_kind": "ConfigMap",
                "missing_name": "checkout-settings",
            },
            provider="kubernetes-http-api",
            query="list Event for checkoutservice",
            locator="k8s://online-boutique/Event/checkout-missing-config",
            freshness="recent",
        )
    )
    resource = build_evidence(
        EvidenceDraft(
            source="kubernetes",
            kind="resource-state",
            observed_at="2026-08-12T01:05:02Z",
            subject={
                "cluster_id": "gcp-dev-01",
                "api_version": "v1",
                "kind": "ConfigMap",
                "namespace": "online-boutique",
                "name": "checkout-settings",
                "uid": None,
                "exists": False,
            },
            summary="Required ConfigMap was not found.",
            facts={"result_status": "NOT_FOUND", "required": True},
            provider="kubernetes-http-api",
            query="get ConfigMap checkout-settings",
            locator="k8s://online-boutique/ConfigMap/checkout-settings",
        )
    )
    return event, resource


class InvestigationScopeTests(unittest.TestCase):
    def test_scope_is_domain_neutral_and_bounded(self) -> None:
        scope = InvestigationScope(
            incident_id="inc-stategraph-scope-0001",
            seed_entity_ids=("ent-stategraph-service-a0001",),
            window=WINDOW,
            domains=("web-service",),
            correlation_keys={"request_id": "request-17"},
            relation_types=("DEPENDS_ON",),
            max_entities=25,
            max_depth=3,
        )

        validate_contract("investigation-scope.schema.json", scope.to_contract())
        self.assertEqual(scope.to_contract()["correlation_keys"]["request_id"], "request-17")

    def test_scope_rejects_an_inverted_time_window(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "must not follow"):
            InvestigationScope(
                incident_id="inc-stategraph-scope-0001",
                seed_entity_ids=("ent-stategraph-service-a0001",),
                window=EvidenceWindow(
                    start="2026-08-12T01:10:00Z",
                    end="2026-08-12T01:00:00Z",
                ),
            )


class StateGraphRepositoryTests(unittest.TestCase):
    def test_in_memory_adapter_satisfies_the_repository_port(self) -> None:
        self.assertIsInstance(InMemoryStateGraphRepository(), StateGraphRepository)

    def test_entity_id_must_be_derived_from_versioned_identity(self) -> None:
        record = graph_entity(
            graph_entity_id("checkout-api"),
            name="checkout-api",
            evidence_id="ev-stategraph-identity-0001",
        )
        record["entity_id"] = "ent-stategraph-mismatched-0001"

        with self.assertRaisesRegex(ContractViolation, "does not match EntityIdentity"):
            InMemoryStateGraphRepository().upsert_entity(record)

    def test_graph_storage_rejects_content_that_still_needs_redaction(self) -> None:
        repository = InMemoryStateGraphRepository()
        entity_id = graph_entity_id("checkout-api")
        repository.upsert_entity(
            graph_entity(
                entity_id,
                name="checkout-api",
                evidence_id="ev-stategraph-identity-0001",
            )
        )

        with self.assertRaisesRegex(ContractViolation, "must be redacted"):
            repository.append_or_extend_snapshot(
                graph_snapshot(
                    entity_id,
                    state={"token": "[REDACTED]"},
                    observed_at="2026-08-12T01:01:00Z",
                    evidence_id="ev-stategraph-state-a001",
                )
            )

    def test_consecutive_equal_snapshots_merge_then_changed_state_closes_interval(self) -> None:
        repository = InMemoryStateGraphRepository()
        entity_id = graph_entity_id("checkout-api")
        repository.upsert_entity(
            graph_entity(
                entity_id,
                name="checkout-api",
                evidence_id="ev-stategraph-identity-0001",
            )
        )
        first = graph_snapshot(
            entity_id,
            state={"health": "degraded"},
            observed_at="2026-08-12T01:01:00Z",
            evidence_id="ev-stategraph-state-a001",
        )
        repeated = graph_snapshot(
            entity_id,
            state={"health": "degraded"},
            observed_at="2026-08-12T01:03:00Z",
            evidence_id="ev-stategraph-state-a002",
        )
        changed = graph_snapshot(
            entity_id,
            state={"health": "healthy"},
            observed_at="2026-08-12T01:05:00Z",
            evidence_id="ev-stategraph-state-a003",
        )

        repository.append_or_extend_snapshot(first)
        repository.append_or_extend_snapshot(repeated)
        merged = repository.list_snapshots(entity_id)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["observed_at"], "2026-08-12T01:03:00Z")
        self.assertEqual(len(merged[0]["evidence_ids"]), 2)

        repository.append_or_extend_snapshot(changed)
        history = repository.list_snapshots(entity_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["valid_to"], "2026-08-12T01:05:00Z")
        self.assertIsNone(history[1]["valid_to"])

    def test_relation_observation_extends_and_explicit_disappearance_closes_it(self) -> None:
        repository = InMemoryStateGraphRepository()
        source_id = graph_entity_id("checkout-api")
        destination_id = graph_entity_id("orders-db")
        repository.upsert_entity(
            graph_entity(
                source_id,
                name="checkout-api",
                evidence_id="ev-stategraph-relation-0001",
            )
        )
        repository.upsert_entity(
            graph_entity(
                destination_id,
                name="orders-db",
                evidence_id="ev-stategraph-relation-0002",
            )
        )
        identity = {
            "source_entity_id": source_id,
            "relation_type": "WRITES_TO",
            "destination_entity_id": destination_id,
            "reference_key": "application-config",
            "projector": "web-service-projector",
        }
        relation_key = stable_graph_id("relkey", identity)
        first = {
            "record_type": "relation_interval",
            "relation_id": stable_graph_id("rel", {"key": relation_key, "at": "01:01"}),
            "relation_key": relation_key,
            **identity,
            "observed_at": "2026-08-12T01:01:00Z",
            "valid_from": "2026-08-12T01:01:00Z",
            "valid_to": None,
            "evidence_ids": ["ev-stategraph-relation-0001"],
        }
        repeated = copy.deepcopy(first)
        repeated["relation_id"] = stable_graph_id(
            "rel", {"key": relation_key, "at": "01:03"}
        )
        repeated["observed_at"] = "2026-08-12T01:03:00Z"
        repeated["valid_from"] = "2026-08-12T01:03:00Z"
        repeated["evidence_ids"] = ["ev-stategraph-relation-0003"]

        repository.append_or_extend_relation(first)
        repository.append_or_extend_relation(repeated)
        self.assertEqual(len(repository.list_relations()), 1)
        self.assertEqual(
            repository.list_relations()[0]["observed_at"],
            "2026-08-12T01:03:00Z",
        )

        repository.close_relation(
            relation_key,
            observed_at=datetime(2026, 8, 12, 1, 4, tzinfo=UTC),
        )
        self.assertEqual(
            repository.list_relations()[0]["valid_to"],
            "2026-08-12T01:04:00Z",
        )


class KubernetesProjectorAndLocalizerTests(unittest.TestCase):
    def test_projector_uses_normalized_evidence_and_creates_a_reference_path(self) -> None:
        event, resource = kubernetes_evidence()
        projector = KubernetesEvidenceProjector()
        repository = InMemoryStateGraphRepository()

        event_projection = projector.project(event)
        resource_projection = projector.project(resource)
        repository.ingest(event_projection.records)
        repository.ingest(resource_projection.records)

        relations = repository.list_relations()
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["relation_type"], "REFERENCES")
        self.assertIn(event["evidence_id"], relations[0]["evidence_ids"])
        self.assertNotIn("message", repository.list_events()[0]["attributes"])

    def test_localizer_freezes_only_bounded_graph_evidence(self) -> None:
        event, resource = kubernetes_evidence()
        projector = KubernetesEvidenceProjector()
        repository = InMemoryStateGraphRepository()
        event_projection = projector.project(event)
        repository.ingest(event_projection.records)
        repository.ingest(projector.project(resource).records)
        pod_entity = next(
            record
            for record in event_projection.records
            if record["record_type"] == "entity"
            and record["entity_type"] == "Pod"
        )
        scope = InvestigationScope(
            incident_id=event["incident_id"],
            seed_entity_ids=(pod_entity["entity_id"],),
            window=WINDOW,
            domains=("kubernetes",),
            relation_types=("REFERENCES",),
            max_entities=2,
            max_depth=1,
        )

        context = GraphLocalizer(repository).build_context(
            scope,
            (event, resource),
            frozen_at=datetime(2026, 8, 12, 1, 7, tzinfo=UTC),
        )

        validate_contract("context-package.schema.json", context)
        self.assertEqual(context["localization"]["strategy"], "stategraph")
        self.assertEqual(context["localization"]["candidate_entities_after"], 2)
        self.assertEqual(context["scope"]["max_depth"], 1)
        self.assertTrue(
            any(path["relations"] == ["REFERENCES"] for path in context["state_paths"])
        )
        self.assertEqual(set(context["evidence_ids"]), {event["evidence_id"], resource["evidence_id"]})

    def test_localizer_rejects_graph_evidence_from_another_incident(self) -> None:
        event, _ = kubernetes_evidence()
        projector = KubernetesEvidenceProjector()
        repository = InMemoryStateGraphRepository()
        projection = projector.project(event)
        repository.ingest(projection.records)
        pod = next(
            record
            for record in projection.records
            if record["record_type"] == "entity" and record["entity_type"] == "Pod"
        )
        scope = InvestigationScope(
            incident_id="inc-different-incident-0001",
            seed_entity_ids=(pod["entity_id"],),
            window=WINDOW,
            domains=("kubernetes",),
        )

        with self.assertRaisesRegex(ContractViolation, "different Incident"):
            GraphLocalizer(repository).build_context(
                scope,
                (event,),
                frozen_at=datetime(2026, 8, 12, 1, 7, tzinfo=UTC),
            )


if __name__ == "__main__":
    unittest.main()
