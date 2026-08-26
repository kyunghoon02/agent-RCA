/**
 * Curated fixture Incidents.
 *
 * Each record exists to make one operational state reachable without a backend:
 * a clean REPORTED run, an Incident parked on a disabled Agent runtime, an
 * ABSTAIN report, insufficient Evidence, a hard collection failure, two
 * in-flight runs, and a truncated detail bundle.
 *
 * Nothing here is randomised. Timestamps are anchored offsets so every reload
 * renders identical bytes.
 */
import type {
  AgentRunAudit,
  ContextPackage,
  EvidenceItem,
  Incident,
  RcaReport,
  ReportBundle,
} from "@/lib/types";
import {
  buildTimeline,
  clock,
  k8s,
  makeEvidence as evidence,
  stableHash,
  workItem,
  type AuditEventFixture,
  type FixtureRecord,
} from "./helpers";

const NS = "online-boutique";
const CLUSTER = "agent-rca-local";

/* ------------------------------------------------------------------ *
 * 1. inc-checkout-0001 — REPORTED, conclusive root cause.
 * ------------------------------------------------------------------ */

const t1 = clock("2026-07-27T01:05:00Z");

const checkoutPod = k8s("Pod", "checkoutservice-7b6c8d9f-x4k2p", NS, {
  uid: "2d0f9ab3-8c18-4ffc-a453-f4cc13e457e1",
});
const checkoutDeployment = k8s("Deployment", "checkoutservice", NS, {
  apiVersion: "apps/v1",
  uid: "9e21c6a4-11bd-4f30-9d2e-6cb0f3a71d55",
});
const checkoutConfigMap = k8s("ConfigMap", "checkout-settings", NS, {
  uid: null,
  exists: false,
});

