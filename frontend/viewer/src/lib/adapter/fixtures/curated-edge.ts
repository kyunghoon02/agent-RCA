/**
 * Fixture Incidents for degraded and in-flight states: INSUFFICIENT_DATA
 * Evidence, a hard collection failure with no Evidence at all, two runs still
 * in progress, and a detail bundle that hit the Viewer's bounded limits.
 */
import type { ContextPackage, EvidenceItem, Incident } from "@/lib/types";
import {
  clock,
  k8s,
  makeEvidence,
  workItem,
  type AuditEventFixture,
  type FixtureRecord,
} from "./helpers";

const NS = "online-boutique";
const CLUSTER = "agent-rca-local";

/* ------------------------------------------------------------------ *
 * 4. inc-rediscart-0004 — PARTIAL. Prometheus timed out, so the metric
 *    Evidence is INSUFFICIENT_DATA with the missing series named.
 * ------------------------------------------------------------------ */

const t4 = clock("2026-07-26T19:02:00Z");
const redisPod = k8s("Pod", "redis-cart-7f8d9c6b5-lk3jd", NS, { uid: "1a9f-redis-lk3jd" });

const redisEvidence: EvidenceItem[] = [
  makeEvidence({
    evidence_id: "ev-redis-mem-01",
    incident_id: "inc-rediscart-0004",
    source: "prometheus",
    kind: "metric-summary",
    observed_at: t4(6),
    window: { start: t4(-1800), end: t4(6) },
    subject: redisPod,
    summary:
      "INSUFFICIENT_DATA: redis-cart memory series returned no samples for the incident window.",
    facts: {
      status: "INSUFFICIENT_DATA",
      reason: "PROMETHEUS_QUERY_TIMEOUT",
      detail:
        "The range query exceeded its 10s deadline after 2 attempts. No samples were returned, so no baseline comparison was computed.",
      missing_series: [
        'redis_memory_used_bytes{namespace="online-boutique",pod="redis-cart-7f8d9c6b5-lk3jd"}',
        'redis_memory_max_bytes{namespace="online-boutique",pod="redis-cart-7f8d9c6b5-lk3jd"}',
      ],
      expected_sample_count: 60,
      returned_sample_count: 0,
    },
    provenance: {
      provider: "prometheus-api",
      query: 'redis_memory_used_bytes{namespace="online-boutique",pod=~"redis-cart-.*"}',
      locator: "prom://range/redis_memory_used_bytes",
      collected_at: t4(6),
    },
    quality: { freshness: "unknown", completeness: 0, confidence: 0 },
    redactions: [],
  }),
  makeEvidence({
    evidence_id: "ev-redis-evt-01",
    incident_id: "inc-rediscart-0004",
    source: "kubernetes",
    kind: "kubernetes-event",
    observed_at: t4(4),
    window: { start: t4(-1800), end: t4(4) },
    subject: redisPod,
    summary: "redis-cart container was OOMKilled twice and restarted by the kubelet.",
    facts: {
      reason: "OOMKilled",
      restart_count: 2,
      last_terminated_exit_code: 137,
      memory_limit: "256Mi",
    },
    provenance: {
      provider: "kubernetes-api",
      query: "get pod redis-cart-7f8d9c6b5-lk3jd -n online-boutique -o json",
      locator: "k8s://online-boutique/Pod/redis-cart-7f8d9c6b5-lk3jd",
      collected_at: t4(4),
    },
    quality: { freshness: "live", completeness: 1, confidence: 1 },
    redactions: [],
  }),
  makeEvidence({
    evidence_id: "ev-redis-log-01",
    incident_id: "inc-rediscart-0004",
    source: "logs",
    kind: "log-pattern",
    observed_at: t4(5),
    window: { start: t4(-900), end: t4(5) },
    subject: redisPod,
    summary:
      "PARTIAL: 3 of 9 log shards responded. The matched pattern appears 14 times in the shards that returned.",
    facts: {
      status: "PARTIAL",
      pattern: "OOM command not allowed when used memory > 'maxmemory'",
      match_count: 14,
      shards_queried: 9,
      shards_returned: 3,
      reason: "LOG_BACKEND_PARTIAL_RESPONSE",
    },
    provenance: {
      provider: "loki",
      query: '{namespace="online-boutique",pod=~"redis-cart-.*"} |= "OOM"',
      locator: "loki://online-boutique/redis-cart",
      collected_at: t4(5),
    },
    quality: { freshness: "recent", completeness: 0.33, confidence: 0.6 },
    redactions: ["line.auth_token"],
  }),
];

