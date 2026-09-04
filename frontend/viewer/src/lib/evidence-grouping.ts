import { entityKind, entityNamespace } from "./lifecycle";
import { reportEvidenceIds } from "./report-refs";
import type {
  ContextPackage,
  EntityRef,
  EvidenceItem,
  ReportBundle,
} from "./types";
import { isGraphEntityRef } from "./types";

/**
 * Relevance tags come only from stored references.
 *
 * The Viewer never reads a metric value to decide whether Evidence supports or
 * contradicts anything — that is the Agent's job, and its conclusions live in
 * the Report. These tags say where an Evidence item is *referenced*, nothing
 * about what it proves.
 */
export type RelevanceTag =
  | "CITED_BY_REPORT"
  | "IN_CONTEXT"
  | "RECENT_CHANGE"
  | "NOT_USED_BY_REPORT"
  | "OUTSIDE_CONTEXT";

export const RELEVANCE_LABELS: Record<RelevanceTag, string> = {
  CITED_BY_REPORT: "Cited by Report",
  IN_CONTEXT: "In Frozen Context",
  RECENT_CHANGE: "Incident-window activity",
  NOT_USED_BY_REPORT: "Not used by Report",
  OUTSIDE_CONTEXT: "Outside Frozen Context",
};

export interface RelevanceIndex {
  citedByReport: Set<string>;
  inContext: Set<string>;
  recentChange: Set<string>;
  /** Evidence IDs a Context or Report references that the API did not return. */
  missingFromPayload: Set<string>;
  hasReport: boolean;
  hasContext: boolean;
}

export function buildRelevanceIndex(
  evidence: readonly EvidenceItem[],
  contexts: readonly ContextPackage[],
  reports: readonly ReportBundle[],
): RelevanceIndex {
  const present = new Set(evidence.map((item) => item.evidence_id));
  const inContext = new Set<string>();
  const recentChange = new Set<string>();
  for (const context of contexts) {
    for (const id of context.evidence_ids) inContext.add(id);
    for (const id of context.recent_change_evidence_ids) recentChange.add(id);
  }

  const citedByReport = new Set<string>();
  for (const bundle of reports) {
    for (const id of reportEvidenceIds(bundle.report)) citedByReport.add(id);
  }

  const missingFromPayload = new Set<string>();
  for (const id of [...inContext, ...citedByReport]) {
    if (!present.has(id)) missingFromPayload.add(id);
  }

  return {
    citedByReport,
    inContext,
    recentChange,
    missingFromPayload,
    hasReport: reports.length > 0,
    hasContext: contexts.length > 0,
  };
}

export function relevanceTags(
  evidenceId: string,
  index: RelevanceIndex,
): RelevanceTag[] {
  const tags: RelevanceTag[] = [];
  if (index.citedByReport.has(evidenceId)) tags.push("CITED_BY_REPORT");
  if (index.inContext.has(evidenceId)) tags.push("IN_CONTEXT");
  else if (index.hasContext) tags.push("OUTSIDE_CONTEXT");
  if (index.recentChange.has(evidenceId)) tags.push("RECENT_CHANGE");
  if (index.hasReport && !index.citedByReport.has(evidenceId)) {
    tags.push("NOT_USED_BY_REPORT");
  }
  return tags;
}

/**
 * Stable identity for an Evidence subject.
 *
 * Kubernetes subjects key on UID when present so two Pods that share a name
 * across restarts never merge; everything else falls back to the addressable
 * tuple the API does provide.
 */
export function subjectKey(subject: EntityRef): string {
  if (isGraphEntityRef(subject)) {
    return ["graph", subject.domain, subject.entity_type, subject.entity_id].join("|");
  }
  return [
    "k8s",
    subject.cluster_id ?? "",
    subject.namespace ?? "",
    subject.kind,
    subject.name,
    subject.uid ?? "",
  ].join("|");
}

export interface SubjectIdentity {
  key: string;
  cluster: string | null;
  namespace: string | null;
  kind: string;
  name: string;
  uid: string | null;
  exists: boolean;
}

export function subjectIdentity(subject: EntityRef): SubjectIdentity {
  return {
    key: subjectKey(subject),
    cluster: isGraphEntityRef(subject)
      ? typeof subject.scope?.["cluster_id"] === "string"
        ? (subject.scope["cluster_id"] as string)
        : null
      : (subject.cluster_id ?? null),
    namespace: entityNamespace(subject),
    kind: entityKind(subject),
    name: subject.name,
    uid: isGraphEntityRef(subject) ? subject.entity_id : subject.uid,
    exists: subject.exists,
  };
}

