import type {
  AgentRunAudit,
  ContextPackage,
  EvidenceItem,
  Incident,
  IncidentDetail,
  IncidentWorkState,
  KubernetesEntityRef,
  RcaReport,
  ReportBundle,
  TimelineEvent,
  TimelineStage,
  TruncationFlags,
  WorkQueueState,
  WorkStage,
  WorkState,
} from "@/lib/types";

/** One stored lifecycle audit row, before it is folded into the timeline. */
export interface AuditEventFixture {
  occurred_at: string;
  event_type: string;
  details: Record<string, unknown>;
}

export interface FixtureRecord {
  incident: Incident;
  evidence: EvidenceItem[];
  contexts: ContextPackage[];
  reports: ReportBundle[];
  agentRuns: AgentRunAudit[];
  auditEvents: AuditEventFixture[];
  work: IncidentWorkState;
  truncated?: Partial<TruncationFlags>;
}

/**
 * Deterministic stand-in for a real content digest.
 *
 * Fixtures must never carry a random hash: the same fixture has to produce the
 * same bytes on every reload so screenshots and tests stay comparable.
 */
export function stableHash(seed: string): string {
  let hex = "";
  for (let chunk = 0; chunk < 8; chunk += 1) {
    let value = 0x811c9dc5 ^ chunk;
    for (let index = 0; index < seed.length; index += 1) {
      value ^= seed.charCodeAt(index);
      value = Math.imul(value, 0x01000193) >>> 0;
    }
    hex += value.toString(16).padStart(8, "0");
  }
  return `sha256:${hex}`;
}

/** Returns `iso(secondsAfterAnchor)` for compact, readable fixture timelines. */
export function clock(anchorIso: string): (offsetSeconds: number) => string {
  const anchor = new Date(anchorIso).getTime();
  return (offsetSeconds: number) =>
    new Date(anchor + offsetSeconds * 1000).toISOString().replace(".000Z", "Z");
}

export function k8s(
  kind: string,
  name: string,
  namespace: string | null,
  options: { uid?: string | null; exists?: boolean; apiVersion?: string | null } = {},
): KubernetesEntityRef {
  return {
    api_version: options.apiVersion ?? "v1",
    kind,
    namespace,
    name,
    uid: options.uid === undefined ? `uid-${kind.toLowerCase()}-${name}` : options.uid,
    exists: options.exists ?? true,
  };
}

/** Mirrors `report_evidence_ids` in src/incident_platform/repository.py. */
export function reportEvidenceIds(report: RcaReport): string[] {
  const ids = new Set<string>();
  for (const id of report.root_cause?.supporting_evidence_ids ?? []) ids.add(id);
  for (const hypothesis of report.hypotheses) {
    for (const id of hypothesis.supporting_evidence_ids) ids.add(id);
    for (const id of hypothesis.contradicting_evidence_ids) ids.add(id);
  }
  return [...ids].sort();
}

/** Mirrors `IncidentViewerQueryService._audit_stage`. */
function auditStage(event: AuditEventFixture): TimelineStage {
  if (event.event_type === "INCIDENT_CREATED" || event.event_type === "ALERT_RESOLVED") {
    return "DETECTION";
  }
  if (event.event_type === "COLLECTION_COMPLETED") return "COLLECTION";
  if (event.event_type === "STATUS_TRANSITIONED") {
    const target = event.details["to"];
    if (target === "COLLECTING") return "COLLECTION";
    if (target === "LOCALIZING") return "LOCALIZATION";
    if (target === "ANALYZING") return "ANALYSIS";
    if (target === "REPORTED") return "REPORT";
  }
  if (event.event_type.startsWith("COLLECTION_")) return "COLLECTION";
  if (event.event_type.startsWith("LOCALIZATION_")) return "LOCALIZATION";
  return "ANALYSIS";
}

/**
 * Merges stored artifacts into one ordered timeline, mirroring
 * `IncidentViewerQueryService._timeline` so fixtures and the live API agree.
 */