const redisContext: ContextPackage = {
  schema_version: "1.0.0",
  context_id: "ctx-redis-0004",
  incident_id: "inc-rediscart-0004",
  frozen_at: t4(12),
  source_entity: redisPod,
  scope: {
    incident_id: "inc-rediscart-0004",
    seed_entity_ids: ["ent-k8s-pod-rediscart-lk3jd"],
    domains: ["kubernetes"],
    correlation_keys: { namespace: NS, service: "redis-cart" },
    relation_types: ["CALLS", "OWNS"],
    time_window: { start: t4(-1800), end: t4(12) },
    max_entities: 100,
    max_depth: 2,
  },
  state_paths: [
    {
      path_id: "path-cart-redis",
      entities: [k8s("Service", "cartservice", NS), k8s("Service", "redis-cart", NS), redisPod],
      relations: ["CALLS", "ROUTES_TO"],
      evidence_ids: ["ev-redis-evt-01"],
    },
  ],
  evidence_ids: ["ev-redis-evt-01", "ev-redis-log-01"],
  recent_change_evidence_ids: [],
  missing_evidence: [
    {
      source: "prometheus",
      reason: "redis_memory_used_bytes returned no samples: PROMETHEUS_QUERY_TIMEOUT after 2 attempts.",
    },
    {
      source: "logs",
      reason: "Only 3 of 9 log shards responded; the pattern count is a lower bound.",
    },
  ],
  collector_failures: [
    { collector: "prometheus", error: "PROMETHEUS_QUERY_TIMEOUT" },
  ],
  localization: {
    strategy: "stategraph",
    candidate_entities_before: 71,
    candidate_entities_after: 4,
    context_completeness: 0.45,
  },
};

export const REDIS_RECORD: FixtureRecord = {
  incident: {
    schema_version: "1.0.0",
    incident_id: "inc-rediscart-0004",
    deduplication_key: "alertmanager:RedisCartOOMKilled:online-boutique:redis-cart",
    status: "PARTIAL",
    severity: "warning",
    source: "alertmanager",
    triggered_at: t4(0),
    window: {
      baseline_start: t4(-1800), incident_start: t4(-600),
      incident_end: null, recovery_end: null,
    },
    alert: {
      fingerprint: "51ce7b30d8a4",
      name: "RedisCartOOMKilled",
      labels: {
        alertname: "RedisCartOOMKilled", namespace: NS, service: "redis-cart",
        severity: "warning", cluster: CLUSTER,
      },
      annotations: { summary: "redis-cart container OOMKilled twice in 10 minutes" },
    },
    source_entity: redisPod,
    collector_statuses: [
      { collector: "kubernetes", status: "SUCCEEDED", attempts: 1, started_at: t4(1), ended_at: t4(4), error: null },
      {
        collector: "prometheus", status: "TIMED_OUT", attempts: 2, started_at: t4(1), ended_at: t4(6),
        error: "PROMETHEUS_QUERY_TIMEOUT",
      },
      {
        collector: "logs", status: "PARTIAL", attempts: 1, started_at: t4(1), ended_at: t4(5),
        error: "LOG_BACKEND_PARTIAL_RESPONSE",
      },
    ],
    created_at: t4(0),
    updated_at: t4(13),
  },
  evidence: redisEvidence,
  contexts: [redisContext],
  reports: [],
  agentRuns: [],
  auditEvents: [
    {
      occurred_at: t4(0), event_type: "INCIDENT_CREATED",
      details: { source: "alertmanager", alert: "RedisCartOOMKilled", severity: "warning" },
    },
    { occurred_at: t4(1), event_type: "STATUS_TRANSITIONED", details: { from: "RECEIVED", to: "COLLECTING" } },
    {
      occurred_at: t4(6), event_type: "COLLECTION_FAILED",
      details: { collector: "prometheus", error_code: "PROMETHEUS_QUERY_TIMEOUT", attempts: 2 },
    },
    {
      occurred_at: t4(7), event_type: "COLLECTION_COMPLETED",
      details: { outcome: "PARTIAL", evidence_count: 3, failed_collectors: 1 },
    },
    { occurred_at: t4(8), event_type: "STATUS_TRANSITIONED", details: { from: "COLLECTING", to: "LOCALIZING" } },
    {
      occurred_at: t4(12), event_type: "LOCALIZATION_COMPLETED",
      details: { outcome: "PARTIAL", context_id: "ctx-redis-0004", context_completeness: 0.45 },
    },
    {
      occurred_at: t4(13), event_type: "STATUS_TRANSITIONED",
      details: { from: "LOCALIZING", to: "PARTIAL", reason: "CONTEXT_BELOW_ANALYSIS_THRESHOLD" },
    },
  ],
  work: {
    schema_version: "1.0.0",
    incident_id: "inc-rediscart-0004",
    collection: workItem({
      stage: "COLLECTION", state: "SUCCEEDED", available_at: t4(1), attempt_count: 2,
      worker_id: "incident-worker-0", claimed_at: t4(1), completed_at: t4(7),
      last_error_code: "PROMETHEUS_QUERY_TIMEOUT",
    }),
    localization: workItem({
      stage: "LOCALIZATION", state: "SUCCEEDED", available_at: t4(8), attempt_count: 1,
      worker_id: "incident-worker-0", claimed_at: t4(8), completed_at: t4(12),
    }),
    analysis: null,
  },
};

