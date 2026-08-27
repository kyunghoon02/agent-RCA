import type { CollectorName, CollectorStatus } from "./types";

export interface CollectorDescriptor {
  collector: string;
  label: string;
  description: string;
}

/**
 * Display catalog for collectors.
 *
 * Keyed by the collector names in `contracts/schemas/incident.schema.json`.
 * `prometheus-api` is the KRCA-style API dependency drilldown described in
 * contracts/krca-drilldown.md, which is why it reads differently from the plain
 * Prometheus metric collector.
 */
export const COLLECTOR_CATALOG: Record<string, Omit<CollectorDescriptor, "collector">> = {
  kubernetes: {
    label: "Kubernetes",
    description: "Resource state, events and owner references from the Kubernetes API.",
  },
  prometheus: {
    label: "Prometheus",
    description: "Bounded range queries summarised into metric Evidence.",
  },
  "prometheus-api": {
    label: "KRCA API Dependency",
    description: "Versioned KRCA profile drilldown across API dependencies.",
  },
  "prometheus-workload": {
    label: "Prometheus Workload",
    description: "Per-workload memory, restart and saturation series for the incident window.",
  },
  "loki-kernel-oom": {
    label: "Loki Kernel OOM",
    description: "Kernel memcg OOM-kill log patterns corroborating Pod termination.",
  },
  deployment: {
    label: "Deployment History",
    description: "Rollout revisions and changed fields near the incident window.",
  },
  logs: {
    label: "Logs",
    description: "Bounded log pattern matches from the configured log Provider.",
  },
  trace: {
    label: "Traces",
    description: "Span summaries for the incident window.",
  },
  hubble: {
    label: "Hubble Network Flows",
    description: "Service-to-service flow verdicts and resets.",
  },
};

/**
 * Providers the Viewer knows how to present, whether or not this Incident ran
 * them. New Providers only need an entry in the catalog above.
 */
/**
 * Providers the Viewer can describe. The live platform reports collectors
 * beyond the schema enum (prometheus-workload, loki-kernel-oom), so this is a
 * display catalog keyed by string with a graceful fallback, not a closed set.
 */
export const KNOWN_COLLECTORS = Object.keys(COLLECTOR_CATALOG);

export function describeCollector(collector: string): Omit<CollectorDescriptor, "collector"> {
  return (
    COLLECTOR_CATALOG[collector] ?? {
      label: collector,
      description: "Provider reported by the platform but not yet described in the Viewer.",
    }
  );
}

/** Collectors that ran, followed by catalog Providers this Incident never used. */
export function collectorRows(statuses: CollectorStatus[]): {
  collector: string;
  status: CollectorStatus | null;
}[] {
  const seen = new Set<string>(statuses.map((status) => status.collector));
  return [
    ...statuses.map((status) => ({ collector: status.collector, status })),
    ...KNOWN_COLLECTORS.filter((collector) => !seen.has(collector)).map((collector) => ({
      collector,
      status: null,
    })),
  ];
}
