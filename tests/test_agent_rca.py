from __future__ import annotations

import copy
import hashlib
import json
import logging
import unittest
from datetime import datetime, timezone

from agents import MaxTurnsExceeded

from incident_platform.agent_rca import (
    AgentInvestigationView,
    AgentInvocation,
    AgentModelRun,
    AgentRCAPolicy,
    AgentRCAService,
    AgentToolRuntime,
    EvidenceCandidateSelector,
    OpenAIAgentsSDKRunner,
    _agent_input,
    inspect_evidence,
)
from incident_platform.evidence import (
    CollectionRequest,
    EvidenceBuilder,
    EvidenceDraft,
    EvidenceWindow,
    ResourceScope,
)
from incident_platform.errors import ContractViolation
from incident_platform.incidents import AlertmanagerNormalizer
from incident_platform.knowledge import (
    BoundedKnowledgeRetriever,
    ReferenceDocument,
)
from incident_platform.repository import InMemoryIncidentRepository


UTC = timezone.utc
NOW = datetime(2026, 8, 22, 2, 0, tzinfo=UTC)
ENTITY_ID = "ent-checkoutservice-pod-0001"


class StaticKnowledgeRepository:
    def __init__(self) -> None:
        content = "Kubernetes kubernetes-event prometheus checkoutservice failure triage"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._documents = (
            ReferenceDocument(
                {
                    "schema_version": "1.0.0",
                    "reference_document_id": "ref-agent-runbook-document-0001",
                    "document_type": "runbook",
                    "title": "Checkout Kubernetes triage",
                    "source_class": "operational-knowledge",
                    "source_kind": "git-path",
                    "source_path_or_uri": "documents/agent-test.md",
                    "version": "1.0.0",
                    "valid_from": "2026-08-01T00:00:00Z",
                    "valid_to": None,
                    "entity_keys": ["domain:kubernetes"],
                    "content_hash": f"sha256:{digest}",
                    "review_status": "approved",
                    "sensitivity": "internal",
                },
                content,
            ),
        )

    def list_documents(self, *, limit: int):
        return self._documents


class SuccessfulFakeRunner:
    model_name = "fake-agent-model"

    def __init__(
        self,
        *,
        inspect_second: bool = True,
        invented: bool = False,
        single_evidence: bool = False,
        exhaust_tool_budget: bool = False,
    ):
        self.inspect_second = inspect_second
        self.invented = invented
        self.single_evidence = single_evidence
        self.exhaust_tool_budget = exhaust_tool_budget

    def run(self, invocation: AgentInvocation) -> AgentModelRun:
        evidence_ids = [item["evidence_id"] for item in invocation.evidence]
        invocation.tool_runtime.inspect_evidence(evidence_ids[0])
        if self.inspect_second:
            invocation.tool_runtime.inspect_evidence(evidence_ids[1])
        reference_id = invocation.references[0]["reference_document_id"]
        invocation.tool_runtime.inspect_reference(reference_id)
        if self.exhaust_tool_budget:
            for _ in range(invocation.policy.max_tool_calls):
                invocation.tool_runtime.inspect_evidence(evidence_ids[0])
        cited_second = (
            "ev-invented-evidence-0001" if self.invented else evidence_ids[1]
        )
        supporting_ids = (
            [evidence_ids[0]]
            if self.single_evidence
            else [evidence_ids[0], cited_second]
        )
        draft = {
            "schema_version": "1.0.0",
            "incident_id": invocation.context["incident_id"],
            "context_id": invocation.context["context_id"],
            "decision": "CONCLUSIVE",
            "root_cause": {
                "summary": "The workload failure is supported by runtime state and metrics.",
                "entity_id": ENTITY_ID,
                "supporting_evidence_ids": supporting_ids,
                "contradicting_evidence_ids": [],
                "reference_document_ids": [reference_id],
            },
            "hypotheses": [
                {
                    "rank": 1,
                    "summary": "Runtime state and metrics identify the same failure.",
                    "entity_id": ENTITY_ID,
                    "confidence": 0.9,
                    "status": "supported",
                    "supporting_evidence_ids": supporting_ids,
                    "contradicting_evidence_ids": [],
                    "reference_document_ids": [reference_id],
                    "missing_evidence": [],
                }
            ],
            "remediation": {
                "suggestions": ["Apply an operator-reviewed change through source control."],
                "verification_conditions": ["The workload becomes Ready and the alert resolves."],
            },
            "limitations": ["The Agent used a frozen, bounded Context."],
            "read_only": True,
        }
        return AgentModelRun(draft, 3, 900, 240, 1140)


