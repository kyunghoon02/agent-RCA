"""Evidence-gated deterministic RCA rules for clear Kubernetes failures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence, Tuple

from .contracts import validate_contract


@dataclass(frozen=True)
class RuleEvaluation:
    rule_id: str
    status: str
    statement: str
    supporting_evidence_ids: Tuple[str, ...] = tuple()
    missing_requirements: Tuple[str, ...] = tuple()


@dataclass(frozen=True)
class DeterministicDecision:
    status: str
    root_cause_id: Optional[str]
    statement: Optional[str]
    supporting_evidence_ids: Tuple[str, ...]
    missing_requirements: Tuple[str, ...]
    evaluations: Tuple[RuleEvaluation, ...]


class DeterministicRule(Protocol):
    rule_id: str

    def evaluate(self, evidence: Sequence[Mapping[str, Any]]) -> RuleEvaluation:
        ...


def _facts(item: Mapping[str, Any]) -> Mapping[str, Any]:
    facts = item.get("facts", {})
    return facts if isinstance(facts, Mapping) else {}


def _same_subject(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    first_subject = first.get("subject", {})
    second_subject = second.get("subject", {})
    if (
        first_subject.get("namespace"),
        first_subject.get("kind"),
        first_subject.get("name"),
    ) != (
        second_subject.get("namespace"),
        second_subject.get("kind"),
        second_subject.get("name"),
    ):
        return False
    for identity_key in ("cluster_id", "uid"):
        first_value = first_subject.get(identity_key)
        second_value = second_subject.get(identity_key)
        if first_value is not None or second_value is not None:
            if not first_value or first_value != second_value:
                return False
    return True


class OOMKilledRule:
    rule_id = "kubernetes.container-oomkilled"

    def evaluate(self, evidence: Sequence[Mapping[str, Any]]) -> RuleEvaluation:
        terminations = [
            item
            for item in evidence
            if item.get("source") == "kubernetes"
            and item.get("kind") == "resource-state"
            and _facts(item).get("last_termination_reason") == "OOMKilled"
        ]
        if not terminations:
            return RuleEvaluation(self.rule_id, "NOT_APPLICABLE", "")

        for termination in terminations:
            restarts = [
                item
                for item in evidence
                if item.get("source") == "prometheus"
                and item.get("kind") == "metric-summary"
                and _facts(item).get("metric") == "restart_count_delta"
                and _facts(item).get("peak_delta", 0) >= 1
                and _same_subject(termination, item)
            ]
            metrics = [
                item
                for item in evidence
                if item.get("source") == "prometheus"
                and item.get("kind") == "metric-summary"
                and _facts(item).get("metric") == "memory_working_set_ratio"
                and _facts(item).get("peak_ratio", 0) >= 0.95
                and _same_subject(termination, item)
            ]
            if restarts and metrics:
                return RuleEvaluation(
                    rule_id=self.rule_id,
                    status="PROVEN",
                    statement=(
                        "Container memory usage reached its limit and the container "
                        "was terminated with OOMKilled."
                    ),
                    supporting_evidence_ids=(
                        termination["evidence_id"],
                        restarts[0]["evidence_id"],
                        metrics[0]["evidence_id"],
                    ),
                )
        matching_restarts = [
            item
            for termination in terminations
            for item in evidence
            if item.get("source") == "prometheus"
            and item.get("kind") == "metric-summary"
            and _facts(item).get("metric") == "restart_count_delta"
            and _facts(item).get("peak_delta", 0) >= 1
            and _same_subject(termination, item)
        ]
        matching_memory = [
            item
            for termination in terminations
            for item in evidence
            if item.get("source") == "prometheus"
            and item.get("kind") == "metric-summary"
            and _facts(item).get("metric") == "memory_working_set_ratio"
            and _facts(item).get("peak_ratio", 0) >= 0.95
            and _same_subject(termination, item)
        ]
        missing = []
        if not matching_restarts:
            missing.append(
                "Prometheus restart_count_delta at or above 1 for the same workload"
            )
        if not matching_memory:
            missing.append(
                "Prometheus memory_working_set_ratio peak at or above 0.95 for the same workload"
            )
        return RuleEvaluation(
            rule_id=self.rule_id,
            status="INSUFFICIENT",
            statement=(
                "OOMKilled was observed but restart and memory-limit corroboration "
                "is incomplete."
            ),
            supporting_evidence_ids=tuple(
                dict.fromkeys(
                    item["evidence_id"]
                    for item in (*terminations, *matching_restarts, *matching_memory)
                )
            ),
            missing_requirements=tuple(missing),
        )


class ImagePullRule:
    rule_id = "kubernetes.image-pull-failure"
    _WAITING_REASONS = frozenset({"ErrImagePull", "ImagePullBackOff"})

    def evaluate(self, evidence: Sequence[Mapping[str, Any]]) -> RuleEvaluation:
        states = [
            item
            for item in evidence
            if item.get("source") == "kubernetes"
            and item.get("kind") == "resource-state"
            and _facts(item).get("waiting_reason") in self._WAITING_REASONS
        ]
        if not states:
            return RuleEvaluation(self.rule_id, "NOT_APPLICABLE", "")

        for state in states:
            events = [
                item
                for item in evidence
                if item.get("source") == "kubernetes"
                and item.get("kind") == "kubernetes-event"
                and _facts(item).get("message_code") in self._WAITING_REASONS
                and _same_subject(state, item)
            ]
            if events:
                image = _facts(state).get("image", "the configured image")
                return RuleEvaluation(
                    rule_id=self.rule_id,
                    status="PROVEN",
                    statement=f"Kubernetes could not pull {image}.",
                    supporting_evidence_ids=(
                        state["evidence_id"],
                        events[0]["evidence_id"],
                    ),
                )
        return RuleEvaluation(
            rule_id=self.rule_id,
            status="INSUFFICIENT",
            statement="An image pull waiting state exists without a matching Event.",
            supporting_evidence_ids=tuple(item["evidence_id"] for item in states),
            missing_requirements=(
                "Matching ErrImagePull or ImagePullBackOff Kubernetes Event",
            ),
        )


class MissingConfigMapRule:
    rule_id = "kubernetes.missing-configmap"

    def evaluate(self, evidence: Sequence[Mapping[str, Any]]) -> RuleEvaluation:
        missing = [
            item
            for item in evidence
            if item.get("source") == "kubernetes"
            and item.get("kind") == "resource-state"
            and item.get("subject", {}).get("kind") == "ConfigMap"
            and item.get("subject", {}).get("exists") is False
            and _facts(item).get("required") is True
        ]
        if not missing:
            return RuleEvaluation(self.rule_id, "NOT_APPLICABLE", "")

        for state in missing:
            subject = state["subject"]
            events = [
                item
                for item in evidence
                if item.get("source") == "kubernetes"
                and item.get("kind") == "kubernetes-event"
                and _facts(item).get("reason")
                in {"CreateContainerConfigError", "FailedMount"}
                and _facts(item).get("missing_kind") == "ConfigMap"
                and _facts(item).get("missing_name") == subject.get("name")
                and item.get("subject", {}).get("namespace")
                == subject.get("namespace")
            ]
            if events:
                return RuleEvaluation(
                    rule_id=self.rule_id,
                    status="PROVEN",
                    statement=(
                        f"Required ConfigMap {subject.get('name')} does not exist."
                    ),
                    supporting_evidence_ids=(
                        state["evidence_id"],
                        events[0]["evidence_id"],
                    ),
                )
        return RuleEvaluation(
            rule_id=self.rule_id,
            status="INSUFFICIENT",
            statement="A required ConfigMap is unresolved without a matching Pod Event.",
            supporting_evidence_ids=tuple(item["evidence_id"] for item in missing),
            missing_requirements=(
                "Matching CreateContainerConfigError or FailedMount Event",
            ),
        )


class DeterministicRCAEngine:
    """Return a result only when exactly one rule has all required Evidence."""

    def __init__(self, rules: Optional[Iterable[DeterministicRule]] = None) -> None:
        self._rules = tuple(
            rules
            if rules is not None
            else (OOMKilledRule(), ImagePullRule(), MissingConfigMapRule())
        )
        rule_ids = [rule.rule_id for rule in self._rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("deterministic rule IDs must be unique")

    def evaluate(
        self, evidence: Sequence[Mapping[str, Any]]
    ) -> DeterministicDecision:
        for item in evidence:
            validate_contract("evidence-item.schema.json", item)
        evaluations = tuple(rule.evaluate(evidence) for rule in self._rules)
        proven = [evaluation for evaluation in evaluations if evaluation.status == "PROVEN"]
        if len(proven) == 1:
            result = proven[0]
            return DeterministicDecision(
                status="PROVEN",
                root_cause_id=result.rule_id,
                statement=result.statement,
                supporting_evidence_ids=result.supporting_evidence_ids,
                missing_requirements=tuple(),
                evaluations=evaluations,
            )
        if len(proven) > 1:
            return DeterministicDecision(
                status="AMBIGUOUS",
                root_cause_id=None,
                statement=None,
                supporting_evidence_ids=tuple(
                    evidence_id
                    for evaluation in proven
                    for evidence_id in evaluation.supporting_evidence_ids
                ),
                missing_requirements=(
                    "Multiple deterministic root causes are simultaneously proven",
                ),
                evaluations=evaluations,
            )
        missing = tuple(
            requirement
            for evaluation in evaluations
            if evaluation.status == "INSUFFICIENT"
            for requirement in evaluation.missing_requirements
        )
        return DeterministicDecision(
            status="ABSTAIN",
            root_cause_id=None,
            statement=None,
            supporting_evidence_ids=tuple(),
            missing_requirements=missing or ("No deterministic failure signature matched",),
            evaluations=evaluations,
        )
