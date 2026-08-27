import { isGraphEntityRef } from "./types";
import type { EntityRef, IncidentStatus, IncidentSummary } from "./types";

/**
 * What the list endpoint can and cannot answer.
 *
 * `viewer-incident-list.schema.json` returns a bounded summary only: no alert
 * labels, no work-queue state, no Report reference. So the list can say where
 * the pipeline is, but never what the RCA concluded, whether an Agent runtime
 * claimed the work, or whether an Incident is a controlled evaluation run.
 * Those require opening the Incident.
 */
export type ReportAvailability = "AVAILABLE" | "NOT_AVAILABLE" | "PENDING";

/**
 * Report availability inferred from lifecycle status alone.
 *
 * REPORTED is the only status the lifecycle reaches by generating a Report, so
 * it is a grounded signal. It deliberately says nothing about the outcome —
 * PROVEN vs ABSTAIN is not in the list payload.
 */
export function reportAvailability(status: IncidentStatus): ReportAvailability {
  if (status === "REPORTED") return "AVAILABLE";
  if (status === "FAILED" || status === "PARTIAL") return "NOT_AVAILABLE";
  return "PENDING";
}

export interface QuickFilter {
  id: string;
  label: string;
  description: string;
  statuses: IncidentStatus[];
  /** Applied to the returned page, since the query contract has no such filter. */
  clientOnly?: boolean;
}

export const QUICK_FILTERS: QuickFilter[] = [
  {
    id: "needs-agent",
    label: "Needs Agent",
    description:
      "Pipeline is at ANALYZING. Whether a runtime has claimed the work is only visible on the Incident.",
    statuses: ["ANALYZING"],
  },
  {
    id: "has-report",
    label: "Has Report",
    description: "Pipeline reached REPORTED, so a Report was generated.",
    statuses: ["REPORTED"],
  },
  {
    id: "failed",
    label: "Failed",
    description: "Pipeline ended as FAILED.",
    statuses: ["FAILED"],
  },
  {
    id: "incomplete",
    label: "Incomplete",
    description: "Pipeline ended as PARTIAL.",
    statuses: ["PARTIAL"],
  },
  {
    id: "collector-problems",
    label: "Collector problems",
    description:
      "At least one Provider reported PARTIAL, FAILED or TIMED_OUT. Applied to the loaded page.",
    statuses: [],
    clientOnly: true,
  },
];

export type RepeatRow =
  | { kind: "single"; key: string; item: IncidentSummary }
  | {
      kind: "repeat";
      key: string;
      items: IncidentSummary[];
      latest: IncidentSummary;
    };

/**
 * Stable identity for the entity an alert fired on.
 *
 * Namespace and name are not identity: two different resources can share both
 * across clusters, and a recreated resource reuses them. Grouping on them would
 * merge unrelated Incidents, so an entity only contributes a shared key when the
 * API gives something genuinely stable — a graph `entity_id`, or a Kubernetes
 * `cluster_id` + `uid` pair. Otherwise the key is made incident-specific so the
 * Incidents stay separate rather than being silently fused.
 */
function entityIdentityKey(entity: EntityRef, incidentId: string): string {
  if (isGraphEntityRef(entity)) {
    return ["graph", entity.domain, entity.entity_type, entity.entity_id].join("|");
  }
  const cluster = entity.cluster_id?.trim();
  const uid = entity.uid?.trim();
  if (cluster && uid) {
    return ["k8s", cluster, entity.kind, uid].join("|");
  }
  return ["unstable", incidentId].join("|");
}

function repeatKey(item: IncidentSummary): string {
  return [
    item.alert_name,
    item.severity,
    item.source,
    entityIdentityKey(item.source_entity, item.incident_id),
  ].join("|");
}

export interface StatusCount {
  status: IncidentStatus;
  count: number;
}

export interface RepeatGroupSummary {
  total: number;
  /** Lifecycle statuses present in the group, most frequent first. */
  statusCounts: StatusCount[];
  /** True when the group spans more than one lifecycle status. */
  isMixed: boolean;
  reportAvailableCount: number;
  collectorProblemTotal: number;
}

/**
 * Aggregates a repeat group.
 *
 * A run of re-fires is not necessarily homogeneous — a live controlled-fault
 * group can hold REPORTED, ANALYZING and FAILED Incidents at once — so the
 * newest member's state must never be presented as the group's state.
 */
export function summariseRepeatGroup(
  items: readonly IncidentSummary[],
): RepeatGroupSummary {
  const counts = new Map<IncidentStatus, number>();
  let reportAvailableCount = 0;
  let collectorProblemTotal = 0;

  for (const item of items) {
    counts.set(item.status, (counts.get(item.status) ?? 0) + 1);
    if (reportAvailability(item.status) === "AVAILABLE") reportAvailableCount += 1;
    collectorProblemTotal += item.collector_problem_count;
  }

  const statusCounts = [...counts.entries()]
    .map(([status, count]) => ({ status, count }))
    .sort((left, right) =>
      right.count === left.count
        ? left.status.localeCompare(right.status)
        : right.count - left.count,
    );

  return {
    total: items.length,
    statusCounts,
    isMixed: statusCounts.length > 1,
    reportAvailableCount,
    collectorProblemTotal,
  };
}

/**
 * Folds consecutive re-fires of the same alert on the same entity into one row.
 *
 * Controlled-fault harnesses re-fire the same alert repeatedly — a live list is
 * 28 of one verification alert and 9 of another — which crowds out everything
 * else. Runs stay adjacent because the list is ordered by updated_at, and every
 * member remains reachable by expanding the row.
 */
export function groupRepeatedIncidents(items: readonly IncidentSummary[]): RepeatRow[] {
  const rows: RepeatRow[] = [];
  let index = 0;

  while (index < items.length) {
    const item = items[index];
    const key = repeatKey(item);
    const run: IncidentSummary[] = [item];
    let cursor = index + 1;
    while (cursor < items.length && repeatKey(items[cursor]) === key) {
      run.push(items[cursor]);
      cursor += 1;
    }

    if (run.length === 1) {
      rows.push({ kind: "single", key: item.incident_id, item });
    } else {
      rows.push({ kind: "repeat", key: `${key}-${item.incident_id}`, items: run, latest: run[0] });
    }
    index = cursor;
  }

  return rows;
}

/**
 * Whether this page contains any run that can be folded safely.
 *
 * Grouping needs a stable source-Entity identity. A live list whose Kubernetes
 * entities carry no `cluster_id` and a null `uid` cannot be grouped at all, and
 * a "Collapse repeats" control that silently does nothing is worse than one
 * that says why it is unavailable.
 */
export function hasGroupableRepeats(items: readonly IncidentSummary[]): boolean {
  return groupRepeatedIncidents(items).some((row) => row.kind === "repeat");
}

/** True when an Incident's source Entity has an identity safe to group on. */
export function hasStableEntityIdentity(item: IncidentSummary): boolean {
  return !repeatKey(item).includes("|unstable|");
}

export function hasCollectorProblems(item: IncidentSummary): boolean {
  return item.collector_problem_count > 0;
}
