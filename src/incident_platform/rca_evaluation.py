"""Post-run RCA scoring with Ground Truth kept outside the runtime boundary."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Mapping, Optional, Sequence

from .contracts import validate_contract
from .deterministic import DeterministicDecision
from .errors import ContractViolation


def _format_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


def prediction_from_deterministic_decision(
    *,
    evaluation_case_id: str,
    scenario_id: str,
    incident_id: str,
    decision: DeterministicDecision,
    available_evidence_ids: Sequence[str],
    completed_at: datetime,
) -> Dict[str, Any]:
    """Create the variant-A evaluation boundary from one Fast Path decision."""

    if decision.status == "PROVEN":
        if decision.root_cause_id is None:
            raise ContractViolation("PROVEN decision has no root_cause_id")
        outcome = "ROOT_CAUSE"
        root_cause_ids = [decision.root_cause_id]
        cited_evidence_ids = list(
            dict.fromkeys(decision.supporting_evidence_ids)
        )
    elif decision.status == "AMBIGUOUS":
        outcome = "AMBIGUOUS"
        root_cause_ids = [
            evaluation.rule_id
            for evaluation in decision.evaluations
            if evaluation.status == "PROVEN"
        ]
        cited_evidence_ids = list(
            dict.fromkeys(decision.supporting_evidence_ids)
        )
    elif decision.status == "ABSTAIN":
        outcome = "ABSTAIN"
        root_cause_ids = []
        cited_evidence_ids = list(
            dict.fromkeys(
                evidence_id
                for evaluation in decision.evaluations
                if evaluation.status == "INSUFFICIENT"
                for evidence_id in evaluation.supporting_evidence_ids
            )
        )
    else:
        raise ContractViolation(
            f"unsupported deterministic decision status: {decision.status}"
        )

    prediction = {
        "schema_version": "1.0.0",
        "evaluation_case_id": evaluation_case_id,
        "scenario_id": scenario_id,
        "incident_id": incident_id,
        "variant_id": "A",
        "path": "fast",
        "outcome": outcome,
        "predicted_root_cause_ids": root_cause_ids,
        "cited_evidence_ids": cited_evidence_ids,
        "available_evidence_ids": list(
            dict.fromkeys(available_evidence_ids)
        ),
        "completed_at": _format_time(completed_at),
    }
    validate_contract("rca-evaluation-prediction.schema.json", prediction)
    return prediction


def prediction_from_agent_report(
    *,
    evaluation_case_id: str,
    scenario_id: str,
    incident_id: str,
    report: Mapping[str, Any],
    available_evidence_ids: Sequence[str],
    completed_at: datetime,
    variant_id: Literal["B", "C", "D"] = "C",
) -> Dict[str, Any]:
    """Create an evaluation Prediction from an Evidence-Gate-accepted Report.

    Variant C is the current runtime mapping: StateGraph-localized Context and
    one bounded Agent run without tree search. The evaluator consumes only
    registered taxonomy IDs; it never infers a label from free-text summaries.
    """

    candidate = copy.deepcopy(dict(report))
    validate_contract("rca-report.schema.json", candidate)
    if candidate["incident_id"] != incident_id:
        raise ContractViolation(
            "Agent Report incident_id does not match evaluation Incident"
        )

    root_cause = candidate["root_cause"]
    ranked_cause_ids: list[str] = []
    if root_cause is not None:
        ranked_cause_ids.append(root_cause["cause_id"])
    for hypothesis in candidate["hypotheses"]:
        cause_id = hypothesis["cause_id"]
        if (
            cause_id is not None
            and hypothesis["status"] in {"supported", "competing"}
            and cause_id not in ranked_cause_ids
        ):
            ranked_cause_ids.append(cause_id)

    cited_evidence_ids: list[str] = []
    if root_cause is not None:
        cited_evidence_ids.extend(root_cause["supporting_evidence_ids"])
    else:
        for hypothesis in candidate["hypotheses"]:
            cited_evidence_ids.extend(hypothesis["supporting_evidence_ids"])
            cited_evidence_ids.extend(hypothesis["contradicting_evidence_ids"])
    cited_evidence_ids = list(dict.fromkeys(cited_evidence_ids))

    if root_cause is not None:
        outcome = "ROOT_CAUSE"
    elif len(ranked_cause_ids) >= 2:
        outcome = "AMBIGUOUS"
    else:
        outcome = "ABSTAIN"
        ranked_cause_ids = []

    prediction = {
        "schema_version": "1.0.0",
        "evaluation_case_id": evaluation_case_id,
        "scenario_id": scenario_id,
        "incident_id": incident_id,
        "variant_id": variant_id,
        "path": candidate["path"],
        "outcome": outcome,
        "predicted_root_cause_ids": ranked_cause_ids[:5],
        "cited_evidence_ids": cited_evidence_ids,
        "available_evidence_ids": list(dict.fromkeys(available_evidence_ids)),
        "completed_at": _format_time(completed_at),
    }
    validate_contract("rca-evaluation-prediction.schema.json", prediction)
    return prediction


def prediction_from_failed_agent_run(
    *,
    evaluation_case_id: str,
    scenario_id: str,
    incident_id: str,
    agent_run: Mapping[str, Any],
    available_evidence_ids: Sequence[str],
    completed_at: datetime,
    variant_id: Literal["B", "C", "D"] = "C",
) -> Dict[str, Any]:
    """Preserve a failed Agent attempt as a scored outcome, not an abstention."""

    candidate = copy.deepcopy(dict(agent_run))
    validate_contract("agent-run-audit.schema.json", candidate)
    if candidate["incident_id"] != incident_id:
        raise ContractViolation(
            "Agent run incident_id does not match evaluation Incident"
        )
    if candidate["status"] == "SUCCEEDED":
        raise ContractViolation("successful Agent run requires an accepted Report")

    prediction = {
        "schema_version": "1.0.0",
        "evaluation_case_id": evaluation_case_id,
        "scenario_id": scenario_id,
        "incident_id": incident_id,
        "variant_id": variant_id,
        "path": "deep",
        "outcome": "FAILED",
        "predicted_root_cause_ids": [],
        "cited_evidence_ids": list(candidate["cited_evidence_ids"]),
        "available_evidence_ids": list(dict.fromkeys(available_evidence_ids)),
        "completed_at": _format_time(completed_at),
    }
    validate_contract("rca-evaluation-prediction.schema.json", prediction)
    return prediction


def evaluate_rca_case(
    ground_truth: Mapping[str, Any],
    prediction: Mapping[str, Any],
    *,
    evaluated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Join one hidden label with one completed prediction and score it.

    The returned artifact contains only aggregate counts and metrics. It does
    not copy expected root-cause labels or relevant Evidence IDs from the
    private Ground Truth record.
    """

    truth = copy.deepcopy(dict(ground_truth))
    candidate = copy.deepcopy(dict(prediction))
    validate_contract("rca-evaluation-ground-truth.schema.json", truth)
    validate_contract("rca-evaluation-prediction.schema.json", candidate)

    identity_fields = ("evaluation_case_id", "scenario_id", "incident_id")
    mismatched = [
        field for field in identity_fields if truth[field] != candidate[field]
    ]
    if mismatched:
        raise ContractViolation(
            "RCA evaluation identity mismatch: " + ", ".join(mismatched)
        )

    expected_causes = tuple(truth["expected_root_cause_ids"])
    predicted_causes = tuple(candidate["predicted_root_cause_ids"])
    relevant_evidence = set(truth["relevant_evidence_ids"])
    cited_evidence = set(candidate["cited_evidence_ids"])
    available_evidence = set(candidate["available_evidence_ids"])
    evidence_groups = tuple(truth.get("relevant_evidence_groups", ()))
    if evidence_groups:
        roles = [group["role"] for group in evidence_groups]
        if len(roles) != len(set(roles)):
            raise ContractViolation("Ground Truth Evidence group roles must be unique")
        grouped_evidence = {
            evidence_id
            for group in evidence_groups
            for evidence_id in group["acceptable_evidence_ids"]
        }
        if grouped_evidence != relevant_evidence:
            raise ContractViolation(
                "Ground Truth Evidence groups must exactly cover relevant_evidence_ids"
            )
        if any(
            int(group["minimum_matches"])
            > len(group["acceptable_evidence_ids"])
            for group in evidence_groups
        ):
            raise ContractViolation(
                "Ground Truth Evidence group minimum exceeds its alternatives"
            )

    missing_labeled_evidence = relevant_evidence - available_evidence
    if missing_labeled_evidence:
        raise ContractViolation(
            "Ground Truth references "
            f"{len(missing_labeled_evidence)} Evidence item(s) outside the "
            "completed prediction snapshot"
        )

    expected_abstain = truth["expected_outcome"] == "ABSTAIN"
    predicted_abstain = candidate["outcome"] == "ABSTAIN"
    prediction_failed = candidate["outcome"] == "FAILED"
    expected_set = set(expected_causes)
    predicted_set = set(predicted_causes)
    matched_evidence = relevant_evidence & cited_evidence
    unsupported_citations = cited_evidence - available_evidence

    if expected_abstain:
        top1_accuracy = None
        top3_recall = None
    else:
        top1_accuracy = float(
            bool(predicted_causes) and predicted_causes[0] == expected_causes[0]
        )
        top3_recall = _ratio(
            len(expected_set & set(predicted_causes[:3])),
            len(expected_set),
        )

    if len(expected_causes) > 1:
        multi_factor_exact_match = float(predicted_set == expected_set)
        multi_factor_partial_match = _ratio(
            len(predicted_set & expected_set),
            len(expected_set),
        )
    else:
        multi_factor_exact_match = None
        multi_factor_partial_match = None

    if cited_evidence:
        evidence_precision = _ratio(
            len(matched_evidence), len(cited_evidence)
        )
    elif relevant_evidence:
        evidence_precision = 0.0
    else:
        evidence_precision = None
    if evidence_groups:
        matched_groups = sum(
            len(cited_evidence & set(group["acceptable_evidence_ids"]))
            >= int(group["minimum_matches"])
            for group in evidence_groups
        )
        evidence_recall = _ratio(matched_groups, len(evidence_groups))
    else:
        evidence_recall = _ratio(
            len(matched_evidence), len(relevant_evidence)
        )

    result = {
        "schema_version": "1.0.0",
        "evaluation_case_id": truth["evaluation_case_id"],
        "scenario_id": truth["scenario_id"],
        "incident_id": truth["incident_id"],
        "variant_id": candidate["variant_id"],
        "evaluated_at": _format_time(evaluated_at or datetime.now(timezone.utc)),
        "counts": {
            "expected_root_causes": len(expected_causes),
            "predicted_root_causes": len(predicted_causes),
            "relevant_evidence": len(relevant_evidence),
            "cited_evidence": len(cited_evidence),
            "available_evidence": len(available_evidence),
            "matched_evidence": len(matched_evidence),
            "unsupported_citations": len(unsupported_citations),
        },
        "metrics": {
            "root_cause_top1_accuracy": top1_accuracy,
            "root_cause_top3_recall": top3_recall,
            "multi_factor_exact_match": multi_factor_exact_match,
            "multi_factor_partial_match": multi_factor_partial_match,
            "evidence_precision": evidence_precision,
            "evidence_recall": evidence_recall,
            "unsupported_evidence_citation_rate": (
                _ratio(len(unsupported_citations), len(cited_evidence)) or 0.0
            ),
            "abstention_correctness": float(
                not prediction_failed and expected_abstain == predicted_abstain
            ),
        },
    }
    validate_contract("rca-evaluation-result.schema.json", result)
    return result
