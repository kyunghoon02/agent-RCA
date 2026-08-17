"""Schema-valid Fast Path RCA artifacts built only from collected Evidence."""

from __future__ import annotations

import copy
import hashlib
import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .contracts import validate_contract
from .deterministic import DeterministicDecision, RuleEvaluation
from .errors import ContractViolation


_FAILED_COLLECTOR_STATUSES = frozenset({"PARTIAL", "FAILED", "TIMED_OUT"})

_REMEDIATION_CATALOG = {
    "kubernetes.container-oomkilled": {
        "suggestions": (
            "Review the workload memory request and limit, then apply an approved "
            "change through the deployment source of truth.",
        ),
        "verification_conditions": (
            "The affected Pod remains Ready without another OOMKilled termination.",
            "Memory working set remains below the declared threshold during the recovery window.",
            "The originating alert resolves.",
        ),
    },
    "kubernetes.image-pull-failure": {
        "suggestions": (
            "Correct the image reference or registry access through the deployment "
            "source of truth.",
        ),
        "verification_conditions": (
            "The configured image can be pulled by the affected node.",
            "The affected workload rollout becomes Ready.",
            "The originating alert resolves.",
        ),
    },
    "kubernetes.missing-configmap": {
        "suggestions": (
            "Restore the reviewed ConfigMap through the deployment source of truth.",
        ),
        "verification_conditions": (
            "The required ConfigMap exists in the affected namespace.",
            "The affected workload becomes Ready.",
            "The originating alert resolves.",
        ),
    },
}


