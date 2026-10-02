"""Bind registered root-cause proofs to identities in a frozen Context.

Only explicit ownership/selection pairs allow a workload to claim a Pod's
proof. Graph proximity, extra citations and matching resource names do not.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from .deterministic import DeterministicRCAEngine, RuleEvaluation
from .stategraph import EntityIdentity


def _scope(entity: Mapping[str, Any]) -> Optional[tuple[str, str]]:
    scope = entity.get("scope", {})
    values = (scope.get("cluster_id"), scope.get("namespace"))
    return values if all(isinstance(value, str) and value for value in values) else None


def _subject_entity_id(subject: Mapping[str, Any]) -> Optional[str]:
    required = ("cluster_id", "kind", "namespace", "name")
    if not all(isinstance(subject.get(key), str) and subject[key] for key in required):
        return None
    if subject["kind"] == "ConfigMap" and subject.get("exists") is False:
        api_version = subject.get("api_version")
        if not isinstance(api_version, str) or not api_version:
            return None
        return EntityIdentity.kubernetes_placeholder(
            cluster_id=subject["cluster_id"], api_version=api_version,
            kind="ConfigMap", namespace=subject["namespace"], name=subject["name"],
        ).entity_id
    uid = subject.get("uid")
    if not isinstance(uid, str) or not uid:
        return None
    return EntityIdentity.kubernetes_resource(
        cluster_id=subject["cluster_id"], uid=uid,
    ).entity_id


def _matches(item: Mapping[str, Any], entity: Mapping[str, Any]) -> bool:
    subject = item.get("subject", {})
    return (
        entity.get("domain") == "kubernetes"
        and _subject_entity_id(subject) == entity["entity_id"]
        and subject.get("kind") == entity.get("entity_type")
        and _scope(entity) == (subject.get("cluster_id"), subject.get("namespace"))
        and subject.get("name") == entity.get("name")
    )


def _descendants(context: Mapping[str, Any], target_id: str) -> set[str]:
    # Context paths may be displayed backwards. Domain/type identify the
    # parent and child; the relation must still join consecutive entities.
    allowed_pairs = {
        ("REPRESENTED_BY", "web-service", "Service", "kubernetes", "Service"),
        ("SELECTS", "kubernetes", "Service", "kubernetes", "Pod"),
        ("OWNS", "kubernetes", "Deployment", "kubernetes", "ReplicaSet"),
        ("OWNS", "kubernetes", "ReplicaSet", "kubernetes", "Pod"),
    }
    children: dict[str, set[str]] = {}
    for path in context["state_paths"]:
        entities = path["entities"]
        for first, relation, second in zip(entities, path["relations"], entities[1:]):
            if _scope(first) is None or _scope(first) != _scope(second):
                continue
            for parent, child in ((first, second), (second, first)):
                pair = (
                    relation, parent["domain"], parent["entity_type"],
                    child["domain"], child["entity_type"],
                )
                if pair in allowed_pairs:
                    children.setdefault(parent["entity_id"], set()).add(child["entity_id"])
    eligible = {target_id}
    pending = [target_id]
    while pending:
        for child_id in children.get(pending.pop(), ()):
            if child_id not in eligible:
                eligible.add(child_id)
                pending.append(child_id)
    return eligible


def bound_root_proof(
    *,
    cause_id: str,
    context: Mapping[str, Any],
    entity: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    proof_engine: DeterministicRCAEngine,
) -> Optional[RuleEvaluation]:
    """Select a proof for this target using only cited, stored Evidence.

    Future cause IDs need an explicit binding policy here. A predicate's
    PROVEN status alone must never authorize an arbitrary Context entity.
    """
    source = context["source_entity"]
    entities = {source["entity_id"]: source} if "entity_id" in source else {}
    for path in context["state_paths"]:
        entities.update((item["entity_id"], item) for item in path["entities"])
    # The catalog may compact display fields; binding uses full frozen data.
    entity = entities.get(entity["entity_id"])
    if entity is None:
        return None
    eligible = _descendants(context, entity["entity_id"])
    pods = [
        item for key, item in entities.items()
        if key in eligible and item["domain"] == "kubernetes"
        and item["entity_type"] == "Pod"
    ]

    def proven(items: Sequence[Mapping[str, Any]]) -> Optional[RuleEvaluation]:
        proof = proof_engine.evaluate_rule(cause_id, items)
        actual_ids = set(proof.supporting_evidence_ids)
        if (proof.status == "PROVEN" and actual_ids
                and actual_ids <= {item["evidence_id"] for item in items}):
            return proof
        return None

    if cause_id in {"kubernetes.container-oomkilled", "kubernetes.image-pull-failure"}:
        for pod in pods:
            proof = proven([item for item in evidence if _matches(item, pod)])
            if proof is not None:
                return proof
    elif cause_id == "kubernetes.missing-configmap":
        # The absence and Event concern different resource UIDs. Evaluate
        # exact pairs so a valid Event for one Pod cannot authorize another.
        missing = [
            item for item in evidence
            if item.get("subject", {}).get("kind") == "ConfigMap"
            and item["subject"].get("exists") is False
            and _subject_entity_id(item["subject"]) is not None
        ]
        for absence in missing:
            subject = absence["subject"]
            direct_configmap = _matches(absence, entity)
            event_pods = (
                [item for item in entities.values()
                 if item["domain"] == "kubernetes" and item["entity_type"] == "Pod"]
                if direct_configmap else pods
            )
            for pod in event_pods:
                if _scope(pod) != (subject["cluster_id"], subject["namespace"]):
                    continue
                for event in evidence:
                    facts = event.get("facts", {})
                    if (event.get("kind") != "kubernetes-event"
                            or not _matches(event, pod)
                            or facts.get("missing_kind") != "ConfigMap"
                            or facts.get("missing_name") != subject["name"]):
                        continue
                    proof = proven([absence, event])
                    if proof is not None:
                        return proof
    return None
