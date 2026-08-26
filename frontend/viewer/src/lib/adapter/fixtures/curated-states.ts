/**
 * Fixture Incidents for the non-happy-path states the Viewer must render
 * honestly: a disabled Agent runtime, an ABSTAIN report, insufficient Evidence,
 * a failed collection, two in-flight runs, and a truncated detail bundle.
 */
import type {
  AgentRunAudit,
  ContextPackage,
  EvidenceItem,
  Incident,
  RcaReport,
} from "@/lib/types";
import {
  clock,
  k8s,
  makeEvidence,
  stableHash,
  workItem,
  type AuditEventFixture,
  type FixtureRecord,
} from "./helpers";

const NS = "online-boutique";
const CLUSTER = "agent-rca-local";

/* ------------------------------------------------------------------ *
 * 2. inc-cartservice-0002 — ANALYZING with the Agent runtime disabled.
 *    Analysis work sits READY on a pinned Context. Not an error.
 * ------------------------------------------------------------------ */

const t2 = clock("2026-07-27T02:14:00Z");
const cartPod = k8s("Pod", "cartservice-6d4f7c85b-9wq2t", NS, { uid: "8f1c-cart-9wq2t" });
const cartDeployment = k8s("Deployment", "cartservice", NS, { apiVersion: "apps/v1" });
const redisService = k8s("Service", "redis-cart", NS);

const cartEvidence: EvidenceItem[] = [
  makeEvidence({
    evidence_id: "ev-cart-lat-01",
    incident_id: "inc-cartservice-0002",
    source: "prometheus",
    kind: "metric-summary",
    observed_at: t2(3),
    window: { start: t2(-1800), end: t2(3) },
    subject: cartDeployment,
    summary:
      "cartservice p99 latency rose from 48ms to 2.9s at 02:09Z and has not recovered.",
    facts: {
      metric: "grpc_server_handling_seconds",
      quantile: 0.99,
      baseline_seconds: 0.048,
      incident_seconds: 2.91,
      change_at: t2(-300),
    },
    provenance: {
      provider: "prometheus-api",
      query:
        'histogram_quantile(0.99, sum by (le) (rate(grpc_server_handling_seconds_bucket{app="cartservice"}[5m])))',
      locator: "prom://range/grpc_server_handling_seconds",
      collected_at: t2(3),
    },
    quality: { freshness: "live", completeness: 1, confidence: 0.92 },
    redactions: [],
  }),
  makeEvidence({
    evidence_id: "ev-cart-redis-01",
    incident_id: "inc-cartservice-0002",
    source: "prometheus",
    kind: "metric-summary",
    observed_at: t2(4),
    window: { start: t2(-1800), end: t2(4) },
    subject: redisService,
    summary:
      "redis-cart connected_clients hit the 64-client maxclients ceiling at 02:09Z, with 41 rejected connections.",
    facts: {
      metric: "redis_connected_clients",
      maxclients: 64,
      connected_clients: 64,
      rejected_connections: 41,
    },
    provenance: {
      provider: "prometheus-api",
      query: 'redis_connected_clients{namespace="online-boutique",service="redis-cart"}',
      locator: "prom://range/redis_connected_clients",
      collected_at: t2(4),
    },
    quality: { freshness: "live", completeness: 1, confidence: 0.88 },
    redactions: [],
  }),
  makeEvidence({
    evidence_id: "ev-cart-pod-01",
    incident_id: "inc-cartservice-0002",
    source: "kubernetes",
    kind: "resource-state",
    observed_at: t2(5),
    window: { start: t2(-1800), end: t2(5) },
    subject: cartPod,
    summary:
      "cartservice Pods are Ready but report elevated goroutine counts; no restarts in the window.",
    facts: { phase: "Running", ready: true, restart_count: 0, ready_replicas: 2 },
    provenance: {
      provider: "kubernetes-api",
      query: "get pods -n online-boutique -l app=cartservice -o json",
      locator: "k8s://online-boutique/Pod/cartservice-6d4f7c85b-9wq2t",
      collected_at: t2(5),
    },
    quality: { freshness: "live", completeness: 1, confidence: 1 },
    redactions: [],
  }),
  makeEvidence({
    evidence_id: "ev-cart-flow-01",
    incident_id: "inc-cartservice-0002",
    source: "network",
    kind: "network-flow-summary",
    observed_at: t2(6),
    window: { start: t2(-900), end: t2(6) },
    subject: redisService,
    summary:
      "cartservice → redis-cart flows show 41 connection resets during the incident window.",
    facts: { source: "cartservice", destination: "redis-cart", resets: 41, verdict: "FORWARDED" },
    provenance: {
      provider: "hubble",
      query: 'observe --namespace online-boutique --to-service redis-cart',
      locator: "hubble://online-boutique/redis-cart",
      collected_at: t2(6),
    },
    quality: { freshness: "recent", completeness: 0.8, confidence: 0.7 },
    redactions: [],
  }),
];