@dataclass(frozen=True)
class FastPathArtifacts:
    """One immutable Context Package plus its JSON and Markdown RCA report."""

    context: Dict[str, Any]
    report: Dict[str, Any]
    markdown: str


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _stable_id(prefix: str, value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _unique(values: Sequence[str]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _collector_failures(incident: Mapping[str, Any]) -> Tuple[Dict[str, str], ...]:
    failures = []
    for status in incident.get("collector_statuses", []):
        if status.get("status") not in _FAILED_COLLECTOR_STATUSES:
            continue
        error = status.get("error") or f"collector ended as {status.get('status')}"
        failures.append({"collector": status["collector"], "error": error})
    return tuple(failures)


def _entity_for_ids(
    evidence_by_id: Mapping[str, Mapping[str, Any]],
    evidence_ids: Sequence[str],
    fallback: Mapping[str, Any],
) -> Dict[str, Any]:
    for evidence_id in evidence_ids:
        item = evidence_by_id.get(evidence_id)
        if item is not None:
            return copy.deepcopy(dict(item["subject"]))
    return copy.deepcopy(dict(fallback))


def _evaluation_evidence_ids(evaluation: RuleEvaluation) -> Tuple[str, ...]:
    return _unique(tuple(evaluation.supporting_evidence_ids))


class FastPathReportBuilder:
    """Build a deterministic report without LLM or additional tool calls.

    The builder freezes all provided Evidence into a namespace-fallback Context
    Package. Every Evidence reference in the report is checked against that
    package before the artifacts are returned.
    """

    def build(
        self,
        *,
        incident: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]],
        decision: DeterministicDecision,
        generated_at: Optional[datetime] = None,
    ) -> FastPathArtifacts:
        frozen_at = generated_at or datetime.now(timezone.utc)
        timestamp = _format_time(frozen_at)
        incident_copy = copy.deepcopy(dict(incident))
        validate_contract("incident.schema.json", incident_copy)
        if decision.status not in {"PROVEN", "ABSTAIN", "AMBIGUOUS"}:
            raise ContractViolation(
                f"unsupported deterministic decision status: {decision.status}"
            )

        evidence_by_id = self._validate_evidence(
            incident_copy["incident_id"], evidence
        )
        self._validate_decision_references(decision, evidence_by_id)

        context = self._build_context(
            incident_copy,
            tuple(evidence_by_id.values()),
            decision,
            timestamp,
        )
        report = self._build_report(
            incident_copy,
            context,
            evidence_by_id,
            decision,
            timestamp,
        )
        self._validate_cross_references(context, report)
        return FastPathArtifacts(
            context=copy.deepcopy(context),
            report=copy.deepcopy(report),
            markdown=render_markdown(report),
        )

    @staticmethod
    def _validate_evidence(
        incident_id: str,
        evidence: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        if not evidence:
            raise ContractViolation("Fast Path report requires at least one Evidence item")
        by_id: Dict[str, Dict[str, Any]] = {}
        for item in evidence:
            candidate = copy.deepcopy(dict(item))
            validate_contract("evidence-item.schema.json", candidate)
            if candidate["incident_id"] != incident_id:
                raise ContractViolation(
                    "Evidence incident_id does not match Fast Path Incident"
                )
            evidence_id = candidate["evidence_id"]
            previous = by_id.get(evidence_id)
            if previous is not None and previous != candidate:
                raise ContractViolation(
                    f"duplicate evidence_id has conflicting content: {evidence_id}"
                )
            by_id[evidence_id] = candidate
        return by_id

    @staticmethod
    def _validate_decision_references(
        decision: DeterministicDecision,
        evidence_by_id: Mapping[str, Mapping[str, Any]],
    ) -> None:
        referenced = list(decision.supporting_evidence_ids)
        for evaluation in decision.evaluations:
            referenced.extend(evaluation.supporting_evidence_ids)
        unknown = sorted(set(referenced) - set(evidence_by_id))
        if unknown:
            raise ContractViolation(
                f"deterministic decision references unknown Evidence: {unknown}"
            )

    @staticmethod
    def _build_context(
        incident: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]],
        decision: DeterministicDecision,
        timestamp: str,
    ) -> Dict[str, Any]:
        source_entity = copy.deepcopy(dict(incident["source_entity"]))
        subjects = [source_entity] + [dict(item["subject"]) for item in evidence]
        canonical_subjects = {
            json.dumps(subject, sort_keys=True, separators=(",", ":")): subject
            for subject in subjects
        }
        namespaces = _unique(
            tuple(
                subject["namespace"]
                for subject in canonical_subjects.values()
                if subject.get("namespace")
            )
        )
        if not namespaces:
            raise ContractViolation("Fast Path context requires a namespace")
        entity_uids = _unique(
            tuple(
                subject["uid"]
                for subject in canonical_subjects.values()
                if subject.get("uid")
            )
        )
        evidence_ids = tuple(item["evidence_id"] for item in evidence)
        failures = _collector_failures(incident)
        statuses = incident.get("collector_statuses", [])
        succeeded = sum(
            status.get("status") == "SUCCEEDED" for status in statuses
        )
        partial = sum(status.get("status") == "PARTIAL" for status in statuses)
        completeness = (
            (succeeded + (0.5 * partial)) / len(statuses) if statuses else 1.0
        )
        end = (
            incident["window"].get("recovery_end")
            or incident["window"].get("incident_end")
            or timestamp
        )
        context_identity = {
            "incident_id": incident["incident_id"],
            "frozen_at": timestamp,
            "evidence_ids": evidence_ids,
            "collector_failures": failures,
        }
        context = {
            "schema_version": "1.0.0",
            "context_id": _stable_id("ctx", context_identity),
            "incident_id": incident["incident_id"],
            "frozen_at": timestamp,
            "source_entity": source_entity,
            "scope": {
                "namespaces": list(namespaces),
                "entity_uids": list(entity_uids),
                "metapaths": [],
                "time_window": {
                    "start": incident["window"]["baseline_start"],
                    "end": end,
                },
                "max_entities": max(1, len(canonical_subjects)),
            },
            "state_paths": [],
            "evidence_ids": list(evidence_ids),
            "recent_change_evidence_ids": [],
            "missing_evidence": [
                {"source": "deterministic-rule", "reason": requirement}
                for requirement in decision.missing_requirements
            ],
            "collector_failures": list(failures),
            "localization": {
                "strategy": "namespace-fallback",
                "candidate_entities_before": len(canonical_subjects),
                "candidate_entities_after": len(canonical_subjects),
                "context_completeness": completeness,
            },
        }
        validate_contract("context-package.schema.json", context)
        return context

    def _build_report(
        self,
        incident: Mapping[str, Any],
        context: Mapping[str, Any],
        evidence_by_id: Mapping[str, Mapping[str, Any]],
        decision: DeterministicDecision,
        timestamp: str,
    ) -> Dict[str, Any]:
        hypotheses = self._build_hypotheses(
            incident, evidence_by_id, decision
        )
        root_cause = None
        if decision.status == "PROVEN":
            assert decision.statement is not None
            root_cause = {
                "summary": decision.statement,
                "entity": _entity_for_ids(
                    evidence_by_id,
                    decision.supporting_evidence_ids,
                    incident["source_entity"],
                ),
                "supporting_evidence_ids": list(
                    _unique(decision.supporting_evidence_ids)
                ),
            }

        failures = context["collector_failures"]
        if failures:
            status = "partial"
        elif decision.status == "PROVEN":
            status = "conclusive"
        else:
            status = "inconclusive"

        remediation = self._remediation(decision)
        limitations = [
            "This Fast Path report used deterministic rules and made no LLM calls.",
            "Confidence values encode deterministic rule status and are not calibrated probabilities.",
            "Remediation is advisory; the read-only platform does not modify Kubernetes resources.",
        ]
        limitations.extend(
            f"Collector {failure['collector']} was incomplete: {failure['error']}"
            for failure in failures
        )
        if decision.status == "ABSTAIN":
            limitations.append(
                "The available Evidence did not satisfy a complete deterministic rule."
            )
        elif decision.status == "AMBIGUOUS":
            limitations.append(
                "Multiple deterministic rules were proven; no single root cause was selected."
            )

        report_identity = {
            "incident_id": incident["incident_id"],
            "context_id": context["context_id"],
            "decision_status": decision.status,
            "root_cause_id": decision.root_cause_id,
            "evidence_ids": context["evidence_ids"],
        }
        report = {
            "schema_version": "1.0.0",
            "report_id": _stable_id("rpt", report_identity),
            "incident_id": incident["incident_id"],
            "context_id": context["context_id"],
            "path": "fast",
            "status": status,
            "generated_at": timestamp,
            "root_cause": root_cause,
            "hypotheses": hypotheses,
            "remediation": remediation,
            "budget": {
                "applicable": False,
                "llm_calls": 0,
                "tool_calls": 0,
                "tree_depth": 0,
                "wall_time_ms": 0,
                "exhausted": False,
            },
            "read_only": True,
            "limitations": limitations,
        }
        validate_contract("rca-report.schema.json", report)
        return report

    @staticmethod
    def _build_hypotheses(
        incident: Mapping[str, Any],
        evidence_by_id: Mapping[str, Mapping[str, Any]],
        decision: DeterministicDecision,
    ) -> list[Dict[str, Any]]:
        if decision.status == "PROVEN":
            assert decision.statement is not None
            return [
                {
                    "rank": 1,
                    "summary": decision.statement,
                    "entity": _entity_for_ids(
                        evidence_by_id,
                        decision.supporting_evidence_ids,
                        incident["source_entity"],
                    ),
                    "confidence": 1.0,
                    "status": "supported",
                    "supporting_evidence_ids": list(
                        _unique(decision.supporting_evidence_ids)
                    ),
                    "contradicting_evidence_ids": [],
                    "missing_evidence": [],
                }
            ]

        if decision.status == "AMBIGUOUS":
            competing = [
                evaluation
                for evaluation in decision.evaluations
                if evaluation.status == "PROVEN"
            ]
            return [
                {
                    "rank": rank,
                    "summary": evaluation.statement,
                    "entity": _entity_for_ids(
                        evidence_by_id,
                        evaluation.supporting_evidence_ids,
                        incident["source_entity"],
                    ),
                    "confidence": 1.0,
                    "status": "competing",
                    "supporting_evidence_ids": list(
                        _evaluation_evidence_ids(evaluation)
                    ),
                    "contradicting_evidence_ids": [],
                    "missing_evidence": list(decision.missing_requirements),
                }
                for rank, evaluation in enumerate(competing, start=1)
            ]

        insufficient = [
            evaluation
            for evaluation in decision.evaluations
            if evaluation.status == "INSUFFICIENT"
        ]
        if insufficient:
            return [
                {
                    "rank": rank,
                    "summary": evaluation.statement,
                    "entity": _entity_for_ids(
                        evidence_by_id,
                        evaluation.supporting_evidence_ids,
                        incident["source_entity"],
                    ),
                    "confidence": 0.0,
                    "status": "unresolved",
                    "supporting_evidence_ids": list(
                        _evaluation_evidence_ids(evaluation)
                    ),
                    "contradicting_evidence_ids": [],
                    "missing_evidence": list(evaluation.missing_requirements),
                }
                for rank, evaluation in enumerate(insufficient, start=1)
            ]

        return [
            {
                "rank": 1,
                "summary": "No deterministic failure signature matched the available Evidence.",
                "entity": copy.deepcopy(dict(incident["source_entity"])),
                "confidence": 0.0,
                "status": "unresolved",
                "supporting_evidence_ids": [],
                "contradicting_evidence_ids": [],
                "missing_evidence": list(decision.missing_requirements),
            }
        ]

    @staticmethod
    def _remediation(decision: DeterministicDecision) -> Dict[str, Any]:
        if decision.status == "PROVEN" and decision.root_cause_id:
            catalog_item = _REMEDIATION_CATALOG.get(decision.root_cause_id)
            if catalog_item is not None:
                return {
                    "suggestions": list(catalog_item["suggestions"]),
                    "verification_conditions": list(
                        catalog_item["verification_conditions"]
                    ),
                }
            return {
                "suggestions": [
                    "Review the proven condition and apply only an operator-approved "
                    "change through the source of truth."
                ],
                "verification_conditions": [
                    "The proven condition clears and the originating alert resolves."
                ],
            }
        return {
            "suggestions": [],
            "verification_conditions": [
                "Collect the missing Evidence and rerun deterministic checks before remediation."
            ],
        }

    @staticmethod
    def _validate_cross_references(
        context: Mapping[str, Any], report: Mapping[str, Any]
    ) -> None:
        context_ids = set(context["evidence_ids"])
        report_ids = set()
        root_cause = report["root_cause"]
        if root_cause is not None:
            report_ids.update(root_cause["supporting_evidence_ids"])
        for hypothesis in report["hypotheses"]:
            report_ids.update(hypothesis["supporting_evidence_ids"])
            report_ids.update(hypothesis["contradicting_evidence_ids"])
        unknown = sorted(report_ids - context_ids)
        if unknown:
            raise ContractViolation(
                f"RCA Report references Evidence outside Context Package: {unknown}"
            )