/* ------------------------------------------------------------------ *
 * 5. inc-payment-0005 — FAILED during collection. No Evidence, no Context.
 * ------------------------------------------------------------------ */

const t5 = clock("2026-07-26T17:30:00Z");
const paymentPod = k8s("Pod", "paymentservice-849fbc7d5-vv8qn", NS, { uid: "6c33-payment-vv8qn" });

export const PAYMENT_RECORD: FixtureRecord = {
  incident: {
    schema_version: "1.0.0",
    incident_id: "inc-payment-0005",
    deduplication_key: "alertmanager:PaymentServiceDown:online-boutique:paymentservice",
    status: "FAILED",
    severity: "critical",
    source: "alertmanager",
    triggered_at: t5(0),
    window: {
      baseline_start: t5(-1800), incident_start: t5(-120),
      incident_end: null, recovery_end: null,
    },
    alert: {
      fingerprint: "e70b2d14ac63",
      name: "PaymentServiceDown",
      labels: {
        alertname: "PaymentServiceDown", namespace: NS, service: "paymentservice",
        severity: "critical", cluster: CLUSTER,
      },
      annotations: { summary: "paymentservice has no ready endpoints" },
    },
    source_entity: paymentPod,
    collector_statuses: [
      {
        collector: "kubernetes", status: "FAILED", attempts: 3, started_at: t5(1), ended_at: t5(94),
        error: "KUBERNETES_API_UNAUTHORIZED",
      },
      {
        collector: "prometheus", status: "FAILED", attempts: 3, started_at: t5(1), ended_at: t5(91),
        error: "PROMETHEUS_CONNECTION_REFUSED",
      },
    ],
    created_at: t5(0),
    updated_at: t5(96),
  },
  evidence: [],
  contexts: [],
  reports: [],
  agentRuns: [],
  auditEvents: [
    {
      occurred_at: t5(0), event_type: "INCIDENT_CREATED",
      details: { source: "alertmanager", alert: "PaymentServiceDown", severity: "critical" },
    },
    { occurred_at: t5(1), event_type: "STATUS_TRANSITIONED", details: { from: "RECEIVED", to: "COLLECTING" } },
    {
      occurred_at: t5(1), event_type: "COLLECTION_CLAIMED",
      details: { worker_id: "incident-worker-1", attempt_count: 1 },
    },
    {
      occurred_at: t5(31), event_type: "COLLECTION_FAILED",
      details: { attempt_count: 1, error_code: "KUBERNETES_API_UNAUTHORIZED" },
    },
    {
      occurred_at: t5(62), event_type: "COLLECTION_FAILED",
      details: { attempt_count: 2, error_code: "KUBERNETES_API_UNAUTHORIZED" },
    },
    {
      occurred_at: t5(94), event_type: "COLLECTION_FAILED",
      details: { attempt_count: 3, error_code: "KUBERNETES_API_UNAUTHORIZED", terminal: true },
    },
    {
      occurred_at: t5(96), event_type: "STATUS_TRANSITIONED",
      details: { from: "COLLECTING", to: "FAILED", reason: "LEASE_ATTEMPTS_EXHAUSTED" },
    },
  ],
  work: {
    schema_version: "1.0.0",
    incident_id: "inc-payment-0005",
    collection: workItem({
      stage: "COLLECTION", state: "FAILED", available_at: t5(1), attempt_count: 3,
      worker_id: "incident-worker-1", claimed_at: t5(63), completed_at: t5(94),
      last_error_code: "KUBERNETES_API_UNAUTHORIZED",
    }),
    localization: null,
    analysis: null,
  },
};