const cartContext: ContextPackage = {
  schema_version: "1.0.0",
  context_id: "ctx-cart-0002",
  incident_id: "inc-cartservice-0002",
  frozen_at: t2(9),
  source_entity: cartPod,
  scope: {
    incident_id: "inc-cartservice-0002",
    seed_entity_ids: ["ent-k8s-pod-cartservice-9wq2t"],
    domains: ["kubernetes"],
    correlation_keys: { namespace: NS, service: "cartservice" },
    relation_types: ["CALLS", "OWNS", "ROUTES_TO"],
    time_window: { start: t2(-1800), end: t2(9) },
    max_entities: 100,
    max_depth: 3,
  },
  state_paths: [
    {
      path_id: "path-frontend-cart",
      entities: [k8s("Service", "frontend", NS), k8s("Service", "cartservice", NS)],
      relations: ["CALLS"],
      evidence_ids: ["ev-cart-lat-01"],
    },
    {
      path_id: "path-cart-redis",
      entities: [k8s("Service", "cartservice", NS), redisService],
      relations: ["CALLS"],
      evidence_ids: ["ev-cart-redis-01", "ev-cart-flow-01"],
    },
    {
      path_id: "path-deployment-pod",
      entities: [cartDeployment, cartPod],
      relations: ["OWNS"],
      evidence_ids: ["ev-cart-pod-01"],
    },
  ],
  evidence_ids: ["ev-cart-lat-01", "ev-cart-redis-01", "ev-cart-pod-01", "ev-cart-flow-01"],
  recent_change_evidence_ids: [],
  missing_evidence: [
    { source: "logs", reason: "No log Provider is configured for this cluster." },
  ],
  collector_failures: [],
  localization: {
    strategy: "stategraph",
    candidate_entities_before: 71,
    candidate_entities_after: 6,
    context_completeness: 0.83,
  },
};

const cartAudit: AuditEventFixture[] = [
  {
    occurred_at: t2(0),
    event_type: "INCIDENT_CREATED",
    details: { source: "alertmanager", alert: "CartServiceHighLatency", severity: "critical" },
  },
  { occurred_at: t2(1), event_type: "STATUS_TRANSITIONED", details: { from: "RECEIVED", to: "COLLECTING" } },
  {
    occurred_at: t2(1),
    event_type: "COLLECTION_CLAIMED",
    details: { worker_id: "incident-worker-0", attempt_count: 1 },
  },
  {
    occurred_at: t2(7),
    event_type: "COLLECTION_COMPLETED",
    details: { outcome: "SUCCEEDED", evidence_count: 4 },
  },
  { occurred_at: t2(8), event_type: "STATUS_TRANSITIONED", details: { from: "COLLECTING", to: "LOCALIZING" } },
  {
    occurred_at: t2(10),
    event_type: "LOCALIZATION_COMPLETED",
    details: { outcome: "SUCCEEDED", context_id: "ctx-cart-0002", strategy: "stategraph" },
  },
  { occurred_at: t2(11), event_type: "STATUS_TRANSITIONED", details: { from: "LOCALIZING", to: "ANALYZING" } },
];

