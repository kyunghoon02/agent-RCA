/**
 * Older background Incidents.
 *
 * These exist so the list has more rows than one page and cursor pagination is
 * exercisable without a backend. They are generated from a fixed table — index
 * in, values out — so the dataset is byte-identical on every run.
 */
import type { EvidenceItem, Incident, IncidentStatus, Severity } from "@/lib/types";
import { clock, k8s, makeEvidence, workItem, type FixtureRecord } from "./helpers";

const NS = "online-boutique";
const CLUSTER = "agent-rca-local";

interface Seed {
  service: string;
  status: IncidentStatus;
  severity: Severity;
  alert: string;
}

/** Fixed table: no randomness, no date arithmetic against "now". */
const SEEDS: Seed[] = [
  { service: "currencyservice", status: "REPORTED", severity: "warning", alert: "CurrencyConversionErrors" },
  { service: "productcatalog", status: "REPORTED", severity: "info", alert: "CatalogQueryLatency" },
  { service: "recommendation", status: "FAILED", severity: "warning", alert: "RecommendationTimeouts" },
  { service: "loadgenerator", status: "REPORTED", severity: "info", alert: "LoadGeneratorRestart" },
  { service: "checkoutservice", status: "PARTIAL", severity: "critical", alert: "CheckoutOrderFailures" },
  { service: "frontend", status: "REPORTED", severity: "warning", alert: "FrontendMemoryPressure" },
  { service: "cartservice", status: "REPORTED", severity: "info", alert: "CartServiceRestart" },
  { service: "adservice", status: "REPORTED", severity: "warning", alert: "AdServiceGrpcErrors" },
  { service: "shippingservice", status: "PARTIAL", severity: "info", alert: "ShippingQuoteLatency" },
  { service: "emailservice", status: "REPORTED", severity: "warning", alert: "EmailDeliveryFailures" },
  { service: "paymentservice", status: "REPORTED", severity: "critical", alert: "PaymentGatewayTimeout" },
  { service: "redis-cart", status: "REPORTED", severity: "warning", alert: "RedisCartEvictions" },
  { service: "productcatalog", status: "FAILED", severity: "warning", alert: "CatalogSyncFailure" },
  { service: "frontend", status: "REPORTED", severity: "info", alert: "FrontendCacheMiss" },
  { service: "currencyservice", status: "REPORTED", severity: "info", alert: "CurrencyRateStale" },
  { service: "checkoutservice", status: "REPORTED", severity: "warning", alert: "CheckoutRetryBudget" },
  { service: "cartservice", status: "REPORTED", severity: "critical", alert: "CartDataInconsistency" },
  { service: "adservice", status: "PARTIAL", severity: "info", alert: "AdServiceColdStart" },
  { service: "recommendation", status: "REPORTED", severity: "info", alert: "RecommendationCacheEvict" },
  { service: "shippingservice", status: "REPORTED", severity: "warning", alert: "ShippingRateLimit" },
  { service: "emailservice", status: "REPORTED", severity: "info", alert: "EmailQueueDrain" },
  { service: "paymentservice", status: "PARTIAL", severity: "warning", alert: "PaymentRetryExhausted" },
  { service: "redis-cart", status: "REPORTED", severity: "info", alert: "RedisCartSlowLog" },
  { service: "loadgenerator", status: "REPORTED", severity: "info", alert: "LoadGeneratorSkew" },
];

/** Newest generated Incident sits below the curated set so page 1 stays curated. */
const GENERATED_ANCHOR = "2026-07-26T08:00:00Z";

function pad(index: number): string {
  return String(index + 9).padStart(4, "0");
}

