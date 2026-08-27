import type { RcaReport } from "./types";

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
  const missing = new Set<string>();
  for (const hypothesis of report.hypotheses) {
    for (const entry of hypothesis.missing_evidence) missing.add(entry);
  }
  return [...missing];
}