def _markdown_text(value: Any) -> str:
    return html.escape(" ".join(str(value).split()), quote=False)


def _markdown_list(values: Sequence[str], *, empty: str = "None") -> list[str]:
    if not values:
        return [f"- {empty}"]
    return [f"- {_markdown_text(value)}" for value in values]


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render a schema-valid RCA Report as safe, stable Markdown."""

    validate_contract("rca-report.schema.json", report)
    lines = [
        f"# RCA Report: {_markdown_text(report['report_id'])}",
        "",
        f"- Incident: `{_markdown_text(report['incident_id'])}`",
        f"- Path: `{_markdown_text(report['path'])}`",
        f"- Status: `{_markdown_text(report['status'])}`",
        f"- Generated at: `{_markdown_text(report['generated_at'])}`",
        "",
        "## Root cause",
        "",
    ]
    if report["root_cause"] is None:
        lines.append("No single root cause was proven.")
    else:
        root = report["root_cause"]
        entity = root["entity"]
        lines.extend(
            [
                _markdown_text(root["summary"]),
                "",
                f"Affected entity: `{_markdown_text(entity['kind'])}/"
                f"{_markdown_text(entity['namespace'])}/"
                f"{_markdown_text(entity['name'])}`",
                "",
                "Supporting Evidence:",
                *_markdown_list(root["supporting_evidence_ids"]),
            ]
        )

    lines.extend(["", "## Hypotheses", ""])
    for hypothesis in report["hypotheses"]:
        lines.extend(
            [
                f"### {hypothesis['rank']}. {_markdown_text(hypothesis['summary'])}",
                "",
                f"- Status: `{_markdown_text(hypothesis['status'])}`",
                f"- Confidence encoding: `{hypothesis['confidence']}`",
                "- Supporting Evidence: "
                + (
                    ", ".join(
                        f"`{_markdown_text(item)}`"
                        for item in hypothesis["supporting_evidence_ids"]
                    )
                    or "None"
                ),
                "- Contradicting Evidence: "
                + (
                    ", ".join(
                        f"`{_markdown_text(item)}`"
                        for item in hypothesis["contradicting_evidence_ids"]
                    )
                    or "None"
                ),
                "- Missing Evidence: "
                + (
                    "; ".join(
                        _markdown_text(item)
                        for item in hypothesis["missing_evidence"]
                    )
                    or "None"
                ),
                "",
            ]
        )

    lines.extend(["## Advisory remediation", ""])
    lines.extend(_markdown_list(report["remediation"]["suggestions"]))
    lines.extend(["", "## Recovery verification", ""])
    lines.extend(
        _markdown_list(report["remediation"]["verification_conditions"])
    )
    lines.extend(["", "## Limitations", ""])
    lines.extend(_markdown_list(report["limitations"]))
    return "\n".join(lines) + "\n"