function buildRecord(seed: Seed, index: number): FixtureRecord {
  const incidentId = `inc-${seed.service.replace(/[^a-z0-9-]/g, "")}-${pad(index)}`;
  const anchorMs = new Date(GENERATED_ANCHOR).getTime() - index * 37 * 60_000;
  const t = clock(new Date(anchorMs).toISOString().replace(".000Z", "Z"));
  const pod = k8s("Pod", `${seed.service}-${pad(index)}-gen`, NS, {
    uid: `uid-${seed.service}-${pad(index)}`,
  });
  const failed = seed.status === "FAILED";
  const partial = seed.status === "PARTIAL";

  const evidence: EvidenceItem[] = failed
    ? []
    : [
        makeEvidence({
          evidence_id: `ev-${seed.service.replace(/[^a-z0-9-]/g, "")}-${pad(index)}`,
          incident_id: incidentId,
          source: "kubernetes",
          kind: "resource-state",
          observed_at: t(4),
          window: { start: t(-1800), end: t(4) },
          subject: pod,
          summary: `${seed.service} resource state captured for ${seed.alert}.`,
          facts: { phase: "Running", ready_replicas: partial ? 1 : 2, restart_count: partial ? 2 : 0 },
          provenance: {
            provider: "kubernetes-api",
            query: `get pods -n ${NS} -l app=${seed.service} -o json`,
            locator: `k8s://${NS}/Pod/${pod.name}`,
            collected_at: t(4),
          },
          quality: {
            freshness: "live",
            completeness: partial ? 0.5 : 1,
            confidence: partial ? 0.6 : 0.95,
          },
          redactions: [],
        }),
      ];

  const incident: Incident = {
    schema_version: "1.0.0",
    incident_id: incidentId,
    deduplication_key: `alertmanager:${seed.alert}:${NS}:${seed.service}`,
    status: seed.status,
    severity: seed.severity,
    source: index % 5 === 0 ? "cloud-monitoring" : "alertmanager",
    triggered_at: t(0),
    window: {
      baseline_start: t(-1800),
      incident_start: t(-300),
      incident_end: t(600),
      recovery_end: t(720),
    },
    alert: {
      fingerprint: `gen${pad(index)}fingerprint`,
      name: seed.alert,
      labels: {
        alertname: seed.alert,
        namespace: NS,
        service: seed.service,
        severity: seed.severity,
        cluster: CLUSTER,
      },
      annotations: { summary: `${seed.alert} fired for ${seed.service}` },
    },
    source_entity: pod,
    collector_statuses: [
      {
        collector: "kubernetes",
        status: failed ? "FAILED" : "SUCCEEDED",
        attempts: failed ? 3 : 1,
        started_at: t(1),
        ended_at: t(4),
        error: failed ? "KUBERNETES_API_TIMEOUT" : null,
      },
      {
        collector: "prometheus",
        status: partial ? "PARTIAL" : failed ? "FAILED" : "SUCCEEDED",
        attempts: 1,
        started_at: t(1),
        ended_at: t(5),
        error: partial ? "PROMETHEUS_PARTIAL_RANGE" : failed ? "PROMETHEUS_UNREACHABLE" : null,
      },
    ],
    created_at: t(0),
    updated_at: t(failed ? 40 : 90),
  };

  return {
    incident,
    evidence,
    contexts: [],
    reports: [],
    agentRuns: [],
    auditEvents: [
      {
        occurred_at: t(0),
        event_type: "INCIDENT_CREATED",
        details: { source: incident.source, alert: seed.alert, severity: seed.severity },
      },
      {
        occurred_at: t(1),
        event_type: "STATUS_TRANSITIONED",
        details: { from: "RECEIVED", to: "COLLECTING" },
      },
      ...(failed
        ? [
            {
              occurred_at: t(40),
              event_type: "STATUS_TRANSITIONED",
              details: { from: "COLLECTING", to: "FAILED", reason: "LEASE_ATTEMPTS_EXHAUSTED" },
            },
          ]
        : [
            {
              occurred_at: t(6),
              event_type: "COLLECTION_COMPLETED",
              details: { outcome: partial ? "PARTIAL" : "SUCCEEDED", evidence_count: evidence.length },
            },
            {
              occurred_at: t(90),
              event_type: "STATUS_TRANSITIONED",
              details: { from: "COLLECTING", to: seed.status },
            },
          ]),
    ],
    work: {
      schema_version: "1.0.0",
      incident_id: incidentId,
      collection: workItem({
        stage: "COLLECTION",
        state: failed ? "FAILED" : "SUCCEEDED",
        available_at: t(1),
        attempt_count: failed ? 3 : 1,
        worker_id: `incident-worker-${index % 2}`,
        claimed_at: t(1),
        completed_at: failed ? t(40) : t(6),
        last_error_code: failed ? "KUBERNETES_API_TIMEOUT" : null,
      }),
      localization: null,
      analysis: null,
    },
  };
}

export const GENERATED_RECORDS: FixtureRecord[] = SEEDS.map(buildRecord);