const checkoutEvidence: EvidenceItem[] = [
  evidence({
    evidence_id: "ev-checkout-cm-01",
    incident_id: "inc-checkout-0001",
    source: "kubernetes",
    kind: "resource-state",
    observed_at: t1(2),
    window: { start: t1(-300), end: t1(2) },
    subject: checkoutConfigMap,
    summary:
      "checkoutservice Pod references ConfigMap checkout-settings via envFrom, but the ConfigMap does not exist in namespace online-boutique.",
    facts: {
      reference_path: "spec.template.spec.containers[0].envFrom[0].configMapRef",
      referenced_by: "checkoutservice-7b6c8d9f-x4k2p",
      required: true,
      resolved: false,
    },
    provenance: {
      provider: "kubernetes-api",
      query: "get configmap checkout-settings -n online-boutique",
      locator: "k8s://online-boutique/ConfigMap/checkout-settings",
      collected_at: t1(2),
    },
    quality: { freshness: "live", completeness: 1, confidence: 1 },
    redactions: [],
  }),
  evidence({
    evidence_id: "ev-checkout-pod-01",
    incident_id: "inc-checkout-0001",
    source: "kubernetes",
    kind: "resource-state",
    observed_at: t1(3),
    window: { start: t1(-300), end: t1(3) },
    subject: checkoutPod,
    summary:
      "Pod is in CreateContainerConfigError with 6 restarts; container checkout has never reached Ready.",
    facts: {
      phase: "Pending",
      container_state: "CreateContainerConfigError",
      restart_count: 6,
      ready_replicas: 0,
      desired_replicas: 2,
    },
    provenance: {
      provider: "kubernetes-api",
      query: "get pod checkoutservice-7b6c8d9f-x4k2p -n online-boutique -o json",
      locator: "k8s://online-boutique/Pod/checkoutservice-7b6c8d9f-x4k2p",
      collected_at: t1(3),
    },
    quality: { freshness: "live", completeness: 1, confidence: 1 },
    redactions: ["spec.template.spec.containers[0].env[*].valueFrom.secretKeyRef"],
  }),
  evidence({
    evidence_id: "ev-checkout-evt-01",
    incident_id: "inc-checkout-0001",
    source: "kubernetes",
    kind: "kubernetes-event",
    observed_at: t1(4),
    window: { start: t1(-300), end: t1(4) },
    subject: checkoutPod,
    summary:
      'Kubelet emitted 12 Warning events: configmap "checkout-settings" not found.',
    facts: {
      reason: "Failed",
      count: 12,
      first_seen: t1(-240),
      last_seen: t1(4),
      message: 'configmap "checkout-settings" not found',
    },
    provenance: {
      provider: "kubernetes-api",
      query: "get events -n online-boutique --field-selector involvedObject.name=checkoutservice-7b6c8d9f-x4k2p",
      locator: "k8s://online-boutique/Event/checkoutservice-7b6c8d9f-x4k2p",
      collected_at: t1(4),
    },
    quality: { freshness: "live", completeness: 1, confidence: 1 },
    redactions: [],
  }),
  evidence({
    evidence_id: "ev-checkout-dep-01",
    incident_id: "inc-checkout-0001",
    source: "deployment",
    kind: "deployment-change",
    observed_at: t1(5),
    window: { start: t1(-1800), end: t1(5) },
    subject: checkoutDeployment,
    summary:
      "Deployment checkoutservice rolled to revision 8 at 00:58Z, 2 minutes before the alert, adding an envFrom ConfigMap reference.",
    facts: {
      revision: 8,
      previous_revision: 7,
      rolled_at: t1(-420),
      changed_fields: ["spec.template.spec.containers[0].envFrom"],
    },
    provenance: {
      provider: "kubernetes-api",
      query: "rollout history deployment/checkoutservice -n online-boutique",
      locator: "k8s://online-boutique/Deployment/checkoutservice#revision-8",
      collected_at: t1(5),
    },
    quality: { freshness: "live", completeness: 1, confidence: 0.95 },
    redactions: [],
  }),
  evidence({
    evidence_id: "ev-checkout-prom-01",
    incident_id: "inc-checkout-0001",
    source: "prometheus",
    kind: "metric-summary",
    observed_at: t1(6),
    window: { start: t1(-1800), end: t1(6) },
    subject: checkoutDeployment,
    summary:
      "kube_deployment_status_replicas_available for checkoutservice fell from 2 to 0 at 01:00Z and stayed at 0.",
    facts: {
      metric: "kube_deployment_status_replicas_available",
      baseline_value: 2,
      incident_value: 0,
      change_at: t1(-300),
      samples: 60,
    },
    provenance: {
      provider: "prometheus-api",
      query:
        'kube_deployment_status_replicas_available{namespace="online-boutique",deployment="checkoutservice"}',
      locator: "prom://range/kube_deployment_status_replicas_available",
      collected_at: t1(6),
    },
    quality: { freshness: "live", completeness: 1, confidence: 0.9 },
    redactions: [],
  }),
  evidence({
    evidence_id: "ev-checkout-prom-02",
    incident_id: "inc-checkout-0001",
    source: "prometheus",
    kind: "metric-summary",
    observed_at: t1(7),
    window: { start: t1(-1800), end: t1(7) },
    subject: k8s("Service", "frontend", NS),
    summary:
      "frontend 5xx ratio rose from 0.1% to 34% within the incident window, concentrated on the /cart/checkout route.",
    facts: {
      metric: "istio_requests_total",
      baseline_ratio: 0.001,
      incident_ratio: 0.34,
      route: "/cart/checkout",
    },
    provenance: {
      provider: "prometheus-api",
      query:
        'sum(rate(istio_requests_total{destination_service="frontend",response_code=~"5.."}[5m]))',
      locator: "prom://range/istio_requests_total",
      collected_at: t1(7),
    },
    quality: { freshness: "live", completeness: 1, confidence: 0.85 },
    redactions: [],
  }),
];