class FailingFakeRunner:
    model_name = "fake-agent-model"

    def run(self, invocation: AgentInvocation) -> AgentModelRun:
        raise RuntimeError("model unavailable")


class MaxTurnsFakeRunner:
    model_name = "fake-agent-model"

    def run(self, invocation: AgentInvocation) -> AgentModelRun:
        invocation.tool_runtime.inspect_evidence(invocation.evidence[0]["evidence_id"])
        raise MaxTurnsExceeded("Maximum turns (6) exceeded")


class UnsupportedRemediationFakeRunner(SuccessfulFakeRunner):
    """Return advice without an accepted root cause, as a model may attempt."""

    def run(self, invocation: AgentInvocation) -> AgentModelRun:
        model_run = super().run(invocation)
        draft = dict(model_run.draft)
        draft["decision"] = "INCONCLUSIVE"
        draft["root_cause"] = None
        return AgentModelRun(
            draft,
            model_run.llm_calls,
            model_run.input_tokens,
            model_run.output_tokens,
            model_run.total_tokens,
        )


def prepared_repository() -> tuple[InMemoryIncidentRepository, str, str]:
    incident = AlertmanagerNormalizer().normalize(
        {
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "AgentRCAFixture",
                        "namespace": "online-boutique",
                        "service": "checkoutservice",
                        "severity": "critical",
                    },
                    "annotations": {},
                    "startsAt": "2026-08-22T01:50:00Z",
                    "endsAt": "0001-01-01T00:00:00Z",
                    "fingerprint": "agent-rca-fixture-0001",
                }
            ]
        },
        received_at=NOW,
    )[0].incident
    incident_id = incident["incident_id"]
    repository = InMemoryIncidentRepository()
    repository.create_or_get_by_deduplication_key(incident, occurred_at=NOW)
    repository.transition(
        incident_id,
        expected_status="RECEIVED",
        next_status="COLLECTING",
        occurred_at=NOW,
    )
    repository.replace_collector_statuses(
        incident_id,
        [
            {
                "collector": "kubernetes",
                "status": "SUCCEEDED",
                "attempts": 1,
                "started_at": "2026-08-22T01:58:00Z",
                "ended_at": "2026-08-22T01:58:01Z",
                "error": None,
            },
            {
                "collector": "prometheus",
                "status": "SUCCEEDED",
                "attempts": 1,
                "started_at": "2026-08-22T01:58:00Z",
                "ended_at": "2026-08-22T01:58:01Z",
                "error": None,
            },
        ],
        occurred_at=NOW,
    )
    request = CollectionRequest(
        request_id="req-agent-rca-fixture-0001",
        incident_id=incident_id,
        window=EvidenceWindow(
            start="2026-08-22T01:45:00Z", end="2026-08-22T01:59:00Z"
        ),
        scope=ResourceScope(
            namespace="online-boutique", resource_names=("checkoutservice",)
        ),
        timeout_seconds=1.0,
    )
    subject = {
        "cluster_id": "gcp-dev-01",
        "api_version": "v1",
        "kind": "Pod",
        "namespace": "online-boutique",
        "name": "checkoutservice",
        "uid": "pod-checkoutservice-0001",
        "exists": True,
    }
    drafts = (
        EvidenceDraft(
            source="kubernetes",
            kind="kubernetes-event",
            observed_at="2026-08-22T01:58:30Z",
            subject=subject,
            summary="The checkout Pod repeatedly failed readiness.",
            facts={"reason": "Unhealthy", "count": 8},
            provider="kubernetes-test",
            query="events for checkoutservice",
            locator="kubernetes://gcp-dev-01/online-boutique/checkoutservice/events",
        ),
        EvidenceDraft(
            source="prometheus",
            kind="metric-summary",
            observed_at="2026-08-22T01:58:30Z",
            subject=subject,
            summary="Checkout request error ratio reached 0.42.",
            facts={"metric": "request_error_ratio", "peak": 0.42},
            provider="prometheus-test",
            query="request_error_ratio{service=checkoutservice}",
            locator="prometheus://gcp-dev-01/checkoutservice/error-ratio",
        ),
    )
    evidence = [
        EvidenceBuilder().build(item, request, collected_at=NOW) for item in drafts
    ]
    repository.store_evidence(incident_id, evidence)
    repository.transition(
        incident_id,
        expected_status="COLLECTING",
        next_status="LOCALIZING",
        occurred_at=NOW,
    )
    entity = {
        "entity_id": ENTITY_ID,
        "entity_type": "Pod",
        "domain": "kubernetes",
        "name": "checkoutservice",
        "scope": {"cluster_id": "gcp-dev-01", "namespace": "online-boutique"},
        "external_ref": "k8s://online-boutique/Pod/checkoutservice",
        "exists": True,
    }
    context = {
        "schema_version": "1.0.0",
        "context_id": "ctx-agent-rca-fixture-0001",
        "incident_id": incident_id,
        "frozen_at": "2026-08-22T01:59:00Z",
        "source_entity": entity,
        "scope": {
            "incident_id": incident_id,
            "seed_entity_ids": [ENTITY_ID],
            "domains": ["kubernetes"],
            "correlation_keys": {},
            "relation_types": ["RUNS_ON"],
            "time_window": {
                "start": "2026-08-22T01:45:00Z",
                "end": "2026-08-22T01:59:00Z",
            },
            "max_entities": 1,
            "max_depth": 1,
        },
        "state_paths": [
            {
                "path_id": "path-agent-rca-fixture-0001",
                "entities": [entity],
                "relations": [],
                "evidence_ids": [item["evidence_id"] for item in evidence],
            }
        ],
        "evidence_ids": [item["evidence_id"] for item in evidence],
        "recent_change_evidence_ids": [],
        "missing_evidence": [],
        "collector_failures": [],
        "localization": {
            "strategy": "stategraph",
            "candidate_entities_before": 1,
            "candidate_entities_after": 1,
            "context_completeness": 1.0,
        },
    }
    repository.store_context(context)
    repository.transition(
        incident_id,
        expected_status="LOCALIZING",
        next_status="ANALYZING",
        occurred_at=NOW,
    )
    return repository, incident_id, context["context_id"]


