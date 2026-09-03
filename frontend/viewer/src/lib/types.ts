/**
 * Wire types for the read-only Viewer.
 *
 * These mirror `contracts/schemas/*.json` field-for-field. The fixture adapter
 * and the HTTP adapter both produce exactly these shapes, so screens never need
 * to know which one is active.
 */

export const INCIDENT_STATUSES = [
  "RECEIVED",
  "COLLECTING",
  "LOCALIZING",
  "ANALYZING",
  "REPORTED",
  "PARTIAL",
  "FAILED",
] as const;
export type IncidentStatus = (typeof INCIDENT_STATUSES)[number];

export const SEVERITIES = ["info", "warning", "critical"] as const;
export type Severity = (typeof SEVERITIES)[number];

export type IncidentSource = "alertmanager" | "cloud-monitoring";

/** contracts/schemas/entity-ref.schema.json — kubernetesEntityRef branch. */
export interface KubernetesEntityRef {
  cluster_id?: string;
  api_version?: string | null;
  kind: string;
  namespace: string | null;
  name: string;
  uid: string | null;
  exists: boolean;
}

/** contracts/schemas/entity-ref.schema.json — graphEntityRef branch. */
export interface GraphEntityRef {
  entity_id: string;
  entity_type: string;
  domain: string;
  name: string;
  scope: Record<string, unknown>;
  external_ref: string | null;
  exists: boolean;
}

export type EntityRef = KubernetesEntityRef | GraphEntityRef;

export function isGraphEntityRef(entity: EntityRef): entity is GraphEntityRef {
  return "entity_id" in entity;
}

export type CollectorName =
  | "prometheus"
  | "prometheus-api"
  | "logs"
  | "kubernetes"
  | "deployment"
  | "trace"
  | "hubble";

export type CollectorStatusValue =
  | "PENDING"
  | "RUNNING"
  | "SUCCEEDED"
  | "PARTIAL"
  | "FAILED"
  | "TIMED_OUT"
  | "SKIPPED";

export interface CollectorStatus {
  collector: CollectorName;
  status: CollectorStatusValue;
  attempts: number;
  started_at: string | null;
  ended_at: string | null;
  error: string | null;
}

export interface IncidentWindow {
  baseline_start: string;
  incident_start: string;
  incident_end: string | null;
  recovery_end: string | null;
}

/** contracts/schemas/incident.schema.json */
export interface Incident {
  schema_version: "1.0.0";
  incident_id: string;
  deduplication_key: string;
  status: IncidentStatus;
  severity: Severity;
  source: IncidentSource;
  triggered_at: string;
  window: IncidentWindow;
  alert: {
    fingerprint: string;
    name: string;
    labels: Record<string, string>;
    annotations: Record<string, string>;
  };
  source_entity: EntityRef;
  collector_statuses: CollectorStatus[];
  created_at: string;
  updated_at: string;
}

/** contracts/schemas/viewer-incident-list.schema.json — $defs.summary */
export interface IncidentSummary {
  incident_id: string;
  status: IncidentStatus;
  severity: Severity;
  source: IncidentSource;
  triggered_at: string;
  updated_at: string;
  alert_name: string;
  source_entity: EntityRef;
  collector_problem_count: number;
}

export interface IncidentListResult {
  schema_version: "1.0.0";
  items: IncidentSummary[];
  next_cursor: string | null;
}

/** contracts/schemas/viewer-incident-query.schema.json */
export interface IncidentListQuery {
  schema_version: "1.0.0";
  statuses: IncidentStatus[];
  severities: Severity[];
  namespace: string | null;
  search: string | null;
  limit: number;
  cursor: string | null;
}

export type EvidenceSource =
  | "prometheus"
  | "logs"
  | "loki"
  | "kubernetes"
  | "deployment"
  | "trace"
  | "network"
  | "hubble";

export type EvidenceKind =
  | "metric-summary"
  | "log-pattern"
  | "resource-state"
  | "state-diff"
  | "kubernetes-event"
  | "deployment-change"
  | "trace-summary"
  | "network-flow-summary";

export type Freshness = "live" | "recent" | "stale" | "unknown";

