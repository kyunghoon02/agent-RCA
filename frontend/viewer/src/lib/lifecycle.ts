import type {
  EntityRef,
  Incident,
  IncidentStatus,
  TimelineEvent,
  TimelineStage,
} from "./types";
import { isGraphEntityRef } from "./types";

/** The ordered happy path. PARTIAL and FAILED are outcomes, not steps. */
export const LIFECYCLE_STEPS: IncidentStatus[] = [
  "RECEIVED",
  "COLLECTING",
  "LOCALIZING",
  "ANALYZING",
  "REPORTED",
];

export type StepState =
  | "complete"
  | "current"
  | "pending"
  | "failed"
  /** The run ended before this step, so it never ran and never will. */
  | "not-reached";

export interface LifecycleStep {
  status: IncidentStatus;
  state: StepState;
}

/**
 * Every lifecycle status the Incident is known to have entered, read from
 * lifecycle audit events. Used so a terminal PARTIAL/FAILED Incident is drawn
 * at the step it actually stopped on instead of guessing.
 */
export function reachedStatusesFromTimeline(
  timeline: readonly TimelineEvent[],
): IncidentStatus[] {
  const reached = new Set<IncidentStatus>();
  for (const event of timeline) {
    if (event.event_type === "INCIDENT_CREATED") {
      reached.add("RECEIVED");
      continue;
    }
    if (event.event_type !== "STATUS_TRANSITIONED") continue;
    const target = event.details?.to;
    if (typeof target !== "string") continue;
    if ((LIFECYCLE_STEPS as string[]).includes(target)) {
      reached.add(target as IncidentStatus);
    }
  }
  return LIFECYCLE_STEPS.filter((step) => reached.has(step));
}

/**
 * Resolve each step's visual state.
 *
 * `reached` carries the observed transitions. When it is empty the derivation
 * falls back to the linear order, and for a terminal PARTIAL/FAILED Incident
 * that means only RECEIVED is claimed as reached — the Viewer would rather show
 * "not reached" than assert progress it cannot evidence.
 */
export function deriveLifecycle(
  status: IncidentStatus,
  reached: readonly IncidentStatus[] = [],
): LifecycleStep[] {
  const terminalFailure = status === "FAILED" || status === "PARTIAL";
  const reachedSet = new Set<IncidentStatus>(reached);
  reachedSet.add("RECEIVED");

  if (!terminalFailure) {
    // The current status is itself a lifecycle step; everything before it must
    // have been entered to get here.
    const currentIndex = LIFECYCLE_STEPS.indexOf(status);
    return LIFECYCLE_STEPS.map((step, index) => {
      if (index < currentIndex) return { status: step, state: "complete" as const };
      if (index === currentIndex) {
        return {
          status: step,
          state: (status === "REPORTED" ? "complete" : "current") as StepState,
        };
      }
      return { status: step, state: "pending" as const };
    });
  }

  // Terminal failure: the last observed step is where the run stopped.
  const stoppedIndex = LIFECYCLE_STEPS.reduce(
    (last, step, index) => (reachedSet.has(step) ? index : last),
    0,
  );
  return LIFECYCLE_STEPS.map((step, index) => {
    if (index < stoppedIndex) return { status: step, state: "complete" as const };
    if (index === stoppedIndex) return { status: step, state: "failed" as const };
    return { status: step, state: "not-reached" as const };
  });
}

/** Steps the Incident entered, for the "n of 5" summary. */
export function completedStepCount(steps: readonly LifecycleStep[]): number {
  return steps.filter((step) => step.state === "complete").length;
}

export const STAGE_LABELS: Record<TimelineStage, string> = {
  DETECTION: "Detection",
  COLLECTION: "Collection",
  LOCALIZATION: "Localization",
  ANALYSIS: "Analysis",
  REPORT: "Report",
};

/** Namespace of an Incident's source Entity, across both entity-ref branches. */
export function entityNamespace(entity: EntityRef): string | null {
  if (isGraphEntityRef(entity)) {
    const scoped = entity.scope?.["namespace"];
    return typeof scoped === "string" ? scoped : null;
  }
  return entity.namespace;
}

export function entityKind(entity: EntityRef): string {
  return isGraphEntityRef(entity) ? entity.entity_type : entity.kind;
}

/** "Pod/checkoutservice-abc" — stable, compact identity for dense tables. */
export function entityLabel(entity: EntityRef): string {
  return `${entityKind(entity)}/${entity.name}`;
}

export function incidentNamespace(incident: Incident): string | null {
  return (
    entityNamespace(incident.source_entity) ??
    incident.alert.labels["namespace"] ??
    null
  );
}