/* ------------------------------------------------------------------ *
 * 6. inc-adservice-0006 — COLLECTING right now. Lease still held.
 * ------------------------------------------------------------------ */

const t6 = clock("2026-07-27T03:20:00Z");
const adPod = k8s("Pod", "adservice-6bc5d8f94-t7rzx", NS, { uid: "0d77-ad-t7rzx" });

export const ADSERVICE_RECORD: FixtureRecord = {
  incident: {
    schema_version: "1.0.0",
    incident_id: "inc-adservice-0006",
    deduplication_key: "cloud-monitoring:AdServiceLatencyBudget:online-boutique:adservice",
    status: "COLLECTING",
    severity: "info",
    source: "cloud-monitoring",
    triggered_at: t6(0),
    window: {
      baseline_start: t6(-1800), incident_start: t6(-240),
      incident_end: null, recovery_end: null,
    },
    alert: {
      fingerprint: "44a1e0b7cc25",
      name: "AdServiceLatencyBudget",
      labels: {
        alertname: "AdServiceLatencyBudget", namespace: NS, service: "adservice",
        severity: "info", cluster: CLUSTER,
      },
      annotations: { summary: "adservice consumed 40% of its latency error budget" },
    },
    source_entity: adPod,
    collector_statuses: [
      { collector: "kubernetes", status: "RUNNING", attempts: 1, started_at: t6(2), ended_at: null, error: null },
      { collector: "prometheus", status: "PENDING", attempts: 0, started_at: null, ended_at: null, error: null },
    ],
    created_at: t6(0),
    updated_at: t6(2),
  },
  evidence: [],
  contexts: [],
  reports: [],
  agentRuns: [],
  auditEvents: [
    {
      occurred_at: t6(0), event_type: "INCIDENT_CREATED",
      details: { source: "cloud-monitoring", alert: "AdServiceLatencyBudget", severity: "info" },
    },
    { occurred_at: t6(1), event_type: "STATUS_TRANSITIONED", details: { from: "RECEIVED", to: "COLLECTING" } },
    {
      occurred_at: t6(2), event_type: "COLLECTION_CLAIMED",
      details: { worker_id: "incident-worker-0", attempt_count: 1, lease_seconds: 120 },
    },
  ],
  work: {
    schema_version: "1.0.0",
    incident_id: "inc-adservice-0006",
    collection: workItem({
      stage: "COLLECTION", state: "RUNNING", available_at: t6(1), attempt_count: 1,
      worker_id: "incident-worker-0", claimed_at: t6(2), lease_expires_at: t6(122),
    }),
    localization: null,
    analysis: null,
  },
};

/* ------------------------------------------------------------------ *
 * 7. inc-shipping-0007 — LOCALIZING. Evidence collected, no Context yet.
 * ------------------------------------------------------------------ */

const t7 = clock("2026-07-27T03:11:00Z");
const shippingPod = k8s("Pod", "shippingservice-59d7b4c86-hq4vm", NS, { uid: "3e55-ship-hq4vm" });