/** contracts/schemas/evidence-item.schema.json */
export interface EvidenceItem {
  schema_version: "1.0.0";
  evidence_id: string;
  incident_id: string;
  source: EvidenceSource;
  kind: EvidenceKind;
  observed_at: string;
  window: { start: string; end: string };
  subject: EntityRef;
  summary: string;
  facts: Record<string, unknown>;
  provenance: {
    provider: string;
    query: string;
    locator: string;
    collected_at: string;
    content_hash: string;
  };
  quality: {
    freshness: Freshness;
    completeness: number;
    confidence: number;
  };
  redactions: string[];
}

export interface InvestigationScope {
  incident_id: string;
  seed_entity_ids: string[];
  domains: string[];
  correlation_keys: Record<string, string>;
  relation_types: string[];
  time_window: { start: string; end: string };
  max_entities: number;
  max_depth: number;
}

export interface LegacyKubernetesScope {
  namespaces: string[];
  entity_uids: string[];
  metapaths: string[][];
  time_window: { start: string; end: string };
  max_entities: number;
}

export type ContextScope = InvestigationScope | LegacyKubernetesScope;

export function isInvestigationScope(scope: ContextScope): scope is InvestigationScope {
  return "seed_entity_ids" in scope;
}

export interface StatePath {
  path_id: string;
  entities: EntityRef[];
  relations: string[];
  evidence_ids: string[];
}

/** contracts/schemas/context-package.schema.json */
export interface ContextPackage {
  schema_version: "1.0.0";
  context_id: string;
  incident_id: string;
  frozen_at: string;
  source_entity: EntityRef;
  scope: ContextScope;
  state_paths: StatePath[];
  evidence_ids: string[];
  recent_change_evidence_ids: string[];
  missing_evidence: { source: string; reason: string }[];
  collector_failures: { collector: string; error: string }[];
  localization: {
    strategy: "stategraph" | "namespace-fallback";
    candidate_entities_before: number;
    candidate_entities_after: number;
    context_completeness: number;
  };
}

export type ReportStatus = "conclusive" | "inconclusive" | "partial";
export type HypothesisStatus = "supported" | "competing" | "rejected" | "unresolved";
export type RootCauseId =
  | "kubernetes.container-oomkilled"
  | "kubernetes.image-pull-failure"
  | "kubernetes.missing-configmap";

export interface RcaHypothesis {
  rank: number;
  /** Absent only on reports persisted before RCA Report schema 1.1.0. */
  cause_id?: RootCauseId | null;
  summary: string;
  entity: EntityRef;
  confidence: number;
  status: HypothesisStatus;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  reference_document_ids: string[];
  missing_evidence: string[];
}

/** contracts/schemas/rca-report.schema.json */
export interface RcaReport {
  schema_version: "1.0.0" | "1.1.0";
  report_id: string;
  incident_id: string;
  context_id: string;
  path: "fast" | "normal" | "deep";
  status: ReportStatus;
  generated_at: string;
  root_cause: {
    /** Absent only on reports persisted before RCA Report schema 1.1.0. */
    cause_id?: RootCauseId;
    summary: string;
    entity: EntityRef;
    supporting_evidence_ids: string[];
    reference_document_ids: string[];
  } | null;
  hypotheses: RcaHypothesis[];
  remediation: {
    suggestions: string[];
    verification_conditions: string[];
  };
  budget: {
    applicable: boolean;
    llm_calls: number;
    tool_calls: number;
    tree_depth: number;
    wall_time_ms: number;
    exhausted: boolean;
  };
  read_only: true;
  limitations: string[];
}

export interface ReportBundle {
  report: RcaReport;
  markdown: string;
}

export type AgentRunStatus =
  | "SUCCEEDED"
  | "GATE_REJECTED"
  | "MODEL_FAILED"
  | "BUDGET_EXHAUSTED";

export interface AgentContractFailure {
  schema_name: "agent-rca-draft.schema.json";
  instance_pointer: string;
  schema_pointer: string;
  keyword: string;
  error_count: number;
}