const checkoutContext: ContextPackage = {
  schema_version: "1.0.0",
  context_id: "ctx-checkout-0001",
  incident_id: "inc-checkout-0001",
  frozen_at: t1(9),
  source_entity: checkoutPod,
  scope: {
    incident_id: "inc-checkout-0001",
    seed_entity_ids: ["ent-k8s-pod-checkoutservice-x4k2p"],
    domains: ["kubernetes"],
    correlation_keys: { namespace: NS, service: "checkoutservice" },
    relation_types: ["REFERENCES", "OWNS", "ROUTES_TO"],
    time_window: { start: t1(-1800), end: t1(9) },
    max_entities: 100,
    max_depth: 3,
  },
  state_paths: [
    {
      path_id: "path-pod-configmap",
      entities: [checkoutPod, checkoutConfigMap],
      relations: ["REFERENCES"],
      evidence_ids: ["ev-checkout-cm-01", "ev-checkout-pod-01", "ev-checkout-evt-01"],
    },
    {
      path_id: "path-deployment-pod",
      entities: [
        checkoutDeployment,
        k8s("ReplicaSet", "checkoutservice-7b6c8d9f", NS, { apiVersion: "apps/v1" }),
        checkoutPod,
      ],
      relations: ["OWNS", "OWNS"],
      evidence_ids: ["ev-checkout-dep-01", "ev-checkout-prom-01"],
    },
    {
      path_id: "path-frontend-checkout",
      entities: [
        k8s("Service", "frontend", NS),
        k8s("Service", "checkoutservice", NS),
        checkoutPod,
      ],
      relations: ["CALLS", "ROUTES_TO"],
      evidence_ids: ["ev-checkout-prom-02"],
    },
  ],
  evidence_ids: [
    "ev-checkout-cm-01",
    "ev-checkout-pod-01",
    "ev-checkout-evt-01",
    "ev-checkout-dep-01",
    "ev-checkout-prom-01",
    "ev-checkout-prom-02",
  ],
  recent_change_evidence_ids: ["ev-checkout-dep-01"],
  missing_evidence: [],
  collector_failures: [],
  localization: {
    strategy: "stategraph",
    candidate_entities_before: 68,
    candidate_entities_after: 7,
    context_completeness: 1,
  },
};
const checkoutReport: RcaReport = {
  schema_version: "1.0.0",
  report_id: "rpt-checkout-0001",
  incident_id: "inc-checkout-0001",
  context_id: "ctx-checkout-0001",
  path: "normal",
  status: "conclusive",
  generated_at: t1(64),
  root_cause: {
    summary:
      "Deployment revision 8 added an envFrom reference to ConfigMap checkout-settings, which was never created in online-boutique. Every checkoutservice Pod fails container creation with CreateContainerConfigError, dropping available replicas to 0 and surfacing as frontend 5xx on /cart/checkout.",
    entity: checkoutConfigMap,
    supporting_evidence_ids: [
      "ev-checkout-cm-01",
      "ev-checkout-pod-01",
      "ev-checkout-evt-01",
      "ev-checkout-dep-01",
    ],
    reference_document_ids: ["ref-k8s-configmap-01"],
  },
  hypotheses: [
    {
      rank: 1,
      summary:
        "Missing ConfigMap checkout-settings referenced by Deployment revision 8 blocks container creation.",
      entity: checkoutConfigMap,
      confidence: 0.94,
      status: "supported",
      supporting_evidence_ids: [
        "ev-checkout-cm-01",
        "ev-checkout-pod-01",
        "ev-checkout-evt-01",
        "ev-checkout-dep-01",
      ],
      contradicting_evidence_ids: [],
      reference_document_ids: ["ref-k8s-configmap-01"],
      missing_evidence: [],
    },
    {
      rank: 2,
      summary:
        "Node-level resource pressure prevented checkoutservice Pods from being scheduled.",
      entity: k8s("Node", "gke-agent-rca-local-default-0", null, { apiVersion: null }),
      confidence: 0.08,
      status: "rejected",
      supporting_evidence_ids: [],
      contradicting_evidence_ids: ["ev-checkout-pod-01", "ev-checkout-evt-01"],
      reference_document_ids: [],
      missing_evidence: [],
    },
    {
      rank: 3,
      summary:
        "Upstream dependency paymentservice degraded and propagated errors to checkout.",
      entity: k8s("Service", "paymentservice", NS),
      confidence: 0.05,
      status: "rejected",
      supporting_evidence_ids: [],
      contradicting_evidence_ids: ["ev-checkout-prom-01"],
      reference_document_ids: [],
      missing_evidence: ["paymentservice trace-summary for the incident window"],
    },
  ],
  remediation: {
    suggestions: [
      "Create ConfigMap checkout-settings in online-boutique with the keys required by revision 8, or roll checkoutservice back to revision 7.",
      "Add a pre-rollout check that every envFrom configMapRef resolves before the Deployment is applied.",
    ],
    verification_conditions: [
      "kube_deployment_status_replicas_available for checkoutservice returns to 2 and holds for 10 minutes.",
      "No further Failed events referencing checkout-settings appear in online-boutique.",
      "frontend 5xx ratio on /cart/checkout returns below 0.5%.",
    ],
  },
  budget: {
    applicable: true,
    llm_calls: 3,
    tool_calls: 6,
    tree_depth: 2,
    wall_time_ms: 41200,
    exhausted: false,
  },
  read_only: true,
  limitations: [
    "Trace Evidence was not collected for this incident; the frontend impact path is inferred from request metrics only.",
    "Analysis is bounded to the frozen Context and did not re-query the cluster.",
  ],
};