def service(repository, runner):
    return AgentRCAService(
        repository,
        BoundedKnowledgeRetriever(
            StaticKnowledgeRepository(), utc_now=lambda: NOW
        ),
        runner,
    )


class AgentRCAServiceTests(unittest.TestCase):
    def test_sdk_runner_disables_context_bearing_failure_logs(self) -> None:
        OpenAIAgentsSDKRunner("fake-agent-model")

        self.assertTrue(logging.getLogger("openai.agents").disabled)

    def test_evidence_tool_accepts_only_a_bounded_batch(self) -> None:
        evidence_ids = inspect_evidence.params_json_schema["properties"][
            "evidence_ids"
        ]

        self.assertEqual(evidence_ids["type"], "array")
        self.assertEqual(evidence_ids["minItems"], 1)
        self.assertEqual(evidence_ids["maxItems"], 4)

    def test_real_agent_boundary_persists_a_gated_report_and_audit(self) -> None:
        repository, incident_id, context_id = prepared_repository()

        run = service(repository, SuccessfulFakeRunner()).run(
            incident_id, context_id=context_id, generated_at=NOW
        )

        self.assertEqual(run.incident["status"], "REPORTED")
        self.assertEqual(run.report["status"], "conclusive")
        self.assertEqual(run.audit["status"], "SUCCEEDED")
        self.assertEqual(len(run.audit["candidate_evidence_ids"]), 2)
        self.assertEqual(run.audit["budget"]["max_evidence_candidates"], 8)
        self.assertEqual(run.audit["input_projection"]["candidate_evidence"], 2)
        self.assertEqual(len(run.audit["inspected_evidence_ids"]), 2)
        self.assertEqual(len(run.audit["inspected_reference_document_ids"]), 1)
        self.assertEqual(
            run.report["root_cause"]["reference_document_ids"],
            ["ref-agent-runbook-document-0001"],
        )
        self.assertEqual(
            repository.get_agent_run(run.audit["agent_run_id"]), run.audit
        )

    def test_evidence_gate_rejects_an_invented_evidence_id(self) -> None:
        repository, incident_id, context_id = prepared_repository()

        with self.assertRaisesRegex(ContractViolation, "outside frozen Context"):
            service(repository, SuccessfulFakeRunner(invented=True)).run(
                incident_id, context_id=context_id, generated_at=NOW
            )

        self.assertEqual(repository.get(incident_id)["status"], "FAILED")

    def test_evidence_gate_requires_tool_inspection_before_citation(self) -> None:
        repository, incident_id, context_id = prepared_repository()

        with self.assertRaisesRegex(ContractViolation, "without tool inspection"):
            service(repository, SuccessfulFakeRunner(inspect_second=False)).run(
                incident_id, context_id=context_id, generated_at=NOW
            )

        self.assertEqual(repository.get(incident_id)["status"], "FAILED")

    def test_model_failure_is_audited_and_marks_incident_failed(self) -> None:
        repository, incident_id, context_id = prepared_repository()

        with self.assertRaisesRegex(RuntimeError, "model unavailable"):
            service(repository, FailingFakeRunner()).run(
                incident_id, context_id=context_id, generated_at=NOW
            )

        self.assertEqual(repository.get(incident_id)["status"], "FAILED")

    def test_max_turns_failure_is_audited_as_budget_exhaustion(self) -> None:
        repository, incident_id, context_id = prepared_repository()

        with self.assertRaises(MaxTurnsExceeded):
            service(repository, MaxTurnsFakeRunner()).run(
                incident_id, context_id=context_id, generated_at=NOW
            )

        audit = repository.query_agent_runs(incident_id, limit=1)[0]
        self.assertEqual(audit["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(audit["reason_code"], "MODEL_BUDGET_EXCEEDED")
        self.assertEqual(audit["usage"]["tool_calls"], 1)
        self.assertEqual(repository.get(incident_id)["status"], "FAILED")

    def test_conclusive_result_requires_distinct_evidence_sources(self) -> None:
        repository, incident_id, context_id = prepared_repository()

        with self.assertRaisesRegex(ContractViolation, "distinct Evidence sources"):
            service(repository, SuccessfulFakeRunner(single_evidence=True)).run(
                incident_id, context_id=context_id, generated_at=NOW
            )

        self.assertEqual(repository.get(incident_id)["status"], "FAILED")

    def test_rootless_result_cannot_recommend_remediation(self) -> None:
        repository, incident_id, context_id = prepared_repository()

        with self.assertRaises(ContractViolation):
            service(repository, UnsupportedRemediationFakeRunner()).run(
                incident_id, context_id=context_id, generated_at=NOW
            )

        self.assertEqual(repository.get(incident_id)["status"], "FAILED")

    def test_tool_attempts_beyond_the_budget_are_rejected(self) -> None:
        repository, incident_id, context_id = prepared_repository()

        with self.assertRaisesRegex(ContractViolation, "investigation budget"):
            service(repository, SuccessfulFakeRunner(exhaust_tool_budget=True)).run(
                incident_id, context_id=context_id, generated_at=NOW
            )

        self.assertEqual(repository.get(incident_id)["status"], "FAILED")

    def test_non_analyzing_incident_cannot_skip_the_lifecycle(self) -> None:
        repository, incident_id, context_id = prepared_repository()
        repository.transition(
            incident_id,
            expected_status="ANALYZING",
            next_status="FAILED",
            occurred_at=NOW,
        )

        with self.assertRaisesRegex(Exception, "requires ANALYZING"):
            service(repository, SuccessfulFakeRunner()).run(
                incident_id, context_id=context_id, generated_at=NOW
            )

    def test_candidate_selector_is_deterministic_and_preserves_source_diversity(
        self,
    ) -> None:
        context = {
            "source_entity": {
                "entity_id": ENTITY_ID,
                "entity_type": "Pod",
                "domain": "kubernetes",
                "name": "checkoutservice",
                "scope": {
                    "cluster_id": "gcp-dev-01",
                    "namespace": "online-boutique",
                },
            },
            "evidence_ids": [f"ev-candidate-{index:04d}" for index in range(1, 10)],
            "recent_change_evidence_ids": ["ev-candidate-0009"],
            "state_paths": [],
        }
        sources_and_kinds = (
            ("kubernetes", "resource-state"),
            ("kubernetes", "kubernetes-event"),
            ("prometheus", "metric-summary"),
            ("loki", "log-pattern"),
            ("deployment", "deployment-change"),
            ("trace", "trace-summary"),
            ("network", "network-flow-summary"),
            ("hubble", "network-flow-summary"),
            ("deployment", "state-diff"),
        )
        evidence = []
        for index, (source, kind) in enumerate(sources_and_kinds, start=1):
            evidence.append(
                {
                    "evidence_id": f"ev-candidate-{index:04d}",
                    "source": source,
                    "kind": kind,
                    "observed_at": "2026-08-22T01:58:30Z",
                    "subject": {
                        "cluster_id": "gcp-dev-01",
                        "kind": "Pod",
                        "namespace": "online-boutique",
                        "name": "checkoutservice",
                    },
                    "summary": f"candidate {index}",
                    "quality": {
                        "freshness": "recent",
                        "completeness": 1.0,
                        "confidence": 0.9,
                    },
                }
            )
        selector = EvidenceCandidateSelector()

        selected = selector.select(context, evidence, max_candidates=4)
        selected_reversed = selector.select(
            context, list(reversed(evidence)), max_candidates=4
        )

        self.assertEqual(selected, selected_reversed)
        self.assertEqual(selected[0], "ev-candidate-0009")
        self.assertEqual(len(selected), 4)
        selected_sources = {
            next(item["source"] for item in evidence if item["evidence_id"] == item_id)
            for item_id in selected
        }
        self.assertEqual(len(selected_sources), 4)

    def test_investigation_view_deduplicates_paths_and_omits_full_payloads(
        self,
    ) -> None:
        repository, incident_id, context_id = prepared_repository()
        context = repository.get_context(context_id)
        evidence = repository.list_evidence(incident_id)
        repeated = copy.deepcopy(context["state_paths"][0])
        context["state_paths"] = []
        for index in range(42):
            path = copy.deepcopy(repeated)
            path["path_id"] = f"path-repeated-{index:04d}"
            context["state_paths"].append(path)
        candidate_ids = EvidenceCandidateSelector().select(
            context, evidence, max_candidates=8
        )

        view = AgentInvestigationView.build(context, evidence, candidate_ids)
        candidate_set = set(candidate_ids)
        invocation = AgentInvocation(
            context=context,
            evidence=tuple(evidence),
            references=(),
            tool_runtime=AgentToolRuntime(
                context_evidence_ids=frozenset(candidate_ids),
                evidence_by_id={
                    item["evidence_id"]: item
                    for item in evidence
                    if item["evidence_id"] in candidate_set
                },
                reference_by_id={},
                max_tool_calls=12,
            ),
            policy=AgentRCAPolicy(),
            investigation_view=view,
        )
        serialized = json.dumps(view.package, sort_keys=True)
        model_input = _agent_input(invocation)
        legacy = json.dumps(
            {"frozen_context": context, "evidence_catalog": evidence},
            sort_keys=True,
        )

        self.assertEqual(view.total_state_paths, 42)
        self.assertEqual(view.included_state_paths, 1)
        self.assertLess(view.serialized_bytes, len(legacy.encode("utf-8")) // 2)
        self.assertIn('"investigation_view":', model_input)
        self.assertNotIn('"frozen_context":', model_input)
        self.assertNotIn('"facts"', serialized)
        self.assertNotIn('"provenance"', serialized)

    def test_tool_runtime_denies_evidence_outside_the_candidate_catalog(self) -> None:
        runtime = AgentToolRuntime(
            context_evidence_ids=frozenset({"ev-candidate-0001"}),
            evidence_by_id={
                "ev-candidate-0001": {"evidence_id": "ev-candidate-0001"},
                "ev-candidate-0002": {"evidence_id": "ev-candidate-0002"},
            },
            reference_by_id={},
            max_tool_calls=2,
        )

        denied = json.loads(runtime.inspect_evidence("ev-candidate-0002"))

        self.assertEqual(denied["status"], "DENIED")
        self.assertNotIn("ev-candidate-0002", runtime.inspected_evidence_ids)

    def test_candidate_budget_must_support_cross_source_conclusions(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 2 and 12"):
            AgentRCAPolicy(max_evidence_candidates=1)
        with self.assertRaisesRegex(ValueError, "candidate budget"):
            AgentRCAPolicy(
                max_evidence_candidates=2,
                minimum_conclusive_evidence_sources=3,
            )


if __name__ == "__main__":
    unittest.main()