export const CART_RECORD: FixtureRecord = {
  incident: {
    schema_version: "1.0.0",
    incident_id: "inc-cartservice-0002",
    deduplication_key: "alertmanager:CartServiceHighLatency:online-boutique:cartservice",
    status: "ANALYZING",
    severity: "critical",
    source: "alertmanager",
    triggered_at: t2(0),
    window: {
      baseline_start: t2(-1800),
      incident_start: t2(-300),
      incident_end: null,
      recovery_end: null,
    },
    alert: {
      fingerprint: "7c1e93aa5b08",
      name: "CartServiceHighLatency",
      labels: {
        alertname: "CartServiceHighLatency",
        namespace: NS,
        service: "cartservice",
        severity: "critical",
        cluster: CLUSTER,
      },
      annotations: { summary: "cartservice p99 latency above 1s for 5 minutes" },
    },
    source_entity: cartPod,
    collector_statuses: [
      { collector: "kubernetes", status: "SUCCEEDED", attempts: 1, started_at: t2(1), ended_at: t2(5), error: null },
      { collector: "prometheus", status: "SUCCEEDED", attempts: 1, started_at: t2(1), ended_at: t2(4), error: null },
      { collector: "hubble", status: "SUCCEEDED", attempts: 1, started_at: t2(1), ended_at: t2(6), error: null },
      { collector: "logs", status: "SKIPPED", attempts: 0, started_at: null, ended_at: null, error: null },
    ],
    created_at: t2(0),
    updated_at: t2(11),
  },
  evidence: cartEvidence,
  contexts: [cartContext],
  reports: [],
  agentRuns: [],
  auditEvents: cartAudit,
  work: {
    schema_version: "1.0.0",
    incident_id: "inc-cartservice-0002",
    collection: workItem({
      stage: "COLLECTION", state: "SUCCEEDED", available_at: t2(1), attempt_count: 1,
      worker_id: "incident-worker-0", claimed_at: t2(1), completed_at: t2(7),
    }),
    localization: workItem({
      stage: "LOCALIZATION", state: "SUCCEEDED", available_at: t2(8), attempt_count: 1,
      worker_id: "incident-worker-0", claimed_at: t2(8), completed_at: t2(10),
    }),
    // Pinned to a frozen Context, never claimed: no Agent runtime is draining it.
    analysis: workItem({
      stage: "ANALYSIS", state: "READY", available_at: t2(11),
      context_id: "ctx-cart-0002",
    }),
  },
};

/* ------------------------------------------------------------------ *
 * 3. inc-frontend-0003 — REPORTED, but the Agent abstained.
 *    Evidence gate rejected the run; the report carries no root cause.
 * ------------------------------------------------------------------ */

const t3 = clock("2026-07-26T22:41:00Z");
const frontendPod = k8s("Pod", "frontend-5f9c7d4b6-2mnhs", NS, { uid: "4b21-frontend-2mnhs" });

const frontendEvidence: EvidenceItem[] = [
  makeEvidence({
    evidence_id: "ev-frontend-5xx-01",
    incident_id: "inc-frontend-0003",
    source: "prometheus",
    kind: "metric-summary",
    observed_at: t3(4),
    window: { start: t3(-1800), end: t3(4) },
    subject: k8s("Service", "frontend", NS),
    summary:
      "frontend 5xx ratio spiked to 12% for 90 seconds at 22:38Z and recovered on its own.",
    facts: {
      metric: "istio_requests_total",
      peak_ratio: 0.12,
      duration_seconds: 90,
      recovered: true,
    },
    provenance: {
      provider: "prometheus-api",
      query: 'sum(rate(istio_requests_total{destination_service="frontend",response_code=~"5.."}[1m]))',
      locator: "prom://range/istio_requests_total",
      collected_at: t3(4),
    },
    quality: { freshness: "live", completeness: 1, confidence: 0.8 },
    redactions: [],
  }),
  makeEvidence({
    evidence_id: "ev-frontend-pod-01",
    incident_id: "inc-frontend-0003",
    source: "kubernetes",
    kind: "resource-state",
    observed_at: t3(5),
    window: { start: t3(-1800), end: t3(5) },
    subject: frontendPod,
    summary: "All frontend Pods stayed Ready with zero restarts across the window.",
    facts: { phase: "Running", ready_replicas: 3, restart_count: 0 },
    provenance: {
      provider: "kubernetes-api",
      query: "get pods -n online-boutique -l app=frontend -o json",
      locator: "k8s://online-boutique/Pod/frontend-5f9c7d4b6-2mnhs",
      collected_at: t3(5),
    },
    quality: { freshness: "live", completeness: 1, confidence: 1 },
    redactions: [],
  }),
];

const frontendContext: ContextPackage = {
  schema_version: "1.0.0",
  context_id: "ctx-frontend-003",
  incident_id: "inc-frontend-0003",
  frozen_at: t3(9),
  source_entity: frontendPod,
  scope: {
    incident_id: "inc-frontend-0003",
    seed_entity_ids: ["ent-k8s-pod-frontend-2mnhs"],
    domains: ["kubernetes"],
    correlation_keys: { namespace: NS, service: "frontend" },
    relation_types: ["CALLS"],
    time_window: { start: t3(-1800), end: t3(9) },
    max_entities: 100,
    max_depth: 2,
  },
  state_paths: [
    {
      path_id: "path-frontend-cart",
      entities: [k8s("Service", "frontend", NS), k8s("Service", "cartservice", NS)],
      relations: ["CALLS"],
      evidence_ids: ["ev-frontend-5xx-01"],
    },
  ],
  evidence_ids: ["ev-frontend-5xx-01", "ev-frontend-pod-01"],
  recent_change_evidence_ids: [],
  missing_evidence: [
    { source: "trace", reason: "No trace Provider is configured; the failing span could not be identified." },
    { source: "logs", reason: "Log retention window ended before collection started." },
  ],
  collector_failures: [
    { collector: "trace", error: "TRACE_PROVIDER_NOT_CONFIGURED" },
  ],
  localization: {
    strategy: "namespace-fallback",
    candidate_entities_before: 71,
    candidate_entities_after: 22,
    context_completeness: 0.4,
  },
};