export const SHIPPING_RECORD: FixtureRecord = {
  incident: {
    schema_version: "1.0.0",
    incident_id: "inc-shipping-0007",
    deduplication_key: "alertmanager:ShippingQuoteErrors:online-boutique:shippingservice",
    status: "LOCALIZING",
    severity: "warning",
    source: "alertmanager",
    triggered_at: t7(0),
    window: {
      baseline_start: t7(-1800), incident_start: t7(-420),
      incident_end: null, recovery_end: null,
    },
    alert: {
      fingerprint: "9fbb2c7e6a10",
      name: "ShippingQuoteErrors",
      labels: {
        alertname: "ShippingQuoteErrors", namespace: NS, service: "shippingservice",
        severity: "warning", cluster: CLUSTER,
      },
      annotations: { summary: "shippingservice GetQuote error ratio above 2%" },
    },
    source_entity: shippingPod,
    collector_statuses: [
      { collector: "kubernetes", status: "SUCCEEDED", attempts: 1, started_at: t7(1), ended_at: t7(4), error: null },
      { collector: "prometheus", status: "SUCCEEDED", attempts: 1, started_at: t7(1), ended_at: t7(5), error: null },
    ],
    created_at: t7(0),
    updated_at: t7(7),
  },
  evidence: [
    makeEvidence({
      evidence_id: "ev-ship-err-01",
      incident_id: "inc-shipping-0007",
      source: "prometheus",
      kind: "metric-summary",
      observed_at: t7(5),
      window: { start: t7(-1800), end: t7(5) },
      subject: k8s("Service", "shippingservice", NS),
      summary: "shippingservice GetQuote error ratio rose from 0.2% to 2.7% at 03:04Z.",
      facts: {
        metric: "grpc_server_handled_total",
        method: "GetQuote",
        baseline_ratio: 0.002,
        incident_ratio: 0.027,
      },
      provenance: {
        provider: "prometheus-api",
        query: 'sum(rate(grpc_server_handled_total{app="shippingservice",grpc_code!="OK"}[5m]))',
        locator: "prom://range/grpc_server_handled_total",
        collected_at: t7(5),
      },
      quality: { freshness: "live", completeness: 1, confidence: 0.86 },
      redactions: [],
    }),
    makeEvidence({
      evidence_id: "ev-ship-pod-01",
      incident_id: "inc-shipping-0007",
      source: "kubernetes",
      kind: "resource-state",
      observed_at: t7(4),
      window: { start: t7(-1800), end: t7(4) },
      subject: shippingPod,
      summary: "shippingservice Pods are Ready; one restarted 18 minutes before the alert.",
      facts: { phase: "Running", ready_replicas: 2, restart_count: 1, last_restart_at: t7(-1080) },
      provenance: {
        provider: "kubernetes-api",
        query: "get pods -n online-boutique -l app=shippingservice -o json",
        locator: "k8s://online-boutique/Pod/shippingservice-59d7b4c86-hq4vm",
        collected_at: t7(4),
      },
      quality: { freshness: "live", completeness: 1, confidence: 1 },
      redactions: [],
    }),
  ],
  contexts: [],
  reports: [],
  agentRuns: [],
  auditEvents: [
    {
      occurred_at: t7(0), event_type: "INCIDENT_CREATED",
      details: { source: "alertmanager", alert: "ShippingQuoteErrors", severity: "warning" },
    },
    { occurred_at: t7(1), event_type: "STATUS_TRANSITIONED", details: { from: "RECEIVED", to: "COLLECTING" } },
    {
      occurred_at: t7(6), event_type: "COLLECTION_COMPLETED",
      details: { outcome: "SUCCEEDED", evidence_count: 2 },
    },
    { occurred_at: t7(7), event_type: "STATUS_TRANSITIONED", details: { from: "COLLECTING", to: "LOCALIZING" } },
    {
      occurred_at: t7(7), event_type: "LOCALIZATION_CLAIMED",
      details: { worker_id: "incident-worker-1", attempt_count: 1, lease_seconds: 120 },
    },
  ],
  work: {
    schema_version: "1.0.0",
    incident_id: "inc-shipping-0007",
    collection: workItem({
      stage: "COLLECTION", state: "SUCCEEDED", available_at: t7(1), attempt_count: 1,
      worker_id: "incident-worker-1", claimed_at: t7(1), completed_at: t7(6),
    }),
    localization: workItem({
      stage: "LOCALIZATION", state: "RUNNING", available_at: t7(7), attempt_count: 1,
      worker_id: "incident-worker-1", claimed_at: t7(7), lease_expires_at: t7(127),
    }),
    analysis: null,
  },
};

/* ------------------------------------------------------------------ *
 * 8. inc-emailsvc-0008 — REPORTED, but the detail bundle hit Viewer limits
 *    and one Evidence item sits outside every StateGraph path.
 * ------------------------------------------------------------------ */

const t8 = clock("2026-07-26T11:47:00Z");
const emailPod = k8s("Pod", "emailservice-7c9d5f6b8-zz1qp", NS, { uid: "5b8a-email-zz1qp" });