export function buildTimeline(record: FixtureRecord): TimelineEvent[] {
  const events: TimelineEvent[] = [];

  for (const audit of record.auditEvents) {
    events.push({
      occurred_at: audit.occurred_at,
      stage: auditStage(audit),
      event_type: audit.event_type,
      evidence_ids: [],
      details: { ...audit.details },
    });
  }
  for (const item of record.evidence) {
    events.push({
      occurred_at: item.observed_at,
      stage: "COLLECTION",
      event_type: "EVIDENCE_OBSERVED",
      evidence_ids: [item.evidence_id],
      details: {
        source: item.source,
        kind: item.kind,
        // Mirrors IncidentViewerQueryService: occurred_at is observation time,
        // collected_at identifies the Provider run that produced the item.
        collected_at: item.provenance.collected_at,
        subject: { ...item.subject },
      },
    });
  }
  for (const context of record.contexts) {
    events.push({
      occurred_at: context.frozen_at,
      stage: "LOCALIZATION",
      event_type: "CONTEXT_FROZEN",
      evidence_ids: [],
      details: {
        context_id: context.context_id,
        strategy: context.localization.strategy,
        context_completeness: context.localization.context_completeness,
        evidence_count: context.evidence_ids.length,
      },
    });
  }
  for (const run of record.agentRuns) {
    events.push({
      occurred_at: run.completed_at,
      stage: "ANALYSIS",
      event_type: "AGENT_RUN_COMPLETED",
      evidence_ids: run.cited_evidence_ids.slice(0, 100),
      details: {
        agent_run_id: run.agent_run_id,
        status: run.status,
        reason_code: run.reason_code,
        model: run.model,
        usage: { ...run.usage },
      },
    });
  }
  for (const bundle of record.reports) {
    const cited = reportEvidenceIds(bundle.report);
    events.push({
      occurred_at: bundle.report.generated_at,
      stage: "REPORT",
      event_type: "REPORT_GENERATED",
      evidence_ids: cited.slice(0, 100),
      details: {
        report_id: bundle.report.report_id,
        status: bundle.report.status,
        path: bundle.report.path,
        cited_evidence_count: cited.length,
      },
    });
  }

  events.sort((left, right) => {
    if (left.occurred_at !== right.occurred_at) {
      return left.occurred_at < right.occurred_at ? -1 : 1;
    }
    if (left.stage !== right.stage) return left.stage < right.stage ? -1 : 1;
    if (left.event_type !== right.event_type) {
      return left.event_type < right.event_type ? -1 : 1;
    }
    return JSON.stringify(left.details) < JSON.stringify(right.details) ? -1 : 1;
  });
  return events;
}

const NO_TRUNCATION: TruncationFlags = {
  evidence: false,
  contexts: false,
  reports: false,
  agent_runs: false,
  audit_events: false,
  timeline: false,
};

export function buildDetail(record: FixtureRecord): IncidentDetail {
  return {
    schema_version: "1.0.0",
    incident: record.incident,
    evidence: record.evidence,
    contexts: record.contexts,
    reports: record.reports,
    agent_runs: record.agentRuns,
    timeline: buildTimeline(record),
    truncated: { ...NO_TRUNCATION, ...record.truncated },
  };
}

/**
 * Builds an Evidence item, deriving the content hash from its identity so the
 * same fixture always reports the same digest.
 */
export function makeEvidence(
  input: Omit<EvidenceItem, "schema_version" | "provenance"> & {
    provenance: Omit<EvidenceItem["provenance"], "content_hash">;
  },
): EvidenceItem {
  return {
    schema_version: "1.0.0",
    ...input,
    provenance: {
      ...input.provenance,
      content_hash: stableHash(`${input.evidence_id}:${input.provenance.locator}`),
    },
  };
}

/** Fills the nullable work-state fields so fixtures stay readable. */
export function workItem(input: {
  stage: WorkStage;
  state: WorkState;
  available_at: string;
  attempt_count?: number;
  worker_id?: string | null;
  lease_expires_at?: string | null;
  claimed_at?: string | null;
  completed_at?: string | null;
  last_error_code?: string | null;
  context_id?: string | null;
}): WorkQueueState {
  return {
    stage: input.stage,
    state: input.state,
    available_at: input.available_at,
    attempt_count: input.attempt_count ?? 0,
    worker_id: input.worker_id ?? null,
    lease_expires_at: input.lease_expires_at ?? null,
    claimed_at: input.claimed_at ?? null,
    completed_at: input.completed_at ?? null,
    last_error_code: input.last_error_code ?? null,
    context_id: input.context_id ?? null,
  };
}

export type { ReportBundle, IncidentWorkState };