/** contracts/schemas/agent-run-audit.schema.json — content-free by contract. */
export interface AgentRunAudit {
  schema_version: "1.0.0";
  agent_run_id: string;
  incident_id: string;
  context_id: string;
  knowledge_audit_id: string;
  knowledge_status: "SUCCEEDED" | "NO_MATCH" | "STALE_ONLY" | "FAILED" | "TIMED_OUT";
  model: string;
  status: AgentRunStatus;
  reason_code:
    | "REPORT_ACCEPTED"
    | "EVIDENCE_GATE_REJECTED"
    | "MODEL_EXECUTION_FAILED"
    | "MODEL_BUDGET_EXCEEDED"
    | "GATE_DRAFT_CONTRACT_INVALID"
    | "GATE_INCIDENT_MISMATCH"
    | "GATE_CONTEXT_MISMATCH"
    | "GATE_UNKNOWN_EVIDENCE_CITATION"
    | "GATE_UNINSPECTED_EVIDENCE_CITATION"
    | "GATE_UNKNOWN_REFERENCE_CITATION"
    | "GATE_UNINSPECTED_REFERENCE_CITATION"
    | "GATE_ENTITY_OUT_OF_SCOPE"
    | "GATE_HYPOTHESIS_RANK_INVALID"
    | "GATE_HYPOTHESIS_SUPPORT_MISSING"
    | "GATE_ROOT_LEADING_MISMATCH"
    | "GATE_PROOF_INSUFFICIENT"
    | "GATE_CONCLUSIVE_ROOT_MISSING"
    | "GATE_CONCLUSIVE_SUPPORT_MISSING"
    | "GATE_CONTRADICTING_EVIDENCE"
    | "GATE_CHANNELS_INSUFFICIENT"
    | "GATE_CONTEXT_INCOMPLETE"
    | "GATE_CONCLUSIVE_COLLECTOR_FAILURE"
    | "GATE_ROOTLESS_REMEDIATION"
    | "GATE_INVESTIGATION_BUDGET_EXCEEDED";
  started_at: string;
  completed_at: string;
  budget: {
    max_turns: number;
    max_llm_calls: number;
    max_tool_calls: number;
    max_output_tokens: number;
    max_wall_time_ms: number;
  };
  usage: {
    llm_calls: number;
    tool_calls: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    wall_time_ms: number;
  };
  tool_events: {
    sequence: number;
    tool_name: "inspect_evidence" | "inspect_reference";
    requested_id: string;
    status: "SUCCEEDED" | "DENIED" | "NOT_FOUND" | "BUDGET_EXHAUSTED";
    result_hash: string;
  }[];
  retrieved_reference_ids: string[];
  inspected_evidence_ids: string[];
  inspected_reference_document_ids: string[];
  cited_evidence_ids: string[];
  cited_reference_document_ids: string[];
  contract_failure?: AgentContractFailure;
}

export const TIMELINE_STAGES = [
  "DETECTION",
  "COLLECTION",
  "LOCALIZATION",
  "ANALYSIS",
  "REPORT",
] as const;
export type TimelineStage = (typeof TIMELINE_STAGES)[number];

export interface TimelineEvent {
  occurred_at: string;
  stage: TimelineStage;
  event_type: string;
  evidence_ids: string[];
  details: Record<string, unknown>;
}

export interface TruncationFlags {
  evidence: boolean;
  contexts: boolean;
  reports: boolean;
  agent_runs: boolean;
  audit_events: boolean;
  timeline: boolean;
}

/** contracts/schemas/viewer-incident-detail.schema.json */
export interface IncidentDetail {
  schema_version: "1.0.0";
  incident: Incident;
  evidence: EvidenceItem[];
  contexts: ContextPackage[];
  reports: ReportBundle[];
  agent_runs: AgentRunAudit[];
  timeline: TimelineEvent[];
  truncated: TruncationFlags;
}

export type WorkState = "READY" | "RUNNING" | "SUCCEEDED" | "FAILED";

export type WorkStage = "COLLECTION" | "LOCALIZATION" | "ANALYSIS";

/**
 * One durable work-queue row, exactly as
 * `contracts/schemas/viewer-incident-work-state.schema.json` exposes it.
 *
 * The fenced `claim_token` is deliberately absent: it is a write capability and
 * the read-only projection never returns it.
 */
export interface WorkQueueState {
  stage: WorkStage;
  state: WorkState;
  available_at: string;
  attempt_count: number;
  worker_id: string | null;
  lease_expires_at: string | null;
  claimed_at: string | null;
  completed_at: string | null;
  last_error_code: string | null;
  /** Only the analysis queue pins an immutable Context Package. */
  context_id: string | null;
}

/** contracts/schemas/viewer-incident-work-state.schema.json */
export interface IncidentWorkState {
  schema_version: "1.0.0";
  incident_id: string;
  collection: WorkQueueState | null;
  localization: WorkQueueState | null;
  analysis: WorkQueueState | null;
}