const emailContext: ContextPackage = {
  schema_version: "1.0.0",
  context_id: "ctx-email-0008",
  incident_id: "inc-emailsvc-0008",
  frozen_at: t8(11),
  source_entity: emailPod,
  scope: {
    namespaces: [NS],
    entity_uids: ["5b8a-email-zz1qp"],
    metapaths: [["Pod", "ReplicaSet", "Deployment"], ["Service", "Pod"]],
    time_window: { start: t8(-1800), end: t8(11) },
    max_entities: 100,
  },
  state_paths: [
    {
      path_id: "path-checkout-email",
      entities: [k8s("Service", "checkoutservice", NS), k8s("Service", "emailservice", NS)],
      relations: ["CALLS"],
      evidence_ids: ["ev-email-grpc-01"],
    },
    {
      path_id: "path-deployment-pod",
      entities: [
        k8s("Deployment", "emailservice", NS, { apiVersion: "apps/v1" }),
        emailPod,
      ],
      relations: ["OWNS"],
      evidence_ids: ["ev-email-pod-01"],
    },
  ],
  evidence_ids: ["ev-email-grpc-01", "ev-email-pod-01"],
  recent_change_evidence_ids: [],
  missing_evidence: [],
  collector_failures: [],
  localization: {
    strategy: "stategraph",
    candidate_entities_before: 71,
    candidate_entities_after: 4,
    context_completeness: 0.9,
  },
};