const checkoutReportMarkdown = `# RCA Report — inc-checkout-0001

## Conclusion
**conclusive** — Deployment revision 8 introduced an unresolvable ConfigMap reference.

## Root cause
Deployment \`checkoutservice\` revision 8 added \`envFrom.configMapRef: checkout-settings\`.
That ConfigMap does not exist in \`online-boutique\`, so every Pod fails container
creation with \`CreateContainerConfigError\` and available replicas fall to 0.

## Supporting evidence
- ev-checkout-cm-01 — ConfigMap checkout-settings not found
- ev-checkout-pod-01 — Pod in CreateContainerConfigError, 6 restarts
- ev-checkout-evt-01 — 12 kubelet Warning events
- ev-checkout-dep-01 — rollout to revision 8 at 00:58Z

## Verification
- Available replicas return to 2 for 10 minutes
- No further Failed events referencing checkout-settings
- frontend 5xx ratio on /cart/checkout below 0.5%

_This report is read-only. It proposes no automated remediation._
`;

const checkoutAgentRun: AgentRunAudit = {
  schema_version: "1.0.0",
  agent_run_id: "arun-checkout-0001",
  incident_id: "inc-checkout-0001",
  context_id: "ctx-checkout-0001",
  knowledge_audit_id: "kaud-checkout-0001",
  knowledge_status: "SUCCEEDED",
  model: "gpt-4.1-mini",
  status: "SUCCEEDED",
  reason_code: "REPORT_ACCEPTED",
  started_at: t1(22),
  completed_at: t1(63),
  budget: {
    max_turns: 8,
    max_llm_calls: 8,
    max_tool_calls: 16,
    max_output_tokens: 4000,
    max_wall_time_ms: 120000,
  },
  usage: {
    llm_calls: 3,
    tool_calls: 6,
    input_tokens: 18432,
    output_tokens: 1876,
    total_tokens: 20308,
    wall_time_ms: 41200,
  },
  tool_events: [
    {
      sequence: 1,
      tool_name: "inspect_evidence",
      requested_id: "ev-checkout-cm-01",
      status: "SUCCEEDED",
      result_hash: stableHash("tool:1:ev-checkout-cm-01"),
    },
    {
      sequence: 2,
      tool_name: "inspect_evidence",
      requested_id: "ev-checkout-dep-01",
      status: "SUCCEEDED",
      result_hash: stableHash("tool:2:ev-checkout-dep-01"),
    },
    {
      sequence: 3,
      tool_name: "inspect_reference",
      requested_id: "ref-k8s-configmap-01",
      status: "SUCCEEDED",
      result_hash: stableHash("tool:3:ref-k8s-configmap-01"),
    },
    {
      sequence: 4,
      tool_name: "inspect_evidence",
      requested_id: "ev-checkout-evt-01",
      status: "SUCCEEDED",
      result_hash: stableHash("tool:4:ev-checkout-evt-01"),
    },
    {
      sequence: 5,
      tool_name: "inspect_evidence",
      requested_id: "ev-payment-secret-99",
      status: "NOT_FOUND",
      result_hash: stableHash("tool:5:not-found"),
    },
    {
      sequence: 6,
      tool_name: "inspect_evidence",
      requested_id: "ev-checkout-pod-01",
      status: "SUCCEEDED",
      result_hash: stableHash("tool:6:ev-checkout-pod-01"),
    },
  ],
  retrieved_reference_ids: ["ref-k8s-configmap-01", "ref-k8s-rollout-01"],
  inspected_evidence_ids: [
    "ev-checkout-cm-01",
    "ev-checkout-dep-01",
    "ev-checkout-evt-01",
    "ev-checkout-pod-01",
  ],
  inspected_reference_document_ids: ["ref-k8s-configmap-01"],
  cited_evidence_ids: [
    "ev-checkout-cm-01",
    "ev-checkout-dep-01",
    "ev-checkout-evt-01",
    "ev-checkout-pod-01",
  ],
  cited_reference_document_ids: ["ref-k8s-configmap-01"],
};

