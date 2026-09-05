import { isGraphEntityRef, type EntityRef, type RcaHypothesis, type RcaReport } from "./types";

/**
 * Evidence IDs a Report actually references.
 *
 * Mirrors `report_evidence_ids` in src/incident_platform/repository.py so the
 * Viewer's idea of "cited" matches the backend's.
 */
export function reportEvidenceIds(report: RcaReport): string[] {
  const ids = new Set<string>();
  for (const id of report.root_cause?.supporting_evidence_ids ?? []) ids.add(id);
  for (const hypothesis of report.hypotheses) {
    for (const id of hypothesis.supporting_evidence_ids) ids.add(id);
    for (const id of hypothesis.contradicting_evidence_ids) ids.add(id);
  }
  return [...ids].sort();
}

/** Missing-Evidence descriptions the Report recorded, deduplicated. */
export function reportMissingEvidence(report: RcaReport): string[] {
  return missingRequirements(report.hypotheses);
}

function missingRequirements(hypotheses: readonly RcaHypothesis[]): string[] {
  const missing = new Set<string>();
  for (const hypothesis of hypotheses) {
    for (const entry of hypothesis.missing_evidence) missing.add(entry);
  }
  return [...missing];
}

/** Compare stored references within one Report, never infer identity from rank/name alone. */
function sameReportEntity(left: EntityRef, right: EntityRef): boolean {
  if (isGraphEntityRef(left) || isGraphEntityRef(right)) {
    return isGraphEntityRef(left) && isGraphEntityRef(right)
      && left.entity_id === right.entity_id
      && left.entity_type === right.entity_type
      && left.domain === right.domain;
  }
  return left.cluster_id === right.cluster_id
    && left.api_version === right.api_version
    && left.kind === right.kind
    && left.namespace === right.namespace
    && left.name === right.name
    && left.uid === right.uid;
}

/**
 * These are missing proof requirements, not absent Evidence objects or failed
 * collectors. null means no exact selected-cause match (including legacy
 * Reports without cause_id), not zero requirements. Other hypotheses cannot
 * invalidate or promote the stored Report outcome.
 */
export function reportRequirementGaps(report: RcaReport): {
  selected: string[] | null;
  other: string[];
} {
  const root = report.root_cause;
  const selected = root?.cause_id
    ? report.hypotheses.filter((hypothesis) =>
      hypothesis.cause_id === root.cause_id
      && sameReportEntity(hypothesis.entity, root.entity))
    : [];
  return {
    selected: selected.length > 0 ? missingRequirements(selected) : null,
    other: missingRequirements(report.hypotheses.filter((item) => !selected.includes(item))),
  };
}