export const EMAIL_RECORD: FixtureRecord = {
  incident: {
    schema_version: "1.0.0",
    incident_id: "inc-emailsvc-0008",
    deduplication_key: "alertmanager:EmailServiceQueueBacklog:online-boutique:emailservice",
    status: "REPORTED",
    severity: "warning",
    source: "alertmanager",
    triggered_at: t8(0),
    window: {
      baseline_start: t8(-1800), incident_start: t8(-540),
      incident_end: t8(300), recovery_end: t8(420),
    },
    alert: {
      fingerprint: "c3d9017fa5be",
      name: "EmailServiceQueueBacklog",
      labels: {
        alertname: "EmailServiceQueueBacklog", namespace: NS, service: "emailservice",
        severity: "warning", cluster: CLUSTER,
      },
      annotations: { summary: "emailservice confirmation queue depth above 5000" },
    },
    source_entity: emailPod,
    collector_statuses: [
      { collector: "kubernetes", status: "SUCCEEDED", attempts: 1, started_at: t8(1), ended_at: t8(4), error: null },
      { collector: "prometheus", status: "SUCCEEDED", attempts: 1, started_at: t8(1), ended_at: t8(6), error: null },
      {
        collector: "trace", status: "PARTIAL", attempts: 1, started_at: t8(1), ended_at: t8(8),
        error: "TRACE_SAMPLING_BELOW_THRESHOLD",
      },
    ],
    created_at: t8(0),
    updated_at: t8(74),
  },
  evidence: [
    makeEvidence({
      evidence_id: "ev-email-grpc-01",
      incident_id: "inc-emailsvc-0008",
      source: "prometheus",
      kind: "metric-summary",
      observed_at: t8(6),
      window: { start: t8(-1800), end: t8(6) },
      subject: k8s("Service", "emailservice", NS),
      summary: "emailservice queue depth grew from 120 to 5,412 over 9 minutes.",
      facts: { metric: "email_queue_depth", baseline: 120, peak: 5412, growth_minutes: 9 },
      provenance: {
        provider: "prometheus-api",
        query: 'email_queue_depth{namespace="online-boutique"}',
        locator: "prom://range/email_queue_depth",
        collected_at: t8(6),
      },
      quality: { freshness: "live", completeness: 1, confidence: 0.91 },
      redactions: [],
    }),
    makeEvidence({
      evidence_id: "ev-email-pod-01",
      incident_id: "inc-emailsvc-0008",
      source: "kubernetes",
      kind: "resource-state",
      observed_at: t8(4),
      window: { start: t8(-1800), end: t8(4) },
      subject: emailPod,
      summary: "emailservice ran a single replica against a horizontal target of 3.",
      facts: { ready_replicas: 1, desired_replicas: 3, hpa_target: 3, cpu_throttled_seconds: 41.2 },
      provenance: {
        provider: "kubernetes-api",
        query: "get deployment emailservice -n online-boutique -o json",
        locator: "k8s://online-boutique/Deployment/emailservice",
        collected_at: t8(4),
      },
      quality: { freshness: "live", completeness: 1, confidence: 1 },
      redactions: [],
    }),
    makeEvidence({
      evidence_id: "ev-email-trace-01",
      incident_id: "inc-emailsvc-0008",
      source: "trace",
      kind: "trace-summary",
      observed_at: t8(8),
      window: { start: t8(-900), end: t8(8) },
      subject: k8s("Service", "emailservice", NS),
      summary:
        "PARTIAL: 41 sampled traces show SendOrderConfirmation spans averaging 4.2s, below the sampling threshold for a reliable percentile.",
      facts: {
        status: "PARTIAL",
        reason: "TRACE_SAMPLING_BELOW_THRESHOLD",
        sampled_traces: 41,
        required_traces: 200,
        mean_span_seconds: 4.2,
      },
      provenance: {
        provider: "tempo",
        query: '{ service.name="emailservice" && name="SendOrderConfirmation" }',
        locator: "tempo://online-boutique/emailservice",
        collected_at: t8(8),
      },
      quality: { freshness: "recent", completeness: 0.2, confidence: 0.35 },
      redactions: [],
    }),
  ],
  contexts: [emailContext],
  reports: [
    {
      report: {
        schema_version: "1.0.0",
        report_id: "rpt-email-0008",
        incident_id: "inc-emailsvc-0008",
        context_id: "ctx-email-0008",
        path: "fast",
        status: "partial",
        generated_at: t8(72),
        root_cause: {
          summary:
            "emailservice ran on a single replica while the confirmation queue grew, so consumers could not keep pace with checkout traffic. Replica count recovered on its own at 11:56Z.",
          entity: k8s("Deployment", "emailservice", NS, { apiVersion: "apps/v1" }),
          supporting_evidence_ids: ["ev-email-pod-01", "ev-email-grpc-01"],
          reference_document_ids: [],
        },
        hypotheses: [
          {
            rank: 1,
            summary: "Under-scaled emailservice replica count caused the queue backlog.",
            entity: k8s("Deployment", "emailservice", NS, { apiVersion: "apps/v1" }),
            confidence: 0.72,
            status: "supported",
            supporting_evidence_ids: ["ev-email-pod-01", "ev-email-grpc-01"],
            contradicting_evidence_ids: [],
            reference_document_ids: [],
            missing_evidence: ["HPA scaling decision events for the incident window"],
          },
          {
            rank: 2,
            summary: "A slow downstream SMTP relay held SendOrderConfirmation spans open.",
            entity: k8s("Service", "emailservice", NS),
            confidence: 0.34,
            status: "competing",
            supporting_evidence_ids: ["ev-email-trace-01"],
            contradicting_evidence_ids: [],
            reference_document_ids: [],
            missing_evidence: [
              "trace-summary at full sampling",
              "SMTP relay egress metrics",
            ],
          },
        ],
        remediation: {
          suggestions: [
            "Review the emailservice HPA minimum replica count against peak checkout throughput.",
            "Raise trace sampling for emailservice so the competing SMTP hypothesis can be settled.",
          ],
          verification_conditions: [
            "emailservice holds at least 3 ready replicas through the next traffic peak.",
            "email_queue_depth stays below 500 for 30 minutes.",
          ],
        },
        budget: {
          applicable: true, llm_calls: 2, tool_calls: 3,
          tree_depth: 1, wall_time_ms: 18700, exhausted: false,
        },
        read_only: true,
        limitations: [
          "Trace Evidence was sampled below the reliability threshold, so the competing SMTP hypothesis could not be resolved.",
          "The detail bundle for this Incident exceeded Viewer limits; some Evidence and audit rows are not shown.",
        ],
      },
      markdown: `# RCA Report — inc-emailsvc-0008

## Conclusion
**partial** — a supported root cause with one unresolved competing hypothesis.

## Root cause
emailservice ran a single replica against a target of 3 while the confirmation
queue grew from 120 to 5,412. Replica count recovered without intervention.

## Unresolved
Trace sampling (41 of 200 required traces) was too low to rule out a slow SMTP relay.

_This report is read-only._
`,
    },
  ],
  agentRuns: [
    {
      schema_version: "1.0.0",
      agent_run_id: "arun-email-0008",
      incident_id: "inc-emailsvc-0008",
      context_id: "ctx-email-0008",
      knowledge_audit_id: "kaud-email-0008",
      knowledge_status: "STALE_ONLY",
      model: "gpt-4.1-mini",
      status: "SUCCEEDED",
      reason_code: "REPORT_ACCEPTED",
      started_at: t8(53),
      completed_at: t8(71),
      budget: {
        max_turns: 4, max_llm_calls: 4, max_tool_calls: 8,
        max_output_tokens: 2000, max_wall_time_ms: 60000,
      },
      usage: {
        llm_calls: 2, tool_calls: 3, input_tokens: 9120,
        output_tokens: 903, total_tokens: 10023, wall_time_ms: 18700,
      },
      tool_events: [
        {
          sequence: 1, tool_name: "inspect_evidence", requested_id: "ev-email-pod-01",
          status: "SUCCEEDED", result_hash: "sha256:" + "d3".repeat(32),
        },
        {
          sequence: 2, tool_name: "inspect_evidence", requested_id: "ev-email-grpc-01",
          status: "SUCCEEDED", result_hash: "sha256:" + "a7".repeat(32),
        },
        {
          sequence: 3, tool_name: "inspect_reference", requested_id: "ref-k8s-hpa-0001",
          status: "DENIED", result_hash: "sha256:" + "0f".repeat(32),
        },
      ],
      retrieved_reference_ids: ["ref-k8s-hpa-0001"],
      inspected_evidence_ids: ["ev-email-pod-01", "ev-email-grpc-01"],
      inspected_reference_document_ids: [],
      cited_evidence_ids: ["ev-email-pod-01", "ev-email-grpc-01"],
      cited_reference_document_ids: [],
    },
  ],
  auditEvents: [
    {
      occurred_at: t8(0), event_type: "INCIDENT_CREATED",
      details: { source: "alertmanager", alert: "EmailServiceQueueBacklog", severity: "warning" },
    },
    { occurred_at: t8(1), event_type: "STATUS_TRANSITIONED", details: { from: "RECEIVED", to: "COLLECTING" } },
    {
      occurred_at: t8(9), event_type: "COLLECTION_COMPLETED",
      details: { outcome: "PARTIAL", evidence_count: 3, failed_collectors: 0 },
    },
    { occurred_at: t8(10), event_type: "STATUS_TRANSITIONED", details: { from: "COLLECTING", to: "LOCALIZING" } },
    {
      occurred_at: t8(11), event_type: "LOCALIZATION_COMPLETED",
      details: { outcome: "SUCCEEDED", context_id: "ctx-email-0008", strategy: "stategraph" },
    },
    { occurred_at: t8(12), event_type: "STATUS_TRANSITIONED", details: { from: "LOCALIZING", to: "ANALYZING" } },
    { occurred_at: t8(300), event_type: "ALERT_RESOLVED", details: { resolved_by: "alertmanager" } },
    {
      occurred_at: t8(72), event_type: "ANALYSIS_COMPLETED",
      details: { outcome: "SUCCEEDED", report_id: "rpt-email-0008", conclusion: "partial" },
    },
    { occurred_at: t8(74), event_type: "STATUS_TRANSITIONED", details: { from: "ANALYZING", to: "REPORTED" } },
  ],
  work: {
    schema_version: "1.0.0",
    incident_id: "inc-emailsvc-0008",
    collection: workItem({
      stage: "COLLECTION", state: "SUCCEEDED", available_at: t8(1), attempt_count: 1,
      worker_id: "incident-worker-0", claimed_at: t8(1), completed_at: t8(9),
    }),
    localization: workItem({
      stage: "LOCALIZATION", state: "SUCCEEDED", available_at: t8(10), attempt_count: 1,
      worker_id: "incident-worker-0", claimed_at: t8(10), completed_at: t8(11),
    }),
    analysis: workItem({
      stage: "ANALYSIS", state: "SUCCEEDED", available_at: t8(12), attempt_count: 2,
      worker_id: "agent-worker-1", claimed_at: t8(53), completed_at: t8(72),
      last_error_code: "MODEL_EXECUTION_FAILED", context_id: "ctx-email-0008",
    }),
  },
  truncated: { evidence: true, audit_events: true, timeline: true },
};

const _unused: Incident | undefined = undefined;
void _unused;
