"""Evidence-gated deterministic RCA rules for clear Kubernetes failures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence, Tuple

from .contracts import validate_contract


OOM_RESTART_DELTA_MINIMUM = 1.0
OOM_MEMORY_RATIO_REFERENCE_THRESHOLD = 0.95
OOM_EVIDENCE_GATE_POLICY = "oom-signature-union-restart-v3"


ROOT_CAUSE_EVIDENCE_REQUIREMENTS: Mapping[str, Tuple[str, ...]] = {
    "kubernetes.container-oomkilled": (
        "An exact Pod-scoped OOMKilled termination or kernel memcg OOM signature",
        "Prometheus restart_count_delta at or above 1 for the same Pod UID",
    ),
    "kubernetes.image-pull-failure": (
        "A Pod container waiting in ErrImagePull or ImagePullBackOff",
        "A matching Kubernetes Event for the same Pod",
    ),
    "kubernetes.missing-configmap": (
        "A required ConfigMap that does not exist",
        "A matching CreateContainerConfigError or FailedMount Event",
    ),
}


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


def _at_least(value: object, threshold: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and value >= threshold
    )


class OOMKilledRule:
    rule_id = "kubernetes.container-oomkilled"

    def evaluate(self, evidence: Sequence[Mapping[str, Any]]) -> RuleEvaluation:
        kubernetes_terminations = [
            item
            for item in evidence
            if item.get("source") == "kubernetes"
            and item.get("kind") == "resource-state"
            and _facts(item).get("last_termination_reason") == "OOMKilled"
        ]
        kernel_oom_signals = [
            item
            for item in evidence
            if item.get("source") == "loki"
            and item.get("kind") == "log-pattern"
            and isinstance(item.get("provenance"), Mapping)
            and item["provenance"].get("provider")
            == "loki-kernel-oom-provider"
            and _facts(item).get("pattern_id") == "kernel-cgroup-oom"
            and _facts(item).get("kernel_constraint") == "CONSTRAINT_MEMCG"
            and _at_least(_facts(item).get("match_count"), 1)
            and isinstance(item.get("subject"), Mapping)
            and item["subject"].get("kind") == "Pod"
            and bool(item["subject"].get("uid"))
            and _facts(item).get("pod_uid") == item["subject"].get("uid")
        ]
        signatures = [*kubernetes_terminations, *kernel_oom_signals]
        if not signatures:
            return RuleEvaluation(self.rule_id, "NOT_APPLICABLE", "")

        for signature in signatures:
            restarts = [
                item
                for item in evidence
                if item.get("source") == "prometheus"
                and item.get("kind") == "metric-summary"
                and _facts(item).get("metric") == "restart_count_delta"
                and _at_least(
                    _facts(item).get("peak_delta"), OOM_RESTART_DELTA_MINIMUM
                )
                and _same_subject(signature, item)
            ]
            if restarts:
                if signature.get("source") == "kubernetes":
                    statement = (
                        "The container was terminated with OOMKilled and the same "
                        "Pod UID recorded a restart increase."
                    )
                else:
                    statement = (
                        "The kernel recorded a Pod cgroup OOM and the same Pod UID "
                        "recorded a restart increase."
                    )
                return RuleEvaluation(
                    rule_id=self.rule_id,
                    status="PROVEN",
                    statement=statement,
                    supporting_evidence_ids=(
                        signature["evidence_id"],
                        restarts[0]["evidence_id"],
                    ),
                )
        matching_restarts = [
            item
            for signature in signatures
            for item in evidence
            if item.get("source") == "prometheus"
            and item.get("kind") == "metric-summary"
            and _facts(item).get("metric") == "restart_count_delta"
            and _at_least(
                _facts(item).get("peak_delta"), OOM_RESTART_DELTA_MINIMUM
            )
            and _same_subject(signature, item)
        ]
        missing = []
        if not matching_restarts:
            missing.append(
                "Prometheus restart_count_delta at or above 1 for the same workload"
            )
        return RuleEvaluation(
            rule_id=self.rule_id,
            status="INSUFFICIENT",
            statement=(
                "An exact Pod OOM signal was observed but same-UID restart "
                "corroboration is incomplete."
            ),
            supporting_evidence_ids=tuple(
                dict.fromkeys(
                    item["evidence_id"]
                    for item in (*signatures, *matching_restarts)
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
                and (
                    _facts(item).get("image_pull_code") in self._WAITING_REASONS
                    or _facts(item).get("message_code") in self._WAITING_REASONS
                )
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


def registered_rule_evaluations(
    evidence: Sequence[Mapping[str, Any]],
) -> Tuple[RuleEvaluation, ...]:
    """Evaluate registered proof predicates without selecting a root cause.

    The caller remains responsible for validating Evidence contracts. This
    lightweight form is used only to rank an already-validated frozen catalog;
    the Evidence Gate uses ``DeterministicRCAEngine.evaluate_rule`` instead.
    """

    return tuple(
        rule.evaluate(evidence)
        for rule in (OOMKilledRule(), ImagePullRule(), MissingConfigMapRule())
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
        self._rules_by_id = {rule.rule_id: rule for rule in self._rules}

    def evaluate_rule(
        self,
        rule_id: str,
        evidence: Sequence[Mapping[str, Any]],
    ) -> RuleEvaluation:
        """Evaluate one registered cause against only the claimed Evidence."""

        try:
            rule = self._rules_by_id[rule_id]
        except KeyError as error:
            raise ValueError(f"unknown deterministic rule ID: {rule_id}") from error
        for item in evidence:
            validate_contract("evidence-item.schema.json", item)
        return rule.evaluate(evidence)

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