export interface SubjectGroup {
  identity: SubjectIdentity;
  items: EvidenceItem[];
  sources: string[];
  kinds: string[];
  /** Earliest and latest observation across the group. */
  firstObservedAt: string;
  lastObservedAt: string;
  citedCount: number;
  inContextCount: number;
  recentChangeCount: number;
  degradedCount: number;
}

/**
 * Collapses an Evidence list into one row per subject.
 *
 * A single Pod commonly carries a kernel OOM log pattern, a restart-count
 * delta, a memory ratio, a termination state and several Kubernetes Events.
 * Grouping them turns a wall of equally weighted cards into one line per thing
 * that was actually observed.
 */
export function groupEvidenceBySubject(
  evidence: readonly EvidenceItem[],
  index: RelevanceIndex,
): SubjectGroup[] {
  const groups = new Map<string, SubjectGroup>();

  for (const item of evidence) {
    const identity = subjectIdentity(item.subject);
    let group = groups.get(identity.key);
    if (!group) {
      group = {
        identity,
        items: [],
        sources: [],
        kinds: [],
        firstObservedAt: item.observed_at,
        lastObservedAt: item.observed_at,
        citedCount: 0,
        inContextCount: 0,
        recentChangeCount: 0,
        degradedCount: 0,
      };
      groups.set(identity.key, group);
    }
    group.items.push(item);
    if (!group.sources.includes(item.source)) group.sources.push(item.source);
    if (!group.kinds.includes(item.kind)) group.kinds.push(item.kind);
    if (item.observed_at < group.firstObservedAt) group.firstObservedAt = item.observed_at;
    if (item.observed_at > group.lastObservedAt) group.lastObservedAt = item.observed_at;
    if (index.citedByReport.has(item.evidence_id)) group.citedCount += 1;
    if (index.inContext.has(item.evidence_id)) group.inContextCount += 1;
    if (index.recentChange.has(item.evidence_id)) group.recentChangeCount += 1;
    if (isDegraded(item)) group.degradedCount += 1;
  }

  for (const group of groups.values()) {
    group.sources.sort();
    group.kinds.sort();
    group.items.sort((left, right) =>
      left.observed_at === right.observed_at
        ? left.evidence_id.localeCompare(right.evidence_id)
        : left.observed_at.localeCompare(right.observed_at),
    );
  }

  // Most-cited first, then richest signal cluster, then stable by name.
  return [...groups.values()].sort((left, right) => {
    if (right.citedCount !== left.citedCount) return right.citedCount - left.citedCount;
    if (right.items.length !== left.items.length) {
      return right.items.length - left.items.length;
    }
    return left.identity.name.localeCompare(right.identity.name);
  });
}

/**
 * The collector's own verdict for this observation, when it recorded one.
 * Surfaced verbatim — the Viewer does not interpret metric values.
 */
export function resultStatus(item: EvidenceItem): string | null {
  // Live collectors record `result_status`; some record `status`. Read both
  // rather than silently dropping the verdict for one of them.
  const explicit = item.facts?.["result_status"];
  if (typeof explicit === "string") return explicit;
  const legacy = item.facts?.["status"];
  return typeof legacy === "string" ? legacy : null;
}

/** Degraded means the collector said so, or completeness is below full. */
export function isDegraded(item: EvidenceItem): boolean {
  const status = resultStatus(item);
  if (status === "INSUFFICIENT_DATA" || status === "PARTIAL") return true;
  return item.quality.completeness < 1;
}

export interface EvidenceSummary {
  total: number;
  inContext: number;
  outsideContext: number;
  recentChange: number;
  degraded: number;
  citedByReport: number;
  subjects: number;
  providerFailures: number;
  missingFromPayload: number;
}

export function summariseEvidence(
  evidence: readonly EvidenceItem[],
  index: RelevanceIndex,
  contexts: readonly ContextPackage[],
  groups: readonly SubjectGroup[],
): EvidenceSummary {
  const providerFailures = contexts.reduce(
    (total, context) => total + context.collector_failures.length,
    0,
  );
  return {
    total: evidence.length,
    inContext: evidence.filter((item) => index.inContext.has(item.evidence_id)).length,
    outsideContext: index.hasContext
      ? evidence.filter((item) => !index.inContext.has(item.evidence_id)).length
      : 0,
    recentChange: evidence.filter((item) => index.recentChange.has(item.evidence_id))
      .length,
    degraded: evidence.filter(isDegraded).length,
    citedByReport: evidence.filter((item) => index.citedByReport.has(item.evidence_id))
      .length,
    subjects: groups.length,
    providerFailures,
    missingFromPayload: index.missingFromPayload.size,
  };
}