const frontendReport: RcaReport = {
  schema_version: "1.0.0",
  report_id: "rpt-frontend-003",
  incident_id: "inc-frontend-0003",
  context_id: "ctx-frontend-003",
  path: "normal",
  status: "inconclusive",
  generated_at: t3(52),
  root_cause: null,
  hypotheses: [
    {
      rank: 1,
      summary:
        "A transient upstream dependency error caused the 90-second 5xx burst, but no Evidence identifies which dependency.",
      entity: k8s("Service", "frontend", NS),
      confidence: 0.31,
      status: "unresolved",
      supporting_evidence_ids: ["ev-frontend-5xx-01"],
      contradicting_evidence_ids: [],
      reference_document_ids: [],
      missing_evidence: [
        "trace-summary for the 22:38Z window",
        "log-pattern Evidence from frontend containers",
      ],
    },
    {
      rank: 2,
      summary: "A frontend Pod restart or eviction dropped in-flight requests.",
      entity: frontendPod,
      confidence: 0.06,
      status: "rejected",
      supporting_evidence_ids: [],
      contradicting_evidence_ids: ["ev-frontend-pod-01"],
      reference_document_ids: [],
      missing_evidence: [],
    },
  ],
  remediation: {
    suggestions: [
      "Enable a trace Provider for online-boutique so the failing dependency span is captured on the next occurrence.",
    ],
    verification_conditions: [
      "A trace Provider returns spans for frontend during a subsequent 5xx burst.",
    ],
  },
  budget: {
    applicable: true,
    llm_calls: 1,
    tool_calls: 2,
    tree_depth: 1,
    wall_time_ms: 9400,
    exhausted: false,
  },
  read_only: true,
  limitations: [
    "The Evidence gate rejected a conclusion: two independent Evidence sources are required and only Kubernetes state was available.",
    "The incident self-recovered before localization completed, so no post-incident state was collected.",
  ],
};

const frontendAgentRun: AgentRunAudit = {
  schema_version: "1.0.0",
  agent_run_id: "arun-frontend-03",
  incident_id: "inc-frontend-0003",
  context_id: "ctx-frontend-003",
  knowledge_audit_id: "kaud-frontend-03",
  knowledge_status: "NO_MATCH",
  model: "gpt-4.1-mini",
  status: "GATE_REJECTED",
  reason_code: "EVIDENCE_GATE_REJECTED",
  started_at: t3(42),
  completed_at: t3(51),
  budget: {
    max_turns: 8, max_llm_calls: 8, max_tool_calls: 16,
    max_output_tokens: 4000, max_wall_time_ms: 120000,
  },
  usage: {
    llm_calls: 1, tool_calls: 2, input_tokens: 7420,
    output_tokens: 612, total_tokens: 8032, wall_time_ms: 9400,
  },
  tool_events: [
    {
      sequence: 1, tool_name: "inspect_evidence", requested_id: "ev-frontend-5xx-01",
      status: "SUCCEEDED", result_hash: stableHash("f3:1"),
    },
    {
      sequence: 2, tool_name: "inspect_evidence", requested_id: "ev-frontend-pod-01",
      status: "SUCCEEDED", result_hash: stableHash("f3:2"),
    },
  ],
  retrieved_reference_ids: [],
  inspected_evidence_ids: ["ev-frontend-5xx-01", "ev-frontend-pod-01"],
  inspected_reference_document_ids: [],
  cited_evidence_ids: ["ev-frontend-5xx-01"],
  cited_reference_document_ids: [],
};