const checkoutAudit: AuditEventFixture[] = [
  {
    occurred_at: t1(0),
    event_type: "INCIDENT_CREATED",
    details: { source: "alertmanager", alert: "KubePodNotReady", severity: "critical" },
  },
  {
    occurred_at: t1(1),
    event_type: "STATUS_TRANSITIONED",
    details: { from: "RECEIVED", to: "COLLECTING" },
  },
  {
    occurred_at: t1(1),
    event_type: "COLLECTION_CLAIMED",
    details: { worker_id: "incident-worker-0", attempt_count: 1, lease_seconds: 120 },
  },
  {
    occurred_at: t1(7),
    event_type: "COLLECTION_COMPLETED",
    details: { outcome: "SUCCEEDED", evidence_count: 6, collectors: 2 },
  },
  {
    occurred_at: t1(8),
    event_type: "STATUS_TRANSITIONED",
    details: { from: "COLLECTING", to: "LOCALIZING" },
  },
  {
    occurred_at: t1(8),
    event_type: "LOCALIZATION_CLAIMED",
    details: { worker_id: "incident-worker-0", attempt_count: 1 },
  },
  {
    occurred_at: t1(10),
    event_type: "LOCALIZATION_COMPLETED",
    details: { outcome: "SUCCEEDED", context_id: "ctx-checkout-0001", strategy: "stategraph" },
  },
  {
    occurred_at: t1(11),
    event_type: "STATUS_TRANSITIONED",
    details: { from: "LOCALIZING", to: "ANALYZING" },
  },
  {
    occurred_at: t1(21),
    event_type: "ANALYSIS_CLAIMED",
    details: { worker_id: "agent-worker-0", attempt_count: 1, context_id: "ctx-checkout-0001" },
  },
  {
    occurred_at: t1(64),
    event_type: "ANALYSIS_COMPLETED",
    details: { outcome: "SUCCEEDED", report_id: "rpt-checkout-0001" },
  },
  {
    occurred_at: t1(65),
    event_type: "STATUS_TRANSITIONED",
    details: { from: "ANALYZING", to: "REPORTED" },
  },
];

const checkoutIncident: Incident = {
  schema_version: "1.0.0",
  incident_id: "inc-checkout-0001",
  deduplication_key: "alertmanager:KubePodNotReady:online-boutique:checkoutservice",
  status: "REPORTED",
  severity: "critical",
  source: "alertmanager",
  triggered_at: t1(0),
  window: {
    baseline_start: t1(-1800),
    incident_start: t1(-300),
    incident_end: null,
    recovery_end: null,
  },
  alert: {
    fingerprint: "2d9a2f5c1b42",
    name: "KubePodNotReady",
    labels: {
      alertname: "KubePodNotReady",
      namespace: NS,
      service: "checkoutservice",
      severity: "critical",
      cluster: CLUSTER,
    },
    annotations: {
      summary: "checkoutservice Pod is not Ready",
      description: "Pod checkoutservice-7b6c8d9f-x4k2p has been not Ready for 5 minutes.",
    },
  },
  source_entity: checkoutPod,
  collector_statuses: [
    {
      collector: "kubernetes",
      status: "SUCCEEDED",
      attempts: 1,
      started_at: t1(1),
      ended_at: t1(5),
      error: null,
    },
    {
      collector: "prometheus",
      status: "SUCCEEDED",
      attempts: 1,
      started_at: t1(1),
      ended_at: t1(7),
      error: null,
    },
    {
      collector: "deployment",
      status: "SUCCEEDED",
      attempts: 1,
      started_at: t1(1),
      ended_at: t1(5),
      error: null,
    },
    {
      collector: "trace",
      status: "SKIPPED",
      attempts: 0,
      started_at: null,
      ended_at: null,
      error: null,
    },
  ],
  created_at: t1(0),
  updated_at: t1(65),
};

export const CHECKOUT_RECORD: FixtureRecord = {
  incident: checkoutIncident,
  evidence: checkoutEvidence,
  contexts: [checkoutContext],
  reports: [{ report: checkoutReport, markdown: checkoutReportMarkdown }],
  agentRuns: [checkoutAgentRun],
  auditEvents: checkoutAudit,
  work: {
    schema_version: "1.0.0",
    incident_id: "inc-checkout-0001",
    collection: workItem({
      stage: "COLLECTION", state: "SUCCEEDED", available_at: t1(1), attempt_count: 1,
      worker_id: "incident-worker-0", claimed_at: t1(1), completed_at: t1(7),
    }),
    localization: workItem({
      stage: "LOCALIZATION", state: "SUCCEEDED", available_at: t1(8), attempt_count: 1,
      worker_id: "incident-worker-0", claimed_at: t1(8), completed_at: t1(10),
    }),
    analysis: workItem({
      stage: "ANALYSIS", state: "SUCCEEDED", available_at: t1(11), attempt_count: 1,
      worker_id: "agent-worker-0", claimed_at: t1(21), completed_at: t1(64),
      context_id: "ctx-checkout-0001",
    }),
  },
};