export const FRONTEND_RECORD: FixtureRecord = {
  incident: {
    schema_version: "1.0.0",
    incident_id: "inc-frontend-0003",
    deduplication_key: "alertmanager:FrontendErrorRateSpike:online-boutique:frontend",
    status: "REPORTED",
    severity: "warning",
    source: "alertmanager",
    triggered_at: t3(0),
    window: {
      baseline_start: t3(-1800), incident_start: t3(-180),
      incident_end: t3(-90), recovery_end: t3(-60),
    },
    alert: {
      fingerprint: "b8043fc2e991",
      name: "FrontendErrorRateSpike",
      labels: {
        alertname: "FrontendErrorRateSpike", namespace: NS, service: "frontend",
        severity: "warning", cluster: CLUSTER,
      },
      annotations: { summary: "frontend 5xx ratio above 5% for 1 minute" },
    },
    source_entity: frontendPod,
    collector_statuses: [
      { collector: "kubernetes", status: "SUCCEEDED", attempts: 1, started_at: t3(1), ended_at: t3(5), error: null },
      { collector: "prometheus", status: "SUCCEEDED", attempts: 1, started_at: t3(1), ended_at: t3(4), error: null },
      {
        collector: "trace", status: "FAILED", attempts: 3, started_at: t3(1), ended_at: t3(7),
        error: "TRACE_PROVIDER_NOT_CONFIGURED",
      },
      {
        collector: "logs", status: "TIMED_OUT", attempts: 2, started_at: t3(1), ended_at: t3(8),
        error: "LOG_QUERY_DEADLINE_EXCEEDED",
      },
    ],
    created_at: t3(0),
    updated_at: t3(53),
  },
  evidence: frontendEvidence,
  contexts: [frontendContext],
  reports: [
    {
      report: frontendReport,
      markdown: `# RCA Report — inc-frontend-0003

## Conclusion
**ABSTAIN** — the Evidence gate rejected a root-cause conclusion.

Two independent Evidence sources are required to name a root cause. Only
Kubernetes resource state was available: the trace Provider is not configured and
the log query exceeded its deadline.

## What is known
- frontend 5xx ratio reached 12% for 90 seconds and recovered without intervention.
- No frontend Pod restarted during the window.

## Missing evidence
- trace-summary for the 22:38Z window
- log-pattern Evidence from frontend containers

_This report is read-only and states no root cause._
`,
    },
  ],
  agentRuns: [frontendAgentRun],
  auditEvents: [
    {
      occurred_at: t3(0), event_type: "INCIDENT_CREATED",
      details: { source: "alertmanager", alert: "FrontendErrorRateSpike", severity: "warning" },
    },
    { occurred_at: t3(1), event_type: "STATUS_TRANSITIONED", details: { from: "RECEIVED", to: "COLLECTING" } },
    {
      occurred_at: t3(7), event_type: "COLLECTION_FAILED",
      details: { collector: "trace", error_code: "TRACE_PROVIDER_NOT_CONFIGURED", attempts: 3 },
    },
    {
      occurred_at: t3(8), event_type: "COLLECTION_COMPLETED",
      details: { outcome: "PARTIAL", evidence_count: 2, failed_collectors: 2 },
    },
    { occurred_at: t3(9), event_type: "STATUS_TRANSITIONED", details: { from: "COLLECTING", to: "LOCALIZING" } },
    {
      occurred_at: t3(10), event_type: "LOCALIZATION_COMPLETED",
      details: { outcome: "SUCCEEDED", context_id: "ctx-frontend-003", strategy: "namespace-fallback" },
    },
    { occurred_at: t3(11), event_type: "STATUS_TRANSITIONED", details: { from: "LOCALIZING", to: "ANALYZING" } },
    {
      occurred_at: t3(52), event_type: "ANALYSIS_COMPLETED",
      details: { outcome: "SUCCEEDED", report_id: "rpt-frontend-003", conclusion: "inconclusive" },
    },
    { occurred_at: t3(53), event_type: "STATUS_TRANSITIONED", details: { from: "ANALYZING", to: "REPORTED" } },
  ],
  work: {
    schema_version: "1.0.0",
    incident_id: "inc-frontend-0003",
    collection: workItem({
      stage: "COLLECTION", state: "SUCCEEDED", available_at: t3(1), attempt_count: 1,
      worker_id: "incident-worker-1", claimed_at: t3(1), completed_at: t3(8),
      last_error_code: "TRACE_PROVIDER_NOT_CONFIGURED",
    }),
    localization: workItem({
      stage: "LOCALIZATION", state: "SUCCEEDED", available_at: t3(9), attempt_count: 1,
      worker_id: "incident-worker-1", claimed_at: t3(9), completed_at: t3(10),
    }),
    analysis: workItem({
      stage: "ANALYSIS", state: "SUCCEEDED", available_at: t3(11), attempt_count: 1,
      worker_id: "agent-worker-0", claimed_at: t3(42), completed_at: t3(52),
      context_id: "ctx-frontend-003",
    }),
  },
};
